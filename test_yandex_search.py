"""
Тесты фазы 1.5 -- интеграция Яндекс.Поиска для новостных каналов.

Без сети: HTTP и LLM перехватываются monkeypatching-ом. Проверяем:
  1. Парсинг XML выдачи (документы, hlword, modtime, пустая выдача code=15,
     ошибка сервиса).
  2. Логику свежести has_fresh_results.
  3. format_search_context.
  4. generate_post (провайдер yandex): выдача попадает в user-промпт как
     источники, стоимость поиска учтена в токенах, деградация при сбое.
  5. check_news_available (провайдер yandex): свежие есть / нет / поиск упал.

Запуск:

    DATABASE_URL=sqlite:///test_ys.db SECRET_KEY=testsecret \\
        python3 test_yandex_search.py
"""

import asyncio
import os
from datetime import datetime, timedelta, timezone

os.environ.setdefault("DATABASE_URL", "sqlite:///test_ys.db")
os.environ.setdefault("SECRET_KEY", "testsecret")
os.environ.setdefault("TRUEPOST_INTERNAL_API_TOKEN", "test-token")

import config
import generator
import yandex_search
from database import Channel

PASSED = []
FAILED = []


def check(name, cond, detail=""):
    if cond:
        PASSED.append(name)
        print(f"  ✓ {name}")
    else:
        FAILED.append(name)
        print(f"  ✗ {name} {detail}")


def _make_channel(**kw) -> Channel:
    defaults = dict(
        user_id=1, title="Новости M&A", tg_chat="", about="сделки M&A в России",
        style="", style_profile="", post_length="100 слов",
        language="русский", channel_type="news",
        use_web_search=True, verified=False,
    )
    defaults.update(kw)
    ch = Channel(**defaults)
    ch.id = 42
    return ch


def _dt_str(dt: datetime) -> str:
    return dt.strftime("%Y%m%dT%H%M%S")


def _sample_xml(modtime_recent: str, modtime_old: str) -> str:
    return f"""<?xml version="1.0" encoding="utf-8"?>
<yandexsearch version="1.0">
<response>
<results><grouping>
<group>
  <doc>
    <url>https://example.com/deal1</url>
    <title>Крупная <hlword>сделка</hlword> на рынке</title>
    <headline>Компания А купила компанию Б</headline>
    <modtime>{modtime_recent}</modtime>
    <passages><passage>Сумма сделки составила 10 млрд рублей, <hlword>M&amp;A</hlword> активность растёт.</passage></passages>
  </doc>
</group>
<group>
  <doc>
    <url>https://example.com/old</url>
    <title>Старая новость</title>
    <modtime>{modtime_old}</modtime>
    <passages><passage>Прошлогодняя сделка.</passage></passages>
  </doc>
</group>
<group>
  <doc>
    <url>https://example.com/nodate</url>
    <title>Без даты</title>
    <passages><passage>Сниппет без modtime.</passage></passages>
  </doc>
</group>
</grouping></results>
</response>
</yandexsearch>"""


EMPTY_XML = """<?xml version="1.0" encoding="utf-8"?>
<yandexsearch version="1.0"><response>
<error code="15">Искомая комбинация слов нигде не встречается</error>
</response></yandexsearch>"""

ERROR_XML = """<?xml version="1.0" encoding="utf-8"?>
<yandexsearch version="1.0"><response>
<error code="32">Превышен лимит запросов</error>
</response></yandexsearch>"""


