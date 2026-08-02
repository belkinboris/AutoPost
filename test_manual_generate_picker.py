"""
Пикер даты/времени для "Написать пост сейчас" (C14, пункт 4 из видения
владельца 01.08): вместо стандартного следующего слота можно выбрать своё
время, и пост встаёт в очередь именно на него -- пересортировка отдельным
кодом не нужна (C14: позиция в очереди -- это и есть scheduled_at, см.
test_unified_queue.py).

Тесты здесь проверяют оба уровня: сам generate_for_channel (без обращения к
модели -- generator замокан, как в test_duplicate_posts.py) и HTTP-валидацию
на границе (main.py) -- прошлое время и кривой формат отклоняются ДО того,
как потрачены токены на генерацию.
"""

from datetime import datetime, timedelta

import pytest

import database
import generator
import tasks
from database import Channel, PostApproval, User


def _stub_generator(monkeypatch, text="Тестовый пост"):
    async def _classify(_topic):
        return "valid"

    async def _generate(channel, material, topic, rules_text, recent_titles):
        return text, 100

    monkeypatch.setattr(generator, "classify_topic", _classify)
    monkeypatch.setattr(generator, "generate_post", _generate)


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


async def test_generate_for_channel_uses_target_scheduled_at(monkeypatch):
    _stub_generator(monkeypatch)
    uid, cid = _make_channel("picker_slot@t.local", auto_publish=True)
    target = datetime.utcnow() + timedelta(days=2, hours=3)

    result = await tasks.generate_for_channel(cid, target_scheduled_at=target)
    assert result["ok"], result

    with database.session() as s:
        from database import Post
        post = s.get(Post, result["post_id"])
        assert post.scheduled_at == target
        assert post.status == "scheduled"


async def test_generate_for_channel_target_time_sets_approval_deadline(monkeypatch):
    """В режиме подтверждения дедлайн подтверждения -- то же самое выбранное время (единая модель очереди)."""
    _stub_generator(monkeypatch)
    uid, cid = _make_channel("picker_confirm@t.local", auto_publish=False)
    with database.session() as s:
        u = s.get(User, uid)
        u.tg_chat_id = 777
        s.add(u); s.commit()

    async def _render(chat_id, message_id, post_id, title, text, deadline, edited=False):
        return {"ok": True, "result": {"message_id": 555}}
    monkeypatch.setattr(tasks, "_render_approval_card", _render)

    target = datetime.utcnow() + timedelta(hours=5)
    result = await tasks.generate_for_channel(cid, target_scheduled_at=target)
    assert result["ok"], result

    with database.session() as s:
        appr = s.exec(
            database.select(PostApproval).where(PostApproval.post_id == result["post_id"])
        ).first()
        assert appr is not None
        assert appr.deadline == target


async def test_generate_for_channel_without_target_uses_default_slot(monkeypatch):
    """Без явного времени поведение не меняется -- обычный _next_queue_slot."""
    _stub_generator(monkeypatch)
    uid, cid = _make_channel("picker_default@t.local", auto_publish=True)

    result = await tasks.generate_for_channel(cid)
    assert result["ok"], result
    with database.session() as s:
        from database import Post
        post = s.get(Post, result["post_id"])
        assert abs((post.scheduled_at - datetime.utcnow()).total_seconds()) < 5


# ── HTTP-уровень: валидация на границе (main.py) ────────────────────────────

async def _make_channel_http(client, token, auto_publish=True):
    r = await client.post("/api/channels", json={
        "title": "Канал", "about": "тема", "tg_chat": "@picker_http", "auto_publish": auto_publish,
    }, headers={"Authorization": f"Bearer {token}"})
    r.raise_for_status()
    return r.json()["id"]


@pytest.fixture
def fake_generate(monkeypatch):
    calls = []

    async def _fake(channel_id, topic="", force_pending=False, target_scheduled_at=None):
        calls.append(target_scheduled_at)
        return {"ok": True, "post_id": None}

    monkeypatch.setattr(tasks, "generate_for_channel", _fake)
    return calls


async def test_generate_endpoint_passes_future_time_through(client, token, fake_generate):
    cid = await _make_channel_http(client, token)
    future = (datetime.utcnow() + timedelta(days=1)).isoformat()
    r = await client.post(f"/api/channels/{cid}/generate", json={"scheduled_at": future},
                          headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200, r.text
    assert len(fake_generate) == 1
    assert fake_generate[0] is not None


async def test_generate_endpoint_rejects_past_time(client, token, fake_generate):
    cid = await _make_channel_http(client, token)
    past = (datetime.utcnow() - timedelta(hours=1)).isoformat()
    r = await client.post(f"/api/channels/{cid}/generate", json={"scheduled_at": past},
                          headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 400
    assert fake_generate == [], "генерация не должна была запуститься для прошедшего времени"


async def test_generate_endpoint_rejects_malformed_time(client, token, fake_generate):
    cid = await _make_channel_http(client, token)
    r = await client.post(f"/api/channels/{cid}/generate", json={"scheduled_at": "не дата"},
                          headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 400
    assert fake_generate == []


async def test_generate_endpoint_without_scheduled_at_still_works(client, token, fake_generate):
    cid = await _make_channel_http(client, token)
    r = await client.post(f"/api/channels/{cid}/generate", json={},
                          headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200, r.text
    assert fake_generate == [None]
