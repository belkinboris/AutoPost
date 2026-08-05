"""
Пересборка очереди при смене расписания (владелец 04.08).

Он поставил окно публикации 16:00-18:00 и интервал «раз в сутки», а посты в
очереди остались стоять на 12:28 и 18:28 — по старым настройкам, с шагом в
шесть часов. Экран при этом обещает «Пишем и публикуем посты только в это
время»: настройка применялась только к постам, которых ещё нет, а обещание
было дано про все (правило 5 в CLAUDE.md).
"""

import json
from datetime import datetime, timedelta

import pytest

import config
import database
import tasks
from database import Channel, Post, PostApproval, User


def _channel(email: str, **kw) -> tuple[int, int]:
    with database.session() as s:
        u = User(email=email, password_hash="x", token_balance=100_000)
        s.add(u); s.commit(); s.refresh(u)
        defaults = dict(user_id=u.id, title="Канал", about="тема",
                        tg_chat=f"@{email.split('@')[0]}", verified=True, enabled=True,
                        auto_publish=True, schedule_kind="interval",
                        interval_hours=6, interval_jitter_minutes=0,
                        publish_window_start="", publish_window_end="")
        defaults.update(kw)
        ch = Channel(**defaults)
        s.add(ch); s.commit(); s.refresh(ch)
        return u.id, ch.id


def _queue(cid: int, uid: int, times: list) -> list:
    ids = []
    with database.session() as s:
        for i, t in enumerate(times):
            p = Post(channel_id=cid, user_id=uid, text=f"пост {i}",
                     status="scheduled", scheduled_at=t)
            s.add(p); s.commit(); s.refresh(p)
            ids.append(p.id)
    return ids


def _times(ids: list) -> list:
    with database.session() as s:
        return [s.get(Post, i).scheduled_at for i in ids]


async def test_new_window_moves_the_whole_queue_inside_it():
    """Главный случай владельца: окно 16:00-18:00 — значит ВСЕ посты в
    очереди обязаны оказаться внутри него, а не только будущие."""
    uid, cid = _channel("resched_window@t.local", interval_hours=24)
    now = datetime.utcnow()
    ids = _queue(cid, uid, [now + timedelta(hours=4), now + timedelta(hours=10)])

    with database.session() as s:
        ch = s.get(Channel, cid)
        ch.publish_window_start, ch.publish_window_end = "16:00", "18:00"
        s.add(ch); s.commit()

    moved = tasks.reschedule_queue(cid)
    assert moved == 2, f"переставлено постов: {moved}"
    for t in _times(ids):
        assert 16 <= t.hour < 18 or (t.hour == 18 and t.minute == 0), (
            f"пост остался вне окна публикации: {t}"
        )


async def test_new_interval_applies_to_posts_already_queued():
    """Второе, что удивило владельца: интервал стоял «раз в сутки», а посты
    были расставлены с шагом в шесть часов — по прежней настройке."""
    uid, cid = _channel("resched_interval@t.local", interval_hours=6)
    now = datetime.utcnow()
    ids = _queue(cid, uid, [now + timedelta(hours=1), now + timedelta(hours=7),
                            now + timedelta(hours=13)])

    with database.session() as s:
        ch = s.get(Channel, cid)
        ch.interval_hours = 24
        s.add(ch); s.commit()

    tasks.reschedule_queue(cid)
    times = _times(ids)
    for a, b in zip(times, times[1:]):
        gap = (b - a).total_seconds() / 3600
        assert 23.9 <= gap <= 24.1, f"шаг между постами {gap:.2f}ч вместо суток"


async def test_reschedule_keeps_the_order_people_see():
    """Порядок очереди человек видит списком и менять его не просил —
    переставляем только времена."""
    uid, cid = _channel("resched_order@t.local")
    now = datetime.utcnow()
    ids = _queue(cid, uid, [now + timedelta(hours=1), now + timedelta(hours=2),
                            now + timedelta(hours=3)])
    tasks.reschedule_queue(cid)
    times = _times(ids)
    assert times == sorted(times), "порядок постов в очереди перемешался"


async def test_reschedule_never_publishes_immediately():
    """Правило 4: человек правил расписание, а не нажимал «Опубликовать».
    Первый пост не должен уехать в канал в ту же минуту."""
    uid, cid = _channel("resched_no_instant@t.local")
    now = datetime.utcnow()
    ids = _queue(cid, uid, [now - timedelta(hours=2), now + timedelta(hours=1)])

    tasks.reschedule_queue(cid)
    first = _times(ids)[0]
    assert first > datetime.utcnow() + timedelta(minutes=1), (
        f"первый пост встал на {first} -- это публикация почти сразу после "
        f"сохранения настроек"
    )


