"""
Единая модель очереди (C14, решение владельца 01-02.08): каждый пост -- что
автопилота, что режима подтверждения -- получает scheduled_at и публикуется
через один и тот же путь (due_scheduled_posts/tick), а не в момент генерации.
Разница между режимами только в том, нужно ли явное подтверждение до этого
времени; если время настало, а подтверждения нет -- пост уходит в конец
очереди (tasks._requeue_unconfirmed_post), а не публикуется молча.

Тесты здесь закрывают куски, которых раньше не было архитектурно и которые
ни один существующий файл не проверял: математику слотов очереди
(_next_slot_after/_next_queue_slot), фильтрацию "занятых подтверждением"
постов в due_scheduled_posts, окно предупреждения (approvals_needing_warning)
и сам перенос в конец очереди без публикации.
"""

from datetime import datetime, timedelta

import pytest
from sqlmodel import select

import config
import database
import tasks
from database import (
    Channel, Post, PostApproval, User,
    approvals_needing_warning, due_scheduled_posts,
)


def _make_channel(email: str, **kwargs) -> tuple[int, int]:
    with database.session() as s:
        u = User(email=email, password_hash="x", token_balance=100_000)
        s.add(u); s.commit(); s.refresh(u)
        defaults = dict(
            user_id=u.id, title="Канал", about="тема", tg_chat=f"@{email.split('@')[0]}",
            verified=True, enabled=True, auto_publish=False,
        )
        defaults.update(kwargs)
        ch = Channel(**defaults)
        s.add(ch); s.commit(); s.refresh(ch)
        return u.id, ch.id


# ── _next_slot_after: чистая математика расписания ─────────────────────────

def test_next_slot_after_interval_no_window():
    with database.session() as s:
        ch = Channel(user_id=1, title="c", about="a", schedule_kind="interval", interval_hours=6)
        anchor = datetime(2026, 1, 1, 10, 0, 0)
        got = tasks._next_slot_after(ch, anchor)
    assert got == anchor + timedelta(hours=6)


def test_next_slot_after_interval_before_window_snaps_forward():
    ch = Channel(user_id=1, title="c", about="a", schedule_kind="interval", interval_hours=1,
                 publish_window_start="09:00", publish_window_end="22:00")
    anchor = datetime(2026, 1, 1, 3, 0, 0)  # +1ч = 04:00, до окна
    got = tasks._next_slot_after(ch, anchor)
    assert got == datetime(2026, 1, 1, 9, 0, 0)


def test_next_slot_after_interval_after_window_rolls_to_next_day():
    ch = Channel(user_id=1, title="c", about="a", schedule_kind="interval", interval_hours=1,
                 publish_window_start="09:00", publish_window_end="22:00")
    anchor = datetime(2026, 1, 1, 21, 30, 0)  # +1ч = 22:30, после окна
    got = tasks._next_slot_after(ch, anchor)
    assert got == datetime(2026, 1, 2, 9, 0, 0)


def test_next_slot_after_daily_picks_next_time_same_day():
    ch = Channel(user_id=1, title="c", about="a", schedule_kind="daily",
                 daily_times='["09:00", "18:00"]')
    anchor = datetime(2026, 1, 1, 10, 0, 0)
    got = tasks._next_slot_after(ch, anchor)
    assert got == datetime(2026, 1, 1, 18, 0, 0)


def test_next_slot_after_daily_rolls_to_next_day_when_past_last_time():
    ch = Channel(user_id=1, title="c", about="a", schedule_kind="daily",
                 daily_times='["09:00", "18:00"]')
    anchor = datetime(2026, 1, 1, 19, 0, 0)
    got = tasks._next_slot_after(ch, anchor)
    assert got == datetime(2026, 1, 2, 9, 0, 0)


# ── _next_queue_slot: где встаёт НОВЫЙ пост ─────────────────────────────────

async def test_next_queue_slot_empty_queue_autopilot_is_now():
    uid, cid = _make_channel("slot_auto_empty@t.local", auto_publish=True)
    with database.session() as s:
        ch = s.get(Channel, cid)
        got = tasks._next_queue_slot(s, ch)
    assert abs((got - datetime.utcnow()).total_seconds()) < 5


