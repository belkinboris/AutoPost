"""
Лимит каналов по тарифу и ручное начисление токенов.

Найдено владельцем 28.07: он создал три канала, не заплатив ни копейки.
Лимиты («1 канал» на бесплатном, 3 на «Про», 10 на «Бизнес») были написаны
в `config.PLANS`, показаны на экране тарифов и в оферте -- и не проверялись
нигде в коде. То есть тариф обещал ограничение, которого не существовало.

Второй файл про то же самое с другой стороны: раз каналы теперь ограничены
тарифом, владельцу нужен способ начислить токены себе и людям при сбоях,
не лазая в базу руками. Это `/api/internal/grant-tokens` за внутренним
токеном.
"""

import os

import pytest

import config
import database
from database import Channel, Payment, User

INTERNAL_HEADERS = {"Authorization": f"Bearer {os.environ['TRUEPOST_INTERNAL_API_TOKEN']}"}


def _pay(user_id: int, package_id: str, status: str = "paid") -> None:
    with database.session() as s:
        s.add(Payment(user_id=user_id, package_id=package_id, label=f"t-{user_id}-{package_id}",
                      rub=490, tokens=600_000, status=status))
        s.commit()


def _user_id_by_token(client, token: str) -> int:
    """id пользователя по его токену -- через ту же ручку, что и фронт."""
    return None  # заполняется в тестах через /api/me при необходимости


async def _create_channel(client, token, title, tg_chat):
    return await client.post("/api/channels", json={
        "title": title, "about": "тема канала", "tg_chat": tg_chat,
    }, headers={"Authorization": f"Bearer {token}"})


# ── Лимит каналов ─────────────────────────────────────────────────────────

async def test_free_user_gets_one_channel(client, token):
    """Ключевой случай: без оплаты второй канал создать нельзя."""
    r = await _create_channel(client, token, "Первый", "@limit_first")
    assert r.status_code == 200, r.text

    r = await _create_channel(client, token, "Второй", "@limit_second")
    assert r.status_code == 400
    assert "один канал" in r.json()["detail"].lower()
    assert "бесплатн" in r.json()["detail"].lower()


async def test_paid_starter_limit_message_names_the_tier(client, token):
    """Найдено владельцем 31.07: «Старт» тоже даёт 1 канал (как бесплатный),
    но сообщение об отказе называло оплаченный тариф бесплатным."""
    me = await client.get("/api/me", headers={"Authorization": f"Bearer {token}"})
    _pay(me.json()["id"], "p1")  # Старт, тоже лимит 1

    r = await _create_channel(client, token, "Первый", "@starter_first")
    assert r.status_code == 200, r.text

    r = await _create_channel(client, token, "Второй", "@starter_second")
    assert r.status_code == 400
    detail = r.json()["detail"]
    assert "бесплатн" not in detail.lower(), "оплаченный тариф назван бесплатным"
    assert "старт" in detail.lower()


async def test_paid_pro_gets_three_channels(client, token):
    """«Про» -- три канала: третий создаётся, четвёртый уже нет."""
    me = await client.get("/api/me", headers={"Authorization": f"Bearer {token}"})
    _pay(me.json()["id"], "p2")   # Про

    for i in range(3):
        r = await _create_channel(client, token, f"Канал {i}", f"@pro_ch_{i}")
        assert r.status_code == 200, f"канал {i + 1} должен создаваться: {r.text}"

    r = await _create_channel(client, token, "Четвёртый", "@pro_ch_4")
    assert r.status_code == 400
    assert "3" in r.json()["detail"]


async def test_agency_package_has_no_limit(client, token):
    """«Агентство» -- без лимита, 0 в CHANNELS_BY_PACKAGE значит «сколько угодно»."""
    me = await client.get("/api/me", headers={"Authorization": f"Bearer {token}"})
    _pay(me.json()["id"], "p4")

    for i in range(5):
        r = await _create_channel(client, token, f"Агентство {i}", f"@agency_ch_{i}")
        assert r.status_code == 200, r.text


