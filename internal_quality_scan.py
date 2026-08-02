"""
GET /api/internal/quality-scan -- автоматический поиск того, что пользователь
увидит как «сервис работает плохо».

Зачем нужен именно такой эндпоинт. Дефекты этого класса не ловятся чтением
кода: код формально корректен. Два поста-близнеца про одно событие
сгенерировались при том, что в промпте прямо стоял запрет повторять темы, а
дедупликация была написана и работала -- просто сравнивала заголовки, а не
содержание. Увидеть это можно было только положив рядом два реальных поста.

Поэтому здесь проверяется не код, а ФАКТИЧЕСКИЙ РЕЗУЛЬТАТ работы сервиса за
последние дни. Каждая находка возвращается с примерами, чтобы её можно было
проверить руками, а не верить на слово.

Формат ответа рассчитан на то, что его целиком отдают ассистенту с просьбой
разобрать причины и внести исправления.

Приватность: эндпоинт закрыт тем же TRUEPOST_INTERNAL_API_TOKEN, что и
остальные internal-ручки, и отдаёт только короткие фрагменты постов (до 160
символов), не полные тексты. Это осознанно отличается от internal_user_journeys,
который текстов не отдаёт вовсе: тот писался для внешнего Growth Agent, а
этот -- для владельца сервиса, которому нужно видеть, что именно вышло не так.
"""

import os
import re
from collections import defaultdict
from datetime import datetime, timedelta

from fastapi import APIRouter, Header, HTTPException
from sqlmodel import select

import database
from database import User, Channel, Post

router = APIRouter()

INTERNAL_API_TOKEN = os.environ.get("TRUEPOST_INTERNAL_API_TOKEN")

# Сколько постов максимум разбираем за один прогон. Сравнение на дубли
# квадратично по числу постов внутри канала, поэтому ограничение нужно.
MAX_POSTS = 500
EXCERPT = 160


def _check_auth(authorization: str | None) -> None:
    if not INTERNAL_API_TOKEN:
        raise HTTPException(status_code=503, detail="TRUEPOST_INTERNAL_API_TOKEN not configured on this server")
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing or invalid Authorization header")
    if authorization.removeprefix("Bearer ").strip() != INTERNAL_API_TOKEN:
        raise HTTPException(status_code=401, detail="Invalid token")


def _head(text: str, n: int = EXCERPT) -> str:
    """Первая строка поста без разметки -- чтобы находку можно было опознать."""
    clean = re.sub(r"<[^>]+>", "", (text or "")).strip()
    first = clean.split("\n")[0].strip()
    return first[:n]


def _finding(code, severity, title, count, why, examples=None, fix=None):
    f = {
        "code": code,
        "severity": severity,          # high | medium | low
        "title": title,
        "count": count,
        "why_it_matters": why,
        "examples": examples or [],
    }
    if fix:
        f["suggested_check"] = fix
    return f


