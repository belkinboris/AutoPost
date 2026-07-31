"""
Оценка поста автором (👍/👎) -- таблица `PostFeedback` и
`POST /api/posts/{id}/feedback`.

Запрошено владельцем 28.07: пусть информация о качестве постов копится сама,
пока человек и так разбирает очередь. Это подступ к C1 («оценить качество
постов честно»): по «опубликован/отклонён» о качестве судить нельзя --
отклонить могли из-за неподходящей темы, а опубликовать «и так сойдёт».

Отдельно проверяется то, на чём прод падал уже четырежды (правило 3 в
CLAUDE.md): новая таблица с user_id обязана убираться при удалении аккаунта.
FK у неё намеренно нет, поэтому падения не будет -- но данные человека после
удаления аккаунта остаться не должны, а «шаг 7» такое молча анонимизирует и
возвращает {"ok": true}, то есть провал выглядел бы как успех.
"""

import pytest
from sqlmodel import select

import database
from database import Channel, Post, PostFeedback, User


async def _channel_with_post(client, token):
    r = await client.post("/api/channels", json={
        "title": "Канал", "about": "тема", "tg_chat": "@fb_demo",
    }, headers={"Authorization": f"Bearer {token}"})
    r.raise_for_status()
    cid = r.json()["id"]
    with database.session() as s:
        ch = s.get(Channel, cid)
        post = Post(channel_id=cid, user_id=ch.user_id, text="Текст поста", status="pending")
        s.add(post); s.commit(); s.refresh(post)
        return cid, post.id, ch.user_id


async def test_up_then_down_replaces_verdict(client, token):
    """Одна строка на пару (человек, пост): мнение меняется, а не копится."""
    cid, pid, uid = await _channel_with_post(client, token)
    h = {"Authorization": f"Bearer {token}"}

    r = await client.post(f"/api/posts/{pid}/feedback", json={"verdict": "up"}, headers=h)
    assert r.json()["verdict"] == "up"

    r = await client.post(f"/api/posts/{pid}/feedback", json={"verdict": "down"}, headers=h)
    assert r.json()["verdict"] == "down"

    with database.session() as s:
        rows = s.exec(select(PostFeedback).where(PostFeedback.post_id == pid)).all()
    assert len(rows) == 1, "оценки копятся вместо замены"
    assert rows[0].verdict == "down"


async def test_none_removes_feedback(client, token):
    """Повторное нажатие снимает оценку -- поставленную случайно нужно убирать."""
    cid, pid, uid = await _channel_with_post(client, token)
    h = {"Authorization": f"Bearer {token}"}

    await client.post(f"/api/posts/{pid}/feedback", json={"verdict": "up"}, headers=h)
    r = await client.post(f"/api/posts/{pid}/feedback", json={"verdict": "none"}, headers=h)
    assert r.json()["verdict"] is None

    with database.session() as s:
        rows = s.exec(select(PostFeedback).where(PostFeedback.post_id == pid)).all()
    assert rows == []


async def test_feedback_returned_with_posts(client, token):
    """Кнопка должна знать своё состояние после перезагрузки страницы."""
    cid, pid, uid = await _channel_with_post(client, token)
    h = {"Authorization": f"Bearer {token}"}
    await client.post(f"/api/posts/{pid}/feedback", json={"verdict": "up"}, headers=h)

    r = await client.get(f"/api/channels/{cid}/posts", headers=h)
    posts = {p["id"]: p for p in r.json()}
    assert posts[pid]["feedback"] == "up"


async def test_invalid_verdict_rejected(client, token):
    cid, pid, uid = await _channel_with_post(client, token)
    r = await client.post(f"/api/posts/{pid}/feedback", json={"verdict": "maybe"},
                          headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 400


async def test_cannot_rate_someone_elses_post(client, token):
    """Оценка ходит через _own_post -- чужой пост оценить нельзя."""
    cid, pid, uid = await _channel_with_post(client, token)

    r = await client.post("/api/register",
                          json={"email": "stranger_fb@test.local", "password": "test12345"})
    other = r.json()["token"]

    r = await client.post(f"/api/posts/{pid}/feedback", json={"verdict": "up"},
                          headers={"Authorization": f"Bearer {other}"})
    assert r.status_code == 404


async def test_feedback_removed_on_account_deletion(client, token):
    """Правило 3: новая таблица с user_id обязана убираться при удалении."""
    cid, pid, uid = await _channel_with_post(client, token)
    h = {"Authorization": f"Bearer {token}"}
    await client.post(f"/api/posts/{pid}/feedback", json={"verdict": "up"}, headers=h)

    with database.session() as s:
        assert s.exec(select(PostFeedback).where(PostFeedback.user_id == uid)).all()

    r = await client.request("DELETE", "/api/me", headers=h)
    assert r.status_code == 200, r.text

    # Проверяем не код ответа, а что строк в базе не осталось: шаг 7 при
    # неизвестном FK анонимизирует запись и всё равно отдаёт {"ok": true}.
    with database.session() as s:
        left = s.exec(select(PostFeedback).where(PostFeedback.user_id == uid)).all()
        assert left == [], "оценки пережили удаление аккаунта"
        assert s.get(User, uid) is None, "сам пользователь не удалён"
