"""
Тесты правила «таймер идёт только тогда, когда мы предупредили человека».

Правило 4 в `CLAUDE.md` (обновлено 01–02.08, единая модель очереди, C14):
таймер подтверждения (`PostApproval`) больше не публикует пост по истечении
— он переносит его в конец очереди (`tasks._requeue_unconfirmed_post`).
Публикуют пост только автопилот (когда подошло время в очереди) или явное
«Опубликовать». Но правило «явно показанный» с самого начала было про сам
таймер, а не про то, что он делает по истечении, и осталось в силе
буквально: таймер переноса заводится только когда карточка реально
доставлена в Telegram.

Раньше запись `PostApproval` заводилась всегда, и у пользователя без
подключённых уведомлений пост публиковался (в исходной версии режима) или
переносился (сейчас) через 30 минут — а сам он об этом не узнавал ниоткуда,
кроме сайта, куда мог и не заходить. FAQ на лендинге при этом отвечал
«каждый пост сначала приходит вам в личку» — то есть система делала ровно
то, что мы обещали не делать.

Теперь без доставленной карточки таймер не заводится, и пост ждёт решения
сколько угодно — ни публикации, ни переноса. Эти тесты закрепляют оба
направления: и что таймер есть, когда предупредить удалось, и что его нет,
когда не удалось. Второе важнее: раньше ценой ошибки была публикация без
ведома владельца, сейчас — молчаливый перенос (лучше, но тоже не должен
происходить без карточки).
"""

from datetime import datetime, timedelta

import pytest
from sqlmodel import select

import database
import tasks
from database import Channel, Post, PostApproval, User


def _make_post(email: str) -> tuple[int, int]:
    with database.session() as s:
        u = User(email=email, password_hash="x", token_balance=100_000)
        s.add(u); s.commit(); s.refresh(u)
        ch = Channel(user_id=u.id, title="Канал", about="тема", tg_chat="@demo",
                     verified=True, enabled=True, auto_publish=False)
        s.add(ch); s.commit(); s.refresh(ch)
        p = Post(channel_id=ch.id, user_id=u.id, text="Текст поста", status="pending")
        s.add(p); s.commit(); s.refresh(p)
        return ch.id, p.id


def _approvals(post_id: int) -> list:
    with database.session() as s:
        return list(s.exec(select(PostApproval).where(PostApproval.post_id == post_id)).all())


@pytest.fixture
def telegram_ok(monkeypatch):
    """Карточка в Telegram доставлена."""
    sent = []

    async def _render(chat_id, msg_id, post_id, title, text, deadline):
        sent.append({"chat_id": chat_id, "post_id": post_id, "deadline": deadline})
        return {"ok": True, "result": {"message_id": 555}}

    monkeypatch.setattr(tasks, "_render_approval_card", _render)
    return sent


@pytest.fixture
def telegram_fails(monkeypatch):
    """Telegram ответил отказом: бот заблокирован, сеть, что угодно."""
    attempts = []

    async def _render(chat_id, msg_id, post_id, title, text, deadline):
        attempts.append(post_id)
        return {"ok": False, "description": "bot was blocked by the user"}

    monkeypatch.setattr(tasks, "_render_approval_card", _render)
    return attempts


# ── 1. Предупредили — таймер идёт ─────────────────────────────────────────

async def test_timer_starts_when_card_delivered(telegram_ok):
    cid, pid = _make_post("tg_ok@t.local")
    deadline = datetime.utcnow() + timedelta(minutes=30)
    await tasks._send_approval_card(pid, cid, 12345, "Канал", "Текст поста", deadline)

    got = _approvals(pid)
    assert len(got) == 1, "карточка доставлена — таймер должен быть заведён"
    assert got[0].review_chat_id == 12345
    assert got[0].review_message_id == 555
    assert got[0].deadline > datetime.utcnow(), "дедлайн должен быть в будущем"
    assert len(telegram_ok) == 1, "карточка должна была уйти ровно один раз"


# ── 2. Предупредить нечем — таймера нет ───────────────────────────────────

async def test_no_timer_without_telegram(telegram_ok):
    """
    Самый дорогой случай: у человека не подключены уведомления. Раньше пост
    уходил к подписчикам через 30 минут, а владелец узнавал об этом постфактум.
    """
    cid, pid = _make_post("no_tg@t.local")
    deadline = datetime.utcnow() + timedelta(minutes=30)
    await tasks._send_approval_card(pid, cid, None, "Канал", "Текст поста", deadline)

    assert _approvals(pid) == [], (
        "таймер заведён при неподключённых уведомлениях — пост опубликуется "
        "сам, а предупредить владельца нечем"
    )
    assert telegram_ok == [], "без chat_id в Telegram ходить незачем"


async def test_no_timer_when_delivery_failed(telegram_fails):
    """Бот заблокирован или Telegram недоступен — человек карточку не увидит."""
    cid, pid = _make_post("blocked@t.local")
    deadline = datetime.utcnow() + timedelta(minutes=30)
    await tasks._send_approval_card(pid, cid, 999, "Канал", "Текст поста", deadline)

    assert telegram_fails == [pid], "попытка отправки должна была быть"
    assert _approvals(pid) == [], (
        "карточка не доставлена, а таймер завели — пост уйдёт в канал молча"
    )


# ── 3. Пост при этом не теряется ──────────────────────────────────────────

async def test_post_stays_in_queue_without_timer(telegram_ok):
    """
    Без таймера пост не пропадает и не публикуется — он просто ждёт решения,
    как пост, написанный вручную. Интерфейс такой пост показывает с подписью
    «сам не опубликуется» (см. renderPostCard: подпись зависит от наличия
    approval_deadline).
    """
    cid, pid = _make_post("waits@t.local")
    deadline = datetime.utcnow() + timedelta(minutes=30)
    await tasks._send_approval_card(pid, cid, None, "Канал", "Текст поста", deadline)

    with database.session() as s:
        post = s.get(Post, pid)
        assert post is not None, "пост не должен исчезать"
        assert post.status == "pending", f"пост должен остаться в очереди, статус={post.status}"
