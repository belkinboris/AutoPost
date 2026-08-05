"""
Выгрузка адресов зарегистрированных пользователей — для писем от владельца.

Запрошено владельцем 04.08: написать людям, что мы благодарны, что нас
столько-то, что переехали на projectautopost.ru, и попросить отзывы.

Отправку писем сервис НЕ делает и делать не начинает: в проекте нет ни SMTP,
ни провайдера рассылок, и заводить их ради одного письма — лишний код в
проде и лишний способ разослать что-нибудь случайно. Здесь только чтение:
эндпоинт отдаёт список адресов и числа, а рассылает владелец своим почтовым
сервисом.

ВАЖНО, ЧЕГО ЗДЕСЬ НАМЕРЕННО НЕТ И ПОЧЕМУ

1. Адресов из Телеграма. Пользователь, пришедший через бота, получает
   синтетический email вида `tg123456@telegram.local` (см. main.py, создание
   аккаунта по tg_user_id) — настоящей почты у него нет вовсе. Письмо туда
   уйдёт в никуда, а домен @telegram.local ещё и испортит репутацию отправителя
   отказами. Считаем их отдельно: до этих людей можно достучаться через бота,
   но это другое действие и другой разговор.

2. Удалённых аккаунтов. delete_account анонимизирует адрес в
   `deleted-xxxx@deleted.local` (правило 3 в CLAUDE.md). Человек ушёл — писать
   ему нельзя, и технически некуда.

3. Ничего похожего на «отправить всем». Эндпоинт только читает.

ПРО СОДЕРЖАНИЕ ПИСЬМА. Политика конфиденциальности (static/legal/privacy)
обещает пользователям «Отправка уведомлений, связанных с работой Сервиса
(не реклама)». Решение владельца 04.08: письмо остаётся сервисным — переезд
на новый домен, благодарность, просьба об отзывах. Снижение цены — это
реклама по ст. 18 ФЗ «О рекламе», для неё нужно отдельное согласие и отписка,
поэтому в письмо оно не идёт: человек увидит новую цену на сайте.
Готовый текст письма — в docs/letter_to_users.md.
"""

import os
from datetime import datetime, timezone

from fastapi import APIRouter, Header, HTTPException
from fastapi.responses import PlainTextResponse
from sqlmodel import select

import database
from database import User

router = APIRouter()

INTERNAL_API_TOKEN = os.environ.get("TRUEPOST_INTERNAL_API_TOKEN")

# Домены-заглушки: адрес есть в базе, но письма туда слать нельзя.
PLACEHOLDER_DOMAINS = ("@telegram.local", "@deleted.local")


def _check_auth(authorization: str | None) -> None:
    if not INTERNAL_API_TOKEN:
        raise HTTPException(status_code=503, detail="TRUEPOST_INTERNAL_API_TOKEN not configured on this server")
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing or invalid Authorization header")
    if authorization.removeprefix("Bearer ").strip() != INTERNAL_API_TOKEN:
        raise HTTPException(status_code=401, detail="Invalid token")


def is_real_email(email: str) -> bool:
    """Адрес, на который есть смысл отправлять письмо."""
    e = (email or "").strip().lower()
    if "@" not in e:
        return False
    return not any(e.endswith(d) for d in PLACEHOLDER_DOMAINS)


def collect_audience() -> dict:
    """
    Читает базу и раскладывает пользователей на тех, кому можно написать, и
    тех, кому нельзя. Вынесено из эндпоинта отдельной функцией, чтобы это
    можно было проверить тестом, не поднимая HTTP.
    """
    with database.session() as s:
        users = s.exec(select(User).order_by(User.created_at)).all()
        rows = [
            {"email": u.email, "created_at": u.created_at.isoformat() + "Z"}
            for u in users if is_real_email(u.email)
        ]
        telegram_only = sum(1 for u in users if (u.email or "").endswith("@telegram.local"))
        deleted = sum(1 for u in users if (u.email or "").endswith("@deleted.local"))

    return {
        "as_of": datetime.now(timezone.utc).isoformat(),
        # Число, которое владелец хочет назвать в письме («нас уже N»).
        # Именно оно, а не общее количество строк в таблице: люди без почты
        # письма не получат, а удалённые аккаунты — не люди.
        "emailable_count": len(rows),
        "telegram_only_count": telegram_only,
        "deleted_count": deleted,
        "total_rows": len(rows) + telegram_only + deleted,
        "recipients": rows,
    }


@router.get("/api/internal/audience")
async def get_audience(authorization: str | None = Header(default=None)):
    """Список адресов и числа — в JSON."""
    _check_auth(authorization)
    return collect_audience()


@router.get("/api/internal/audience.txt", response_class=PlainTextResponse)
async def get_audience_text(authorization: str | None = Header(default=None)):
    """
    То же самое одним столбцом — чтобы вставить в поле «скрытая копия» или
    импортировать в сервис рассылок без обработки.

    Предупреждение про скрытую копию в шапке файла намеренно: отправить
    сотню адресов в поле «Кому» — это показать каждому подписчику почту всех
    остальных. Такое не отменяется, а по 152-ФЗ это утечка персональных
    данных, о которой придётся уведомлять Роскомнадзор.
    """
    data = collect_audience()
    lines = [
        f"# АвтоПост — адреса для письма, выгружено {data['as_of']}",
        f"# Кому можно написать: {data['emailable_count']}",
        f"# Без почты (пришли из Телеграма, письма не получат): {data['telegram_only_count']}",
        f"# Удалённые аккаунты (пропущены): {data['deleted_count']}",
        "#",
        "# ВСТАВЛЯЙТЕ ТОЛЬКО В ПОЛЕ «СКРЫТАЯ КОПИЯ» (BCC).",
        "# В поле «Кому» каждый получатель увидит адреса всех остальных —",
        "# это утечка персональных данных, и отменить её будет нельзя.",
        "",
    ]
    lines += [r["email"] for r in data["recipients"]]
    return "\n".join(lines) + "\n"


# Подключение в main.py:
#     from internal_audience import router as audience_router
#     app.include_router(audience_router)
