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


def test_short_words_are_actually_stemmed():
    """
    Аудит 02.08: при _STEM_LEN=6 усечение не делало НИЧЕГО для слов из 5-6
    букв -- а это самые содержательные признаки «пост про то же самое».
    «Путин»/«Путина», «друг»/«друга», «двор»/«двора» оставались разными
    токенами, то есть два сильнейших сигнала не засчитывались вовсе.
    """
    for base, inflected in [("путин", "путина"), ("друг", "друга"),
                             ("двор", "двора"), ("школа", "школы")]:
        assert _content_words(base) == _content_words(inflected), (
            f"«{base}» и «{inflected}» считаются разными словами -- "
            f"детектор не увидит, что посты про одно и то же"
        )

    # Честное ограничение метода: беглая гласная («отец»/«отца») префиксным
    # усечением не склеивается ни при какой длине основы. Лечится только
    # настоящей лемматизацией (словарь/pymorphy) -- отдельная зависимость,
    # которую сюда не тянем. Фиксируем как известную дыру, а не как «работает».
    assert _content_words("отец") != _content_words("отца")


def test_storytelling_twins_are_caught():
    """
    Реальная пара с прода 02.08, которую владелец увидел в своей очереди:
    один и тот же эпизод биографии, пересказанный другими словами. Жаккар на
    ней давал 0.080 при пороге 0.10 -- пропуск. Именно ради таких пар
    добавлен коэффициент перекрытия.
    """
    a = ("Он выходил один против пятерых. И не боялся.\n\n"
         "Вот вам история, которую я недавно нашел в воспоминаниях друга детства Путина. "
         "Владимир Владимирович с ранних лет не давал себя в обиду: если задирали во дворе, "
         "отвечал сразу, не считая, сколько человек против него.")
    b = ("Он не боялся выйти один против пятерых. И это не про политику\n\n"
         "В школе Володя Путин не был драчуном, но и спуску не давал. Его друг детства вспоминал: "
         "во дворе мальчишки быстро поняли, что связываться не стоит -- характер проявился рано, "
         "и отвечал он сразу, даже если противников было пятеро.")
    assert tasks._is_duplicate(a, b), (
        f"пара близнецов-сторителлинга не поймана: "
        f"Жаккар {_similarity(a, b):.3f}, перекрытие {tasks._overlap(a, b):.3f}"
    )


def test_different_events_still_not_flagged_by_either_metric():
    """Обратная сторона: смягчение метрики не должно начать браковать разные события."""
    for i, first in enumerate(DIFFERENT):
        for second in DIFFERENT[i + 1:]:
            assert not tasks._is_duplicate(first, second), (
                f"разные события помечены дублем: Жаккар {_similarity(first, second):.3f}, "
                f"перекрытие {tasks._overlap(first, second):.3f}"
            )


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

    async def _generate(channel, material, topic, rules_text, recent_titles, avoid_text="", **kw):
        calls.append(topic)
        return texts[min(len(calls) - 1, len(texts) - 1)], 100

    monkeypatch.setattr(generator, "classify_topic", _classify)
    monkeypatch.setattr(generator, "generate_post", _generate)
    return calls


async def test_duplicate_triggers_one_retry_and_is_saved_with_a_warning(channel_with_post, monkeypatch):
    """
    Обе попытки дали текст про то же событие -- пост всё равно создаётся,
    но с пометкой «похоже на дубль».

    ЭТОТ ТЕСТ ПЕРЕВЁРНУТ 03.08, и вот почему. Раньше он закреплял ровно
    обратное: «пост не должен создаваться вовсе, короткая очередь лучше, чем
    близнец в канале». В проде это дало отказ КАЖДОМУ посту подряд на канале,
    где все посты про одного человека: детектор не отличает там пересказ от
    другого эпизода (см. таблицу замеров в tasks.py). Очередь не пополнялась,
    тик пробовал заново каждую минуту, около 10 000 токенов в минуту сгорало
    молча, а на экране мигало «генерируется…».

    Правильная развилка не «создать или нет», а «решить за человека или
    показать ему». Показываем: пост есть, пометка есть, оба текста рядом --
    дальше он решает сам (сквозной принцип из CLAUDE.md).
    """
    import database
    from database import Post
    from sqlmodel import select

    calls = _stub_generator(monkeypatch, [TWIN_B, TWIN_B])
    before = _count_posts(database, Post, select, channel_with_post)

    result = await tasks.generate_for_channel(channel_with_post, topic="история")

    assert result.get("ok") is True, f"пост обязан создаться даже при подозрении: {result}"
    assert len(calls) == 2, f"должна быть ровно одна перегенерация, вызовов: {len(calls)}"
    assert _count_posts(database, Post, select, channel_with_post) == before + 1

    with database.session() as s:
        newest = s.exec(
            select(Post).where(Post.channel_id == channel_with_post)
            .order_by(Post.created_at.desc())
        ).first()
        assert newest.duplicate_suspected is True, "пометка «похоже на дубль» не выставлена"


