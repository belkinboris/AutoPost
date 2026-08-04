"""
Миграции схемы и живучесть ответа при сломанной схеме.

Прод-инцидент 03.08. Миграция `duplicate_suspected` была написана как
`BOOLEAN NOT NULL DEFAULT 0`. SQLite проглатывает 0 как булев ноль, Postgres
отвергает: «column is of type boolean but default expression is of type
integer». `_add_missing_columns` глушит исключения (правильно — упавшая
миграция не должна ронять сервис), поэтому приложение стартовало со схемой
без колонки. Дальше любой запрос к постам падал с UndefinedColumn: модель
колонку уже объявила, SQLAlchemy перечисляет её в каждом SELECT.

У владельца это выглядело так: на дашборде первый канал целый, а два
следующих — без названия, без хэндла, «Канал ещё не подключён». Данные были
целы, врал ответ API (см. test_one_broken_channel_does_not_blank_the_others).

Здесь же -- второе расхождение движков, найденное тем же прогоном на
Postgres: `ORDER BY ... DESC` ставит там NULL ПЕРВЫМИ, а SQLite -- последними.
См. test_next_queue_slot_ignores_posts_without_time.

Все тесты идут на SQLite, кроме отмеченных: на нём воспроизвести эти ошибки
нельзя, поэтому проверяем тексты миграций и форму запроса.
"""

import os
import re
from pathlib import Path

import pytest
from sqlmodel import SQLModel, select

import database
from database import Channel, Post, User

SRC = Path(__file__).parent / "database.py"


def test_no_boolean_migration_uses_numeric_default():
    """Лобовая проверка текста миграций — единственная, которая ловит эту
    ошибку без живого Postgres. Все булевы миграции в файле с самого начала
    писались `DEFAULT FALSE`; выбился ровно один, и он уехал в прод."""
    bad = re.findall(r"ALTER TABLE[^\"']*BOOLEAN[^\"']*DEFAULT\s+(\d+)", SRC.read_text())
    assert not bad, (
        f"булева колонка с числовым DEFAULT {bad} — на Postgres такая миграция "
        f"падает, пишите DEFAULT FALSE/TRUE"
    )


def test_migrations_leave_no_missing_columns():
    """После create_all + миграций схема обязана быть полной. На SQLite это
    почти тавтология, но функция `missing_columns` — то самое, что теперь
    кричит в лог при старте, и её собственная поломка была бы незаметна."""
    database.init_db()
    assert database.missing_columns() == {}


def test_missing_columns_actually_detects_drift():
    """Обратная сторона: проверка должна УМЕТЬ находить пропажу, иначе она
    зелёная всегда и не значит ничего. Инцидент случился именно потому, что
    никакая проверка схемы не запускалась вовсе."""
    fake = type("FakeCol", (), {"name": "колонки_такой_нет"})()
    table = SQLModel.metadata.tables["post"]
    table.append_column.__self__  # noqa: B018 -- только чтобы не молчать про импорт
    real = database.missing_columns()
    assert real == {}, f"схема уже неполная до подмены: {real}"

    original = database.inspect

    class _Inspector:
        def get_table_names(self):
            return list(SQLModel.metadata.tables)

        def get_columns(self, name):
            cols = [{"name": c.name} for c in SQLModel.metadata.tables[name].columns]
            return cols[:-1] if name == "post" else cols

    database.inspect = lambda _engine: _Inspector()
    try:
        drift = database.missing_columns()
    finally:
        database.inspect = original
    assert "post" in drift and drift["post"], "пропажа колонки не обнаружена"


@pytest.mark.skipif(
    not os.getenv("PYTEST_DATABASE_URL", "").startswith("postgres"),
    reason="нужен настоящий Postgres: PYTEST_DATABASE_URL=postgresql://...",
)
def test_all_migrations_run_on_postgres():
    """Единственный тест, который поймал бы инцидент по-настоящему. Гоняется
    только когда дали настоящий Postgres — см. CLAUDE.md, там это уже
    записано как шаг перед выкладкой (и именно его я в тот раз пропустил)."""
    database.init_db()
    database._add_missing_columns()  # идемпотентность: второй прогон не ломается
    assert database.missing_columns() == {}