async def test_next_queue_slot_empty_queue_confirm_mode_has_minimum_delay():
    uid, cid = _make_channel("slot_confirm_empty@t.local", auto_publish=False)
    with database.session() as s:
        ch = s.get(Channel, cid)
        got = tasks._next_queue_slot(s, ch)
    expected = datetime.utcnow() + timedelta(minutes=config.SOFT_CONTROL_APPROVAL_MINUTES)
    assert abs((got - expected).total_seconds()) < 5


async def test_next_queue_slot_steps_after_last_queued_post():
    uid, cid = _make_channel("slot_after_last@t.local", auto_publish=True,
                              schedule_kind="interval", interval_hours=2)
    last_slot = datetime.utcnow() + timedelta(hours=5)
    with database.session() as s:
        s.add(Post(channel_id=cid, user_id=uid, text="уже в очереди", status="scheduled",
                    scheduled_at=last_slot))
        s.commit()
        ch = s.get(Channel, cid)
        got = tasks._next_queue_slot(s, ch)
    assert got == last_slot + timedelta(hours=2)


async def test_next_queue_slot_chains_from_overdue_last_slot():
    """
    Прод-инцидент 02.08 (найден владельцем в день выкладки): раньше просроченный
    последний слот считался "как будто очереди нет" -- новый пост планировался
    от "сейчас" вместо продолжения интервала. Для автопилота с
    MAX_GEN_PER_TICK=1 это происходило КАЖДЫЙ раз при публикации (пост
    публикуется ровно когда его scheduled_at уже <= now, то есть уже
    "просрочен" в момент, когда _refill_queue готовит следующий) -- интервал
    канала (например, раз в сутки) схлопывался до частоты тика (обычно 60с),
    токены сгорали впустую на посты каждую минуту. Правильно: пока в очереди
    есть хоть один "scheduled" пост -- новый планируется от него
    (_next_slot_after), даже если его время уже наступило или чуть прошло.
    "Пусто" -- это ноль постов со статусом scheduled, а не "время последнего прошло".
    """
    uid, cid = _make_channel("slot_past_last@t.local", auto_publish=True,
                              schedule_kind="interval", interval_hours=24)
    overdue_at = datetime.utcnow() - timedelta(seconds=30)
    with database.session() as s:
        s.add(Post(channel_id=cid, user_id=uid, text="просрочен", status="scheduled",
                    scheduled_at=overdue_at))
        s.commit()
        ch = s.get(Channel, cid)
        got = tasks._next_queue_slot(s, ch)
    assert got == overdue_at + timedelta(hours=24), (
        "новый пост должен продолжить интервал от просроченного слота, а не спланироваться на «сейчас»"
    )
    # Действительно пустая очередь (ни одного scheduled поста вообще) --
    # это другое дело и по-прежнему "сейчас" для автопилота, см.
    # test_next_queue_slot_empty_queue_autopilot_is_now выше.


# ── due_scheduled_posts: подтверждение исключает пост из общей публикации ──

async def test_due_scheduled_posts_includes_autopilot_without_approval():
    """Обычный случай: автопилот, подтверждение не заводится вовсе -- публикуется по тику."""
    uid, cid = _make_channel("due_auto@t.local", auto_publish=True)
    past = datetime.utcnow() - timedelta(minutes=1)
    with database.session() as s:
        p = Post(channel_id=cid, user_id=uid, text="автопилот", status="scheduled", scheduled_at=past)
        s.add(p); s.commit(); s.refresh(p)
        pid = p.id
        due_ids = {row.id for row in due_scheduled_posts(s, datetime.utcnow())}
    assert pid in due_ids


async def test_due_scheduled_posts_excludes_disabled_channel():
    """
    Прод-инцидент 02.08: без фильтра по Channel.enabled пауза канала не
    останавливала уже запланированные посты -- останавливала только
    _refill_queue (новую генерацию). Интерфейс прямо обещает "не
    публикуется, пока канал на паузе" (app.part11.js) -- это должно быть
    правдой и для постов, вставших в очередь ДО постановки на паузу.
    """
    uid, cid = _make_channel("due_disabled@t.local", auto_publish=True, enabled=False)
    past = datetime.utcnow() - timedelta(minutes=1)
    with database.session() as s:
        p = Post(channel_id=cid, user_id=uid, text="на паузе", status="scheduled", scheduled_at=past)
        s.add(p); s.commit(); s.refresh(p)
        pid = p.id
        due_ids = {row.id for row in due_scheduled_posts(s, datetime.utcnow())}
    assert pid not in due_ids, "канал на паузе не должен публиковать даже уже запланированные посты"


