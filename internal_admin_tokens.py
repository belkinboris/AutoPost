"""
Ручное начисление токенов владельцем сервиса.

Зачем отдельная ручка, а не правка в базе руками: у владельца нет постоянного
доступа к консоли Postgres на Timeweb, а начислять токены нужно регулярно --
себе для тестирования, и людям, у которых что-то пошло не так (сбой генерации,
возврат, компенсация). Правка `UPDATE user SET token_balance = ...` вслепую
через панель хостинга -- ровно тот случай, когда легко промахнуться строкой.

Защищена тем же `TRUEPOST_INTERNAL_API_TOKEN`, что и остальная внутренняя
диагностика (`internal_*.py`).

Начисление ПРИБАВЛЯЕТ к текущему балансу, а не заменяет его: заменяющая
семантика опаснее (случайно обнулить оплаченный баланс), и это не то, что
имеют в виду словами «докинуть токенов». Для точной установки значения есть
отдельный параметр `mode: "set"` -- но по умолчанию всегда прибавление.

Использование:

    curl -X POST https://projectautopost.ru/api/internal/grant-tokens \\
      -H "Authorization: Bearer $TRUEPOST_INTERNAL_API_TOKEN" \\
      -H "Content-Type: application/json" \\
      -d '{"email": "1@1.1", "tokens": 600000}'

Посмотреть баланс, ничего не меняя:

    curl "https://projectautopost.ru/api/internal/user-balance?email=1@1.1" \\
      -H "Authorization: Bearer $TRUEPOST_INTERNAL_API_TOKEN"
"""

import logging
import os

from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel
from sqlmodel import select

import tasks
from database import session, User

logger = logging.getLogger("autopost")

router = APIRouter()

INTERNAL_API_TOKEN = os.environ.get("TRUEPOST_INTERNAL_API_TOKEN")

# Верхняя граница на одно начисление. Не про безопасность (ручка и так за
# токеном), а про опечатку: лишний ноль в 600000 превращается в 6 миллионов,
# и это тихо разъедется с любыми расчётами себестоимости.
MAX_GRANT = 10_000_000


def _check_auth(authorization: str | None) -> None:
    if not INTERNAL_API_TOKEN:
        raise HTTPException(status_code=503,
                            detail="TRUEPOST_INTERNAL_API_TOKEN not configured on this server")
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing or invalid Authorization header")
    if authorization.removeprefix("Bearer ").strip() != INTERNAL_API_TOKEN:
        raise HTTPException(status_code=401, detail="Invalid token")


class GrantTokensIn(BaseModel):
    email: str
    tokens: int
    mode: str = "add"          # "add" -- прибавить (по умолчанию), "set" -- установить
    reason: str = ""           # свободный комментарий, попадает в лог


@router.get("/api/internal/user-balance")
def user_balance(email: str, authorization: str | None = Header(default=None)):
    """Баланс пользователя, ничего не меняя -- чтобы сверить до и после."""
    _check_auth(authorization)
    with session() as s:
        user = s.exec(select(User).where(User.email == email)).first()
        if not user:
            raise HTTPException(status_code=404, detail=f"Пользователь {email} не найден")
        return {
            "email": user.email,
            "user_id": user.id,
            "token_balance": user.token_balance,
            # Тот же расчёт, что показывает экран «Тарифы» -- чтобы цифры
            # в панели и в ответе ручки не расходились.
            "posts_estimate": f"{user.token_balance // 40000}–{user.token_balance // 20000}",
        }


@router.post("/api/internal/grant-tokens")
async def grant_tokens(data: GrantTokensIn, authorization: str | None = Header(default=None)):
    _check_auth(authorization)

    if data.mode not in ("add", "set"):
        raise HTTPException(status_code=400, detail="mode должен быть 'add' или 'set'")
    if data.tokens < 0:
        raise HTTPException(status_code=400, detail="tokens не может быть отрицательным")
    if data.tokens > MAX_GRANT:
        raise HTTPException(
            status_code=400,
            detail=f"tokens больше предохранителя {MAX_GRANT} -- проверьте, не лишний ли ноль",
        )

    with session() as s:
        user = s.exec(select(User).where(User.email == data.email)).first()
        if not user:
            raise HTTPException(status_code=404, detail=f"Пользователь {data.email} не найден")

        before = user.token_balance
        user.token_balance = (before + data.tokens) if data.mode == "add" else data.tokens
        after = user.token_balance
        # id читаем ДО выхода из сессии: после commit() объект отсоединяется,
        # и обращение к его полям падает DetachedInstanceError. Поймано тестом
        # до первого реального вызова.
        user_id = user.id
        s.add(user)
        s.commit()

    # Начисление мимо оплаты -- это ручное вмешательство в деньги, и след
    # о нём должен остаться: иначе расхождение баланса с историей платежей
    # потом невозможно объяснить.
    logger.info(
        "[grant-tokens] email=%s user_id=%s mode=%s delta=%s balance %s -> %s reason=«%s»",
        data.email, user_id, data.mode, data.tokens, before, after, data.reason,
    )

    if after > before:
        # См. tasks.resume_starved_channels -- то же самое "не ждать тика
        # после пополнения", что и для обычной оплаты и апгрейда тарифа.
        try:
            await tasks.resume_starved_channels(user_id)
        except Exception as e:
            logger.warning(f"resume_starved_channels после grant-tokens: {e}")

    return {
        "ok": True,
        "email": data.email,
        "user_id": user_id,
        "mode": data.mode,
        "balance_before": before,
        "balance_after": after,
        "posts_estimate": f"{after // 40000}–{after // 20000}",
    }
