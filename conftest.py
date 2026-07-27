"""
Настройка pytest для этого репозитория.

Зачем этот файл вообще появился. Все test_*.py писались как самостоятельные
скрипты под живой сервер: async-функции, httpx и абсолютный BASE_URL. Через
`python3 -m pytest` они не запускались ни одного разу: 21 падение вида
«async def function and no async plugin installed» и 42 ошибки «fixture
'client' not found». То есть автоматической защиты от регрессий у проекта не
было вовсе -- при том, что сами тесты написаны и логику проверяют.

Здесь три вещи, каждая по своей причине:

1. Запуск async-тестов своими силами. pytest-asyncio в окружении нет, и
   тянуть новую зависимость ради этого не хочется: pytest_pyfunc_call ниже
   занимает десять строк и не ломается при смене версий плагина. Цикл
   событий один на всю сессию -- иначе httpx-клиент, созданный в одном
   цикле, нельзя использовать в другом.

2. Фикстура client поднимает приложение прямо в процессе через
   ASGITransport. Не нужен ни uvicorn, ни свободный порт, ни sleep 3 в
   ожидании старта -- прежние инструкции в шапках тестов требовали именно
   этого.

3. Переменные окружения выставляются ДО импорта main. Внутренние роуты
   читают TRUEPOST_INTERNAL_API_TOKEN на импорте модуля, а не на запросе:
   поставь его позже -- и тесты внутренних ручек будут отбиты 401.
"""

import asyncio
import atexit
import inspect
import os
import shutil
import tempfile
import uuid

import pytest

# ── Окружение до импорта приложения ───────────────────────────────────────
# Присваиваем жёстко, а не через setdefault. Причина серьёзнее удобства: если
# в оболочке случайно окажется боевой DATABASE_URL, setdefault оставит его --
# и тесты начнут регистрировать пользователей и писать посты в прод. Ценой
# этого решения теряется возможность прогнать тесты на своей БД через
# переменную окружения; кому нужно -- правит эту строку осознанно.
_TMP_DIR = tempfile.mkdtemp(prefix="autopost-pytest-")
# PYTEST_DATABASE_URL -- осознанный способ прогнать тесты на настоящем
# Postgres (нарушения FK на SQLite видны не все, см. ниже). Отдельное имя, а
# не DATABASE_URL, именно чтобы боевой URL нельзя было подставить случайно:
# его сюда надо вписать руками.
os.environ["DATABASE_URL"] = os.environ.get(
    "PYTEST_DATABASE_URL", f"sqlite:///{_TMP_DIR}/test.db"
)
os.environ["SECRET_KEY"] = "testsecret"
os.environ["TRUEPOST_INTERNAL_API_TOKEN"] = "test-token"
# Тесты собирают адреса как f"{BASE_URL}/api/...". Хост произвольный: весь
# трафик клиента идёт в ASGITransport и наружу не выходит.
os.environ["BASE_URL"] = "http://testserver"
# Фоновая генерация в тестах не нужна и стоит реальных денег. Планировщик и
# так не стартует (ASGITransport не шлёт lifespan-события), но если кто-то
# поднимет приложение иначе -- пусть тик будет заведомо недостижимым.
os.environ["TICK_SECONDS"] = "99999"

atexit.register(lambda: shutil.rmtree(_TMP_DIR, ignore_errors=True))

import httpx  # noqa: E402

import database  # noqa: E402
import main  # noqa: E402

# SQLite по умолчанию НЕ проверяет внешние ключи, и это уже стоило нам трёх
# аварий подряд: удаление аккаунта проходило локально и падало в проде на
# Postgres (User.referred_by, IdempotencyKey, PostApproval). Включаем проверку
# на каждом соединении, чтобы обычный прогон вёл себя как прод.
if database.engine.dialect.name == "sqlite":
    from sqlalchemy import event as _sa_event

    @_sa_event.listens_for(database.engine, "connect")
    def _enable_sqlite_fk(dbapi_connection, _record):
        cur = dbapi_connection.cursor()
        cur.execute("PRAGMA foreign_keys=ON")
        cur.close()

# lifespan приложения через ASGITransport не выполняется, а таблицы создаёт
# именно он. Создаём схему сами -- заодно не запускаем планировщик.
database.init_db()

# Один цикл на всю сессию: фикстуры и тесты обязаны работать в одном и том же,
# иначе httpx.AsyncClient падает с «attached to a different loop».
_LOOP = asyncio.new_event_loop()
asyncio.set_event_loop(_LOOP)
atexit.register(_LOOP.close)


@pytest.hookimpl(tryfirst=True)
def pytest_pyfunc_call(pyfuncitem):
    """Выполняет async-тесты. Синхронные отдаёт стандартному механизму pytest."""
    func = pyfuncitem.obj
    if not inspect.iscoroutinefunction(func):
        return None
    kwargs = {name: pyfuncitem.funcargs[name] for name in pyfuncitem._fixtureinfo.argnames}
    _LOOP.run_until_complete(func(**kwargs))
    return True


def make_client() -> httpx.AsyncClient:
    """
    HTTP-клиент к приложению, поднятому в этом же процессе.

    Вынесено в функцию, а не только в фикстуру, потому что часть тестов
    создаёт клиент сама внутри `async with` (test_user_events.py) -- им нужен
    тот же транспорт, иначе запрос уходит в реальную сеть на localhost:8000.
    """
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=main.app),
        base_url=os.environ["BASE_URL"],
        timeout=30,
    )


@pytest.fixture
def client():
    """
    Фикстура синхронная намеренно: httpx.AsyncClient создаётся без работающего
    цикла событий, а закрывать его нечем -- ASGITransport не держит сокетов,
    закрывать нужно только пул соединений, которого здесь нет.
    """
    return make_client()


@pytest.fixture
def token(client):
    """Токен свежезарегистрированного пользователя -- для тестов полного HTTP-пути."""
    async def _register():
        r = await client.post(
            "/api/register",
            json={"email": f"fixture_{uuid.uuid4().hex[:12]}@test.local", "password": "test12345"},
        )
        r.raise_for_status()
        return r.json()["token"]

    return _LOOP.run_until_complete(_register())