async def test_one_broken_channel_does_not_blank_the_others(client, token):
    """Главный урок инцидента, отдельный от самой миграции.

    В `_channel_dict` при ошибке стоял `s.rollback()` — он спасал от
    «current transaction is aborted» для следующего канала, но rollback
    протухает ВСЕ объекты сессии, а список каналов загружен заранее. Для
    протухшего объекта `model_dump()` не подтягивает данные заново, а читает
    пустой __dict__ и отдаёт None во всех полях. Один сбой обнулял все
    следующие карточки.

    Проверяем инвариант, а не ту конкретную ошибку: что бы ни случилось при
    обогащении карточки одного канала, названия остальных обязаны доехать.
    """
    hdr = {"Authorization": f"Bearer {token}"}
    # Лимит каналов на бесплатном тарифе -- отмечаем оплату, иначе второй и
    # третий канал просто не создадутся и тест проверит пустоту.
    r = await client.get("/api/me", headers=hdr)
    with database.session() as s:
        u = s.exec(select(User).where(User.email == r.json()["email"])).first()
        s.add(database.Payment(user_id=u.id, package_id="p2", label="test", rub=990,
                               tokens=1_500_000, status="paid", operation_id="op-blank-1"))
        s.commit()
    for i in range(3):
        r = await client.post("/api/channels", json={
            "title": f"Канал {i}", "about": "тема", "tg_chat": f"@blank_test_{i}",
        }, headers=hdr)
        r.raise_for_status()

    import main

    original = main.tasks.queue_target_for_user
    calls = {"n": 0}

    def _boom(s, user_id, channel=None):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("сломалось на первом канале")
        return original(s, user_id, channel)

    main.tasks.queue_target_for_user = _boom
    try:
        r = await client.get("/api/channels", headers=hdr)
        r.raise_for_status()
        got = r.json()
    finally:
        main.tasks.queue_target_for_user = original

    blanked = [c for c in got if c.get("title") is None]
    assert not blanked, (
        f"{len(blanked)} каналов вернулись без названия из-за сбоя на другом канале"
    )
    assert all(c.get("tg_chat") is not None for c in got), "у каналов пропали хэндлы"


# ── Различия движков: сортировка NULL ──────────────────────────────────────

def test_next_queue_slot_query_excludes_posts_without_time():
    """Форма запроса, а не только поведение.

    Поведенческий тест ниже падает лишь на Postgres, а обычный прогон идёт на
    SQLite -- значит защита от повторной ошибки должна быть и здесь. Проверяем,
    что `_next_queue_slot` явно отбрасывает посты без времени: без этого
    условия результат зависит от того, куда движок кладёт NULL при сортировке
    по убыванию, а движки расходятся.
    """
    import tasks
    from sqlmodel import select as sel
    from database import Post

    # Тот же запрос, что внутри _next_queue_slot -- сверяем, что условие есть.
    src = (Path(__file__).parent / "tasks.py").read_text()
    body = src[src.index("def _next_queue_slot("):]
    body = body[:body.index("\ndef ", 1)]
    assert "scheduled_at.is_not(None)" in body, (
        "_next_queue_slot больше не отбрасывает посты без времени -- на Postgres "
        "он снова начнёт считать очередь пустой и ставить все посты на «сейчас»"
    )


async def test_next_queue_slot_ignores_posts_without_time():
    """Поведение. Пост со статусом «scheduled», но без времени существует одно
    мгновение -- в sync_posts_to_channel_mode между присвоением статуса и
    времени, и autoflush успевает его записать. На Postgres такой пост
    оказывался первым в сортировке по убыванию, очередь считалась пустой, и
    каждый следующий черновик вставал на «сейчас»: при включении автопилота с
    несколькими черновиками они уходили подписчикам пачкой.
    """
    import tasks
    from datetime import datetime, timedelta

    with database.session() as s:
        u = User(email="nullorder@t.local", password_hash="x", token_balance=1000)
        s.add(u); s.commit(); s.refresh(u)
        ch = Channel(user_id=u.id, title="Канал", about="тема", tg_chat="@nullorder",
                     verified=True, enabled=True, schedule_kind="interval",
                     interval_hours=6, interval_jitter_minutes=0,
                     publish_window_start="", publish_window_end="")
        s.add(ch); s.commit(); s.refresh(ch)
        anchor = datetime.utcnow() + timedelta(hours=2)
        s.add(Post(channel_id=ch.id, user_id=u.id, text="в очереди",
                   status="scheduled", scheduled_at=anchor))
        # Тот самый промежуточный пост: статус уже проставлен, времени ещё нет.
        s.add(Post(channel_id=ch.id, user_id=u.id, text="без времени",
                   status="scheduled", scheduled_at=None))
        s.commit()
        cid, expected = ch.id, anchor

    with database.session() as s:
        slot = tasks._next_queue_slot(s, s.get(Channel, cid))

    delta_h = (slot - expected).total_seconds() / 3600
    assert 5.9 <= delta_h <= 6.1, (
        f"слот посчитан не от последнего поста очереди, а от «сейчас»: +{delta_h:.2f}ч"
    )
