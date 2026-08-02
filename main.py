"""
Автопост — FastAPI приложение.
Запускает API + раздаёт сайт + планировщик.
"""

import json
import logging
import re
import secrets
import string
from contextlib import asynccontextmanager
from datetime import datetime, timedelta

import uvicorn
from fastapi import FastAPI, Depends, HTTPException, Request, Header, BackgroundTasks
from fastapi.responses import PlainTextResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from sqlmodel import select, delete

import config
import security
import billing
import generator
import research
import telegram_api
import tasks
from database import (
    init_db, session,
    User, Channel, Source, Post, Payment, Referral, LandingEvent, IdempotencyKey, ProductEvent,
    TrafficAttribution, PostApproval, TelegramIdentity, PostFeedback,
)
from attribution import classify_utm
from pydantic import BaseModel as _BaseModel
from typing import Optional as _Opt

class _VerifyIn(_BaseModel):
    tg_chat: str

class _ConsultIn(_BaseModel):
    message: str
    history: list = []

class _RuleIn(_BaseModel):
    rule_text: str

class _MePatch(_BaseModel):
    notify_new_post: _Opt[bool] = None
    notify_published: _Opt[bool] = None
    notify_approval_pending: _Opt[bool] = None
    notify_low_tokens: _Opt[bool] = None
    tg_chat_id: _Opt[int] = None

from schemas import (
    AuthIn, TelegramMiniAppAuthIn, ChannelIn, ChannelPatch, SourceIn,
    AnalyzeIn, AnalyzeStyleOnly, GenerateFormatIn, PostIn,
    PostPatch, ScheduleIn, BuyIn, UpgradeIn,
)

logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("autopost")


# ── Планировщик ───────────────────────────────────────────────
try:
    from apscheduler.schedulers.asyncio import AsyncIOScheduler
    _scheduler = AsyncIOScheduler(timezone="UTC")
    _HAS_SCHEDULER = True
except ImportError:
    _scheduler = None
    _HAS_SCHEDULER = False


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    logger.info("БД готова")
    if _HAS_SCHEDULER and _scheduler:
        _scheduler.add_job(
            tasks.tick, "interval", seconds=config.TICK_SECONDS,
            id="master_tick", replace_existing=True, max_instances=1, coalesce=True,
        )
        # КРИТИЧНО (P1 fix): /start у @maintrpost_bot раньше ловился только
        # внутри tick() (раз в 60с) -- пользователь мог ждать ответа до
        # минуты. Отдельная, более частая задача специально для этого --
        # не трогаем общий TICK_SECONDS, который разумен для генерации/
        # публикации постов, но слишком редок для интерактивного /start.
        _scheduler.add_job(
            tasks.poll_main_bot, "interval", seconds=config.MAIN_BOT_POLL_SECONDS,
            id="main_bot_poll", replace_existing=True, max_instances=1, coalesce=True,
        )
        # Продление подписок. Раз в час, а не в общем tick(): списание денег --
        # не та операция, которую стоит гонять раз в минуту, а точность до часа
        # для месячной подписки более чем достаточна. max_instances=1 плюс
        # детерминированный Idempotence-Key в charge_due_subscriptions
        # исключают двойное списание при наложении запусков.
        _scheduler.add_job(
            tasks.charge_due_subscriptions, "interval", hours=1,
            id="subscription_charges", replace_existing=True, max_instances=1, coalesce=True,
        )
        # Ежедневный контроль качества результата. Дефекты вроде постов-
        # близнецов не видны ни в коде, ни в метриках -- сервис проверяет
        # собственный вывод сам и пишет владельцу, если что-то не так.
        _scheduler.add_job(
            tasks.daily_quality_check, "interval", hours=24,
            id="daily_quality_check", replace_existing=True, max_instances=1, coalesce=True,
        )
        _scheduler.start()
        logger.info(f"Планировщик запущен, тик каждые {config.TICK_SECONDS}с, /start-поллинг каждые {config.MAIN_BOT_POLL_SECONDS}с")
    yield
    if _HAS_SCHEDULER and _scheduler and _scheduler.running:
        _scheduler.shutdown(wait=False)


app = FastAPI(title="Автопост", lifespan=lifespan)

# CORS: сейчас фронтенд и API раздаются с одного и того же FastAPI-приложения
# (см. app.mount("/static", ...) и роуты "/", "/landing" ниже) -- для этих
# запросов CORS браузером не проверяется вообще (same-origin). Список ниже --
# страховка на случай кросс-доменного обращения (например со старого
# закэшированного домена у части пользователей, или будущей отдельной
# статики) с Authorization-заголовком.
from fastapi.middleware.cors import CORSMiddleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "https://projectautopost.ru",
    ],
    allow_methods=["*"],
    allow_headers=["Authorization", "Content-Type"],
    max_age=86400,
)

from internal_metrics import router as internal_metrics_router
app.include_router(internal_metrics_router)

from internal_landing_funnel import router as landing_funnel_router
app.include_router(landing_funnel_router)

from internal_schema_diagnostics import router as schema_diag_router
app.include_router(schema_diag_router)

from internal_payment_path import router as payment_path_router
app.include_router(payment_path_router)

# Реальная себестоимость поста по фактическим Post.tokens_used -- чтобы цену
# тарифов можно было проверять цифрами, а не оценкой "20-40 тысяч токенов".
from internal_token_economics import router as token_economics_router
app.include_router(token_economics_router)

# Автоматический поиск дефектов, которые видит пользователь, а код-ревью не
# ловит: дубли постов, Markdown-мусор, переспросы вместо постов, подключённые
# каналы без единого поста. Проверяет фактический результат, а не исходники.
from internal_quality_scan import router as quality_scan_router
app.include_router(quality_scan_router)

from internal_user_journeys import router as user_journeys_router
from internal_llm_compare import router as llm_compare_router
from internal_telegram_ping import router as telegram_ping_router
app.include_router(telegram_ping_router)
app.include_router(llm_compare_router)
app.include_router(user_journeys_router)

# Live-лента событий для Growth Agent (кнопка/команда /live). Модуль был
# написан раньше, но роутер не был подключён — endpoint возвращал 404.
from internal_user_events import router as user_events_router
app.include_router(user_events_router)

# Ручное начисление токенов владельцем (себе для тестов, людям — при сбоях).
from internal_admin_tokens import router as admin_tokens_router
app.include_router(admin_tokens_router)

# ── Авторизация ───────────────────────────────────────────────

def current_user(authorization: str = Header(default="")) -> User:
    if not authorization.startswith("Bearer "):
        logger.info("[auth] 401: no Bearer prefix in Authorization header")
        raise HTTPException(401, "Не авторизован")
    uid = security.verify_token(authorization[7:])
    if not uid:
        logger.info(f"[auth] 401: verify_token returned None (token invalid/expired), token_prefix={authorization[7:17]}...")
        raise HTTPException(401, "Сессия истекла, войдите снова")
    with session() as s:
        user = s.get(User, uid)
        if not user:
            logger.warning(f"[auth] 401: uid={uid} from valid token, but no such User in DB")
            raise HTTPException(401, "Пользователь не найден")
        s.expunge(user)
        return user


def _gen_ref_code() -> str:
    chars = string.ascii_uppercase + string.digits
    return "".join(secrets.choice(chars) for _ in range(8))


def _link_registration_attribution(s, user: User, lp_session: str, utm_source: str, utm_medium: str, utm_campaign: str, utm_content: str):
    """CTA/Journey Diagnostics -- общая логика для /api/register и
    /api/auth/telegram_miniapp: пишет LandingEvent("register_success") и
    привязывает/создаёт TrafficAttribution к новому пользователю. Не
    блокирует регистрацию при сбое (read-only диагностика, см. LandingEvent)."""
    if lp_session:
        try:
            s.add(LandingEvent(
                session_id=lp_session[:64],
                event="register_success",
                user_id=user.id,
                utm_source=(utm_source or "")[:50],
                utm_medium=(utm_medium or "")[:50],
                utm_campaign=(utm_campaign or "")[:100],
            ))
            s.commit()
        except Exception:
            pass

    try:
        linked = False
        if lp_session:
            existing = s.exec(
                select(TrafficAttribution).where(
                    TrafficAttribution.landing_session_id == lp_session[:64],
                    TrafficAttribution.user_id == None,  # noqa: E711
                )
            ).first()
            if existing:
                existing.user_id = user.id
                s.add(existing)
                s.commit()
                linked = True

        if not linked and utm_source:
            src, med = classify_utm(utm_source, utm_medium)
            s.add(TrafficAttribution(
                user_id=user.id,
                landing_session_id=(lp_session[:64] if lp_session else None),
                source=src,
                medium=med,
                campaign=(utm_campaign or "")[:100],
                content=(utm_content or "")[:100],
            ))
            s.commit()
    except Exception:
        pass


def _own_channel(s, channel_id: int, user: User) -> Channel:
    ch = s.get(Channel, channel_id)
    if not ch or ch.user_id != user.id:
        raise HTTPException(404, "Канал не найден")
    return ch


def _channel_limit(s, user_id: int) -> int:
    """Сколько каналов разрешено пользователю. 0 -- без лимита («Агентство»)."""
    return _channel_limit_with_plan(s, user_id)[0]


def _channel_limit_with_plan(s, user_id: int) -> tuple[int, str | None]:
    """Лимит каналов и название тарифа, который его даёт (None -- бесплатный).

    Признак оплаты тот же, что и у глубины очереди (`queue_target_for_user` в
    tasks.py): платёж со статусом "paid". Берём максимум по всем оплаченным
    пакетам, а не последний -- если человек купил «Бизнес», а потом добрал
    «Старт», отнимать у него уже оплаченные каналы нельзя.

    Название тарифа нужно отдельно от лимита: «Старт» (490 ₽) и бесплатный
    тариф дают одинаковый лимит в 1 канал, но сообщение об отказе не может
    называть оплаченный тариф бесплатным -- владелец поймал это на себе 31.07.
    """
    paid = s.exec(
        select(Payment).where(Payment.user_id == user_id, Payment.status == "paid")
    ).all()
    if not paid:
        return config.FREE_CHANNELS, None
    limits = [config.CHANNELS_BY_PACKAGE.get(p.package_id, config.FREE_CHANNELS) for p in paid]
    if any(limit == 0 for limit in limits):
        return 0, None  # без лимита -- уточнять тариф в сообщении не для чего
    best = max(limits)
    title = None
    for p, lim in zip(paid, limits):
        if lim == best:
            pkg = config.package_by_id(p.package_id)
            if pkg:
                title = pkg["title"]
    return best, title


def _own_post(s, post_id: int, user: User) -> Post:
    p = s.get(Post, post_id)
    if not p or p.user_id != user.id:
        raise HTTPException(404, "Пост не найден")
    return p


# ── Мета ──────────────────────────────────────────────────────

def _frontend_version() -> str:
    """
    Версия фронтенда, реально лежащего на сервере -- строка ?v= из index.html.

    Нужна для одного простого вопроса: «что сейчас задеплоено?». Раньше ответить
    на него можно было только косвенно, разглядывая интерфейс и гадая, свежий он
    или из кэша. Теперь достаточно открыть /api/config и сравнить версию с той,
    что в репозитории.
    """
    try:
        with open("static/index.html", encoding="utf-8") as f:
            m = re.search(r"\?v=([0-9a-z]+)", f.read())
            return m.group(1) if m else "unknown"
    except Exception:
        return "unknown"


@app.get("/api/config")
def get_config():
    return {
        "bot_username": config.TELEGRAM_BOT_USERNAME,
        "public_url": config.PUBLIC_URL,
        "packages": config.TOKEN_PACKAGES,
        "soft_control_minutes": config.SOFT_CONTROL_APPROVAL_MINUTES,
        "soft_control_warning_minutes": config.SOFT_CONTROL_WARNING_MINUTES,
        # Целевая глубина очереди -- сколько готовых постов система держит
        # наготове (tasks.MIN_QUEUE). Фронт показывает "в очереди N из M" и
        # пустые слоты-заглушки, поэтому значение обязано приходить с сервера,
        # а не быть захардкожено в двух местах.
        "min_queue": tasks.MIN_QUEUE,
        # Нужен фронту, чтобы честно назвать периодичность списания до оплаты.
        "subscription_period_days": config.SUBSCRIPTION_PERIOD_DAYS,
        "subscription_enabled": config.SUBSCRIPTION_ENABLED,
        # Что реально задеплоено -- видно без гаданий, прямо в браузере.
        "frontend_version": _frontend_version(),
        "yookassa_enabled": billing.is_configured(),
        # Старый ключ оставлен для совместимости с фронтом, если браузер закэширует app.js.
        "yoomoney_enabled": billing.is_configured(),
    }