async def test_confirm_mode_deadline_follows_the_post():
    """После C14 дедлайн подтверждения и время поста — одно и то же. Если
    пост поехал, а дедлайн остался, карточка в Телеграме считает до одного
    момента, а пост стоит на другом."""
    uid, cid = _channel("resched_deadline@t.local", auto_publish=False)
    now = datetime.utcnow()
    ids = _queue(cid, uid, [now + timedelta(hours=3)])
    with database.session() as s:
        s.add(PostApproval(post_id=ids[0], channel_id=cid, review_chat_id=1,
                           deadline=now + timedelta(hours=3), status="waiting"))
        s.commit()
        ch = s.get(Channel, cid)
        ch.interval_hours = 48
        s.add(ch); s.commit()

    tasks.reschedule_queue(cid)
    with database.session() as s:
        post = s.get(Post, ids[0])
        appr = s.exec(database.select(PostApproval)
                      .where(PostApproval.post_id == ids[0])).first()
    assert appr.deadline == post.scheduled_at, (
        f"дедлайн {appr.deadline} разъехался со временем поста {post.scheduled_at}"
    )


async def test_reschedule_does_not_touch_published_or_rejected():
    """Опубликованному посту время публикации менять нечего, а отклонённый
    вообще вне очереди."""
    uid, cid = _channel("resched_untouched@t.local")
    now = datetime.utcnow()
    with database.session() as s:
        pub = Post(channel_id=cid, user_id=uid, text="вышел", status="published",
                   scheduled_at=now - timedelta(days=1), published_at=now - timedelta(days=1))
        rej = Post(channel_id=cid, user_id=uid, text="отклонён", status="rejected",
                   scheduled_at=now - timedelta(hours=5))
        s.add(pub); s.add(rej); s.commit(); s.refresh(pub); s.refresh(rej)
        pub_id, rej_id, was_pub, was_rej = pub.id, rej.id, pub.scheduled_at, rej.scheduled_at

    tasks.reschedule_queue(cid)
    with database.session() as s:
        assert s.get(Post, pub_id).scheduled_at == was_pub
        assert s.get(Post, rej_id).scheduled_at == was_rej


async def test_saving_schedule_through_the_api_moves_the_queue(client, token):
    """Сквозная проверка: человек нажимает «Сохранить» в расширенных
    настройках -- очередь едет, и ответ говорит СКОЛЬКО постов поехало
    (фронт показывает это в тосте: молча двигать время публикации нельзя)."""
    hdr = {"Authorization": f"Bearer {token}"}
    r = await client.post("/api/channels", json={
        "title": "Расписание", "about": "тема", "tg_chat": "@resched_http",
        "auto_publish": True, "interval_hours": 6,
    }, headers=hdr)
    r.raise_for_status()
    cid = r.json()["id"]
    now = datetime.utcnow()
    with database.session() as s:
        ch = s.get(Channel, cid)
        ch.verified = True
        s.add(ch)
        for i in range(3):
            s.add(Post(channel_id=cid, user_id=ch.user_id, text=f"пост {i}",
                       status="scheduled", scheduled_at=now + timedelta(hours=1 + 6 * i)))
        s.commit()

    r = await client.patch(f"/api/channels/{cid}", json={
        "publish_window_start": "16:00", "publish_window_end": "18:00",
    }, headers=hdr)
    r.raise_for_status()
    assert r.json()["rescheduled_posts"] == 3, r.json()

    with database.session() as s:
        for p in s.exec(database.select(Post).where(Post.channel_id == cid)).all():
            assert 16 <= p.scheduled_at.hour <= 18, f"пост вне окна: {p.scheduled_at}"


async def test_editing_unrelated_settings_leaves_the_queue_alone(client, token):
    """Правка темы или стиля времени публикации не касается -- двигать
    очередь за компанию значило бы менять то, о чём не просили."""
    hdr = {"Authorization": f"Bearer {token}"}
    r = await client.post("/api/channels", json={
        "title": "Не трогать", "about": "тема", "tg_chat": "@resched_untouched_http",
        "interval_hours": 6,
    }, headers=hdr)
    r.raise_for_status()
    cid = r.json()["id"]
    now = datetime.utcnow()
    with database.session() as s:
        ch = s.get(Channel, cid)
        s.add(Post(channel_id=cid, user_id=ch.user_id, text="пост",
                   status="scheduled", scheduled_at=now + timedelta(hours=5)))
        s.commit()
    before = [p for p in _times_of_channel(cid)]

    r = await client.patch(f"/api/channels/{cid}", json={"about": "другая тема"},
                           headers=hdr)
    r.raise_for_status()
    assert r.json()["rescheduled_posts"] == 0
    assert _times_of_channel(cid) == before, "очередь поехала от правки темы"


def _times_of_channel(cid: int) -> list:
    with database.session() as s:
        return [p.scheduled_at for p in s.exec(
            database.select(Post).where(Post.channel_id == cid)
            .order_by(Post.scheduled_at)).all()]
