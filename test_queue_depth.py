"""
Настраиваемая глубина очереди (C14, решение владельца 01.08): базовая глубина
3 поста, пользователь может увеличить до потолка своего тарифа (7 у
оплатившего). Channel.queue_depth хранит выбор, queue_target_for_user
зажимает его в [MIN_QUEUE_DEPTH, потолок] -- отдельно от самого потолка,
который как и раньше зависит только от факта оплаты.

Границы намеренно разные константы (владелец 02.08): MIN_QUEUE (3) --
потолок бесплатного тарифа, MIN_QUEUE_DEPTH (1) -- минимум, который может
выбрать пользователь. Раньше MIN_QUEUE работал в обеих ролях сразу, и
опустить очередь до одного поста было нельзя.
"""

from datetime import datetime, timedelta

import database
import tasks
from database import Channel, Payment, Post, User


def _make_user(email: str, paid: bool = False) -> int:
    with database.session() as s:
        u = User(email=email, password_hash="x", token_balance=100_000)
        s.add(u); s.commit(); s.refresh(u)
        if paid:
            s.add(Payment(user_id=u.id, package_id="p1", label="test", rub=490,
                          tokens=600_000, status="paid"))
            s.commit()
        return u.id


def _make_channel(user_id: int, queue_depth=None) -> int:
    with database.session() as s:
        ch = Channel(user_id=user_id, title="Канал", about="тема",
                     queue_depth=queue_depth)
        s.add(ch); s.commit(); s.refresh(ch)
        return ch.id


async def test_default_ceiling_unchanged_for_free_user():
    uid = _make_user("qd_free_default@t.local")
    with database.session() as s:
        assert tasks.queue_target_for_user(s, uid) == tasks.MIN_QUEUE


async def test_default_ceiling_unchanged_for_paid_user():
    uid = _make_user("qd_paid_default@t.local", paid=True)
    with database.session() as s:
        assert tasks.queue_target_for_user(s, uid) == tasks.PAID_QUEUE


async def test_channel_without_queue_depth_uses_full_ceiling():
    uid = _make_user("qd_paid_no_depth@t.local", paid=True)
    cid = _make_channel(uid, queue_depth=None)
    with database.session() as s:
        ch = s.get(Channel, cid)
        assert tasks.queue_target_for_user(s, uid, ch) == tasks.PAID_QUEUE


async def test_paid_user_can_pick_value_within_range():
    uid = _make_user("qd_paid_pick@t.local", paid=True)
    cid = _make_channel(uid, queue_depth=5)
    with database.session() as s:
        ch = s.get(Channel, cid)
        assert tasks.queue_target_for_user(s, uid, ch) == 5


async def test_free_user_queue_depth_clamped_to_free_ceiling():
    """Бесплатный пользователь физически не может задать больше 3 -- даже если queue_depth в базе почему-то равен 7."""
    uid = _make_user("qd_free_clamped@t.local")
    cid = _make_channel(uid, queue_depth=7)
    with database.session() as s:
        ch = s.get(Channel, cid)
        assert tasks.queue_target_for_user(s, uid, ch) == tasks.MIN_QUEUE


async def test_queue_depth_clamped_to_paid_ceiling():
    """Оплативший не может задать больше 7, даже если в базе оказалось больше."""
    uid = _make_user("qd_paid_overflow@t.local", paid=True)
    cid = _make_channel(uid, queue_depth=99)
    with database.session() as s:
        ch = s.get(Channel, cid)
        assert tasks.queue_target_for_user(s, uid, ch) == tasks.PAID_QUEUE


async def test_queue_depth_of_one_is_honored():
    """Владелец 02.08: очередь можно опустить до одного поста.

    Раньше нижней границей был MIN_QUEUE (3) -- и выбор «держать наготове
    один пост» молча превращался в три: степпер показывал бы 1, а очередь
    набирала бы 3. Теперь граница -- отдельная константа MIN_QUEUE_DEPTH,
    и MIN_QUEUE остался тем, чем и был: потолком бесплатного тарифа
    (см. test_free_user_queue_depth_clamped_to_free_ceiling выше).
    """
    uid = _make_user("qd_paid_low@t.local", paid=True)
    cid = _make_channel(uid, queue_depth=1)
    with database.session() as s:
        ch = s.get(Channel, cid)
        assert tasks.queue_target_for_user(s, uid, ch) == 1


async def test_queue_depth_clamped_up_to_absolute_minimum():
    """Ноль и отрицательные значения (кривой запрос, старые данные) не
    должны останавливать очередь совсем: канал без единого поста наготове
    перестал бы писать вообще, а интерфейс продолжал бы обещать посты.
    """
    uid = _make_user("qd_zero@t.local", paid=True)
    cid = _make_channel(uid, queue_depth=-5)
    with database.session() as s:
        ch = s.get(Channel, cid)
        assert tasks.queue_target_for_user(s, uid, ch) >= tasks.MIN_QUEUE_DEPTH


async def test_refill_queue_respects_channel_queue_depth(monkeypatch):
    """_refill_queue должен пополнять до queue_depth канала, а не до общего потолка тарифа."""
    calls = []

    async def _fake(channel_id, topic="", force_pending=False):
        calls.append(channel_id)
        return {"ok": True, "post_id": None}

    monkeypatch.setattr(tasks, "generate_for_channel", _fake)

    uid = _make_user("qd_refill@t.local", paid=True)
    with database.session() as s:
        ch = Channel(user_id=uid, title="Канал", about="тема", tg_chat="@qd_refill",
                     verified=True, enabled=True, queue_depth=4)
        s.add(ch); s.commit(); s.refresh(ch)
        cid = ch.id
        from database import Post
        for i in range(4):
            s.add(Post(channel_id=cid, user_id=uid, text=f"пост {i}", status="pending"))
        s.commit()

    # Очередь уже на уровне queue_depth=4 (меньше потолка тарифа 7) -- пополнять не нужно.
    await tasks._refill_queue(cid)
    assert calls == [], "очередь заполнена до выбранной глубины 4 -- потолок тарифа (7) здесь ни при чём"


