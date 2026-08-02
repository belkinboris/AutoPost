"""
Смена режима публикации канала (auto_publish) не должна оставлять
"осиротевшие" посты в поведении прежнего режима (решение владельца 02.08,
найдено на живом канале): "либо автоматическая — и все посты публикуются
без подтверждения, либо нет — и для каждого поста нужно решение". См.
tasks.sync_posts_to_channel_mode.
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
                        tg_chat=f"@{email.split('@')[0]}", verified=True, enabled=True)
        defaults.update(kwargs)
        ch = Channel(**defaults)
        s.add(ch); s.commit(); s.refresh(ch)
        return u.id, ch.id


async def test_switching_to_autopilot_promotes_orphaned_pending_post():
    """Онбординг-черновик (pending, без scheduled_at) не должен навсегда остаться «Ждёт вашего решения»."""
    uid, cid = _make_channel("switch_auto_orphan@t.local", auto_publish=True)
    with database.session() as s:
        p = Post(channel_id=cid, user_id=uid, text="черновик", status="pending", scheduled_at=None)
        s.add(p); s.commit(); s.refresh(p)
        pid = p.id

    await tasks.sync_posts_to_channel_mode(cid)

    with database.session() as s:
        post = s.get(Post, pid)
    assert post.status == "scheduled"
    assert post.scheduled_at is not None
    assert post.scheduled_at > datetime.utcnow() - timedelta(seconds=5)


async def test_switching_to_autopilot_chains_multiple_orphans_properly():
    """Несколько осиротевших черновиков не должны все встать на «сейчас» -- каждый на своё место."""
    uid, cid = _make_channel("switch_auto_multi@t.local", auto_publish=True,
                              schedule_kind="interval", interval_hours=6)
    with database.session() as s:
        p1 = Post(channel_id=cid, user_id=uid, text="черновик 1", status="pending", scheduled_at=None)
        p2 = Post(channel_id=cid, user_id=uid, text="черновик 2", status="pending", scheduled_at=None)
        s.add(p1); s.add(p2); s.commit(); s.refresh(p1); s.refresh(p2)
        pid1, pid2 = p1.id, p2.id

    await tasks.sync_posts_to_channel_mode(cid)

    with database.session() as s:
        post1 = s.get(Post, pid1)
        post2 = s.get(Post, pid2)
    assert post1.scheduled_at is not None and post2.scheduled_at is not None
    diff_hours = abs((post2.scheduled_at - post1.scheduled_at).total_seconds()) / 3600
    assert diff_hours >= 5.9, "второй черновик должен встать через полный интервал после первого, а не рядом с ним"


async def test_switching_to_autopilot_does_not_touch_pending_with_scheduled_at():
    """editable-пост со scheduled_at не существует по дизайну, но защитная проверка не помешает: pending без scheduled_at -- единственная цель."""
    uid, cid = _make_channel("switch_auto_untouched@t.local", auto_publish=True)
    with database.session() as s:
        p = Post(channel_id=cid, user_id=uid, text="уже решено", status="rejected")
        s.add(p); s.commit(); s.refresh(p)
        pid = p.id

    await tasks.sync_posts_to_channel_mode(cid)

    with database.session() as s:
        post = s.get(Post, pid)
    assert post.status == "rejected", "отклонённый пост трогать не нужно"


async def test_switching_to_confirm_mode_adds_approval_to_orphaned_scheduled_post(monkeypatch):
    """Пост, стоявший в очереди при автопилоте (без подтверждения), должен получить обычный цикл при переходе на подтверждение."""
    calls = []

    async def _render(chat_id, message_id, post_id, title, text, deadline, edited=False):
        calls.append({"post_id": post_id, "deadline": deadline})
        return {"ok": True, "result": {"message_id": 555}}
    monkeypatch.setattr(tasks, "_render_approval_card", _render)

    uid, cid = _make_channel("switch_confirm_orphan@t.local", auto_publish=False)
    with database.session() as s:
        u = s.get(User, uid); u.tg_chat_id = 111; s.add(u)
        slot = datetime.utcnow() + timedelta(hours=6)
        p = Post(channel_id=cid, user_id=uid, text="пост автопилота", status="scheduled", scheduled_at=slot)
        s.add(p); s.commit(); s.refresh(p)
        pid = p.id

    await tasks.sync_posts_to_channel_mode(cid)

    assert len(calls) == 1
    assert calls[0]["deadline"] == slot, "время публикации не должно меняться -- только добавляется подтверждение"
    with database.session() as s:
        appr = s.exec(database.select(PostApproval).where(PostApproval.post_id == pid)).first()
    assert appr is not None
    assert appr.status == "waiting"
    assert appr.deadline == slot


async def test_switching_to_confirm_mode_does_not_duplicate_existing_approval(monkeypatch):
    calls = []

    async def _render(chat_id, message_id, post_id, title, text, deadline, edited=False):
        calls.append(post_id)
        return {"ok": True, "result": {"message_id": 555}}
    monkeypatch.setattr(tasks, "_render_approval_card", _render)

    uid, cid = _make_channel("switch_confirm_existing@t.local", auto_publish=False)
    with database.session() as s:
        u = s.get(User, uid); u.tg_chat_id = 222; s.add(u)
        slot = datetime.utcnow() + timedelta(hours=3)
        p = Post(channel_id=cid, user_id=uid, text="пост", status="scheduled", scheduled_at=slot)
        s.add(p); s.commit(); s.refresh(p)
        s.add(PostApproval(post_id=p.id, channel_id=cid, review_chat_id=222, review_message_id=1,
                            deadline=slot, status="waiting"))
        s.commit()

    await tasks.sync_posts_to_channel_mode(cid)

    assert calls == [], "у поста уже есть активное подтверждение -- трогать его не нужно"


# ── HTTP-уровень ─────────────────────────────────────────────────────────

async def test_patch_channel_triggers_mode_sync(client, token, monkeypatch):
    calls = []

    async def _fake_sync(channel_id):
        calls.append(channel_id)

    monkeypatch.setattr(tasks, "sync_posts_to_channel_mode", _fake_sync)

    r = await client.post("/api/channels", json={
        "title": "Канал", "about": "тема", "tg_chat": "@modesync_http", "auto_publish": False,
    }, headers={"Authorization": f"Bearer {token}"})
    cid = r.json()["id"]

    r = await client.patch(f"/api/channels/{cid}", json={"auto_publish": True},
                            headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200, r.text
    assert calls == [cid]


async def test_patch_channel_skips_sync_when_auto_publish_not_changed(client, token, monkeypatch):
    calls = []

    async def _fake_sync(channel_id):
        calls.append(channel_id)

    monkeypatch.setattr(tasks, "sync_posts_to_channel_mode", _fake_sync)

    r = await client.post("/api/channels", json={
        "title": "Канал", "about": "тема", "tg_chat": "@modesync_http2", "auto_publish": False,
    }, headers={"Authorization": f"Bearer {token}"})
    cid = r.json()["id"]

    r = await client.patch(f"/api/channels/{cid}", json={"title": "Новое имя"},
                            headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200, r.text
    assert calls == [], "без смены auto_publish синхронизация не нужна"
