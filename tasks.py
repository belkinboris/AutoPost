"""
Оркестрация задач: генерация, публикация, уведомления.
"""

import json
import logging
import random
import re
from datetime import datetime, timedelta
from typing import Optional

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


async def generate_for_channel(channel_id: int, topic: str = "", force_pending: bool = False) -> dict:
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
        with session() as s:
            existing_posts = s.exec(select(Post).where(Post.channel_id == channel_id)).first()
            if not existing_posts:
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
                first_line = p.text.strip().split("\n")[0]
                # Убираем HTML теги для читаемости
                import re as _re
                first_line = _re.sub(r"<[^>]+>", "", first_line).strip()
                if first_line:
                    titles.append(f"- {first_line[:120]}")
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

    with session() as s:
        channel = s.get(Channel, channel_id)
        user = s.get(User, channel.user_id)
        post = Post(
            channel_id=channel_id,
            user_id=channel.user_id,
            text=text,
            tokens_used=tokens,
            status="pending",
        )
        prev_balance = user.token_balance
        user.token_balance = max(0, user.token_balance - tokens)
        channel.last_generated_at = datetime.utcnow()

        if channel.auto_publish and not force_pending:
            result = await telegram_api.send_message(channel.tg_chat, text)
            if result.get("ok"):
                post.status = "published"
                post.published_at = datetime.utcnow()
                post.tg_message_id = result["result"].get("message_id")

        s.add(post); s.add(user); s.add(channel)
        s.commit(); s.refresh(post)
        pid = post.id

        # Читаем всё необходимое для уведомлений пока сессия открыта
        notify_chat_id = user.tg_chat_id
        notify_pub = user.notify_published and post.status == "published"
        notify_low = user.notify_low_tokens and prev_balance > LOW_TOKENS_THRESHOLD and user.token_balance <= LOW_TOKENS_THRESHOLD
        chan_title = channel.title
        # "Публикация после подтверждения": канал не в автопилоте, это
        # регулярная генерация по расписанию (не ручной запрос из
        # приложения и не догенерация резерва очереди -- обе всегда
        # приходят с force_pending=True).
        #
        # КРИТИЧНО (fix): раньше здесь ещё стояло bool(user.tg_chat_id) --
        # без подключённых личных уведомлений в Telegram запись PostApproval
        # вообще не заводилась, а значит и таймер на 30 минут (весь смысл
        # режима "публикация после подтверждения") никогда не запускался.
        # Пост просто вечно висел в очереди как pending, хотя интерфейс
        # везде обещает "опубликуется сам через 30 мин, если не
        # отреагируете" -- обещание было ложным для любого пользователя без
        # подключённых уведомлений. Теперь таймер заводится всегда;
        # Telegram-карточка (см. _send_approval_card) -- лишь опциональный
        # дополнительный канал подтверждения поверх него, подтвердить или
        # отклонить всегда можно и на сайте, независимо от Telegram.
        needs_approval = (not channel.auto_publish) and (not force_pending) and post.status == "pending"
        approval_chat_id = user.tg_chat_id
        approval_channel_id = channel_id

    # Уведомления — вне сессии, с явным chat_id
    if notify_chat_id:
        if notify_pub:
            await _notify_user_by_id(notify_chat_id, f"✅ <b>Пост опубликован</b>\n\nКанал: {chan_title}")
        if notify_low:
            await _notify_user_by_id(notify_chat_id, f"⚠️ <b>Токены заканчиваются</b>\n\nОсталось ~1 пост. Пополните баланс в приложении.")

    if needs_approval:
        try:
            await _send_approval_card(pid, approval_channel_id, approval_chat_id, chan_title, text)
        except Exception as e:
            logger.warning(f"approval card для поста {pid}: {e}")

    # Если пост сразу опубликован (автопилот) — догенерируем очередь
    if pid:
        with session() as s:
            p = s.get(Post, pid)
            if p and p.status == "published":
                try:
                    await _ensure_queue(pid)
                except Exception as e:
                    logger.warning(f"auto-refill after publish: {e}")

    return {"ok": True, "message": "Черновик создан", "post_id": pid, "tokens_used": tokens, "text": text}


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
    редактирования) гарантирует минимум SOFT_CONTROL_FINAL_GRACE_SECONDS
    до следующей возможной публикации -- даже если исходный дедлайн уже
    прошёл, пост не публикуется в ту же секунду, что и правка.
    """
    floor = datetime.utcnow() + timedelta(seconds=config.SOFT_CONTROL_FINAL_GRACE_SECONDS)
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
    текста)."""
    preview = generator._clean_post(post_text)
    if len(preview) > 500:
        preview = preview[:500].rstrip() + "…"
    minutes_left = max(0, round((deadline - datetime.utcnow()).total_seconds() / 60))
    prefix = "✏️ <b>Текст обновлён.</b>\n\n" if edited else f"📝 <b>Новый пост для канала «{channel_title}»</b>\n\n"
    card_text = (
        f"{prefix}{preview}\n\n"
        f"⏱ Опубликуется автоматически через {minutes_left} мин, если не подтвердите раньше."
    )
    keyboard = _approval_keyboard(post_id)
    if message_id:
        return await telegram_api.edit_message_text(chat_id, message_id, card_text, keyboard=keyboard)
    return await telegram_api.send_dm_with_keyboard(chat_id, card_text, keyboard)


