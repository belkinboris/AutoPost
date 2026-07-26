"""Конфигурация из переменных окружения."""

import os, json

try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:
    pass

SECRET_KEY = os.getenv("SECRET_KEY", "change-me-set-long-random-string")
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
ANTHROPIC_MODEL = os.getenv("ANTHROPIC_MODEL", "claude-sonnet-4-6")

# === LLM-провайдер (миграция в РФ, план от 2026-07-17) ===
# "anthropic" (по умолчанию, текущее поведение) или "yandex" (Alice AI /
# Foundation Models в Yandex Cloud). Переключение одной переменной,
# мгновенный откат. См. DEPLOY_NOTE и generator.py::_call_llm.
LLM_PROVIDER = os.getenv("LLM_PROVIDER", "anthropic").strip().lower()
YANDEX_API_KEY = os.getenv("YANDEX_API_KEY", "")       # Api-Key сервисного аккаунта Yandex Cloud
YANDEX_FOLDER_ID = os.getenv("YANDEX_FOLDER_ID", "")   # id каталога (folder) в Yandex Cloud
# URI модели. Точное имя модели Alice AI смотрим в консоли Foundation Models
# и задаём через env; дефолт — актуальная général-модель каталога.
# "native" (YandexGPT через foundationModels API) или "openai"
# (открытые модели DeepSeek/Qwen через OpenAI-совместимый эндпоинт).
YANDEX_API_MODE = os.getenv("YANDEX_API_MODE", "native").strip().lower()
# Температура генерации постов (0..1). 0.7 -- живой текст без хаоса.
# 0.3 даёт сухие шаблонные посты, 0.9+ -- риск бессвязности.
LLM_TEMPERATURE = float(os.getenv("LLM_TEMPERATURE", "0.7"))

# Релей до Telegram Bot API через Cloudflare Workers (см. telegram_api.py).
# Пусто по умолчанию = прямой api.telegram.org, поведение не меняется, пока
# явно не задать адрес воркера. Формат: https://твой-воркер.workers.dev
TELEGRAM_API_BASE = os.getenv("TELEGRAM_API_BASE", "").strip()
YANDEX_MODEL_URI = os.getenv("YANDEX_MODEL_URI") or (
    f"gpt://{YANDEX_FOLDER_ID}/yandexgpt/latest" if YANDEX_FOLDER_ID else ""
)

# === Яндекс.Поиск (фаза 1.5) — замена web_search для новостных каналов ===
# Отдельный сервис Yandex Cloud (Search API v2, синхронный режим). Ключ по
# умолчанию тот же Api-Key, что и для Foundation Models, но сервисному
# аккаунту нужна ДОПОЛНИТЕЛЬНАЯ роль search-api.webSearch.user на каталог.
# Тарифицируется за запрос; для внутреннего учёта конвертируем в токены.
YANDEX_SEARCH_ENABLED = os.getenv("YANDEX_SEARCH_ENABLED", "true").lower() in {"1", "true", "yes", "on"}
YANDEX_SEARCH_API_KEY = os.getenv("YANDEX_SEARCH_API_KEY") or YANDEX_API_KEY
YANDEX_SEARCH_MAX_RESULTS = int(os.getenv("YANDEX_SEARCH_MAX_RESULTS", "8"))
# Условная стоимость одного поискового запроса во внутренних токенах
# (для списания с баланса пользователя, по аналогии с web_search у Claude).
YANDEX_SEARCH_TOKEN_COST = int(os.getenv("YANDEX_SEARCH_TOKEN_COST", "3000"))
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_BOT_USERNAME = os.getenv("TELEGRAM_BOT_USERNAME", "")
# @maintrpost_bot -- вход в Mini App (Main Mini App режим включён в BotFather).
# Отдельный бот от TELEGRAM_BOT_TOKEN (который управляет @Trpst_bot, publishing bot).
MAIN_BOT_TOKEN = os.getenv("MAIN_BOT_TOKEN", "")
MAIN_BOT_USERNAME = os.getenv("MAIN_BOT_USERNAME", "maintrpost_bot")
# Режим публикации "публикация после подтверждения" (Channel.auto_publish=
# False): сколько минут висит карточка с кнопками в личке владельца канала,
# прежде чем пост публикуется автоматически, если не было реакции.
SOFT_CONTROL_APPROVAL_MINUTES = int(os.getenv("SOFT_CONTROL_APPROVAL_MINUTES", "30"))
# Финальный буфер перед самой публикацией (и после дедлайна, и после
# только что присланной правки текста) -- последний шанс отменить/
# поправить, вместо мгновенной необратимой публикации в канал.
SOFT_CONTROL_FINAL_GRACE_SECONDS = int(os.getenv("SOFT_CONTROL_FINAL_GRACE_SECONDS", "60"))
DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./postbot.db")
PUBLIC_URL = os.getenv("PUBLIC_URL") or (
    f"https://{os.getenv('RAILWAY_PUBLIC_DOMAIN')}"
    if os.getenv("RAILWAY_PUBLIC_DOMAIN") else "http://localhost:8000"
)

