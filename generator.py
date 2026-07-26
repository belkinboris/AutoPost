"""
Генерация постов и анализ стиля через Claude API.
"""

import re
import logging
import httpx

import config
import yandex_search
from database import Channel

logger = logging.getLogger(__name__)
ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"

VOICE_MAP = {
    "author": "от первого лица — «я», «мы», личный опыт и мнение автора",
    "news":   "сухой новостной стиль — факты без личного мнения, без «я»",
    "expert": "голос эксперта — авторитетно, с объяснением «почему», без личных историй",
}
FORMAT_MAP = {
    "story":    "история или кейс — есть начало, середина, вывод",
    "tips":     "полезные советы — конкретные шаги которые можно применить сегодня",
    "news":     "новостной пост — что случилось, почему важно, что дальше",
    "question": "пост-вопрос — подводишь к теме и задаёшь вопрос аудитории",
}
EMOJI_MAP = {
    "none":    "без эмодзи вообще",
    "minimal": "1-2 эмодзи максимум, только если очень уместны",
    "rich":    "эмодзи активно — для акцентов, заголовков, списков",
}

_TRASH_PATTERNS = [
    r"Отличн\w+.{0,100}", r"Пишу пост.*", r"Вот пост.*", r"Готово[!.]?",
    r"Конечно[,!\s].*", r"Дай мне.*", r"Сейчас.{0,40}", r"Есть всё.*",
    r"Нашёл.*", r"Ищу.*", r"Смотрю.*", r"Тема:\s*.*", r"[-\u2014\u2013]{1,3}",
    # Thinking-блоки Claude при web_search
    r"Беру.{0,80}(сделк|новост|факт|пост|поиск).*",
    r"Взял.{0,80}(сделк|новост|факт|пост|поиск).*",
    r"Нашёл.{0,80}(сделк|новост|факт|пост|поиск).*",
    r"Выбрал.{0,80}(сделк|новост|факт|пост|тему).*",
    r"Использую.{0,80}",
    r"Из поиска.{0,80}",
    r"По результатам.{0,80}",
    r"Проверил.{0,80}",
    r"Вижу.{0,80}(сделк|новост|факт).*",
]
_TRASH_RE = re.compile(r"^(" + "|".join(_TRASH_PATTERNS) + r")\s*$", re.IGNORECASE)

# Детектор "это не пост, а отказ/уточняющий вопрос модели" — срабатывает когда
# web_search не нашёл релевантных фактов и Claude вместо поста переспрашивает
# тему (иногда на английском, несмотря на запрет в системном промпте). Это не
# thinking-блок в начале текста, а весь ответ целиком, поэтому ловим отдельно.
_REFUSAL_PATTERNS = [
    r"what topic", r"please (share|specify|provide|clarify)", r"could you (clarify|specify|provide)",
    r"i (need|don't have|couldn't find|wasn't able)", r"let me know",
    r"уточните тему", r"какую тему", r"не могу найти", r"не удалось найти",
    r"подскажите[,]? пожалуйста", r"расскажите подробнее", r"какая тема",
]
_REFUSAL_RE = re.compile("|".join(_REFUSAL_PATTERNS), re.IGNORECASE)


def _looks_like_refusal(text: str) -> bool:
    """True если текст похож на отказ/уточняющий вопрос, а не на готовый пост."""
    if not text or len(text.strip()) < 80:
        return True  # подозрительно короткий ответ — скорее всего не пост
    return bool(_REFUSAL_RE.search(text[:300]))  # ищем только в начале, не во всём посте

# Паттерн для определения thinking-блока: абзац до "---" или до пустой строки
# который выглядит как рассуждение а не пост
_THINKING_RE = re.compile(
    r"^(Беру|Взял|Нашёл|Выбрал|Использую|Из поиска|По результатам|Проверил|Вижу|Смотрю|Ищу|Изучил|Анализирую).+",
    re.IGNORECASE | re.DOTALL
)


def _headers() -> dict:
    return {
        "x-api-key": config.ANTHROPIC_API_KEY,
        "anthropic-version": "2023-06-01",
        "content-type": "application/json",
    }


def _extract_text(data: dict) -> str:
    return "\n".join(b["text"] for b in data.get("content", []) if b.get("type") == "text").strip()


def _usage_tokens(data: dict) -> int:
    u = data.get("usage", {})
    return int(u.get("input_tokens", 0)) + int(u.get("output_tokens", 0))


def _clean_post(text: str) -> str:
    text = text.strip()

    # Режем всё до разделителя --- если он есть
    if "\n---\n" in text:
        text = text.split("\n---\n", 1)[1].strip()
    elif "\n---" in text and text.index("\n---") < 300:
        text = text.split("\n---", 1)[1].strip()

    lines = text.split("\n")

    # Убираем мусорные строки в начале
    while lines and (not lines[0].strip() or _TRASH_RE.match(lines[0].strip())):
        lines.pop(0)

    # Если первый абзац — thinking-блок (до первой пустой строки), убираем его
    if lines:
        first_para_lines = []
        for i, line in enumerate(lines):
            if not line.strip():
                break
            first_para_lines.append(line)
        first_para = " ".join(first_para_lines)
        if _THINKING_RE.match(first_para.strip()):
            # Пропускаем первый абзац и пустые строки после него
            rest = lines[len(first_para_lines):]
            while rest and not rest[0].strip():
                rest.pop(0)
            if rest:  # только если есть что-то после
                lines = rest

    text = "\n".join(lines).strip()
    if text.startswith('"') and text.endswith('"'):
        text = text[1:-1].strip()
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


