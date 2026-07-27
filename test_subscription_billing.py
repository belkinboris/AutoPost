"""
Тесты автосписания по подпискам (tasks.charge_due_subscriptions).

Почему это покрыто отдельно и подробно. Здесь единственное место в сервисе,
где деньги списываются без участия человека. Ошибка стоит не «некрасиво», а
«списали дважды» или «списали с того, кто отписался». При этом проверить
защиту на живых деньгах невозможно, а сейчас она вообще ни разу не
выполнялась: `SUBSCRIPTION_ENABLED=false`, рекуррент на согласовании с
ЮKassa. То есть в день включения автоплатежей весь этот код заработает
впервые сразу на боевых картах.

ЮKassa здесь подменена: `billing.charge_recurring` и `billing.is_configured`
перехватываются, наружу не уходит ни один запрос. Проверяется наша логика --
идемпотентность, начисление токенов, обработка неизвестного исхода, -- а не
поведение платёжного провайдера.

Отдельно проверяется различие «отказ» и «мы не узнали исход»: при обрыве
связи деньги могли уйти, и помечать такой платёж failed нельзя.
"""

import os
from datetime import datetime, timedelta

import pytest
from sqlmodel import select

import billing
import config
import database
import tasks
from database import Payment, Subscription, User

PKG = "p1"  # «Старт», 490 ₽, 600 000 токенов
FIRST_PERIOD = 1  # Subscription.period_no по умолчанию


@pytest.fixture(autouse=True)
def _clean_subscriptions():
    """
    Джоба обходит ВСЕ подписки, у которых наступила дата продления, а не
    только созданную тестом. Без очистки соседний тест ловил бы чужие
    списания и «лишние» обращения в ЮKassa.
    """
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


def _make_subscriber(price_rub: float = 490, **sub_kw) -> tuple[int, int]:
    """Пользователь с подпиской, у которой уже наступила дата продления."""
    with database.session() as s:
        u = User(email=f"sub_{datetime.utcnow().timestamp()}_{len(sub_kw)}@bill.test",
                 password_hash="x", token_balance=0)
        s.add(u)
        s.commit()
        s.refresh(u)
        defaults = dict(
            user_id=u.id, package_id=PKG, price_rub=price_rub,
            payment_method_id="pm-test", status="active",
            next_charge_at=datetime.utcnow() - timedelta(minutes=1),
        )
        defaults.update(sub_kw)
        sub = Subscription(**defaults)
        s.add(sub)
        s.commit()
        s.refresh(sub)
        return u.id, sub.id


def _state(uid: int, sub_id: int) -> dict:
    with database.session() as s:
        u = s.get(User, uid)
        sub = s.get(Subscription, sub_id)
        pays = s.exec(select(Payment).where(Payment.user_id == uid)).all()
        return {
            "balance": u.token_balance if u else None,
            "status": sub.status,
            "period_no": sub.period_no,
            "fail_count": sub.fail_count,
            "last_period_key": sub.last_period_key,
            "next_charge_at": sub.next_charge_at,
            "payments": [(p.label, p.status, p.rub) for p in pays],
        }


@pytest.fixture
def yookassa(monkeypatch):
    """
    Подменяет ЮKassa. Возвращает объект, у которого можно задать ответ и
    посмотреть, с какими аргументами и сколько раз её позвали.
    """
    class _Fake:
        def __init__(self):
            self.calls = []
            self.response = {"id": "pay-1", "status": "succeeded", "paid": True}
            self.raises = None

        async def charge_recurring(self, **kw):
            self.calls.append(kw)
            if self.raises:
                raise self.raises
            return self.response

    fake = _Fake()
    monkeypatch.setattr(billing, "is_configured", lambda: True)
    monkeypatch.setattr(billing, "charge_recurring", fake.charge_recurring)
    monkeypatch.setattr(config, "SUBSCRIPTION_ENABLED", True)
    return fake


# ── 1. Успешное продление начисляет токены ровно один раз ─────────────────

