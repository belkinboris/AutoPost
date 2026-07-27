"""
Тесты удаления аккаунта (DELETE /api/me).

Почему именно этот эндпоинт покрыт отдельным файлом. Он падал в проде три
раза подряд, каждый раз по одной и той же причине: появлялась новая таблица
с внешним ключом на user.id, а шаг её очистки в delete_account() дописать
забывали. Пользователь видел красное «Не удалось удалить аккаунт»,
разработчик -- зелёные локальные тесты: SQLite по умолчанию не проверяет
внешние ключи, поэтому локально удаление проходило всегда.

Отсюда два обязательных условия, без которых эти тесты бессмысленны:

1. Проверка внешних ключей должна быть включена. `conftest.py` включает
   PRAGMA foreign_keys=ON для SQLite; на настоящем Postgres прогон делается
   так:

       service postgresql start
       sudo -u postgres psql -c "CREATE DATABASE autopost_test;"
       PYTEST_DATABASE_URL=postgresql://postgres:postgres@localhost:5432/autopost_test \\
           python3 -m pytest test_account_deletion.py -q

2. Проверять надо не код ответа, а факт удаления строки. У шага 7 в
   delete_account() есть fallback: при неизвестном FK он анонимизирует
   пользователя вместо удаления и всё равно возвращает {"ok": true}. То есть
   ровно тот сбой, который мы ловим, снаружи выглядит успехом. Тест, который
   смотрит только на 200, пропустил бы все три прошлые аварии.

Тест `test_no_table_with_fk_to_user_is_forgotten` -- страховка на будущее:
он сверяет список таблиц, ссылающихся на user.id, с тем, что удаление
реально подчищает, и падает при добавлении новой таблицы. Это дешевле, чем
узнавать о пропущенной таблице из прода в четвёртый раз.
"""

import os
from datetime import datetime, timedelta

from sqlmodel import select

import database
from database import (
    Channel,
    IdempotencyKey,
    Payment,
    Post,
    PostApproval,
    Referral,
    Subscription,
    TelegramIdentity,
    User,
)

BASE_URL = os.environ.get("BASE_URL", "http://localhost:8000")

_counter = 0


def _email(prefix: str) -> str:
    global _counter
    _counter += 1
    return f"{prefix}_{_counter}@del.test"


async def _register(client, email: str, **extra) -> dict:
    r = await client.post(f"{BASE_URL}/api/register", json={"email": email, "password": "test12345", **extra})
    r.raise_for_status()
    return r.json()


async def _me(client, token: str) -> dict:
    r = await client.get(f"{BASE_URL}/api/me", headers={"Authorization": f"Bearer {token}"})
    r.raise_for_status()
    return r.json()


def _fill_everything(uid: int) -> dict:
    """
    Создаёт по одной строке в каждой таблице, у которой есть внешний ключ на
    этого пользователя или на его канал. Именно такой аккаунт и ломался в
    проде -- пустой удаляется без единой проблемы и ничего не проверяет.
    """
    with database.session() as s:
        ch = Channel(user_id=uid, title="Тестовый канал", tg_chat="@test_del")
        s.add(ch)
        s.commit()
        s.refresh(ch)

        post = Post(channel_id=ch.id, user_id=uid, text="Текст тестового поста")
        s.add(post)
        s.commit()
        s.refresh(post)

        s.add(PostApproval(
            post_id=post.id, channel_id=ch.id, review_chat_id=1,
            deadline=datetime.utcnow() + timedelta(minutes=30),
        ))
        s.add(TelegramIdentity(tg_user_id=100000 + uid, user_id=uid))
        s.add(IdempotencyKey(user_id=uid, client_request_id=f"req-{uid}", channel_id=ch.id))
        s.add(Payment(user_id=uid, package_id="start", label=f"lbl-{uid}", rub=490, tokens=600000))
        s.add(Subscription(user_id=uid, package_id="start", price_rub=490, payment_method_id="pm-test"))
        s.commit()
        return {"channel_id": ch.id, "post_id": post.id}


def _rows_left(uid: int, channel_id: int) -> dict:
    """Что осталось в БД после удаления. Пусто должно быть везде."""
    with database.session() as s:
        return {
            "user": len(s.exec(select(User).where(User.id == uid)).all()),
            "channel": len(s.exec(select(Channel).where(Channel.user_id == uid)).all()),
            "post": len(s.exec(select(Post).where(Post.user_id == uid)).all()),
            "post_approval": len(s.exec(select(PostApproval).where(PostApproval.channel_id == channel_id)).all()),
            "telegram_identity": len(s.exec(select(TelegramIdentity).where(TelegramIdentity.user_id == uid)).all()),
            "idempotency_key": len(s.exec(select(IdempotencyKey).where(IdempotencyKey.user_id == uid)).all()),
            "payment": len(s.exec(select(Payment).where(Payment.user_id == uid)).all()),
            "subscription": len(s.exec(select(Subscription).where(Subscription.user_id == uid)).all()),
        }


# ── 1. Аккаунт со всеми связями удаляется полностью ───────────────────────