# ── Auth ──────────────────────────────────────────────────────

@app.post("/api/register")
def register(data: AuthIn):
    email = data.email.strip().lower()
    if "@" not in email or len(data.password) < 6:
        raise HTTPException(400, "Нужен корректный email и пароль от 6 символов")

    with session() as s:
        if s.exec(select(User).where(User.email == email)).first():
            raise HTTPException(400, "Пользователь с таким email уже есть")

        # Проверяем реферальный код
        referrer = None
        ref_code = (data.ref_code or "").strip().upper()
        if ref_code:
            referrer = s.exec(select(User).where(User.ref_code == ref_code)).first()

        user = User(
            email=email,
            password_hash=security.hash_password(data.password),
            token_balance=config.WELCOME_TOKENS,
            ref_code=_gen_ref_code(),
            referred_by=referrer.id if referrer else None,
        )
        s.add(user)
        s.commit()
        s.refresh(user)

        _link_registration_attribution(
            s, user, data.lp_session, data.utm_source, data.utm_medium, data.utm_campaign, data.utm_content
        )

        # Начисляем бонус рефереру
        if referrer:
            referrer_obj = s.get(User, referrer.id)
            if referrer_obj:
                referrer_obj.token_balance += config.REFERRAL_BONUS_TOKENS
                ref = Referral(
                    referrer_id=referrer.id,
                    referred_id=user.id,
                    bonus_tokens=config.REFERRAL_BONUS_TOKENS,
                )
                s.add(referrer_obj)
                s.add(ref)
                # Бонус новому пользователю тоже
                user.token_balance += config.REFERRAL_BONUS_TOKENS
                s.add(user)
                s.commit()
                s.refresh(user)

        return {"token": security.create_token(user.id), "email": user.email}


class _LandingEventIn(_BaseModel):
    session_id: str
    event: str
    url: str = ""
    utm_source: str = ""
    utm_medium: str = ""
    utm_campaign: str = ""
    utm_content: str = ""
    yclid: str = ""
    user_agent: str = ""


_ALLOWED_LANDING_EVENTS = {
    "landing_view",
    "cta_hero_bot_click",
    "cta_hero_app_click",
    "cta_header_click",
    "cta_final_click",
    "bot_start_from_landing",
    "web_register_opened",
    "register_success",
    "activation_1",
    "first_post_generated",
    "channel_connected",
    "first_post_published",
    "howto_view",
}


@app.post("/api/landing-event")
def landing_event(data: _LandingEventIn, request: Request):
    """
    Read-only диагностика пути landing -> Telegram/web -> registration
    (CTA/Journey Diagnostics). Не влияет на бизнес-логику, не блокирует
    пользователя при сбое -- событие просто не записывается.
    """
    if not data.session_id or not data.event:
        return {"ok": False}
    if data.event not in _ALLOWED_LANDING_EVENTS:
        return {"ok": False}
    try:
        ua = data.user_agent or request.headers.get("user-agent", "")
        with session() as s:
            s.add(LandingEvent(
                session_id=data.session_id[:64],
                event=data.event,
                url=(data.url or "")[:500],
                utm_source=(data.utm_source or "")[:50],
                utm_medium=(data.utm_medium or "")[:50],
                utm_campaign=(data.utm_campaign or "")[:100],
                yclid=(data.yclid or "")[:100],
                user_agent=ua[:300],
            ))
            # Attribution: фиксируем источник трафика как можно раньше (на
            # первом событии лендинга с UTM), без user_id -- привязка к
            # user_id произойдёт позже в /api/register по тому же session_id.
            # Пишем только один раз на сессию (landing_view -- первое событие
            # пути), чтобы не плодить дублирующие записи на каждый клик.
            if data.event == "landing_view" and data.utm_source:
                already = s.exec(
                    select(TrafficAttribution).where(
                        TrafficAttribution.landing_session_id == data.session_id[:64]
                    )
                ).first()
                if not already:
                    src, med = classify_utm(data.utm_source, data.utm_medium)
                    s.add(TrafficAttribution(
                        landing_session_id=data.session_id[:64],
                        source=src,
                        medium=med,
                        campaign=(data.utm_campaign or "")[:100],
                        content=(data.utm_content or "")[:100],
                    ))
            s.commit()
    except Exception:
        pass
    return {"ok": True}


class _ProductEventIn(_BaseModel):
    event: str
    package_id: str = ""


_ALLOWED_PRODUCT_EVENTS = {
    "pricing_viewed",
    "payment_cta_clicked",
    "payment_failed",
    "payment_returned",
    "quota_warning_seen",
    "limit_reached",
    # Онбординг: выбор пути в начале quick start.
    # package_id хранит значение: generate_first_post / analyze_existing_channel / skip
    "onboarding_choice_selected",
    # Качество первого поста: пользователь оценивает результат генерации.
    # package_id хранит: good / bad
    "first_post_feedback",
    # Причина недовольства первым постом (если first_post_feedback == bad).
    # package_id хранит: too_generic / wrong_style / wrong_topic / too_dry / too_salesy / other
    "first_post_feedback_reason",
    # Эксперимент commercial_bridge (SPEC_TRUEPOST_QUEUE_OFFER): мост от
    # хорошего первого поста к тарифам через предложение очереди на неделю.
    # queue_offer_shown -- блок показан после good feedback.
    # queue_offer_clicked -- клик по кнопке "Собрать очередь" (ведёт к тарифам).
    # package_id пустой у обоих.
    "queue_offer_shown",
    "queue_offer_clicked",
}


@app.post("/api/product-event")
def product_event(data: _ProductEventIn, user: User = Depends(current_user)):
    """
    Минимальная диагностика payment path после регистрации (не для рекламной
    атрибуции -- для этого уже есть LandingEvent/Метрика). Read-only, не
    влияет на бизнес-логику, не блокирует пользователя при сбое.

    Намеренно не пишет события которые уже есть как backend truth
    (registration/channel_created/post_generated/payment_started/
    payment_success) -- те уже надёжно видны через User/Channel/Post/Payment
    напрямую, дублировать их здесь не нужно (см. карту событий в
    internal_payment_path.py).
    """
    if data.event not in _ALLOWED_PRODUCT_EVENTS:
        return {"ok": False}
    try:
        with session() as s:
            s.add(ProductEvent(
                user_id=user.id,
                event=data.event,
                package_id=(data.package_id or "")[:20],
            ))
            s.commit()
    except Exception:
        pass
    return {"ok": True}


@app.post("/api/login")
def login(data: AuthIn):
    email = data.email.strip().lower()
    with session() as s:
        user = s.exec(select(User).where(User.email == email)).first()
        if not user or not security.verify_password(data.password, user.password_hash):
            raise HTTPException(401, "Неверный email или пароль")
        return {"token": security.create_token(user.id), "email": user.email}


@app.post("/api/auth/telegram_miniapp")
def auth_telegram_miniapp(data: TelegramMiniAppAuthIn):
    """
    Вход/автосоздание аккаунта по initData Telegram Mini App -- без
    email/пароля. Фронт вызывает это только с ЯВНОГО нажатия кнопки
    "Продолжить как <имя>" (см. renderAuth в static/app.part02.js), не
    молча при загрузке -- у пользователя с уже существующим email-аккаунтом
    остаётся видимый путь "Войти по email" рядом.

    initData -- одноразовая сессия WebApp, но повторные вызовы с ней же
    безопасны (verify_telegram_init_data не одноразовая, только по времени) --
    открытие Mini App заново всегда даёт свежую initData от Telegram.
    """
    if not config.MAIN_BOT_TOKEN:
        raise HTTPException(503, "Вход через Телеграм сейчас недоступен")

    parsed = security.verify_telegram_init_data(data.init_data, config.MAIN_BOT_TOKEN)
    if not parsed:
        raise HTTPException(401, "Не удалось подтвердить данные Телеграм")

    tg_user = parsed.get("user") or {}
    tg_user_id = tg_user.get("id")
    if not isinstance(tg_user_id, int):
        raise HTTPException(401, "Не удалось определить пользователя Телеграм")
    tg_username = (tg_user.get("username") or "")[:64]

    with session() as s:
        identity = s.exec(select(TelegramIdentity).where(TelegramIdentity.tg_user_id == tg_user_id)).first()

        if identity:
            user = s.get(User, identity.user_id)
            if not user:
                raise HTTPException(404, "Аккаунт не найден")
            if tg_username and user.tg_username != tg_username:
                user.tg_username = tg_username
                s.add(user)
                s.commit()
            return {"token": security.create_token(user.id), "email": user.email, "is_new": False}

        # Новый Telegram-пользователь -- создаём аккаунт автоматически.
        # tg_chat_id = tg_user_id: для приватного чата с ботом это одно и то
        # же число, поэтому уведомления от бота-публикатора начинают
        # работать сразу, без отдельного шага подключения в настройках.
        referrer = None
        ref_code = (data.ref_code or "").strip().upper()
        if ref_code:
            referrer = s.exec(select(User).where(User.ref_code == ref_code)).first()

        user = User(
            email=f"tg{tg_user_id}@telegram.local",
            password_hash=security.hash_password(secrets.token_hex(32)),
            token_balance=config.WELCOME_TOKENS,
            ref_code=_gen_ref_code(),
            referred_by=referrer.id if referrer else None,
            tg_chat_id=tg_user_id,
            tg_username=tg_username,
        )
        s.add(user)
        s.commit()
        s.refresh(user)

        s.add(TelegramIdentity(tg_user_id=tg_user_id, user_id=user.id, tg_username=tg_username))
        s.commit()

        _link_registration_attribution(
            s, user, data.lp_session, data.utm_source, data.utm_medium, data.utm_campaign, data.utm_content
        )

        if referrer:
            referrer_obj = s.get(User, referrer.id)
            if referrer_obj:
                referrer_obj.token_balance += config.REFERRAL_BONUS_TOKENS
                s.add(Referral(referrer_id=referrer.id, referred_id=user.id, bonus_tokens=config.REFERRAL_BONUS_TOKENS))
                user.token_balance += config.REFERRAL_BONUS_TOKENS
                s.add(referrer_obj)
                s.add(user)
                s.commit()
                s.refresh(user)

        return {"token": security.create_token(user.id), "email": user.email, "is_new": True}


@app.get("/api/me")
def me(user: User = Depends(current_user)):
    with session() as s:
        refs = s.exec(select(Referral).where(Referral.referrer_id == user.id)).all()
        count = len(refs)
        # Название тарифа для шапки (topbar показывает её на КАЖДОЙ странице,
        # не только на "Тарифах"). Найдено владельцем 31.07 вместе с багом
        # лимита каналов: шапка везде писала обезличенное "Тарифы", хотя
        # человек уже оплатил -- негде было даже увидеть, какой тариф у него
        # активен, не заходя специально в раздел оплаты.
        channel_limit, plan_title = _channel_limit_with_plan(s, user.id)
    return {
        "id": user.id,
        "email": user.email,
        "token_balance": user.token_balance,
        "is_admin": user.is_admin,
        "ref_code": user.ref_code,
        "referrals_count": count,
        # 0 -- без лимита (см. _channel_limit_with_plan). Нужен фронту, чтобы
        # предупредить о лимите ДО формы нового канала, а не после того как
        # человек её заполнил и нажал "Создать" (владелец 31.07).
        "channel_limit": channel_limit,
        "tg_chat_id": user.tg_chat_id,
        "notify_published": user.notify_published,
        "notify_approval_pending": user.notify_approval_pending,
        "notify_low_tokens": user.notify_low_tokens,
        "plan_title": plan_title,
    }


# ── Каналы ────────────────────────────────────────────────────