def test_parse():
    print("\n[1] Парсинг XML выдачи")
    now = datetime.now(timezone.utc)
    xml = _sample_xml(_dt_str(now - timedelta(hours=5)), _dt_str(now - timedelta(days=200)))
    res = yandex_search.parse_search_xml(xml)
    check("три документа", len(res) == 3, f"len={len(res)}")
    check("hlword склеен в title", res[0]["title"] == "Крупная сделка на рынке", res[0]["title"])
    check("headline + passages в snippet",
          "Компания А купила" in res[0]["snippet"] and "10 млрд" in res[0]["snippet"],
          res[0]["snippet"])
    check("modtime распарсен", res[0]["modtime"] is not None)
    check("без modtime -> None", res[2]["modtime"] is None)

    check("code=15 -> пустая выдача", yandex_search.parse_search_xml(EMPTY_XML) == [])
    try:
        yandex_search.parse_search_xml(ERROR_XML)
        check("ошибка сервиса -> SearchUnavailable", False)
    except yandex_search.SearchUnavailable:
        check("ошибка сервиса -> SearchUnavailable", True)
    try:
        yandex_search.parse_search_xml("не xml вообще")
        check("мусор -> SearchUnavailable", False)
    except yandex_search.SearchUnavailable:
        check("мусор -> SearchUnavailable", True)


def test_freshness():
    print("\n[2] Свежесть результатов")
    now = datetime.now(timezone.utc)
    fresh = {"title": "a", "url": "u", "snippet": "s", "modtime": now - timedelta(hours=3)}
    stale = {"title": "b", "url": "u", "snippet": "s", "modtime": now - timedelta(days=30)}
    nodate = {"title": "c", "url": "u", "snippet": "s", "modtime": None}
    check("пустая выдача -> False", yandex_search.has_fresh_results([]) is False)
    check("свежий документ -> True", yandex_search.has_fresh_results([stale, fresh]) is True)
    check("только старые -> False", yandex_search.has_fresh_results([stale]) is False)
    check("без дат -> True (fail-open по дате)", yandex_search.has_fresh_results([nodate]) is True)


def test_format():
    print("\n[3] format_search_context")
    now = datetime.now(timezone.utc)
    ctx = yandex_search.format_search_context([
        {"title": "Заголовок", "url": "https://e.com/1", "snippet": "Текст сниппета", "modtime": now},
        {"title": "Второй", "url": "https://e.com/2", "snippet": "Ещё", "modtime": None},
    ])
    check("заголовок и дата", "Заголовок" in ctx and now.strftime("%d.%m.%Y") in ctx)
    check("url источника", "https://e.com/1" in ctx)
    check("результат без даты без скобок", "Второй\n" in ctx)
    check("пустой список -> пустая строка", yandex_search.format_search_context([]) == "")


async def test_generate_post_integration():
    print("\n[4] generate_post c провайдером yandex")
    generator.FORCE_PROVIDER = "yandex"
    now = datetime.now(timezone.utc)
    captured = {}

    async def fake_yandex_llm(system, messages, max_tokens=700):
        captured["system"] = system
        captured["user"] = messages[-1]["content"]
        # первый вызов -- пост, дальше topic-match check (YES)
        if "Тема:" in messages[-1]["content"]:
            return "YES", 50
        return "<b>Сделка века</b>\n\nКомпания А купила компанию Б за 10 млрд.", 1000

    async def fake_search_ok(topic, max_results=None, **kw):
        captured["search_topic"] = topic
        return [{"title": "Сделка века", "url": "https://e.com/1",
                 "snippet": "А купила Б за 10 млрд рублей", "modtime": now}]

    async def fake_search_fail(topic, max_results=None, **kw):
        raise yandex_search.SearchUnavailable("тест: сеть упала")

    orig_llm = generator._call_yandex
    orig_search = yandex_search.search_news
    try:
        generator._call_yandex = fake_yandex_llm

        # 4a. Поиск отработал -> выдача в промпте, токены поиска учтены
        yandex_search.search_news = fake_search_ok
        ch = _make_channel()
        text, tokens = await generator.generate_post(ch)
        check("тема канала ушла в поиск", captured.get("search_topic") == "сделки M&A в России",
              captured.get("search_topic"))
        # user-промпт последнего "постового" вызова содержал выдачу
        check("выдача попала в источники",
              "ВЫДАЧА ПОИСКА" not in text and tokens >= 1000 + config.YANDEX_SEARCH_TOKEN_COST,
              f"tokens={tokens}")
        check("пост сгенерирован", "Сделка века" in text)

        # 4b. Поиск упал -> генерация без поиска, без падения
        yandex_search.search_news = fake_search_fail
        text2, tokens2 = await generator.generate_post(ch)
        check("деградация при сбое поиска: пост есть", "Сделка века" in text2)
        check("стоимость поиска не списана при сбое", tokens2 < 1000 + config.YANDEX_SEARCH_TOKEN_COST,
              f"tokens={tokens2}")

        # 4c. Явный topic от пользователя + use_web_search -> поиск тоже работает
        yandex_search.search_news = fake_search_ok
        captured.pop("search_topic", None)
        text3, _ = await generator.generate_post(ch, topic="банкротство застройщиков")
        check("поиск по явной теме пользователя",
              captured.get("search_topic") == "банкротство застройщиков",
              captured.get("search_topic"))
        check("источники в user-промпте при topic", "Источники:" in captured["user"] or "Тема:" in captured["user"])
    finally:
        generator._call_yandex = orig_llm
        yandex_search.search_news = orig_search
        generator.FORCE_PROVIDER = None


