"""
Яндекс.Поиск для новостных каналов (фаза 1.5 миграции в РФ).

У Yandex Foundation Models (DeepSeek через Responses API) нет встроенного
web_search, как у Anthropic. Этот модуль закрывает дыру: перед генерацией
поста для news-канала делаем запрос в Yandex Search API v2 (синхронный
режим), парсим выдачу и отдаём сниппеты в контекст промпта.

API: POST https://searchapi.api.cloud.yandex.net/v2/web/search
Авторизация: Api-Key сервисного аккаунта (роль search-api.webSearch.user).
Ответ: JSON {"rawData": "<base64 XML выдачи>"}.

Тарификация: за запрос (не за токены), отдельно от Foundation Models.
Для внутреннего учёта в токенах используем config.YANDEX_SEARCH_TOKEN_COST.
"""

import base64
import logging
import re
import asyncio
from datetime import datetime, timedelta, timezone
from xml.etree import ElementTree

import httpx

import config

logger = logging.getLogger(__name__)

SEARCH_URL = "https://searchapi.api.cloud.yandex.net/v2/web/search"

# Сколько дней результат считается "свежей новостью" для check_news_available
FRESH_DAYS = 3


class SearchUnavailable(Exception):
    """Поиск недоступен (сеть/авторизация/квота). Вызывающий код должен
    деградировать в генерацию без поиска, а не падать."""


def _search_enabled() -> bool:
    return bool(
        config.YANDEX_SEARCH_ENABLED
        and config.YANDEX_SEARCH_API_KEY
        and config.YANDEX_FOLDER_ID
    )


def _build_body(query_text: str, max_results: int, page: int = 0) -> dict:
    return {
        "query": {
            "searchType": "SEARCH_TYPE_RU",
            "queryText": query_text[:400],
            "familyMode": "FAMILY_MODE_MODERATE",
            "page": str(max(0, int(page))),
        },
        # Плоская группировка: 1 документ = 1 группа, максимум сниппетов
        "groupSpec": {
            "groupMode": "GROUP_MODE_FLAT",
            "groupsOnPage": str(max_results),
            "docsInGroup": "1",
        },
        "maxPassages": "3",
        "region": "225",  # Россия
        "l10n": "LOCALIZATION_RU",
        "folderId": config.YANDEX_FOLDER_ID,
        "responseFormat": "FORMAT_XML",
    }


_TAG_RE = re.compile(r"<[^>]+>")


def _xml_text(el) -> str:
    """Собирает весь текст элемента, включая вложенные <hlword> и пр."""
    if el is None:
        return ""
    return re.sub(r"\s+", " ", "".join(el.itertext())).strip()


def _parse_modtime(raw: str):
    """modtime приходит как 20260719T120301 (иногда с суффиксами). None при мусоре."""
    if not raw:
        return None
    m = re.match(r"(\d{8})T?(\d{6})?", raw.strip())
    if not m:
        return None
    try:
        date_part = m.group(1)
        time_part = m.group(2) or "000000"
        return datetime.strptime(date_part + time_part, "%Y%m%d%H%M%S").replace(
            tzinfo=timezone.utc
        )
    except ValueError:
        return None


def parse_search_xml(xml_text: str) -> list[dict]:
    """
    Парсит XML выдачу Яндекса в список результатов:
    [{"title", "url", "snippet", "modtime": datetime|None}, ...]
    Бросает SearchUnavailable на XML с <error>.
    """
    try:
        root = ElementTree.fromstring(xml_text)
    except ElementTree.ParseError as e:
        raise SearchUnavailable(f"Некорректный XML выдачи: {e}")

    err = root.find(".//error")
    if err is not None:
        code = err.get("code", "?")
        # code 15 = "искомая комбинация слов нигде не встречается" — это не
        # ошибка сервиса, а пустая выдача
        if code == "15":
            return []
        raise SearchUnavailable(f"Ошибка Яндекс.Поиска code={code}: {_xml_text(err)[:200]}")

    results = []
    for doc in root.findall(".//doc"):
        url = _xml_text(doc.find("url"))
        title = _xml_text(doc.find("title"))
        headline = _xml_text(doc.find("headline"))
        passages = [
            _xml_text(p) for p in doc.findall(".//passages/passage")
        ]
        snippet = " ".join([t for t in ([headline] + passages) if t])[:600]
        modtime = _parse_modtime(_xml_text(doc.find("modtime")))
        if url and (title or snippet):
            results.append({
                "title": title,
                "url": url,
                "snippet": snippet,
                "modtime": modtime,
            })
    return results


