"""
`tasks.resume_starved_channels` -- после пополнения баланса (обычная оплата,
апгрейд тарифа, ручное начисление) пробуем сразу дополнить очередь, не
дожидаясь планового тика.

Найдено владельцем 31.07: оплатил, проверил канал -- в очереди всё ещё 0,
хотя баланс уже есть. Генерация молчаливо блокируется нулевым балансом
(generate_for_channel), и раньше возобновления приходилось ждать до
ближайшего тика планировщика.

Единая модель очереди (C14, решение владельца 01-02.08): пополнение больше не
может создать внеочередную публикацию -- новый пост просто получает следующий
свободный слот расписания (`_next_queue_slot`), а не публикуется сам. Поэтому
здесь больше нет разницы между автопилотом и режимом подтверждения, и нет
проверки "расписание ещё не подошло" (`_is_due` удалена вместе со старой
моделью) -- единственный критерий это глубина очереди (`_refill_queue`).
"""

from datetime import datetime, timedelta

import pytest

import database
import tasks
from database import Channel, Post, User


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


async def test_empty_queue_autopilot_channel_is_generated_now(fake_generate):
    """Ключевой случай из отчёта владельца: очередь пуста, баланс уже есть."""
    uid, cid = _make_channel(
        "overdue_auto@example.com", auto_publish=True, interval_hours=1,
        last_generated_at=datetime.utcnow() - timedelta(hours=3),
    )
    await tasks.resume_starved_channels(uid)
    assert fake_generate == [{"channel_id": cid, "force_pending": False}]


async def test_recently_generated_autopilot_still_refills_if_queue_empty(fake_generate):
    """
    В единой модели очереди расписание (`last_generated_at`) больше не
    решает, пополнять ли очередь -- решает только её глубина. Раньше
    "только что сгенерировали" останавливало пополнение автопилота
    (`_is_due`); теперь у автопилота, как и у подтверждения, посты живут в
    очереди со `scheduled_at`, и пустая очередь -- это всегда повод
    пополнить, даже если последний пост был только что.
    """
    uid, cid = _make_channel(
        "not_due_auto@example.com", auto_publish=True, interval_hours=6,
        last_generated_at=datetime.utcnow(),
    )
    await tasks.resume_starved_channels(uid)
    assert fake_generate == [{"channel_id": cid, "force_pending": False}]


async def test_autopilot_channel_with_full_queue_is_left_alone(fake_generate):
    """Очередь уже на целевой глубине -- лишний пост генерировать не нужно."""
    uid, cid = _make_channel(
        "full_auto@example.com", auto_publish=True, interval_hours=6,
        last_generated_at=datetime.utcnow(),
    )
    with database.session() as s:
        for i in range(tasks.MIN_QUEUE):
            s.add(Post(channel_id=cid, user_id=uid, text=f"пост {i}", status="scheduled",
                       scheduled_at=datetime.utcnow() + timedelta(hours=i + 1)))
        s.commit()
    await tasks.resume_starved_channels(uid)
    assert fake_generate == []


async def test_manual_channel_reserve_refills_regardless_of_schedule(fake_generate):
    """Резерву подтверждения расписание не указ -- он и на тике общий."""
    uid, cid = _make_channel(
        "manual_resume@example.com", auto_publish=False, interval_hours=6,
        last_generated_at=datetime.utcnow(),
    )
    await tasks.resume_starved_channels(uid)
    # force_pending=True остался только для онбординга (generate_format) --
    # плановое/ручное пополнение очереди всегда идёт по общему пути со
    # scheduled_at, см. tasks._refill_queue.
    assert fake_generate == [{"channel_id": cid, "force_pending": False}]


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
