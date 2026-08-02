"""
Перенос времени поста ("Запланировать"/"Перенести", POST /api/posts/{id}/schedule)
должен сдвигать уже идущее ожидание решения вместе с постом, а не спрашивать
заново (решение владельца 02.08): пост, который ещё можно перенести, по
определению ещё не подтверждён -- подтверждение в этой модели это и есть
публикация. См. tasks._sync_approval_to_reschedule.
"""

from datetime import datetime, timedelta

import database
import tasks
from database import Channel, Post, PostApproval, User


def _make_channel(email: str, **kwargs) -> tuple[int, int]:
    with database.session() as s:
        u = User(email=email, password_hash="x", token_balance=100_000)
        s.add(u); s.commit(); s.refresh(u)
        defaults = dict(user_id=u.id, title="Канал", about="тема",
                        tg_chat=f"@{email.split('@')[0]}", verified=True, enabled=True,
                        auto_publish=False)
        defaults.update(kwargs)
        ch = Channel(**defaults)
        s.add(ch); s.commit(); s.refresh(ch)
        return u.id, ch.id


def _stub_render(monkeypatch, ok=True, message_id=999):
    calls = []

    async def _render(chat_id, message_id_arg, post_id, title, text, deadline, edited=False):
        calls.append({"chat_id": chat_id, "message_id": message_id_arg, "deadline": deadline})
        if ok:
            return {"ok": True, "result": {"message_id": message_id}}
        return {"ok": False, "description": "bot was blocked by the user"}

    monkeypatch.setattr(tasks, "_render_approval_card", _render)
    return calls


async def test_reschedule_updates_existing_waiting_approval(monkeypatch):
    calls = _stub_render(monkeypatch)
    uid, cid = _make_channel("resched_existing@t.local")
    with database.session() as s:
        u = s.get(User, uid); u.tg_chat_id = 111; s.add(u)
        old_deadline = datetime.utcnow() + timedelta(minutes=10)
        p = Post(channel_id=cid, user_id=uid, text="пост", status="scheduled", scheduled_at=old_deadline)
        s.add(p); s.commit(); s.refresh(p)
        appr = PostApproval(post_id=p.id, channel_id=cid, review_chat_id=111, review_message_id=42,
                             deadline=old_deadline, status="waiting", final_warning_sent=True)
        s.add(appr); s.commit(); s.refresh(appr)
        pid, appr_id = p.id, appr.id

    new_deadline = datetime.utcnow() + timedelta(days=3)
    await tasks._sync_approval_to_reschedule(pid, new_deadline)

    assert calls == [{"chat_id": 111, "message_id": 42, "deadline": new_deadline}], (
        "должны были ПРАВИТЬ существующую карточку (message_id=42), а не слать новую"
    )
    with database.session() as s:
        appr = s.get(PostApproval, appr_id)
        all_for_post = s.exec(database.select(PostApproval).where(PostApproval.post_id == pid)).all()
    assert len(all_for_post) == 1, "не должно появиться второй строки подтверждения"
    assert appr.deadline == new_deadline
    assert appr.status == "waiting"
    assert appr.final_warning_sent is False, "предупреждение должно сброситься для нового срока"


async def test_reschedule_creates_approval_when_none_existed(monkeypatch):
    """Например, онбординг-черновик впервые ставят на конкретное время вручную."""
    calls = _stub_render(monkeypatch, message_id=777)
    uid, cid = _make_channel("resched_new@t.local")
    with database.session() as s:
        u = s.get(User, uid); u.tg_chat_id = 222; s.add(u)
        p = Post(channel_id=cid, user_id=uid, text="черновик", status="pending", scheduled_at=None)
        s.add(p); s.commit(); s.refresh(p)
        pid = p.id

    new_deadline = datetime.utcnow() + timedelta(hours=5)
    await tasks._sync_approval_to_reschedule(pid, new_deadline)

    assert calls[0]["message_id"] is None, "новой карточки ещё не было -- должны были отправить новую, не редактировать"
    with database.session() as s:
        appr = s.exec(database.select(PostApproval).where(PostApproval.post_id == pid)).first()
    assert appr is not None
    assert appr.deadline == new_deadline
    assert appr.status == "waiting"
    assert appr.review_message_id == 777