async def test_due_scheduled_posts_excludes_confirm_mode_even_without_approval():
    """
    Критично: режим подтверждения без карточки в Telegram (нет tg_chat_id,
    бот заблокирован) вообще не заводит строку PostApproval. Раньше
    исключение держалось только на её наличии -- такой пост молча
    опубликовался бы сам по достижении scheduled_at, хотя ни кнопки, ни
    подтверждения не было. Режим подтверждения публикует только по явному
    "Опубликовать" (см. /api/posts/{id}/publish), через тик -- никогда.
    """
    uid, cid = _make_channel("due_confirm_no_card@t.local", auto_publish=False)
    past = datetime.utcnow() - timedelta(minutes=1)
    with database.session() as s:
        p = Post(channel_id=cid, user_id=uid, text="без карточки", status="scheduled", scheduled_at=past)
        s.add(p); s.commit(); s.refresh(p)
        pid = p.id
        due_ids = {row.id for row in due_scheduled_posts(s, datetime.utcnow())}
    assert pid not in due_ids, "пост режима подтверждения не должен публиковаться по тику никогда"


async def test_due_scheduled_posts_excludes_confirm_mode_with_resolved_approval():
    """Approval со статусом done тоже не делает пост режима подтверждения кандидатом на публикацию по тику."""
    uid, cid = _make_channel("due_resolved@t.local", auto_publish=False)
    past = datetime.utcnow() - timedelta(minutes=1)
    with database.session() as s:
        p = Post(channel_id=cid, user_id=uid, text="решено", status="scheduled", scheduled_at=past)
        s.add(p); s.commit(); s.refresh(p)
        pid = p.id
        s.add(PostApproval(post_id=p.id, channel_id=cid, review_chat_id=1,
                            deadline=past, status="done"))
        s.commit()

        due_ids = {row.id for row in due_scheduled_posts(s, datetime.utcnow())}
    assert pid not in due_ids


async def test_due_scheduled_posts_excludes_autopilot_with_leftover_waiting_approval():
    """
    Защита узкого случая: канал переключили на автопилот, пока у уже
    стоящего в очереди поста ещё висело неразрешённое подтверждение с
    прошлого режима -- публиковать его по тику молча всё равно нельзя.
    """
    uid, cid = _make_channel("due_switched@t.local", auto_publish=True)
    past = datetime.utcnow() - timedelta(minutes=1)
    with database.session() as s:
        p = Post(channel_id=cid, user_id=uid, text="висит с прошлого режима",
                  status="scheduled", scheduled_at=past)
        s.add(p); s.commit(); s.refresh(p)
        pid = p.id
        s.add(PostApproval(post_id=p.id, channel_id=cid, review_chat_id=1,
                            deadline=past, status="waiting"))
        s.commit()

        due_ids = {row.id for row in due_scheduled_posts(s, datetime.utcnow())}
    assert pid not in due_ids


# ── approvals_needing_warning: окно предупреждения ──────────────────────────

async def test_approvals_needing_warning_within_lead_window():
    uid, cid = _make_channel("warn_in@t.local", auto_publish=False)
    with database.session() as s:
        p = Post(channel_id=cid, user_id=uid, text="скоро дедлайн", status="scheduled",
                  scheduled_at=datetime.utcnow() + timedelta(minutes=5))
        s.add(p); s.commit(); s.refresh(p)
        appr = PostApproval(post_id=p.id, channel_id=cid, review_chat_id=1,
                             deadline=datetime.utcnow() + timedelta(minutes=5), status="waiting")
        s.add(appr); s.commit(); s.refresh(appr)

        got = {a.id for a in approvals_needing_warning(s, datetime.utcnow(), 10)}
    assert appr.id in got


async def test_approvals_needing_warning_excludes_already_warned():
    uid, cid = _make_channel("warn_dup@t.local", auto_publish=False)
    with database.session() as s:
        p = Post(channel_id=cid, user_id=uid, text="уже предупреждён", status="scheduled",
                  scheduled_at=datetime.utcnow() + timedelta(minutes=5))
        s.add(p); s.commit(); s.refresh(p)
        appr = PostApproval(post_id=p.id, channel_id=cid, review_chat_id=1,
                             deadline=datetime.utcnow() + timedelta(minutes=5), status="waiting",
                             final_warning_sent=True)
        s.add(appr); s.commit(); s.refresh(appr)

        got = {a.id for a in approvals_needing_warning(s, datetime.utcnow(), 10)}
    assert appr.id not in got