import asyncio

class GenerationError(Exception):
    """Понятная ошибка генерации для показа пользователю."""
    pass


class TopicRejected(Exception):
    """
    Тема не прошла валидацию (неясная/недопустимая/неподдерживаемая).
    message -- готовый русский текст для показа пользователю.
    classification -- для логирования (Part 7 задачи).
    """
    def __init__(self, message: str, classification: str):
        self.message = message
        self.classification = classification
        super().__init__(message)


_TOPIC_CLASSIFY_SYSTEM = """Ты классифицируешь тему для Telegram-канала. Ответь ОДНИМ словом, без пояснений:

valid_topic — нормальная тема для контент-канала: бизнес, технологии, развлечения, игры, новости, лайфстайл, здоровье в нейтральном ключе, юмор без откровенного контента, а также образовательная сексология, психология отношений, интимная коммуникация, уверенность в отношениях, секс-просвещение, половое здоровье — БЕЗ откровенных описаний и порнографического содержания.

ambiguous_intimate_topic — тема касается интимной/сексуальной сферы, но сформулирована провокационно, в духе "соблазнения"/"мастерства в постели"/"пикапа", и неясно, имеется в виду образовательный контент (тогда можно сделать valid) или откровенный/манипулятивный контент (тогда нет). Используй эту категорию когда тема балансирует на границе, а не когда она однозначно образовательная или однозначно непристойная.

unclear_topic — набор случайных символов, бессмысленный текст, невозможно понять о чём.

adult_or_sexual_topic — порнографический или откровенно сексуальный контент с явными описаниями половых актов, секс-услуги, контент с несовершеннолетними в сексуальном контексте, изнасилование/принуждение/эксплуатация как тема для контента, унизительные манипулятивные "пикап"-техники.

unsafe_topic — призывы к насилию, экстремизм, незаконные действия, опасный контент.

unsupported_topic — тема технически невозможна для канала (например пустая).

Примеры valid_topic: "сексология: как говорить с партнёром о желаниях", "как стать увереннее в интимных отношениях", "психология близости в паре", "консультация по половому здоровью".
Примеры ambiguous_intimate_topic: "пост про возбуждение и как быть крутым в постели. Сексология." — формулировка провокационная (фокус на "крутости"/перформансе), но упомянута сексология как образовательный контекст — это серая зона, не отказ.
Примеры adult_or_sexual_topic: явный запрос порнографического описания, секс по запросу, контент с несовершеннолетними, контент про принуждение/насилие как развлекательную тему.

Если тема двусмысленная но может иметь нейтральное прочтение (например медицинский термин) — выбирай valid_topic, не будь излишне строгим. Сомневаешься между valid_topic и unclear_topic — выбирай valid_topic. Сомневаешься между valid_topic и ambiguous_intimate_topic для образовательного контента про отношения/близость — выбирай valid_topic, не ambiguous_intimate_topic. ambiguous_intimate_topic — только когда формулировка реально провокационная/перформанс-ориентированная."""


async def classify_topic(topic: str) -> str:
    """
    Классифицирует тему перед генерацией (Part 1 задачи).
    Возвращает одно из: valid_topic, ambiguous_intimate_topic, unclear_topic,
    adult_or_sexual_topic, unsafe_topic, unsupported_topic, classification_failed.

    ambiguous_intimate_topic (Task E): серая зона интимной/сексуальной темы —
    провокационная формулировка, но возможен легитимный образовательный
    контент (сексология, психология близости). Ведёт к мягкому уточнению
    у пользователя, не к жёсткому отказу как adult_or_sexual_topic.

    ВАЖНО (исправлено по итогам ревью): при сбое классификации (API error,
    timeout, непонятный ответ модели) возвращается classification_failed,
    а НЕ valid_topic. Это блокирующий статус — лучше лишний раз попросить
    пользователя попробовать снова, чем пропустить неподходящую тему из-за
    технической ошибки самой проверки.
    """
    topic = (topic or "").strip()
    if not topic:
        return "unsupported_topic"
    if len(topic) < 2:
        return "unclear_topic"
    try:
        text, _ = await _call_llm(_TOPIC_CLASSIFY_SYSTEM, topic, use_web_search=False, max_tokens=20)
        text = (text or "").strip().lower()
        # Порядок важен: ambiguous_intimate_topic проверяем раньше valid_topic,
        # чтобы случайное совпадение подстроки не увело в неправильную категорию.
        for label in ("ambiguous_intimate_topic", "adult_or_sexual_topic", "unsafe_topic", "unclear_topic", "unsupported_topic", "valid_topic"):
            if label in text:
                return label
        # Модель ответила, но не одним из ожидаемых слов — неоднозначный
        # результат классификации, тоже блокируем, а не пропускаем молча.
        logger.warning(f"classify_topic: неожиданный ответ классификатора «{text}» для темы «{topic}»")
        return "classification_failed"
    except Exception as e:
        logger.warning(f"Ошибка классификации темы «{topic}»: {e}")
        return "classification_failed"


