"""
`tasks.resume_starved_channels` -- после пополнения баланса (обычная оплата,
апгрейд тарифа, ручное начисление) пробуем сдвинуть каналы, которые уже
просрочили расписание, не дожидаясь планового тика.

Найдено владельцем 31.07: оплатил, проверил канал -- в очереди всё ещё 0,
хотя по расписанию пост давно должен был выйти. Генерация молчаливо
блокируется нулевым балансом (generate_for_channel), и раньше возобновления
приходилось ждать до ближайшего тика планировщика.

Автопилот трогаем ТОЛЬКО если канал уже просрочил расписание (`_is_due`) --
пополнение баланса заранее, до конца текущего интервала, не должно вызвать
внеочередной пост раньше обещанного времени. Резерв ручного подтверждения
трогаем всегда, как и на каждом тике.
"""

from datetime import datetime, timedelta

import pytest

import database
import tasks
from database import Channel, User


def _make_channel(email: str, *, auto_publish: bool, interval_hours: float = 6,
                  last_generated_at=None, verified: bool = True, enabled: bool = True) -> tuple[int, int]:
    with database.session() as s:
        u = User(email=email, password_hash="x", token_balance=100_000)
        s.add(u); s.commit(); s.refresh(u)
        ch = Channel(
            user_id=u.id, title="Канал", about="тема", tg_chat=f"@resume_{u.id}",
            verified=verified, enabled=enabled, auto_publish=auto_publish,
            interval_hours=interval_hours, last_generated_at=last_generated_at,
        )
        s.add(ch); s.commit(); s.refresh(ch)
        return u.id, ch.id


@pytest.fixture
def fake_generate(monkeypatch):
    calls = []

    async def _fake(channel_id, topic="", force_pending=False):
        calls.append({"channel_id": channel_id, "force_pending": force_pending})
        return {"ok": True, "post_id": None}

    monkeypatch.setattr(tasks, "generate_for_channel", _fake)
    return calls


async def test_overdue_autopilot_channel_is_generated_now(fake_generate):
    """Ключевой случай из отчёта владельца: расписание уже просрочено."""
    uid, cid = _make_channel(
        "overdue_auto@example.com", auto_publish=True, interval_hours=1,
        last_generated_at=datetime.utcnow() - timedelta(hours=3),
    )
    await tasks.resume_starved_channels(uid)
    assert fake_generate == [{"channel_id": cid, "force_pending": False}]


async def test_autopilot_channel_not_yet_due_is_left_alone(fake_generate):
    """Пополнили баланс заранее -- внеочередного поста раньше срока быть не должно."""
    uid, cid = _make_channel(
        "not_due_auto@example.com", auto_publish=True, interval_hours=6,
        last_generated_at=datetime.utcnow(),
    )
    await tasks.resume_starved_channels(uid)
    assert fake_generate == [], "автопилот сгенерировал пост раньше обещанного интервала"


async def test_manual_channel_reserve_refills_regardless_of_schedule(fake_generate):
    """Резерву ручного подтверждения расписание не указ -- он и на тике общий."""
    uid, cid = _make_channel(
        "manual_resume@example.com", auto_publish=False, interval_hours=6,
        last_generated_at=datetime.utcnow(),
    )
    await tasks.resume_starved_channels(uid)
    assert fake_generate == [{"channel_id": cid, "force_pending": True}]


async def test_disabled_channel_is_skipped(fake_generate):
    uid, cid = _make_channel(
        "disabled_resume@example.com", auto_publish=True, interval_hours=1,
        last_generated_at=datetime.utcnow() - timedelta(hours=3), enabled=False,
    )
    await tasks.resume_starved_channels(uid)
    assert fake_generate == []


async def test_unverified_channel_is_skipped(fake_generate):
    uid, cid = _make_channel(
        "unverified_resume@example.com", auto_publish=True, interval_hours=1,
        last_generated_at=datetime.utcnow() - timedelta(hours=3), verified=False,
    )
    await tasks.resume_starved_channels(uid)
    assert fake_generate == []


async def test_only_touches_the_given_users_channels(fake_generate):
    uid_a, cid_a = _make_channel(
        "resume_user_a@example.com", auto_publish=True, interval_hours=1,
        last_generated_at=datetime.utcnow() - timedelta(hours=3),
    )
    uid_b, cid_b = _make_channel(
        "resume_user_b@example.com", auto_publish=True, interval_hours=1,
        last_generated_at=datetime.utcnow() - timedelta(hours=3),
    )
    await tasks.resume_starved_channels(uid_a)
    assert fake_generate == [{"channel_id": cid_a, "force_pending": False}]
