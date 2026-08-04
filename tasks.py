"""
Оркестрация задач: генерация, публикация, уведомления.
"""

import json
import logging
import re
from datetime import datetime, timedelta
from typing import Optional

import os

import config
import generator
import research
import telegram_api
from database import (
    session, Channel, ChannelRule, Source, Post, User, TrafficAttribution, PostApproval, Payment,
    claim_post_for_publish, release_post_publish_claim,
    claim_channel_for_generation, release_channel_generation_claim,
)
from sqlmodel import select
from sqlalchemy import text

logger = logging.getLogger(__name__)

LOW_TOKENS_THRESHOLD = 20000  # ~1 пост


def _looks_like_menu(text: str) -> bool:
    text_stripped = text.strip()
    lines = text_stripped.split("\n")
    bullet_lines = sum(1 for l in lines if re.match(r"^\s*[-•*\d]", l))
    if bullet_lines >= 3 and len(lines) <= 15:
        if "?" in text_stripped[:200]:
            return True
    if re.search(r"(напиши тему|какую тему|что именно|уточни|подскажи)\s*[.?]?\s*$", text_stripped, re.IGNORECASE):
        return True
    return False


async def _notify_user(user: User, text: str):
    """Отправляет уведомление пользователю в Telegram если подключён."""
    if not user.tg_chat_id:
        return
    ok, err = await telegram_api.send_notification(user.tg_chat_id, text)
    if not ok:
        logger.warning(f"Уведомление пользователю {user.id}: {err}")

async def _notify_user_by_id(chat_id: int, text: str):
    """Отправляет уведомление напрямую по chat_id (без объекта User)."""
    ok, err = await telegram_api.send_notification(chat_id, text)
    if not ok:
        logger.warning(f"Уведомление chat_id={chat_id}: {err}")


# ── Защита от постов-близнецов ────────────────────────────────────────
# Сравнение по заголовкам не работает: модель охотно пишет про тот же факт
# под новым заголовком, и формально запрет «не повторять темы» не нарушен.
# Особенно это выражено у новостных каналов с поиском -- выдача между двумя
# генерациями подряд одинаковая, а инструкция «используй только эти факты»
# сильнее, чем «возьми новое событие». Поэтому сравниваем СОДЕРЖАНИЕ.

_DUP_STOPWORDS = {
    "который", "которая", "которые", "этого", "этому", "этим", "такое",
    "когда", "потому", "чтобы", "просто", "очень", "может", "можно", "нужно",
    "после", "перед", "около", "через", "между", "здесь", "тогда", "сейчас",
    "именно", "всего", "более", "менее", "самый", "самая", "самое", "своих",
    "своей", "своего", "будет", "было", "были", "есть", "стал", "стала",
}


# Длина основы слова. Русский сильно флективен: «патриархом» и «патриарха»,
# «крестил» и «крестила» -- это одно и то же слово в разных формах, и
# сравнение точных словоформ их не сопоставляет.
#
# Было 6, стало 4 (аудит 02.08). При 6 усечение не делало НИЧЕГО для слов из
# 5-6 букв, а это как раз самые содержательные признаки «пост про то же
# самое»: «Путин»/«Путина», «друг»/«друга», «двор»/«двора», «отец»/«отца» --
# все они оставались разными токенами. На реальной паре близнецов с прода
# («один против пятерых», два пересказа одного эпизода) это давало 0.080 при
# пороге 0.10 -- то есть детектор их не видел. Замер на тех же текстах:
#   _STEM_LEN=4 -> 0.124 (ловится)   =5 -> 0.101 (впритык)   =6 -> 0.080 (пропуск)
# При этом худшая пара «разные события одной темы» остаётся 0.039, то есть
# разрыв сохраняется.
_STEM_LEN = 4

# Сколько страниц выдачи Яндекса перебираем, прежде чем вернуться к первой.
# Пять -- это примерно 40 источников при YANDEX_SEARCH_MAX_RESULTS=8; дальше
# выдача заметно теряет качество, а к моменту возврата на первую страницу там
# уже другие новости.
SEARCH_PAGES_CYCLE = 5


def _content_words(text: str) -> set:
    """
    Основы значимых слов: без разметки, коротких слов и частотного мусора.

    Стоп-слова отсекаются ПОСЛЕ усечения (аудит 02.08). Раньше было наоборот,
    и список из 39 слов ловил одну-две формы из шести: «который» отсекался, а
    «которую»/«которых»/«которым» проходили и оседали в пересечении как
    «котор». В пересечении реальной пары близнецов честно нашлись «если» и
    «даже» -- служебные слова считались доказательством схожести.
    """
    clean = re.sub(r"<[^>]+>", " ", text or "").lower().replace("ё", "е")
    words = re.findall(r"[а-яa-z0-9]{3,}", clean)
    stems = {w[:_STEM_LEN] for w in words}
    return stems - _DUP_STOPWORD_STEMS


def _similarity(a: str, b: str) -> float:
    """
    Доля общих значимых слов (Жаккар: общее / объединение). Хорошо ловит
    пересказ новости, где неизбежно делятся редкие имена собственные.

    Сам по себе НЕ достаточен -- см. _overlap и _is_duplicate.
    """
    wa, wb = _content_words(a), _content_words(b)
    if not wa or not wb:
        return 0.0
    return len(wa & wb) / len(wa | wb)


def _overlap(a: str, b: str) -> float:
    """
    Коэффициент перекрытия: общее / размер меньшего множества.

    Добавлен 02.08 после того, как пара близнецов прошла Жаккара с 0.080 при
    пороге 0.10. Жаккар структурно слеп к пересказу «другими словами»: он
    делит на ОБЪЕДИНЕНИЕ, поэтому чем длиннее и литературнее два поста про
    один факт, тем ниже число. У биографического сторителлинга общий факт
    выражается обычными словами, а всё остальное -- разное «мясо», растущее
    в знаменателе. Перекрытие от длины текста не страдает.
    """
    wa, wb = _content_words(a), _content_words(b)
    if not wa or not wb:
        return 0.0
    return len(wa & wb) / min(len(wa), len(wb))


# Сколько общих основ обязано быть, чтобы ВЕРИТЬ перекрытию.
#
# Перекрытие делит на МЕНЬШЕЕ множество, поэтому оно структурно завышено на
# коротких текстах: пост из 12 основ, разделивший три общих слова, сразу даёт
# 0.25. На монотематическом канале эти три слова -- «владимир», «школа»,
# «тренер», то есть словарь канала, а не общее событие. Замер 03.08:
#
#   близнецы-сторителлинг      22 общих основы, перекрытие 0.63
#   короткий пост про ДРУГОЕ    5 общих основ,  перекрытие 0.42  <- ложное
#   близнецы разной длины       5 общих основ,  перекрытие 0.26
#
# Вторая и третья строки неразличимы по числу: 5 и 5. Значит порогом по
# перекрытию их не развести, и надо выбирать, какой ошибкой платить.
# Выбираю точность: пометка «похоже на дубль» обязана что-то значить, иначе
# она превращается в шум на каждой карточке (правило 5). Цена выбора --
# близнецы разной длины больше не ловятся; это записано в тесте прямым
# текстом, а не подразумевается.
MIN_SHARED_STEMS_FOR_OVERLAP = 10


def _is_duplicate(a: str, b: str) -> bool:
    """
    Два поста про одно и то же, если сработала любая из двух метрик. У них
    разные слепые зоны: Жаккар делит на объединение и потому честен к длине,
    перекрытие ловит пересказ другими словами -- но только когда общего
    материала действительно много (см. MIN_SHARED_STEMS_FOR_OVERLAP).
    """
    if _similarity(a, b) >= DUPLICATE_THRESHOLD:
        return True
    wa, wb = _content_words(a), _content_words(b)
    if len(wa & wb) < MIN_SHARED_STEMS_FOR_OVERLAP:
        return False
    return _overlap(a, b) >= DUPLICATE_OVERLAP_THRESHOLD


# Порог Жаккара. Был 0.10 -- и это оказалось уровнем фона, а не уровнем
# близнеца (прод-инцидент 03.08, канал «Истории из жизни Путина»).
#
# Все замеры, которые у меня есть, в одной таблице -- порог обязан лежать
# между двумя полосами, а не выбираться из головы:
#
#   ЧТО                                        Жаккар   перекрытие
#   разные эпизоды одной темы (лог прода)      0.10-0.13    —
#   разные эпизоды, мои тексты того же вида    0.100      0.182
#   близнецы-сторителлинг (test_duplicate_)    0.240      0.414
#   близнецы разной длины (test_duplicate_)    0.076      0.263
#   близнецы-новости, калибровочная пара       0.5+       0.63
#
# Отсюда 0.18 по Жаккару (между 0.13 и 0.240) и 0.24 по перекрытию (между
# 0.182 и 0.263). Запас тонкий, особенно у второй пары -- и это не
# придирка к числу, а свойство метрики: полосы почти соприкасаются.
#
# ИМЕННО ПОЭТОМУ решение детектора больше не отменяет пост (см.
# Post.duplicate_suspected). При пороге 0.10 ошибка стоила пользователю
# всей очереди и реальных денег -- цикл «сгенерировали, забраковали,
# сгенерировали» крутился каждую минуту и молча. Теперь ошибка в любую
# сторону стоит одной пометки на карточке, и тонкий запас перестал быть
# опасным.
DUPLICATE_THRESHOLD = float(os.getenv("DUPLICATE_THRESHOLD", "0.18"))

# Порог перекрытия -- своя шкала, он всегда выше Жаккара. Границы полос см.
# в таблице выше.
#
# Пробовал считать перекрытие со взвешиванием слов по редкости внутри канала
# (idf): настоящий дубль поднялся на первое место, но отрыв от ближайшей пары
# «разные эпизоды» вышел 0.152 против 0.132 -- этого мало, чтобы поставить
# порог, и я не стал добавлять метрику, которую не могу откалибровать. Это
# уже второй заход на подбор порогов; третьего не будет -- следующим шагом
# нужна не другая константа, а другая метрика (эмбеддинги).
DUPLICATE_OVERLAP_THRESHOLD = float(os.getenv("DUPLICATE_OVERLAP_THRESHOLD", "0.24"))

# Стоп-слова хранятся уже усечёнными -- иначе фильтр не совпадёт с тем, что
# реально лежит в множестве основ (см. _content_words).
_DUP_STOPWORD_STEMS = {w[:_STEM_LEN] for w in _DUP_STOPWORDS}


def _find_duplicate(text: str, existing: list) -> tuple:
    """
    Возвращает (совпавший_текст, коэффициент) или (None, лучший_коэффициент).

    Дублем считается срабатывание любой из двух метрик (см. _is_duplicate);
    в коэффициенте отдаём Жаккара -- он идёт в логи и в quality-scan как
    привычное число.
    """
    best, score, matched = None, 0.0, False
    for other in existing:
        sim = _similarity(text, other)
        if _is_duplicate(text, other):
            # Дубль важнее «самого похожего»: возвращаем первый найденный
            # с его коэффициентом, даже если он ниже, чем у не-дубля.
            return other, max(sim, score)
        if sim > score:
            best, score = other, sim
    return (None, score)


def _reload_recent_texts(channel_id: int, fallback: list) -> list:
    """
    Свежий снимок текстов канала прямо перед сверкой на дубли.

    Нужен потому, что основной снимок берётся ДО генерации, а генерация
    занимает минуту-полторы: пост, созданный за это время другим вызовом, в
    старом снимке отсутствует -- и именно он оказывался близнецом. При любой
    ошибке чтения возвращаем прежний список: сверка по устаревшим данным
    лучше, чем отсутствие сверки.
    """
    try:
        with session() as s:
            rows = s.exec(
                select(Post).where(
                    Post.channel_id == channel_id,
                    Post.status.in_(["pending", "scheduled", "published"]),
                ).order_by(Post.created_at.desc()).limit(30)
            ).all()
            return [p.text or "" for p in rows]
    except Exception as e:
        logger.warning(f"канал {channel_id}: не удалось перечитать тексты для сверки на дубли: {e}")
        return fallback


# Иноязычные вкрапления. Найдено владельцем 02.08 на живом канале: в русском
# посте оказалось «В 1989 году在东德 Дрездене» -- китайское «в Восточной
# Германии», скопированное моделью из иноязычного фрагмента поисковой выдачи.
# Проверки языка в проекте не было НИ ОДНОЙ: ни на входе (материал поиска
# подставляется дословно), ни на выходе. Единственное, что стояло между
# пользователем и иероглифами, -- строка «ЯЗЫК: русский» в промпте, причём
# даже не в блоке жёстких запретов.
_FOREIGN_SCRIPT_RE = re.compile(
    "["
    "一-鿿"    # китайский
    "぀-ゟ"    # хирагана
    "゠-ヿ"    # катакана
    "가-힯"    # хангыль
    "؀-ۿ"    # арабица
    "֐-׿"    # иврит
    "฀-๿"    # тайский
    "]"
)


def _foreign_script_chars(text: str) -> str:
    """Символы чужих письменностей в тексте (пусто -- всё чисто)."""
    return "".join(_FOREIGN_SCRIPT_RE.findall(text or ""))


async def _set_generating(channel_id: int, on: bool):
    """
    Индикатор "генерируется следующий пост" (C14, пункт 6 из видения
    владельца 01.08) -- отдельная короткая транзакция до/после generate_for_channel,
    а не часть его основной сессии: должна пережить и успех, и падение
    generate_for_channel одинаково (см. вызов через try/finally ниже).
    """
    with session() as s:
        channel = s.get(Channel, channel_id)
        if not channel:
            return
        channel.generating_since = datetime.utcnow() if on else None
        s.add(channel)
        s.commit()