@router.get("/api/internal/quality-scan")
def quality_scan(
    period_days: int = 7,
    authorization: str | None = Header(default=None),
):
    """
    Прогоняет батарею проверок по реальным данным за period_days и возвращает
    findings -- список проблем, которые увидел бы пользователь.
    """
    _check_auth(authorization)
    since = datetime.utcnow() - timedelta(days=period_days)
    findings = []

    with database.session() as s:
        posts = list(s.exec(
            select(Post).where(Post.created_at >= since)
            .order_by(Post.created_at.desc()).limit(MAX_POSTS)
        ).all())
        channels = {c.id: c for c in s.exec(select(Channel)).all()}
        users_total = len(s.exec(select(User).where(User.created_at >= since)).all())

    by_channel = defaultdict(list)
    for p in posts:
        by_channel[p.channel_id].append(p)

    # ── 1. Посты-близнецы ─────────────────────────────────────────────
    # Главная проверка. Дубли в канале -- то, за что отписываются и отменяют
    # подписку, и то, что невозможно заметить по коду.
    try:
        # Берём _is_duplicate, а не голый порог: скан должен быть НЕ МЕНЕЕ
        # чувствителен, чем генерация. Раньше он импортировал ту же функцию и
        # тот же порог, то есть был слеп ровно там же, где и превентивная
        # проверка -- и пару близнецов с прода 02.08 не увидел (аудит).
        from tasks import _similarity, _is_duplicate, DUPLICATE_THRESHOLD
        dup_examples, dup_count = [], 0
        for cid, items in by_channel.items():
            for i in range(len(items)):
                for j in range(i + 1, len(items)):
                    sim = _similarity(items[i].text, items[j].text)
                    if _is_duplicate(items[i].text, items[j].text):
                        dup_count += 1
                        if len(dup_examples) < 5:
                            dup_examples.append({
                                "channel": (channels.get(cid).title if channels.get(cid) else cid),
                                "similarity": round(sim, 3),
                                "post_a": {"id": items[i].id, "status": items[i].status, "head": _head(items[i].text)},
                                "post_b": {"id": items[j].id, "status": items[j].status, "head": _head(items[j].text)},
                            })
        if dup_count:
            findings.append(_finding(
                "duplicate_posts", "high",
                "Посты про одно и то же событие в одном канале",
                dup_count,
                "Пользователь видит в своей очереди или в канале два поста об одном факте под разными "
                "заголовками. Это самая заметная претензия к качеству и прямая причина отмены подписки.",
                dup_examples,
                "Открыть оба поста и убедиться, что это действительно один факт, а не разные события "
                "на общую тему. Если ложное срабатывание -- поднять DUPLICATE_THRESHOLD.",
            ))
    except Exception as e:  # проверка не должна ронять весь скан
        findings.append(_finding("duplicate_check_failed", "low",
                                 "Проверка на дубли не отработала", 1, str(e)))

    # ── 2. Разметка, которую Telegram не покажет ──────────────────────
    md = [p for p in posts if re.search(r"(^|\n)#{1,3}\s|\*\*|```", p.text or "")]
    if md:
        findings.append(_finding(
            "markdown_leak", "high",
            "В постах осталась Markdown-разметка",
            len(md),
            "Telegram не отображает #, ** и ``` -- читатель увидит эти символы как мусор прямо в посте. "
            "В промпте это запрещено, значит запрет местами не срабатывает.",
            [{"id": p.id, "status": p.status, "head": _head(p.text)} for p in md[:5]],
        ))

    # ── 2б. Чужая письменность в русском посте ────────────────────────
    # Найдено владельцем 02.08 на живом канале: «В 1989 году在东德 Дрездене».
    # Модель скопировала иноязычный фрагмент из поисковой выдачи. Теперь это
    # отсекается на входе (yandex_search) и на выходе (tasks), но скан нужен
    # чтобы увидеть, сколько такого уже лежит в очередях.
    from tasks import _foreign_script_chars
    foreign = [(p, _foreign_script_chars(p.text)) for p in posts]
    foreign = [(p, ch) for p, ch in foreign if ch]
    if foreign:
        findings.append(_finding(
            "foreign_script", "high",
            "В постах символы чужой письменности",
            len(foreign),
            "Читатель русского канала видит иероглифы или арабицу прямо в тексте поста. "
            "Обычно это скопированный фрагмент иноязычного источника из поисковой выдачи.",
            [{"id": p.id, "status": p.status, "chars": ch[:20], "head": _head(p.text)}
             for p, ch in foreign[:5]],
        ))

    # ── 3. Пост-переспрос вместо поста ────────────────────────────────
    ask = [p for p in posts if re.search(
        r"(уточните|какую тему|напишите тему|не могу определить|подскажите, о чём)",
        (p.text or "").lower())]
    if ask:
        findings.append(_finding(
            "clarifying_question_as_post", "high",
            "Вместо поста сгенерирован вопрос пользователю",
            len(ask),
            "В очередь попал не пост, а переспрос модели. Если такой текст уйдёт в канал, "
            "это выглядит как поломка сервиса.",
            [{"id": p.id, "status": p.status, "head": _head(p.text)} for p in ask[:5]],
        ))

    # ── 4. Аномальная длина ───────────────────────────────────────────
    short = [p for p in posts if len(re.sub(r"<[^>]+>", "", p.text or "").strip()) < 200]
    if short:
        findings.append(_finding(
            "too_short", "medium",
            "Подозрительно короткие посты",
            len(short),
            "Меньше 200 символов -- обычно признак оборванной генерации: модель не уложилась в лимит "
            "или ответ пришёл неполным. Публиковать такое нельзя.",
            [{"id": p.id, "status": p.status, "chars": len(p.text or ""), "head": _head(p.text)} for p in short[:5]],
        ))

    # ── 5. Подключённые каналы, где ничего не происходит ──────────────
    stuck = []
    for cid, ch in channels.items():
        if not (ch.verified and ch.enabled):
            continue
        if not by_channel.get(cid):
            stuck.append(ch)
    if stuck:
        findings.append(_finding(
            "connected_but_silent", "high",
            "Канал подключён, но постов нет",
            len(stuck),
            "Пользователь подключил канал и ждёт результата, а сервис молчит. Это худший из возможных "
            "первых опытов: человек сделал самый сложный шаг и не получил ничего.",
            [{"channel": c.title, "id": c.id, "about": (c.about or "")[:80]} for c in stuck[:5]],
            "Проверить логи генерации по этим каналам: закончился баланс токенов, отклонена тема "
            "или падает сам вызов модели.",
        ))

    # ── 6. Пользователи без единого поста ─────────────────────────────
    with database.session() as s:
        recent_users = list(s.exec(select(User).where(User.created_at >= since)).all())
        no_post = []
        for u in recent_users:
            has = s.exec(select(Post).where(Post.user_id == u.id)).first()
            if not has:
                no_post.append(u.id)
    if no_post:
        findings.append(_finding(
            "registered_without_post", "high",
            "Зарегистрировались и не получили ни одного поста",
            len(no_post),
            "Человек дошёл до регистрации и не увидел главного -- что сервис вообще умеет. "
            "Из всех точек оттока эта самая дорогая: за неё уже заплачено рекламой.",
            [{"user_id": uid} for uid in no_post[:10]],
            "Пройти онбординг самому с чистого аккаунта и найти, где обрывается путь.",
        ))

    # ── Итог ──────────────────────────────────────────────────────────
    order = {"high": 0, "medium": 1, "low": 2}
    findings.sort(key=lambda f: (order.get(f["severity"], 3), -f["count"]))

    return {
        "period_days": period_days,
        "scanned": {
            "posts": len(posts),
            "channels": len(channels),
            "new_users": users_total,
        },
        "findings": findings,
        "summary": (
            "Проблем не найдено." if not findings
            else f"Найдено проблем: {len(findings)} "
                 f"(критичных: {sum(1 for f in findings if f['severity']=='high')})."
        ),
        "how_to_use": (
            "Отдайте этот ответ целиком ассистенту с задачей: разобрать причину каждой находки в коде, "
            "исправить и проверить. Примеры в examples нужны, чтобы проверить находку руками, "
            "а не верить цифре."
        ),
    }