_REJECTION_MESSAGES = {
    "adult_or_sexual_topic": "Не могу сделать пост на такую тему в этом формате. Попробуйте нейтральную или деловую тему — например: «новости M&A в России», «канал про Roblox», «советы для малого бизнеса».",
    "unsafe_topic": "Не могу сделать пост на такую тему в этом формате. Попробуйте нейтральную или деловую тему — например: «новости M&A в России», «канал про Roblox», «советы для малого бизнеса».",
    "unclear_topic": "Не понял тему. Напишите проще: например «M&A сделки в России», «новости крипты», «канал про Roblox».",
    "unsupported_topic": "Не понял тему. Напишите проще: например «M&A сделки в России», «новости крипты», «канал про Roblox».",
    "classification_failed": "Не удалось проверить тему. Попробуйте переформулировать.",
}

# Task E: ambiguous_intimate_topic — это НЕ rejection, это уточняющий вопрос.
# Намеренно не в _REJECTION_MESSAGES (которая блокирует генерацию полностью) —
# отдельная константа, чтобы вызывающий код мог показать мягкий reframe и
# при желании дать пользователю выбор продолжить с безопасной версией темы.
AMBIGUOUS_INTIMATE_CLARIFICATION = (
    "Могу сделать пост в образовательном формате: про уверенность, "
    "коммуникацию и уважение в интимных отношениях, без откровенных описаний. Подойдёт?"
)


def rejection_message(classification: str) -> str | None:
    """Готовое русское сообщение для отклонённой темы, либо None если тема валидна."""
    return _REJECTION_MESSAGES.get(classification)

YANDEX_LLM_URL = "https://llm.api.cloud.yandex.net/foundationModels/v1/completion"
# Responses API AI Studio — для открытых моделей (DeepSeek, Qwen, gpt-oss).
# ВАЖНО: это НЕ Chat Completions (llm.api.cloud.yandex.net/v1/chat/completions --
# та ошибочная версия давала "Failed to parse model URI"), а официальный
# Responses-эндпоинт с другим хостом и форматом тела (см. AI Studio ->
# DeepSeek 4 Flash -> "Посмотреть код" -> Python).
YANDEX_RESPONSES_URL = "https://ai.api.cloud.yandex.net/v1/responses"

# Принудительный провайдер для internal-сравнения качества (см.
# internal_llm_compare.py). None = использовать config.LLM_PROVIDER.
FORCE_PROVIDER: str | None = None