async def generate_for_channel(channel_id: int, topic: str = "", force_pending: bool = False,
                                target_scheduled_at: Optional[datetime] = None,
                                respect_queue_depth: bool = True) -> dict:
    """
    Единственная точка входа в генерацию. Делает три вещи, которых раньше не
    делал никто, из-за чего очередь росла выше заданной глубины и появлялись
    посты-близнецы (аудит 02.08):

    1. АТОМАРНО захватывает канал (claim_channel_for_generation). Пока по
       каналу идёт генерация, второй вызов не начнёт свою, а вернётся с
       already_generating. Раньше замка не было вовсе: между «прочитали, что
       в очереди 3 из 4» и «записали пост» проходили десятки секунд запроса к
       модели, и в это окно успевали тик, клик пользователя, догенерация после
       публикации, кнопка в Telegram и вебхук ЮKassa -- каждый со своей
       генерацией. Channel.generating_since раньше был только индикатором для
       интерфейса; теперь он же и замок.
    2. Перепроверяет глубину очереди уже ПОД захватом -- то есть после того,
       как все параллельные вызовы отсеялись. Это ловит и случай, когда
       ограничения не было вовсе: у ручки "Написать пост сейчас"
       (POST /api/channels/{id}/generate) проверки глубины не было ни одной,
       и кнопка в настройках канала спокойно перебивала лимит.
    3. Снимает захват в finally -- падение генерации не должно оставить канал
       заблокированным (плюс сам захват протухает, см. claim_channel_for_generation).

    respect_queue_depth=False -- только для онбординга (первый черновик
    показывается до того, как у канала вообще есть очередь).
    """
    with session() as s:
        if not claim_channel_for_generation(s, channel_id):
            logger.info(f"канал {channel_id}: генерация уже идёт, второй раз не запускаем")
            return {"ok": False, "message": "Пост для этого канала уже готовится",
                    "already_generating": True}
    try:
        if respect_queue_depth:
            with session() as s:
                channel = s.get(Channel, channel_id)
                if not channel:
                    return {"ok": False, "message": "Канал не найден"}
                if _queue_len(s, channel_id) >= queue_target_for_user(s, channel.user_id, channel):
                    logger.info(f"канал {channel_id}: очередь уже полна, генерацию пропускаем")
                    return {"ok": False, "message": "Очередь уже заполнена",
                            "queue_full": True}
        result = await _generate_for_channel_impl(channel_id, topic, force_pending, target_scheduled_at)
        _record_generation_outcome(channel_id, result)
        return result
    finally:
        with session() as s:
            release_channel_generation_claim(s, channel_id)


MAX_GEN_FAIL_STREAK = 3


def _record_generation_outcome(channel_id: int, result: dict) -> None:
    """
    Ведёт счётчик неудач подряд (Channel.gen_fail_streak).

    Прод-инцидент 03.08: генерация возвращала отказ, тик пробовал заново через
    минуту, и так без конца -- около 54 000 токенов за пять минут, и ни строчки
    на экране. Конкретную причину (детектор дублей) мы убрали, но форма ошибки
    осталась бы: отказов, после которых пост не создаётся, в generate_for_channel
    целых четыре, и любой зациклился бы точно так же. Поэтому считаем здесь, в
    одном месте на все пути сразу, а не заплатками по каждому.

    Считаем только отказы, где генерация РЕАЛЬНО пробовала и не смогла
    (`generation_failed`). «Очередь полна», «уже генерируется», «нет баланса» --
    не неудачи: первые два штатны, а про баланс экран очереди и так говорит
    отдельной красной плашкой с кнопкой пополнения.
    """
    with session() as s:
        channel = s.get(Channel, channel_id)
        if not channel:
            return
        if result.get("ok"):
            if channel.gen_fail_streak or channel.gen_fail_reason:
                channel.gen_fail_streak = 0
                channel.gen_fail_reason = ""
                s.add(channel); s.commit()
            return
        if not result.get("generation_failed"):
            return
        channel.gen_fail_streak = (channel.gen_fail_streak or 0) + 1
        channel.gen_fail_reason = (result.get("message") or "")[:300]
        s.add(channel); s.commit()
        if channel.gen_fail_streak == MAX_GEN_FAIL_STREAK:
            logger.warning(
                f"канал {channel_id}: {MAX_GEN_FAIL_STREAK} неудачи подряд "
                f"(«{channel.gen_fail_reason}») -- плановую генерацию останавливаю, "
                f"причина показана пользователю"
            )


def reset_generation_failures(channel_id: int) -> None:
    """
    Снимает стоп: обстоятельства изменились, пробовать снова осмысленно.

    Зовётся оттуда, где человек что-то сделал руками -- опубликовал, удалил
    или отклонил пост, поменял настройки канала, пополнил баланс, нажал
    «Написать пост сейчас». Именно эти действия и меняют то, из-за чего
    генерация не получалась (например, короче становится список постов, с
    которыми сверяется детектор дублей).
    """
    with session() as s:
        channel = s.get(Channel, channel_id)
        if channel and (channel.gen_fail_streak or channel.gen_fail_reason):
            channel.gen_fail_streak = 0
            channel.gen_fail_reason = ""
            s.add(channel); s.commit()