def _channel_dict(s, ch: Channel) -> dict:
    d = ch.model_dump()
    try:
        d["daily_times"] = json.loads(ch.daily_times or "[]")
    except Exception:
        d["daily_times"] = []

    # Данные для карточки канала в кабинете: что дальше в очереди и когда
    # опубликуется -- без этого карточка показывает только настройки, а не
    # реальное состояние (см. редизайн кабинета).
    #
    # КРИТИЧНО: обёрнуто в try/except с rollback. Раньше исключение здесь
    # (например в generator._clean_post на реальном, а не тестовом тексте
    # поста) валило весь /api/channels целиком -- пользователь не видел
    # вообще ни одного канала вместо одной недостающей детали на карточке.
    # rollback() обязателен для Postgres: без него упавший запрос оставляет
    # транзакцию в aborted-состоянии, и следующий запрос (для этого же или
    # СЛЕДУЮЩЕГО канала в списке, та же сессия s) тоже упадёт с
    # "current transaction is aborted", даже если сам по себе он корректен.
    d["next_post_preview"] = None
    d["queue_count"] = 0
    d["published_count"] = 0
    d["approval_deadline"] = None
    # Целевая глубина очереди зависит от того, оплачивал ли владелец (см.
    # tasks.queue_target_for_user), поэтому приходит здесь, а не в /api/config:
    # тот отдаётся без авторизации и одинаков для всех.
    #
    # queue_target -- фактическая цель пополнения (с учётом Channel.queue_depth,
    # если задан). queue_ceiling -- потолок тарифа БЕЗ учёта queue_depth: нужен
    # фронту отдельно, чтобы нарисовать степпер "от MIN_QUEUE до потолка" и
    # честно показать, докуда вообще можно увеличивать (C14, владелец 01.08).
    d["queue_target"] = tasks.MIN_QUEUE
    d["queue_ceiling"] = tasks.MIN_QUEUE
    try:
        d["queue_target"] = tasks.queue_target_for_user(s, ch.user_id, ch)
        d["queue_ceiling"] = tasks.queue_target_for_user(s, ch.user_id)
    except Exception:
        logger.exception(f"_channel_dict: не удалось определить queue_target для канала {ch.id}")
        s.rollback()
    try:
        # Единая модель очереди (C14): посты встают в очередь со статусом
        # "scheduled", а не "pending" (pending остаётся только у
        # онбординг-черновиков, см. generate_for_channel force_pending) --
        # "следующий" пост это ближайший по scheduled_at, а не по дате
        # создания.
        next_post = s.exec(
            select(Post).where(Post.channel_id == ch.id, Post.status.in_(["pending", "scheduled"]))
            .order_by(Post.scheduled_at.is_(None).asc(), Post.scheduled_at, Post.created_at)
        ).first()
        d["next_post_preview"] = generator._clean_post(next_post.text)[:220] if next_post else None
        d["queue_count"] = len(s.exec(
            select(Post).where(Post.channel_id == ch.id, Post.status.in_(["pending", "scheduled"]))
        ).all())
        d["published_count"] = len(s.exec(
            select(Post).where(Post.channel_id == ch.id, Post.status == "published")
        ).all())
        if next_post:
            approval = s.exec(
                select(PostApproval).where(PostApproval.post_id == next_post.id, PostApproval.status == "waiting")
            ).first()
            if approval:
                d["approval_deadline"] = approval.deadline.isoformat() + "Z"
    except Exception:
        logger.exception(f"_channel_dict: не удалось обогатить карточку канала {ch.id}, отдаю без превью/счётчиков")
        s.rollback()
    return d


@app.get("/api/channels")
def list_channels(user: User = Depends(current_user)):
    with session() as s:
        chans = s.exec(select(Channel).where(Channel.user_id == user.id)).all()
        return [_channel_dict(s, c) for c in chans]


class _TopicValidateIn(_BaseModel):
    topic: str


@app.post("/api/validate-topic")
async def validate_topic(data: _TopicValidateIn, user: User = Depends(current_user)):
    """
    Валидация темы ДО создания канала (quick start onboarding).

    Критично: этот эндпоинт не создаёт ничего в БД. Раньше тема проверялась
    только внутри generate_for_channel(), то есть ПОСЛЕ создания Channel —
    из-за этого неподходящая тема всё равно попадала в dashboard/settings
    как уже существующий канал, даже если генерация поста потом отказывала.
    Теперь фронт обязан вызвать этот эндпоинт первым и не создавать канал
    при отрицательном результате.
    """
    classification = await generator.classify_topic(data.topic)
    logger.info(f"validate_topic: user={user.id} topic_classification={classification} topic=«{data.topic[:80]}»")

    if classification == "ambiguous_intimate_topic":
        # Task E: серая зона — не жёсткий отказ, а уточняющий вопрос.
        # ok=false (канал пока не создаём), но это другая категория чем
        # rejection — фронт должен показать иной UX (предложение продолжить
        # с переформулированной/уточнённой темой, не просто "тема запрещена").
        return {
            "ok": False,
            "classification": classification,
            "message": generator.AMBIGUOUS_INTIMATE_CLARIFICATION,
            "is_clarification": True,
        }

    rejection_msg = generator.rejection_message(classification)
    return {
        "ok": rejection_msg is None,
        "classification": classification,
        "message": rejection_msg,
        "is_clarification": False,
    }


@app.post("/api/channels")
def create_channel(data: ChannelIn, user: User = Depends(current_user)):
    with session() as s:
        # Идемпотентность quick start (task item E): если этот client_request_id
        # уже обработан раньше (повторный клик после "Load failed", двойной
        # сабмит формы) -- возвращаем уже созданный канал, не создаём новый.
        #
        # КРИТИЧНО (P0 fix): возвращаем существующий канал ТОЛЬКО если его
        # about совпадает с текущим запросом. Если client_request_id совпал,
        # но about отличается -- это значит ключ "протёк" из предыдущей
        # quick-start сессии (stale App._qsRequestId на фронте, browser
        # back-forward cache, или любая другая причина повторного
        # использования ключа), а не настоящий повторный клик внутри одной
        # генерации. В этом случае НЕЛЬЗЯ тихо вернуть канал со старой темой —
        # лучше создать новый канал с правильной темой, чем дать пользователю
        # пост про то, что он не вводил.
        if data.client_request_id:
            existing_key = s.exec(
                select(IdempotencyKey).where(
                    IdempotencyKey.user_id == user.id,
                    IdempotencyKey.client_request_id == data.client_request_id,
                )
            ).first()
            if existing_key:
                existing_channel = s.get(Channel, existing_key.channel_id)
                if existing_channel and existing_channel.about == data.about:
                    logger.info(f"create_channel: повторный client_request_id «{data.client_request_id}», about совпадает, возвращаю существующий канал {existing_channel.id}")
                    return _channel_dict(s, existing_channel)
                elif existing_channel:
                    logger.warning(
                        f"create_channel: client_request_id «{data.client_request_id}» совпал, но about отличается "
                        f"(existing=«{existing_channel.about}» vs new=«{data.about}») -- stale request_id, создаю новый канал, НЕ возвращаю старый"
                    )

        # Лимит каналов по тарифу. Проверяем ПОСЛЕ идемпотентности: повторный
        # клик по той же кнопке не должен упираться в лимит из-за канала,
        # который сам же и создал.
        #
        # Уже созданные сверх лимита каналы не трогаем -- ограничение работает
        # только вперёд. Отнимать у людей то, чем они уже пользуются, из-за
        # нашей же недоделанной проверки нельзя.
        limit, plan_title = _channel_limit_with_plan(s, user.id)
        if limit:
            existing = len(s.exec(select(Channel).where(Channel.user_id == user.id)).all())
            if existing >= limit:
                logger.info(
                    f"create_channel: отказ по лимиту тарифа, user_id={user.id} "
                    f"каналов={existing} лимит={limit} тариф={plan_title or 'бесплатный'}"
                )
                if limit > 1:
                    msg = (
                        f"Больше каналов не добавить: на вашем тарифе их {limit}, "
                        f"а у вас уже {existing}. Выберите тариф побольше в разделе «Тарифы»."
                    )
                elif plan_title:
                    # Найдено владельцем 31.07: «Старт» и бесплатный тариф оба дают
                    # 1 канал, а сообщение раньше не различало их и называло
                    # оплаченный тариф бесплатным.
                    msg = (
                        f"На тарифе «{plan_title}» доступен 1 канал — он у вас уже есть. "
                        f"Чтобы вести несколько каналов, выберите тариф выше в разделе «Тарифы»."
                    )
                else:
                    msg = (
                        f"На бесплатном тарифе доступен один канал, он у вас уже есть. "
                        f"Чтобы вести несколько каналов, выберите тариф в разделе «Тарифы»."
                    )
                raise HTTPException(400, msg)

        ch = Channel(
            user_id=user.id,
            title=data.title,
            tg_chat=data.tg_chat.strip(),
            about=data.about,
            style=data.style,
            style_profile=data.style_profile,
            post_length=data.post_length,
            language=data.language,
            post_voice=data.post_voice,
            post_format=data.post_format,
            emoji_style=data.emoji_style,
            cta_enabled=data.cta_enabled,
            cta_text=data.cta_text,
            use_web_search=data.use_web_search,
            auto_publish=data.auto_publish,
            schedule_kind=data.schedule_kind,
            interval_hours=data.interval_hours,
            daily_times=json.dumps(data.daily_times),
            enabled=data.enabled,
            onboarded=data.onboarded,
        )
        s.add(ch)
        s.commit()
        s.refresh(ch)

        if data.client_request_id:
            # Если у этого client_request_id уже была другая запись (stale,
            # about не совпал) -- не плодим дублирующиеся idempotency-записи
            # на один ключ, перезаписываем на актуальный канал.
            old_keys = s.exec(
                select(IdempotencyKey).where(
                    IdempotencyKey.user_id == user.id,
                    IdempotencyKey.client_request_id == data.client_request_id,
                )
            ).all()
            for k in old_keys:
                s.delete(k)
            s.add(IdempotencyKey(
                user_id=user.id,
                client_request_id=data.client_request_id,
                channel_id=ch.id,
            ))
            s.commit()

        logger.info(f"[create_channel] создан channel_id={ch.id} title=«{ch.title}» about=«{ch.about}» client_request_id=«{data.client_request_id}»")
        return _channel_dict(s, ch)


@app.get("/api/channels/{channel_id}")
def get_channel(channel_id: int, user: User = Depends(current_user)):
    with session() as s:
        return _channel_dict(s, _own_channel(s, channel_id, user))


@app.patch("/api/channels/{channel_id}")
def patch_channel(channel_id: int, data: ChannelPatch, user: User = Depends(current_user)):
    with session() as s:
        ch = _own_channel(s, channel_id, user)
        payload = data.model_dump(exclude_none=True)
        if "daily_times" in payload:
            payload["daily_times"] = json.dumps(payload["daily_times"])
        if "tg_chat" in payload:
            new_chat = payload["tg_chat"].strip()
            payload["tg_chat"] = new_chat
            # Сбрасываем verified только если реально поменялся username
            if new_chat != (ch.tg_chat or ""):
                ch.verified = False
        if "queue_depth" in payload:
            # Настраиваемая глубина очереди (C14, владелец 01.08): зажимаем
            # в [MIN_QUEUE, потолок тарифа] здесь же, при записи -- иначе
            # бесплатный пользователь мог бы сохранить queue_depth=7, который
            # молча ничего не делает (queue_target_for_user всё равно обрежет
            # его до потолка при чтении), и не понимать, почему очередь не
            # растёт (правило 5: интерфейс не обещает того, чего нет).
            ceiling = tasks.queue_target_for_user(s, user.id)
            payload["queue_depth"] = max(tasks.MIN_QUEUE, min(payload["queue_depth"], ceiling))
        # При возобновлении ставим last_generated_at = now
        # чтобы следующая авто-генерация была через полный интервал, а не немедленно
        if payload.get("enabled") is True and not ch.enabled:
            ch.last_generated_at = datetime.utcnow()
        for k, v in payload.items():
            setattr(ch, k, v)
        s.add(ch)
        s.commit()
        s.refresh(ch)
        return _channel_dict(s, ch)


