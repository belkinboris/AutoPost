"""
POST /api/channels/{id}/generate ("Написать пост сейчас") должен вести себя
так, как обещает режим канала, а не всегда требовать подтверждения.

Найдено владельцем 31.07: на канале с включённым автопилотом кнопка
"Написать пост сейчас" создавала пост "Ждёт вашего решения... сам не
опубликуется" -- ровно то, что автопилот и обещает НЕ требовать. Причина:
main.py передавал force_pending=True безусловно, независимо от
channel.auto_publish.
"""

import pytest

import tasks


async def _make_channel(client, token, auto_publish, tg_chat):
    r = await client.post("/api/channels", json={
        "title": "Канал", "about": "тема", "tg_chat": tg_chat, "auto_publish": auto_publish,
    }, headers={"Authorization": f"Bearer {token}"})
    r.raise_for_status()
    return r.json()["id"]


@pytest.fixture
def fake_generate(monkeypatch):
    calls = []

    async def _fake(channel_id, topic="", force_pending=False):
        calls.append({"channel_id": channel_id, "force_pending": force_pending})
        return {"ok": True, "post_id": None}

    monkeypatch.setattr(tasks, "generate_for_channel", _fake)
    return calls


async def test_manual_generate_on_autopilot_does_not_force_pending(client, token, fake_generate):
    """Ключевой случай: автопилот -- решение принимать не нужно вообще."""
    cid = await _make_channel(client, token, True, "@manual_gen_auto")
    r = await client.post(f"/api/channels/{cid}/generate", json={},
                          headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200, r.text
    assert fake_generate == [{"channel_id": cid, "force_pending": False}]


async def test_manual_generate_on_manual_confirm_still_needs_decision(client, token, fake_generate):
    """Режим подтверждения не задет -- пост по-прежнему ждёт решения."""
    cid = await _make_channel(client, token, False, "@manual_gen_manual")
    r = await client.post(f"/api/channels/{cid}/generate", json={},
                          headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200, r.text
    assert fake_generate == [{"channel_id": cid, "force_pending": True}]
