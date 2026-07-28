"""
Тесты на то, что очередь не растёт бесконечно: ни резерв (`_refill_if_active`),
ни плановая генерация по расписанию (`_generate_if_due`).

Первая находка (28.07): у канала с включённым автопилотом в очереди навсегда
зависли три поста «Ждёт вашего решения», созданные в одну минуту. Автопилот
их не публикует — плановая генерация делает свой пост и публикует его
напрямую (`generate_for_channel`, ветка `channel.auto_publish and not
force_pending`), минуя очередь вовсе. А резерв (`MIN_QUEUE=3`,
`force_pending=True`) прежде пополнялся для ЛЮБОГО канала — включая тот,
где решение принимать некому и не с чего: подтверждать эти посты в
автопилоте никто не должен.

Вторая находка, решение владельца в том же разговоре (C8): плановая
генерация для канала БЕЗ автопилота прежде не смотрела, сколько постов уже
ждут решения — только на прошедшее время. Пользователь, не заходивший
неделю, получал не «очередь на неделю» (обещание онбординга — 7 постов
оплатившему, 3 бесплатному, см. `queue_target_for_user`), а сколько угодно
постов — по числу сработавших тиков расписания. Теперь `_generate_if_due`
останавливается на той же целевой глубине, что и резерв.

Резерв (первая находка) нужен только режиму «подтверждение вручную» и
пополняется только там. Потолок плановой генерации (вторая находка) общий
для обоих режимов: он же защищает автопилот на случай, если Telegram
перестал принимать сообщения и посты застревают в pending.
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


# ── C8: плановая генерация не должна расти бесконечно ──────────────────────

async def test_scheduled_generation_stops_at_target_depth(fake_generate):
    """Ручной режим, очередь уже полна (3 неразобранных поста) -- новую
    плановую генерацию не запускаем, сколько бы тиков расписания ни сработало."""
    cid = _make_channel("cap@example.com", auto_publish=False)
    with database.session() as s:
        ch = s.get(Channel, cid)
        for i in range(3):
            s.add(Post(channel_id=cid, user_id=ch.user_id, text=f"старый {i}", status="pending"))
        s.commit()

    await tasks._generate_if_due(cid)
    assert fake_generate == [], "плановая генерация не остановилась на целевой глубине"


async def test_scheduled_generation_resumes_when_slot_frees_up(fake_generate):
    """Как только один пост решён (снят из очереди), генерация возобновляется."""
    cid = _make_channel("resume@example.com", auto_publish=False)
    with database.session() as s:
        ch = s.get(Channel, cid)
        for i in range(2):
            s.add(Post(channel_id=cid, user_id=ch.user_id, text=f"старый {i}", status="pending"))
        s.commit()

    await tasks._generate_if_due(cid)
    assert fake_generate == [{"channel_id": cid, "force_pending": False}]


async def test_paid_user_cap_is_seven_not_three(fake_generate):
    """Оплатившему полагается «очередь на неделю» (7), а не бесплатные 3 --
    это тот же queue_target_for_user, что и у резерва, потолок общий."""
    from database import Payment

    cid = _make_channel("paid@example.com", auto_publish=False)
    with database.session() as s:
        ch = s.get(Channel, cid)
        s.add(Payment(user_id=ch.user_id, package_id="p1", label="test",
                      rub=490, tokens=600_000, status="paid"))
        for i in range(5):
            s.add(Post(channel_id=cid, user_id=ch.user_id, text=f"пост {i}", status="pending"))
        s.commit()

    # 5 из 7 -- ещё есть место, генерация должна сработать
    await tasks._generate_if_due(cid)
    assert fake_generate == [{"channel_id": cid, "force_pending": False}]


async def test_healthy_autopilot_is_not_blocked(fake_generate):
    """Автопилот без сбоев публикует сразу, в очереди пусто -- потолок не
    должен мешать плановой генерации в штатном режиме."""
    cid = _make_channel("healthy_auto@example.com", auto_publish=True)
    await tasks._generate_if_due(cid)
    assert fake_generate == [{"channel_id": cid, "force_pending": False}]


async def test_autopilot_with_stuck_pending_is_also_capped(fake_generate):
    """Если у автопилота Telegram перестал принимать сообщения, посты
    остаются pending (см. generate_for_channel) -- потолок защищает и этот
    канал от бесконечных попыток, хотя формально это не «резерв»."""
    cid = _make_channel("broken_auto@example.com", auto_publish=True)
    with database.session() as s:
        ch = s.get(Channel, cid)
        for i in range(3):
            s.add(Post(channel_id=cid, user_id=ch.user_id, text=f"не ушёл {i}", status="pending"))
        s.commit()

    await tasks._generate_if_due(cid)
    assert fake_generate == [], "автопилот с зависшими постами продолжает генерировать без ограничений"