async def _call_yandex(system, messages, max_tokens=700):
    """
    Вызов Alice AI / Foundation Models (Yandex Cloud).
    messages: [{"role": "user"|"assistant", "content": str}, ...]
    Возвращает (text, tokens) — тот же контракт, что _call_claude.
    Ограничение: web_search у Yandex API нет — вызывающий код должен
    учитывать это сам (см. _call_llm).
    """
    if not config.YANDEX_API_KEY or not config.YANDEX_MODEL_URI:
        raise GenerationError("Yandex LLM не настроен (YANDEX_API_KEY / YANDEX_FOLDER_ID).")
    if config.YANDEX_API_MODE == "openai":
        # Открытые модели (DeepSeek/Qwen/gpt-oss) идут через Responses API.
        # messages здесь всегда одно user-сообщение (см. _call_llm) --
        # Responses API у AI Studio принимает input строкой, а не историю.
        url = YANDEX_RESPONSES_URL
        user_text = "\n\n".join(m["content"] for m in messages if m.get("content"))
        body = {
            "model": config.YANDEX_MODEL_URI,
            "temperature": config.LLM_TEMPERATURE,
            "instructions": system,
            "input": user_text,
            # DeepSeek тратит невидимые reasoning-токены ДО ответа, а параметр
            # их отключения Яндекс не пробрасывает (проверено: 200 OK, но
            # incomplete/max_output_tokens). Поэтому бюджет с запасом:
            # рассуждения + сам ответ. Reasoning-токены на Flash дешёвые.
            "max_output_tokens": max_tokens + 8000,
            # DeepSeek V4 включает "режим размышлений" по умолчанию: без явного
            # отключения токены уходят на невидимый reasoning_content, а на
            # финальный ответ бюджета может не остаться вовсе (incomplete_details:
            # max_output_tokens при пустом output). Пишем посты, не решаем задачи --
            # рассуждения не нужны, отключаем, чтобы весь лимит шёл на текст.
            "thinking": {"type": "disabled"},
        }
    else:
        url = YANDEX_LLM_URL
        ya_messages = [{"role": "system", "text": system}] + [
            {"role": m["role"], "text": m["content"]} for m in messages
        ]
        body = {
            "modelUri": config.YANDEX_MODEL_URI,
            "completionOptions": {"stream": False, "temperature": config.LLM_TEMPERATURE, "maxTokens": str(max_tokens)},
            "messages": ya_messages,
        }
    headers = {"Authorization": f"Api-Key {config.YANDEX_API_KEY}", "Content-Type": "application/json"}

    last_error = None
    for attempt in range(3):
        try:
            async with httpx.AsyncClient(timeout=120) as client:
                r = await client.post(url, headers=headers, json=body)
        except httpx.TimeoutException:
            last_error = "timeout"
            await asyncio.sleep(2 * (attempt + 1))
            continue

        if r.status_code < 400:
            data = r.json()
            try:
                if config.YANDEX_API_MODE == "openai":
                    if "output_text" in data and data["output_text"]:
                        text = data["output_text"]
                    else:
                        # Формат OpenAI Responses: output -- список объектов,
                        # у каждого content -- список частей {type, text}.
                        parts = []
                        for item in (data.get("output") or []):
                            if item.get("type") == "reasoning":
                                continue  # внутренние размышления -- не для поста
                            for c in (item.get("content") or []):
                                if c.get("text"):
                                    parts.append(c["text"])
                        text = "\n".join(parts)
                    usage = data.get("usage") or {}
                    tokens = int(usage.get("total_tokens") or usage.get("output_tokens", 0) or 0)
                    # Диагностика себестоимости: без этого нельзя ответить на
                    # вопрос "сколько реально стоит пост". Отдельно логируем
                    # reasoning-токены -- у DeepSeek "режим размышлений" включён
                    # по умолчанию, и выше мы пытаемся его отключить
                    # (thinking: disabled). Работает ли это отключение на
                    # стороне Яндекса, видно только по этой цифре: если
                    # reasoning_tokens стабильно 0 -- отключение сработало,
                    # если нет -- мы платим за невидимые токены на каждой
                    # генерации, и max_output_tokens с запасом +8000 их
                    # оплачивает.
                    _out_details = usage.get("output_tokens_details") or {}
                    _reasoning = int(_out_details.get("reasoning_tokens", 0) or 0)
                    logger.info(
                        "[llm-usage] model=%s input=%s output=%s reasoning=%s total=%s",
                        config.YANDEX_MODEL_URI,
                        usage.get("input_tokens"),
                        usage.get("output_tokens"),
                        _reasoning,
                        tokens or None,
                    )
                    if not usage:
                        logger.warning(
                            "[llm-usage] провайдер не вернул usage -- расход этого вызова "
                            "не будет учтён нигде (tokens_used=0)"
                        )
                    if not text:
                        raise KeyError("empty output_text")
                    inc = (data.get("incomplete_details") or {}).get("reason")
                    if inc:
                        logger.warning(f"Yandex LLM: ответ неполный (reason={inc}), длина текста {len(text)}")
                else:
                    text = data["result"]["alternatives"][0]["message"]["text"]
                    tokens = int(data.get("result", {}).get("usage", {}).get("totalTokens", 0) or 0)
            except (KeyError, IndexError):
                logger.error(f"Yandex LLM unexpected response: {str(data)[:800]}")
                raise GenerationError("Неожиданный ответ ИИ. Попробуйте ещё раз.")
            return text, tokens

        logger.error(f"Yandex LLM {r.status_code}: {r.text[:500]}")
        if r.status_code == 429:
            last_error = "overloaded"
            await asyncio.sleep(3 * (attempt + 1))
            continue
        if r.status_code in (401, 403):
            raise GenerationError("Ошибка авторизации ИИ. Обратитесь в поддержку.")
        if r.status_code == 400:
            raise GenerationError("Не удалось сгенерировать пост. Попробуйте изменить тему.")
        last_error = f"http_{r.status_code}"
        await asyncio.sleep(2)

    if last_error == "overloaded":
        raise GenerationError("Серверы ИИ сейчас перегружены. Попробуйте через минуту.")
    if last_error == "timeout":
        raise GenerationError("Превышено время ожидания. Попробуйте ещё раз.")
    raise GenerationError("Временная ошибка ИИ. Попробуйте ещё раз через минуту.")


async def _call_llm(system, user, use_web_search, max_tokens=700, messages=None):
    """
    Единая точка вызова LLM. Провайдер: config.LLM_PROVIDER
    ("anthropic" | "yandex"), FORCE_PROVIDER перекрывает для internal-тестов.
    user — строка (одиночное сообщение) ЛИБО messages — готовая история.
    ВАЖНО: у Yandex нет web_search; в этом режиме генерируем без поиска
    (новостные каналы — см. ограничение в DEPLOY_NOTE, фаза 1.5 —
    интеграция Яндекс.Поиск API).
    """
    provider = FORCE_PROVIDER or config.LLM_PROVIDER
    msgs = messages if messages is not None else [{"role": "user", "content": user}]
    if provider == "yandex":
        if use_web_search:
            logger.warning("web_search недоступен у провайдера yandex — генерация без поиска")
        return await _call_yandex(system, msgs, max_tokens=max_tokens)
    # anthropic (по умолчанию)
    if messages is not None:
        return await _call_claude_messages(system, msgs, max_tokens=max_tokens)
    return await _call_claude(system, user, use_web_search, max_tokens=max_tokens)