async def test_check_news_available():
    print("\n[5] check_news_available c провайдером yandex")
    generator.FORCE_PROVIDER = "yandex"
    now = datetime.now(timezone.utc)
    orig_search = yandex_search.search_news
    try:
        ch = _make_channel()

        async def fresh(topic, max_results=None, **kw):
            return [{"title": "t", "url": "u", "snippet": "s", "modtime": now}]
        yandex_search.search_news = fresh
        has, tokens = await generator.check_news_available(ch)
        check("свежие новости -> True", has is True)
        check("проверка стоит токенов поиска", tokens == config.YANDEX_SEARCH_TOKEN_COST, f"tokens={tokens}")

        async def stale(topic, max_results=None, **kw):
            return [{"title": "t", "url": "u", "snippet": "s", "modtime": now - timedelta(days=30)}]
        yandex_search.search_news = stale
        has2, _ = await generator.check_news_available(ch)
        check("только старые -> False (пропуск генерации)", has2 is False)

        async def broken(topic, max_results=None, **kw):
            raise yandex_search.SearchUnavailable("тест")
        yandex_search.search_news = broken
        has3, tokens3 = await generator.check_news_available(ch)
        check("поиск упал -> fail-open True", has3 is True and tokens3 == 0)
    finally:
        yandex_search.search_news = orig_search
        generator.FORCE_PROVIDER = None


async def main():
    test_parse()
    test_freshness()
    test_format()
    await test_generate_post_integration()
    await test_check_news_available()
    print(f"\n{'='*50}\nИтог: {len(PASSED)} прошло, {len(FAILED)} упало")
    if FAILED:
        print("Упавшие:", ", ".join(FAILED))
        raise SystemExit(1)


if __name__ == "__main__":
    asyncio.run(main())


# ── Страница выдачи: корень повторяющихся тем (прод 03.08) ─────────────────

def test_build_body_paging():
    """Страница уходит в запрос. Была прибита к «0», и это оказалось корнем
    повторов: запрос собирается из channel.about, то есть одинаков при каждой
    генерации -- первая страница выдачи тоже одна и та же, и модель раз за
    разом получала те же восемь источников."""
    assert yandex_search._build_body("тема", 8)["query"]["page"] == "0"
    assert yandex_search._build_body("тема", 8, page=3)["query"]["page"] == "3"
    # Отрицательная страница -- не ошибка API, а тихо испорченная выдача.
    assert yandex_search._build_body("тема", 8, page=-2)["query"]["page"] == "0"


