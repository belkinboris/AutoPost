"""
POST /api/channels/{id}/generate ("Написать пост сейчас") встаёт в общую
очередь на тех же правах, что и плановая генерация по расписанию -- независимо
от режима публикации канала.

История вопроса, два неверных захода подряд:
1. Найдено владельцем 31.07: на канале с автопилотом кнопка создавала пост
   "Ждёт вашего решения... сам не опубликуется" -- endpoint передавал
   force_pending=True безусловно, не глядя на channel.auto_publish.
2. Исправлено 31.07 на force_pending=not auto_publish -- но это заставляло
   автопилот публиковать пост МГНОВЕННО в момент нажатия кнопки, в обход
   очереди и обратного отсчёта. Владелец отверг это явно 01-02.08: "написать
   пост сейчас" не должно значить "опубликовать сейчас" ни в одном режиме.

Единая модель очереди (C14, решение владельца 01-02.08): force_pending
остался только за онбордингом (`generate_format`, первый черновик без
очереди). Ручная генерация -- что на автопилоте, что при подтверждении --
всегда получает scheduled_at и публикуется через общий путь
(due_scheduled_posts/tick), разница только в том, нужно ли подтверждение
перед тем как время придёт (см. generate_for_channel, needs_approval).
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

    async def _fake(channel_id, topic="", force_pending=False, target_scheduled_at=None):
        calls.append({"channel_id": channel_id, "force_pending": force_pending,
                       "target_scheduled_at": target_scheduled_at})
        return {"ok": True, "post_id": None}

    monkeypatch.setattr(tasks, "generate_for_channel", _fake)
    return calls


async def test_manual_generate_on_autopilot_joins_queue(client, token, fake_generate):
    """Автопилот: пост встаёт в очередь со scheduled_at, публикуется сам, когда придёт время -- не мгновенно."""
    cid = await _make_channel(client, token, True, "@manual_gen_auto")
    r = await client.post(f"/api/channels/{cid}/generate", json={},
                          headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200, r.text
    assert fake_generate == [{"channel_id": cid, "force_pending": False, "target_scheduled_at": None}]


async def test_manual_generate_on_manual_confirm_joins_queue(client, token, fake_generate):
    """Режим подтверждения: пост тоже встаёт в очередь со scheduled_at -- ждёт кнопки «Опубликовать», а не пропускает очередь."""
    cid = await _make_channel(client, token, False, "@manual_gen_manual")
    r = await client.post(f"/api/channels/{cid}/generate", json={},
                          headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200, r.text
    assert fake_generate == [{"channel_id": cid, "force_pending": False, "target_scheduled_at": None}]