async def _call_claude_messages(system, messages, max_tokens=700):
    """История сообщений для Anthropic (используется consult)."""
    body = {
        "model": config.ANTHROPIC_MODEL,
        "max_tokens": max_tokens,
        "system": system,
        "messages": messages,
    }
    async with httpx.AsyncClient(timeout=60) as client:
        r = await client.post(ANTHROPIC_URL, headers=_headers(), json=body)
        data = r.json()
    return _extract_text(data), _usage_tokens(data)


async def _call_claude(system, user, use_web_search, max_tokens=700):
    body = {
        "model": config.ANTHROPIC_MODEL,
        "max_tokens": max_tokens,
        "system": system,
        "messages": [{"role": "user", "content": user}],
    }
    if use_web_search:
        body["tools"] = [{"type": "web_search_20250305", "name": "web_search", "max_uses": 2}]

    last_error = None
    for attempt in range(3):  # до 3 попыток на перегрузку
        try:
            async with httpx.AsyncClient(timeout=120) as client:
                r = await client.post(ANTHROPIC_URL, headers=_headers(), json=body)
        except httpx.TimeoutException:
            last_error = "timeout"
            await asyncio.sleep(2 * (attempt + 1))
            continue

        if r.status_code < 400:
            data = r.json()
            return _extract_text(data), _usage_tokens(data)

        # Ошибки
        logger.error(f"Claude API {r.status_code}: {r.text[:500]}")
        if r.status_code in (429, 529):  # rate limit / overloaded
            last_error = "overloaded"
            await asyncio.sleep(3 * (attempt + 1))
            continue
        if r.status_code == 401:
            raise GenerationError("Ошибка авторизации ИИ. Обратитесь в поддержку.")
        if r.status_code == 400:
            raise GenerationError("Не удалось сгенерировать пост. Попробуйте изменить тему.")
        # Прочие ошибки — последняя попытка
        last_error = f"http_{r.status_code}"
        await asyncio.sleep(2)

    # Все попытки исчерпаны
    if last_error == "overloaded":
        raise GenerationError("Серверы ИИ сейчас перегружены. Попробуйте через минуту.")
    if last_error == "timeout":
        raise GenerationError("Превышено время ожидания. Попробуйте ещё раз.")
    raise GenerationError("Временная ошибка ИИ. Попробуйте ещё раз через минуту.")


