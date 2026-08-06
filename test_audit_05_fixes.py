"""
Аудит 05.08: правки после широкого разбора кода.

Здесь -- то, что удобнее проверять напрямую: атомарные захваты денег
(апгрейд/возврат больше не начисляют вдвое при гонке), защита schedule_post,
удаление опасного /api/bot/start, и что func.count не изменил счётчики.

Гонки апгрейда/возврата тестируются на уровне ПРИМИТИВОВ захвата, а не через
два параллельных HTTP-запроса: тестовый клиент крутит корутины по очереди в
одном event loop, настоящей параллельности там нет. Атомарность живёт в
условном UPDATE (rowcount==1), его и проверяем -- ровно как для уже
существующих claim_payment_for_credit/claim_post_for_publish.
"""

from datetime import datetime, timedelta

import pytest
from sqlmodel import select

import database
from database import Channel, Payment, Post, Subscription, User


# ── #1: опасный /api/bot/start удалён ─────────────────────────────────────

async def test_bot_start_endpoint_is_gone(client):
    """Публичный незащищённый эндпоинт захвата аккаунта должен вернуть 404 --
    его больше нет в роутинге. /start обрабатывается поллингом."""
    r = await client.post("/api/bot/start", json={
        "message": {"text": "/start u1", "chat": {"id": 999}},
    })
    assert r.status_code == 404, "эндпоинт /api/bot/start всё ещё принимает запросы"


# ── #7: schedule_post не воскрешает опубликованный / не пускает в прошлое ──

def _user_channel_post(email, status="scheduled"):
    with database.session() as s:
        u = User(email=email, password_hash="x", token_balance=1000)
        s.add(u); s.commit(); s.refresh(u)
        ch = Channel(user_id=u.id, title="К", about="тема", tg_chat="@x", verified=True)
        s.add(ch); s.commit(); s.refresh(ch)
        p = Post(channel_id=ch.id, user_id=u.id, text="пост", status=status,
                 scheduled_at=datetime.utcnow() + timedelta(hours=2))
        s.add(p); s.commit(); s.refresh(p)
        return u.id, ch.id, p.id


async def _login(client, email):
    # регистрируем через API, чтобы получить рабочий токен под этого юзера
    r = await client.post("/api/register", json={"email": email, "password": "pass12345"})
    return r.json()["token"], r.json().get("id") or (await client.get(
        "/api/me", headers={"Authorization": f"Bearer {r.json()['token']}"})).json()["id"]


async def _own_post_for(client, token, uid, status):
    with database.session() as s:
        ch = Channel(user_id=uid, title="К", about="тема", tg_chat="@x", verified=True)
        s.add(ch); s.commit(); s.refresh(ch)
        p = Post(channel_id=ch.id, user_id=uid, text="пост", status=status,
                 scheduled_at=datetime.utcnow() - timedelta(hours=1) if status == "published" else None,
                 published_at=datetime.utcnow() - timedelta(hours=1) if status == "published" else None)
        s.add(p); s.commit(); s.refresh(p)
        return p.id


