"""
Выгрузка адресов для письма от владельца (internal_audience.py).

Цена ошибки здесь выше обычной: письмо уходит живым людям и не отменяется.
Поэтому проверяем не «эндпоинт что-то отдал», а ровно те три вещи, которые
могут испортить рассылку:

1. В список не попал тот, кому писать нельзя (удалённый аккаунт) или некуда
   (пришёл из Телеграма, настоящей почты нет).
2. Число, которое владелец назовёт в письме («нас уже N»), — это число живых
   адресатов, а не строк в таблице.
3. Выгрузка закрыта токеном и ничего не отправляет сама.
"""

import os

import pytest

import database
import internal_audience
from database import User


def _user(email: str):
    with database.session() as s:
        u = User(email=email, password_hash="x")
        s.add(u); s.commit()


def test_placeholder_addresses_are_not_real():
    """Заглушки распознаются по домену. Адрес из Телеграма выглядит как почта
    (`tg123@telegram.local`) и в наивной выгрузке прошёл бы как настоящий."""
    assert internal_audience.is_real_email("human@example.com")
    assert not internal_audience.is_real_email("tg123456@telegram.local")
    assert not internal_audience.is_real_email("deleted-abc123@deleted.local")
    assert not internal_audience.is_real_email("")
    assert not internal_audience.is_real_email("без-собаки")


def test_export_skips_telegram_and_deleted_accounts():
    """Главный тест файла. Письмо на @telegram.local уйдёт в никуда, а отказы
    по несуществующему домену бьют по репутации отправителя — следующие
    письма поедут в спам уже всем. Писать удалённому аккаунту нельзя тем
    более: человек ушёл."""
    _user("живой1@example.com")
    _user("живой2@example.com")
    _user("tg777001@telegram.local")
    _user("deleted-zzz999@deleted.local")

    data = internal_audience.collect_audience()
    addresses = {r["email"] for r in data["recipients"]}

    assert "живой1@example.com" in addresses
    assert "живой2@example.com" in addresses
    assert not [a for a in addresses if a.endswith(("@telegram.local", "@deleted.local"))], (
        f"в рассылку попали адреса-заглушки: {addresses}"
    )


def test_counts_separate_the_reachable_from_the_rest():
    """Число в письме («нас уже N») должно быть числом живых адресатов.
    Взять общее количество строк — значит написать людям цифру больше
    настоящей, то есть соврать им в первом же абзаце."""
    before = internal_audience.collect_audience()
    _user("счёт1@example.com")
    _user("tg888002@telegram.local")
    _user("deleted-www888@deleted.local")
    after = internal_audience.collect_audience()

    assert after["emailable_count"] == before["emailable_count"] + 1
    assert after["telegram_only_count"] == before["telegram_only_count"] + 1
    assert after["deleted_count"] == before["deleted_count"] + 1
    assert after["emailable_count"] == len(after["recipients"])
    assert after["total_rows"] > after["emailable_count"], (
        "тест бессмысленен, если в базе нет ни одного недоступного адреса"
    )


async def test_export_requires_the_internal_token(client):
    """Список почт — персональные данные. Открытым он быть не может."""
    r = await client.get("/api/internal/audience")
    assert r.status_code in (401, 503), r.text
    # Токен латиницей: HTTP-заголовки кириллицу не принимают, и с русским
    # словом тест падал бы на кодировке, а не на проверке доступа.
    r = await client.get("/api/internal/audience",
                         headers={"Authorization": "Bearer wrong-token"})
    assert r.status_code in (401, 503), r.text


async def test_export_returns_addresses_with_the_right_token(client):
    _user("токен@example.com")
    token = os.environ.get("TRUEPOST_INTERNAL_API_TOKEN", "test-token")
    r = await client.get("/api/internal/audience",
                         headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert "emailable_count" in body and "recipients" in body
    assert "токен@example.com" in {x["email"] for x in body["recipients"]}


async def test_text_export_warns_about_bcc(client):
    """Отправить сотню адресов в поле «Кому» — показать каждому подписчику
    почту всех остальных. Это не отменяется, и по 152-ФЗ это утечка. Файл,
    который человек копирует, обязан предупреждать об этом сам."""
    _user("копия@example.com")
    token = os.environ.get("TRUEPOST_INTERNAL_API_TOKEN", "test-token")
    r = await client.get("/api/internal/audience.txt",
                         headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200, r.text
    assert "СКРЫТАЯ КОПИЯ" in r.text, "предупреждение про BCC пропало из выгрузки"
    assert "копия@example.com" in r.text


def test_module_cannot_send_anything():
    """Страховка от того, что кто-то (включая меня в будущем) добавит сюда
    отправку. Выгрузка обязана оставаться только чтением: рассылку запускает
    человек своим почтовым сервисом, а не эндпоинт, до которого можно
    случайно достучаться."""
    from pathlib import Path
    src = Path(internal_audience.__file__).read_text()
    code = "\n".join(
        line for line in src.splitlines()
        if not line.strip().startswith("#")
    )
    for forbidden in ("smtplib", "sendmail", "send_message", "requests.post", "httpx.post"):
        assert forbidden not in code, (
            f"в модуль выгрузки просочилась отправка ({forbidden}) -- рассылать "
            f"должен человек, а не сервис"
        )
