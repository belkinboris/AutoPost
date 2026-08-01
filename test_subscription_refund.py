"""
POST /api/subscription/refund -- самообслуживаемый возврат через API ЮKassa
вместо письма на почту.

Запрошено владельцем 31.07: письмо можно не увидеть вовремя, а обещанный
"1 рабочий день" в static/legal/refund.html — реальное обязательство перед
человеком. Условия ровно те же, что в документе: 3 дня с оплаты и ни одного
поста после неё. ЮKassa здесь подменена (billing.refund_payment) -- наружу
не уходит ни один запрос.
"""

from datetime import datetime, timedelta

import pytest
from sqlmodel import select

import billing
import config
import database
from database import Channel, Payment, Post, Subscription, User

PKG = "p1"

# Никакой глобальной очистки таблиц здесь: каждый тест регистрирует
# собственного пользователя через фикстуру token, а все запросы ручки
# фильтруются по user_id -- пересечения с данными других тестов нет. Общая
# очистка Post/PostApproval через все тесты сессии уже один раз уронила
# соседние файлы FK-нарушением (PostApproval.post_id) -- решили не повторять.


@pytest.fixture
def yookassa(monkeypatch):
    class _Fake:
        def __init__(self):
            self.calls = []
            self.response = {"id": "refund-1", "status": "succeeded"}

        async def refund_payment(self, **kw):
            self.calls.append(kw)
            return self.response

    fake = _Fake()
    monkeypatch.setattr(billing, "is_configured", lambda: True)
    monkeypatch.setattr(billing, "refund_payment", fake.refund_payment)
    monkeypatch.setattr(config, "SUBSCRIPTION_ENABLED", True)
    return fake


async def _subscriber_with_payment(client, token, *, paid_at, package_id=PKG,
                                   rub=490, tokens=600_000, payment_method_id="pm-test"):
    me = await client.get("/api/me", headers={"Authorization": f"Bearer {token}"})
    uid = me.json()["id"]
    with database.session() as s:
        u = s.get(User, uid)
        u.token_balance = tokens
        s.add(u)
        s.add(Subscription(
            user_id=uid, package_id=package_id, price_rub=rub,
            payment_method_id=payment_method_id, status="active",
            next_charge_at=datetime.utcnow() + timedelta(days=20),
        ))
        pay = Payment(
            user_id=uid, package_id=package_id, label=f"u{uid}-test",
            rub=rub, tokens=tokens, status="paid",
            operation_id="yk-payment-1", paid_at=paid_at,
        )
        s.add(pay)
        s.commit()
        s.refresh(pay)
        return uid, pay.id


async def _refund(client, token):
    return await client.post("/api/subscription/refund", headers={"Authorization": f"Bearer {token}"})


# ── 1. Основной случай: возврат в пределах 3 дней, токены не тронуты ──────

async def test_refund_within_window_cancels_subscription_and_credits_back(client, token, yookassa):
    uid, pay_id = await _subscriber_with_payment(client, token, paid_at=datetime.utcnow() - timedelta(hours=1))

    r = await _refund(client, token)
    assert r.status_code == 200, r.text
    assert r.json()["refunded_rub"] == 490

    assert len(yookassa.calls) == 1
    assert yookassa.calls[0]["payment_operation_id"] == "yk-payment-1"
    assert yookassa.calls[0]["amount_rub"] == 490
    assert yookassa.calls[0]["idempotence_key"] == f"refund-{pay_id}"

    with database.session() as s:
        sub = s.exec(select(Subscription).where(Subscription.user_id == uid)).first()
        pay = s.get(Payment, pay_id)
        u = s.get(User, uid)
        assert sub.status == "cancelled"
        assert sub.payment_method_id == ""
        assert sub.next_charge_at is None
        assert pay.status == "refunded"
        assert u.token_balance == 0, "выданные этой оплатой токены должны списаться обратно"


# ── 2. Дедлайн: больше 3 дней -- возврат недоступен ───────────────────────

async def test_refund_rejected_after_window(client, token, yookassa):
    await _subscriber_with_payment(client, token, paid_at=datetime.utcnow() - timedelta(days=4))
    r = await _refund(client, token)
    assert r.status_code == 400
    assert "дней" in r.json()["detail"].lower()
    assert yookassa.calls == []


# ── 3. Токены уже использованы (появился пост после оплаты) ──────────────

async def test_refund_rejected_if_tokens_already_used(client, token, yookassa):
    uid, pay_id = await _subscriber_with_payment(client, token, paid_at=datetime.utcnow() - timedelta(hours=2))
    with database.session() as s:
        ch = Channel(user_id=uid, title="Канал", about="тема", tg_chat="@refund_used_test")
        s.add(ch); s.commit(); s.refresh(ch)
        s.add(Post(channel_id=ch.id, user_id=uid, text="Пост", status="published",
                   created_at=datetime.utcnow() - timedelta(hours=1)))
        s.commit()

    r = await _refund(client, token)
    assert r.status_code == 400
    assert "пост" in r.json()["detail"].lower()
    assert yookassa.calls == []


# ── 4. Без активной подписки -- нечего гасить ─────────────────────────────

async def test_refund_without_subscription_is_rejected(client, token, yookassa):
    r = await _refund(client, token)
    assert r.status_code == 400
    assert yookassa.calls == []


# ── 5. Повторный вызов после успешного возврата не проходит дважды ───────

async def test_double_refund_is_blocked(client, token, yookassa):
    await _subscriber_with_payment(client, token, paid_at=datetime.utcnow() - timedelta(hours=1))
    r1 = await _refund(client, token)
    assert r1.status_code == 200, r1.text

    r2 = await _refund(client, token)
    assert r2.status_code == 400
    assert len(yookassa.calls) == 1, "второй возврат не должен уйти в ЮKassa"


# ── 6. /api/subscription заранее сообщает о недоступности, с причиной ────

async def test_subscription_endpoint_exposes_refund_eligibility(client, token, yookassa):
    await _subscriber_with_payment(client, token, paid_at=datetime.utcnow() - timedelta(days=10))
    r = await client.get("/api/subscription", headers={"Authorization": f"Bearer {token}"})
    body = r.json()["subscription"]
    assert body["refund_eligible"] is False
    assert body["refund_reason"]
    assert body["refund_amount_rub"] is None