async def test_schedule_rejects_published_post(client):
    token, uid = await _login(client, "sched_pub@t.local")
    pid = await _own_post_for(client, token, uid, "published")
    future = (datetime.utcnow() + timedelta(days=1)).isoformat() + "Z"
    r = await client.post(f"/api/posts/{pid}/schedule", json={"scheduled_at": future},
                          headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 400, "опубликованный пост нельзя вернуть в очередь"
    with database.session() as s:
        assert s.get(Post, pid).status == "published", "статус опубликованного поста изменён"


async def test_schedule_rejects_past_time(client):
    token, uid = await _login(client, "sched_past@t.local")
    pid = await _own_post_for(client, token, uid, "scheduled")
    past = (datetime.utcnow() - timedelta(hours=1)).isoformat() + "Z"
    r = await client.post(f"/api/posts/{pid}/schedule", json={"scheduled_at": past},
                          headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 400, "прошлое время -> мгновенная публикация автопилотом, это не «Запланировать»"


async def test_schedule_accepts_future_time(client):
    token, uid = await _login(client, "sched_ok@t.local")
    pid = await _own_post_for(client, token, uid, "scheduled")
    future = (datetime.utcnow() + timedelta(days=1)).isoformat() + "Z"
    r = await client.post(f"/api/posts/{pid}/schedule", json={"scheduled_at": future},
                          headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200, r.text


# ── #3: атомарный захват платежа на возврат ───────────────────────────────

def _paid_payment(uid, operation_id="yk-1"):
    with database.session() as s:
        pay = Payment(user_id=uid, package_id="p1", label=f"u{uid}-x", rub=490,
                      tokens=600_000, status="paid", operation_id=operation_id,
                      paid_at=datetime.utcnow())
        s.add(pay); s.commit(); s.refresh(pay)
        return pay.id


def test_refund_claim_wins_exactly_once():
    """Два «возврата» по одному платежу: захват отдаёт True ровно одному,
    второй получает False и НЕ должен списывать токены (иначе человек теряет
    вдвое больше, чем вернул)."""
    with database.session() as s:
        u = User(email="refclaim@t.local", password_hash="x", token_balance=600_000)
        s.add(u); s.commit(); s.refresh(u)
        uid = u.id
    pid = _paid_payment(uid)

    with database.session() as s:
        first = database.claim_payment_for_refund(s, pid)
    with database.session() as s:
        second = database.claim_payment_for_refund(s, pid)

    assert first is True and second is False, "захват возврата не сериализует параллельные запросы"
    with database.session() as s:
        assert s.get(Payment, pid).status == "refunding"


def test_refund_claim_hides_payment_from_refundable_search():
    """Захваченный платёж (status='refunding') обязан ВЫПАСТЬ из выборки
    refundable -- иначе второй параллельный запрос вернул бы СЛЕДУЮЩИЙ по
    свежести платёж, о котором человек не просил."""
    import main
    with database.session() as s:
        u = User(email="refhide@t.local", password_hash="x", token_balance=600_000)
        s.add(u); s.commit(); s.refresh(u)
        uid = u.id
    pid = _paid_payment(uid)

    with database.session() as s:
        assert database.claim_payment_for_refund(s, pid) is True
    with database.session() as s:
        found, reason = main._find_refundable_payment(s, uid)
    assert found is None, "платёж в процессе возврата всё ещё считается доступным к возврату"


def test_refund_release_returns_payment_to_paid():
    """ЮKassa не подтвердила возврат -> захват отпускается, человек может
    повторить."""
    with database.session() as s:
        u = User(email="refrel@t.local", password_hash="x", token_balance=600_000)
        s.add(u); s.commit(); s.refresh(u)
        uid = u.id
    pid = _paid_payment(uid)
    with database.session() as s:
        database.claim_payment_for_refund(s, pid)
    with database.session() as s:
        database.release_payment_refund_claim(s, pid)
        assert s.get(Payment, pid).status == "paid"


# ── #2: атомарный захват апгрейда ─────────────────────────────────────────

def test_upgrade_claim_applies_once():
    """Два параллельных апгрейда по одной старой цене: захват выигрывает
    ровно один (он и начислит токены), второй получает False и не начисляет.
    Списание в ЮKassa при этом одно (детерминированный idempotence_key)."""
    with database.session() as s:
        u = User(email="upclaim@t.local", password_hash="x", token_balance=0)
        s.add(u); s.commit(); s.refresh(u)
        sub = Subscription(user_id=u.id, package_id="p1", price_rub=490,
                           payment_method_id="pm", status="active",
                           next_charge_at=datetime.utcnow() + timedelta(days=20))
        s.add(sub); s.commit(); s.refresh(sub)
        sub_id = sub.id
    nca = datetime.utcnow() + timedelta(days=30)

    with database.session() as s:
        first = database.claim_subscription_upgrade(s, sub_id, from_price_rub=490,
                                                    new_package_id="p2", new_price_rub=990,
                                                    next_charge_at=nca)
    with database.session() as s:
        second = database.claim_subscription_upgrade(s, sub_id, from_price_rub=490,
                                                     new_package_id="p2", new_price_rub=990,
                                                     next_charge_at=nca)
    assert first is True and second is False, "апгрейд применился дважды -> двойное начисление токенов"
    with database.session() as s:
        sub = s.get(Subscription, sub_id)
        assert sub.package_id == "p2" and sub.price_rub == 990


def test_upgrade_claim_ignores_wrong_starting_price():
    """Захват по УЖЕ НЕ той цене не проходит: подписку уже подняли, повторный
    запрос со старой ценой на входе не должен ничего менять."""
    with database.session() as s:
        u = User(email="upwrong@t.local", password_hash="x", token_balance=0)
        s.add(u); s.commit(); s.refresh(u)
        sub = Subscription(user_id=u.id, package_id="p2", price_rub=990,
                           payment_method_id="pm", status="active",
                           next_charge_at=datetime.utcnow() + timedelta(days=20))
        s.add(sub); s.commit(); s.refresh(sub)
        sub_id = sub.id
    with database.session() as s:
        # приходит запрос, думающий что цена ещё 490 -- уже неверно
        applied = database.claim_subscription_upgrade(s, sub_id, from_price_rub=490,
                                                      new_package_id="p3", new_price_rub=2490,
                                                      next_charge_at=datetime.utcnow())
    assert applied is False
    with database.session() as s:
        assert s.get(Subscription, sub_id).package_id == "p2", "апгрейд применился по устаревшей цене"


# ── #13: func.count не изменил счётчики ───────────────────────────────────

async def test_channel_counters_are_correct_after_count_optimization(client):
    """queue_count и published_count после перехода на func.count() обязаны
    совпадать с реальным числом постов -- это регрессионная страховка на
    оптимизацию, которая грузила все тексты ради len()."""
    token, uid = await _login(client, "counters@t.local")
    with database.session() as s:
        ch = Channel(user_id=uid, title="К", about="тема", tg_chat="@cnt", verified=True)
        s.add(ch); s.commit(); s.refresh(ch)
        cid = ch.id
        for i in range(3):
            s.add(Post(channel_id=cid, user_id=uid, text=f"в очереди {i}",
                       status="scheduled", scheduled_at=datetime.utcnow() + timedelta(hours=i + 1)))
        for i in range(5):
            s.add(Post(channel_id=cid, user_id=uid, text=f"опубликован {i}", status="published"))
        s.commit()

    r = await client.get("/api/channels", headers={"Authorization": f"Bearer {token}"})
    r.raise_for_status()
    card = next(c for c in r.json() if c["id"] == cid)
    assert card["queue_count"] == 3, card
    assert card["published_count"] == 5, card