@app.delete("/api/channels/{channel_id}")
def delete_channel(channel_id: int, user: User = Depends(current_user)):
    from database import ChannelRule, PostApproval
    try:
        with session() as s:
            ch = _own_channel(s, channel_id, user)
            # PostApproval -- FK и на post.id, и на channel.id (режим "публикация
            # после подтверждения"). Найдено владельцем 31.07: удаление канала
            # с хотя бы одним таймером подтверждения падало на FK-нарушении --
            # ChannelRule/Source/Post чистились, а PostApproval нет. На Postgres
            # это IntegrityError, пользователь видел общее "не удалось удалить
            # канал, обновите страницу". Тот же класс бага, что уже трижды ловили
            # в delete_account() (правило 3), только для другого эндпоинта.
            #
            # flush() после каждой партии обязателен: без явных relationship()
            # между моделями SQLAlchemy не выстраивает DELETE-операторы одной
            # транзакции в порядке зависимостей сам -- а SQLite проверяет FK
            # немедленно на каждый оператор, а не в конце транзакции. Без
            # flush() удаление падало даже ПОСЛЕ того как PostApproval стали
            # чистить: DELETE FROM channel мог уйти раньше дочерних DELETE.
            # Воспроизведено и проверено отдельным скриптом до правки.
            for pa in s.exec(select(PostApproval).where(PostApproval.channel_id == channel_id)).all():
                s.delete(pa)
            s.flush()
            for src in s.exec(select(Source).where(Source.channel_id == channel_id)).all():
                s.delete(src)
            s.flush()
            for p in s.exec(select(Post).where(Post.channel_id == channel_id)).all():
                s.delete(p)
            s.flush()
            for r in s.exec(select(ChannelRule).where(ChannelRule.channel_id == channel_id)).all():
                s.delete(r)
            s.flush()
            s.delete(ch)
            s.commit()
    except HTTPException:
        raise  # 404 "канал не найден" от _own_channel — пропускаем как есть, это уже понятный текст
    except Exception as e:
        logger.error(f"delete_channel: не удалось удалить канал {channel_id}: {e}")
        raise HTTPException(500, "Не удалось удалить канал. Обновите страницу и попробуйте ещё раз.")

    # Чистим idempotency-ключи, указывающие на этот канал (task item E) —
    # иначе повторный клик с тем же client_request_id попытается вернуть
    # уже удалённый канал.
    #
    # КРИТИЧНО (P0 regression fix): это отдельная, изолированная попытка,
    # ПОСЛЕ того как сам канал и все его данные уже успешно удалены. Раньше
    # очистка IdempotencyKey была частью той же транзакции, что и удаление
    # канала — если таблица IdempotencyKey по любой причине не существовала
    # в БД (например create_all() не успел создать её на проде), весь запрос
    # падал с OperationalError и откатывал ВСЮ транзакцию, включая удаление
    # канала. Теперь это не может случиться: основное удаление уже
    # подтверждено и закоммичено выше, эта очистка — best-effort, любая её
    # ошибка только логируется.
    try:
        with session() as s:
            for k in s.exec(select(IdempotencyKey).where(IdempotencyKey.channel_id == channel_id)).all():
                s.delete(k)
            s.commit()
    except Exception as e:
        logger.warning(f"delete_channel: не удалось очистить IdempotencyKey для канала {channel_id} (не критично, канал уже удалён): {e}")

    return {"ok": True}


@app.post("/api/channels/{channel_id}/verify")
async def verify_channel(channel_id: int, user: User = Depends(current_user)):
    with session() as s:
        ch = _own_channel(s, channel_id, user)
        chat = ch.tg_chat
    if not chat:
        raise HTTPException(400, "Сначала укажите @username канала")
    ok, message = await telegram_api.verify_channel(chat)
    with session() as s:
        ch = _own_channel(s, channel_id, user)
        ch.verified = ok
        s.add(ch)
        s.commit()
    return {"ok": ok, "message": message}


@app.post("/api/channels/{channel_id}/generate")
async def generate_channel(channel_id: int, data: PostIn = PostIn(), user: User = Depends(current_user)):
    with session() as s:
        _own_channel(s, channel_id, user)
    # Единая модель очереди (C14, решение владельца 01-02.08): "Написать пост
    # сейчас" встаёт в общую очередь на тех же правах, что и плановая
    # генерация по расписанию -- получает scheduled_at и (в режиме
    # подтверждения) обычный таймер согласования. force_pending=False
    # (по умолчанию) -- этот флаг остался только для онбординга, где первый
    # черновик показывается сразу на экране, ещё до всякой очереди.
    #
    # Найдено владельцем 31.07, исправлено повторно 02.08: раньше здесь
    # стояло force_pending=True безусловно (пост "Ждёт вашего решения ... сам
    # не опубликуется" даже на автопилоте), затем force_pending=not
    # auto_publish (публиковал МГНОВЕННО на автопилоте) -- оба варианта
    # противоречили принципу "пост никогда не публикуется в момент
    # генерации", который владелец сформулировал явно после разбора обеих
    # попыток.
    result = await tasks.generate_for_channel(channel_id, topic=data.topic)
    if not result["ok"]:
        raise HTTPException(400, result["message"])
    return result


@app.post("/api/channels/{channel_id}/generate_format")
async def generate_channel_format(
    channel_id: int, data: GenerateFormatIn, user: User = Depends(current_user)
):
    """Генерирует пост в конкретном формате (для онбординга — 3 варианта)."""
    with session() as s:
        ch = _own_channel(s, channel_id, user)
        u = s.get(User, user.id)
        if u.token_balance <= 0:
            raise HTTPException(400, "Бесплатный лимит закончился. Пополните баланс, чтобы создавать новые посты.")

    # Временно меняем формат для этой генерации
    with session() as s:
        ch = s.get(Channel, channel_id)
        original_format = ch.post_format
        ch.post_format = data.post_format
        s.add(ch)
        s.commit()

    try:
        result = await tasks.generate_for_channel(channel_id, force_pending=True)
    finally:
        # Возвращаем оригинальный формат
        with session() as s:
            ch = s.get(Channel, channel_id)
            if ch:
                ch.post_format = original_format
                s.add(ch)
                s.commit()

    if not result["ok"]:
        raise HTTPException(400, result["message"])

    # Возвращаем текст поста напрямую для онбординга
    with session() as s:
        post = s.get(Post, result.get("post_id"))
        text = post.text if post else ""

    return {
        "ok": True,
        "post_id": result.get("post_id"),
        "text": text,
        "tokens_used": result.get("tokens_used", 0),
    }


@app.post("/api/channels/{channel_id}/analyze")
async def analyze_channel(channel_id: int, data: AnalyzeIn, user: User = Depends(current_user)):
    with session() as s:
        _own_channel(s, channel_id, user)
        u = s.get(User, user.id)
        if u.token_balance <= 0:
            raise HTTPException(400, "Бесплатный лимит закончился. Пополните баланс, чтобы создавать новые посты.")

    posts = await research.scrape_channel(data.link)
    if not posts:
        raise HTTPException(400, "Не удалось прочитать канал. Он должен быть публичным.")

    profile, tokens = await generator.analyze_style(posts)

    with session() as s:
        ch = s.get(Channel, channel_id)
        ch.style_profile = profile
        u = s.get(User, user.id)
        u.token_balance = max(0, u.token_balance - tokens)
        s.add(ch); s.add(u); s.commit()

    return {"ok": True, "profile": profile, "analyzed_posts": len(posts), "tokens_used": tokens}


@app.post("/api/analyze_style_only")
async def analyze_style_only(data: AnalyzeStyleOnly, user: User = Depends(current_user)):
    """Анализ стиля без привязки к каналу — для онбординга."""
    with session() as s:
        u = s.get(User, user.id)
        if u.token_balance <= 0:
            raise HTTPException(400, "Бесплатный лимит закончился. Пополните баланс, чтобы создавать новые посты.")

    posts = await research.scrape_channel(data.link)
    if not posts:
        raise HTTPException(400, "Не удалось прочитать канал. Он должен быть публичным.")

    profile, tokens = await generator.analyze_style(posts)

    with session() as s:
        u = s.get(User, user.id)
        u.token_balance = max(0, u.token_balance - tokens)
        s.add(u); s.commit()

    return {"ok": True, "profile": profile, "analyzed_posts": len(posts)}


# ── Источники ─────────────────────────────────────────────────

@app.get("/api/channels/{channel_id}/sources")
def list_sources(channel_id: int, user: User = Depends(current_user)):
    with session() as s:
        _own_channel(s, channel_id, user)
        srcs = s.exec(select(Source).where(Source.channel_id == channel_id)).all()
        return [src.model_dump() for src in srcs]


@app.post("/api/channels/{channel_id}/sources")
def add_source(channel_id: int, data: SourceIn, user: User = Depends(current_user)):
    url = data.url.strip()
    if not url.startswith("http"):
        raise HTTPException(400, "Источник должен быть ссылкой (http/https)")
    with session() as s:
        _own_channel(s, channel_id, user)
        src = Source(channel_id=channel_id, url=url)
        s.add(src); s.commit(); s.refresh(src)
        return src.model_dump()


@app.delete("/api/sources/{source_id}")
def delete_source(source_id: int, user: User = Depends(current_user)):
    with session() as s:
        src = s.get(Source, source_id)
        if not src:
            raise HTTPException(404, "Источник не найден")
        _own_channel(s, src.channel_id, user)
        s.delete(src); s.commit()
    return {"ok": True}


# ── Посты ──────────────────────────────────────────────────────

@app.get("/api/channels/{channel_id}/posts")
def list_posts(channel_id: int, user: User = Depends(current_user)):
    with session() as s:
        _own_channel(s, channel_id, user)
        posts = s.exec(
            select(Post).where(Post.channel_id == channel_id).order_by(Post.created_at.desc())
        ).all()
        # Дедлайн автопубликации -- ПОштучно, а не одним флагом на канал.
        # КРИТИЧНО: таймер заводится только в режиме подтверждения, и только
        # если карточка реально доставлена в Telegram (см. _send_approval_card
        # в tasks.py) -- иначе пост ждёт решения без таймера сколько угодно.
        # Раньше очередь показывала общее обещание "опубликуется сам через 30
        # мин" на уровне всего канала, и
        # для большинства постов это была неправда.
        deadlines = {
            a.post_id: a.deadline.isoformat() + "Z"
            for a in s.exec(
                select(PostApproval).where(
                    PostApproval.channel_id == channel_id,
                    PostApproval.status == "waiting",
                )
            ).all()
        }
        # Оценки автора (👍/👎) -- одним запросом на весь список, а не по
        # запросу на карточку.
        feedback = {
            f.post_id: f.verdict
            for f in s.exec(
                select(PostFeedback).where(PostFeedback.user_id == user.id)
            ).all()
        }
        out = []
        for p in posts:
            d = p.model_dump()
            d["approval_deadline"] = deadlines.get(p.id)
            d["feedback"] = feedback.get(p.id)
            out.append(d)
        return out


class PostFeedbackIn(_BaseModel):
    verdict: str    # "up" | "down" | "none" (снять оценку)


@app.post("/api/posts/{post_id}/feedback")
def post_feedback(post_id: int, data: PostFeedbackIn, user: User = Depends(current_user)):
    """
    Оценка поста автором: 👍 / 👎 / снять оценку.

    Ничего в поведении продукта не меняет -- ни публикацию, ни генерацию.
    Это накопление данных для C1 («оценить качество постов честно»): по
    опубликован/отклонён судить о качестве нельзя, отклонить могли и из-за
    неподходящей темы.
    """
    if data.verdict not in ("up", "down", "none"):
        raise HTTPException(400, "verdict должен быть 'up', 'down' или 'none'")

    with session() as s:
        post = _own_post(s, post_id, user)
        existing = s.exec(
            select(PostFeedback).where(
                PostFeedback.post_id == post_id,
                PostFeedback.user_id == user.id,
            )
        ).first()

        if data.verdict == "none":
            if existing:
                s.delete(existing)
                s.commit()
            return {"ok": True, "verdict": None}

        if existing:
            existing.verdict = data.verdict
            existing.updated_at = datetime.utcnow()
            s.add(existing)
        else:
            s.add(PostFeedback(
                post_id=post_id, user_id=user.id,
                channel_id=post.channel_id, verdict=data.verdict,
            ))
        s.commit()
        return {"ok": True, "verdict": data.verdict}


