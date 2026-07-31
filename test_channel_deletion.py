"""
Удаление канала -- FK-минное поле, как и delete_account() (правило 3 в
CLAUDE.md), только для одного канала, а не всего аккаунта.

Найдено владельцем 31.07: удаление канала с активным таймером подтверждения
("публикация после подтверждения") падало с "Не удалось удалить канал,
обновите страницу". Причина: PostApproval ссылается FK и на post.id, и на
channel.id, а delete_channel() чистил Source/Post/ChannelRule, но не её --
на Postgres это IntegrityError при удалении Post (PostApproval.post_id) или
Channel (PostApproval.channel_id).
"""

from datetime import datetime

import pytest
from sqlmodel import select

import database
from database import Channel, ChannelRule, Post, PostApproval, Source


async def _channel(client, token, tg_chat="@del_demo"):
    r = await client.post("/api/channels", json={
        "title": "Канал на удаление", "about": "тема", "tg_chat": tg_chat,
    }, headers={"Authorization": f"Bearer {token}"})
    r.raise_for_status()
    return r.json()["id"]


async def test_delete_channel_with_pending_approval_succeeds(client, token):
    """Ключевой случай: раньше падало именно здесь."""
    cid = await _channel(client, token, "@del_approval")
    with database.session() as s:
        ch = s.get(Channel, cid)
        post = Post(channel_id=cid, user_id=ch.user_id, text="Текст", status="pending")
        s.add(post); s.commit(); s.refresh(post)
        s.add(PostApproval(
            post_id=post.id, channel_id=cid, review_chat_id=1,
            deadline=datetime.utcnow(),
        ))
        s.commit()
        pid = post.id

    r = await client.delete(f"/api/channels/{cid}", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200, r.text

    with database.session() as s:
        assert s.get(Channel, cid) is None
        assert s.get(Post, pid) is None
        assert s.exec(select(PostApproval).where(PostApproval.channel_id == cid)).all() == []


async def test_delete_channel_removes_sources_posts_and_rules(client, token):
    cid = await _channel(client, token, "@del_basic")
    with database.session() as s:
        ch = s.get(Channel, cid)
        s.add(Source(channel_id=cid, url="https://example.com/feed"))
        s.add(ChannelRule(channel_id=cid, rule_text="Не писать про политику"))
        s.add(Post(channel_id=cid, user_id=ch.user_id, text="Текст", status="published"))
        s.commit()

    r = await client.delete(f"/api/channels/{cid}", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200, r.text

    with database.session() as s:
        assert s.get(Channel, cid) is None
        assert s.exec(select(Source).where(Source.channel_id == cid)).all() == []
        assert s.exec(select(ChannelRule).where(ChannelRule.channel_id == cid)).all() == []
        assert s.exec(select(Post).where(Post.channel_id == cid)).all() == []


async def test_cannot_delete_someone_elses_channel(client, token):
    cid = await _channel(client, token, "@del_stranger")
    r = await client.post("/api/register", json={"email": "del_stranger@test.local", "password": "test12345"})
    other = r.json()["token"]
    r = await client.delete(f"/api/channels/{cid}", headers={"Authorization": f"Bearer {other}"})
    assert r.status_code == 404
    with database.session() as s:
        assert s.get(Channel, cid) is not None