async def test_generation_never_dead_ends_on_duplicates(channel_with_post, monkeypatch):
    """Главная защита от прод-инцидента 03.08: сколько бы близнецов подряд ни
    выдала модель, каждая генерация ЗАВЕРШАЕТСЯ созданием поста.

    Проверяем именно это, а не пометку: цикл в проде крутился потому, что
    очередь не росла -- пока пост создаётся, тик успокаивается сам, и не важно,
    насколько точен детектор.
    """
    import database
    from database import Post
    from sqlmodel import select

    _stub_generator(monkeypatch, [TWIN_B, TWIN_B])
    before = _count_posts(database, Post, select, channel_with_post)

    for i in range(3):
        # respect_queue_depth=False -- иначе на третьем круге сработает гейт
        # глубины очереди и тест перестанет проверять то, ради чего написан.
        # В самом инциденте очередь была неполной (3 из 4), поэтому гейт не
        # спасал: тик пробовал снова каждую минуту.
        result = await tasks.generate_for_channel(channel_with_post, topic="история",
                                                  respect_queue_depth=False)
        assert result.get("ok") is True, f"попытка {i + 1} закончилась отказом: {result}"

    assert _count_posts(database, Post, select, channel_with_post) == before + 3, \
        "очередь не выросла -- значит цикл «сгенерировали и выбросили» вернулся"


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


# ── 5. Чужая письменность (аудит 02.08) ───────────────────────────────────

def test_foreign_script_is_detected():
    """
    Реальный дефект с прода: «В 1989 году在东德 Дрездене Владимир Путин…».
    Проверки языка в проекте не было ни одной -- ни на входе, ни на выходе.
    """
    assert tasks._foreign_script_chars("В 1989 году在东德 Дрездене") == "在东德"
    assert tasks._foreign_script_chars("Обычный русский текст без вкраплений") == ""
    assert tasks._foreign_script_chars("Latin text is fine too") == ""
    # Одного символа достаточно: вкрапление всегда короткое, доля от длины
    # текста его бы не поймала.
    assert tasks._foreign_script_chars("Совершенно нормальный длинный русский пост про историю 中") == "中"


def test_foreign_script_covers_main_scripts():
    for sample in ["漢字", "ひらがな", "カタカナ", "한글", "العربية", "עברית", "ไทย"]:
        assert tasks._foreign_script_chars(f"текст {sample} текст"), f"не поймано: {sample}"


def test_search_snippets_with_foreign_script_are_dropped():
    """Материал с иероглифами не должен доезжать до модели: промпт прямо требует «используй только эти факты»."""
    import yandex_search
    from datetime import datetime as _dt
    results = [
        {"title": "Путин в Дрездене", "snippet": "Обычный русский сниппет", "url": "u1", "modtime": None},
        {"title": "在东德", "snippet": "иноязычный фрагмент", "url": "u2", "modtime": None},
    ]
    ctx = yandex_search.format_search_context(results)
    assert "Обычный русский сниппет" in ctx
    assert "在东德" not in ctx