@app.get("/api/channels/{channel_id}/schedule_preview")
def schedule_preview(channel_id: int, user: User = Depends(current_user)):
    """
    Прогноз ближайших автопубликаций для календаря в кабинете (см. C12 в
    PRODUCT_ROADMAP.md, запрос владельца 28.07: смена частоты должна сразу
    отражаться в календаре).

    Отдаём только для канала с включённым автопилотом. Без него у постов нет
    даты, когда они выйдут сами -- решение всегда за пользователем, и любой
    прогноз здесь читался бы как обещание автопубликации, которого система
    не выполняет (принцип 5 в CLAUDE.md).
    """
    with session() as s:
        ch = _own_channel(s, channel_id, user)
        # Те же условия, что и у настоящего tick() (tasks.py: due_ids
        # собираются по `c.verified and _is_due(...)`) -- без подтверждённого
        # бота автопилот не публикует ничего и никогда, и прогноз дат,
        # которые не наступят, был бы обманом, а не прогнозом.
        if not ch.auto_publish or not ch.verified or not ch.enabled:
            return {"slots": []}
        slots = tasks.project_upcoming_slots(ch, datetime.utcnow(), count=30)
        return {"slots": [s.isoformat() + "Z" for s in slots]}


@app.patch("/api/posts/{post_id}")
def edit_post(post_id: int, data: PostPatch, user: User = Depends(current_user)):
    with session() as s:
        p = _own_post(s, post_id, user)
        if p.status == "published":
            raise HTTPException(400, "Опубликованный пост нельзя редактировать")
        p.text = data.text
        s.add(p); s.commit(); s.refresh(p)
        return p.model_dump()


@app.get("/api/posts/{post_id}/status")
def post_status(post_id: int, user: User = Depends(current_user)):
    """
    Лёгкий статус-эндпоинт для reconciliation на фронте после ложного
    timeout публикации (P0 fix): фронт опрашивает его, чтобы узнать
    реальное состояние поста, не повторяя сам publish.
    """
    with session() as s:
        p = _own_post(s, post_id, user)
        return {
            "id": p.id,
            "status": p.status,
            "telegram_message_id": p.tg_message_id,
            "published_at": p.published_at.isoformat() if p.published_at else None,
        }


@app.post("/api/posts/{post_id}/publish")
async def publish(post_id: int, background_tasks: BackgroundTasks, user: User = Depends(current_user)):
    with session() as s:
        _own_post(s, post_id, user)
    result = await tasks.publish_post(post_id)
    if not result["ok"]:
        raise HTTPException(400, result["message"])
    # Гасим карточку "публикация после подтверждения" в Telegram, если она
    # есть -- иначе устаревшая кнопка там могла бы среагировать повторно.
    tasks.cancel_pending_approval(post_id)
    # Уведомление и автодогенерация очереди — после ответа клиенту, не
    # блокируют его (см. tasks.publish_post: это была причина false timeout,
    # когда автодогенерация следующего поста в очереди задерживала HTTP-ответ
    # на десятки секунд уже после успешной публикации в Telegram).
    if not result.get("already_published"):
        background_tasks.add_task(tasks.post_publish_followup, post_id)
    return result


@app.post("/api/posts/{post_id}/schedule")
def schedule_post(post_id: int, data: ScheduleIn, user: User = Depends(current_user)):
    try:
        when = datetime.fromisoformat(data.scheduled_at.replace("Z", ""))
    except Exception:
        raise HTTPException(400, "Неверный формат даты")
    with session() as s:
        p = _own_post(s, post_id, user)
        p.status = "scheduled"
        p.scheduled_at = when
        s.add(p); s.commit(); s.refresh(p)
        return p.model_dump()


@app.post("/api/posts/{post_id}/reject")
async def reject_post(post_id: int, user: User = Depends(current_user)):
    with session() as s:
        p = _own_post(s, post_id, user)
        channel_id = p.channel_id
        p.status = "rejected"
        s.add(p); s.commit()
    tasks.cancel_pending_approval(post_id)
    await tasks._refill_queue(channel_id)
    return {"ok": True}


@app.delete("/api/posts/{post_id}")
async def delete_post(post_id: int, user: User = Depends(current_user)):
    with session() as s:
        p = _own_post(s, post_id, user)
        channel_id = p.channel_id
        s.delete(p); s.commit()
    tasks.cancel_pending_approval(post_id)
    await tasks._refill_queue(channel_id)
    return {"ok": True}


# ── Биллинг ───────────────────────────────────────────────────

@app.get("/api/packages")
def packages():
    return config.TOKEN_PACKAGES


async def _sync_yookassa_pending_payments(user_id: int) -> None:
    """
    Резервная синхронизация платежей с YooKassa.

    Нужна на случай, если HTTP-уведомление YooKassa не дошло или было
    пропущено. Проверяем только платежи текущего пользователя со статусом
    pending/waiting_for_capture и уже известным operation_id = payment_id YooKassa.
    """
    if not billing.is_configured():
        return

    with session() as s:
        pending = s.exec(
            select(Payment).where(
                Payment.user_id == user_id,
                Payment.status.in_(["pending", "waiting_for_capture"]),
                Payment.operation_id != "",
            ).order_by(Payment.created_at.desc())
        ).all()
        items = [(p.id, p.operation_id) for p in pending if p.operation_id]

    any_credited = False
    for local_payment_id, yookassa_payment_id in items:
        try:
            yk_payment = await billing.get_payment(yookassa_payment_id)
        except billing.YooKassaError as exc:
            logger.warning(
                "Не удалось синхронизировать платёж YooKassa %s: %s",
                yookassa_payment_id, exc,
            )
            continue

        status = yk_payment.get("status", "")
        paid = yk_payment.get("paid") is True

        with session() as s:
            pay = s.get(Payment, local_payment_id)
            if not pay or pay.user_id != user_id:
                continue

            if status == "canceled":
                if pay.status != "paid":
                    pay.status = "canceled"
                    s.add(pay)
                    s.commit()
                continue

            if status != "succeeded" or not paid:
                if status and pay.status != status:
                    pay.status = status
                    s.add(pay)
                    s.commit()
                continue

            try:
                actual_amount = round(float((yk_payment.get("amount") or {}).get("value", 0)), 2)
            except Exception:
                actual_amount = 0
            expected_amount = round(float(pay.rub), 2)
            if actual_amount != expected_amount:
                logger.error(
                    "YooKassa sync: сумма не совпала, payment_id=%s, actual=%s, expected=%s",
                    yookassa_payment_id, actual_amount, expected_amount,
                )
                continue

            if pay.status != "paid":
                pay.status = "paid"
                pay.paid_at = datetime.utcnow()
                u = s.get(User, pay.user_id)
                if u:
                    u.token_balance += pay.tokens
                    s.add(u)
                s.add(pay)
                s.commit()
                logger.info(
                    "Платёж YooKassa зачтён через sync: пользователь %s +%s токенов",
                    pay.user_id, pay.tokens,
                )
                _activate_subscription(s, pay, yk_payment)
                any_credited = True

    if any_credited:
        # Вне сессии выше: генерация может занять секунды (вызов модели), и
        # держать открытую сессию БД всё это время не нужно. См.
        # tasks.resume_starved_channels -- почему это важнее, чем дождаться
        # планового тика.
        try:
            await tasks.resume_starved_channels(user_id)
        except Exception as e:
            logger.warning(f"resume_starved_channels после sync-начисления: {e}")


@app.get("/api/payments")
async def payments(user: User = Depends(current_user)):
    await _sync_yookassa_pending_payments(user.id)
    with session() as s:
        ps = s.exec(
            select(Payment).where(Payment.user_id == user.id).order_by(Payment.created_at.desc())
        ).all()
        return [p.model_dump() for p in ps]


def _find_refundable_payment(s, user_id: int) -> tuple[Payment | None, str | None]:
    """
    Последний оплаченный платёж пользователя и причина, по которой его нельзя
    вернуть автоматически (None -- можно).

    Условия -- те же три дня и "токены не использовались", что написаны в
    static/legal/refund.html, только проверяются кодом, а не человеком на
    слово. "Использовались" проверяем буквально по документу: появился ли
    после оплаты хоть один пост (Post.created_at > paid_at) -- не пытаемся
    точно приписать конкретные токены конкретной оплате (баланс общий,
    пополняется из нескольких источников), это ровно то же упрощение, что и
    в перерасчёте при апгрейде тарифа.
    """
    pay = s.exec(
        select(Payment).where(Payment.user_id == user_id, Payment.status == "paid")
        .order_by(Payment.created_at.desc())
    ).first()
    if not pay:
        return None, "Оплаченных платежей нет"
    if not pay.paid_at:
        return pay, "Платёж ещё не подтверждён"
    if datetime.utcnow() - pay.paid_at > timedelta(days=config.REFUND_WINDOW_DAYS):
        return pay, f"С момента оплаты прошло больше {config.REFUND_WINDOW_DAYS} дней"
    used = s.exec(
        select(Post).where(Post.user_id == user_id, Post.created_at > pay.paid_at)
    ).first()
    if used:
        return pay, "После оплаты уже был создан пост"
    if not pay.operation_id:
        return pay, "У платежа нет номера ЮKassa"
    return pay, None


@app.get("/api/subscription")
def get_subscription(user: User = Depends(current_user)):
    """Текущая подписка пользователя (или none, если её нет)."""
    from database import Subscription
    with session() as s:
        sub = s.exec(
            select(Subscription).where(
                Subscription.user_id == user.id,
                Subscription.status.in_(["active", "suspended"]),
            ).order_by(Subscription.created_at.desc())
        ).first()
        if not sub:
            return {"subscription": None, "payment_method": None}
        pkg = config.package_by_id(sub.package_id) or {}
        payment, refund_blocked_reason = _find_refundable_payment(s, user.id)
        return {"payment_method": ({
            "title": sub.payment_method_title or "Сохранённый способ оплаты",
        } if sub.payment_method_id else None), "subscription": {
            "status": sub.status,
            "package_id": sub.package_id,
            "title": pkg.get("title", sub.package_id),
            # sub.price_rub -- зафиксированная цена подписчика (п.3 оферты),
            # а НЕ текущая цена тарифа из конфига. Раньше здесь стояло
            # pkg.get("rub") -- если цены поднимутся, кабинет показывал бы
            # подписчику чужую (новую) цену вместо той, по которой он
            # оформился, хотя списание всё равно шло бы по price_rub.
            "rub": sub.price_rub or pkg.get("rub"),
            "period_days": config.SUBSCRIPTION_PERIOD_DAYS,
            "next_charge_at": sub.next_charge_at.isoformat() + "Z" if sub.next_charge_at else None,
            "last_error": sub.last_error,
            "refund_eligible": bool(payment and not refund_blocked_reason),
            "refund_reason": refund_blocked_reason,
            "refund_amount_rub": payment.rub if (payment and not refund_blocked_reason) else None,
            "refund_window_days": config.REFUND_WINDOW_DAYS,
        }}


@app.delete("/api/subscription")
def cancel_subscription(user: User = Depends(current_user)):
    """
    Отмена подписки: прекращаем автосписания И отвязываем сохранённый способ
    оплаты.

    Очистка payment_method_id здесь обязательна, а не косметика: ЮKassa
    подключает рекуррентные платежи только тем магазинам, где покупатель может
    самостоятельно отвязать карту, не обращаясь в поддержку. Эта ручка (и
    кнопка «Отменить подписку» в кабинете, которая её дёргает) -- и есть
    выполнение того требования. После неё сохранённым методом физически нечем
    списать: продление берёт payment_method_id именно отсюда.

    Уже оплаченные токены НЕ отбираем и подписку не удаляем задним числом --
    человек оплатил текущий период и должен им пользоваться.
    """
    from database import Subscription
    with session() as s:
        subs = s.exec(
            select(Subscription).where(
                Subscription.user_id == user.id,
                Subscription.status.in_(["active", "suspended"]),
            )
        ).all()
        if not subs:
            raise HTTPException(404, "Активной подписки нет")
        for sub in subs:
            sub.status = "cancelled"
            sub.cancelled_at = datetime.utcnow()
            sub.next_charge_at = None
            sub.payment_method_id = ""   # отвязка карты
            s.add(sub)
        s.commit()
        logger.info(
            f"Подписка пользователя {user.id} отменена ({len(subs)} шт.), "
            f"сохранённый способ оплаты отвязан"
        )
    return {"ok": True}