async def generate_post(channel: Channel, source_material: str = "", topic: str = "", custom_rules: str = "", recent_titles: str = "") -> tuple[str, int]:
    voice = VOICE_MAP.get(getattr(channel, "post_voice", "author"), VOICE_MAP["author"])
    fmt = FORMAT_MAP.get(getattr(channel, "post_format", "story"), FORMAT_MAP["story"])
    emoji = EMOJI_MAP.get(getattr(channel, "emoji_style", "minimal"), EMOJI_MAP["minimal"])
    cta_enabled = getattr(channel, "cta_enabled", False)
    cta_text = getattr(channel, "cta_text", "") or ""

    style_block = channel.style or ""
    # Итерация "не тот стиль" (SPEC_TRUEPOST_GENERATOR_STYLE): если
    # пользователь вставил свои посты как образец (маркер [ОБРАЗЦЫ СТИЛЯ]
    # в style_profile, см. create_channel) -- строим блок стилевого
    # зеркалирования вместо обычного "Профиль стиля".
    style_mirror_block = ""
    if channel.style_profile and channel.style_profile.startswith("[ОБРАЗЦЫ СТИЛЯ]"):
        samples = channel.style_profile.removeprefix("[ОБРАЗЦЫ СТИЛЯ]").strip()[:3000]
        style_mirror_block = f"""

ОБРАЗЦЫ СТИЛЯ АВТОРА (реальные посты пользователя):
{samples}

Проанализируй лексику, длину абзацев, обращение (ты/вы), эмодзи, ритм этих постов и напиши новый пост НЕОТЛИЧИМЫМ по стилю. Не копируй содержание."""
    elif channel.style_profile:
        style_block += f"\n\nПрофиль стиля:\n{channel.style_profile}"

    # Если стиля нет ВООБЩЕ (не подключён канал, нет образцов, нет описания
    # стиля) -- задаём явный пресет тона по типу канала, а не "нейтрально".
    # Именно "средний ИИ-тон" без стилевой рамки давал 75% "не тот стиль".
    tone_preset_block = ""
    if not style_block.strip() and not style_mirror_block:
        channel_type = getattr(channel, "channel_type", "thematic")
        if channel_type == "news":
            tone_preset_block = """

ТОН (пресет "новостной дайджест"): пиши как живой редактор новостного
канала -- коротко, по делу, с одной яркой деталью на пост. Без канцелярита,
без "как известно". Разговорный, но собранный. Как будто пересказываешь
новость умному другу за 30 секунд."""
        else:
            tone_preset_block = """

ТОН (пресет "автор-эксперт"): пиши как человек, который реально живёт этой
темой и ведёт канал для своих. От первого лица, с личным отношением к
фактам. Живые формулировки, никакой энциклопедичности. Как будто делишься
находкой с подписчиками, которых уважаешь."""

    cta_instruction = f'\nВ КОНЦЕ добавь призыв: «{cta_text.strip()}»' if cta_enabled and cta_text.strip() else ""

    rules_block = f"\n\nПЕРСОНАЛЬНЫЕ ПРАВИЛА (обязательно соблюдать):\n{custom_rules}" if custom_rules else ""

    about_lower = (channel.about or "").lower()
    style_lower = (channel.style or "").lower()
    combined = about_lower + " " + style_lower

    is_humor = any(w in combined for w in [
        "смешн", "юмор", "абсурд", "мем", "прикол", "зумер", "дебил",
        "весел", "ирони", "сатир", "угар", "ржать", "ржака", "шутк", "хохот",
    ])

    if is_humor:
        mood_instruction = """
ЭТО ЮМОРИСТИЧЕСКИЙ КАНАЛ.
Пиши как зумер в интернете — коротко, дерзко, абсурд и неожиданные повороты.
Строчные буквы, обрывистые фразы, эмодзи как реакция (💀🙏😭).
Никакой морали. Никаких выводов о жизни. Длина — короткая."""
    else:
        mood_instruction = f"""
ОПРЕДЕЛИ НАСТРОЕНИЕ ПО ТЕМЕ:
• Здоровье, болезни → БЕРЕЖНО. Без советов. Честно и тепло.
• Семья, дети → ТЕПЛО. Как разговор с близким.
• Отношения, психология → ЧЕСТНО. Конкретный приём.
• Деньги, крипта → КОНКРЕТНО. Цифры, факты, вывод.
• Бизнес, карьера → КАК ИНСАЙДЕР. Реальные кейсы.
• Lifestyle, роскошь → АТМОСФЕРНО. Детали, образы, конкретный человек.
• Политика, новости → НЕЙТРАЛЬНО И ОСТРО. Факт + неочевидный угол.
• Саморазвитие → ВДОХНОВЛЯЮЩЕ. Конкретный шаг сегодня.

СТРУКТУРА:
1. <b>Заголовок</b> — цепляет с первой строки
2. 2-3 абзаца с конкретикой — имя, ситуация, деталь
3. Вывод или вопрос{", призыв" if cta_enabled else ""}"""

    system = f"""Ты — автор Telegram-канала «{channel.title}».

О КАНАЛЕ: {channel.about}
{"СТИЛЬ: " + style_block if style_block.strip() else ""}{style_mirror_block}{tone_preset_block}
ЯЗЫК: {channel.language}
ДЛИНА: {channel.post_length}
Голос: {voice if not is_humor else "зумерский"}
Формат: {fmt if not is_humor else "короткий абсурд"}
Эмодзи: {emoji}
{cta_instruction}
{rules_block}
{mood_instruction}

{f'''ЭТИ ТЕМЫ УЖЕ ЕСТЬ — в очереди или уже опубликованы, не повторять:
{recent_titles}
Правило: выбирай НОВОЕ событие которого нет в списке.
Если все свежие события уже покрыты — возьми другой угол: последствия, реакция рынка, сравнение с похожим случаем. Никогда не пиши про то же событие теми же словами, даже под другим заголовком.

''' if recent_titles else ""}БАЗОВЫЕ ПРАВИЛА (нарушение недопустимо):
- Markdown ЗАПРЕЩЁН ПОЛНОСТЬЮ: символы #, ##, ###, **, __, ``` не существуют. Telegram их не отображает — читатель увидит мусор. Форматирование ТОЛЬКО тегами <b>жирный</b> и <i>курсив</i>
- ЗАПРЕЩЕНЫ шаблонные открытия: «Привет, друзья», «Привет всем», «Хочу поделиться», «Сегодня поговорим», «Давайте разберёмся», «Все мы знаем», «В современном мире» — и любые похожие. Первая строка = сразу факт, деталь, вопрос или заголовок, цепляющий читателя
- ЗАПРЕЩЕНЫ пустые обобщения: «это очень важно», «становится всё более актуальным», «играет большую роль». Вместо оценки — конкретика: цифра, пример, ситуация
- Длинное тире — ЗАПРЕЩЕНО. Используй только короткое тире -
- ЗАПРЕЩЕНО любое вступление, объяснение или рассуждение перед постом
- ЗАПРЕЩЕНЫ фразы-действия: «Беру сделку», «Взял новость», «Нашёл факт», «Из поиска», «Выбрал тему», «Использую», «Проверил» — и любые похожие
- ЗАПРЕЩЕНО отделять рассуждения от поста символами ---, ***, или пустыми строками
- Первая строка ответа = первая строка поста. Ноль слов до поста
- Никаких «следует отметить», «является», «в рамках»
- ЗАПРЕЩЕНО спрашивать уточнения — пиши сам
- Абзацы через ОДНУ пустую строку
- Верни ТОЛЬКО текст поста. Никаких комментариев ни до ни после"""

    use_search = channel.use_web_search and not topic
    effective_topic = topic or channel.about  # тема, на которую пост ОБЯЗАН быть

    # === Фаза 1.5: Яндекс.Поиск вместо web_search у провайдера yandex ===
    # У Yandex Foundation Models нет встроенного поиска. Если пост требует
    # актуальных фактов (use_web_search или явный topic без материалов) --
    # сами ходим в Yandex Search API и подкладываем выдачу как источники.
    # При сбое поиска тихо деградируем в прежнее поведение (без поиска).
    search_tokens = 0
    provider = FORCE_PROVIDER or config.LLM_PROVIDER
    if (
        provider == "yandex"
        and not source_material
        and (use_search or (topic and channel.use_web_search))
    ):
        try:
            found = await yandex_search.search_news(effective_topic)
            if found:
                source_material = (
                    "СВЕЖАЯ ВЫДАЧА ПОИСКА (Яндекс) по теме -- используй только эти факты, не выдумывай.\n"
                    "ВАЖНО: источники ниже -- это отдельные, не связанные друг с другом результаты поиска "
                    "(разные статьи, разные события). НЕ пересказывай их по очереди одним абзацем на источник -- "
                    "так пост превращается в список фактов без единого сюжета. Вместо этого: выбери ОДИН самый "
                    "конкретный и яркий факт или историю из всей выдачи и построй вокруг него весь пост. "
                    "Остальные источники используй только если они напрямую подкрепляют эту же историю "
                    "(например, дают цифру или контекст к тому же событию) -- иначе игнорируй их.\n\n"
                    + yandex_search.format_search_context(found)
                )
                search_tokens = config.YANDEX_SEARCH_TOKEN_COST
                logger.info(f"Канал {channel.id}: yandex_search дал {len(found)} результатов для «{effective_topic[:60]}»")
            else:
                logger.info(f"Канал {channel.id}: yandex_search пусто для «{effective_topic[:60]}», генерация без поиска")
        except yandex_search.SearchUnavailable as e:
            logger.warning(f"Канал {channel.id}: yandex_search недоступен ({e}), генерация без поиска")
        use_search = False  # у _call_yandex поиска всё равно нет

    if topic:
        user_msg = f"Напиши пост на тему: «{topic}»."
        if source_material:
            user_msg += f"\n\nИсточники:\n{source_material[:4000]}"
        elif channel.use_web_search:
            user_msg += " Найди актуальные факты по этой теме."
    elif source_material:
        user_msg = f"Напиши пост на тему «{effective_topic}» из этих материалов:\n\n" + source_material[:4000]
    elif channel.use_web_search:
        # КРИТИЧНО: тема канала должна явно попадать в user-сообщение, а не
        # только в системный промпт — иначе модель может "соскользнуть" на
        # более комфортную/частую тему при поиске (это и было причиной P0-бага,
        # когда пост про "соски" ушёл в крипту). Жёстко фиксируем тему здесь.
        user_msg = f"Напиши пост на тему «{effective_topic}». Найди свежий конкретный факт именно по этой теме. Используй только актуальные данные из поиска, не из памяти. Пост должен быть строго про «{effective_topic}», ни про что другое."
    else:
        user_msg = f"Напиши пост на тему «{effective_topic}». Конкретный пример — человек, ситуация, деталь."

    text, tokens = await _call_llm(system, user_msg, use_search, max_tokens=650)
    cleaned = _clean_post(text)
    total_tokens = tokens + search_tokens
    fallback_used = "none"

    if _looks_like_refusal(cleaned) and use_search:
        # Поиск не нашёл релевантных фактов и модель вместо поста переспрашивает
        # тему. Не показываем это пользователю — тихо перегенерируем обычным
        # постом по теме, без поиска. Пользователь просто получает результат.
        logger.info(f"Канал {channel.id}: web_search дал отказ/вопрос вместо поста, fallback_used=no_search_retry")
        fallback_used = "no_search_retry"
        fallback_msg = f"Напиши пост на тему «{effective_topic}». Конкретный пример — человек, ситуация, деталь. Не нужно искать в интернете, пиши по своим знаниям. Пост должен быть строго про «{effective_topic}»."
        text2, tokens2 = await _call_llm(system, fallback_msg, False, max_tokens=650)
        cleaned = _clean_post(text2)
        total_tokens += tokens2

    # Post-topic match check (Part 5 задачи): проверяем что результат реально
    # про заданную тему, а не ушёл в другую тематику. Только для случаев без
    # явного topic от вызывающего кода (онбординг, авто-генерация) — там цена
    # ошибки выше, потому что пользователь не формулировал topic сам только что.
    if not _looks_like_refusal(cleaned):
        match_ok, match_tokens = await _check_topic_match(cleaned, effective_topic)
        total_tokens += match_tokens
        logger.info(f"Канал {channel.id}: post_topic_match_score={'pass' if match_ok else 'fail'} fallback_used={fallback_used}")
        if not match_ok:
            logger.info(f"Канал {channel.id}: post-topic mismatch для темы «{effective_topic}», повторная генерация без поиска")
            fallback_used = "topic_mismatch_retry"
            retry_msg = f"Напиши пост СТРОГО на тему «{effective_topic}», не отклоняясь от неё. Конкретный пример — человек, ситуация, деталь. Не используй поиск в интернете."
            text3, tokens3 = await _call_llm(system, retry_msg, False, max_tokens=650)
            cleaned = _clean_post(text3)
            total_tokens += tokens3

    return cleaned, total_tokens


