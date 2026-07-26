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
from database import Post, Channel

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
    billing_rub: float | None = None,
    billing_tokens: int | None = None,
    authorization: str | None = Header(default=None),
):
    """
    Реальный расход токенов на пост и, если передана цена, экономика тарифов.

    period_days       -- за какой период смотреть посты (по умолчанию 30 дней)
    price_per_million -- цена в рублях за 1 000 000 токенов у провайдера.
                         Смотреть в Yandex Cloud: Биллинг -> Детализация, либо
                         на странице цен нужной модели. Если не передана,
                         эндпоинт вернёт только расход, без денег.
    billing_rub       -- сколько РЕАЛЬНО списал Yandex Cloud за тот же период.
                         Это источник истины: он включает и те вызовы, которые
                         не привязаны к постам (см. not_counted_here).
    billing_tokens    -- сколько токенов показывает биллинг за период, если
                         эта цифра там есть. Вместе с суммой даёт настоящую
                         цену за миллион, без догадок.
    """
    _check_auth(authorization)

    since = datetime.utcnow() - timedelta(days=period_days)
    with database.session() as s:
        all_rows = s.exec(
            select(Post.tokens_used).where(Post.created_at >= since)
        ).all()
        # Разбивка «с поиском / без». Признак берём с канала (Channel.
        # use_web_search), потому что у поста своего флага нет -- поиск
        # включается настройкой канала на момент генерации. Если настройку
        # позже переключили, старые посты попадут не в ту группу, поэтому
        # цифры показательны на дистанции, а не на единичных постах.
        search_rows = s.exec(
            select(Channel.use_web_search, Post.tokens_used)
            .join(Channel, Channel.id == Post.channel_id)
            .where(Post.created_at >= since)
        ).all()

    # Посты с нулевым/отсутствующим расходом исключаем из статистики, но
    # обязательно показываем их количество: если таких много, среднее по
    # остальным перестаёт описывать реальность (значит, провайдер не вернул
    # usage, и часть расхода вообще не учтена).
    zero_or_missing = sum(1 for v in all_rows if not v)
    values = sorted(int(v) for v in all_rows if v)
    n = len(values)

    if not n:
        return {
            "period_days": period_days,
            "posts_measured": 0,
            "posts_without_token_data": zero_or_missing,
            "note": (
                "За период нет ни одного поста с записанным расходом токенов. "
                "Либо постов ещё не генерировали, либо период слишком короткий."
            ),
        }

    # ── Стоимость поиска в интернете ──────────────────────────────────
    # Отвечает на вопрос «во сколько обходится поиск»: он и раздувает
    # входящие токены (выдача подкладывается в промпт), и стоит отдельных
    # денег как запрос к Search API.
    def _stats(vals):
        vals = sorted(v for v in vals if v)
        if not vals:
            return None
        return {
            "posts": len(vals),
            "avg_tokens": int(round(sum(vals) / len(vals))),
            "median_tokens": _percentile(vals, 0.5),
            "p90_tokens": _percentile(vals, 0.9),
        }

    with_search = _stats([t for ws, t in search_rows if ws])
    without_search = _stats([t for ws, t in search_rows if not ws])
    web_search = {"with_search": with_search, "without_search": without_search}
    if with_search and without_search:
        diff = with_search["avg_tokens"] - without_search["avg_tokens"]
        web_search["extra_tokens_per_post"] = diff
        web_search["extra_pct"] = round(diff / without_search["avg_tokens"] * 100, 1)
    else:
        web_search["note"] = (
            "Для сравнения нужны посты обеих групп: и с включённым поиском, и с "
            "выключенным. Сейчас есть только одна группа."
        )
    # Сам запрос к Search API токенами не оплачивается -- он тарифицируется
    # отдельно, поштучно. Цену берём из биллинга Yandex Cloud (SKU «Дневные/
    # Ночные синхронные текстовые запросы»), в токенах её не видно вовсе.
    web_search["search_api_note"] = (
        "Запросы к Search API тарифицируются отдельно от токенов: ~0.49 ₽ за запрос днём "
        "и ~0.37 ₽ ночью (488 и 366 ₽ за 1000). На пост приходится один запрос. "
        "Эти деньги НЕ входят в цифры выше -- их видно только в биллинге Yandex Cloud, "
        "строка «Yandex AI Studio. Search API»."
    )

    total = sum(values)
    avg = total / n
    result = {
        "period_days": period_days,
        "posts_measured": n,
        "posts_without_token_data": zero_or_missing,
        # ЧЕГО ЭТИ ЦИФРЫ НЕ ВИДЯТ. Post.tokens_used -- нижняя граница расхода,
        # а не полный счёт. Не попадают сюда:
        #   - classify_topic() -- вызывается перед КАЖДОЙ генерацией и на
        #     каждой проверке темы в онбординге, результат учёта отбрасывается;
        #   - check_news_available() для новостных каналов, когда новости
        #     найдены и генерация продолжилась;
        #   - analyze_style() -- списывается с баланса, но поста не создаёт;
        #   - consult() -- ИИ-консультант вообще не считает токены.
        # Поэтому источник истины по деньгам -- биллинг Yandex Cloud, а эти
        # цифры нужны для распределения расхода по постам и для коэффициента
        # расхождения (см. billing_reconciliation).
        "not_counted_here": [
            "classify_topic (перед каждой генерацией и при проверке темы)",
            "check_news_available (новостные каналы, когда новости найдены)",
            "analyze_style (анализ чужого канала)",
            "consult (ИИ-консультант)",
        ],
        "tokens_per_post": {
            "avg": int(round(avg)),
            "median": _percentile(values, 0.5),
            "p90": _percentile(values, 0.9),
            "p99": _percentile(values, 0.99),
            "min": values[0],
            "max": values[-1],
        },
        "tokens_total": total,
        "web_search": web_search,
        # Оценка, зашитая в интерфейсе и оферте -- проверяем, не разошлась ли
        # она с реальностью. Если разошлась, это надо чинить в текстах.
        "declared_estimate": {
            "min": config.POST_TOKENS_MIN,
            "max": config.POST_TOKENS_MAX,
            "avg_within_declared_range": config.POST_TOKENS_MIN <= avg <= config.POST_TOKENS_MAX,
        },
    }

    # Сверка с биллингом. Считаем настоящую цену за миллион и коэффициент,
    # показывающий, во сколько раз реальный расход больше учтённого в постах.
    if billing_rub is not None and billing_tokens:
        real_price = billing_rub / billing_tokens * 1_000_000
        result["billing_reconciliation"] = {
            "billing_rub": billing_rub,
            "billing_tokens": billing_tokens,
            "real_price_per_million_rub": round(real_price, 2),
            "tokens_counted_in_posts": total,
            "uncounted_ratio": round(billing_tokens / total, 2) if total else None,
            "comment": (
                "uncounted_ratio -- во сколько раз реальный расход больше того, что "
                "записано в постах. 1.0 значит, что учтено всё; 1.3 -- что треть расхода "
                "идёт мимо постов (классификация тем, анализ стиля, консультант). "
                "На этот коэффициент нужно умножать себестоимость поста."
            ),
        }
        if price_per_million is None:
            price_per_million = real_price
            result["price_source"] = "рассчитана из биллинга"
    elif price_per_million is not None:
        result["price_source"] = "передана вручную"

    if price_per_million is None:
        result["note"] = (
            "Чтобы получить деньги, передайте ЛИБО ?price_per_million=<рублей за 1 млн>, "
            "ЛИБО (точнее) ?billing_rub=<сумма из биллинга>&billing_tokens=<токены из биллинга> "
            "за тот же период -- тогда цена посчитается из фактического счёта, а заодно "
            "станет виден коэффициент неучтённого расхода."
        )
        return result

    # Расход, который не привязан к постам (классификация тем, анализ стиля,
    # консультант), реального счёта не отменяет -- он просто не списывается с
    # баланса пользователя. Поэтому себестоимость домножаем на коэффициент из
    # сверки с биллингом. Без сверки коэффициент = 1, и цифра будет занижена;
    # это явно помечено в overhead_multiplier.
    overhead = 1.0
    recon = result.get("billing_reconciliation")
    if recon and recon.get("uncounted_ratio"):
        overhead = recon["uncounted_ratio"]

    cost_per_post = avg * price_per_million / 1_000_000 * overhead
    result["price_per_million_rub"] = round(price_per_million, 2)
    result["overhead_multiplier"] = overhead
    result["cost_per_post_rub"] = round(cost_per_post, 4)
    if overhead == 1.0:
        result["overhead_warning"] = (
            "Коэффициент неучтённого расхода не известен (не переданы billing_rub и "
            "billing_tokens), поэтому себестоимость посчитана только по расходу, "
            "записанному в постах, и занижена на величину вызовов из not_counted_here."
        )

    plans = []
    for pkg in config.TOKEN_PACKAGES:
        tokens = pkg["tokens"]
        rub = pkg["rub"]
        # Худший случай: пользователь израсходовал весь пакет токенов.
        max_cost = tokens * price_per_million / 1_000_000 * overhead
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
