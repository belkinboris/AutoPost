"""
База данных: SQLModel + SQLAlchemy.
Postgres (Railway) или SQLite (локально).
"""

import logging
from datetime import datetime, timedelta
from typing import Optional
from sqlmodel import Field, SQLModel, create_engine, Session, select
from sqlalchemy import inspect, text
import config

logger = logging.getLogger("autopost")

db_url = config.DATABASE_URL
if db_url.startswith("postgres://"):
    db_url = db_url.replace("postgres://", "postgresql://", 1)

connect_args = {"check_same_thread": False} if db_url.startswith("sqlite") else {}
engine = create_engine(db_url, echo=False, connect_args=connect_args, pool_pre_ping=True)


class User(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    email: str = Field(index=True, unique=True)
    password_hash: str
    token_balance: int = 0
    is_admin: bool = False
    plan: str = "free"
    plan_posts_used: int = 0
    plan_reset_at: Optional[datetime] = None
    ref_code: str = ""
    referred_by: Optional[int] = Field(default=None, foreign_key="user.id")
    ref_bonus_given: bool = False
    # Telegram уведомления
    tg_chat_id: Optional[int] = None       # числовой id чата с ботом (из /start)
    tg_username: str = ""                   # для отображения
    notify_new_post: bool = False
    notify_published: bool = False
    notify_low_tokens: bool = True
    # Отдельный тумблер (решение владельца 02.08, C14): пинг за
    # SOFT_CONTROL_WARNING_MINUTES до переноса неподтверждённого поста в
    # конец очереди. Не переиспользует notify_new_post/notify_published --
    # это другое по смыслу событие ("нужно решение", а не "решение принято
    # за вас").
    notify_approval_pending: bool = False
    created_at: datetime = Field(default_factory=datetime.utcnow)


class Channel(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    user_id: int = Field(foreign_key="user.id", index=True)

    title: str
    tg_chat: str = ""
    verified: bool = False

    about: str = ""
    style: str = ""
    style_profile: str = ""
    post_length: str = "100-200 слов"
    language: str = "русский"

    post_voice: str = "author"
    post_format: str = "story"
    emoji_style: str = "minimal"
    cta_enabled: bool = False
    cta_text: str = ""

    use_web_search: bool = True
    auto_publish: bool = False

    schedule_kind: str = "interval"
    interval_hours: float = 12.0           # float: 0.25=15мин, 0.5=30мин, 1, 3, 6...
    interval_jitter_minutes: int = 0       # ±N минут рандомизации
    publish_window_start: str = ""         # "09:00" — начало окна публикации
    publish_window_end: str = ""           # "22:00" — конец окна
    daily_times: str = '["10:00"]'

    channel_type: str = "thematic"  # "thematic" или "news"
    enabled: bool = True
    onboarded: bool = False
    last_generated_at: Optional[datetime] = None
    created_at: datetime = Field(default_factory=datetime.utcnow)
    # Настраиваемая глубина очереди (C14, решение владельца 01.08): базово
    # держим MIN_QUEUE постов, пользователь может увеличить до потолка своего
    # тарифа (PAID_QUEUE=7 оплатившим, tasks.queue_target_for_user). None --
    # старое поведение без явного выбора: используется весь потолок тарифа
    # целиком, как до появления этой настройки.
    queue_depth: Optional[int] = None
    # Момент начала текущей генерации поста (C14, пункт 6 из видения
    # владельца 01.08: "генерируется следующий пост" под последним постом
    # очереди). Выставляется в начале tasks.generate_for_channel и снимается
    # в конце (см. _set_generating) -- отражает реальную работу на сервере,
    # а не декоративный таймер: если генерация упадёт по исключению, флаг
    # снимется всё равно (try/finally), а по возрасту больше нескольких
    # минут фронт и API считают его протухшим (сервер мог перезапуститься
    # посреди генерации) -- см. _channel_dict.
    generating_since: Optional[datetime] = None


class ChannelRule(SQLModel, table=True):
    """Персональные правила стиля канала из диалога с ИИ-консультантом."""
    id: Optional[int] = Field(default=None, primary_key=True)
    channel_id: int = Field(foreign_key="channel.id", index=True)
    rule_text: str
    created_at: datetime = Field(default_factory=datetime.utcnow)


class Source(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    channel_id: int = Field(foreign_key="channel.id", index=True)
    url: str
    enabled: bool = True


class Post(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    channel_id: int = Field(foreign_key="channel.id", index=True)
    user_id: int = Field(foreign_key="user.id", index=True)

    text: str
    status: str = "pending"
    scheduled_at: Optional[datetime] = None
    published_at: Optional[datetime] = None
    tg_message_id: Optional[int] = None
    tokens_used: int = 0
    post_format: str = ""
    created_at: datetime = Field(default_factory=datetime.utcnow)
    # Момент, когда пост не подтвердили вовремя и tasks._requeue_unconfirmed_post
    # перенёс его в конец очереди с новым scheduled_at (C14, решение владельца
    # 01-02.08). Нужна отдельная колонка, а не просто новый approval-цикл --
    # фронт должен показать красную плашку "не подтвердили вовремя", а не
    # молча выдать пост за обычный новый в очереди.
    requeued_at: Optional[datetime] = None


class PostApproval(SQLModel, table=True):
    """
    Состояние поста в режиме "публикация после подтверждения"
    (Channel.auto_publish=False). Пока для поста есть строка здесь со
    статусом "waiting" и deadline в будущем -- в личке владельца канала
    висит карточка с кнопками "Опубликовать сейчас" / "Отклонить" /
    "Редактировать". deadline -- это же самое время, что и Post.scheduled_at
    (единая модель очереди, C14, решение владельца 01-02.08): пост либо
    подтверждают до этого момента, либо он переносится в конец очереди
    (tasks._requeue_unconfirmed_post), а НЕ публикуется молча по таймеру.

    status: waiting (таймер идёт) | awaiting_edit (ждём новый текст
    ответным сообщением) | done (решено -- опубликован/отклонён/устарел/
    перенесён в конец очереди новой строкой).

    final_warning_sent: за SOFT_CONTROL_WARNING_MINUTES до deadline tick()
    присылает жёлтое предупреждение "не подтвердите -- перенесём в конец
    очереди" и выставляет этот флаг, чтобы не слать его повторно. Разовое
    уведомление в Telegram (не только правка карточки) -- по отдельному
    тумблеру User.notify_approval_pending.

    Новая отдельная таблица -- та же безопасная схема, что LandingEvent/
    TrafficAttribution/IdempotencyKey: создаётся через create_all(), без
    ALTER TABLE на Post/Channel.
    """
    id: Optional[int] = Field(default=None, primary_key=True)
    post_id: int = Field(foreign_key="post.id", index=True, unique=True)
    channel_id: int = Field(foreign_key="channel.id", index=True)
    review_chat_id: int
    review_message_id: Optional[int] = None
    deadline: datetime
    status: str = "waiting"
    final_warning_sent: bool = False
    created_at: datetime = Field(default_factory=datetime.utcnow)


class TelegramIdentity(SQLModel, table=True):
    """
    Связывает Telegram-пользователя (id из initData Telegram Mini App) с
    аккаунтом АвтоПост -- позволяет войти в Mini App без email/пароля,
    одним нажатием ("Продолжить как <имя>"), см. POST /api/auth/telegram_miniapp.

    tg_user_id -- это тот же числовой id, что Telegram использует как
    chat_id личного чата с ботом, поэтому при автосоздании аккаунта он же
    пишется в User.tg_chat_id -- уведомления от бота-публикатора начинают
    работать сразу, без отдельного шага "подключить Telegram" в настройках.

    Новая отдельная таблица -- та же безопасная схема, что PostApproval/
    LandingEvent/TrafficAttribution: создаётся через create_all(), без
    ALTER TABLE на User.
    """
    id: Optional[int] = Field(default=None, primary_key=True)
    tg_user_id: int = Field(index=True, unique=True)
    user_id: int = Field(foreign_key="user.id", index=True)
    tg_username: str = ""
    created_at: datetime = Field(default_factory=datetime.utcnow)


class Payment(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    user_id: int = Field(foreign_key="user.id", index=True)
    package_id: str
    label: str = Field(index=True)
    rub: float
    tokens: int
    status: str = "pending"
    operation_id: str = ""
    created_at: datetime = Field(default_factory=datetime.utcnow)
    paid_at: Optional[datetime] = None


class Referral(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    referrer_id: int = Field(foreign_key="user.id", index=True)
    referred_id: int = Field(foreign_key="user.id")
    bonus_tokens: int = 0
    created_at: datetime = Field(default_factory=datetime.utcnow)


class LandingEvent(SQLModel, table=True):
    """
    Журнал событий пути landing -> Telegram/web -> registration.
    Только для диагностики воронки (CTA/Journey Diagnostics) -- не используется
    в основной бизнес-логике продукта, не влияет на работу приложения.
    Read-only снаружи: пишется через POST /api/landing-event, читается через
    GET /api/internal/landing-funnel-diagnostics.
    """
    id: Optional[int] = Field(default=None, primary_key=True)
    session_id: str = Field(index=True)          # landing_session_id из localStorage/cookie
    event: str = Field(index=True)                 # landing_view, cta_hero_bot_click, bot_start_from_landing, register_success...
    user_id: Optional[int] = None                   # если событие связано с конкретным юзером (register_success) -- без FK, чисто для диагностики
    url: str = ""
    utm_source: str = ""
    utm_medium: str = ""
    utm_campaign: str = ""
    yclid: str = ""
    user_agent: str = ""
    created_at: datetime = Field(default_factory=datetime.utcnow, index=True)


class ProductEvent(SQLModel, table=True):
    """
    Журнал product-events после регистрации, для диагностики payment path:
    почему пользователи доходят до генерации постов, но не доходят до оплаты.

    Минимальная версия -- без source attribution (Yandex/Telegram Ads), без
    test-user exclusion, без allowlist метаданных. Если эти возможности
    понадобятся позже -- добавлять осознанно, отдельной задачей, не сейчас.

    Та же безопасная схема что LandingEvent/IdempotencyKey: новая таблица,
    создаётся через create_all() без ALTER TABLE существующих таблиц.
    user_id без FK -- аналитический журнал не должен ломать удаление аккаунта
    (см. прошлый продовый инцидент с IdempotencyKey -- тот же класс риска
    здесь предотвращён заранее).

    Read-only снаружи: пишется через POST /api/product-event, читается через
    GET /api/internal/payment-path-diagnostics.
    """
    id: Optional[int] = Field(default=None, primary_key=True)
    user_id: Optional[int] = Field(default=None, index=True)
    event: str = Field(index=True)  # pricing_viewed, payment_cta_clicked, payment_failed, payment_returned, quota_warning_seen, limit_reached
    package_id: str = ""
    created_at: datetime = Field(default_factory=datetime.utcnow, index=True)


class TrafficAttribution(SQLModel, table=True):
    """
    Источник трафика пользователя -- для разделения Telegram Ads vs Yandex
    Direct vs organic перед запуском Telegram Ads.

    Новая отдельная таблица (та же безопасная схема что LandingEvent/
    ProductEvent/IdempotencyKey): создаётся через create_all(), без ALTER
    TABLE на User или других существующих таблицах. User не трогаем.

    Источник определяется ДО регистрации (на лендинге через UTM, или в
    Telegram через /start параметр) и сохраняется здесь либо сразу с
    user_id (если регистрация уже произошла), либо позже привязывается
    к user_id по landing_session_id когда пользователь регистрируется.

    source: telegram_ads / yandex_direct / direct / unknown
    medium: cpc / organic / unknown
    campaign: utm_campaign либо campaign-часть start-параметра
    content: utm_content либо creative-часть start-параметра
    raw_start_param: сырой текст после /start (для отладки разбора, не
        показывается владельцу в обычных сообщениях -- только в diagnostics)

    Read-only снаружи: пишется через POST /api/landing-event (расширенный)
    и при /start у @maintrpost_bot, читается через source_breakdown в
    GET /api/internal/payment-path-diagnostics.
    """
    id: Optional[int] = Field(default=None, primary_key=True)
    user_id: Optional[int] = Field(default=None, index=True)
    landing_session_id: Optional[str] = Field(default=None, index=True)
    source: str = "unknown"   # telegram_ads / yandex_direct / direct / unknown
    medium: str = "unknown"   # cpc / organic / unknown
    campaign: str = ""
    content: str = ""
    raw_start_param: Optional[str] = None
    created_at: datetime = Field(default_factory=datetime.utcnow, index=True)


class PostFeedback(SQLModel, table=True):
    """
    Оценка поста автором: понравился / не понравился.

    Зачем: единственный вопрос, на который у нас нет ответа, -- насколько
    хороши посты на самом деле (C1 в PRODUCT_ROADMAP.md). Косвенные признаки
    врут: опубликованный пост мог быть опубликован «и так сойдёт», а
    отклонённый -- не подойти по теме, а не по качеству. Прямая оценка
    накапливается сама, пока человек и так разбирает очередь.

    Та же безопасная схема, что LandingEvent/ProductEvent/TrafficAttribution:
    новая таблица, создаётся через create_all() без ALTER TABLE на
    существующих. FK на post.id и user.id намеренно НЕТ -- иначе удаление
    аккаунта снова упрётся в чужую таблицу (правило 3 в CLAUDE.md: на этом
    уже четыре раза падал прод).

    Одна строка на пару (пользователь, пост): повторная оценка перезаписывает
    прежнюю, а не копит историю мнений -- считать среднее по нескольким
    оценкам одного человека бессмысленно.
    """
    id: Optional[int] = Field(default=None, primary_key=True)
    post_id: int = Field(index=True)
    user_id: int = Field(index=True)
    channel_id: Optional[int] = Field(default=None, index=True)
    verdict: str = Field(index=True)   # "up" | "down"
    created_at: datetime = Field(default_factory=datetime.utcnow, index=True)
    updated_at: datetime = Field(default_factory=datetime.utcnow)


class IdempotencyKey(SQLModel, table=True):
    """
    Защита от дублей при quick start (task item E): клиент генерирует
    client_request_id один раз на сессию онбординга, хранит в localStorage,
    передаёт при создании канала. Если запрос с тем же ключом приходит
    повторно (например после "Load failed" и повторного клика, или после
    случайного двойного сабмита формы) -- возвращаем уже созданный канал,
    а не создаём новый.

    Новая отдельная таблица -- безопасно создаётся через create_all(), не
    требует ALTER TABLE на существующих таблицах (Channel/User и т.д.).
    """
    id: Optional[int] = Field(default=None, primary_key=True)
    user_id: int = Field(foreign_key="user.id", index=True)
    client_request_id: str = Field(index=True)
    channel_id: int
    created_at: datetime = Field(default_factory=datetime.utcnow)


class Subscription(SQLModel, table=True):
    """
    Подписка на тариф: регулярное списание через YooKassa сохранённым методом
    оплаты (см. billing.charge_recurring и tasks.charge_due_subscriptions).

    Новая отдельная таблица -- та же безопасная схема, что PostApproval/
    TelegramIdentity/IdempotencyKey: создаётся через create_all(), без ALTER
    TABLE на User/Payment и других уже задеплоенных таблицах. В частности,
    payment_method_id намеренно живёт здесь, а не новой колонкой в Payment.

    status:
      active     -- списываем в next_charge_at
      cancelled  -- пользователь отменил, больше не списываем; оплаченный
                    период при этом не отбираем (см. next_charge_at)
      suspended  -- подряд не прошло SUBSCRIPTION_MAX_FAILS списаний
                    (нет денег/карта отвязана), автосписания прекращены

    last_period_key -- защита от двойного списания: ключ оплаченного периода
    (id подписки + порядковый номер периода). Перед списанием сверяем его и
    используем как Idempotence-Key для YooKassa, поэтому повторный запуск
    джобы или её параллельный инстанс не спишут деньги дважды.
    """
    id: Optional[int] = Field(default=None, primary_key=True)
    user_id: int = Field(foreign_key="user.id", index=True)
    package_id: str
    # Цена, зафиксированная при оформлении подписки. Продлеваем именно по
    # ней, а не по текущей цене из конфига: и оферта (п. 3), и плашка на
    # тарифах обещают, что цена оформления сохранится за подписчиком. Без
    # этого поля повышение цен молча подняло бы списания уже подписанным.
    price_rub: float = 0
    payment_method_id: str = Field(default="", index=True)
    # Человекочитаемое описание сохранённого способа оплаты («Банковская карта
    # •••• 4444»). Нужно, чтобы в кабинете было видно, ЧТО именно привязано и
    # что пользователь удаляет -- ЮKassa требует наглядный сценарий отвязки.
    # Реквизиты карты целиком мы не получаем и не храним, только маску.
    payment_method_title: str = ""
    status: str = Field(default="active", index=True)
    period_no: int = 1
    last_period_key: str = ""
    next_charge_at: Optional[datetime] = Field(default=None, index=True)
    fail_count: int = 0
    last_error: str = ""
    created_at: datetime = Field(default_factory=datetime.utcnow)
    cancelled_at: Optional[datetime] = None


def _add_missing_columns():
    """
    Точечный самолечащийся фикс уже случившегося дрейфа схемы -- НЕ общая
    миграционная система и не отмена правила "новая логика только через
    новые таблицы, без ALTER TABLE на существующих". Проблема: таблица
    postapproval была задеплоена БЕЗ колонки final_warning_sent (коммит
    "Режим публикации после подтверждения"), а колонка добавлена в модель
    только следующим коммитом ("Финальный буфер ~1 мин") в предположении,
    что таблица ещё не в проде -- предположение оказалось неверным, прод
    уже был задеплоен на тот момент. create_all() не добавляет колонки в
    существующие таблицы, поэтому каждый запрос к PostApproval падал с
    psycopg2.errors.UndefinedColumn -- ломая не только карточки каналов
    (/api/channels), но и tick() (due_post_approvals), то есть весь режим
    "публикация после подтверждения" целиком.

    Идемпотентно и безопасно на повторных запусках: проверяет колонку
    через inspector ПЕРЕД тем как пытаться её добавить.
    """
    try:
        inspector = inspect(engine)
        if "postapproval" not in inspector.get_table_names():
            return  # таблица ещё не создана -- create_all() выше создаст её сразу с колонкой
        existing_cols = {c["name"] for c in inspector.get_columns("postapproval")}
        if "final_warning_sent" not in existing_cols:
            with engine.begin() as conn:
                conn.execute(text(
                    "ALTER TABLE postapproval ADD COLUMN final_warning_sent BOOLEAN NOT NULL DEFAULT FALSE"
                ))
            logger.info("Миграция: добавлена колонка postapproval.final_warning_sent")
    except Exception:
        logger.exception("Миграция postapproval.final_warning_sent не удалась")

    # Subscription.price_rub -- зафиксированная цена подписки. Таблица
    # subscription могла быть уже создана предыдущим деплоем без этой
    # колонки, а create_all() колонки в существующие таблицы не добавляет.
    # Та же идемпотентная, проверяющая inspector'ом схема, что и выше.
    try:
        inspector = inspect(engine)
        if "subscription" in inspector.get_table_names():
            cols = {c["name"] for c in inspector.get_columns("subscription")}
            if "price_rub" not in cols:
                with engine.begin() as conn:
                    conn.execute(text(
                        "ALTER TABLE subscription ADD COLUMN price_rub DOUBLE PRECISION NOT NULL DEFAULT 0"
                        if engine.dialect.name == "postgresql"
                        else "ALTER TABLE subscription ADD COLUMN price_rub REAL NOT NULL DEFAULT 0"
                    ))
                logger.info("Миграция: добавлена колонка subscription.price_rub")
            if "payment_method_title" not in cols:
                with engine.begin() as conn:
                    conn.execute(text(
                        "ALTER TABLE subscription ADD COLUMN payment_method_title VARCHAR NOT NULL DEFAULT ''"
                    ))
                logger.info("Миграция: добавлена колонка subscription.payment_method_title")
    except Exception:
        logger.exception("Миграция subscription.price_rub не удалась")

    # User.notify_approval_pending -- новый тумблер уведомлений (C14, единая
    # модель очереди, 02.08). Таблица user точно уже задеплоена, поэтому
    # колонка идёт той же идемпотентной миграцией, а не только в модели.
    try:
        inspector = inspect(engine)
        if "user" in inspector.get_table_names():
            cols = {c["name"] for c in inspector.get_columns("user")}
            if "notify_approval_pending" not in cols:
                with engine.begin() as conn:
                    conn.execute(text(
                        "ALTER TABLE \"user\" ADD COLUMN notify_approval_pending BOOLEAN NOT NULL DEFAULT FALSE"
                    ))
                logger.info("Миграция: добавлена колонка user.notify_approval_pending")
    except Exception:
        logger.exception("Миграция user.notify_approval_pending не удалась")

    # Post.requeued_at -- метка "не подтвердили вовремя, перенесён в конец
    # очереди" (C14, единая модель очереди, 02.08), см. класс Post.
    try:
        inspector = inspect(engine)
        if "post" in inspector.get_table_names():
            cols = {c["name"] for c in inspector.get_columns("post")}
            if "requeued_at" not in cols:
                with engine.begin() as conn:
                    conn.execute(text(
                        "ALTER TABLE post ADD COLUMN requeued_at TIMESTAMP"
                    ))
                logger.info("Миграция: добавлена колонка post.requeued_at")
    except Exception:
        logger.exception("Миграция post.requeued_at не удалась")

    # Channel.queue_depth -- настраиваемая глубина очереди (C14, решение
    # владельца 01.08), см. класс Channel.
    try:
        inspector = inspect(engine)
        if "channel" in inspector.get_table_names():
            cols = {c["name"] for c in inspector.get_columns("channel")}
            if "queue_depth" not in cols:
                with engine.begin() as conn:
                    conn.execute(text(
                        "ALTER TABLE channel ADD COLUMN queue_depth INTEGER"
                    ))
                logger.info("Миграция: добавлена колонка channel.queue_depth")
    except Exception:
        logger.exception("Миграция channel.queue_depth не удалась")

    # Channel.generating_since -- индикатор "генерируется следующий пост"
    # (C14, решение владельца 01.08), см. класс Channel.
    try:
        inspector = inspect(engine)
        if "channel" in inspector.get_table_names():
            cols = {c["name"] for c in inspector.get_columns("channel")}
            if "generating_since" not in cols:
                with engine.begin() as conn:
                    conn.execute(text(
                        "ALTER TABLE channel ADD COLUMN generating_since TIMESTAMP"
                    ))
                logger.info("Миграция: добавлена колонка channel.generating_since")
    except Exception:
        logger.exception("Миграция channel.generating_since не удалась")


def init_db():
    SQLModel.metadata.create_all(engine)
    _add_missing_columns()


def session():
    return Session(engine)


def all_enabled_channels(s: Session) -> list[Channel]:
    return list(s.exec(select(Channel).where(Channel.enabled == True)).all())  # noqa


def due_scheduled_posts(s: Session, now: datetime) -> list[Post]:
    """
    Посты со статусом "scheduled", время которых пришло -- публикуются
    автоматически через tick(). Единая модель очереди (C14, 01-02.08): пост
    автопилота и пост в режиме подтверждения оба лежат в очереди со
    scheduled_at одинаково, разница только в том, нужно ли подтверждение
    прежде чем время наступит.

    КРИТИЧНО: фильтр по Channel.auto_publish, а НЕ только по отсутствию
    активного PostApproval. Пост в режиме подтверждения, которому не удалось
    доставить карточку в Telegram (нет tg_chat_id, бот заблокирован, сеть),
    вообще не получает строку PostApproval (см. tasks._send_approval_card) --
    если бы исключение держалось только на её наличии, такой пост молча
    опубликовался бы сам по достижении scheduled_at, ровно то, что правило 4
    в CLAUDE.md запрещает. Режим подтверждения публикует ТОЛЬКО по явному
    "Опубликовать" (см. /api/posts/{id}/publish) -- через tick он не
    публикуется никогда, только переносится в конец очереди
    (tasks._requeue_unconfirmed_post) или ждёт решения сколько угодно.

    Проверка по PostApproval.status=="waiting" оставлена ДОПОЛНИТЕЛЬНО --
    защищает от узкого случая переключения канала на автопилот, пока у уже
    стоящего в очереди поста ещё висит неразрешённое подтверждение.
    """
    awaiting_ids = {
        pid for pid in s.exec(
            select(PostApproval.post_id).where(PostApproval.status == "waiting")
        ).all()
    }
    posts = s.exec(
        select(Post).join(Channel, Channel.id == Post.channel_id)
        .where(Post.status == "scheduled", Post.scheduled_at <= now, Channel.auto_publish == True)  # noqa: E712
    ).all()
    return [p for p in posts if p.id not in awaiting_ids]


def due_post_approvals(s: Session, now: datetime) -> list[PostApproval]:
    """Дедлайн подтверждения истёк без реакции -- пост переносится в конец
    очереди (tasks._requeue_unconfirmed_post), а не публикуется."""
    return list(
        s.exec(select(PostApproval).where(PostApproval.status == "waiting", PostApproval.deadline <= now)).all()
    )


def approvals_needing_warning(s: Session, now: datetime, lead_minutes: int) -> list[PostApproval]:
    """Подтверждения, чей дедлайн наступит в ближайшие lead_minutes и о
    которых ещё не предупреждали (final_warning_sent=False) -- жёлтая
    плашка "не подтвердите за N мин -- уйдёт в конец очереди"."""
    lead = timedelta(minutes=lead_minutes)
    return list(
        s.exec(select(PostApproval).where(
            PostApproval.status == "waiting",
            PostApproval.final_warning_sent == False,  # noqa: E712
            PostApproval.deadline > now,
            PostApproval.deadline <= now + lead,
        )).all()
    )