async def test_generation_asks_for_a_different_page_as_the_queue_grows(monkeypatch):
    """Сквозная проверка: чем больше постов у канала, тем дальше страница.

    Проверяем именно стык tasks -> generator -> yandex_search. Каждое звено
    по отдельности «работало» и раньше -- страница просто не доезжала до
    запроса, потому что её никто не передавал.
    """
    import database
    import generator
    import tasks
    from database import Channel, Post, User

    seen_pages = []

    async def _classify(_topic):
        return "valid"

    async def _generate(channel, material, topic, rules_text, recent_titles,
                        avoid_text="", search_page=0):
        seen_pages.append(search_page)
        # Тексты должны различаться СЛОВАМИ, а не цифрами: _content_words
        # выбрасывает короткие токены, поэтому «текст 1» и «текст 2» дают
        # одинаковые основы, детектор видит дубль и добавляет лишний вызов
        # перегенерации -- список страниц перестаёт быть тем, что мы меряем.
        bodies = [
            "Кофейня на углу Рубинштейна закрылась после двадцати лет работы.",
            "Ботанический сад раздаёт саженцы редких клёнов всем желающим.",
            "Метро запустило ночные поезда по выходным на фиолетовой ветке.",
            "Городской архив оцифровал переписку купеческих семей девятнадцатого века.",
            "Приют для собак ищет волонтёров выгуливать питомцев по утрам.",
            "Театр кукол показал спектакль на языке жестов для слабослышащих детей.",
            "Пекарня начала печь хлеб на закваске из местной ржаной муки.",
            "Велодорожку вдоль набережной продлили до старого моста.",
        ]
        return bodies[(len(seen_pages) - 1) % len(bodies)], 100

    monkeypatch.setattr(generator, "classify_topic", _classify)
    monkeypatch.setattr(generator, "generate_post", _generate)

    with database.session() as s:
        u = User(email="pagecycle@t.local", password_hash="x", token_balance=100_000)
        s.add(u); s.commit(); s.refresh(u)
        ch = Channel(user_id=u.id, title="Канал", about="тема", tg_chat="@pagecycle",
                     verified=True, enabled=True, use_web_search=True)
        s.add(ch); s.commit(); s.refresh(ch)
        cid, uid = ch.id, u.id

    for _ in range(3):
        r = await tasks.generate_for_channel(cid, respect_queue_depth=False)
        assert r.get("ok") is True, r

    assert seen_pages == [0, 1, 2], (
        f"страницы выдачи не менялись с ростом очереди: {seen_pages}"
    )

    # ВАЖНО: выше подменён сам generate_post, поэтому проверено только звено
    # tasks -> generator. Второе звено (generator -> yandex_search) проверяет
    # test_generator_forwards_the_page_to_search: без него мутация «прибить
    # page=0 внутри generator» не ловилась ничем -- проверено.

    # И цикл не уходит в бесконечность: на дальних страницах выдача мусорная.
    with database.session() as s:
        for i in range(tasks.SEARCH_PAGES_CYCLE + 2):
            s.add(Post(channel_id=cid, user_id=uid, text=f"добивка {i}", status="published"))
        s.commit()
    seen_pages.clear()
    await tasks.generate_for_channel(cid, respect_queue_depth=False)
    assert 0 <= seen_pages[0] < tasks.SEARCH_PAGES_CYCLE, (
        f"страница вышла за цикл: {seen_pages[0]}"
    )


async def test_generator_forwards_the_page_to_search(monkeypatch):
    """Второе звено цепочки: generator обязан донести страницу до поиска.

    Тест выше подменяет generate_post целиком и потому слеп к тому, что
    происходит ВНУТРИ него. Мутация «page=0 прямо в generator» проходила мимо
    обоих тестов -- ровно так исходная ошибка и жила: параметр вроде бы есть,
    а до запроса не доезжает.
    """
    import generator

    seen = {}

    async def fake_search(topic, max_results=None, page=0):
        seen["page"] = page
        return [{"title": "Что-то", "url": "https://e.com/1",
                 "snippet": "текст", "modtime": None}]

    async def fake_llm(system, messages, max_tokens=700):
        if "Тема:" in messages[-1]["content"]:
            return "YES", 10
        return "<b>Заголовок</b>\n\nТекст поста про городские новости.", 100

    monkeypatch.setattr(yandex_search, "search_news", fake_search)
    monkeypatch.setattr(generator, "_call_yandex", fake_llm)
    monkeypatch.setattr(generator, "FORCE_PROVIDER", "yandex")

    await generator.generate_post(_make_channel(), search_page=4)
    assert seen.get("page") == 4, f"страница не доехала до поиска: {seen}"
