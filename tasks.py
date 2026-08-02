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
from database import session, Channel, ChannelRule, Source, Post, User, TrafficAttribution, PostApproval, Payment
from sqlmodel import select

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
# сравнение точных словоформ их не сопоставляет. На реальной паре постов-
# близнецов совпадение по словоформам вышло 0.10, то есть детектор их не
# видел вовсе. Грубое усечение до основы решает это без словарей и
# зависимостей.
_STEM_LEN = 6


def _content_words(text: str) -> set:
    """Основы значимых слов: без разметки, коротких слов и частотного мусора."""
    clean = re.sub(r"<[^>]+>", " ", text or "").lower().replace("ё", "е")
    words = re.findall(r"[а-яa-z0-9]{4,}", clean)
    return {w[:_STEM_LEN] for w in words if w not in _DUP_STOPWORDS}


def _similarity(a: str, b: str) -> float:
    """
    Доля общих значимых слов (Жаккар). Два поста про одно событие делят
    редкие слова -- имена, даты, названия -- и дают заметно высокий
    коэффициент, тогда как разные темы почти не пересекаются.
    """
    wa, wb = _content_words(a), _content_words(b)
    if not wa or not wb:
        return 0.0
    return len(wa & wb) / len(wa | wb)


# Порог откалиброван на реальной паре постов-близнецов с продового канала
# (два поста про тайное крещение Путина, созданные в одну минуту) и на парах
# «разные события внутри одной темы канала»:
#   близнецы                      0.171
#   разные события, общая тема     0.000 - 0.030
# Разрыв пятикратный, поэтому 0.10 надёжно ловит первое и не задевает второе.
#
# Смещение намеренно в сторону лишних срабатываний: ложная перегенерация
# стоит около рубля, а пропущенный близнец пользователь видит в своём канале
# -- именно на это и была жалоба.
DUPLICATE_THRESHOLD = float(os.getenv("DUPLICATE_THRESHOLD", "0.10"))


def _find_duplicate(text: str, existing: list) -> tuple:
    """Возвращает (совпавший_текст, коэффициент) или (None, 0.0)."""
    best, score = None, 0.0
    for other in existing:
        sim = _similarity(text, other)
        if sim > score:
            best, score = other, sim
    return (best, score) if score >= DUPLICATE_THRESHOLD else (None, score)


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
                                target_scheduled_at: Optional[datetime] = None) -> dict:
    """
    Тонкая обёртка вокруг _generate_for_channel_impl -- выставляет
    Channel.generating_since ДО тяжёлой работы (классификация темы, поиск,
    сам запрос к модели) и снимает его ПОСЛЕ, независимо от результата
    (try/finally: исключение внутри генерации не должно оставить канал
    навсегда "генерирующим" на экране).
    """
    await _set_generating(channel_id, True)
    try:
        return await _generate_for_channel_impl(channel_id, topic, force_pending, target_scheduled_at)
    finally:
        await _set_generating(channel_id, False)


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
        text, tokens = await generator.generate_post(channel, material, topic, rules_text, recent_titles)
    except generator.GenerationError as e:
        # Понятная ошибка — показываем как есть
        logger.info(
            f"Канал {channel_id}: generation_failed_reason=generation_error "
            f"user_input_topic=«{topic_to_classify[:80]}» topic_classification={classification} "
            f"generation_mode={generation_mode}"
        )
        return {"ok": False, "message": str(e)}
    except Exception as e:
        logger.error(f"Ошибка генерации канала {channel_id}: {e}")
        logger.info(
            f"Канал {channel_id}: generation_failed_reason=exception "
            f"user_input_topic=«{topic_to_classify[:80]}» topic_classification={classification} "
            f"generation_mode={generation_mode}"
        )
        return {"ok": False, "message": "Временная ошибка. Попробуйте ещё раз через минуту."}

    # Логирование для диагностики (Part 7 задачи): видно какая тема пришла,
    # как классифицирована, какой режим генерации и какая тема в итоге вышла.
    final_topic_line = text.strip().split("\n")[0][:100] if text else ""
    logger.info(
        f"Канал {channel_id}: user_input_topic=«{topic_to_classify[:80]}» "
        f"topic_classification={classification} generation_mode={generation_mode} "
        f"final_post_topic=«{final_topic_line}»"
    )

    if _looks_like_menu(text):
        return {"ok": False, "message": "ИИ не смог определить тему. Задайте тему поста вручную."}

    # Пост-проверка на близнеца. Инструкции в промпте оказалось недостаточно:
    # у новостного канала выдача поиска между генерациями одна и та же, и
    # указание «используй только эти факты» перевешивает «возьми новое
    # событие» -- получается тот же факт под новым заголовком. Сравниваем
    # готовый текст с тем, что уже лежит в очереди и опубликовано, и при
    # совпадении переписываем один раз, прямо назвав, что повторять нельзя.
    dup, score = _find_duplicate(text, recent_texts)
    if dup:
        dup_head = re.sub(r"<[^>]+>", "", dup.strip().split("\n")[0])[:100]
        logger.warning(
            f"Канал {channel_id}: пост дублирует уже существующий "
            f"(совпадение {score:.2f}) «{dup_head}» -- перегенерирую"
        )
        try:
            retry_hint = (
                f"{topic}\n\n" if topic else ""
            ) + (
                "ЗАПРЕЩЕНО писать про это событие -- пост о нём уже есть:\n"
                f"{re.sub(r'<[^>]+>', '', dup)[:600]}\n\n"
                "Возьми СОВЕРШЕННО ДРУГОЕ событие или факт. Не пересказывай то же самое "
                "другими словами и не меняй только заголовок."
            )
            text2, tokens2 = await generator.generate_post(
                channel, material, retry_hint, rules_text, recent_titles
            )
            tokens += tokens2
            dup2, score2 = _find_duplicate(text2, recent_texts)
            if dup2:
                # Вторая попытка тоже про то же самое -- значит по этой теме
                # свежих событий действительно нет. Лучше не выдавать
                # пользователю близнеца вовсе: очередь просто останется
                # короче, и следующий тик попробует снова.
                logger.warning(
                    f"Канал {channel_id}: повтор и после перегенерации "
                    f"(совпадение {score2:.2f}) -- пост не создаю"
                )
                return {
                    "ok": False,
                    "message": "Свежих событий по теме пока нет — попробуем позже.",
                    "duplicate_skipped": True,
                }
            text = text2
        except generator.GenerationError as e:
            logger.warning(f"Канал {channel_id}: перегенерация не удалась ({e}), оставляю исходный пост")

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

    result = await telegram_api.send_message(chat, text)
    if not result.get("ok"):
        # Сырой Telegram description никогда не попадает в message напрямую —
        # логируем отдельно, пользователю отдаём только нормализованный текст.
        raw_desc = result.get("description", "")
        logger.warning(f"Пост {post_id}: ошибка публикации в Telegram, raw_telegram_error=«{raw_desc}»")
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
        try:
            await _refill_queue(channel_id)
        except Exception as e:
            logger.warning(f"auto-refill failed для поста {post_id}: {e}")


