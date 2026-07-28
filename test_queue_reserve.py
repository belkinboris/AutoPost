"""
Тест на резерв очереди (`tasks._refill_if_active`) и автопилот.

Найдено вживую 28.07: у канала с включённым автопилотом в очереди навсегда
зависли три поста «Ждёт вашего решения», созданные в одну минуту. Автопилот
их не публикует — плановая генерация делает свой пост и публикует его
напрямую (`generate_for_channel`, ветка `channel.auto_publish and not
force_pending`), минуя очередь вовсе. А резерв (`MIN_QUEUE=3`,
`force_pending=True`) прежде пополнялся для ЛЮБОГО канала — включая тот,
где решение принимать некому и не с чего: подтверждать эти посты в
автопилоте никто не должен.

Итог был такой: реальные токены тратились на посты, которые не публикует
ни автопилот (у него свой пост), ни пользователь (для автопилота в
интерфейсе нет экрана подтверждения) — они просто лежали мёртвым грузом.

Резерв нужен только режиму «подтверждение вручную», и только там должен
пополняться.
"""

import pytest
from sqlmodel import select

import database
import tasks
from database import Channel, Post, User


def _make_channel(email: str, auto_publish: bool) -> int:
    with database.session() as s:
        u = User(email=email, password_hash="x", token_balance=100_000)
        s.add(u); s.commit(); s.refresh(u)
        ch = Channel(user_id=u.id, title="Канал", about="тема", tg_chat="@demo",
                     verified=True, enabled=True, auto_publish=auto_publish)
        s.add(ch); s.commit(); s.refresh(ch)
        return ch.id


@pytest.fixture
def fake_generate(monkeypatch):
    """Считает вызовы, не обращаясь к модели -- резерв это делает и в проде."""
    calls = []

    async def _fake(channel_id, topic="", force_pending=False):
        calls.append({"channel_id": channel_id, "force_pending": force_pending})
        return {"ok": True, "post_id": None}

    monkeypatch.setattr(tasks, "generate_for_channel", _fake)
    return calls


async def test_autopilot_channel_does_not_refill_reserve(fake_generate):
    """Ключевой случай: автопилот, очередь пуста -- резерв не трогаем вовсе."""
    cid = _make_channel("autopilot@example.com", auto_publish=True)
    await tasks._refill_if_active(cid)
    assert fake_generate == [], "резерв пополнился, хотя автопилот его не использует"


async def test_manual_channel_still_refills_reserve(fake_generate):
    """Ручной режим не задет: резерв по-прежнему держит MIN_QUEUE постов."""
    cid = _make_channel("manual@example.com", auto_publish=False)
    await tasks._refill_if_active(cid)
    assert fake_generate == [{"channel_id": cid, "force_pending": True}]


async def test_manual_channel_stops_once_target_reached(fake_generate):
    """Резерв не растёт бесконечно -- как только очередь полна, вызовов нет."""
    cid = _make_channel("full@example.com", auto_publish=False)
    with database.session() as s:
        ch = s.get(Channel, cid)
        target = tasks.queue_target_for_user(s, ch.user_id)
        for i in range(target):
            s.add(Post(channel_id=cid, user_id=ch.user_id, text=f"пост {i}", status="pending"))
        s.commit()

    await tasks._refill_if_active(cid)
    assert fake_generate == [], "резерв продолжает пополняться при уже полной очереди"