async def _generate_for_channel_impl(channel_id: int, topic: str = "", force_pending: bool = False,
                                      target_scheduled_at: Optional[datetime] = None) -> dict:
    with session() as s:
        channel = s.get(Channel, channel_id)
        if not channel:
            return {"ok": False, "message": "Канал не найден"}
        user = s.get(User, channel.user_id)
        if not user:
            return {"ok": False, "message": "Владелец не найден"}
        if user.token_balance <= 0:
            return {"ok": False, "message": "Бесплатный лимит закончился. Пополните баланс, чтобы создавать новые посты."}
        channel_about = channel.about
        channel_title = channel.title
        sources = s.exec(
            select(Source).where(Source.channel_id == channel_id, Source.enabled == True)  # noqa
        ).all()
        source_urls = [src.url for src in sources]
        # Загружаем правила канала
        rules = s.exec(select(ChannelRule).where(ChannelRule.channel_id == channel_id)).all()
        rules_text = "\n".join(f"• {r.rule_text}" for r in rules) if rules else ""

    # Диагностическое логирование (task item 6, P0 stale-topic bug): полная
    # видимость что реально пришло в эту генерацию -- channel_id, его title/
    # about из БД прямо сейчас, и явный topic если передан. Это позволит
    # увидеть на реальных логах Railway, доходит ли правильная тема до этой
    # точки, или подмена происходит раньше (на фронте) либо позже (в самой
    # generator.generate_post).
    logger.info(
        f"[generate_for_channel] channel_id={channel_id} channel.title=«{channel_title}» "
        f"channel.about=«{channel_about}» explicit_topic=«{topic}» "
        f"effective_topic_source={'explicit_topic' if topic else 'channel.about'}"
    )

    # Topic validation (Parts 1-3 задачи): классифицируем тему ДО любых дорогих
    # операций (research, web_search). Тема для проверки — явный topic если
    # передан, иначе тема канала (channel.about), потому что именно она пойдёт
    # в генерацию когда topic не указан явно (см. generator.generate_post).
    topic_to_classify = topic or channel_about
    classification = await generator.classify_topic(topic_to_classify)
    logger.info(f"Канал {channel_id}: topic_classification={classification} для «{topic_to_classify[:80]}»")

    if classification == "ambiguous_intimate_topic":
        # Task E: серая зона дошла до генерации напрямую (минуя /validate-topic,
        # например defense-in-depth расхождение классификаторов). Та же логика
        # очистки черновика что и для rejection, но сообщение — уточняющее,
        # не отказное.
        with session() as s:
            existing_posts = s.exec(select(Post).where(Post.channel_id == channel_id)).first()
            if not existing_posts:
                ch = s.get(Channel, channel_id)
                if ch:
                    logger.info(f"Канал {channel_id}: удаляю draft-канал, тема требует уточнения (ambiguous_intimate_topic)")
                    from database import IdempotencyKey
                    for k in s.exec(select(IdempotencyKey).where(IdempotencyKey.channel_id == channel_id)).all():
                        s.delete(k)
                    s.delete(ch)
                    s.commit()
        return {
            "ok": False,
            "message": generator.AMBIGUOUS_INTIMATE_CLARIFICATION,
            "topic_classification": classification,
            "is_clarification": True,
            "channel_deleted": True,
        }

    rejection_msg = generator.rejection_message(classification)
    if rejection_msg:
        # Defense in depth: тема уже должна была быть отклонена на этапе
        # /api/validate-topic до создания канала (см. основной фикс). Если
        # мы всё же оказались здесь с invalid topic — это редкий случай
        # расхождения между двумя независимыми вызовами классификатора на
        # погранично-неопределённой теме. Подчищаем черновик канала, чтобы
        # неподходящая тема не осталась видимой в dashboard/settings —
        # но только если у канала ещё нет ни одного поста (это значит он
        # только что создан в онбординге, а не существующий канал
        # пользователя, тему которого позже отредактировали в настройках).
        # КРИТИЧНО: при classification_unavailable удалять НЕЧЕГО. Этот статус
        # означает, что до модели не дозвонились -- провайдер лежит, таймаут,
        # кончился баланс. Тема пользователя не отклонена, она вообще не
        # проверялась. Удалить в этот момент только что созданный канал --
        # значит наказать человека за наш сбой и стереть его работу.
        with session() as s:
            existing_posts = s.exec(select(Post).where(Post.channel_id == channel_id)).first()
            if not existing_posts and classification != "classification_unavailable":
                ch = s.get(Channel, channel_id)
                if ch:
                    logger.info(f"Канал {channel_id}: удаляю draft-канал из-за отклонённой темы ({classification})")
                    from database import IdempotencyKey
                    for k in s.exec(select(IdempotencyKey).where(IdempotencyKey.channel_id == channel_id)).all():
                        s.delete(k)
                    s.delete(ch)
                    s.commit()
        return {
            "ok": False,
            "message": rejection_msg,
            "topic_classification": classification,
            "channel_deleted": True,
        }

    # Загружаем заголовки последних постов чтобы не повторять темы.
    #
    # КРИТИЧНО: раньше учитывались только status=="published" -- для канала
    # в режиме "публикация после подтверждения" посты подолгу сидят в
    # pending/scheduled и не становятся published, пока пользователь явно
    # не подтвердит (или не пройдёт таймаут). Из-за этого проверка на
    # повтор темы была слепа к уже СГЕНЕРИРОВАННЫМ, но ещё не опубликованным
    # постам -- при узкой теме (например "кошки") это привело к тому, что
    # одна и та же новость (кот из Эрмитажа, кот спасён дроном) генерировалась
    # заново каждый цикл, потому что ни один из уже стоящих в очереди постов
    # об этом не учитывался. Теперь смотрим на все НЕ отклонённые посты.
    recent_titles = ""
    recent_texts = []
    # Страница выдачи Яндекса для этой генерации. Прод 03.08: поисковый запрос
    # собирается из channel.about и потому одинаков всегда, а страница была
    # прибита к нулю -- модель раз за разом получала те же восемь источников и
    # писала те же три-четыре известные истории. Листаем по числу уже
    # написанных постов: у каждого следующего поста материал другой.
    #
    # По кругу через SEARCH_PAGES_CYCLE, а не бесконечно вперёд: на дальних
    # страницах выдача становится мусорной, а канал живёт долго. Цикл
    # возвращает нас к первой странице, где к тому времени уже другие новости.
    search_page = 0
    try:
        with session() as s:
            from sqlmodel import select as sel
            recent_posts = s.exec(
                sel(Post).where(
                    Post.channel_id == channel_id,
                    Post.status.in_(["pending", "scheduled", "published"])
                ).order_by(Post.created_at.desc()).limit(30)
            ).all()
            titles = []
            for p in recent_posts:
                recent_texts.append(p.text or "")
                lines = [l.strip() for l in (p.text or "").strip().split("\n") if l.strip()]
                first_line = re.sub(r"<[^>]+>", "", lines[0]).strip() if lines else ""
                # КРИТИЧНО: одного заголовка мало. Модель спокойно пишет про тот
                # же факт под новым заголовком, и запрет формально не нарушен.
                # Даём ещё и первые предложения -- по ним видно САМО СОБЫТИЕ.
                body = re.sub(r"<[^>]+>", "", " ".join(lines[1:3])).strip()
                if first_line:
                    entry = f"- {first_line[:120]}"
                    if body:
                        entry += f"\n  (о чём: {body[:180]})"
                    titles.append(entry)
            if titles:
                recent_titles = "\n".join(titles)
            search_page = len(recent_posts) % SEARCH_PAGES_CYCLE
    except Exception as e:
        logger.warning(f"Ошибка загрузки заголовков: {e}")

    material = ""
    if source_urls:
        try:
            material = await research.fetch_sources(source_urls)
        except Exception as e:
            logger.warning(f"Ошибка сбора источников: {e}")

    # Для новостных каналов — сначала проверяем есть ли свежие новости
    if getattr(channel, "channel_type", "thematic") == "news" and not topic:
        try:
            has_news, check_tokens = await generator.check_news_available(channel)
            if not has_news:
                logger.info(f"Канал {channel_id}: новостей нет, пропускаем генерацию")
                # Списываем минимум токенов за проверку
                with session() as s:
                    u = s.get(User, channel.user_id)
                    if u:
                        u.token_balance = max(0, u.token_balance - check_tokens)
                        s.add(u); s.commit()
                return {"ok": True, "message": "Новостей нет, публикация пропущена", "skipped": True}
        except Exception as e:
            logger.warning(f"Ошибка проверки новостей: {e}")

    generation_mode = "news" if (getattr(channel, "channel_type", "thematic") == "news") else (
        "evergreen" if not channel.use_web_search or topic else "news"
    )
    try:
        text, tokens = await generator.generate_post(channel, material, topic, rules_text,
                                                     recent_titles, search_page=search_page)
    except generator.GenerationError as e:
        # Понятная ошибка — показываем как есть
        logger.info(
            f"Канал {channel_id}: generation_failed_reason=generation_error "
            f"user_input_topic=«{topic_to_classify[:80]}» topic_classification={classification} "
            f"generation_mode={generation_mode}"
        )
        return {"ok": False, "message": str(e), "generation_failed": True}
    except Exception as e:
        logger.error(f"Ошибка генерации канала {channel_id}: {e}")
        logger.info(
            f"Канал {channel_id}: generation_failed_reason=exception "
            f"user_input_topic=«{topic_to_classify[:80]}» topic_classification={classification} "
            f"generation_mode={generation_mode}"
        )
        return {"ok": False, "message": "Временная ошибка. Попробуйте ещё раз через минуту.",
                "generation_failed": True}

    # Логирование для диагностики (Part 7 задачи): видно какая тема пришла,
    # как классифицирована, какой режим генерации и какая тема в итоге вышла.
    final_topic_line = text.strip().split("\n")[0][:100] if text else ""
    logger.info(
        f"Канал {channel_id}: user_input_topic=«{topic_to_classify[:80]}» "
        f"topic_classification={classification} generation_mode={generation_mode} "
        f"final_post_topic=«{final_topic_line}»"
    )

    if _looks_like_menu(text):
        return {"ok": False, "message": "ИИ не смог определить тему. Задайте тему поста вручную.",
                "generation_failed": True}

    # Ставится ниже, если детектор дублей сработал и после перегенерации.
    # Пост при этом всё равно создаётся -- см. длинный комментарий там же.
    duplicate_suspected = False

    # Иноязычные вкрапления: одна перегенерация, потом отказ. Порог «хотя бы
    # один символ» -- вкрапление всегда короткое (два-три иероглифа внутри
    # русской фразы), доля от длины текста его не поймает.
    foreign = _foreign_script_chars(text)
    if foreign:
        logger.warning(
            f"Канал {channel_id}: в тексте символы чужой письменности «{foreign[:40]}» -- перегенерирую"
        )
        try:
            text2, tokens2 = await generator.generate_post(
                channel, material, topic, rules_text, recent_titles,
                avoid_text=("В прошлой попытке в текст попали символы чужого алфавита. "
                            "Пиши СТРОГО на языке канала: ни одного слова и ни одного символа "
                            "другого алфавита, даже если они есть в источниках. Иноязычные "
                            "названия переводи или транслитерируй."),
            )
            tokens += tokens2
            if _foreign_script_chars(text2):
                logger.warning(f"Канал {channel_id}: чужой алфавит и после перегенерации -- пост не создаю")
                return {"ok": False, "message": "Не удалось написать пост на нужном языке — попробуем позже.",
                        "foreign_script_skipped": True, "generation_failed": True}
            text = text2
        except generator.GenerationError as e:
            logger.warning(f"Канал {channel_id}: перегенерация из-за чужого алфавита не удалась ({e})")
            return {"ok": False, "message": "Не удалось написать пост на нужном языке — попробуем позже.",
                    "foreign_script_skipped": True, "generation_failed": True}

    # Пост-проверка на близнеца. Инструкции в промпте оказалось недостаточно:
    # у новостного канала выдача поиска между генерациями одна и та же, и
    # указание «используй только эти факты» перевешивает «возьми новое
    # событие» -- получается тот же факт под новым заголовком. Сравниваем
    # готовый текст с тем, что уже лежит в очереди и опубликовано, и при
    # совпадении переписываем один раз, прямо назвав, что повторять нельзя.
    # КРИТИЧНО (аудит 02.08): снимок recent_texts снят ДО генерации, то есть
    # минуту-полторы назад. Пост, созданный за это время параллельным вызовом,
    # в нём отсутствует -- а именно он и оказывался близнецом. Перечитываем
    # непосредственно перед сверкой: запрос дешёвый, а без него детектор
    # слеп по построению, каким бы точным ни был порог.
    recent_texts = _reload_recent_texts(channel_id, fallback=recent_texts)

    dup, score = _find_duplicate(text, recent_texts)
    if dup:
        dup_head = re.sub(r"<[^>]+>", "", dup.strip().split("\n")[0])[:100]
        logger.warning(
            f"Канал {channel_id}: пост дублирует уже существующий "
            f"(совпадение {score:.2f}) «{dup_head}» -- перегенерирую"
        )
        try:
            # КРИТИЧНО (аудит 02.08): запрет передаётся ОТДЕЛЬНО, а не в
            # аргументе topic. Раньше сюда клался текст забракованного поста,
            # и generator использовал его как тему: он же уходил в поисковый
            # запрос (первые 400 символов запрещённого поста!), он же
            # подставлялся в «Напиши пост на тему: ...», по нему же потом
            # сверялось соответствие теме. То есть механизм «уведи модель от
            # повтора» для каналов с веб-поиском активно ВОЗВРАЩАЛ её к тому
            # же событию, попутно тратя лишний поиск и два вызова модели.
            avoid_hint = (
                "ЗАПРЕЩЕНО писать про это событие -- пост о нём уже есть:\n"
                f"{re.sub(r'<[^>]+>', '', dup)[:600]}\n\n"
                "Возьми СОВЕРШЕННО ДРУГОЕ событие или факт. Не пересказывай то же самое "
                "другими словами и не меняй только заголовок."
            )
            text2, tokens2 = await generator.generate_post(
                channel, material, topic, rules_text, recent_titles, avoid_text=avoid_hint,
                # Следующая страница выдачи: на той же второй попытке модель
                # читала бы ровно тот материал, из которого только что вышел
                # повтор, и «возьми другое событие» ей взять было неоткуда.
                search_page=(search_page + 1) % SEARCH_PAGES_CYCLE,
            )
            tokens += tokens2
            recent_texts = _reload_recent_texts(channel_id, fallback=recent_texts)
            dup2, score2 = _find_duplicate(text2, recent_texts)
            text = text2
            if dup2:
                # ПРОД-ИНЦИДЕНТ 03.08. Здесь стояло «пост не создаю»: очередь
                # останется короче, следующий тик попробует снова. На
                # монотематическом канале «снова» означало каждую минуту без
                # конца -- детектор браковал любой пост, потому что фон
                # совпадений там сам по себе выше порога. Никто не публиковался,
                # очередь не росла, на экране мигало «генерируется…», и всё это
                # молча жгло около 10 000 токенов в минуту.
                #
                # Теперь пост создаётся всегда, а сомнение становится видимой
                # пометкой (Post.duplicate_suspected). Это и есть сквозной
                # принцип из CLAUDE.md: платформа не отменяет ничего молча --
                # она показывает человеку оба текста и даёт решить. Заодно
                # исчезает сам цикл: очередь пополняется, тик успокаивается.
                #
                # Правило 4 при этом не нарушено: пост идёт обычным путём --
                # автопилот опубликует его в своё время, режим подтверждения
                # дождётся кнопки. Нового пути публикации не появилось.
                duplicate_suspected = True
                logger.warning(
                    f"Канал {channel_id}: повтор и после перегенерации "
                    f"(совпадение {score2:.2f}) -- создаю с пометкой «похоже на дубль»"
                )
        except generator.GenerationError as e:
            # Перегенерация не состоялась (провайдер моргнул) -- значит у нас
            # на руках первый текст, про который детектор сказал «похоже на
            # дубль». Отменять пост из-за этого мы больше не отменяем (см.
            # ветку выше): сохраняем с пометкой, решение за человеком.
            duplicate_suspected = True
            logger.warning(
                f"Канал {channel_id}: перегенерация не удалась ({e}) -- "
                f"создаю исходный пост с пометкой «похоже на дубль»"
            )

    with session() as s:
        channel = s.get(Channel, channel_id)
        user = s.get(User, channel.user_id)
        # Единая модель очереди (C14, решение владельца 01-02.08): пост
        # НИКОГДА не публикуется в момент генерации -- ни автопилот, ни
        # ручная кнопка "Написать пост сейчас". Каждый пост встаёт в очередь
        # со своим временем публикации (_next_queue_slot). Автопилот
        # публикует его сам, когда время наступит (due_scheduled_posts);
        # режим подтверждения -- только после явного "Опубликовать", а если
        # время наступило без реакции -- переносит пост в конец очереди
        # (_requeue_unconfirmed_post), а не публикует молча.
        #
        # force_pending=True -- отдельный путь только для онбординга: первый
        # черновик показывается сразу на экране, до того как у канала вообще
        # есть настроенное расписание, ни очереди, ни подтверждения тут нет.
        #
        # target_scheduled_at (C14, пункт 4 из видения владельца 01.08): при
        # ручном "Написать пост сейчас" можно выбрать конкретное время вместо
        # автоматического следующего слота -- пост встаёт в очередь на это
        # место (пересортировка не нужна отдельным кодом, см. C14: позиция в
        # очереди -- это и есть scheduled_at). Будущность времени проверяет
        # вызывающая сторона (main.py) -- это граница системы, где есть
        # пользовательский ввод.
        if force_pending:
            scheduled_at = None
        elif target_scheduled_at is not None:
            scheduled_at = target_scheduled_at
        else:
            scheduled_at = _next_queue_slot(s, channel)
        post = Post(
            channel_id=channel_id,
            user_id=channel.user_id,
            text=text,
            tokens_used=tokens,
            status=("pending" if force_pending else "scheduled"),
            scheduled_at=scheduled_at,
            duplicate_suspected=duplicate_suspected,
        )
        prev_balance = user.token_balance
        user.token_balance = max(0, user.token_balance - tokens)
        channel.last_generated_at = datetime.utcnow()

        s.add(post); s.add(user); s.add(channel)
        s.commit(); s.refresh(post)
        pid = post.id

        # Читаем всё необходимое для уведомлений пока сессия открыта
        notify_chat_id = user.tg_chat_id
        notify_low = user.notify_low_tokens and prev_balance > LOW_TOKENS_THRESHOLD and user.token_balance <= LOW_TOKENS_THRESHOLD
        chan_title = channel.title
        # Подтверждение — только в режиме "публикация после подтверждения",
        # и только для постов, реально вставших в очередь (не для
        # force_pending=True онбординг-черновика).
        #
        # КРИТИЧНО (fix, унаследован): раньше здесь ещё стояло
        # bool(user.tg_chat_id) -- без подключённых личных уведомлений в
        # Telegram запись PostApproval вообще не заводилась, а значит и
        # таймер (весь смысл режима "публикация после подтверждения")
        # никогда не запускался. Таймер заводится всегда; Telegram-карточка
        # (см. _send_approval_card) -- лишь опциональный дополнительный
        # канал подтверждения поверх него, подтвердить или отклонить всегда
        # можно и на сайте, независимо от Telegram.
        needs_approval = (not channel.auto_publish) and (not force_pending)
        approval_chat_id = user.tg_chat_id
        approval_channel_id = channel_id
        approval_deadline = scheduled_at

    if notify_chat_id and notify_low:
        await _notify_user_by_id(notify_chat_id, f"⚠️ <b>Токены заканчиваются</b>\n\nОсталось ~1 пост. Пополните баланс в приложении.")

    if needs_approval:
        try:
            await _send_approval_card(pid, approval_channel_id, approval_chat_id, chan_title, text, approval_deadline)
        except Exception as e:
            logger.warning(f"approval card для поста {pid}: {e}")

    return {"ok": True, "message": "Пост поставлен в очередь", "post_id": pid, "tokens_used": tokens, "text": text}


async def publish_post(post_id: int) -> dict:
    with session() as s:
        post = s.get(Post, post_id)
        if not post:
            return {"ok": False, "message": "Пост не найден"}
        if post.status == "published":
            # Идемпотентность: если пост уже опубликован (например из-за
            # повторного клика после ложного timeout на фронте), не публикуем
            # второй раз — просто сообщаем что уже готово.
            return {
                "ok": True, "message": "Пост уже опубликован", "already_published": True,
                "telegram_message_id": post.tg_message_id,
                "published_at": post.published_at.isoformat() if post.published_at else None,
            }
        channel = s.get(Channel, post.channel_id)
        user = s.get(User, post.user_id)
        text = generator._clean_post(post.text)  # дочищаем перед публикацией
        chat = channel.tg_chat

    # КРИТИЧНО (аудит 02.08): проверка status=="published" выше сама по себе от
    # двойной публикации НЕ защищает -- между ней и записью результата стоит
    # await на Telegram, и два одновременных исполнителя (кнопка на сайте и
    # кнопка в карточке Telegram, либо тик и ручка) проходили её оба, после
    # чего пост уходил подписчикам ДВАЖДЫ. Захват атомарный: право отправлять
    # получает только тот, чей UPDATE реально изменил строку.
    with session() as s:
        if not claim_post_for_publish(s, post_id):
            post = s.get(Post, post_id)
            if post and post.status == "published":
                return {
                    "ok": True, "message": "Пост уже опубликован", "already_published": True,
                    "telegram_message_id": post.tg_message_id,
                    "published_at": post.published_at.isoformat() if post.published_at else None,
                }
            logger.info(f"Пост {post_id}: публикация уже идёт в другом месте, второй раз не отправляем")
            return {"ok": True, "message": "Пост уже публикуется", "already_published": True}

    result = await telegram_api.send_message(chat, text)
    if not result.get("ok"):
        # Сырой Telegram description никогда не попадает в message напрямую —
        # логируем отдельно, пользователю отдаём только нормализованный текст.
        raw_desc = result.get("description", "")
        logger.warning(f"Пост {post_id}: ошибка публикации в Telegram, raw_telegram_error=«{raw_desc}»")
        with session() as s:
            release_post_publish_claim(s, post_id)
        return {"ok": False, "message": telegram_api.normalize_publish_error(raw_desc)}

    # КРИТИЧНО (P0 fix): сохраняем published-статус в БД СРАЗУ после успеха
    # Telegram и немедленно возвращаем ответ клиенту. Уведомления и
    # автодогенерация очереди уходят в фон отдельной задачей — раньше они
    # выполнялись синхронно до return, и автодогенерация (полный вызов
    # Claude API с web_search) могла занимать десятки секунд, из-за чего
    # фронт получал false timeout уже ПОСЛЕ того как пост появился в Telegram.
    published_at = datetime.utcnow()
    message_id = result["result"].get("message_id")
    with session() as s:
        post = s.get(Post, post_id)
        post.status = "published"
        post.published_at = published_at
        post.tg_message_id = message_id
        post.publishing_since = None   # захват отработал, снимаем
        s.add(post); s.commit()

    return {
        "ok": True, "message": "Опубликовано",
        "telegram_message_id": message_id,
        "published_at": published_at.isoformat(),
    }