async def test_reschedule_does_nothing_without_telegram(monkeypatch):
    calls = _stub_render(monkeypatch)
    uid, cid = _make_channel("resched_no_tg@t.local")
    with database.session() as s:
        p = Post(channel_id=cid, user_id=uid, text="пост", status="scheduled",
                  scheduled_at=datetime.utcnow() + timedelta(hours=1))
        s.add(p); s.commit(); s.refresh(p)
        pid = p.id

    await tasks._sync_approval_to_reschedule(pid, datetime.utcnow() + timedelta(days=1))

    assert calls == [], "без tg_chat_id предупреждать нечем -- карточку трогать не должны"
    with database.session() as s:
        appr = s.exec(database.select(PostApproval).where(PostApproval.post_id == pid)).first()
    assert appr is None, "таймер не заводится без способа предупредить, как и при первой генерации"


async def test_reschedule_ignored_for_autopilot_channel(monkeypatch):
    calls = _stub_render(monkeypatch)
    uid, cid = _make_channel("resched_auto@t.local", auto_publish=True)
    with database.session() as s:
        u = s.get(User, uid); u.tg_chat_id = 333; s.add(u)
        p = Post(channel_id=cid, user_id=uid, text="пост", status="scheduled",
                  scheduled_at=datetime.utcnow() + timedelta(hours=1))
        s.add(p); s.commit(); s.refresh(p)
        pid = p.id

    await tasks._sync_approval_to_reschedule(pid, datetime.utcnow() + timedelta(days=1))

    assert calls == [], "автопилот не использует подтверждение -- функция не должна ничего слать"
    with database.session() as s:
        appr = s.exec(database.select(PostApproval).where(PostApproval.post_id == pid)).first()
    assert appr is None


async def test_reschedule_does_not_update_deadline_when_delivery_fails(monkeypatch):
    calls = _stub_render(monkeypatch, ok=False)
    uid, cid = _make_channel("resched_fail@t.local")
    with database.session() as s:
        u = s.get(User, uid); u.tg_chat_id = 444; s.add(u)
        old_deadline = datetime.utcnow() + timedelta(minutes=10)
        p = Post(channel_id=cid, user_id=uid, text="пост", status="scheduled", scheduled_at=old_deadline)
        s.add(p); s.commit(); s.refresh(p)
        appr = PostApproval(post_id=p.id, channel_id=cid, review_chat_id=444, review_message_id=1,
                             deadline=old_deadline, status="waiting")
        s.add(appr); s.commit(); s.refresh(appr)
        pid, appr_id = p.id, appr.id

    await tasks._sync_approval_to_reschedule(pid, datetime.utcnow() + timedelta(days=2))

    assert len(calls) == 1
    with database.session() as s:
        appr = s.get(PostApproval, appr_id)
    assert appr.deadline == old_deadline, "доставка провалилась -- дедлайн не должен был поменяться"


# ── HTTP-уровень: POST /api/posts/{id}/schedule вызывает синхронизацию ─────

async def test_schedule_endpoint_syncs_approval(client, token, monkeypatch):
    calls = []

    async def _fake_sync(post_id, new_deadline):
        calls.append((post_id, new_deadline))

    monkeypatch.setattr(tasks, "_sync_approval_to_reschedule", _fake_sync)

    r = await client.post("/api/channels", json={
        "title": "Канал", "about": "тема", "tg_chat": "@resched_http", "auto_publish": False,
    }, headers={"Authorization": f"Bearer {token}"})
    cid = r.json()["id"]

    with database.session() as s:
        p = Post(channel_id=cid, user_id=r.json()["user_id"], text="пост", status="pending")
        s.add(p); s.commit(); s.refresh(p)
        pid = p.id

    future = (datetime.utcnow() + timedelta(days=1)).isoformat()
    r = await client.post(f"/api/posts/{pid}/schedule", json={"scheduled_at": future},
                          headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200, r.text
    assert len(calls) == 1
    assert calls[0][0] == pid