async def search_web(query_text: str, max_results: int | None = None, page: int = 0) -> list[dict]:
    """
    Синхронный (по режиму API) запрос к Яндекс.Поиску.
    Возвращает список результатов (может быть пустым).
    Бросает SearchUnavailable при недоступности сервиса.
    """
    if not _search_enabled():
        raise SearchUnavailable("Яндекс.Поиск не сконфигурирован (YANDEX_SEARCH_ENABLED/ключ/folder)")

    max_results = max_results or config.YANDEX_SEARCH_MAX_RESULTS
    body = _build_body(query_text, max_results, page)
    headers = {"Authorization": f"Api-Key {config.YANDEX_SEARCH_API_KEY}"}

    last_error = None
    for attempt in range(2):
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                r = await client.post(SEARCH_URL, headers=headers, json=body)
        except httpx.HTTPError as e:
            last_error = f"network: {e}"
            await asyncio.sleep(1 + attempt)
            continue

        if r.status_code < 400:
            try:
                raw = r.json().get("rawData", "")
                xml_text = base64.b64decode(raw).decode("utf-8", errors="replace")
            except Exception as e:
                raise SearchUnavailable(f"Не удалось декодировать rawData: {e}")
            return parse_search_xml(xml_text)

        logger.error(f"Yandex Search API {r.status_code}: {r.text[:300]}")
        if r.status_code == 429:
            last_error = "rate_limit"
            await asyncio.sleep(2 * (attempt + 1))
            continue
        if r.status_code in (401, 403):
            raise SearchUnavailable("Авторизация Яндекс.Поиска отклонена (проверьте ключ и роль search-api.webSearch.user)")
        last_error = f"http_{r.status_code}"
        await asyncio.sleep(1)

    raise SearchUnavailable(f"Яндекс.Поиск недоступен: {last_error}")


async def search_news(topic: str, max_results: int | None = None, page: int = 0) -> list[dict]:
    """
    Поиск свежих новостей по теме канала. Обогащаем запрос словом «новости»
    и сортируем найденное по modtime (свежее выше). Релевантностную
    сортировку самой выдачи не трогаем — SORT_MODE_BY_TIME у Яндекса сильно
    роняет качество, свежесть добираем фильтром на нашей стороне.

    `page` — страница выдачи. Раньше она была прибита к нулю, и это оказалось
    корнем повторов (прод 03.08): запрос собирается из `channel.about`, то
    есть при каждой генерации он ОДИН И ТОТ ЖЕ, первая страница выдачи тоже
    одна и та же, и модель раз за разом получала те же восемь источников.
    Отсюда одни и те же три-четыре известные истории под новыми заголовками —
    сколько ни запрещай повторяться в промпте, другого материала у неё просто
    не было. Вызывающая сторона листает страницы по числу уже написанных
    постов (generator.generate_post).
    """
    topic = (topic or "").strip()
    if not topic:
        return []
    query = topic if "новост" in topic.lower() else f"{topic} последние новости"
    results = await search_web(query, max_results, page=page)
    # Свежие (или бездатные — у Яндекса modtime есть не всегда) выше
    now = datetime.now(timezone.utc)
    fresh_cut = now - timedelta(days=FRESH_DAYS)

    def sort_key(r):
        mt = r["modtime"]
        if mt is None:
            return (1, datetime.min.replace(tzinfo=timezone.utc))
        return (0, -mt.timestamp()) if mt >= fresh_cut else (2, -mt.timestamp())

    return sorted(results, key=sort_key)


def has_fresh_results(results: list[dict], days: int = FRESH_DAYS) -> bool:
    """
    True если в выдаче есть документы свежее N дней.
    Документы без modtime считаем потенциально свежими (Яндекс часто не
    отдаёт дату) — поэтому пустая выдача -> False, выдача без дат -> True.
    """
    if not results:
        return False
    cut = datetime.now(timezone.utc) - timedelta(days=days)
    for r in results:
        if r["modtime"] is None or r["modtime"] >= cut:
            return True
    return False


# Чужие письменности в сниппетах выдачи. Найдено владельцем 02.08: в русский
# пост попало «В 1989 году在东德 Дрездене» -- модель скопировала иноязычный
# фрагмент прямо из материала, тем более что промпт прямо требует «используй
# только эти факты, не выдумывай». Дешевле не давать ей такой материал, чем
# потом ловить результат на выходе (ловим и там тоже, см. tasks._foreign_script_chars).
_FOREIGN_SCRIPT_RE = re.compile("[一-鿿぀-ゟ゠-ヿ가-힯؀-ۿ֐-׿฀-๿]")


def _has_foreign_script(text: str) -> bool:
    return bool(_FOREIGN_SCRIPT_RE.search(text or ""))


def format_search_context(results: list[dict], limit: int = 3500) -> str:
    """
    Сниппеты выдачи -> блок контекста для промпта генерации.

    Результаты с иероглифами/арабицей/хангылем выбрасываем целиком: русский
    пост не должен собираться из иноязычных кусков. Если после фильтра не
    осталось ничего -- возвращаем пусто, генерация корректно уходит в режим
    без поиска (см. generator: пустой материал обрабатывается штатно).
    """
    if not results:
        return ""
    chunks = []
    dropped = 0
    for r in results:
        if _has_foreign_script(f"{r.get('title', '')} {r.get('snippet', '')}"):
            dropped += 1
            continue
        date_str = r["modtime"].strftime("%d.%m.%Y") if r["modtime"] else ""
        head = f"• {r['title']}" + (f" ({date_str})" if date_str else "")
        chunks.append(f"{head}\n{r['snippet']}\nИсточник: {r['url']}")
    if dropped:
        logger.info(f"format_search_context: отброшено иноязычных результатов: {dropped}")
    text = "\n\n".join(chunks)
    return text[:limit]
