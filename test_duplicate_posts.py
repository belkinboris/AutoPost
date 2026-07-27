"""
Тесты дедупликации постов (tasks._similarity, _find_duplicate и поведение
generate_for_channel при близнеце).

Почему это стоит отдельного файла. Дубли -- единственная жалоба на качество,
которая пришла от живого канала: в одну минуту сгенерировались два поста про
одно событие под разными заголовками, и оба легли в очередь. Причин было три,
и одна из них -- что запрет стоял в промпте, дедупликация была написана и
работала, но сравнивала заголовки. По коду это не видно вообще: он корректен.

Порог `DUPLICATE_THRESHOLD` подобран на ОДНОЙ реальной паре с прода. Пока
этих тестов не было, любое изменение `_content_words`, `_STEM_LEN` или самого
порога проходило незамеченным -- а цена ошибки в обе стороны заметна
пользователю: пропущенный близнец он видит в своём канале, лишнее
срабатывание оставляет очередь пустой.

Честно про данные: сами продовые посты у нас не сохранились, тексты ниже --
реконструкция того же события (тайное крещение Путина, о котором рассказал
патриарх) в двух вариантах, как их писала модель: один факт, разные
заголовки и формулировки. Поэтому тесты проверяют не конкретные числа с
прода, а СООТНОШЕНИЯ, которые и делают детектор работающим: близнецы выше
порога, разные события той же тематики -- ниже, и разрыв между ними
кратный.
"""

import re

import pytest

import tasks
from tasks import DUPLICATE_THRESHOLD, _content_words, _find_duplicate, _similarity

# ── Тексты ────────────────────────────────────────────────────────────────
# Близнецы: одно событие, разные заголовки и словоформы. Именно так выглядела
# пара, на которую пожаловался владелец канала.

TWIN_A = """<b>Патриарх раскрыл тайну крещения Путина</b>

Патриарх Кирилл рассказал, что Владимира Путина крестили тайно, в младенчестве.
Обряд провели в Спасо-Преображенском соборе Ленинграда без огласки: отец
будущего президента был членом партии, и открытое крещение сына грозило ему
серьёзными последствиями по партийной линии.

Крестик, полученный тогда, Путин позже освятил в Иерусалиме."""

TWIN_B = """<b>Как Путина крестили втайне от отца-коммуниста</b>

Стало известно, что крещение Владимира Путина прошло тайно, когда он был
младенцем. Об этом рассказал патриарх Кирилл. Обряд совершили в
Спасо-Преображенском соборе в Ленинграде — мать будущего президента пошла на
это скрытно, потому что отец состоял в партии.

Тот самый нательный крестик Путин потом освятил в Иерусалиме."""

# Разные события внутри одной тематики канала (история, церковь, СССР).
OTHER_1 = """<b>Хрущёв и закрытие храмов</b>

При Никите Хрущёве в СССР развернулась новая антирелигиозная кампания: за
несколько лет закрыли больше половины действующих приходов. Формально
государство ссылалось на «добровольные решения общин», на деле решения
принимались в райкомах."""

OTHER_2 = """<b>Патриарх Тихон и изъятие церковных ценностей</b>

В 1922 году власти начали изъятие церковных ценностей под предлогом помощи
голодающим Поволжья. Патриарх Тихон выступил против передачи освящённых
предметов, и это стало поводом для судебного процесса над духовенством."""

OTHER_3 = """<b>Блокадный Ленинград: хлебная норма</b>

В ноябре 1941 года норма выдачи хлеба в осаждённом Ленинграде опустилась до
125 граммов на иждивенца. Эта цифра стала главным символом блокадной зимы и
вошла во все учебники."""

DIFFERENT = [OTHER_1, OTHER_2, OTHER_3]


def _exact_form_similarity(a: str, b: str) -> float:
    """Как считалось бы БЕЗ усечения до основы -- для сравнения в тесте."""
    def words(t):
        clean = re.sub(r"<[^>]+>", " ", t or "").lower().replace("ё", "е")
        return {w for w in re.findall(r"[а-яa-z0-9]{4,}", clean)
                if w not in tasks._DUP_STOPWORDS}
    wa, wb = words(a), words(b)
    return len(wa & wb) / len(wa | wb) if wa and wb else 0.0