@app.post("/api/subscription/refund")
async def refund_subscription(user: User = Depends(current_user)):
    """
    Самообслуживаемый возврат последнего платежа + отмена подписки.

    Решение владельца 31.07: основной способ возврата -- эта кнопка, а не
    письмо на почту, потому что владелец может не увидеть письмо вовремя и
    потом придётся спорить с человеком, который не уложился в обещанный день
    ответа. Условия ровно те же, что в static/legal/refund.html (3 дня,
    токены не тронуты) -- проверяет `_find_refundable_payment`, здесь же и
    показывается причина отказа, если возврат недоступен.

    Подписка гасится сразу же вместе с возвратом -- нельзя вернуть деньги за
    период и продолжать им пользоваться.
    """
    from database import Subscription
    with session() as s:
        sub = s.exec(select(Subscription).where(
            Subscription.user_id == user.id,
            Subscription.status.in_(["active", "suspended"]),
        )).first()
        if not sub:
            raise HTTPException(400, "Активной подписки нет")
        payment, reason = _find_refundable_payment(s, user.id)
        if not payment or reason:
            raise HTTPException(400, reason or "Возврат недоступен")
        payment_pk = payment.id
        operation_id = payment.operation_id
        rub = payment.rub
        tokens = payment.tokens
        sub_id = sub.id

    idempotence_key = f"refund-{payment_pk}"
    try:
        result = await billing.refund_payment(
            payment_operation_id=operation_id, amount_rub=rub, idempotence_key=idempotence_key,
        )
    except billing.YooKassaError as exc:
        raise HTTPException(400, f"Не удалось оформить возврат: {exc}")

    if result.get("status") not in ("succeeded", "pending"):
        raise HTTPException(400, "ЮKassa не подтвердила возврат. Попробуйте ещё раз позже или напишите в поддержку.")

    with session() as s:
        pay = s.get(Payment, payment_pk)
        if pay:
            pay.status = "refunded"
            s.add(pay)
        u = s.get(User, user.id)
        if u:
            # Общий баланс, не привязка к конкретной оплате (та же оговорка,
            # что и в перерасчёте апгрейда) -- отнимаем ровно столько, сколько
            # эта оплата дала, но не уходим в минус.
            u.token_balance = max(0, u.token_balance - tokens)
            s.add(u)
        sub = s.get(Subscription, sub_id)
        if sub:
            sub.status = "cancelled"
            sub.cancelled_at = datetime.utcnow()
            sub.next_charge_at = None
            sub.payment_method_id = ""
            s.add(sub)
        s.commit()

    logger.info(
        "Возврат: пользователь %s, платёж %s, %s ₽, подписка отменена",
        user.id, payment_pk, rub,
    )
    return {"ok": True, "refunded_rub": rub}


@app.post("/api/subscription/upgrade")
async def upgrade_subscription(data: UpgradeIn, user: User = Depends(current_user)):
    """
    Смена тарифа на более дорогой, с перерасчётом по неизрасходованному остатку.

    Решение владельца 31.07: понизить тариф самому нельзя вообще -- кто хочет
    более простой тариф, отменяет подписку (эта опция уже есть) и оформляет
    заново. Так проще для пользователя (нечего объяснять) и без риска
    случайного возврата денег кодом.

    Перерасчёт -- по НЕИЗРАСХОДОВАННЫМ ТОКЕНАМ текущего тарифа, а не по дням:
    остаток = token_balance / сколько токенов давал текущий тариф (не больше
    100% -- если баланс больше за счёт рефералов или прошлых тарифов, лишнее
    не возвращаем). Эта доля от уже уплаченной цены (`sub.price_rub`, а НЕ
    текущей цены из конфига -- п.3 оферты про фиксацию цены) вычитается из
    цены нового тарифа.

    Списание -- сразу, по уже сохранённой карте (`sub.payment_method_id`),
    без редиректа: то же самое, чем `charge_due_subscriptions` продлевает
    подписку каждый период, только вне расписания. Именно поэтому апгрейд
    недоступен без сохранённого способа оплаты (нечем списать без участия
    человека) -- предлагаем отменить и оформить заново вместо того, чтобы
    городить редирект-путь ради редкого случая.

    period_no/last_period_key планового автосписания НЕ трогаем: апгрейд —
    отдельный платёж вне обычного цикла (свой label/idempotence_key), поэтому
    следующее плановое продление само пойдёт по расписанию без коллизий.
    """
    if not config.SUBSCRIPTION_ENABLED:
        raise HTTPException(400, "Смена тарифа доступна только с активной подпиской")

    target_pkg = config.package_by_id(data.package_id)
    if not target_pkg:
        raise HTTPException(400, "Тариф не найден")

    from database import Subscription
    with session() as s:
        sub = s.exec(select(Subscription).where(
            Subscription.user_id == user.id,
            Subscription.status.in_(["active", "suspended"]),
        )).first()
        if not sub:
            raise HTTPException(400, "Активной подписки нет — выберите тариф обычной покупкой")
        if not sub.payment_method_id:
            raise HTTPException(
                400,
                "Нет привязанного способа оплаты — сменить тариф автоматически нечем. "
                "Отмените подписку и оформите новый тариф заново.",
            )
        if target_pkg["rub"] <= sub.price_rub:
            raise HTTPException(
                400,
                "Сменить можно только на тариф дороже текущего. Если хотите тариф проще — "
                "отмените подписку в этом же разделе.",
            )

        current_pkg = config.package_by_id(sub.package_id) or {}
        current_tokens = current_pkg.get("tokens", 0)
        u = s.get(User, user.id)
        unused_fraction = min(1.0, max(0.0, u.token_balance / current_tokens)) if current_tokens else 0.0
        credit_rub = round(sub.price_rub * unused_fraction)
        charge_rub = max(0, target_pkg["rub"] - credit_rub)

        sub_id = sub.id
        payment_method_id = sub.payment_method_id
        user_email = u.email
        # Идемпотентный, но привязанный к КОНКРЕТНОМУ переходу (откуда, за
        # сколько было оплачено, куда) -- случайный повторный клик до ответа
        # сервера сойдётся в тот же ключ и ЮKassa не спишет дважды; а вот
        # апгрейд на тот же тариф ПОСЛЕ уже свершившегося перехода получит
        # другой sub.price_rub на входе и упадёт раньше, на проверке выше.
        idempotence_key = f"upgrade-{sub_id}-{sub.package_id}-{sub.price_rub}-to-{data.package_id}"
        label = f"u{user.id}-upgrade-{data.package_id}-{secrets.token_hex(6)}"

    charged = charge_rub
    yk_payment = {}
    if charge_rub > 0:
        try:
            yk_payment = await billing.charge_recurring(
                payment_method_id=payment_method_id,
                amount_rub=charge_rub,
                description=f"Автопост: смена тарифа на «{target_pkg['title']}» (перерасчёт остатка)",
                user_id=user.id,
                package_id=data.package_id,
                label=label,
                idempotence_key=idempotence_key,
                user_email=user_email,
            )
        except billing.YooKassaError as exc:
            raise HTTPException(400, f"Не удалось списать оплату: {exc}")
        if not (yk_payment.get("status") == "succeeded" and yk_payment.get("paid")):
            raise HTTPException(400, "Платёж не подтверждён ЮKassa. Попробуйте ещё раз через минуту.")

    now = datetime.utcnow()
    with session() as s:
        sub = s.get(Subscription, sub_id)
        u = s.get(User, user.id)
        if not sub or not u:
            raise HTTPException(404, "Подписка не найдена")
        s.add(Payment(
            user_id=user.id, package_id=data.package_id, label=label,
            rub=charged, tokens=target_pkg["tokens"], status="paid",
            operation_id=yk_payment.get("id", ""),
        ))
        u.token_balance += target_pkg["tokens"]
        sub.package_id = data.package_id
        sub.price_rub = target_pkg["rub"]
        sub.next_charge_at = now + timedelta(days=config.SUBSCRIPTION_PERIOD_DAYS)
        sub.status = "active"
        sub.fail_count = 0
        sub.last_error = ""
        s.add(u); s.add(sub); s.commit()

    logger.info(
        "Апгрейд подписки: пользователь %s -> %s, списано %s ₽ (кредит %s ₽)",
        user.id, data.package_id, charged, credit_rub,
    )
    try:
        await tasks.resume_starved_channels(user.id)
    except Exception as e:
        logger.warning(f"resume_starved_channels после апгрейда: {e}")
    return {"ok": True, "package_id": data.package_id, "charged_rub": charged, "credit_rub": credit_rub}


def _activate_subscription(s, pay: Payment, yk_payment: dict) -> None:
    """
    Заводит или продлевает подписку после успешной оплаты.

    Вызывается из обоих путей зачисления (вебхук и sync) сразу после того, как
    платёж переведён в "paid" -- одной функцией, чтобы поведение не разъезжалось
    между путями.

    Если YooKassa не сохранила метод оплаты (пользователь платил способом, не
    поддерживающим рекурренты), подписку всё равно НЕ заводим -- иначе в
    интерфейсе висела бы активная подписка, списать по которой невозможно.
    Оплаченные токены при этом уже зачислены и никуда не денутся: человек
    просто остаётся на разовой оплате.
    """
    from database import Subscription
    if not config.SUBSCRIPTION_ENABLED:
        # Разовый режим: токены уже начислены выше, подписке взяться неоткуда.
        return
    try:
        method_id = billing.extract_saved_method_id(yk_payment)
        now = datetime.utcnow()
        sub = s.exec(
            select(Subscription).where(
                Subscription.user_id == pay.user_id,
                Subscription.status.in_(["active", "suspended"]),
            )
        ).first()

        if not method_id and not sub:
            # Самая частая причина -- у магазина не подключены автоплатежи.
            # Включить их самостоятельно в настройках нельзя, это делается
            # через менеджера ЮKassa, поэтому логируем сырой payment_method:
            # по полю saved сразу видно, сохранила ли ЮKassa метод оплаты.
            logger.warning(
                "Платёж %s: метод оплаты НЕ сохранён -- подписка не заводится, "
                "человек остаётся на разовой оплате. payment_method=%s. "
                "Если saved=false -- у магазина не подключены автоплатежи "
                "(запрашиваются у менеджера ЮKassa).",
                pay.id, yk_payment.get("payment_method"),
            )
            return

        next_charge = now + timedelta(days=config.SUBSCRIPTION_PERIOD_DAYS)
        if sub:
            # Продление уже существующей подписки (в том числе воскрешение
            # приостановленной, если человек оплатил вручную).
            sub.status = "active"
            sub.package_id = pay.package_id or sub.package_id
            # Цену НЕ переписываем текущей ценой из конфига -- за подписчиком
            # сохраняется та, по которой он оформился (оферта, п. 3).
            # Проставляем только если её ещё нет (старые строки до миграции).
            if not sub.price_rub:
                sub.price_rub = pay.rub
            if method_id:
                sub.payment_method_id = method_id
                sub.payment_method_title = billing.describe_payment_method(yk_payment)
            sub.next_charge_at = next_charge
            sub.fail_count = 0
            sub.last_error = ""
            s.add(sub)
        else:
            s.add(Subscription(
                user_id=pay.user_id,
                package_id=pay.package_id,
                price_rub=pay.rub,
                payment_method_id=method_id,
                payment_method_title=billing.describe_payment_method(yk_payment),
                status="active",
                period_no=1,
                next_charge_at=next_charge,
            ))
        s.commit()
        logger.info(
            "Подписка пользователя %s активна, следующее списание %s",
            pay.user_id, next_charge.isoformat(),
        )
    except Exception:
        # Деньги уже зачислены выше -- падать здесь нельзя, иначе вебхук
        # ответит ошибкой и YooKassa начнёт слать повторы по уже
        # обработанному платежу.
        logger.exception("Не удалось активировать подписку по платежу %s", pay.id)
        try:
            s.rollback()
        except Exception:
            pass