def cancel_pending_approval(post_id: int):
    """
    Гасит карточку "публикация после подтверждения", если пост был
    опубликован/отклонён/удалён из веб-приложения раньше, чем истёк
    таймер -- иначе tick() или уже неактуальная кнопка в Telegram могли бы
    среагировать на уже решённый пост (например заново опубликовать пост,
    который пользователь только что отклонил в приложении).
    Вызывается из main.py при публикации/отклонении/удалении поста.
    """
    with session() as s:
        approval = s.exec(
            select(PostApproval).where(
                PostApproval.post_id == post_id,
                PostApproval.status.in_(["waiting", "awaiting_edit"]),
            )
        ).first()
        if approval:
            approval.status = "done"
            s.add(approval); s.commit()


def _resume_deadline(current_deadline: datetime) -> datetime:
    """
    При возврате в статус "waiting" (после правки текста или отмены
    редактирования) гарантирует минимум SOFT_CONTROL_WARNING_MINUTES до
    дедлайна -- даже если исходный дедлайн уже прошёл, пост не переносится
    в конец очереди в ту же секунду, что и правка (успеть хотя бы увидеть
    предупреждение, а не попасть под уже истёкший дедлайн немедленно).
    """
    floor = datetime.utcnow() + timedelta(minutes=config.SOFT_CONTROL_WARNING_MINUTES)
    return max(current_deadline, floor)


def _approval_keyboard(post_id: int) -> list:
    return [
        [{"text": "✅ Опубликовать сейчас", "callback_data": f"appub:{post_id}"}],
        [{"text": "✏️ Редактировать", "callback_data": f"apedit:{post_id}"},
         {"text": "🗑 Отклонить", "callback_data": f"aprej:{post_id}"}],
    ]


async def _render_approval_card(chat_id: int, message_id: Optional[int], post_id: int,
                                 channel_title: str, post_text: str, deadline: datetime,
                                 edited: bool = False) -> dict:
    """Собирает и отправляет/обновляет карточку поста в личке. Общая для
    первой отправки (message_id=None -- шлём новое сообщение) и для
    обновлений (после "Отмена" редактирования, после присланного нового
    текста).

    Единая модель очереди (C14, решение владельца 01-02.08): деадлайн
    подтверждения БОЛЬШЕ НЕ означает "опубликуем сами" -- он означает
    "перенесём в конец очереди, если не подтвердите". Молчаливая публикация
    по таймеру противоречила сквозному принципу проекта (см. CLAUDE.md,
    правило 4): пользователь должен полностью контролировать, что уходит в
    канал.
    """
    preview = generator._clean_post(post_text)
    if len(preview) > 500:
        preview = preview[:500].rstrip() + "…"
    minutes_left = max(0, round((deadline - datetime.utcnow()).total_seconds() / 60))
    prefix = "✏️ <b>Текст обновлён.</b>\n\n" if edited else f"📝 <b>Новый пост для канала «{channel_title}»</b>\n\n"
    card_text = (
        f"{prefix}{preview}\n\n"
        f"⏱ Если не подтвердите за {minutes_left} мин, пост уйдёт в конец очереди."
    )
    keyboard = _approval_keyboard(post_id)
    if message_id:
        return await telegram_api.edit_message_text(chat_id, message_id, card_text, keyboard=keyboard)
    return await telegram_api.send_dm_with_keyboard(chat_id, card_text, keyboard)


async def _send_approval_card(post_id: int, channel_id: int, chat_id: Optional[int],
                               channel_title: str, post_text: str, deadline: datetime):
    """
    Заводит запись PostApproval для поста в режиме "публикация после
    подтверждения" -- но ТОЛЬКО если мы смогли предупредить человека
    карточкой в Telegram.

    Почему так. Правило 4 в CLAUDE.md разрешает публикацию (а теперь и
    перенос в конец очереди по таймеру) лишь когда таймер **явно показан**.
    Раньше запись заводилась всегда, и у пользователя без подключённых
    уведомлений пост уходил в канал через 30 минут, а сам он об этом не
    узнавал ниоткуда, кроме сайта, куда мог и не заходить.

    Теперь без доставленной карточки таймер не заводится: пост остаётся в
    очереди на своём месте и ждёт решения сколько угодно.

    deadline -- это же самое время, что и Post.scheduled_at (единая модель
    очереди, C14): дедлайн подтверждения и время в очереди -- одно и то же,
    отдельного "30 минут после генерации" таймера больше нет.

    review_chat_id=0 -- сентинел "нет Telegram-карточки" (0 не может быть
    настоящим chat_id) вместо NULL, чтобы не менять тип существующей
    NOT NULL колонки на уже задеплоенной таблице.
    """
    if not chat_id:
        logger.info(
            f"пост {post_id}: таймер подтверждения не заводим -- у пользователя "
            f"не подключены уведомления в Telegram, предупредить его нечем"
        )
        return

    result = await _render_approval_card(chat_id, None, post_id, channel_title, post_text, deadline)
    if not result.get("ok"):
        # Карточка не доставлена (бот заблокирован, сеть, ошибка Telegram).
        # Заводить таймер нельзя по той же причине: человек его не увидит.
        logger.warning(
            f"approval card для поста {post_id}: не удалось отправить ({result.get('description')}) -- "
            f"таймер не заводим, пост ждёт решения в очереди"
        )
        return

    with session() as s:
        s.add(PostApproval(
            post_id=post_id, channel_id=channel_id,
            review_chat_id=chat_id, review_message_id=result["result"].get("message_id"),
            deadline=deadline,
        ))
        s.commit()


async def _send_approval_warning(approval_id: int, post_id: int, review_chat_id: int,
                                  review_message_id: Optional[int]):
    """
    За SOFT_CONTROL_WARNING_MINUTES до дедлайна -- жёлтое предупреждение.
    Карточку в Telegram обновляем всегда (если она есть); отдельный пинг
    отдельным сообщением -- только если у человека включён тумблер
    notify_approval_pending (карточка сама по себе не пингует -- edit
    сообщения в Telegram происходит без уведомления).
    """
    with session() as s:
        approval = s.get(PostApproval, approval_id)
        if not approval or approval.status != "waiting" or approval.final_warning_sent:
            return
        approval.final_warning_sent = True
        s.add(approval); s.commit()

        post = s.get(Post, post_id)
        user = s.get(User, post.user_id) if post else None
        should_ping = bool(user and user.notify_approval_pending and user.tg_chat_id)
        ping_chat_id = user.tg_chat_id if should_ping else None
        chan_title = ""
        if post:
            channel = s.get(Channel, post.channel_id)
            chan_title = channel.title if channel else ""

    if review_message_id:
        try:
            await telegram_api.edit_message_text(
                review_chat_id, review_message_id,
                f"⏳ Осталось {config.SOFT_CONTROL_WARNING_MINUTES} мин — не подтвердите, "
                f"пост уйдёт в конец очереди.",
                keyboard=_approval_keyboard(post_id),
            )
        except Exception as e:
            logger.warning(f"approval warning: не удалось обновить карточку поста {post_id}: {e}")

    if ping_chat_id:
        try:
            await _notify_user_by_id(
                ping_chat_id,
                f"⏳ <b>Пост для канала «{chan_title}» ждёт подтверждения</b>\n\n"
                f"Осталось {config.SOFT_CONTROL_WARNING_MINUTES} мин — иначе перенесём в конец очереди.",
            )
        except Exception as e:
            logger.warning(f"approval warning: не удалось отправить пинг для поста {post_id}: {e}")


async def _requeue_unconfirmed_post(approval_id: int, post_id: int, review_chat_id: int,
                                     review_message_id: Optional[int]):
    """
    Вызывается из tick(), когда дедлайн подтверждения истёк без реакции.

    Решение владельца 01-02.08 (единая модель очереди, C14): пост НЕ
    публикуется молча по таймеру -- он переносится в конец очереди с новым
    scheduled_at и новым циклом подтверждения. Число постов в очереди не
    меняется (новый не генерируется), пост, который был вторым, становится
    первым. Это заменяет прежнее двухфазное "предупредили -> подождали
    ещё немного -> опубликовали": предупреждение теперь отдельно и заранее
    (см. _send_approval_warning, за SOFT_CONTROL_WARNING_MINUTES до
    дедлайна), а по самому дедлайну решение уже однозначное.

    PostApproval.post_id уникален (одна строка на пост за всю его жизнь) --
    новый цикл подтверждения ОБНОВЛЯЕТ ту же строку (новый deadline, статус
    снова "waiting"), а не заводит вторую: второй insert с тем же post_id
    падал на UNIQUE constraint, и новая карточка тихо не отправлялась
    (поймано тестом test_unified_queue.py, а не в проде).
    """
    with session() as s:
        approval = s.get(PostApproval, approval_id)
        if not approval or approval.status != "waiting":
            return  # уже обработано (нажали кнопку) между выборкой и этим тиком

        post = s.get(Post, post_id)
        if not post or post.status != "scheduled":
            approval.status = "done"
            s.add(approval); s.commit()
            return  # решено другим путём (например отклонено в приложении)

        channel = s.get(Channel, post.channel_id)
        if not channel or channel.auto_publish:
            # Канал переключили на автопилот, пока подтверждение висело.
            # Переносить нечего: на автопилоте пост публикуется сам по своему
            # времени, а бесконечный перенос как раз и делал его вечным
            # (аудит 02.08). Просто закрываем подтверждение.
            approval.status = "done"
            s.add(approval); s.commit()
            return

        new_slot = _next_queue_slot(s, channel)
        post.scheduled_at = new_slot
        post.requeued_at = datetime.utcnow()
        s.add(post)
        s.commit()

        chat_id = s.get(User, post.user_id).tg_chat_id if post.user_id else None
        chan_title = channel.title if channel else ""
        post_text = post.text

    if review_message_id:
        try:
            await telegram_api.edit_message_text(
                review_chat_id, review_message_id,
                "🔴 Время вышло — пост не подтверждён, перенесён в конец очереди.",
            )
        except Exception as e:
            logger.warning(f"requeue поста {post_id}: не удалось обновить старую карточку: {e}")

    # Новый цикл подтверждения -- новое сообщение в Telegram (старое уже
    # помечено выше как просроченное), но та же строка PostApproval.
    new_result = None
    if chat_id:
        try:
            new_result = await _render_approval_card(chat_id, None, post_id, chan_title, post_text, new_slot)
        except Exception as e:
            logger.warning(f"requeue поста {post_id}: не удалось отправить новую карточку: {e}")

    with session() as s:
        approval = s.get(PostApproval, approval_id)
        if not approval:
            return
        if chat_id and new_result and new_result.get("ok"):
            approval.review_chat_id = chat_id
            approval.review_message_id = new_result["result"].get("message_id")
            approval.deadline = new_slot
            approval.status = "waiting"
            approval.final_warning_sent = False
        else:
            # Не удалось предупредить о новом цикле -- по тому же принципу,
            # что и в _send_approval_card, таймер заводить нельзя: пост ждёт
            # решения в очереди сколько угодно. Безопасно даже без активной
            # записи PostApproval -- due_scheduled_posts публикует по тику
            # только каналы с auto_publish=True, режим подтверждения сам по
            # себе никогда не публикуется через общий путь.
            approval.status = "done"
        s.add(approval)
        s.commit()