async def test_approvals_needing_warning_excludes_far_deadline():
    uid, cid = _make_channel("warn_far@t.local", auto_publish=False)
    with database.session() as s:
        p = Post(channel_id=cid, user_id=uid, text="ещё далеко", status="scheduled",
                  scheduled_at=datetime.utcnow() + timedelta(hours=2))
        s.add(p); s.commit(); s.refresh(p)
        appr = PostApproval(post_id=p.id, channel_id=cid, review_chat_id=1,
                             deadline=datetime.utcnow() + timedelta(hours=2), status="waiting")
        s.add(appr); s.commit(); s.refresh(appr)

        got = {a.id for a in approvals_needing_warning(s, datetime.utcnow(), 10)}
    assert appr.id not in got


# ── _requeue_unconfirmed_post: перенос в конец, а не публикация ─────────────

@pytest.fixture
def telegram_stub(monkeypatch):
    """Карточка в Telegram всегда «доставляется» успешно -- проверяем логику переноса, не саму отправку."""
    async def _render(chat_id, message_id, post_id, title, text, deadline, edited=False):
        return {"ok": True, "result": {"message_id": 777}}

    async def _edit(chat_id, message_id, text, keyboard=None):
        return {"ok": True}

    monkeypatch.setattr(tasks, "_render_approval_card", _render)
    monkeypatch.setattr(tasks.telegram_api, "edit_message_text", _edit)


async def test_requeue_moves_post_to_back_without_publishing(telegram_stub):
    uid, cid = _make_channel("requeue@t.local", auto_publish=False)
    old_slot = datetime.utcnow() - timedelta(minutes=1)  # дедлайн истёк
    with database.session() as s:
        ch = s.get(Channel, cid)
        u = s.get(User, uid)
        u.tg_chat_id = 4242
        s.add(u)
        p = Post(channel_id=cid, user_id=uid, text="не подтверждён", status="scheduled",
                  scheduled_at=old_slot)
        s.add(p); s.commit(); s.refresh(p)
        appr = PostApproval(post_id=p.id, channel_id=cid, review_chat_id=4242,
                             review_message_id=1, deadline=old_slot, status="waiting")
        s.add(appr); s.commit(); s.refresh(appr)
        appr_id, post_id = appr.id, p.id

    await tasks._requeue_unconfirmed_post(appr_id, post_id, 4242, 1)

    with database.session() as s:
        post = s.get(Post, post_id)
        appr = s.get(PostApproval, appr_id)
        all_approvals_for_post = s.exec(
            select(PostApproval).where(PostApproval.post_id == post_id)
        ).all()

    assert post.status == "scheduled", "пост не должен публиковаться молча по истечении дедлайна"
    assert post.scheduled_at > old_slot, "перенесённый пост должен получить новое, более позднее время"
    assert post.requeued_at is not None, "должна остаться отметка «не подтвердили вовремя»"
    # PostApproval.post_id уникален -- новый цикл ОБНОВЛЯЕТ ту же строку, а не
    # заводит вторую (второй insert падал на UNIQUE constraint).
    assert len(all_approvals_for_post) == 1
    assert appr.status == "waiting", "карточка нового цикла доставлена -- таймер должен продолжаться"
    assert appr.deadline == post.scheduled_at
    assert appr.final_warning_sent is False, "новый цикл -- предупреждение ещё не отправлялось"


async def test_requeue_is_noop_if_already_resolved(telegram_stub):
    """Между выборкой в tick() и вызовом решение могли принять кнопкой -- повторный перенос не нужен."""
    uid, cid = _make_channel("requeue_resolved@t.local", auto_publish=False)
    with database.session() as s:
        p = Post(channel_id=cid, user_id=uid, text="уже решено", status="published",
                  scheduled_at=datetime.utcnow() - timedelta(minutes=1))
        s.add(p); s.commit(); s.refresh(p)
        appr = PostApproval(post_id=p.id, channel_id=cid, review_chat_id=1,
                             deadline=datetime.utcnow() - timedelta(minutes=1), status="waiting")
        s.add(appr); s.commit(); s.refresh(appr)
        appr_id, post_id = appr.id, p.id

    await tasks._requeue_unconfirmed_post(appr_id, post_id, 1, None)

    with database.session() as s:
        post = s.get(Post, post_id)
        appr = s.get(PostApproval, appr_id)
    assert post.status == "published", "уже опубликованный пост трогать нельзя"
    assert appr.status == "done"


