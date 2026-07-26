"""
GET /api/internal/token-economics -- сколько на самом деле стоит один пост.

Отвечает на вопрос, который иначе можно только гадать: реальный расход
токенов на пост считается не по оценке "20-40 тысяч", а по фактическим
Post.tokens_used, которые Сервис записывает при каждой генерации.

Что делает эндпоинт:
  1. Берёт реальный расход по постам за период.
  2. Считает распределение (среднее, медиана, перцентили, максимум) -- среднее
     само по себе врёт, если есть редкие очень дорогие посты.
  3. Если передан ?price_per_million=<руб>, сразу пересчитывает это в
     себестоимость поста и маржинальность каждого тарифа.

Цену за миллион токенов подставляет пользователь: она зависит от модели и
тарифа Yandex Cloud, в коде её нет и захардкодить её было бы враньём.

Подключение в main.py (рядом с остальными internal-роутерами):

    from internal_token_economics import router as token_economics_router
    app.include_router(token_economics_router)

Тот же токен, что и у остальных internal-эндпоинтов:
    TRUEPOST_INTERNAL_API_TOKEN (Authorization: Bearer {token})
"""

import os
from datetime import datetime, timedelta

from fastapi import APIRouter, Header, HTTPException
from sqlmodel import select

import config
import database
from database import Post

router = APIRouter()

INTERNAL_API_TOKEN = os.environ.get("TRUEPOST_INTERNAL_API_TOKEN")


def _check_auth(authorization: str | None) -> None:
    if not INTERNAL_API_TOKEN:
        raise HTTPException(status_code=503, detail="TRUEPOST_INTERNAL_API_TOKEN not configured on this server")
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing or invalid Authorization header")
    token = authorization.removeprefix("Bearer ").strip()
    if token != INTERNAL_API_TOKEN:
        raise HTTPException(status_code=401, detail="Invalid token")


def _percentile(sorted_values: list[int], p: float) -> int:
    """Перцентиль по уже отсортированному списку (без numpy)."""
    if not sorted_values:
        return 0
    if len(sorted_values) == 1:
        return sorted_values[0]
    idx = (len(sorted_values) - 1) * p
    lo, hi = int(idx), min(int(idx) + 1, len(sorted_values) - 1)
    frac = idx - lo
    return int(round(sorted_values[lo] * (1 - frac) + sorted_values[hi] * frac))


@router.get("/api/internal/token-economics")
def token_economics(
    period_days: int = 30,
    price_per_million: float | None = None,
    authorization: str | None = Header(default=None),
):
    """
    Реальный расход токенов на пост и, если передана цена, экономика тарифов.

    period_days       -- за какой период смотреть посты (по умолчанию 30 дней)
    price_per_million -- цена в рублях за 1 000 000 токенов у провайдера.
                         Смотреть в Yandex Cloud: Биллинг -> Детализация, либо
                         на странице цен нужной модели. Если не передана,
                         эндпоинт вернёт только расход, без денег.
    """
    _check_auth(authorization)

    since = datetime.utcnow() - timedelta(days=period_days)
    with database.session() as s:
        rows = s.exec(
            select(Post.tokens_used).where(
                Post.created_at >= since,
                Post.tokens_used != None,  # noqa: E711
                Post.tokens_used > 0,
            )
        ).all()

    values = sorted(int(v) for v in rows if v)
    n = len(values)

    if not n:
        return {
            "period_days": period_days,
            "posts_measured": 0,
            "note": (
                "За период нет ни одного поста с записанным расходом токенов. "
                "Либо постов ещё не генерировали, либо период слишком короткий."
            ),
        }

    total = sum(values)
    avg = total / n
    result = {
        "period_days": period_days,
        "posts_measured": n,
        "tokens_per_post": {
            "avg": int(round(avg)),
            "median": _percentile(values, 0.5),
            "p90": _percentile(values, 0.9),
            "p99": _percentile(values, 0.99),
            "min": values[0],
            "max": values[-1],
        },
        "tokens_total": total,
        # Оценка, зашитая в интерфейсе и оферте -- проверяем, не разошлась ли
        # она с реальностью. Если разошлась, это надо чинить в текстах.
        "declared_estimate": {
            "min": config.POST_TOKENS_MIN,
            "max": config.POST_TOKENS_MAX,
            "avg_within_declared_range": config.POST_TOKENS_MIN <= avg <= config.POST_TOKENS_MAX,
        },
    }

    if price_per_million is None:
        result["note"] = (
            "Передайте ?price_per_million=<рублей за 1 млн токенов>, чтобы получить "
            "себестоимость поста и маржинальность тарифов. Цену смотрите в Yandex Cloud: "
            "Биллинг -> Детализация за прошлый месяц (сумма / израсходованные токены), "
            "либо на странице цен вашей модели."
        )
        return result

    cost_per_post = avg * price_per_million / 1_000_000
    result["price_per_million_rub"] = price_per_million
    result["cost_per_post_rub"] = round(cost_per_post, 4)

    plans = []
    for pkg in config.TOKEN_PACKAGES:
        tokens = pkg["tokens"]
        rub = pkg["rub"]
        # Худший случай: пользователь израсходовал весь пакет токенов.
        max_cost = tokens * price_per_million / 1_000_000
        posts_possible = tokens / avg if avg else 0
        plans.append({
            "id": pkg["id"],
            "title": pkg["title"],
            "price_rub": rub,
            "tokens": tokens,
            "posts_at_real_avg": int(posts_possible),
            "cost_if_fully_used_rub": round(max_cost, 2),
            "margin_if_fully_used_rub": round(rub - max_cost, 2),
            "margin_pct_if_fully_used": round((rub - max_cost) / rub * 100, 1) if rub else None,
            "profitable_if_fully_used": max_cost < rub,
            # Сколько постов можно отдать, прежде чем тариф уйдёт в минус.
            "breakeven_posts": int(rub / cost_per_post) if cost_per_post > 0 else None,
        })
    result["plans"] = plans
    result["verdict"] = (
        "Все тарифы прибыльны даже при полной выработке токенов."
        if all(p["profitable_if_fully_used"] for p in plans)
        else "ВНИМАНИЕ: часть тарифов убыточна при полной выработке токенов -- см. plans."
    )
    return result