async def _sync_approval_to_reschedule(post_id: int, new_deadline: datetime):
    """
    Вызывается из main.py schedule_post при ручном переносе времени поста
    ("Запланировать"/"Перенести"). Решение владельца 02.08: пост, который
    ещё МОЖНО перенести, по определению ещё не подтверждён -- подтверждение
    в этой модели это и есть публикация, а опубликованный пост в очереди уже
    не показывается и никуда не переносится. Значит перенос времени должен
    просто сдвинуть уже идущее ожидание решения вместе с постом, а не
    начинать его заново и не спрашивать повторно: та же строка PostApproval
    (или новая, если её не было вовсе), тот же статус "waiting", новый
    дедлайн = новое время. Предупреждение за SOFT_CONTROL_WARNING_MINUTES
    сбрасывается, чтобы прийти заново перед НОВЫМ сроком.

    Ничего не делает для автопилота (там подтверждения не бывает) и для
    постов без способа предупредить (нет tg_chat_id) -- те по тому же
    принципу, что и при первой генерации, просто ждут решения на сайте без
    таймера.
    """
    with session() as s:
        post = s.get(Post, post_id)
        if not post:
            return
        channel = s.get(Channel, post.channel_id)
        if not channel or channel.auto_publish:
            return
        approval = s.exec(select(PostApproval).where(PostApproval.post_id == post_id)).first()
        user = s.get(User, post.user_id)
        chat_id = user.tg_chat_id if user else None
        chan_title = channel.title
        post_text = post.text
        approval_id = approval.id if approval else None
        # Карточку переиспользуем только если решение реально ещё висит
        # ("waiting") -- если approval почему-то "done" при всё ещё
        # scheduled-посте (нетипичный случай), шлём новую, а не правим
        # закрытую карточку задним числом.
        reuse_message_id = approval.review_message_id if (approval and approval.status == "waiting") else None

    if not chat_id:
        return  # предупредить нечем -- пост ждёт решения без таймера, как и при первой генерации

    try:
        result = await _render_approval_card(chat_id, reuse_message_id, post_id, chan_title, post_text, new_deadline)
    except Exception as e:
        logger.warning(f"перенос поста {post_id}: не удалось обновить/отправить карточку: {e}")
        return
    if not result.get("ok"):
        logger.warning(f"перенос поста {post_id}: карточка не доставлена ({result.get('description')})")
        return

    with session() as s:
        if approval_id:
            approval = s.get(PostApproval, approval_id)
            if not approval:
                return
        else:
            approval = PostApproval(post_id=post_id, channel_id=post.channel_id,
                                     review_chat_id=chat_id, deadline=new_deadline)
        approval.review_chat_id = chat_id
        approval.review_message_id = result["result"].get("message_id")
        approval.deadline = new_deadline
        approval.status = "waiting"
        approval.final_warning_sent = False
        s.add(approval)
        s.commit()


async def backfill_orphaned_posts() -> dict:
    """
    Разовая (идемпотентная) починка данных, накопленных прошлыми версиями.
    Вызывается на старте приложения; повторные запуски безвредны -- если
    чинить нечего, не делает ни одной записи.

    Три вида мусора, найденные аудитом 02.08 на живом проде:

    1. Незакрытые подтверждения на автопилот-каналах. Пост не публиковался
       никогда: due_scheduled_posts его пропускал, а дедлайн бесконечно
       переносил в конец очереди. Закрываем -- дальше он публикуется сам.
    2. Посты в status="pending" без scheduled_at на автопилот-каналах.
       Наследие доC14, когда автопилот публиковал пост прямо в момент
       генерации: если отправка в Telegram не удалась, пост навсегда
       оставался в этом статусе. Ставим их в очередь.
    3. Зависшие захваты публикации (publishing_since от упавшего процесса) --
       снимаем, иначе пост не опубликуется до истечения таймаута.

    Онбординг-черновики (pending без scheduled_at) на каналах В РЕЖИМЕ
    ПОДТВЕРЖДЕНИЯ намеренно не трогаем: там «ждёт вашего решения» -- правда.
    """
    fixed = {"approvals_closed": 0, "posts_scheduled": 0, "claims_released": 0}
    with session() as s:
        auto_channel_ids = [
            c.id for c in s.exec(select(Channel).where(Channel.auto_publish == True)).all()  # noqa: E712
        ]

        if auto_channel_ids:
            stale_approvals = s.exec(
                select(PostApproval).where(
                    PostApproval.channel_id.in_(auto_channel_ids),
                    PostApproval.status.in_(["waiting", "awaiting_edit"]),
                )
            ).all()
            for appr in stale_approvals:
                appr.status = "done"
                s.add(appr)
            fixed["approvals_closed"] = len(stale_approvals)
            if stale_approvals:
                s.commit()

        released = s.execute(
            text("UPDATE post SET publishing_since = NULL "
                 "WHERE publishing_since IS NOT NULL AND status != 'published'")
        )
        fixed["claims_released"] = released.rowcount or 0
        s.commit()

    # Постановка в очередь -- отдельно и по одному каналу: _next_queue_slot
    # должен видеть уже поставленные посты, иначе все получат одно время.
    for cid in auto_channel_ids:
        with session() as s:
            channel = s.get(Channel, cid)
            if not channel:
                continue
            orphans = s.exec(
                select(Post).where(
                    Post.channel_id == cid, Post.status == "pending",
                    Post.scheduled_at.is_(None),
                ).order_by(Post.created_at)
            ).all()
            for p in orphans:
                p.status = "scheduled"
                p.scheduled_at = _next_queue_slot(s, channel)
                s.add(p)
                s.commit()
                fixed["posts_scheduled"] += 1

    if any(fixed.values()):
        logger.info(f"backfill_orphaned_posts: починено {fixed}")
    return fixed


async def sync_posts_to_channel_mode(channel_id: int):
    """
    Вызывается из main.py patch_channel при сохранении настроек канала.
    Решение владельца 02.08: "либо автоматическая — и тогда все посты
    публикуются без подтверждения, либо нет — и тогда для каждого поста
    нужно решение". Раньше переключение auto_publish не трогало уже
    существующие посты: онбординг-черновик (status="pending", без
    scheduled_at, force_pending=True при генерации) навсегда оставался
    "Ждёт вашего решения" с кнопкой на зелёном фоне, даже если канал давно
    переключили на автопилот -- владелец нашёл это на живом канале.

    Синхронизация в обе стороны:
    - Включили автопилот: "осиротевшие" pending-посты без scheduled_at
      (только они и остаются в этом статусе -- см. force_pending) встают в
      очередь как обычные посты автопилота, каждый на своё место
      (_next_queue_slot, с учётом уже поставленных в этом же вызове).
    - Включили подтверждение: посты, уже стоящие в очереди (status=
      "scheduled") без активного подтверждения (остались от автопилота, где
      подтверждение не заводится вовсе), получают обычный цикл -- дедлайн
      равен уже назначенному scheduled_at, само время не меняется.
    """
    with session() as s:
        channel = s.get(Channel, channel_id)
        if not channel:
            return

        if channel.auto_publish:
            # КРИТИЧНО (аудит 02.08): гасим ВСЕ незакрытые подтверждения канала.
            # Без этого посты, стоявшие в очереди на момент включения
            # автопилота, не публиковались никогда: due_scheduled_posts их
            # пропускал (висит подтверждение), а дедлайн бесконечно переносил
            # их в конец очереди, занимая слот и рассылая карточки на канал,
            # где интерфейс обещает «подтверждать ничего не нужно».
            # awaiting_edit гасим тоже -- иначе присланный позже текст
            # воскресит подтверждение уже на автопилот-канале.
            stale = s.exec(
                select(PostApproval).where(
                    PostApproval.channel_id == channel_id,
                    PostApproval.status.in_(["waiting", "awaiting_edit"]),
                )
            ).all()
            for appr in stale:
                appr.status = "done"
                s.add(appr)
            if stale:
                s.commit()
                logger.info(
                    f"канал {channel_id}: включён автопилот, закрыто подтверждений: {len(stale)}"
                )

            orphans = s.exec(
                select(Post).where(
                    Post.channel_id == channel_id, Post.status == "pending",
                    Post.scheduled_at.is_(None),  # force_pending -- только они остаются без времени
                ).order_by(Post.created_at)
            ).all()
            for p in orphans:
                p.status = "scheduled"
                p.scheduled_at = _next_queue_slot(s, channel)
                s.add(p)
                s.commit()
            return

        # Режим подтверждения. Посты в status="pending" без scheduled_at
        # (онбординг-черновики и наследие доC14) здесь не трогаем: у них нет
        # времени в очереди, они честно показываются как «ждёт вашего решения
        # · сам не опубликуется» -- это правда для обоих режимов.
        scheduled_posts = s.exec(
            select(Post).where(Post.channel_id == channel_id, Post.status == "scheduled")
        ).all()
        # awaiting_edit тоже считаем «занятым»: человек прямо сейчас правит
        # текст в Telegram, повторная карточка сбила бы диалог и потеряла
        # присланный следом текст (аудит 02.08).
        busy_post_ids = {
            a.post_id for a in s.exec(
                select(PostApproval).where(
                    PostApproval.channel_id == channel_id,
                    PostApproval.status.in_(["waiting", "awaiting_edit"]),
                )
            ).all()
        }
        need_approval = [(p.id, p.scheduled_at) for p in scheduled_posts if p.id not in busy_post_ids]

    for post_id, deadline in need_approval:
        try:
            await _sync_approval_to_reschedule(post_id, deadline)
        except Exception as e:
            logger.warning(f"синхронизация режима, пост {post_id}: {e}")


async def _handle_approval_callback(cq: dict):
    """Обрабатывает нажатие кнопки на карточке поста в личке."""
    cq_id = cq.get("id")
    data = cq.get("data", "") or ""
    chat_id = cq.get("message", {}).get("chat", {}).get("id")
    message_id = cq.get("message", {}).get("message_id")

    try:
        action, post_id_str = data.split(":", 1)
        post_id = int(post_id_str)
    except ValueError:
        await telegram_api.answer_callback_query(cq_id)
        return

    with session() as s:
        approval = s.exec(select(PostApproval).where(PostApproval.post_id == post_id)).first()

    if not approval or approval.review_chat_id != chat_id:
        await telegram_api.answer_callback_query(cq_id, "Карточка устарела.", show_alert=True)
        return

    if action == "appub":
        if approval.status != "waiting":
            await telegram_api.answer_callback_query(cq_id, "Уже обработано.")
            return
        with session() as s:
            a = s.get(PostApproval, approval.id)
            a.status = "done"; s.add(a); s.commit()
            post = s.get(Post, post_id)
            still_waiting = bool(post and post.status == "scheduled")
        await telegram_api.answer_callback_query(cq_id, "Публикую…")
        if not still_waiting:
            await telegram_api.edit_message_text(chat_id, message_id, "Пост уже решён другим путём.")
            return
        result = await publish_post(post_id)
        if result.get("ok"):
            await telegram_api.edit_message_text(chat_id, message_id, "✅ Опубликовано.")
            await post_publish_followup(post_id)
        else:
            await telegram_api.edit_message_text(chat_id, message_id, f"⚠️ Не удалось опубликовать: {result.get('message', 'ошибка')}")

    elif action == "aprej":
        if approval.status != "waiting":
            await telegram_api.answer_callback_query(cq_id, "Уже обработано.")
            return
        with session() as s:
            a = s.get(PostApproval, approval.id)
            a.status = "done"; s.add(a)
            post = s.get(Post, post_id)
            channel_id = post.channel_id if post else None
            if post and post.status == "scheduled":
                post.status = "rejected"
                s.add(post)
            s.commit()
        await telegram_api.answer_callback_query(cq_id, "Отклонено.")
        await telegram_api.edit_message_text(chat_id, message_id, "🗑 Пост отклонён.")
        if channel_id:
            # Отклонение из карточки в Телеграме -- то же действие человека,
            # что и кнопка на сайте: снимаем стоп по неудачам подряд.
            reset_generation_failures(channel_id)
            await _refill_queue(channel_id)

    elif action == "apedit":
        if approval.status != "waiting":
            await telegram_api.answer_callback_query(cq_id, "Уже обработано.")
            return
        with session() as s:
            a = s.get(PostApproval, approval.id)
            a.status = "awaiting_edit"; s.add(a); s.commit()
        await telegram_api.answer_callback_query(cq_id)
        await telegram_api.edit_message_text(
            chat_id, message_id,
            "✏️ Пришлите новый текст поста ответным сообщением боту.",
            keyboard=[[{"text": "Отмена", "callback_data": f"apcancel:{post_id}"}]],
        )

    elif action == "apcancel":
        if approval.status != "awaiting_edit":
            await telegram_api.answer_callback_query(cq_id, "Уже обработано.")
            return
        with session() as s:
            a = s.get(PostApproval, approval.id)
            a.status = "waiting"
            a.deadline = _resume_deadline(a.deadline)
            a.final_warning_sent = False
            s.add(a); s.commit()
            post = s.get(Post, post_id)
            channel = s.get(Channel, post.channel_id) if post else None
            post_text = post.text if post else ""
            channel_title = channel.title if channel else ""
            deadline = a.deadline
        await telegram_api.answer_callback_query(cq_id)
        await _render_approval_card(chat_id, message_id, post_id, channel_title, post_text, deadline)

    else:
        await telegram_api.answer_callback_query(cq_id)


async def _handle_possible_edit_reply(chat_id: int, new_text: str):
    """Если этот чат сейчас в режиме редактирования поста (нажали
    "Редактировать" на карточке) -- сообщение считается новым текстом
    поста. Иначе просто игнорируется (не /start, не команда)."""
    with session() as s:
        approval = s.exec(
            select(PostApproval).where(
                PostApproval.review_chat_id == chat_id,
                PostApproval.status == "awaiting_edit",
            )
        ).first()
        if not approval:
            return
        post = s.get(Post, approval.post_id)
        if not post:
            return
        cleaned = new_text.strip()
        if not cleaned:
            return
        post.text = cleaned
        approval.status = "waiting"
        approval.deadline = _resume_deadline(approval.deadline)
        approval.final_warning_sent = False
        channel = s.get(Channel, post.channel_id)
        s.add(post); s.add(approval); s.commit()
        post_id = post.id
        message_id = approval.review_message_id
        deadline = approval.deadline
        channel_title = channel.title if channel else ""

    await _render_approval_card(chat_id, message_id, post_id, channel_title, cleaned, deadline, edited=True)