async def _check_topic_match(post_text: str, topic: str) -> tuple[bool, int]:
    """
    Post-topic match check (Part 5): быстрая проверка что текст поста
    реально соответствует заданной теме, а не ушёл в другую тематику.
    Возвращает (match_ok, tokens_used). При сбое проверки -- match_ok=True
    (не блокируем публикацию из-за технической ошибки самой проверки).
    """
    if not post_text or not topic:
        return True, 0
    system = "Ответь ОДНИМ словом: YES если текст поста соответствует заданной теме (хотя бы по смыслу, не обязательно дословно), NO если пост явно про другую тему."
    user = f"Тема: «{topic}»\n\nТекст поста:\n{post_text[:600]}"
    try:
        text, tokens = await _call_llm(system, user, False, max_tokens=10)
        return ("no" not in text.strip().lower()), tokens
    except Exception as e:
        logger.warning(f"Ошибка post-topic match check: {e}")
        return True, 0


async def check_news_available(channel: "Channel") -> tuple:
    """Проверяет есть ли свежие новости по теме. Возвращает (bool, tokens_used)."""
    provider = FORCE_PROVIDER or config.LLM_PROVIDER
    if provider == "yandex":
        # Фаза 1.5: свежесть проверяем прямым запросом в Яндекс.Поиск.
        # Fail-open: любая техническая проблема поиска НЕ блокирует цикл
        # генерации (пользователь важнее идеальной проверки).
        try:
            found = await yandex_search.search_news(channel.about)
        except yandex_search.SearchUnavailable as e:
            logger.warning(f"check_news_available: yandex_search недоступен ({e}) — считаем что новости есть")
            return True, 0
        has_news = yandex_search.has_fresh_results(found)
        logger.info(
            f"check_news_available (yandex): {len(found)} результатов, fresh={'yes' if has_news else 'no'} "
            f"для «{(channel.about or '')[:60]}»"
        )
        return has_news, config.YANDEX_SEARCH_TOKEN_COST
    system = "You are a news editor. Reply only YES or NO."
    user = (
        f"Topic: {channel.about}\n\n"
        f"Are there any NEW events or news on this topic in the last 24 hours worth covering? "
        f"Answer with one word: YES or NO."
    )
    body = {
        "model": config.ANTHROPIC_MODEL,
        "max_tokens": 10,
        "system": system,
        "messages": [{"role": "user", "content": user}],
        "tools": [{"type": "web_search_20250305", "name": "web_search", "max_uses": 1}],
    }
    async with httpx.AsyncClient(timeout=60) as client:
        r = await client.post(ANTHROPIC_URL, headers=_headers(), json=body)
        data = r.json()
    text = _extract_text(data).strip().upper()
    tokens = _usage_tokens(data)
    has_news = "YES" in text
    return has_news, tokens


