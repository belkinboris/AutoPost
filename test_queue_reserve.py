"""
Тесты на то, что очередь не растёт бесконечно и пополняется одинаково для
обоих режимов публикации -- `tasks._refill_queue`.

История вопроса (28.07-02.08, три последовательные находки на одном и том же
месте кода):

1. (C10) У канала с включённым автопилотом в очереди навсегда зависли три
   поста «Ждёт вашего решения», созданные в одну минуту. Причина: плановая
   генерация публиковала пост автопилота НАПРЯМУЮ, минуя очередь, а резерв
   (`_refill_if_active`, `force_pending=True`) пополнялся для ЛЮБОГО канала --
   включая автопилот, у которого решение принимать некому. Тогда починили,
   исключив автопилот из пополнения резерва вовсе.
2. (C8) Плановая генерация для режима подтверждения не смотрела на глубину
   очереди -- только на прошедшее время, и пользователь, не заходивший
   неделю, получал сколько угодно постов вместо обещанной «очереди на
   неделю» (`queue_target_for_user`). Добавили потолок `_generate_if_due`,
   отдельный от резерва `_refill_if_active`, но с той же целью и с тем же
   MAX_GEN_PER_TICK.
3. (C14, единая модель очереди, решение владельца 01-02.08) Оказалось, что
   единственная причина, по которой автопилот нужно было исключать из
   пополнения (находка 1) -- это то, что генерация публиковала пост СРАЗУ, в
   обход очереди, и пополнять резерв для канала без способа его когда-либо
   опубликовать было бессмысленно. Как только генерация перестала публиковать
   что-либо напрямую (каждый пост получает `scheduled_at` и публикуется через
   `due_scheduled_posts`/`tick()`, см. `generate_for_channel`), эта причина
   исчезла: пополнение больше не может создать внеочередную публикацию, оно
   только добавляет будущий слот. `_refill_if_active` и `_generate_if_due`
   слились в одну функцию `_refill_queue` без различия по режиму.
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


async def test_autopilot_channel_now_also_refills(fake_generate):
    """
    Ключевой случай, инвертированный решением 01-02.08: раньше автопилот с
    пустой очередью НЕ пополнялся (находка 1 выше). Теперь пополняется как
    любой канал -- посты автопилота тоже стоят в очереди со scheduled_at и
    публикуются сами через tick(), так что пополнение для него так же
    безопасно, как и для подтверждения.
    """
    cid = _make_channel("autopilot@example.com", auto_publish=True)
    await tasks._refill_queue(cid)
    assert fake_generate == [{"channel_id": cid, "force_pending": False}]


async def test_manual_channel_still_refills(fake_generate):
    """Режим подтверждения не задет: очередь по-прежнему держится на MIN_QUEUE."""
    cid = _make_channel("manual@example.com", auto_publish=False)
    await tasks._refill_queue(cid)
    # force_pending=True остался только за онбордингом (generate_format) --
    # обычное пополнение очереди всегда идёт по общему пути со scheduled_at.
    assert fake_generate == [{"channel_id": cid, "force_pending": False}]


async def test_refill_stops_once_target_reached(fake_generate):
    """Очередь не растёт бесконечно -- как только она полна, вызовов нет."""
    cid = _make_channel("full@example.com", auto_publish=False)
    with database.session() as s:
        ch = s.get(Channel, cid)
        target = tasks.queue_target_for_user(s, ch.user_id)
        for i in range(target):
            s.add(Post(channel_id=cid, user_id=ch.user_id, text=f"пост {i}", status="pending"))
        s.commit()

    await tasks._refill_queue(cid)
    assert fake_generate == [], "очередь продолжает пополняться при уже полной глубине"


async def test_refill_resumes_when_slot_frees_up(fake_generate):
    """Как только один пост решён (снят из очереди), пополнение возобновляется."""
    cid = _make_channel("resume@example.com", auto_publish=False)
    with database.session() as s:
        ch = s.get(Channel, cid)
        for i in range(2):
            s.add(Post(channel_id=cid, user_id=ch.user_id, text=f"старый {i}", status="pending"))
        s.commit()

    await tasks._refill_queue(cid)
    assert fake_generate == [{"channel_id": cid, "force_pending": False}]


async def test_paid_user_cap_is_seven_not_three(fake_generate):
    """Оплатившему полагается «очередь на неделю» (7), а не бесплатные 3 --
    тот же queue_target_for_user, что и у резерва, потолок общий."""
    from database import Payment

    cid = _make_channel("paid@example.com", auto_publish=False)
    with database.session() as s:
        ch = s.get(Channel, cid)
        s.add(Payment(user_id=ch.user_id, package_id="p1", label="test",
                      rub=490, tokens=600_000, status="paid"))
        for i in range(5):
            s.add(Post(channel_id=cid, user_id=ch.user_id, text=f"пост {i}", status="pending"))
        s.commit()

    # 5 из 7 -- ещё есть место, пополнение должно сработать
    await tasks._refill_queue(cid)
    assert fake_generate == [{"channel_id": cid, "force_pending": False}]


async def test_autopilot_with_stuck_pending_is_also_capped(fake_generate):
    """Если у автопилота Telegram перестал принимать сообщения и посты
    застряли, потолок глубины по-прежнему защищает от бесконечной генерации."""
    cid = _make_channel("broken_auto@example.com", auto_publish=True)
    with database.session() as s:
        ch = s.get(Channel, cid)
        for i in range(3):
            s.add(Post(channel_id=cid, user_id=ch.user_id, text=f"не ушёл {i}", status="pending"))
        s.commit()

    await tasks._refill_queue(cid)
    assert fake_generate == [], "автопилот с зависшими постами продолжает генерировать без ограничений"


async def test_disabled_channel_is_not_refilled(fake_generate):
    cid = _make_channel("disabled@example.com", auto_publish=True)
    with database.session() as s:
        ch = s.get(Channel, cid)
        ch.enabled = False
        s.add(ch); s.commit()

    await tasks._refill_queue(cid)
    assert fake_generate == []