async def test_successful_charge_credits_tokens_once(yookassa):
    uid, sub_id = _make_subscriber()
    await tasks.charge_due_subscriptions()

    st = _state(uid, sub_id)
    assert len(yookassa.calls) == 1, "ЮKassa должна быть вызвана ровно один раз"
    assert st["balance"] == 600_000, f"токены начислены неверно: {st['balance']}"
    assert st["period_no"] == FIRST_PERIOD + 1, "номер периода должен сдвинуться"
    assert st["last_period_key"] == f"sub-{sub_id}-period-{FIRST_PERIOD}"
    assert st["payments"] == [(f"sub-{sub_id}-period-{FIRST_PERIOD}", "paid", 490)]
    assert st["next_charge_at"] > datetime.utcnow() + timedelta(days=29)


# ── 2. Повторный запуск джобы не списывает и не начисляет второй раз ──────

async def test_second_run_does_not_charge_twice(yookassa):
    """
    Самый дорогой сценарий: джоба запустилась дважды (наложение по времени,
    перезапуск процесса). Второй раз ни денег, ни токенов уйти не должно.
    """
    uid, sub_id = _make_subscriber()
    await tasks.charge_due_subscriptions()
    balance_after_first = _state(uid, sub_id)["balance"]

    # Принудительно возвращаем дату продления в прошлое -- имитируем повторный
    # запуск на том же периоде, как если бы дата не успела записаться.
    with database.session() as s:
        sub = s.get(Subscription, sub_id)
        sub.next_charge_at = datetime.utcnow() - timedelta(minutes=1)
        sub.period_no -= 1  # тот же период, что уже оплачен
        s.add(sub); s.commit()

    await tasks.charge_due_subscriptions()
    st = _state(uid, sub_id)
    assert st["balance"] == balance_after_first, "токены начислены повторно за тот же период"
    assert len(yookassa.calls) == 1, "второе обращение в ЮKassa за тот же период"


# ── 3. Оплаченный Payment за период останавливает повторное начисление ────

async def test_paid_payment_blocks_recharge_after_crash(yookassa):
    """
    Процесс упал ПОСЛЕ успешного списания, но ДО записи last_period_key.
    По Idempotence-Key ЮKassa вернула бы тот же платёж (деньги не удвоятся),
    но токены мы начислили бы второй раз -- если бы не проверка paid-Payment.
    """
    uid, sub_id = _make_subscriber()
    with database.session() as s:
        s.add(Payment(
            user_id=uid, package_id=PKG, label=f"sub-{sub_id}-period-{FIRST_PERIOD}",
            rub=490, tokens=600_000, status="paid", paid_at=datetime.utcnow(),
        ))
        s.commit()

    await tasks.charge_due_subscriptions()
    st = _state(uid, sub_id)
    assert not yookassa.calls, "нельзя обращаться в ЮKassa за уже оплаченный период"
    assert st["balance"] == 0, "токены за уже оплаченный период начислены повторно"
    assert st["last_period_key"] == f"sub-{sub_id}-period-{FIRST_PERIOD}"
    assert st["next_charge_at"] > datetime.utcnow() + timedelta(days=29), \
        "дата продления должна сдвинуться, иначе джоба будет крутиться на этом периоде"


# ── 4. Неизвестный исход не помечается отказом ────────────────────────────

async def test_unknown_outcome_keeps_payment_pending(yookassa):
    """
    Обрыв связи: мы не знаем, ушли деньги или нет. Пометить платёж failed --
    значит соврать в собственной отчётности и, возможно, списать второй раз
    при следующей попытке уже другим ключом.
    """
    uid, sub_id = _make_subscriber()
    yookassa.raises = RuntimeError("таймаут соединения")

    await tasks.charge_due_subscriptions()
    st = _state(uid, sub_id)
    labels = dict((lbl, status) for lbl, status, _ in st["payments"])
    assert labels[f"sub-{sub_id}-period-{FIRST_PERIOD}"] == "pending", \
        f"при неизвестном исходе платёж не должен становиться failed: {st['payments']}"
    assert st["balance"] == 0, "токены при неизвестном исходе начислять нельзя"
    assert st["fail_count"] == 1