async def post_publish_followup(post_id: int):
    """
    Неблокирующие операции после публикации: уведомление пользователю и
    автодогенерация очереди. Выполняются в фоне отдельной задачей, чтобы не
    задерживать HTTP-ответ клиенту (см. publish_post — это была причина
    false timeout в Bug 2).

    Единая модель очереди (C14): новый пост генерируется именно тогда, когда
    публикуется последний -- не по интервалу с фиксированной точкой отсчёта,
    а поддержанием queue_target_for_user постоянно (см. _refill_queue).
    Вызывается отсюда для ЛЮБОЙ публикации -- и по явному нажатию, и
    автопилотом через due_scheduled_posts (см. tick()).
    """
    notify_chat_id = None
    notify_title = ""
    channel_id = None
    try:
        with session() as s:
            post = s.get(Post, post_id)
            if not post:
                return
            channel_id = post.channel_id
            channel = s.get(Channel, post.channel_id)
            user = s.get(User, post.user_id)
            if user and user.notify_published and user.tg_chat_id:
                notify_chat_id = user.tg_chat_id
                notify_title = channel.title if channel else ""
    except Exception as e:
        logger.warning(f"post-publish followup (notify lookup) для поста {post_id}: {e}")

    if notify_chat_id:
        try:
            await _notify_user_by_id(notify_chat_id, f"✅ <b>Пост опубликован</b>\n\nКанал: {notify_title}")
        except Exception as e:
            logger.warning(f"post-publish followup (notify send) для поста {post_id}: {e}")

    if channel_id:
        # Публикация меняет обстоятельства: пост ушёл из очереди, список для
        # сверки на дубли стал короче -- значит попытка, которая не удавалась,
        # теперь может удаться. Снимаем стоп по неудачам подряд ПЕРЕД
        # пополнением, иначе _refill_queue выйдет на нём же (инцидент 03.08).
        reset_generation_failures(channel_id)
        try:
            await _refill_queue(channel_id)
        except Exception as e:
            logger.warning(f"auto-refill failed для поста {post_id}: {e}")


def project_upcoming_slots(channel: Channel, now: datetime, count: int = 30,
                           anchor: Optional[datetime] = None) -> list:
    """Прогноз следующих `count` моментов автопубликации по ТЕКУЩИМ настройкам
    расписания -- для календаря в кабинете (владелец 28.07 попросил, чтобы
    смена частоты сразу отражалась в календаре, а не только в момент, когда
    пост реально опубликован).

    `anchor` -- от чего отсчитывать первый прогнозный слот в режиме
    "интервал". Аудит 02.08: раньше отсчёт всегда шёл от
    `channel.last_generated_at`, а настоящая очередь строится от времени
    ПОСЛЕДНЕГО поста в ней (`_next_queue_slot` -> `_next_slot_after`
    от `last.scheduled_at`). При очереди из четырёх постов эти две
    арифметики расходились на три интервала: календарь рисовал «ожидается
    по расписанию» поверх дней, на которые уже стоят настоящие посты, и
    дальше врал тем сильнее, чем длиннее очередь -- на месяц вперёд
    расхождение доходило до недель. Вызывающая сторона (main.py) передаёт
    сюда время последнего запланированного поста, и прогноз продолжает
    очередь, а не спорит с ней.

    Намеренно не вызывать для канала без автопилота: там нет обязательства
    "выйдет само" вообще, показывать прогноз публикации значило бы обещать
    то, чего система не делает (принцип 5) -- эту проверку оставляем на
    вызывающей стороне (main.py), а не здесь, чтобы функция при этом
    оставалась чистой математикой расписания и её было проще тестировать.

    Без jitter: `_next_publish_time` в реальной генерации применяет
    случайный сдвиг, чтобы посты не выходили "по секундомеру", но для
    прогноза это дало бы дни, прыгающие между обновлениями страницы без
    всякой пользы -- берём середину интервала.
    """
    slots = []
    if channel.schedule_kind == "daily":
        try:
            times = sorted(json.loads(channel.daily_times or "[]"))
        except Exception:
            times = []
        if not times:
            return []
        cursor = now
        # Не больше 400 дней вперёд, даже если count большой -- защита от
        # зацикливания при пустом/испорченном daily_times.
        for _ in range(400):
            if len(slots) >= count:
                break
            for hhmm in times:
                try:
                    hh, mm = map(int, hhmm.split(":"))
                except Exception:
                    continue
                candidate = cursor.replace(hour=hh, minute=mm, second=0, microsecond=0)
                if candidate > now:
                    slots.append(candidate)
                if len(slots) >= count:
                    break
            cursor = cursor + timedelta(days=1)
        return slots[:count]

    if channel.schedule_kind == "interval":
        base_seconds = max(60, channel.interval_hours * 3600)
        ws, we = channel.publish_window_start, channel.publish_window_end
        cursor = anchor or channel.last_generated_at or now
        for _ in range(count * 3):  # запас на случаи, которые окно сдвигает вперёд
            if len(slots) >= count:
                break
            cursor = cursor + timedelta(seconds=base_seconds)
            slot = cursor
            if ws and we:
                try:
                    wsh, wsm = map(int, ws.split(":"))
                    weh, wem = map(int, we.split(":"))
                    window_start = slot.replace(hour=wsh, minute=wsm, second=0, microsecond=0)
                    window_end = slot.replace(hour=weh, minute=wem, second=0, microsecond=0)
                    if slot < window_start:
                        slot = window_start
                    elif slot > window_end:
                        slot = (slot + timedelta(days=1)).replace(hour=wsh, minute=wsm, second=0)
                except Exception:
                    pass
            if slot > now:
                slots.append(slot)
                cursor = slot
        return slots[:count]

    return []


_last_update_id = 0
_last_main_bot_update_id = 0

async def _process_bot_updates():
    """
    Получает обновления от бота: /start привязывает chat_id к аккаунту,
    callback_query обрабатывает кнопки карточки "публикация после
    подтверждения" (см. _handle_approval_callback), обычный текст —
    новый текст поста, если чат сейчас в режиме редактирования
    (см. _handle_possible_edit_reply).
    """
    global _last_update_id
    import telegram_api as tg
    result = await tg._call("getUpdates", {
        "offset": _last_update_id + 1,
        "timeout": 0,
        "limit": 100,
        "allowed_updates": ["message", "callback_query"]
    })
    if not result.get("ok"):
        return
    updates = result.get("result", [])
    for upd in updates:
        _last_update_id = upd["update_id"]

        cq = upd.get("callback_query")
        if cq:
            try:
                await _handle_approval_callback(cq)
            except Exception as e:
                logger.warning(f"approval callback: {e}")
            continue

        msg = upd.get("message", {})
        text = msg.get("text", "")
        chat_id = msg.get("chat", {}).get("id")
        if not chat_id:
            continue

        if not text.startswith("/start"):
            if text:
                try:
                    await _handle_possible_edit_reply(chat_id, text)
                except Exception as e:
                    logger.warning(f"edit reply от chat_id={chat_id}: {e}")
            continue

        parts = text.strip().split()
        user_id = None
        if len(parts) > 1 and parts[1].startswith("u"):
            try:
                user_id = int(parts[1][1:])
            except ValueError:
                pass
        if user_id:
            with session() as s:
                u = s.get(User, user_id)
                if u and u.tg_chat_id != chat_id:
                    u.tg_chat_id = chat_id
                    s.add(u); s.commit()
                    # Приветствие
                    await tg.send_notification(chat_id,
                        "✅ Аккаунт подключён! Теперь буду присылать уведомления об Автопост.")
                    logger.info(f"Linked tg_chat_id={chat_id} to user_id={user_id}")


_main_bot_last_reply = {}  # chat_id -> timestamp последнего ответа (debounce)
MAIN_BOT_DEBOUNCE_SECONDS = 10  # не отвечать тому же chat_id чаще чем раз в 10с

async def poll_main_bot():
    """Публичная обёртка для отдельного, более частого scheduler job (P1 fix)."""
    try:
        await _process_main_bot_updates()
    except Exception as e:
        logger.warning(f"main bot polling: {e}")


async def _process_main_bot_updates():
    """
    Обрабатывает /start у @maintrpost_bot (вход в Mini App, Task 1).
    Это ОТДЕЛЬНЫЙ бот от @Trpst_bot (publishing) -- свой токен, свой
    независимый offset обновлений (Telegram API ведёт отдельный поток
    update_id для каждого бота).

    Раньше /start у этого бота вообще ничего не отвечал, если пользователь
    написал его без параметра (или с незнакомым параметром) -- человек
    оставался в боте без единой подсказки что делать дальше. Теперь любой
    /start здесь получает приветствие с кнопкой Mini App.

    P1 fix (debounce): если пользователь нажимает /start несколько раз
    подряд за время, прошедшее между опросами (раньше -- 60с тика, теперь --
    3с, но та же проблема может возникнуть при любом интервале если несколько
    /start пришли в одном пакете getUpdates), цикл ниже обрабатывал каждое
    сообщение как отдельный /start и отправлял отдельное приветствие на
    каждое -- отсюда "пачка одинаковых сообщений". Теперь не отвечаем
    повторно тому же chat_id чаще чем раз в MAIN_BOT_DEBOUNCE_SECONDS.
    """
    global _last_main_bot_update_id
    if not config.MAIN_BOT_TOKEN:
        return
    import telegram_api as tg
    import time
    result = await tg._call("getUpdates", {
        "offset": _last_main_bot_update_id + 1,
        "timeout": 0,
        "limit": 100,
        "allowed_updates": ["message"]
    }, token=config.MAIN_BOT_TOKEN)
    if not result.get("ok"):
        return
    updates = result.get("result", [])
    for upd in updates:
        _last_main_bot_update_id = upd["update_id"]
        msg = upd.get("message", {})
        text = msg.get("text", "")
        chat_id = msg.get("chat", {}).get("id")
        if not text.startswith("/start") or not chat_id:
            continue

        now = time.monotonic()
        last_reply = _main_bot_last_reply.get(chat_id, 0)
        if now - last_reply < MAIN_BOT_DEBOUNCE_SECONDS:
            logger.info(f"main_bot /start: debounce сработал для chat_id={chat_id}, повторный /start проигнорирован")
            continue
        _main_bot_last_reply[chat_id] = now

        # Attribution: /start <param> может содержать рекламную метку
        # (tgads_<campaign>_<content> для Telegram Ads). Если распознан --
        # сохраняем источник трафика ДО регистрации (user_id ещё нет),
        # привязка к user_id произойдёт позже в /api/register по тому же
        # lp_session, если пользователь дойдёт до регистрации через Mini App.
        # Не блокирует приветствие при сбое -- та же безопасная схема что
        # остальные диагностические записи в проекте.
        mini_app_url = config.PUBLIC_URL
        parts = text.strip().split(maxsplit=1)
        start_param = parts[1].strip() if len(parts) > 1 else ""
        if start_param:
            try:
                from attribution import classify_start_param
                src, med, campaign, content = classify_start_param(start_param)
                if src != "unknown":
                    lp_session = f"tg{chat_id}_{int(now)}"
                    with session() as s:
                        s.add(TrafficAttribution(
                            landing_session_id=lp_session,
                            source=src,
                            medium=med,
                            campaign=campaign[:100],
                            content=content[:100],
                            raw_start_param=start_param[:200],
                        ))
                        s.commit()
                    # Прокидываем lp_session в Mini App, чтобы веб-часть
                    # (captureLandingSession в app.js) подхватила её и
                    # передала на /api/register -- тогда регистрация
                    # привяжется к этой же TrafficAttribution записи.
                    mini_app_url = f"{config.PUBLIC_URL}?lp_session={lp_session}"
            except Exception:
                logger.warning("main_bot /start: attribution parsing failed", exc_info=True)

        # Кнопка типа web_app открывает именно Mini App (не внешний браузер) —
        # это единственный программный способ гарантировать одно нажатие на
        # Android и iOS одинаково. Обычная url-кнопка открыла бы системный
        # браузер, а не Mini App внутри Telegram.
        await tg._call("sendMessage", {
            "chat_id": chat_id,
            "text": "👋 Привет! АвтоПост пишет посты для вашего Телеграм-канала и помогает публиковать их по расписанию.\n\nНажмите кнопку ниже, чтобы открыть приложение.",
            "reply_markup": {
                "inline_keyboard": [[
                    {"text": "Открыть АвтоПост", "web_app": {"url": mini_app_url}}
                ]]
            }
        }, token=config.MAIN_BOT_TOKEN)
        logger.info(f"main_bot /start: отправлено приветствие chat_id={chat_id}")


MIN_QUEUE_DEPTH = 1  # минимум, который может выбрать пользователь в настройках.
                     # Отдельная константа от MIN_QUEUE намеренно (владелец
                     # 02.08): MIN_QUEUE работает сразу в двух ролях -- он и
                     # потолок бесплатного тарифа, и нижняя граница выбора.
                     # Если просто опустить MIN_QUEUE до 1, вместе с выбором
                     # «держать один пост» у всех бесплатных каналов
                     # схлопнется и потолок, и очередь перестанет наполняться.
                     #
                     # Один пост в очереди -- законный сценарий: человек хочет
                     # видеть ровно следующий пост и решать по нему, а не
                     # разбирать запас на неделю. Интерфейс в этом случае
                     # обязан объяснить, что запас можно увеличить, иначе
                     # пустое место после единственного поста читается как
                     # «дальше ничего не будет» (см. _renderQueueSlots).
MIN_QUEUE = 3        # глубина очереди на бесплатном старте
PAID_QUEUE = 7       # "очередь на неделю" -- ровно то, что обещает оффер после
                     # первого удачного поста (см. fpFeedbackGood во фронте:
                     # «Автопост подготовит 7 постов по вашей теме»). Раньше это
                     # обещание не выполнялось вообще ничем: оплата только
                     # начисляла токены, а очередь как держалась на 3, так и
                     # оставалась на 3 -- у оплатившего не появлялось ничего,
                     # за что он заплатил, кроме баланса.