# ── PATCH /api/channels/{id}: зажим queue_depth при записи ─────────────────

async def _create_channel(client, token):
    r = await client.post("/api/channels", json={
        "title": "Канал", "about": "тема", "tg_chat": "@qd_http",
    }, headers={"Authorization": f"Bearer {token}"})
    r.raise_for_status()
    return r.json()["id"]


async def test_patch_clamps_queue_depth_for_free_user(client, token):
    cid = await _create_channel(client, token)
    r = await client.patch(f"/api/channels/{cid}", json={"queue_depth": 7},
                            headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200, r.text
    assert r.json()["queue_depth"] == tasks.MIN_QUEUE, (
        "бесплатный пользователь не должен суметь сохранить queue_depth выше потолка тарифа"
    )


async def test_patch_accepts_queue_depth_within_paid_ceiling(client, token):
    r = await client.get("/api/me", headers={"Authorization": f"Bearer {token}"})
    email = r.json()["email"]
    with database.session() as s:
        u = s.exec(database.select(User).where(User.email == email)).first()
        s.add(Payment(user_id=u.id, package_id="p1", label="test", rub=490,
                      tokens=600_000, status="paid"))
        s.commit()

    cid = await _create_channel(client, token)
    r = await client.patch(f"/api/channels/{cid}", json={"queue_depth": 5},
                            headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200, r.text
    assert r.json()["queue_depth"] == 5


async def test_patch_saves_queue_depth_of_one(client, token):
    r = await client.get("/api/me", headers={"Authorization": f"Bearer {token}"})
    email = r.json()["email"]
    with database.session() as s:
        u = s.exec(database.select(User).where(User.email == email)).first()
        s.add(Payment(user_id=u.id, package_id="p1", label="test", rub=490,
                      tokens=600_000, status="paid"))
        s.commit()

    cid = await _create_channel(client, token)
    r = await client.patch(f"/api/channels/{cid}", json={"queue_depth": 1},
                            headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200, r.text
    # Сохранилось ровно то, что выбрали. Зажим до MIN_QUEUE здесь стоял до
    # 02.08 -- степпер уже умел показывать 1, а сервер молча переписывал
    # значение на 3, и человек не понимал, почему очередь всё равно растёт.
    assert r.json()["queue_depth"] == 1
    assert r.json()["queue_target"] == 1
    with database.session() as s:
        assert s.get(Channel, cid).queue_depth == 1


async def test_refill_queue_writes_exactly_one_post_at_depth_one(monkeypatch):
    """Глубина 1 должна реально останавливать пополнение после первого поста.

    Проверяем не зажим, а последствие: раньше значение 1 зажималось до
    MIN_QUEUE, и такой канал молча набирал три поста. Тест смотрит на число
    вызовов генерации -- то, что человек видит как длину очереди.
    """
    calls = []

    async def _fake(channel_id, topic="", force_pending=False, **kw):
        calls.append(channel_id)
        with database.session() as s:
            s.add(Post(channel_id=channel_id, user_id=uid, text=f"пост {len(calls)}",
                       status="scheduled", scheduled_at=datetime.utcnow() + timedelta(days=len(calls))))
            s.commit()
        return {"ok": True, "post_id": None}

    monkeypatch.setattr(tasks, "generate_for_channel", _fake)

    uid = _make_user("qd_depth_one@t.local", paid=True)
    cid = _make_channel(uid, queue_depth=1)

    await tasks._refill_queue(cid)
    assert len(calls) == 1, f"при глубине 1 написали постов: {len(calls)}"

    # Второй проход не должен добавить ничего: место занято.
    await tasks._refill_queue(cid)
    assert len(calls) == 1, "очередь глубиной 1 продолжила расти"


async def test_channel_card_gets_time_of_the_next_publication(client, token):
    """Карточка канала показывает время ПУБЛИКАЦИИ ближайшего поста
    (владелец 02.08), а берёт его из `next_post_at`. Раньше этого поля не
    было вовсе, и карточка считала время следующей генерации по формуле,
    которой нет на сервере.
    """
    r = await client.post("/api/channels", json={
        "title": "Карточка", "about": "тема", "tg_chat": "@card_demo",
        "auto_publish": True, "interval_hours": 24,
    }, headers={"Authorization": f"Bearer {token}"})
    r.raise_for_status()
    cid = r.json()["id"]

    soon = datetime.utcnow() + timedelta(hours=3)
    later = datetime.utcnow() + timedelta(days=2)
    with database.session() as s:
        u = s.get(Channel, cid)
        # Кладём в обратном порядке -- поле обязано вернуть БЛИЖАЙШИЙ пост,
        # а не первый созданный.
        s.add(Post(channel_id=cid, user_id=u.user_id, text="дальний",
                   status="scheduled", scheduled_at=later))
        s.commit()
        s.add(Post(channel_id=cid, user_id=u.user_id, text="ближний",
                   status="scheduled", scheduled_at=soon))
        s.commit()

    r = await client.get(f"/api/channels/{cid}", headers={"Authorization": f"Bearer {token}"})
    r.raise_for_status()
    got = r.json()["next_post_at"]
    assert got, "карточке нечего показать: next_post_at пуст"
    assert abs((datetime.fromisoformat(got.rstrip("Z")) - soon).total_seconds()) < 2, (
        f"вернулось время дальнего поста, а не ближайшего: {got}"
    )