async def _send_approval_card(post_id: int, channel_id: int, chat_id: Optional[int], channel_title: str, post_text: str):
    """
    Заводит дедлайн и запись PostApproval для поста в режиме "публикация
    после подтверждения" -- ВСЕГДА, вне зависимости от того, подключены ли
    у пользователя личные уведомления в Telegram (chat_id может быть None).
    Карточка в Telegram -- опциональный дополнительный канал подтверждения
    поверх этого таймера, не обязательное условие для его работы:
    подтвердить/отклонить/отредактировать всегда можно и на сайте, через
    обычную очередь (см. /api/posts/{id}/publish и cancel_pending_approval).

    review_chat_id=0 -- сентинел "нет Telegram-карточки" (0 не может быть
    настоящим chat_id) вместо NULL, чтобы не менять тип существующей
    NOT NULL колонки на уже задеплоенной таблице.
    """
    deadline = datetime.utcnow() + timedelta(minutes=config.SOFT_CONTROL_APPROVAL_MINUTES)
    review_chat_id = 0
    review_message_id = None
    if chat_id:
        result = await _render_approval_card(chat_id, None, post_id, channel_title, post_text, deadline)
        if result.get("ok"):
            review_chat_id = chat_id
            review_message_id = result["result"].get("message_id")
        else:
            logger.warning(f"approval card для поста {post_id}: не удалось отправить, {result.get('description')}")
    with session() as s:
        s.add(PostApproval(
            post_id=post_id, channel_id=channel_id,
            review_chat_id=review_chat_id, review_message_id=review_message_id,
            deadline=deadline,
        ))
        s.commit()