# ── _send_approval_warning: предупреждение за N минут, один раз ────────────

async def test_send_approval_warning_sets_flag_and_pings_when_enabled(monkeypatch):
    uid, cid = _make_channel("warn_ping@t.local", auto_publish=False)
    edits = []
    pings = []

    async def _edit(chat_id, message_id, text, keyboard=None):
        edits.append((chat_id, message_id, text))
        return {"ok": True}

    async def _notify(chat_id, text):
        pings.append((chat_id, text))
        return True, None

    monkeypatch.setattr(tasks.telegram_api, "edit_message_text", _edit)
    monkeypatch.setattr(tasks.telegram_api, "send_notification", _notify)

    with database.session() as s:
        u = s.get(User, uid)
        u.notify_approval_pending = True
        u.tg_chat_id = 555
        s.add(u)
        p = Post(channel_id=cid, user_id=uid, text="предупредить", status="scheduled",
                  scheduled_at=datetime.utcnow() + timedelta(minutes=10))
        s.add(p); s.commit(); s.refresh(p)
        appr = PostApproval(post_id=p.id, channel_id=cid, review_chat_id=555, review_message_id=9,
                             deadline=datetime.utcnow() + timedelta(minutes=10), status="waiting")
        s.add(appr); s.commit(); s.refresh(appr)
        appr_id, post_id = appr.id, p.id

    await tasks._send_approval_warning(appr_id, post_id, 555, 9)

    with database.session() as s:
        appr = s.get(PostApproval, appr_id)
    assert appr.final_warning_sent is True
    assert len(edits) == 1, "карточка в Telegram должна обновиться"
    assert len(pings) == 1, "включённый тумблер notify_approval_pending должен дать отдельный пинг"


async def test_send_approval_warning_no_ping_when_toggle_disabled(monkeypatch):
    uid, cid = _make_channel("warn_noping@t.local", auto_publish=False)
    pings = []

    async def _edit(chat_id, message_id, text, keyboard=None):
        return {"ok": True}

    async def _notify(chat_id, text):
        pings.append((chat_id, text))
        return True, None

    monkeypatch.setattr(tasks.telegram_api, "edit_message_text", _edit)
    monkeypatch.setattr(tasks.telegram_api, "send_notification", _notify)

    with database.session() as s:
        u = s.get(User, uid)
        u.notify_approval_pending = False
        u.tg_chat_id = 556
        s.add(u)
        p = Post(channel_id=cid, user_id=uid, text="без пинга", status="scheduled",
                  scheduled_at=datetime.utcnow() + timedelta(minutes=10))
        s.add(p); s.commit(); s.refresh(p)
        appr = PostApproval(post_id=p.id, channel_id=cid, review_chat_id=556, review_message_id=9,
                             deadline=datetime.utcnow() + timedelta(minutes=10), status="waiting")
        s.add(appr); s.commit(); s.refresh(appr)
        appr_id, post_id = appr.id, p.id

    await tasks._send_approval_warning(appr_id, post_id, 556, 9)
    assert pings == [], "тумблер выключен -- дополнительного сообщения быть не должно"


async def test_send_approval_warning_is_idempotent(monkeypatch):
    uid, cid = _make_channel("warn_once@t.local", auto_publish=False)
    edits = []

    async def _edit(chat_id, message_id, text, keyboard=None):
        edits.append(1)
        return {"ok": True}

    monkeypatch.setattr(tasks.telegram_api, "edit_message_text", _edit)

    with database.session() as s:
        p = Post(channel_id=cid, user_id=uid, text="дважды не предупреждать", status="scheduled",
                  scheduled_at=datetime.utcnow() + timedelta(minutes=10))
        s.add(p); s.commit(); s.refresh(p)
        appr = PostApproval(post_id=p.id, channel_id=cid, review_chat_id=1, review_message_id=1,
                             deadline=datetime.utcnow() + timedelta(minutes=10), status="waiting")
        s.add(appr); s.commit(); s.refresh(appr)
        appr_id, post_id = appr.id, p.id

    await tasks._send_approval_warning(appr_id, post_id, 1, 1)
    await tasks._send_approval_warning(appr_id, post_id, 1, 1)
    assert len(edits) == 1, "повторный вызов после final_warning_sent не должен слать ещё раз"