def test_twins_of_different_length_are_a_known_blind_spot():
    """
    ИЗВЕСТНАЯ ДЫРА, записанная намеренно. Раньше этот тест утверждал, что
    такая пара ловится перекрытием -- 03.08 выяснилось, что вместе с ней
    перекрытие ловит и совершенно нормальные короткие посты.

    Замер, из-за которого выбор сделан так, а не иначе:
      близнецы разной длины      5 общих основ, перекрытие 0.26
      короткий пост про ДРУГОЕ   5 общих основ, перекрытие 0.42
    Числа общих основ одинаковые, перекрытие у ЛОЖНОГО срабатывания даже
    выше. Развести эти два случая мешком слов нельзя -- можно только выбрать,
    какой ошибкой платить. Платим пропуском: пометка «похоже на дубль»
    должна что-то значить, иначе она висит на каждой карточке.

    Тест фиксирует пропуск явно, чтобы через месяц никто не решил, что эта
    пара ловится. Закрывается это не порогом, а другой метрикой
    (эмбеддинги) -- см. C3 в PRODUCT_ROADMAP.md.
    """
    short = ("Путин в Дрездене спас архив от толпы.\n\n"
             "В 1989 году к зданию советского представительства подошла толпа. "
             "Офицер вышел один и сказал, что охрана будет стрелять. Толпа отступила.")
    long = ("Как один человек остановил толпу у ворот\n\n"
            "Декабрь восемьдесят девятого, Дрезден. Берлинская стена уже пала, здание Штази разгромлено, "
            "и следующей целью становится соседнее представительство. Внутри жгут документы, печь не справляется. "
            "К воротам выходит сотрудник и негромко предупреждает: люди внутри вооружены. "
            "Слова звучат буднично, без угрозы, и именно это производит впечатление. Толпа расходится, бумаги уцелели. "
            "Много лет спустя этот эпизод будут пересказывать как первое свидетельство характера.")

    shared = len(tasks._content_words(short) & tasks._content_words(long))
    assert shared < tasks.MIN_SHARED_STEMS_FOR_OVERLAP, (
        f"общего материала стало больше ({shared} основ) -- предпосылка "
        f"дыры изменилась, тест надо пересмотреть"
    )
    assert not tasks._is_duplicate(short, long), (
        "пара вдруг поймалась -- проверьте, не ловятся ли вместе с ней "
        "обычные короткие посты (ради этого дыра и оставлена)"
    )


def test_overlap_threshold_keeps_headroom_from_different_events():
    """Разрыв между близнецами и разными событиями должен оставаться кратным."""
    worst_different = max(
        tasks._overlap(a, b)
        for a in DIFFERENT + [TWIN_A, TWIN_B]
        for b in DIFFERENT
        if a is not b
    )
    assert worst_different < tasks.DUPLICATE_OVERLAP_THRESHOLD, (
        f"перекрытие разных событий {worst_different:.3f} дошло до порога "
        f"{tasks.DUPLICATE_OVERLAP_THRESHOLD} -- начнутся ложные срабатывания"
    )
    assert tasks._overlap(TWIN_A, TWIN_B) > worst_different * 2


# ── Фон монотематического канала (прод-инцидент 03.08) ─────────────────────

# Посты про РАЗНЫЕ эпизоды биографии одного человека. Мои тексты, не с прода
# (постов оттуда у меня нет), но того же характера: один герой, одна эпоха,
# общая лексика. В логе прода такие пары давали 0.10-0.13 -- ровно уровень
# прежнего порога 0.10, поэтому детектор браковал каждый пост подряд.
_SAME_TOPIC_DIFFERENT_EPISODES = [
    "Первая работа Путина: сторож на стройке. Летом после школы Владимир "
    "устроился сторожем на стройку в Ленинграде. Отец считал, что сын должен "
    "знать цену деньгам. Работа была ночная, платили немного, но эти деньги "
    "он заработал сам.",
    "Как дзюдоист из Питера покорил сердце одноклассницы. В школьные годы "
    "Владимир занимался самбо и дзюдо. Тренер вспоминал, что мальчик был "
    "упорным. Одноклассники говорили, что он никогда не сдавался, даже "
    "проигрывая.",
    "Он выходил один против пятерых. И не боялся. Во дворе ленинградского "
    "дома дрались часто. Владимир был невысоким, но в драку шёл первым. "
    "Друзья вспоминают, что отступать он не умел совсем.",
    "Троечник, который стал президентом. В младших классах Владимир учился "
    "неровно, отец ругал за оценки. Перелом случился в шестом классе, когда "
    "мальчик всерьёз занялся спортом и подтянул учёбу.",
]