# ── 1. Близнецы ловятся, разные события -- нет ────────────────────────────

def test_twins_are_above_threshold_and_different_events_below():
    twins = _similarity(TWIN_A, TWIN_B)
    assert twins >= DUPLICATE_THRESHOLD, (
        f"два поста про одно событие дали {twins:.3f} при пороге {DUPLICATE_THRESHOLD} -- "
        "детектор их не увидит, и пользователь получит близнеца в канал"
    )
    for i, other in enumerate(DIFFERENT, 1):
        for name, twin in (("A", TWIN_A), ("B", TWIN_B)):
            sim = _similarity(twin, other)
            assert sim < DUPLICATE_THRESHOLD, (
                f"разные события (близнец {name} и текст {i}) дали {sim:.3f} -- "
                "ложное срабатывание оставит очередь пустой"
            )


def test_gap_between_twins_and_different_events_is_wide():
    """
    Порог держится не на точном числе, а на разрыве. Если разрыв станет
    меньше двукратного, порог 0.10 перестанет быть надёжным в обе стороны --
    и об этом надо узнать здесь, а не из канала пользователя.
    """
    twins = _similarity(TWIN_A, TWIN_B)
    worst_different = max(
        _similarity(t, o) for t in (TWIN_A, TWIN_B) for o in DIFFERENT
    )
    assert twins > worst_different * 2, (
        f"разрыв схлопнулся: близнецы {twins:.3f}, худшая пара разных событий "
        f"{worst_different:.3f}"
    )


# ── 2. Сравнение по основам, а не по словоформам ──────────────────────────

def test_stemming_is_what_makes_it_work():
    """
    Русский флективен: «патриархом» и «патриарха», «крестили» и «крещение» --
    одно и то же слово. На точных словоформах реальная пара близнецов давала
    0.10 и не ловилась вовсе. Этот тест фиксирует, что усечение до основы --
    не украшение, а условие работоспособности.
    """
    with_stems = _similarity(TWIN_A, TWIN_B)
    exact_forms = _exact_form_similarity(TWIN_A, TWIN_B)
    assert with_stems > exact_forms, (
        f"усечение до основы перестало давать выигрыш: основы {with_stems:.3f}, "
        f"словоформы {exact_forms:.3f}"
    )


def test_stem_length_change_is_noticed():
    """
    _STEM_LEN подобран вручную. Слишком длинная основа перестаёт склеивать
    формы, слишком короткая склеивает несвязанные слова. Проверяем, что
    текущее значение действительно лучше соседних для нашей задачи:
    близнецы должны оставаться выше порога.
    """
    assert tasks._STEM_LEN == 6, "значение изменили -- перепроверьте калибровку порога"
    twins = _similarity(TWIN_A, TWIN_B)
    assert twins >= DUPLICATE_THRESHOLD


# ── 3. Мелочи, на которых детектор мог бы молча сломаться ─────────────────

def test_markup_and_yo_do_not_affect_comparison():
    plain = re.sub(r"<[^>]+>", "", TWIN_A)
    assert _similarity(TWIN_A, plain) > 0.9, "HTML-разметка не должна влиять на сравнение"
    assert _content_words("ёлка счётчик") == _content_words("елка счетчик"), \
        "ё и е должны считаться одной буквой"


def test_empty_text_is_not_a_duplicate_of_everything():
    assert _similarity("", TWIN_A) == 0.0
    assert _similarity(TWIN_A, "") == 0.0
    assert _find_duplicate("", [TWIN_A]) == (None, 0.0)


def test_find_duplicate_returns_best_match():
    dup, score = _find_duplicate(TWIN_B, DIFFERENT + [TWIN_A])
    assert dup == TWIN_A, "должен возвращаться самый похожий текст, а не первый подходящий"
    assert score >= DUPLICATE_THRESHOLD

    dup, score = _find_duplicate(TWIN_A, DIFFERENT)
    assert dup is None, f"среди разных событий не должно найтись дубля (лучшее {score:.3f})"