# За один тик догенерируем не больше этого числа постов на канал.
#
# Ровно 1, а не 2: две генерации подряд идут на ОДНОЙ И ТОЙ ЖЕ выдаче поиска
# (между ними проходят секунды), и модель закономерно писала оба поста про
# одно событие, меняя только заголовок -- это ловилось на реальном канале.
# Один пост за тик даёт выдаче обновиться и заодно не забивает общий цикл
# планировщика: каждая генерация это классификация темы, поиск и сам запрос.
# Очередь всё равно дособирается за несколько минут.
MAX_GEN_PER_TICK = 1

# За один тик публикуем не больше стольких постов НА КАНАЛ.
#
# Найдено владельцем 02.08: «7 постов опубликовалось за несколько минут».
# Накопившаяся просрочка (канал стоял на паузе, сервер был недоступен, время
# постов схлопнулось прошлым багом) выплёвывалась одним заходом -- подписчики
# получали пачку постов подряд. Публикация задумана как «по одному, по
# расписанию»; всплеск в ленте канала -- это ровно то, за что от канала
# отписываются. Разбираем просрочку по одному посту за тик: через несколько
# минут канал сам возвращается к нормальному ритму.
MAX_PUBLISH_PER_TICK_PER_CHANNEL = 1


def queue_target_for_user(s, user_id: int, channel: Optional[Channel] = None) -> int:
    """
    Сколько готовых постов держим наготове. Платящему -- неделя вперёд, всем
    остальным -- стартовые 3. Это потолок тарифа.

    Признак оплаты -- любой платёж со статусом "paid" (User.plan в схеме есть,
    но не используется нигде в коде, полагаться на него нельзя). Отдельная
    система тарифов для этого не нужна и намеренно не заводится.

    Владелец 01.08 (C14): в пределах потолка тарифа пользователь может
    настроить глубину очереди канала (`Channel.queue_depth`, от
    MIN_QUEUE_DEPTH до потолка) -- поэтому передаём channel, когда он под
    рукой. Без channel (или если queue_depth не задан) -- поведение как
    раньше: весь потолок тарифа целиком.

    Нижняя граница -- MIN_QUEUE_DEPTH (1), а не MIN_QUEUE (3): владелец
    02.08 попросил разрешить «держать наготове ровно один пост». Раньше
    зажим стоял по MIN_QUEUE, и значения 1 и 2 молча превращались в 3 --
    степпер показывал бы выбранное, а очередь набирала бы своё.
    """
    from sqlmodel import select as sel
    paid = s.exec(
        sel(Payment).where(Payment.user_id == user_id, Payment.status == "paid")
    ).first()
    ceiling = PAID_QUEUE if paid else MIN_QUEUE
    if channel and channel.queue_depth:
        return max(MIN_QUEUE_DEPTH, min(channel.queue_depth, ceiling))
    return ceiling


def _clamp_to_publish_window(channel: Channel, slot: datetime) -> datetime:
    """
    Двигает время внутрь окна публикации канала («Когда писать посты»).
    Раньше эта арифметика жила только внутри _next_slot_after, из-за чего
    первый пост в пустой очереди (там время бралось как «сейчас») выходил
    в канал в любое время суток мимо окна -- аудит 02.08.

    Окно без обоих концов -- ограничения нет. Кривые значения игнорируем
    молча: настройка не должна уметь заблокировать публикацию совсем.
    """
    ws, we = channel.publish_window_start, channel.publish_window_end
    if not (ws and we):
        return slot
    try:
        wsh, wsm = map(int, ws.split(":"))
        weh, wem = map(int, we.split(":"))
    except Exception:
        return slot
    window_start = slot.replace(hour=wsh, minute=wsm, second=0, microsecond=0)
    window_end = slot.replace(hour=weh, minute=wem, second=0, microsecond=0)
    if slot < window_start:
        return window_start
    if slot > window_end:
        return (slot + timedelta(days=1)).replace(hour=wsh, minute=wsm, second=0, microsecond=0)
    return slot


def _next_slot_after(channel: Channel, anchor: datetime) -> datetime:
    """
    Следующий слот очереди после anchor -- шаг вперёд по расписанию канала
    (интервал+окно публикации, либо ближайшее время из daily_times), от
    ПРОИЗВОЛЬНОЙ точки, а не только от channel.last_generated_at.

    Нужна отдельно от project_upcoming_slots (та считает прогноз для
    календаря именно от last_generated_at, см. C12) -- здесь anchor обычно
    это scheduled_at последнего поста, УЖЕ стоящего в очереди, который к
    моменту вызова может быть сильно позже last_generated_at.
    """
    if channel.schedule_kind == "daily":
        try:
            times = sorted(json.loads(channel.daily_times or "[]"))
        except Exception:
            times = []
        if not times:
            return anchor + timedelta(hours=24)
        cursor = anchor
        for _ in range(400):
            for hhmm in times:
                try:
                    hh, mm = map(int, hhmm.split(":"))
                except Exception:
                    continue
                candidate = cursor.replace(hour=hh, minute=mm, second=0, microsecond=0)
                if candidate > anchor:
                    return candidate
            cursor = cursor + timedelta(days=1)
        return anchor + timedelta(hours=24)

    base_seconds = max(60, channel.interval_hours * 3600)
    # Разброс ±N минут («чтобы посты появлялись не по секундомеру»). Настройка
    # существовала в БД и на экране, но её не читала ни одна функция
    # планирования -- обещание висело впустую (аудит 02.08). Детерминированный
    # сдвиг от anchor, а не random: одна и та же очередь при пересчёте не
    # должна «прыгать», иначе таймер на экране дёргался бы на каждой
    # перерисовке.
    jitter = max(0, min(getattr(channel, "interval_jitter_minutes", 0) or 0, 120))
    if jitter:
        span = jitter * 2 + 1
        offset = (int(anchor.timestamp()) + (channel.id or 0)) % span - jitter
        base_seconds += offset * 60
        base_seconds = max(60, base_seconds)
    slot = _clamp_to_publish_window(channel, anchor + timedelta(seconds=base_seconds))
    return slot


def _next_queue_slot(s, channel: Channel) -> datetime:
    """
    Время публикации для НОВОГО поста в очереди -- сразу после уже стоящих
    в очереди (единая модель очереди, C14, решение владельца 01-02.08).

    КРИТИЧНО (прод-инцидент 02.08, найден владельцем в тот же день): раньше
    условие было `last.scheduled_at > now` -- то есть последний пост в
    очереди считался "как будто его нет" не только когда очередь ПУСТА, но и
    когда его время уже наступило (он вот-вот опубликуется, но ещё не успел).
    Для автопилота с MAX_GEN_PER_TICK=1 это КАЖДЫЙ раз, когда пост
    публикуется: due_scheduled_posts забирает пост ровно в момент, когда
    `scheduled_at <= now`, то есть условие `> now` для него уже ложно -- и
    _refill_queue, вызванный тут же из post_publish_followup, планировал
    следующий пост на "сейчас" вместо "+interval_hours". Раз в тик
    (TICK_SECONDS, обычно 60с) публиковался ещё один пост -- интервал канала
    (например, раз в сутки) схлопывался до частоты тика, а токены сгорали
    впустую. Воспроизведено тестом test_next_queue_slot_after_publish_
    autopilot_respects_interval (прежде чем чинить, тест ловил баг: --0.00ч
    вместо +24ч).

    Правильно: пока в очереди есть хоть один пост со статусом "scheduled" --
    неважно, наступило его время или уже чуть просрочено -- новый пост
    планируется от НЕГО (`_next_slot_after`), а не от "сейчас". На "сейчас"
    (для автопилота) или "сейчас + SOFT_CONTROL_APPROVAL_MINUTES" (для
    подтверждения) переходим только когда в очереди действительно пусто --
    ни одного поста со статусом "scheduled" вообще нет.
    """
    now = datetime.utcnow()
    # `scheduled_at IS NOT NULL` в условии -- не украшение (найдено прогоном
    # на настоящем Postgres 03.08). Postgres в `ORDER BY ... DESC` ставит NULL
    # ПЕРВЫМИ, SQLite -- последними. Пост со статусом "scheduled", но ещё без
    # времени существует ровно одно мгновение: в sync_posts_to_channel_mode
    # между `p.status = "scheduled"` и присвоением scheduled_at. Autoflush
    # успевает записать это промежуточное состояние перед нашим SELECT -- и
    # на Postgres `.first()` возвращал сам этот пост с NULL. Условие
    # `if last and last.scheduled_at` тогда ложно, очередь считалась пустой,
    # и КАЖДЫЙ черновик вставал на «сейчас» вместо своего слота: при
    # включении автопилота с несколькими онбординг-черновиками они уходили
    # к подписчикам пачкой. На SQLite тест этого не видел -- сортировка
    # прятала баг.
    last = s.exec(
        select(Post).where(
            Post.channel_id == channel.id, Post.status == "scheduled",
            Post.scheduled_at.is_not(None),
        ).order_by(Post.scheduled_at.desc())
    ).first()
    if last and last.scheduled_at:
        return _next_slot_after(channel, last.scheduled_at)

    # Очередь пуста. Раньше здесь стояло голое `return now` для автопилота --
    # и первый пост уходил подписчикам на ближайшем тике, в любое время
    # суток, мимо окна публикации канала (аудит 02.08). Для человека, который
    # только что подключил канал или пополнил баланс, это выглядит как
    # «сервис постит когда попало», а обещание «каждые N часов» нарушается
    # на самом первом посте. Первый пост тоже обязан попадать в окно.
    base = now if channel.auto_publish else now + timedelta(minutes=config.SOFT_CONTROL_APPROVAL_MINUTES)
    return _clamp_to_publish_window(channel, base)


async def _refill_queue(channel_id: int):
    """
    Держит очередь канала заполненной до целевой глубины -- одинаково для
    обоих режимов публикации (единая модель очереди, C14, решение владельца
    01-02.08).

    Раньше автопилот и режим подтверждения жили по разным правилам:
    автопилот генерировал и публиковал один пост по истечении интервала,
    подтверждение -- держало резерв на глубину queue_target_for_user, но
    только НЕ для автопилота (см. C10 в PRODUCT_ROADMAP.md: раньше резерв
    рос и для автопилота тоже, а публиковать эти посты было некому -- они
    зависали в очереди навсегда).

    Теперь у ОБОИХ режимов посты всегда получают scheduled_at и публикуются
    через один и тот же путь (due_scheduled_posts/tick, см. generate_for_channel)
    -- разница только в том, нужно ли подтверждение прежде чем время
    наступит. Пополнение очереди больше не публикует ничего преждевременно
    (просто добавляет будущий слот), поэтому C10 здесь повториться не может:
    любой лишний пост просто получит более позднее время публикации, а не
    зависнет без способа когда-либо быть опубликованным.
    """
    with session() as s:
        channel = s.get(Channel, channel_id)
        if not channel or not channel.enabled:
            return
        # Стоп после MAX_GEN_FAIL_STREAK неудач подряд. Без него любой отказ,
        # после которого пост не создаётся, превращается в вечный цикл раз в
        # минуту -- ровно то, что случилось 03.08. Ручная кнопка не проходит
        # через _refill_queue и продолжает работать: человек всегда может
        # попросить попробовать ещё раз, увидев причину на экране.
        if (channel.gen_fail_streak or 0) >= MAX_GEN_FAIL_STREAK:
            return
        pending_count = len(s.exec(
            select(Post).where(
                Post.channel_id == channel_id,
                Post.status.in_(["pending", "scheduled"]),
            )
        ).all())
        target = queue_target_for_user(s, channel.user_id, channel)

    if pending_count >= target:
        return

    for _ in range(min(target - pending_count, MAX_GEN_PER_TICK)):
        try:
            result = await generate_for_channel(channel_id)
            if not result.get("ok"):
                break  # баланс кончился, генерация уже идёт, или другая ошибка
        except Exception as e:
            logger.warning(f"пополнение очереди канала {channel_id}: {e}")
            break


def _queue_len(s, channel_id: int) -> int:
    return len(s.exec(
        select(Post).where(
            Post.channel_id == channel_id,
            Post.status.in_(["pending", "scheduled"]),
        )
    ).all())


async def resume_starved_channels(user_id: int):
    """
    После пополнения баланса (обычная оплата, апгрейд тарифа, ручное
    начисление) пробуем сразу дополнить очередь, не дожидаясь ближайшего
    планового тика.

    Найдено владельцем 31.07: пополнил баланс, проверил канал -- в очереди
    всё ещё 0. Генерация молчаливо блокируется нулевым балансом (см.
    generate_for_channel), и до этой правки ждать возобновления приходилось
    до ближайшего тика планировщика.

    В единой модели очереди (C14) пополнение больше не может создать
    внеочередную публикацию: новый пост просто получает следующий свободный
    слот расписания (см. _next_queue_slot), а не публикуется сam -- поэтому,
    в отличие от прежней версии, здесь не нужно различать автопилот и режим
    подтверждения: _refill_queue безопасен для обоих всегда.
    """
    with session() as s:
        channel_ids = [c.id for c in s.exec(select(Channel).where(
            Channel.user_id == user_id,
            Channel.enabled == True,   # noqa
            Channel.verified == True,  # noqa
        )).all()]

    # Пополнение баланса -- изменившиеся обстоятельства: генерация могла
    # падать именно из-за нуля. Снимаем стоп по неудачам подряд (инцидент
    # 03.08), иначе _refill_queue выйдет на нём и деньги не заработают.
    for cid in channel_ids:
        reset_generation_failures(cid)

    for cid in channel_ids:
        try:
            await _refill_queue(cid)
        except Exception as e:
            logger.warning(f"resume_starved_channels: канал {cid}: {e}")