async def _auto_publish_after_timeout(approval_id: int, post_id: int, review_chat_id: int, review_message_id: Optional[int]):
    """
    Вызывается из tick(), когда дедлайн подтверждения истёк без реакции.
    Двухфазно: сначала предупреждение + короткий финальный буфер
    (SOFT_CONTROL_FINAL_GRACE_SECONDS), и только потом реальная
    публикация -- мгновенный тик не должен публиковать пост без ни
    единого шанса передумать в последний момент.
    """
    with session() as s:
        approval = s.get(PostApproval, approval_id)
        if not approval or approval.status != "waiting":
            return  # уже обработано (нажали кнопку) между выборкой и этим тиком

        if not approval.final_warning_sent:
            # Фаза 1: предупреждаем и даём ещё немного времени, не публикуем
            approval.final_warning_sent = True
            approval.deadline = datetime.utcnow() + timedelta(seconds=config.SOFT_CONTROL_FINAL_GRACE_SECONDS)
            s.add(approval); s.commit()
            if review_message_id:
                try:
                    await telegram_api.edit_message_text(
                        review_chat_id, review_message_id,
                        f"⏳ Время вышло — публикую примерно через {config.SOFT_CONTROL_FINAL_GRACE_SECONDS} сек. "
                        f"Ещё можно нажать «Отклонить» или «Редактировать».",
                        keyboard=_approval_keyboard(post_id),
                    )
                except Exception as e:
                    logger.warning(f"approval timeout: не удалось отправить финальное предупреждение поста {post_id}: {e}")
            return

        # Фаза 2: буфер тоже прошёл — реальная публикация
        approval.status = "done"
        s.add(approval); s.commit()

        post = s.get(Post, post_id)
        if not post or post.status != "pending":
            return  # решено другим путём (например отклонено в приложении)

    result = await publish_post(post_id)
    if review_message_id:
        if result.get("ok"):
            text = "⏱ Время на подтверждение истекло — опубликовано автоматически."
        else:
            text = f"⚠️ Время на подтверждение истекло, но опубликовать не удалось: {result.get('message', 'ошибка')}"
        try:
            await telegram_api.edit_message_text(review_chat_id, review_message_id, text)
        except Exception as e:
            logger.warning(f"approval timeout: не удалось обновить карточку поста {post_id}: {e}")
    if result.get("ok"):
        await post_publish_followup(post_id)


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
            still_pending = bool(post and post.status == "pending")
        await telegram_api.answer_callback_query(cq_id, "Публикую…")
        if not still_pending:
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
            if post and post.status == "pending":
                post.status = "rejected"
                s.add(post)
            s.commit()
        await telegram_api.answer_callback_query(cq_id, "Отклонено.")
        await telegram_api.edit_message_text(chat_id, message_id, "🗑 Пост отклонён.")
        if channel_id:
            await _refill_if_active(channel_id)

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
    """
    notify_chat_id = None
    notify_title = ""
    try:
        with session() as s:
            post = s.get(Post, post_id)
            if not post:
                return
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

    try:
        await _ensure_queue(post_id)
    except Exception as e:
        logger.warning(f"auto-refill failed для поста {post_id}: {e}")


def _next_publish_time(channel: Channel, now: datetime) -> datetime:
    """Вычисляет следующее время генерации с учётом jitter и окна публикации."""
    base_seconds = channel.interval_hours * 3600
    jitter = channel.interval_jitter_minutes or 0
    if jitter > 0:
        delta = random.randint(-jitter * 60, jitter * 60)
        base_seconds = max(60, base_seconds + delta)

    next_time = (channel.last_generated_at or now) + timedelta(seconds=base_seconds)

    # Применяем окно публикации
    ws = channel.publish_window_start
    we = channel.publish_window_end
    if ws and we:
        try:
            wsh, wsm = map(int, ws.split(":"))
            weh, wem = map(int, we.split(":"))
            window_start = next_time.replace(hour=wsh, minute=wsm, second=0, microsecond=0)
            window_end = next_time.replace(hour=weh, minute=wem, second=0, microsecond=0)

            if next_time < window_start:
                next_time = window_start
            elif next_time > window_end:
                # Переносим на начало следующего дня
                next_time = (next_time + timedelta(days=1)).replace(hour=wsh, minute=wsm, second=0)
        except Exception:
            pass

    return next_time


def _is_due(channel: Channel, now: datetime) -> bool:
    if channel.schedule_kind == "interval":
        if channel.last_generated_at is None:
            # Первая генерация — проверяем окно если задано
            ws = channel.publish_window_start
            we = channel.publish_window_end
            if ws and we:
                try:
                    wsh, wsm = map(int, ws.split(":"))
                    weh, wem = map(int, we.split(":"))
                    cur_minutes = now.hour * 60 + now.minute
                    start_minutes = wsh * 60 + wsm
                    end_minutes = weh * 60 + wem
                    if not (start_minutes <= cur_minutes <= end_minutes):
                        return False
                except Exception:
                    pass
            return True
        # Строго проверяем что прошёл нужный интервал
        min_seconds = channel.interval_hours * 3600
        elapsed = (now - channel.last_generated_at).total_seconds()
        if elapsed < min_seconds * 0.9:  # 10% допуск
            return False
        next_time = _next_publish_time(channel, now)
        return now >= next_time

    if channel.schedule_kind == "daily":
        try:
            times = json.loads(channel.daily_times or "[]")
        except Exception:
            times = []
        hhmm = now.strftime("%H:%M")
        if hhmm in times:
            last = channel.last_generated_at
            if last is None:
                return True
            return not (last.date() == now.date() and last.strftime("%H:%M") == hhmm)
    return False


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
            "text": "👋 Привет! АвтоПост пишет посты для вашего Telegram-канала и помогает публиковать их по расписанию.\n\nНажмите кнопку ниже, чтобы открыть приложение.",
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

# За один тик догенерируем не больше этого числа постов на канал. Заполнение
# 3 -> 7 это до 4 генераций подряд, каждая с классификацией темы, поиском и
# самой генерацией -- пачкой в одном тике они забили бы весь цикл планировщика
# (tick общий на всех пользователей). Очередь дособерётся за несколько минут.
MAX_GEN_PER_TICK = 2


def queue_target_for_user(s, user_id: int) -> int:
    """
    Сколько готовых постов держим наготове. Платящему -- неделя вперёд, всем
    остальным -- стартовые 3.

    Признак оплаты -- любой платёж со статусом "paid" (User.plan в схеме есть,
    но не используется нигде в коде, полагаться на него нельзя). Отдельная
    система тарифов для этого не нужна и намеренно не заводится.
    """
    from sqlmodel import select as sel
    paid = s.exec(
        sel(Payment).where(Payment.user_id == user_id, Payment.status == "paid")
    ).first()
    return PAID_QUEUE if paid else MIN_QUEUE


async def _refill_if_active(channel_id: int):
    """Догенерирует посты до целевой глубины очереди если канал активен."""
    with session() as s:
        channel = s.get(Channel, channel_id)
        if not channel or not channel.enabled:
            return
        from sqlmodel import select as sel
        pending_count = len(s.exec(
            sel(Post).where(
                Post.channel_id == channel_id,
                Post.status.in_(["pending", "scheduled"])
            )
        ).all())
        target = queue_target_for_user(s, channel.user_id)

    if pending_count < target:
        for _ in range(min(target - pending_count, MAX_GEN_PER_TICK)):
            try:
                await generate_for_channel(channel_id, force_pending=True)
            except Exception as e:
                logger.warning(f"auto-refill gen: {e}")
                break


async def _ensure_queue(published_post_id: int):
    """После публикации проверяет очередь и догенерирует если меньше MIN_QUEUE."""
    with session() as s:
        post = s.get(Post, published_post_id)
        if not post:
            return
        channel_id = post.channel_id
    await _refill_if_active(channel_id)


async def tick():
    now = datetime.utcnow()

    # Polling Telegram bot updates для привязки chat_id (publishing bot)
    try:
        await _process_bot_updates()
    except Exception as e:
        logger.warning(f"bot polling: {e}")

    with session() as s:
        channels = s.exec(select(Channel).where(Channel.enabled == True)).all()  # noqa
        due_ids = [c.id for c in channels if c.verified and _is_due(c, now)]

    for cid in due_ids:
        try:
            await generate_for_channel(cid)
        except Exception as e:
            logger.error(f"tick: канал {cid}: {e}")

    # Держим минимум 3 поста в очереди для ВСЕХ верифицированных каналов
    with session() as s:
        all_verified = s.exec(select(Channel).where(
            Channel.enabled == True,   # noqa
            Channel.verified == True,  # noqa
        )).all()
        all_verified_ids = [c.id for c in all_verified]

    for cid in all_verified_ids:
        try:
            await _refill_if_active(cid)
        except Exception as e:
            logger.warning(f"queue-refill канал {cid}: {e}")

    with session() as s:
        from database import due_scheduled_posts
        due_posts = [p.id for p in due_scheduled_posts(s, now)]

    for pid in due_posts:
        try:
            await publish_post(pid)
        except Exception as e:
            logger.error(f"tick: пост {pid}: {e}")

    # "Публикация после подтверждения": дедлайн истёк без реакции — публикуем сами
    with session() as s:
        from database import due_post_approvals
        due_approvals = [(a.id, a.post_id, a.review_chat_id, a.review_message_id) for a in due_post_approvals(s, now)]

    for approval_id, post_id, review_chat_id, review_message_id in due_approvals:
        try:
            await _auto_publish_after_timeout(approval_id, post_id, review_chat_id, review_message_id)
        except Exception as e:
            logger.error(f"tick: approval-timeout пост {post_id}: {e}")