async def consult(channel: "Channel", user_message: str, history: list, rules_text: str = "") -> tuple:
    """
    ИИ-консультант для настройки стиля.
    Возвращает (ответ, suggested_rule или None).
    """
    msg_count = len([h for h in history if h.get("role") == "user"])
    system = f"""Ты — помощник по настройке стиля Telegram-канала «{channel.title}».

О канале: {channel.about}
Текущие правила: {rules_text or "нет"}

ПРАВИЛА ДИАЛОГА:
— Задавай ТОЛЬКО ОДИН короткий вопрос за раз. Никаких списков вариантов.
— Пиши коротко — 1-3 предложения максимум.
— Никакого Markdown: не используй **, *, #, ---
— Для выделения жирным используй HTML-теги Telegram: <b>текст</b>
— Примеры правил могут включать форматирование через <b></b> — это нормально
— Разговорный стиль — как живой человек, не как ассистент.
— Когда понял конкретное требование, сразу предложи правило.
— В конце ответа, если есть конкретное правило, добавь: ПРАВИЛО: [текст правила]

Примеры правил:
ПРАВИЛО: Не использовать длинное тире, только короткое
ПРАВИЛО: Писать только про события 2025-2026 года
ПРАВИЛО: Каждый пост начинать с цифры или конкретного имени

Это сообщение {msg_count + 1} в диалоге. Будь конкретен и краток."""

    messages = []
    for h in history[-6:]:  # последние 6 сообщений
        messages.append({"role": h["role"], "content": h["content"]})
    messages.append({"role": "user", "content": user_message})

    response, _ = await _call_llm(system, None, False, max_tokens=500, messages=messages)

    # Извлекаем правило если есть
    rule = None
    rule_match = re.search(r"ПРАВИЛО:\s*(.+?)(?:\n|$)", response)
    if rule_match:
        rule = rule_match.group(1).strip()
        response = response[:rule_match.start()].strip()

    return response, rule


async def analyze_style(posts: list[str]) -> tuple[str, int]:
    if not posts:
        return "", 0
    sample = "\n\n---\n\n".join(posts[:15])[:7000]
    system = "Ты — редактор. Анализируешь стиль Telegram-канала чтобы другой автор мог писать неотличимо."
    user = (
        "Профиль стиля (7-9 пунктов): тон, настроение, структура поста, "
        "длина предложений, начало/конец постов, характерные приёмы, что никогда не встречается.\n\n"
        + sample
    )
    return await _call_llm(system, user, False, max_tokens=450)