def project_upcoming_slots(channel: Channel, now: datetime, count: int = 30) -> list:
    """Прогноз следующих `count` моментов автопубликации по ТЕКУЩИМ настройкам
    расписания -- для календаря в кабинете (владелец 28.07 попросил, чтобы
    смена частоты сразу отражалась в календаре, а не только в момент, когда
    пост реально опубликован).

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
        cursor = channel.last_generated_at or now
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


def queue_target_for_user(s, user_id: int, channel: Optional[Channel] = None) -> int:
    """
    Сколько готовых постов держим наготове. Платящему -- неделя вперёд, всем
    остальным -- стартовые 3. Это потолок тарифа.

    Признак оплаты -- любой платёж со статусом "paid" (User.plan в схеме есть,
    но не используется нигде в коде, полагаться на него нельзя). Отдельная
    система тарифов для этого не нужна и намеренно не заводится.

    Владелец 01.08 (C14): в пределах потолка тарифа пользователь может
    настроить глубину очереди канала (`Channel.queue_depth`, от MIN_QUEUE до
    потолка) -- поэтому передаём channel, когда он под рукой. Без channel
    (или если queue_depth не задан) -- поведение как раньше: весь потолок
    тарифа целиком.
    """
    from sqlmodel import select as sel
    paid = s.exec(
        sel(Payment).where(Payment.user_id == user_id, Payment.status == "paid")
    ).first()
    ceiling = PAID_QUEUE if paid else MIN_QUEUE
    if channel and channel.queue_depth:
        return max(MIN_QUEUE, min(channel.queue_depth, ceiling))
    return ceiling


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
    slot = anchor + timedelta(seconds=base_seconds)
    ws, we = channel.publish_window_start, channel.publish_window_end
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
    return slot


def _next_queue_slot(s, channel: Channel) -> datetime:
    """
    Время публикации для НОВОГО поста в очереди -- сразу после уже стоящих
    в очереди (единая модель очереди, C14, решение владельца 01-02.08).

    Пустая очередь (или последний слот уже в прошлом) -- новый слот от
    "сейчас". Для режима подтверждения при этом гарантируем минимум
    SOFT_CONTROL_APPROVAL_MINUTES на решение -- иначе первый пост в пустой
    очереди получил бы дедлайн "прямо сейчас". Автопилоту это не нужно --
    решения он ни от кого не ждёт.
    """
    now = datetime.utcnow()
    last = s.exec(
        select(Post).where(Post.channel_id == channel.id, Post.status == "scheduled")
        .order_by(Post.scheduled_at.desc())
    ).first()
    if last and last.scheduled_at and last.scheduled_at > now:
        return _next_slot_after(channel, last.scheduled_at)
    if channel.auto_publish:
        return now
    return now + timedelta(minutes=config.SOFT_CONTROL_APPROVAL_MINUTES)


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
                break  # баланс кончился или другая ошибка -- не долбим тик впустую
        except Exception as e:
            logger.warning(f"пополнение очереди канала {channel_id}: {e}")
            break


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

    # Публикация: автопилот и явно запланированные ("Запланировать") посты.
    # Посты режима подтверждения, ещё ожидающие решения, due_scheduled_posts
    # сама исключает (см. database.py) -- их дедлайн обрабатывается ниже.
    with session() as s:
        from database import due_scheduled_posts
        due_posts = [p.id for p in due_scheduled_posts(s, now)]

    for pid in due_posts:
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