def test_different_episodes_of_one_life_are_not_duplicates():
    """Прямая проверка того, что сломалось в проде: разные эпизоды одной
    биографии не должны считаться дублями друг друга. При пороге 0.10 пара
    «дзюдо»/«драка во дворе» давала ровно 0.100 и помечалась дублем -- и так
    каждый новый пост канала."""
    import itertools
    bad = []
    for a, b in itertools.combinations(_SAME_TOPIC_DIFFERENT_EPISODES, 2):
        if tasks._is_duplicate(a, b):
            bad.append((tasks._similarity(a, b), tasks._overlap(a, b),
                        a.split(".")[0], b.split(".")[0]))
    assert not bad, "разные эпизоды одной темы приняты за дубли: " + "; ".join(
        f"J={j:.3f} O={o:.3f} «{x}» / «{y}»" for j, o, x, y in bad
    )


def test_new_post_on_a_mono_thematic_channel_gets_through():
    """Тот же фон, но так, как это происходит в жизни: новый пост сверяется
    СРАЗУ СО ВСЕМИ уже готовыми. Максимум по четырём сравнениям выше, чем по
    одному, и именно он пробивал порог -- очередь длиннее, шанс ложного
    срабатывания выше."""
    fresh = ("Друг детства вспоминает: Владимир всегда защищал младших во "
             "дворе Ленинграда, а после школы бежал на тренировку по самбо.")
    dup, score = tasks._find_duplicate(fresh, _SAME_TOPIC_DIFFERENT_EPISODES)
    assert dup is None, (
        f"новый пост про другое принят за дубль «{dup.split('.')[0]}» (Жаккар {score:.3f})"
    )


def test_thresholds_stay_between_the_measured_bands():
    """Страховка на будущее: пороги обязаны лежать МЕЖДУ измеренными полосами
    (таблица в tasks.py). Тест падает, если кто-то снова опустит порог в фон
    или поднимет выше настоящих близнецов -- оба раза это уже случалось."""
    worst_different = max(
        max(tasks._similarity(a, b), 0) for a, b in
        [(x, y) for i, x in enumerate(_SAME_TOPIC_DIFFERENT_EPISODES)
         for y in _SAME_TOPIC_DIFFERENT_EPISODES[i + 1:]]
    )
    assert tasks.DUPLICATE_THRESHOLD > worst_different, (
        f"порог Жаккара {tasks.DUPLICATE_THRESHOLD} не выше фона разных эпизодов {worst_different:.3f}"
    )
    assert tasks._is_duplicate(TWIN_A, TWIN_B), "настоящие близнецы перестали ловиться"


# ── Пометка должна называть конкретный пост (владелец 04.08) ───────────────

async def test_duplicate_flag_points_at_the_post_it_matched(channel_with_post, monkeypatch):
    """Владелец про прежнюю пометку: «не понимаю этот блок».

    «Похоже на уже готовый пост» не отвечало на единственный вопрос, который
    возникает у человека: похоже на КАКОЙ, что с чем сравнивать. Проверяем,
    что пост запоминает, на кого он похож, — без этого фронту нечего показать
    и ссылка «Показать его» существовать не может.
    """
    import database
    from database import Post
    from sqlmodel import select

    _stub_generator(monkeypatch, [TWIN_B, TWIN_B])
    await tasks.generate_for_channel(channel_with_post, topic="история",
                                     respect_queue_depth=False)

    with database.session() as s:
        newest = s.exec(
            select(Post).where(Post.channel_id == channel_with_post)
            .order_by(Post.created_at.desc())
        ).first()
        assert newest.duplicate_suspected is True
        assert newest.duplicate_of_post_id is not None, "пометка не знает, на кого похоже"
        twin = s.get(Post, newest.duplicate_of_post_id)
        assert twin is not None and twin.id != newest.id
        assert TWIN_A.split("\n")[0][:20] in twin.text, "ссылка ведёт не на тот пост"


async def test_no_duplicate_flag_means_no_reference(channel_with_post, monkeypatch):
    """Обратная сторона: у обычного поста ссылки быть не должно, иначе фронт
    нарисует пометку там, где сомнения не было."""
    import database
    from database import Post
    from sqlmodel import select

    _stub_generator(monkeypatch, [OTHER_2])
    await tasks.generate_for_channel(channel_with_post, topic="история",
                                     respect_queue_depth=False)

    with database.session() as s:
        newest = s.exec(
            select(Post).where(Post.channel_id == channel_with_post)
            .order_by(Post.created_at.desc())
        ).first()
        assert newest.duplicate_suspected is False
        assert newest.duplicate_of_post_id is None