async def charge_due_subscriptions():
    """
    Автосписание по подпискам, у которых наступила дата продления.

    Защита от двойного списания двухслойная:
      1. Idempotence-Key детерминированный -- "sub-{id}-period-{n}". Если
         запрос ушёл в YooKassa, но ответ потерялся и джоба перезапустилась,
         YooKassa по тому же ключу вернёт ТОТ ЖЕ платёж, а не создаст новый.
      2. last_period_key в БД: период, который уже успешно оплачен, второй раз
         не обрабатываем даже локально.

    Неудачное списание не роняет подписку сразу: пробуем SUBSCRIPTION_MAX_FAILS
    раз с паузой SUBSCRIPTION_RETRY_HOURS, и только потом переводим в
    suspended. Токены начисляем только после реального succeeded.
    """
    import billing
    from database import Subscription, Payment as _Payment

    if not billing.is_configured():
        return
    if not config.SUBSCRIPTION_ENABLED:
        # Рекуррент не согласован с ЮKassa -- списывать нечем и незачем.
        return

    now = datetime.utcnow()
    with session() as s:
        due = s.exec(select(Subscription).where(
            Subscription.status == "active",
            Subscription.next_charge_at != None,  # noqa: E711
            Subscription.next_charge_at <= now,
        )).all()
        due_ids = [x.id for x in due]

    for sub_id in due_ids:
        with session() as s:
            sub = s.get(Subscription, sub_id)
            if not sub or sub.status != "active":
                continue
            period_key = f"sub-{sub.id}-period-{sub.period_no}"
            if sub.last_period_key == period_key:
                # Этот период уже оплачен -- просто двигаем дату вперёд.
                sub.period_no += 1
                sub.next_charge_at = now + timedelta(days=config.SUBSCRIPTION_PERIOD_DAYS)
                s.add(sub); s.commit()
                continue
            if not sub.payment_method_id:
                sub.status = "suspended"
                sub.last_error = "нет сохранённого метода оплаты"
                s.add(sub); s.commit()
                logger.warning(f"Подписка {sub.id}: нет payment_method_id, приостановлена")
                continue
            pkg = config.package_by_id(sub.package_id)
            if not pkg:
                sub.status = "suspended"
                sub.last_error = f"тариф {sub.package_id} больше не существует"
                s.add(sub); s.commit()
                logger.error(f"Подписка {sub.id}: неизвестный тариф {sub.package_id}")
                continue
            user = s.get(User, sub.user_id)
            user_email = user.email if user else None
            uid, pkg_id = sub.user_id, sub.package_id
            # КРИТИЧНО: списываем цену, ЗАФИКСИРОВАННУЮ при оформлении, а не
            # текущую из конфига. Оферта (п. 3) и плашка на тарифах обещают
            # подписчику сохранение его цены -- если брать pkg["rub"],
            # повышение цен молча подняло бы списания уже подписанным, то есть
            # мы нарушили бы собственный договор. Фолбэк на текущую цену -- для
            # строк, созданных до появления price_rub.
            charge_rub = sub.price_rub or pkg["rub"]

        # Метка платежа детерминированная (равна period_key), БЕЗ случайной
        # части. Это второй рубеж защиты от двойного начисления: если процесс
        # упал ПОСЛЕ успешного списания, но ДО записи last_period_key, то при
        # повторном запуске YooKassa по тому же Idempotence-Key вернёт тот же
        # платёж (денег спишется столько же), а вот токены мы бы начислили
        # второй раз. Поэтому перед списанием проверяем, нет ли уже
        # оплаченного Payment за этот период.
        label = period_key
        with session() as s:
            existing = s.exec(select(_Payment).where(_Payment.label == label)).all()
            already_paid = next((p for p in existing if p.status == "paid"), None)
            if already_paid:
                sub = s.get(Subscription, sub_id)
                if sub:
                    sub.last_period_key = period_key
                    sub.period_no += 1
                    sub.next_charge_at = datetime.utcnow() + timedelta(days=config.SUBSCRIPTION_PERIOD_DAYS)
                    sub.fail_count = 0
                    s.add(sub); s.commit()
                logger.warning(
                    f"Подписка {sub_id}: период {period_key} уже оплачен -- "
                    f"повторное начисление пропущено, дата продления сдвинута"
                )
                continue
            # Переиспользуем зависший pending от прошлой неудачной попытки,
            # чтобы не плодить мусорные строки на каждый ретрай.
            pay = next((p for p in existing if p.status == "pending"), None)
            if pay is None:
                pay = _Payment(
                    user_id=uid, package_id=pkg_id, label=label,
                    rub=charge_rub, tokens=pkg["tokens"], status="pending",
                )
                s.add(pay); s.commit(); s.refresh(pay)
            pay_id = pay.id

        try:
            result = await billing.charge_recurring(
                payment_method_id=sub.payment_method_id,
                amount_rub=charge_rub,
                description=f"Автопост: продление подписки «{pkg['title']}»",
                user_id=uid,
                package_id=pkg_id,
                label=label,
                idempotence_key=period_key,
                user_email=user_email,
            )
            status = result.get("status", "")
            paid = bool(result.get("paid"))
        except Exception as e:
            # ВАЖНО отличать "YooKassa ответила отказом" от "мы не узнали
            # исход". При обрыве связи деньги могли уже списаться, и помечать
            # такой платёж failed нельзя -- он остаётся pending, а повторная
            # попытка с тем же Idempotence-Key вернёт настоящий статус.
            status, paid, result, outcome_known = "error", False, {}, False
            logger.warning(f"Подписка {sub_id}: списание не удалось: {e}")
        else:
            outcome_known = True

        with session() as s:
            sub = s.get(Subscription, sub_id)
            pay = s.get(_Payment, pay_id)
            if not sub:
                continue
            if status == "succeeded" and paid:
                if pay:
                    pay.status = "paid"
                    pay.paid_at = datetime.utcnow()
                    pay.operation_id = result.get("id", "")
                    s.add(pay)
                u = s.get(User, sub.user_id)
                if u:
                    u.token_balance += pkg["tokens"]
                    s.add(u)
                sub.last_period_key = period_key
                sub.period_no += 1
                sub.next_charge_at = datetime.utcnow() + timedelta(days=config.SUBSCRIPTION_PERIOD_DAYS)
                sub.fail_count = 0
                sub.last_error = ""
                s.add(sub); s.commit()
                logger.info(f"Подписка {sub_id} продлена: пользователь {sub.user_id} +{pkg['tokens']} токенов")
            else:
                # failed ставим только когда исход точно известен и он
                # отрицательный. Иначе оставляем pending -- см. выше.
                if pay and outcome_known and pay.status == "pending":
                    pay.status = "failed"
                    s.add(pay)
                sub.fail_count += 1
                sub.last_error = f"status={status}"
                if sub.fail_count >= config.SUBSCRIPTION_MAX_FAILS:
                    sub.status = "suspended"
                    logger.warning(f"Подписка {sub_id} приостановлена после {sub.fail_count} неудач")
                else:
                    # Повторим позже, тот же period_no -> тот же Idempotence-Key.
                    sub.next_charge_at = datetime.utcnow() + timedelta(hours=config.SUBSCRIPTION_RETRY_HOURS)
                s.add(sub); s.commit()


async def daily_quality_check():
    """
    Ежедневный автоматический контроль качества: прогоняет quality-scan и, если
    нашлись критичные проблемы, присылает владельцу сводку в Telegram.

    Смысл именно в уведомлении, а не в эндпоинте. Дефекты вроде постов-
    близнецов не видны ни в коде, ни в метриках -- их замечает только тот, кто
    смотрит на реальные посты. Рассчитывать, что кто-то будет делать это
    регулярно вручную, нельзя. Поэтому сервис сам проверяет свой результат и
    сам сообщает, когда с ним что-то не так.

    Получатели -- пользователи с is_admin и подключённым Telegram. Если таких
    нет, находки всё равно остаются в логах.
    """
    try:
        from internal_quality_scan import quality_scan
    except Exception as e:
        logger.warning(f"quality-scan недоступен: {e}")
        return

    try:
        # Вызываем напрямую, минуя HTTP: свой же процесс, авторизация не нужна.
        import internal_quality_scan as _qs
        token = _qs.INTERNAL_API_TOKEN
        if not token:
            logger.info("daily_quality_check: TRUEPOST_INTERNAL_API_TOKEN не задан, проверка пропущена")
            return
        report = quality_scan(period_days=1, authorization=f"Bearer {token}")
    except Exception as e:
        logger.warning(f"daily_quality_check: скан не отработал: {e}")
        return

    high = [f for f in report.get("findings", []) if f.get("severity") == "high"]
    logger.info(
        f"[quality-scan] постов={report['scanned']['posts']} "
        f"находок={len(report.get('findings', []))} критичных={len(high)}"
    )
    for f in report.get("findings", []):
        logger.info(f"[quality-scan] {f['severity']}: {f['title']} -- {f['count']}")

    if not high:
        return

    lines = ["⚠️ <b>АвтоПост: проверка качества</b>", ""]
    for f in high:
        lines.append(f"• <b>{f['title']}</b> — {f['count']}")
    lines.append("")
    lines.append("Подробности с примерами: /api/internal/quality-scan")
    text = "\n".join(lines)

    with session() as s:
        admins = [
            u.tg_chat_id for u in s.exec(select(User).where(User.is_admin == True)).all()  # noqa
            if u.tg_chat_id
        ]
    if not admins:
        logger.info("daily_quality_check: нет админов с подключённым Telegram, только лог")
        return
    for chat_id in admins:
        try:
            await _notify_user_by_id(chat_id, text)
        except Exception as e:
            logger.warning(f"daily_quality_check: не отправилось {chat_id}: {e}")


async def tick():
    now = datetime.utcnow()

    # Polling Telegram bot updates для привязки chat_id (publishing bot)
    try:
        await _process_bot_updates()
    except Exception as e:
        logger.warning(f"bot polling: {e}")

    # Единая модель очереди (C14): держим целевую глубину очереди для ВСЕХ
    # верифицированных каналов одинаково, независимо от режима публикации --
    # разница между автопилотом и подтверждением не в том, генерируем ли мы
    # посты заранее, а в том, нужно ли подтверждение перед их публикацией.
    with session() as s:
        all_verified_ids = [c.id for c in s.exec(select(Channel).where(
            Channel.enabled == True,   # noqa
            Channel.verified == True,  # noqa
        )).all()]

    for cid in all_verified_ids:
        try:
            await _refill_queue(cid)
        except Exception as e:
            logger.warning(f"queue-refill канал {cid}: {e}")

    # Публикация: только автопилот (режим подтверждения через тик не
    # публикуется никогда, см. database.due_scheduled_posts).
    #
    # КРИТИЧНО (аудит 02.08, жалоба владельца «7 постов за несколько минут»):
    # не больше MAX_PUBLISH_PER_TICK_PER_CHANNEL постов на канал за тик. Если
    # накопилась просрочка (канал стоял на паузе, сервер лежал, время постов
    # схлопнулось прошлым багом), раньше тик выплёвывал ВСЮ пачку разом --
    # подписчики получали несколько постов подряд за минуту. Просрочка
    # разбирается по одному посту за тик: канал возвращается к нормальному
    # ритму сам, без ручного вмешательства и без всплеска в ленте.
    with session() as s:
        from database import due_scheduled_posts
        due_posts = due_scheduled_posts(s, now)
        # Самые просроченные -- первыми, чтобы очередь разбиралась по порядку
        due_posts.sort(key=lambda p: (p.scheduled_at or now))
        by_channel: dict = {}
        due_ids = []
        for p in due_posts:
            taken = by_channel.get(p.channel_id, 0)
            if taken >= MAX_PUBLISH_PER_TICK_PER_CHANNEL:
                continue
            by_channel[p.channel_id] = taken + 1
            due_ids.append(p.id)
        skipped = len(due_posts) - len(due_ids)
    if skipped:
        logger.info(f"tick: отложено публикаций до следующего тика: {skipped} (защита от всплеска)")

    for pid in due_ids:
        try:
            result = await publish_post(pid)
            if result.get("ok"):
                await post_publish_followup(pid)
        except Exception as e:
            logger.error(f"tick: пост {pid}: {e}")

    # За SOFT_CONTROL_WARNING_MINUTES до дедлайна подтверждения -- предупреждение.
    with session() as s:
        from database import approvals_needing_warning
        warnings = [
            (a.id, a.post_id, a.review_chat_id, a.review_message_id)
            for a in approvals_needing_warning(s, now, config.SOFT_CONTROL_WARNING_MINUTES)
        ]

    for approval_id, post_id, review_chat_id, review_message_id in warnings:
        try:
            await _send_approval_warning(approval_id, post_id, review_chat_id, review_message_id)
        except Exception as e:
            logger.error(f"tick: approval-warning пост {post_id}: {e}")

    # Дедлайн подтверждения истёк без реакции — перенос в конец очереди
    # (НЕ публикация, см. _requeue_unconfirmed_post).
    with session() as s:
        from database import due_post_approvals
        due_approvals = [(a.id, a.post_id, a.review_chat_id, a.review_message_id) for a in due_post_approvals(s, now)]

    for approval_id, post_id, review_chat_id, review_message_id in due_approvals:
        try:
            await _requeue_unconfirmed_post(approval_id, post_id, review_chat_id, review_message_id)
        except Exception as e:
            logger.error(f"tick: approval-timeout пост {post_id}: {e}")
