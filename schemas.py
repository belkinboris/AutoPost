from typing import Optional
from pydantic import BaseModel


class AuthIn(BaseModel):
    email: str
    password: str
    ref_code: str = ""
    utm_source: str = ""
    utm_medium: str = ""
    utm_campaign: str = ""
    utm_content: str = ""
    lp_session: str = ""  # landing_session_id для CTA/Journey Diagnostics -- не сохраняется в User, только в LandingEvent/TrafficAttribution


class TelegramMiniAppAuthIn(BaseModel):
    init_data: str  # сырая строка window.Telegram.WebApp.initData, проверяется через security.verify_telegram_init_data
    ref_code: str = ""
    utm_source: str = ""
    utm_medium: str = ""
    utm_campaign: str = ""
    utm_content: str = ""
    lp_session: str = ""


class ChannelIn(BaseModel):
    title: str
    tg_chat: str = ""
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
    interval_hours: float = 12
    daily_times: list[str] = ["10:00"]
    # Окно публикации и разброс. Аудит 02.08: этих полей не было НИ в
    # ChannelIn, НИ в ChannelPatch -- фронт их отправлял, pydantic молча
    # выбрасывал, PATCH отвечал 200 и рисовал «Сохранено ✓», а значение
    # оставалось пустым. Настройка «Пишем и публикуем посты только в это
    # время» не работала никогда и не могла заработать (правило 5).
    publish_window_start: str = ""
    publish_window_end: str = ""
    interval_jitter_minutes: int = 0
    channel_type: str = "thematic"
    enabled: bool = True
    onboarded: bool = False
    client_request_id: str = ""  # idempotency key для quick start (task item E)


class ChannelPatch(BaseModel):
    title: Optional[str] = None
    tg_chat: Optional[str] = None
    about: Optional[str] = None
    style: Optional[str] = None
    style_profile: Optional[str] = None
    post_length: Optional[str] = None
    language: Optional[str] = None
    post_voice: Optional[str] = None
    post_format: Optional[str] = None
    emoji_style: Optional[str] = None
    cta_enabled: Optional[bool] = None
    cta_text: Optional[str] = None
    use_web_search: Optional[bool] = None
    auto_publish: Optional[bool] = None
    schedule_kind: Optional[str] = None
    interval_hours: Optional[float] = None
    publish_window_start: Optional[str] = None
    publish_window_end: Optional[str] = None
    interval_jitter_minutes: Optional[int] = None
    daily_times: Optional[list[str]] = None
    channel_type: Optional[str] = None
    enabled: Optional[bool] = None
    onboarded: Optional[bool] = None
    queue_depth: Optional[int] = None


class SourceIn(BaseModel):
    url: str


class AnalyzeIn(BaseModel):
    link: str


class AnalyzeStyleOnly(BaseModel):
    link: str


class GenerateFormatIn(BaseModel):
    post_format: str = "story"


class PostIn(BaseModel):
    topic: str = ""
    # C14, пункт 4: при "Написать пост сейчас" можно выбрать своё время в
    # очереди вместо автоматического следующего слота. ISO-строка с фронта
    # (datetime-local input), как в ScheduleIn.
    scheduled_at: Optional[str] = None


class PostPatch(BaseModel):
    text: str


class ScheduleIn(BaseModel):
    scheduled_at: str


class BuyIn(BaseModel):
    package_id: str


class UpgradeIn(BaseModel):
    package_id: str