@app.post("/api/billing/buy")
async def buy(data: BuyIn, user: User = Depends(current_user)):
    pkg = config.package_by_id(data.package_id)
    if not pkg:
        raise HTTPException(400, "Пакет не найден")
    if not billing.is_configured():
        raise HTTPException(400, "Приём платежей не настроен")

    label = f"u{user.id}-{data.package_id}-{secrets.token_hex(6)}"
    description = (
        f"Автопост: подписка «{pkg['title']}», {config.SUBSCRIPTION_PERIOD_DAYS} дн."
        if config.SUBSCRIPTION_ENABLED
        else f"Автопост: пакет «{pkg['title']}» ({pkg['tokens']} токенов)"
    )

    with session() as s:
        pay = Payment(
            user_id=user.id,
            package_id=pkg["id"],
            label=label,
            rub=pkg["rub"],
            tokens=pkg["tokens"],
            status="pending",
        )
        s.add(pay)
        s.commit()
        s.refresh(pay)
        local_payment_id = pay.id

    try:
        yk_payment = await billing.create_payment(
            label=label,
            amount_rub=pkg["rub"],
            description=description,
            user_id=user.id,
            package_id=pkg["id"],
            user_email=user.email,
            # Первый платёж подписки: просим YooKassa сохранить метод оплаты,
            # иначе продлевать будет нечем (см. billing.charge_recurring).
            # Пока рекуррент не согласован с ЮKassa -- не просим вовсе, чтобы
            # не создавать видимость подписки там, где её не будет.
            save_payment_method=config.SUBSCRIPTION_ENABLED,
        )
    except billing.YooKassaError as exc:
        with session() as s:
            pay = s.get(Payment, local_payment_id)
            if pay:
                pay.status = "failed"
                s.add(pay)
                s.commit()
        error_msg = str(exc)
        logger.error(f"YooKassa payment error for user {user.id}: {error_msg}")
        raise HTTPException(400, f"Ошибка оплаты: {error_msg}")

    payment_id = yk_payment.get("id", "")
    payment_status = yk_payment.get("status", "pending")
    confirmation_url = (yk_payment.get("confirmation") or {}).get("confirmation_url")
    if not confirmation_url:
        # Раньше Payment оставался pending навсегда -- diagnostics не мог
        # отличить "провайдер не ответил" от "пользователь ещё не оплатил".
        with session() as s:
            pay = s.get(Payment, local_payment_id)
            if pay:
                pay.status = "failed"
                s.add(pay)
                s.commit()
        raise HTTPException(502, "YooKassa не вернула ссылку на оплату")

    with session() as s:
        pay = s.get(Payment, local_payment_id)
        if pay:
            pay.operation_id = payment_id
            pay.status = payment_status or "pending"
            s.add(pay)
            s.commit()

    return {"payment_url": confirmation_url, "label": label, "payment_id": payment_id}


@app.post("/api/yookassa/notify")
async def yookassa_notify(request: Request):
    """Webhook YooKassa. Начисляет токены только после проверки платежа через API YooKassa."""
    try:
        payload = await request.json()
    except Exception:
        logger.warning("YooKassa webhook: невалидный JSON")
        return PlainTextResponse("OK", status_code=200)

    event = payload.get("event", "")
    obj = payload.get("object") or {}
    payment_id = obj.get("id", "")
    if not payment_id:
        logger.warning("YooKassa webhook без payment id: %s", payload)
        return PlainTextResponse("OK", status_code=200)

    try:
        yk_payment = await billing.get_payment(payment_id)
    except billing.YooKassaError as exc:
        logger.warning("Не удалось проверить платёж YooKassa %s: %s", payment_id, exc)
        return PlainTextResponse("retry", status_code=500)

    metadata = yk_payment.get("metadata") or obj.get("metadata") or {}
    label = metadata.get("label", "")
    status = yk_payment.get("status", "")
    paid = yk_payment.get("paid") is True

    with session() as s:
        pay = s.exec(select(Payment).where(Payment.operation_id == payment_id)).first()
        if not pay and label:
            pay = s.exec(select(Payment).where(Payment.label == label)).first()
        if not pay:
            logger.warning("YooKassa webhook: локальный платёж не найден, payment_id=%s, label=%s", payment_id, label)
            return PlainTextResponse("OK", status_code=200)

        pay.operation_id = payment_id

        if event == "payment.canceled" or status == "canceled":
            if pay.status != "paid":
                pay.status = "canceled"
                s.add(pay)
                s.commit()
            return PlainTextResponse("OK", status_code=200)

        if event != "payment.succeeded" or status != "succeeded" or not paid:
            pay.status = status or pay.status
            s.add(pay)
            s.commit()
            return PlainTextResponse("OK", status_code=200)

        try:
            actual_amount = round(float((yk_payment.get("amount") or {}).get("value", 0)), 2)
        except Exception:
            actual_amount = 0
        expected_amount = round(float(pay.rub), 2)
        if actual_amount != expected_amount:
            logger.error(
                "YooKassa webhook: сумма не совпала, payment_id=%s, actual=%s, expected=%s",
                payment_id, actual_amount, expected_amount,
            )
            return PlainTextResponse("OK", status_code=200)

        if pay.status != "paid":
            pay.status = "paid"
            pay.paid_at = datetime.utcnow()
            u = s.get(User, pay.user_id)
            if u:
                u.token_balance += pay.tokens
                s.add(u)
            s.add(pay)
            s.commit()
            logger.info("Платёж YooKassa зачтён: пользователь %s +%s токенов", pay.user_id, pay.tokens)
            _activate_subscription(s, pay, yk_payment)
            credited_user_id = pay.user_id
        else:
            credited_user_id = None

    if credited_user_id:
        # См. tasks.resume_starved_channels -- пробуем сдвинуть просроченные
        # по расписанию каналы сразу, не дожидаясь планового тика.
        try:
            await tasks.resume_starved_channels(credited_user_id)
        except Exception as e:
            logger.warning(f"resume_starved_channels после webhook-начисления: {e}")

    return PlainTextResponse("OK", status_code=200)


# ── Раздача сайта ─────────────────────────────────────────────

@app.delete("/api/me")
def delete_account(user: User = Depends(current_user)):
    from database import ChannelRule, Subscription
    import uuid as _uuid
    uid = user.id
    correlation_id = _uuid.uuid4().hex[:12]
    log_prefix = f"[delete_account#{correlation_id}] uid={uid}"

    def _fail(step: str, e: Exception):
        logger.error(
            f"{log_prefix} ОШИБКА на шаге «{step}»: "
            f"exception_type={type(e).__name__} repr={repr(e)} "
            f"orig={repr(getattr(e, 'orig', None))}"
        )
        raise HTTPException(
            500,
            f"Не удалось удалить аккаунт. Обновите страницу и попробуйте ещё раз. (код: {correlation_id})"
        )

    try:
        with session() as s:
            chans = s.exec(select(Channel).where(Channel.user_id == uid)).all()
            chan_ids = [c.id for c in chans]
            logger.info(f"{log_prefix} шаг 1: найдено channels={len(chan_ids)}")
    except Exception as e:
        _fail("чтение channels", e)

    # КРИТИЧНО (настоящий root cause красной всплывашки): PostApproval
    # (режим "публикация после подтверждения") хранит настоящий FK
    # post_id -> post.id (unique) и channel_id -> channel.id. Она создаётся
    # для КАЖДОГО поста в режиме auto_publish=False (это основной сценарий
    # после фикса soft-control этой же сессии) -- то есть у любого активного
    # пользователя почти наверняка есть такие строки. Без очистки ДО
    # удаления Post ниже падает ForeignKeyViolation
    # "postapproval_post_id_fkey" -- именно тот 500, который видит
    # пользователь (этот шаг не имеет fallback, в отличие от шага 7).
    approvals_count = 0
    try:
        with session() as s:
            for ch in chans:
                for pa in s.exec(select(PostApproval).where(PostApproval.channel_id == ch.id)).all():
                    s.delete(pa); approvals_count += 1
            s.commit()
            logger.info(f"{log_prefix} шаг 1.5: удалены PostApproval={approvals_count}")
    except Exception as e:
        _fail("удаление PostApproval", e)

    posts_count = sources_count = rules_count = 0
    try:
        with session() as s:
            for ch in chans:
                for p in s.exec(select(Post).where(Post.channel_id == ch.id)).all():
                    s.delete(p); posts_count += 1
                for src in s.exec(select(Source).where(Source.channel_id == ch.id)).all():
                    s.delete(src); sources_count += 1
                for r in s.exec(select(ChannelRule).where(ChannelRule.channel_id == ch.id)).all():
                    s.delete(r); rules_count += 1
            # Посты могут существовать и без явной привязки в цикле выше, если
            # модель Post хранит user_id напрямую -- подчищаем по user_id тоже.
            for p in s.exec(select(Post).where(Post.user_id == uid)).all():
                s.delete(p); posts_count += 1
            s.commit()
            logger.info(f"{log_prefix} шаг 2: удалены posts={posts_count} sources={sources_count} rules={rules_count}")
    except Exception as e:
        _fail("удаление posts/sources/channel_rules", e)

    try:
        with session() as s:
            for ch in chans:
                ch2 = s.get(Channel, ch.id)
                if ch2:
                    s.delete(ch2)
            s.commit()
            logger.info(f"{log_prefix} шаг 3: удалены channels={len(chan_ids)}")
    except Exception as e:
        _fail("удаление channels", e)

    try:
        with session() as s:
            payments_count = len(s.exec(select(Payment).where(Payment.user_id == uid)).all())
            s.exec(delete(Payment).where(Payment.user_id == uid))
            s.commit()
            logger.info(f"{log_prefix} шаг 4: удалены payments={payments_count}")
    except Exception as e:
        _fail("удаление payments", e)

    try:
        with session() as s:
            referrals_count = (
                len(s.exec(select(Referral).where(Referral.referrer_id == uid)).all())
                + len(s.exec(select(Referral).where(Referral.referred_id == uid)).all())
            )
            s.exec(delete(Referral).where(Referral.referrer_id == uid))
            s.exec(delete(Referral).where(Referral.referred_id == uid))
            s.commit()
            logger.info(f"{log_prefix} шаг 5: удалены referrals={referrals_count}")
    except Exception as e:
        _fail("удаление referrals", e)

    try:
        with session() as s:
            # КРИТИЧНО (root cause найден): User.referred_by -- это FK на
            # user.id у ДРУГИХ пользователей (тех, кто зарегистрировался по
            # реферальному коду этого аккаунта). На Postgres это настоящий
            # FK constraint -- попытка удалить юзера, на которого ссылается
            # чужая строка через referred_by, падает с ForeignKeyViolation.
            # Локальный тест на SQLite этого не поймал, потому что SQLite по
            # умолчанию не enforces FK constraints — баг проявлялся только
            # на реальных продовых аккаунтах, у которых есть рефералы.
            # Обнуляем ссылку (не удаляем самих рефералов, они остаются
            # обычными пользователями, просто без привязки к удалённому
            # пригласившему).
            referred_users = s.exec(select(User).where(User.referred_by == uid)).all()
            for ru in referred_users:
                ru.referred_by = None
                s.add(ru)
            s.commit()
            logger.info(f"{log_prefix} шаг 6: обнулён referred_by у {len(referred_users)} пользователей, которых пригласил uid={uid}")
    except Exception as e:
        _fail("обнуление referred_by у приглашённых пользователей", e)

    # КРИТИЧНО (настоящий root cause, найден по реальному логу Railway):
    # реальная ошибка была
    #   "update or delete on table user violates foreign key constraint
    #    idempotencykey_user_id_fkey ... Key (id)=(21) is still referenced
    #    from table idempotencykey"
    # Очистка IdempotencyKey раньше стояла ПОСЛЕ удаления User (шаг 8) --
    # это и было причиной FK violation: Postgres не разрешает удалить
    # родительскую строку, пока есть ссылающиеся дочерние. Переносим этот
    # шаг ДО удаления User. Чистим и по user_id (это и есть constraint,
    # который реально нарушался), и по channel_id (на случай записей без
    # явного user_id или рассинхрона).
    try:
        with session() as s:
            removed = 0
            for k in s.exec(select(IdempotencyKey).where(IdempotencyKey.user_id == uid)).all():
                s.delete(k); removed += 1
            for cid in chan_ids:
                for k in s.exec(select(IdempotencyKey).where(IdempotencyKey.channel_id == cid)).all():
                    s.delete(k); removed += 1
            s.commit()
            logger.info(f"{log_prefix} шаг 6.5: IdempotencyKey очищены ДО удаления User: {removed}")
    except Exception as e:
        logger.warning(f"{log_prefix} шаг 6.5 (IdempotencyKey) не удался: exception_type={type(e).__name__} repr={repr(e)} orig={repr(getattr(e, 'orig', None))}")
        # НЕ критично само по себе как шаг, НО если эта очистка не сработала
        # (например другая ошибка), то шаг 7 (удаление User) ниже всё равно
        # упадёт с тем же FK violation -- лог покажет это явно на шаге 7.

    # КРИТИЧНО (тот же класс бага, что и IdempotencyKey выше): TelegramIdentity
    # (добавлена для one-tap входа в Mini App через /start) хранит настоящий
    # FK user_id -> user.id. Без очистки удаление User с привязанным Telegram
    # id падает с тем же ForeignKeyViolation, что раньше был на IdempotencyKey.
    # LandingEvent/ProductEvent/TrafficAttribution намеренно без FK (см. их
    # докстринги) -- их чистить не нужно.
    try:
        with session() as s:
            removed = 0
            for ti in s.exec(select(TelegramIdentity).where(TelegramIdentity.user_id == uid)).all():
                s.delete(ti); removed += 1
            s.commit()
            logger.info(f"{log_prefix} шаг 6.6: TelegramIdentity очищены ДО удаления User: {removed}")
    except Exception as e:
        logger.warning(f"{log_prefix} шаг 6.6 (TelegramIdentity) не удался: exception_type={type(e).__name__} repr={repr(e)} orig={repr(getattr(e, 'orig', None))}")

    # КРИТИЧНО (тот же класс бага четвёртый раз, найден тестом до прода):
    # Subscription.user_id -- настоящий FK на user.id. Пока
    # SUBSCRIPTION_ENABLED=false, строк в таблице нет и удаление проходит; в
    # день включения автосписаний любой подписчик, решивший удалить аккаунт,
    # получил бы ForeignKeyViolation на шаге 7 -- и, что хуже, fallback ниже
    # молча анонимизировал бы запись, вернув {"ok": true}. Подписка при этом
    # осталась бы жива вместе с payment_method_id, то есть автосписания
    # продолжили бы уходить с карты человека, который аккаунт удалил.
    # Удаляем строку целиком: платить больше не за что, а сохранённая карта
    # не должна пережить аккаунт.
    try:
        with session() as s:
            removed = 0
            for sub in s.exec(select(Subscription).where(Subscription.user_id == uid)).all():
                s.delete(sub); removed += 1
            s.commit()
            logger.info(f"{log_prefix} шаг 6.7: удалены Subscription={removed} (вместе с сохранённой картой)")
    except Exception as e:
        _fail("удаление Subscription", e)

    # PostFeedback: FK у таблицы намеренно нет (правило 3), поэтому падения на
    # шаге 7 она не вызовет. Но оценки постов -- это данные человека, и после
    # удаления аккаунта они остаться не должны: id пользователя в них хранится
    # прямым числом, а не обезличенной ссылкой.
    try:
        with session() as s:
            removed = 0
            for fb in s.exec(select(PostFeedback).where(PostFeedback.user_id == uid)).all():
                s.delete(fb); removed += 1
            s.commit()
            logger.info(f"{log_prefix} шаг 6.8: удалены PostFeedback={removed}")
    except Exception as e:
        _fail("удаление PostFeedback", e)

    try:
        with session() as s:
            u = s.get(User, uid)
            if u:
                s.delete(u)
                s.commit()
            logger.info(f"{log_prefix} шаг 7: пользователь удалён, ВСЁ ОСНОВНОЕ УДАЛЕНИЕ УСПЕШНО")
    except Exception as e:
        logger.error(
            f"{log_prefix} шаг 7 (hard delete) ПРОВАЛЕН: "
            f"exception_type={type(e).__name__} repr={repr(e)} orig={repr(getattr(e, 'orig', None))}"
        )
        # Fallback (task requirement): если есть FK constraint, который мы не
        # предусмотрели заранее (неизвестная таблица), не показываем
        # пользователю мёртвый отказ -- анонимизируем запись через уже
        # существующие поля вместо физического удаления строки. Это не
        # требует изменения схемы (новых колонок типа is_deleted), поэтому
        # безопасно деплоить прямо сейчас. Пользователь теряет доступ
        # (email больше не совпадает ни с одним логином), что эквивалентно
        # удалению аккаунта с точки зрения UX.
        try:
            with session() as s2:
                u2 = s2.get(User, uid)
                if u2:
                    anon_suffix = correlation_id
                    u2.email = f"deleted-{anon_suffix}@deleted.local"
                    u2.password_hash = "deleted"
                    u2.tg_chat_id = None
                    u2.tg_username = ""
                    u2.ref_code = f"DEL-{anon_suffix}"
                    s2.add(u2)
                    s2.commit()
                logger.warning(f"{log_prefix} шаг 7 fallback: запись анонимизирована (soft-delete через существующие поля), физическая строка User сохранена из-за неизвестного FK")
        except Exception as e2:
            logger.error(f"{log_prefix} шаг 7 fallback ТОЖЕ ПРОВАЛЕН: exception_type={type(e2).__name__} repr={repr(e2)}")
            _fail("удаление самого User (включая fallback-анонимизацию)", e)

    return {"ok": True}