# YooKassa API
YOOKASSA_SHOP_ID = os.getenv("YOOKASSA_SHOP_ID", "")
YOOKASSA_SECRET_KEY = os.getenv("YOOKASSA_SECRET_KEY", "")
YOOKASSA_RETURN_URL = os.getenv("YOOKASSA_RETURN_URL", f"{PUBLIC_URL}/?paid=1")
YOOKASSA_SEND_RECEIPT = os.getenv("YOOKASSA_SEND_RECEIPT", "false").lower() in {"1", "true", "yes", "on"}
YOOKASSA_VAT_CODE = int(os.getenv("YOOKASSA_VAT_CODE", "1"))  # 1 = без НДС

# Токенов в подарок новому пользователю
WELCOME_TOKENS = int(os.getenv("WELCOME_TOKENS", "200000"))  # бесплатная квота для новых пользователей
# Бонус за реферала (обоим)
REFERRAL_BONUS_TOKENS = int(os.getenv("REFERRAL_BONUS_TOKENS", "200000"))  # по 200к и пригласившему, и приглашённому

TICK_SECONDS = int(os.getenv("TICK_SECONDS", "60"))
# Отдельный, более частый интервал специально для /start у @maintrpost_bot
# (P1 fix) -- интерактивный сценарий не может ждать 60с как генерация постов.
MAIN_BOT_POLL_SECONDS = int(os.getenv("MAIN_BOT_POLL_SECONDS", "3"))

# ── Планы подписки ────────────────────────────────────────────────
# Лимит в токенах. Пост стоит 20 000–40 000 токенов в зависимости от сложности.
# Пользователю показываем ДИАПАЗОН постов: токены / 40k (мин) … токены / 20k (макс)
# Объёмы токенов рассчитаны от РЕАЛЬНОЙ себестоимости, снятой с биллинга
# Yandex Cloud (DeepSeek v4 flash: вход 300 ₽/млн, выход 500 ₽/млн, кеш
# 75 ₽/млн; при фактическом соотношении вход:выход 3.4:1 смешанная цена
# ~334 ₽/млн). Каждый тариф держит ~56% маржи ДАЖЕ если подписчик выберет
# все токены до последнего. Прежние объёмы этого не обеспечивали: на входном
# тарифе токены съедали 82% выручки.
PLANS = {
    "free":     {"title": "Бесплатно",  "rub": 0,     "channels": 1,  "tokens": 120_000},    # 3–6 постов
    "starter":  {"title": "Старт",      "rub": 490,   "channels": 1,  "tokens": 600_000},    # 15–30 постов
    "pro":      {"title": "Про",        "rub": 990,   "channels": 3,  "tokens": 1_200_000},  # 30–60 постов
    "business": {"title": "Бизнес",     "rub": 2490,  "channels": 10, "tokens": 3_000_000},  # 75–150 постов
    "agency":   {"title": "Агентство",  "rub": 4990,  "channels": 0,  "tokens": 6_000_000},  # 150–300 постов
}

# Стоимость поста в токенах (для расчёта диапазона)
POST_TOKENS_MIN = 20_000   # простой пост
POST_TOKENS_MAX = 40_000   # сложный пост (много промптов, web_search, история)

# Тарифы подписки (списываются через YooKassa раз в SUBSCRIPTION_PERIOD_DAYS,
# см. billing.charge_recurring и tasks.charge_due_subscriptions).
# rub_regular -- цена после окончания стартового периода. Показывается
# зачёркнутой рядом с текущей. ЭТО НЕ «БЫЛО»: по этой цене никто ещё не
# платил, поэтому и подписана она как будущая («потом»), а не как прошлая.
# Выдавать не существовавшую прошлую цену за настоящую -- это вводящая в
# заблуждение реклама (ст. 5 ФЗ «О рекламе»), и делать так нельзя.
# Чтобы убрать плашку акции -- достаточно убрать rub_regular у тарифов.
_DEFAULT_PACKAGES = [
    {"id": "p1", "title": "Старт",     "rub": 490,  "rub_regular": 990,  "tokens": 600_000},    # 15–30 постов
    {"id": "p2", "title": "Про",       "rub": 990,  "rub_regular": 1990, "tokens": 1_200_000},  # 30–60 постов
    {"id": "p3", "title": "Бизнес",    "rub": 2490, "rub_regular": 4990, "tokens": 3_000_000},  # 75–150 постов
    {"id": "p4", "title": "Агентство", "rub": 4990, "rub_regular": 9990, "tokens": 6_000_000},  # 150–300 постов
]

# ── Подписка ──────────────────────────────────────────────────────
# Период между автосписаниями. Календарный месяц не используем осознанно:
# фиксированные 30 дней дают предсказуемую дату и не создают краевых случаев
# с 29–31 числом.
SUBSCRIPTION_PERIOD_DAYS = int(os.getenv("SUBSCRIPTION_PERIOD_DAYS", "30"))
# Сколько раз подряд пробуем списать, прежде чем приостановить подписку.
SUBSCRIPTION_MAX_FAILS = int(os.getenv("SUBSCRIPTION_MAX_FAILS", "3"))
# Пауза между повторными попытками списания после неудачи.
SUBSCRIPTION_RETRY_HOURS = int(os.getenv("SUBSCRIPTION_RETRY_HOURS", "24"))
try:
    TOKEN_PACKAGES = json.loads(os.getenv("TOKEN_PACKAGES", "")) or _DEFAULT_PACKAGES
except Exception:
    TOKEN_PACKAGES = _DEFAULT_PACKAGES


def package_by_id(pid: str):
    for p in TOKEN_PACKAGES:
        if p["id"] == pid:
            return p
    return None