async def test_pending_payment_does_not_raise_limit(client, token):
    """Неоплаченный платёж лимит не поднимает -- иначе достаточно было бы
    нажать «Выбрать тариф» и не платить."""
    me = await client.get("/api/me", headers={"Authorization": f"Bearer {token}"})
    _pay(me.json()["id"], "p3", status="pending")

    r = await _create_channel(client, token, "Первый", "@pending_first")
    assert r.status_code == 200
    r = await _create_channel(client, token, "Второй", "@pending_second")
    assert r.status_code == 400


async def test_existing_channels_over_limit_are_not_removed(client, token):
    """Каналы, созданные до появления проверки, остаются доступны -- ломать
    то, чем человек уже пользуется, из-за нашей недоделки нельзя."""
    me = await client.get("/api/me", headers={"Authorization": f"Bearer {token}"})
    uid = me.json()["id"]
    with database.session() as s:
        for i in range(3):
            s.add(Channel(user_id=uid, title=f"Старый {i}", about="тема",
                          tg_chat=f"@old_{uid}_{i}"))
        s.commit()

    r = await client.get("/api/channels", headers={"Authorization": f"Bearer {token}"})
    assert len(r.json()) == 3, "существующие каналы должны остаться на месте"

    r = await _create_channel(client, token, "Новый", f"@new_{uid}")
    assert r.status_code == 400, "а вот новый сверх лимита -- уже нет"


# ── Ручное начисление токенов ─────────────────────────────────────────────

# ── Название тарифа в /api/me (для шапки и карточек "Ваш тариф") ──────────

async def test_me_plan_title_is_none_for_free_user(client, token):
    r = await client.get("/api/me", headers={"Authorization": f"Bearer {token}"})
    assert r.json()["plan_title"] is None


async def test_me_plan_title_names_paid_tier(client, token):
    """Найдено владельцем 31.07: шапка и лимит каналов должны видеть один и
    тот же тариф, а не рассинхронизироваться."""
    me = await client.get("/api/me", headers={"Authorization": f"Bearer {token}"})
    _pay(me.json()["id"], "p1")  # Старт
    r = await client.get("/api/me", headers={"Authorization": f"Bearer {token}"})
    assert r.json()["plan_title"] == "Старт"


async def test_grant_tokens_adds_to_balance(client, token):
    me = await client.get("/api/me", headers={"Authorization": f"Bearer {token}"})
    email, before = me.json()["email"], me.json()["token_balance"]

    r = await client.post("/api/internal/grant-tokens",
                          json={"email": email, "tokens": 600_000, "reason": "тест"},
                          headers=INTERNAL_HEADERS)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["balance_before"] == before
    assert body["balance_after"] == before + 600_000


async def test_grant_tokens_set_mode_replaces(client, token):
    me = await client.get("/api/me", headers={"Authorization": f"Bearer {token}"})
    r = await client.post("/api/internal/grant-tokens",
                          json={"email": me.json()["email"], "tokens": 50, "mode": "set"},
                          headers=INTERNAL_HEADERS)
    assert r.json()["balance_after"] == 50


async def test_grant_tokens_rejects_extra_zero(client, token):
    """Предохранитель от опечатки: 6 млн вместо 600 тысяч."""
    me = await client.get("/api/me", headers={"Authorization": f"Bearer {token}"})
    r = await client.post("/api/internal/grant-tokens",
                          json={"email": me.json()["email"], "tokens": 99_000_000},
                          headers=INTERNAL_HEADERS)
    assert r.status_code == 400


async def test_grant_tokens_requires_internal_token(client, token):
    me = await client.get("/api/me", headers={"Authorization": f"Bearer {token}"})
    r = await client.post("/api/internal/grant-tokens",
                          json={"email": me.json()["email"], "tokens": 1000},
                          headers={"Authorization": "Bearer wrong-token"})
    assert r.status_code == 401


async def test_grant_tokens_unknown_email_is_404(client):
    r = await client.post("/api/internal/grant-tokens",
                          json={"email": "nobody@nowhere.local", "tokens": 1000},
                          headers=INTERNAL_HEADERS)
    assert r.status_code == 404