# ── 5. Явный отказ помечается failed и уходит в ретрай ────────────────────

async def test_explicit_refusal_marks_failed_and_retries(yookassa):
    uid, sub_id = _make_subscriber()
    yookassa.response = {"id": "pay-x", "status": "canceled", "paid": False}

    await tasks.charge_due_subscriptions()
    st = _state(uid, sub_id)
    assert st["payments"][0][1] == "failed", f"явный отказ должен быть failed: {st['payments']}"
    assert st["status"] == "active", "одна неудача не должна приостанавливать подписку"
    assert st["fail_count"] == 1
    assert st["next_charge_at"] < datetime.utcnow() + timedelta(hours=25), \
        "следующая попытка должна быть через часы, а не через месяц"


# ── 6. После SUBSCRIPTION_MAX_FAILS подписка приостанавливается ───────────

async def test_suspended_after_max_fails(yookassa):
    uid, sub_id = _make_subscriber(fail_count=config.SUBSCRIPTION_MAX_FAILS - 1)
    yookassa.response = {"id": "pay-x", "status": "canceled", "paid": False}

    await tasks.charge_due_subscriptions()
    st = _state(uid, sub_id)
    assert st["status"] == "suspended", f"после {config.SUBSCRIPTION_MAX_FAILS} неудач ожидается suspended"
    assert st["balance"] == 0


# ── 7. Списываем зафиксированную цену, а не текущую из конфига ────────────

async def test_charges_locked_price_not_current(yookassa, monkeypatch):
    """
    И оферта, и плашка на тарифах обещают подписчику сохранение цены
    оформления. Если брать текущую цену из конфига, повышение тарифов молча
    подняло бы списания уже подписанным -- то есть мы нарушили бы договор.
    """
    uid, sub_id = _make_subscriber(price_rub=390)  # оформился по акции
    raised = [dict(p, rub=1990) if p["id"] == PKG else p for p in config.TOKEN_PACKAGES]
    monkeypatch.setattr(config, "TOKEN_PACKAGES", raised)

    await tasks.charge_due_subscriptions()
    assert yookassa.calls[0]["amount_rub"] == 390, \
        f"списали не зафиксированную цену: {yookassa.calls[0]['amount_rub']}"


# ── 8. Идемпотентный ключ детерминирован и привязан к периоду ─────────────

async def test_idempotence_key_is_deterministic(yookassa):
    uid, sub_id = _make_subscriber()
    await tasks.charge_due_subscriptions()
    assert yookassa.calls[0]["idempotence_key"] == f"sub-{sub_id}-period-{FIRST_PERIOD}", (
        "ключ должен зависеть только от подписки и номера периода: на нём держится "
        "защита от повторного списания при перезапуске"
    )


# ── 9. Подписка без сохранённой карты не списывается, а приостанавливается ─

async def test_no_payment_method_suspends_instead_of_charging(yookassa):
    uid, sub_id = _make_subscriber(payment_method_id="")
    await tasks.charge_due_subscriptions()
    st = _state(uid, sub_id)
    assert not yookassa.calls
    assert st["status"] == "suspended"
    assert st["payments"] == [], "не должно появляться платежей, которые никто не пытался провести"


# ── 10. При выключенном флаге не происходит вообще ничего ─────────────────

async def test_disabled_flag_stops_everything(yookassa, monkeypatch):
    """
    Сейчас в проде именно так: SUBSCRIPTION_ENABLED=false, рекуррент не
    согласован. Джоба висит в расписании и обязана быть безвредной.
    """
    monkeypatch.setattr(config, "SUBSCRIPTION_ENABLED", False)
    uid, sub_id = _make_subscriber()
    await tasks.charge_due_subscriptions()
    st = _state(uid, sub_id)
    assert not yookassa.calls
    assert st["balance"] == 0
    assert st["period_no"] == FIRST_PERIOD
    assert st["payments"] == []