@app.post("/api/verify_channel_only")
async def verify_channel_only(data: _VerifyIn, user: User = Depends(current_user)):
    chat = data.tg_chat.strip()
    if not chat:
        raise HTTPException(400, "Укажите @username канала")
    ok, message = await telegram_api.verify_channel(chat)
    return {"ok": ok, "message": message}


@app.patch("/api/me")
def patch_me(data: _MePatch, user: User = Depends(current_user)):
    with session() as s:
        u = s.get(User, user.id)
        if data.notify_new_post is not None: u.notify_new_post = data.notify_new_post
        if data.notify_published is not None: u.notify_published = data.notify_published
        if data.notify_approval_pending is not None: u.notify_approval_pending = data.notify_approval_pending
        if data.notify_low_tokens is not None: u.notify_low_tokens = data.notify_low_tokens
        if data.tg_chat_id is not None: u.tg_chat_id = data.tg_chat_id
        s.add(u); s.commit()
    return {"ok": True}


@app.post("/api/channels/{channel_id}/consult")
async def consult_channel(channel_id: int, data: _ConsultIn, user: User = Depends(current_user)):
    with session() as s:
        ch = s.get(Channel, channel_id)
        if not ch or ch.user_id != user.id:
            raise HTTPException(404, "Канал не найден")
        from database import ChannelRule
        from sqlmodel import select as sel
        rules = s.exec(sel(ChannelRule).where(ChannelRule.channel_id == channel_id)).all()
        rules_text = "\n".join(f"- {r.rule_text}" for r in rules)
    response, suggested_rule = await generator.consult(ch, data.message, data.history, rules_text)
    return {"response": response, "suggested_rule": suggested_rule}


@app.get("/api/channels/{channel_id}/rules")
def list_rules(channel_id: int, user: User = Depends(current_user)):
    from database import ChannelRule
    from sqlmodel import select as sel
    with session() as s:
        ch = s.get(Channel, channel_id)
        if not ch or ch.user_id != user.id:
            raise HTTPException(404, "Канал не найден")
        rules = s.exec(sel(ChannelRule).where(ChannelRule.channel_id == channel_id)).all()
        return [{"id": r.id, "rule_text": r.rule_text, "created_at": str(r.created_at)} for r in rules]


@app.post("/api/channels/{channel_id}/rules")
def add_rule(channel_id: int, data: _RuleIn, user: User = Depends(current_user)):
    from database import ChannelRule
    with session() as s:
        ch = s.get(Channel, channel_id)
        if not ch or ch.user_id != user.id:
            raise HTTPException(404, "Канал не найден")
        rule = ChannelRule(channel_id=channel_id, rule_text=data.rule_text.strip())
        s.add(rule); s.commit(); s.refresh(rule)
        return {"id": rule.id, "rule_text": rule.rule_text}


@app.delete("/api/rules/{rule_id}")
def delete_rule(rule_id: int, user: User = Depends(current_user)):
    from database import ChannelRule
    with session() as s:
        rule = s.get(ChannelRule, rule_id)
        if not rule:
            raise HTTPException(404, "Правило не найдено")
        ch = s.get(Channel, rule.channel_id)
        if not ch or ch.user_id != user.id:
            raise HTTPException(403, "Нет доступа")
        s.delete(rule); s.commit()
    return {"ok": True}


@app.post("/api/bot/start")
async def bot_start(request: Request):
    """Webhook для получения /start от бота — привязывает tg_chat_id к аккаунту."""
    try:
        data = await request.json()
        message = data.get("message", {})
        text = message.get("text", "")
        chat_id = message.get("chat", {}).get("id")
        if text.startswith("/start") and chat_id:
            parts = text.split()
            if len(parts) > 1 and parts[1].startswith("u"):
                user_id = int(parts[1][1:])
                with session() as s:
                    u = s.get(User, user_id)
                    if u:
                        u.tg_chat_id = chat_id
                        s.add(u); s.commit()
    except Exception:
        pass
    return {"ok": True}


class _TgVerifyIn(_BaseModel):
    username: str

@app.post("/api/me/verify_tg")
async def verify_tg_username(data: _TgVerifyIn, user: User = Depends(current_user)):
    """Пробует отправить тестовое сообщение пользователю по username."""
    username = data.username.strip()
    if not username.startswith("@"):
        username = "@" + username
    # Bot API не поддерживает отправку по username для личных чатов.
    # Сохраняем username и инструктируем пользователя написать /start боту.
    with session() as s:
        u = s.get(User, user.id)
        u.tg_username = username
        s.add(u); s.commit()
    bot_link = f"https://t.me/{config.TELEGRAM_BOT_USERNAME or 'trpst_bot'}?start=u{user.id}"
    return {
        "ok": True,
        "message": f"Username сохранён. Для активации уведомлений напиши /start боту — он свяжет аккаунты автоматически.",
        "bot_link": bot_link
    }


# КРИТИЧНО: HTML-страницы отдаём с запретом кэширования.
#
# Версии в ссылках на скрипты (?v=20260726c) обновляют ТОЛЬКО сами скрипты, и
# то лишь при условии, что браузер перечитал index.html. Сам index.html
# отдавался вообще без заголовков кэша -- Safari и мобильные браузеры держат
# такую страницу у себя сколь угодно долго и продолжают запрашивать СТАРЫЕ
# версии скриптов. В результате свежий деплой пользователю просто не виден, и
# никакая смена ?v= этого не пробивает: старый index.html о новой версии не
# знает. Ровно так новый блок на странице тарифов не доехал до пользователя.
#
# no-cache (а не no-store) -- страница по-прежнему может отдаваться как 304
# по ETag, если реально не менялась, то есть трафик не растёт.
_HTML_NO_CACHE = {
    "Cache-Control": "no-cache, must-revalidate",
    "Pragma": "no-cache",
    "Expires": "0",
}


def _html(path: str) -> FileResponse:
    return FileResponse(path, headers=_HTML_NO_CACHE)


@app.get("/legal/offer")
def legal_offer():
    return _html("static/legal/offer.html")

@app.get("/legal/privacy")
def legal_privacy():
    return _html("static/legal/privacy.html")

@app.get("/legal/refund")
def legal_refund():
    return _html("static/legal/refund.html")


@app.get("/landing")
def landing():
    return _html("static/landing.html")

@app.get("/how-to")
def how_to():
    return _html("static/how-to.html")

@app.get("/robots.txt")
def robots_txt():
    return FileResponse("static/robots.txt", media_type="text/plain")

@app.get("/sitemap.xml")
def sitemap_xml():
    return FileResponse("static/sitemap.xml", media_type="application/xml")

@app.get("/")
def index():
    return _html("static/index.html")


app.mount("/static", StaticFiles(directory="static"), name="static")


# ── Запуск ────────────────────────────────────────────────────

if __name__ == "__main__":
    import os
    uvicorn.run("main:app", host="0.0.0.0", port=int(os.getenv("PORT", 8000)), reload=False)