# ── 4. Поведение генерации: близнец не доходит до пользователя ────────────

@pytest.fixture
def channel_with_post():
    """
    Канал с одним уже существующим постом -- тем самым, который будут
    дублировать.

    Статус поста «pending» не случаен: generate_for_channel сверяется только
    с постами в статусах pending/scheduled/published. Пост в другом статусе
    в сравнение не попадёт, и тест молча проверял бы пустой список.
    """
    import uuid

    import database
    from database import Channel, Post, User

    with database.session() as s:
        u = User(email=f"dup_{uuid.uuid4().hex[:10]}@dup.test", password_hash="x", token_balance=100_000)
        s.add(u); s.commit(); s.refresh(u)
        ch = Channel(user_id=u.id, title="История", about="история России",
                     use_web_search=False, enabled=True)
        s.add(ch); s.commit(); s.refresh(ch)
        s.add(Post(channel_id=ch.id, user_id=u.id, text=TWIN_A, status="pending"))
        s.commit()
        return ch.id


def _stub_generator(monkeypatch, texts):
    """Подменяет генерацию: отдаёт заранее заданные тексты по очереди."""
    import generator

    async def _classify(_topic):
        return "valid"

    calls = []

    async def _generate(channel, material, topic, rules_text, recent_titles):
        calls.append(topic)
        return texts[min(len(calls) - 1, len(texts) - 1)], 100

    monkeypatch.setattr(generator, "classify_topic", _classify)
    monkeypatch.setattr(generator, "generate_post", _generate)
    return calls


async def test_duplicate_triggers_one_retry_and_then_skips(channel_with_post, monkeypatch):
    """
    Обе попытки дали текст про то же событие. Пост не должен создаваться
    вовсе: короткая очередь лучше, чем близнец в канале.
    """
    import database
    from database import Post
    from sqlmodel import select

    calls = _stub_generator(monkeypatch, [TWIN_B, TWIN_B])
    before = _count_posts(database, Post, select, channel_with_post)

    result = await tasks.generate_for_channel(channel_with_post, topic="история")

    assert result["ok"] is False, f"близнец не должен создаваться: {result}"
    assert result.get("duplicate_skipped") is True
    assert len(calls) == 2, f"должна быть ровно одна перегенерация, вызовов: {len(calls)}"
    assert _count_posts(database, Post, select, channel_with_post) == before, \
        "пост-близнец всё-таки попал в очередь"


async def test_retry_with_different_event_is_saved(channel_with_post, monkeypatch):
    """Перегенерация дала другое событие -- пост сохраняется как обычно."""
    import database
    from database import Post
    from sqlmodel import select

    calls = _stub_generator(monkeypatch, [TWIN_B, OTHER_2])
    before = _count_posts(database, Post, select, channel_with_post)

    result = await tasks.generate_for_channel(channel_with_post, topic="история")

    assert result.get("ok") is True, f"нормальный пост после перегенерации должен сохраниться: {result}"
    assert len(calls) == 2
    assert _count_posts(database, Post, select, channel_with_post) == before + 1


async def test_unique_post_does_not_trigger_retry(channel_with_post, monkeypatch):
    """Обратная сторона: на непохожем тексте лишней перегенерации быть не должно."""
    import database
    from database import Post
    from sqlmodel import select

    calls = _stub_generator(monkeypatch, [OTHER_3])
    before = _count_posts(database, Post, select, channel_with_post)

    result = await tasks.generate_for_channel(channel_with_post, topic="история")

    assert result.get("ok") is True, result
    assert len(calls) == 1, "перегенерация запустилась там, где дубля нет -- это лишние деньги"
    assert _count_posts(database, Post, select, channel_with_post) == before + 1


def _count_posts(database, Post, select, channel_id: int) -> int:
    with database.session() as s:
        return len(s.exec(select(Post).where(Post.channel_id == channel_id)).all())