async def test_full_account_is_really_deleted(client):
    reg = await _register(client, _email("full"))
    token = reg["token"]
    uid = (await _me(client, token))["id"]
    created = _fill_everything(uid)

    r = await client.delete(f"{BASE_URL}/api/me", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200, f"удаление вернуло {r.status_code}: {r.text}"

    left = _rows_left(uid, created["channel_id"])
    # Проверяем строку User отдельно и первой: именно её сохраняет
    # fallback-анонимизация, из-за которой провал выглядит как успех.
    assert left["user"] == 0, (
        "строка User осталась в базе. Значит сработал fallback шага 7: "
        "какая-то таблица ссылается на user.id, а её очистку в delete_account() "
        f"не дописали. Остатки: {left}"
    )
    assert all(v == 0 for v in left.values()), f"после удаления остались строки: {left}"


# ── 2. Приглашённые пользователи остаются, но без ссылки на удалённого ─────

async def test_referred_users_survive_without_broken_link(client):
    """
    User.referred_by -- FK на user.id у ДРУГИХ пользователей. Из-за него
    удаление падало у всех, кто кого-то пригласил. Приглашённые должны
    остаться обычными пользователями, просто без пригласившего.
    """
    inviter = await _register(client, _email("inviter"))
    inviter_uid = (await _me(client, inviter["token"]))["id"]
    ref_code = (await _me(client, inviter["token"])).get("ref_code") or ""

    invited = await _register(client, _email("invited"), ref=ref_code)
    invited_uid = (await _me(client, invited["token"]))["id"]

    with database.session() as s:
        u = s.get(User, invited_uid)
        u.referred_by = inviter_uid
        s.add(u)
        s.add(Referral(referrer_id=inviter_uid, referred_id=invited_uid))
        s.commit()

    r = await client.delete(f"{BASE_URL}/api/me", headers={"Authorization": f"Bearer {inviter['token']}"})
    assert r.status_code == 200, f"удаление вернуло {r.status_code}: {r.text}"

    with database.session() as s:
        assert s.get(User, inviter_uid) is None, "пригласивший должен быть удалён физически"
        still = s.get(User, invited_uid)
        assert still is not None, "приглашённый пользователь не должен удаляться вместе с пригласившим"
        assert still.referred_by is None, "ссылка на удалённого пригласившего должна быть обнулена"


# ── 3. Подписка с привязанной картой не переживает удаление аккаунта ───────

async def test_subscription_with_saved_card_is_removed(client):
    """
    Отдельно от теста 1, потому что цена ошибки здесь не «некрасиво», а
    «списываем деньги». Subscription хранит payment_method_id -- сохранённую
    карту. Если строка переживёт удаление аккаунта, charge_due_subscriptions
    продолжит списывать с карты человека, который свой аккаунт удалил.
    """
    reg = await _register(client, _email("sub"))
    uid = (await _me(client, reg["token"]))["id"]
    with database.session() as s:
        s.add(Subscription(
            user_id=uid, package_id="start", price_rub=490,
            payment_method_id="pm-saved-card", status="active",
        ))
        s.commit()

    r = await client.delete(f"{BASE_URL}/api/me", headers={"Authorization": f"Bearer {reg['token']}"})
    assert r.status_code == 200, f"удаление вернуло {r.status_code}: {r.text}"

    with database.session() as s:
        left = s.exec(select(Subscription).where(Subscription.user_id == uid)).all()
        assert not left, (
            "подписка с сохранённой картой пережила удаление аккаунта -- "
            "автосписания продолжат уходить с карты удалённого пользователя"
        )
        assert s.get(User, uid) is None, "строка User осталась: сработал fallback шага 7"


# ── 4. Пустой аккаунт тоже удаляется ──────────────────────────────────────

async def test_empty_account_is_deleted(client):
    reg = await _register(client, _email("empty"))
    uid = (await _me(client, reg["token"]))["id"]
    r = await client.delete(f"{BASE_URL}/api/me", headers={"Authorization": f"Bearer {reg['token']}"})
    assert r.status_code == 200, f"удаление вернуло {r.status_code}: {r.text}"
    with database.session() as s:
        assert s.get(User, uid) is None


# ── 5. Ни одна таблица с FK на пользователя не забыта ─────────────────────

def test_no_table_with_fk_to_user_is_forgotten():
    """
    Страховка от повторения истории. Смотрим схему: какие таблицы ссылаются
    на user.id, channel.id или post.id -- и сверяем со списком, который
    delete_account() действительно чистит. Новая таблица без дописанной
    очистки уронит этот тест здесь, а не у пользователя в проде.

    Аналитические таблицы (LandingEvent, ProductEvent, TrafficAttribution)
    намеренно сделаны без FK, чтобы не мешать удалению аккаунта, -- поэтому
    в схеме их тут и не будет.

    Честно про ограничение: проверка грубая -- ищем имя модели в тексте
    функции. Упоминания в комментарии хватит, чтобы тест успокоился, хотя
    очистки нет. Это ловит забывчивость («добавил таблицу и не вспомнил про
    удаление»), но не ловит небрежность («написал про неё и не дописал код»).
    Настоящая проверка -- тесты 1 и 3 выше, которые смотрят на строки в БД.
    """
    import re

    referencing = set()
    for table in database.SQLModel.metadata.sorted_tables:
        for fk in table.foreign_keys:
            if fk.column.table.name in ("user", "channel", "post"):
                referencing.add(table.name)

    src = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "main.py")).read()
    body = src[src.index("def delete_account("):]
    body = body[: body.index("\n@app.")]

    # Имя модели в коде -- CamelCase, имя таблицы -- lowercase без разделителей.
    cleaned = {
        m.lower()
        for m in re.findall(r"\b([A-Z][A-Za-z]+)\b", body)
    }
    forgotten = sorted(t for t in referencing if t not in cleaned and t != "user")
    assert not forgotten, (
        f"эти таблицы ссылаются на user/channel/post, но delete_account() их не трогает: {forgotten}. "
        "Допишите очистку -- иначе удаление аккаунта упадёт на Postgres с ForeignKeyViolation."
    )
