"""
Смена тарифа на более дорогой, с перерасчётом по неизрасходованным токенам
(`POST /api/subscription/upgrade`).

Решение владельца 31.07: даунгрейд запрещён полностью (кто хочет тариф
проще -- отменяет подписку, это отдельная уже готовая ручка), а апгрейд
стоит дешевле полной цены на долю неизрасходованного остатка ТЕКУЩЕГО
тарифа. Доля считается по токенам, а не по дням -- ровно то, что попросил
владелец: `token_balance / токены_текущего_тарифа`, не больше 100%.

ЮKassa здесь подменена (billing.charge_recurring), как и в
test_subscription_billing.py -- наружу не уходит ни один запрос. Списание
идёт сразу по сохранённой карте, без редиректа -- тот же механизм, что и
плановое автопродление.
"""

import os
from datetime import datetime, timedelta

import pytest
from sqlmodel import select

import billing
import config
import database
from database import Payment, Subscription, User

STARTER = "p1"  # «Старт», 490 ₽, 600 000 токенов
PRO = "p2"      # «Про», 990 ₽, 1 200 000 токенов


@pytest.fixture(autouse=True)
def _clean_subscriptions():
    def _wipe():
        with database.session() as s:
            for sub in s.exec(select(Subscription)).all():
                s.delete(sub)
            for p in s.exec(select(Payment)).all():
                s.delete(p)
            s.commit()

    _wipe()
    yield
    _wipe()


@pytest.fixture
def yookassa(monkeypatch):
    class _Fake:
        def __init__(self):
            self.calls = []
            self.response = {"id": "pay-upgrade-1", "status": "succeeded", "paid": True}

        async def charge_recurring(self, **kw):
            self.calls.append(kw)
            return self.response

    fake = _Fake()
    monkeypatch.setattr(billing, "is_configured", lambda: True)
    monkeypatch.setattr(billing, "charge_recurring", fake.charge_recurring)
    monkeypatch.setattr(config, "SUBSCRIPTION_ENABLED", True)
    return fake


async def _subscriber(client, token, *, balance: int, price_rub: float = 490,
                      package_id: str = STARTER, payment_method_id: str = "pm-test"):
    me = await client.get("/api/me", headers={"Authorization": f"Bearer {token}"})
    uid = me.json()["id"]
    with database.session() as s:
        u = s.get(User, uid)
        u.token_balance = balance
        s.add(u)
        sub = Subscription(
            user_id=uid, package_id=package_id, price_rub=price_rub,
            payment_method_id=payment_method_id, status="active",
            next_charge_at=datetime.utcnow() + timedelta(days=20),
        )
        s.add(sub)
        s.commit()
        s.refresh(sub)
        return uid, sub.id


async def _upgrade(client, token, package_id):
    return await client.post(
        "/api/subscription/upgrade",
        json={"package_id": package_id},
        headers={"Authorization": f"Bearer {token}"},
    )


# ── 1. Основной случай: списывается разница за вычетом неиспользованного ──

async def test_upgrade_charges_prorated_difference(client, token, yookassa):
    """Использована половина токенов «Старта» (490 ₽, 600 000) -- кредит
    245 ₽, доплата за «Про» (990 ₽) должна быть 745 ₽."""
    uid, sub_id = await _subscriber(client, token, balance=300_000)

    r = await _upgrade(client, token, PRO)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["credit_rub"] == 245
    assert body["charged_rub"] == 745

    assert len(yookassa.calls) == 1
    assert yookassa.calls[0]["amount_rub"] == 745

    with database.session() as s:
        sub = s.get(Subscription, sub_id)
        u = s.get(User, uid)
        assert sub.package_id == PRO
        assert sub.price_rub == 990, "будущие продления должны идти по полной цене нового тарифа"
        assert sub.next_charge_at > datetime.utcnow() + timedelta(days=29)
        assert u.token_balance == 300_000 + 1_200_000

        pay = s.exec(select(Payment).where(Payment.user_id == uid)).all()
        assert len(pay) == 1
        assert pay[0].rub == 745 and pay[0].status == "paid"


# ── 2. Кредит не может превышать уже уплаченную цену тарифа ───────────────

async def test_unused_balance_above_plan_tokens_caps_credit_at_full_price(client, token, yookassa):
    """Баланс больше, чем давал текущий тариф (например за счёт рефералов) --
    кредит не может быть больше уже уплаченной за тариф цены."""
    uid, sub_id = await _subscriber(client, token, balance=900_000)  # больше 600 000

    r = await _upgrade(client, token, PRO)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["credit_rub"] == 490, "кредит не может быть больше уплаченной цены тарифа"
    assert body["charged_rub"] == 500


# ── 3. Даунгрейд запрещён полностью ────────────────────────────────────────

async def test_downgrade_is_rejected(client, token, yookassa):
    uid, sub_id = await _subscriber(client, token, balance=0, price_rub=990, package_id=PRO)
    r = await _upgrade(client, token, STARTER)
    assert r.status_code == 400
    assert "отмените подписку" in r.json()["detail"].lower()
    assert yookassa.calls == []


async def test_same_tier_is_rejected(client, token, yookassa):
    """Повторная «смена» на тот же тариф -- тоже не апгрейд."""
    uid, sub_id = await _subscriber(client, token, balance=0, price_rub=490, package_id=STARTER)
    r = await _upgrade(client, token, STARTER)
    assert r.status_code == 400
    assert yookassa.calls == []


# ── 4. Без подписки или без сохранённой карты -- сменить нечем ────────────

async def test_upgrade_without_subscription_is_rejected(client, token, yookassa):
    r = await _upgrade(client, token, PRO)
    assert r.status_code == 400
    assert yookassa.calls == []


async def test_upgrade_without_saved_card_is_rejected(client, token, yookassa):
    uid, sub_id = await _subscriber(client, token, balance=0, payment_method_id="")
    r = await _upgrade(client, token, PRO)
    assert r.status_code == 400
    assert "способа оплаты" in r.json()["detail"].lower()
    assert yookassa.calls == []


async def test_upgrade_requires_subscription_enabled(client, token, monkeypatch):
    monkeypatch.setattr(config, "SUBSCRIPTION_ENABLED", False)
    uid, sub_id = await _subscriber(client, token, balance=0)
    r = await _upgrade(client, token, PRO)
    assert r.status_code == 400


# ── 5. Кредит математически не может превысить доплату ─────────────────────
#
# credit_rub <= sub.price_rub (обрезан выше), а target_pkg["rub"] строго
# больше sub.price_rub (иначе апгрейд отклонён как даунгрейд/тот же тариф) --
# значит charge_rub = target["rub"] - credit_rub всегда > 0. Проверяем это
# именно на границе (100% неиспользовано), а не выдумываем недостижимый
# сценарий нулевого платежа.

async def test_charge_is_always_positive_even_at_100_percent_unused(client, token, yookassa):
    uid, sub_id = await _subscriber(client, token, balance=10_000_000, price_rub=2490, package_id="p3")
    r = await _upgrade(client, token, "p4")  # Агентство, 4990
    assert r.status_code == 200, r.text
    assert r.json()["credit_rub"] == 2490, "кредит не может быть больше уплаченной цены тарифа"
    assert r.json()["charged_rub"] == 2500
    assert r.json()["charged_rub"] > 0
    assert len(yookassa.calls) == 1
