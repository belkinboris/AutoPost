"""
Тесты класса ошибок «интерфейс обещает то, чего система не делает»
(правило 5 в CLAUDE.md).

Аудит 02.08 нашёл двенадцать таких мест сразу, и это не совпадение: у всех
одна форма. Настройка есть на экране, но не доезжает до базы; надпись
описывает поведение, которого в коде нет; состояние поста показывается
фразой из другого режима. Обычные тесты этот класс не ловят, потому что
каждая функция по отдельности работает правильно — врёт стык между ними.

Здесь три проверки на стыки, а не на функции:

1. Всё, что экран отправляет, должно сохраняться. Настройка «окно
   публикации» полгода не работала ровно потому, что `publish_window_start`
   не было в `ChannelPatch`: pydantic молча выбрасывал поле, экран показывал
   «Сохранено ✓», а в базе не менялось ничего. Ни один тест не падал —
   каждый проверял свою половину.

2. Каждое сочетание «режим канала × статус поста × подтверждение» должно
   быть самосогласованным. Владелец нашёл на живом канале пост с надписью
   «ждёт вашего решения» и зелёной кнопкой «Опубликовать» — на канале с
   включённым автопилотом, где интерфейс двумя строками выше обещает
   «подтверждать ничего не нужно». Проверяем всю таблицу, а не отдельные
   клетки.

3. Обещание паузы. «Пока канал на паузе, новые посты не создаются и ничего
   не публикуется» — обе половины, а не только первая: до 02.08 пауза
   останавливала генерацию, но не публикацию уже стоящих в очереди постов.
"""

import re
from datetime import datetime, timedelta
from itertools import product
from pathlib import Path

import pytest

import database
import tasks
from database import Channel, Post, PostApproval, User
from schemas import ChannelIn, ChannelPatch

STATIC = Path(__file__).parent / "static"


# ── 1. Настройка с экрана обязана доехать до базы ──────────────────────────

# Функции фронтенда, которые сохраняют настройки канала (app.part13.js).
# Если появится четвёртая — её надо дописать сюда, иначе её поля окажутся
# непроверенными; проверка _extraction_is_not_empty ниже страхует от того,
# что список молча перестанет что-либо находить.
_SAVE_FUNCTIONS = ("_silentSave", "saveChannel", "saveAdvanced")


def _payload_fields_sent_by_frontend() -> set:
    """Имена полей, которые экран кладёт в PATCH /api/channels/{id}.

    Разбираем JS текстом намеренно: любая другая проверка (например, список
    полей, записанный руками в тесте) устареет ровно тогда же, когда
    устареет код, — и промолчит.
    """
    src = (STATIC / "app.part13.js").read_text(encoding="utf-8")
    fields = set()
    for name in _SAVE_FUNCTIONS:
        start = src.index(f"function {name}(")
        rest = src[start + 1:]
        m = re.search(r"\n(?:async )?function ", rest)
        body = rest[: m.start()] if m else rest
        # `title:(...)` и `auto_publish:$("sw_auto")...` — ключи объекта
        fields |= set(re.findall(r"[\s{,]([a-z][a-z0-9_]*)\s*:", body))
        # `payload.queue_depth=App._chan.queue_depth`
        fields |= set(re.findall(r"payload\.([a-z][a-z0-9_]*)\s*=", body))
    return fields


def test_extraction_is_not_empty():
    """Страховка от «зелёного» теста, который на самом деле ничего не
    проверяет: если разбор JS перестанет находить поля (переименовали
    функцию, поменяли форму вызова), тест ниже пройдёт на пустом множестве
    и мы об этом не узнаем. Такое в этом репозитории уже случалось — дважды.
    """
    fields = _payload_fields_sent_by_frontend()
    assert {"title", "about", "auto_publish", "interval_hours",
            "publish_window_start", "publish_window_end",
            "interval_jitter_minutes", "queue_depth"} <= fields, (
        f"разбор app.part13.js нашёл только: {sorted(fields)}"
    )


def test_every_channel_field_the_screen_sends_is_accepted_by_the_schema():
    """Главный тест этого файла.

    Поле, которое есть в модели `Channel` и которое экран отправляет в
    PATCH, обязано быть в `ChannelPatch` — иначе pydantic выбросит его
    молча. Именно так «окно публикации» и «разброс времени» жили на экране,
    ничего не делая: колонки в базе есть, поле в форме есть, в схеме — нет.
    """
    sent = _payload_fields_sent_by_frontend()
    missing = sorted(
        f for f in sent
        if f in Channel.model_fields and f not in ChannelPatch.model_fields
    )
    assert not missing, (
        f"экран отправляет {missing}, но ChannelPatch их не принимает — "
        f"настройка молча не сохранится"
    )


def test_channel_create_accepts_the_same_fields_as_patch():
    """Асимметрия между «создать» и «изменить» — та же ловушка с другого
    входа: настройка, которую можно поменять, но нельзя задать при
    создании, ведёт себя по-разному на новом и на старом канале.
    """
    for f in ("publish_window_start", "publish_window_end", "interval_jitter_minutes",
              "interval_hours", "auto_publish"):
        assert f in ChannelIn.model_fields, f"{f} нельзя задать при создании канала"
        assert f in ChannelPatch.model_fields, f"{f} нельзя изменить"


async def test_schedule_settings_actually_reach_the_database(client, token):
    """Круговой рейс: отправили — прочитали из базы. Проверяем именно базу,
    а не ответ эндпоинта: ответ собирается из того же объекта в памяти и
    зелёный даже тогда, когда до commit значение не дошло.
    """
    r = await client.post("/api/channels", json={
        "title": "Настройки", "about": "тема", "tg_chat": "@settings_demo",
    }, headers={"Authorization": f"Bearer {token}"})
    r.raise_for_status()
    cid = r.json()["id"]

    r = await client.patch(f"/api/channels/{cid}", json={
        "interval_hours": 8,
        "interval_jitter_minutes": 25,
        "publish_window_start": "09:30",
        "publish_window_end": "21:00",
    }, headers={"Authorization": f"Bearer {token}"})
    r.raise_for_status()

    with database.session() as s:
        ch = s.get(Channel, cid)
        assert ch.interval_hours == 8
        assert ch.interval_jitter_minutes == 25
        assert ch.publish_window_start == "09:30"
        assert ch.publish_window_end == "21:00"


async def test_saved_publish_window_actually_moves_the_slot(client, token):
    """Сохранить мало — настройка должна что-то менять. Окно 09:00–10:00
    обязано притянуть время поста внутрь себя; если `_next_slot_after` его
    игнорирует, значение в базе остаётся красивой, но мёртвой строкой.
    """
    r = await client.post("/api/channels", json={
        "title": "Окно", "about": "тема", "tg_chat": "@window_demo",
    }, headers={"Authorization": f"Bearer {token}"})
    r.raise_for_status()
    cid = r.json()["id"]
    r = await client.patch(f"/api/channels/{cid}", json={
        "interval_hours": 24, "interval_jitter_minutes": 0,
        "publish_window_start": "09:00", "publish_window_end": "10:00",
    }, headers={"Authorization": f"Bearer {token}"})
    r.raise_for_status()

    with database.session() as s:
        ch = s.get(Channel, cid)
        # Отсчитываем от полудня: +24ч попадает снова в полдень, то есть
        # мимо окна, и слот обязан переехать на 09:00 следующего дня.
        anchor = datetime(2026, 8, 2, 12, 0, 0)
        slot = tasks._next_slot_after(ch, anchor)
        assert (slot.hour, slot.minute) == (9, 0), f"окно не применилось: {slot}"


async def test_broken_window_is_rejected_loudly(client, token):
    """Кривое значение не сохраняем молча: экран показал бы «Сохранено ✓»
    рядом с полем, которое ни на что не влияет — та же болезнь, только уже
    по вине пользователя, а не схемы.
    """
    r = await client.post("/api/channels", json={
        "title": "Кривое окно", "about": "тема", "tg_chat": "@badwindow_demo",
    }, headers={"Authorization": f"Bearer {token}"})
    r.raise_for_status()
    cid = r.json()["id"]

    r = await client.patch(f"/api/channels/{cid}", json={"publish_window_start": "25:99"},
                           headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 400
    with database.session() as s:
        assert s.get(Channel, cid).publish_window_start == ""


# ── 2. Таблица состояний: режим × статус поста × подтверждение ─────────────

_STATUSES = ["scheduled", "pending", "published", "rejected", "failed"]
_APPROVALS = [None, "waiting", "done"]


def _make_state(email_tag: str, *, auto_publish: bool, enabled: bool,
                status: str, approval: str | None) -> tuple:
    """Собирает одну клетку таблицы и возвращает (channel_id, post_id)."""
    with database.session() as s:
        u = User(email=f"{email_tag}@example.com", password_hash="x", token_balance=100_000)
        s.add(u); s.commit(); s.refresh(u)
        ch = Channel(user_id=u.id, title="Канал", about="тема", tg_chat=f"@{email_tag}",
                     verified=True, enabled=enabled, auto_publish=auto_publish,
                     schedule_kind="interval", interval_hours=24, interval_jitter_minutes=0)
        s.add(ch); s.commit(); s.refresh(ch)
        past = datetime.utcnow() - timedelta(minutes=5)
        p = Post(channel_id=ch.id, user_id=u.id, text="текст", status=status,
                 scheduled_at=past if status in ("scheduled", "published") else None)
        s.add(p); s.commit(); s.refresh(p)
        if approval:
            s.add(PostApproval(post_id=p.id, channel_id=ch.id, review_chat_id=1,
                               status=approval, deadline=past))
            s.commit()
        return ch.id, p.id


@pytest.mark.parametrize("auto_publish,enabled,status,approval",
                         list(product([True, False], [True, False], _STATUSES, _APPROVALS)))
def test_state_table_selectors_agree_with_the_mode(auto_publish, enabled, status, approval):
    """Все 60 сочетаний разом.

    Два правила, которые интерфейс обещает и от которых зависит вся модель
    очереди (правило 4 в CLAUDE.md):

    * сам, без нажатия кнопки, публикуется ТОЛЬКО пост автопилота на
      работающем канале — и висящее с прошлого режима подтверждение это не
      отменяет (иначе пост не выйдет никогда и займёт слот навсегда);
    * подтверждения существуют только в режиме подтверждения — на
      автопилоте их не должно забирать ничто, иначе пост вечно переносится
      в конец очереди на канале, где «подтверждать ничего не нужно».
    """
    tag = f"st_{int(auto_publish)}{int(enabled)}_{status}_{approval or 'none'}"
    cid, pid = _make_state(tag, auto_publish=auto_publish, enabled=enabled,
                           status=status, approval=approval)
    now = datetime.utcnow()

    with database.session() as s:
        due_posts = {p.id for p in database.due_scheduled_posts(s, now)}
        due_appr = {a.post_id for a in database.due_post_approvals(s, now)}

    should_publish = (status == "scheduled" and auto_publish and enabled)
    assert (pid in due_posts) is should_publish, (
        f"auto={auto_publish} enabled={enabled} status={status} approval={approval}: "
        f"пост {'должен' if should_publish else 'не должен'} публиковаться сам"
    )

    should_requeue = (approval == "waiting" and not auto_publish)
    assert (pid in due_appr) is should_requeue, (
        f"auto={auto_publish} enabled={enabled} status={status} approval={approval}: "
        f"подтверждение {'должно' if should_requeue else 'не должно'} истекать"
    )

    # И главное: ни одна клетка не может одновременно и публиковаться сама,
    # и ждать подтверждения. Ровно это владелец увидел на живом канале.
    assert not (pid in due_posts and pid in due_appr), (
        "пост одновременно публикуется сам и ждёт подтверждения"
    )


@pytest.mark.parametrize("approval", ["waiting", "awaiting_edit"])
async def test_switching_to_autopilot_leaves_no_post_waiting_for_a_decision(approval):
    """После включения автопилота на канале не должно остаться ни одного
    поста в состоянии «ждёт вашего решения»: интерфейс автопилота обещает
    «подтверждать ничего не нужно», а карточка такого поста показывала
    зелёную кнопку «Опубликовать» и обратный отсчёт.
    """
    cid, pid = _make_state(f"switch_{approval}", auto_publish=False, enabled=True,
                           status="scheduled", approval=approval)
    with database.session() as s:
        ch = s.get(Channel, cid)
        ch.auto_publish = True
        s.add(ch); s.commit()

    await tasks.sync_posts_to_channel_mode(cid)

    with database.session() as s:
        left = s.exec(
            database.select(PostApproval).where(
                PostApproval.channel_id == cid,
                PostApproval.status.in_(["waiting", "awaiting_edit"]),
            )
        ).all()
        assert not left, "на автопилоте остались незакрытые подтверждения"
        # И пост при этом не «потерялся»: он по-прежнему в очереди со своим
        # временем, а не завис без scheduled_at.
        p = s.get(Post, pid)
        assert p.status == "scheduled" and p.scheduled_at is not None


async def test_autopilot_publishes_post_left_over_from_confirm_mode():
    """Обратная сторона того же: закрыть подтверждение мало — пост обязан
    после этого реально уйти в канал. Раньше `due_scheduled_posts` пропускал
    его из-за висящей строки PostApproval, и он не публиковался никогда.
    """
    cid, pid = _make_state("leftover_pub", auto_publish=False, enabled=True,
                           status="scheduled", approval="waiting")
    with database.session() as s:
        ch = s.get(Channel, cid)
        ch.auto_publish = True
        s.add(ch); s.commit()
    await tasks.sync_posts_to_channel_mode(cid)

    with database.session() as s:
        due = {p.id for p in database.due_scheduled_posts(s, datetime.utcnow())}
    assert pid in due, "пост от прежнего режима так и не попал в публикацию"


# ── 3. Обещание паузы целиком ──────────────────────────────────────────────

async def test_stale_approval_on_a_published_post_is_closed_not_acted_on():
    """Уточнение к таблице выше. `due_post_approvals` не смотрит на статус
    поста, поэтому забирает и подтверждение, оставшееся от уже
    опубликованного поста, — и на первый взгляд это выглядит так, будто мы
    собираемся перенести опубликованный пост в конец очереди.

    Не собираемся: `_requeue_unconfirmed_post` первым делом проверяет
    `post.status != "scheduled"` и просто закрывает строку. Проверяем это
    явно, чтобы поведение было закреплено, а не подразумевалось: без такой
    проверки опубликованный пост получил бы новое время публикации и вышел
    бы к подписчикам второй раз.
    """
    cid, pid = _make_state("stale_after_publish", auto_publish=False, enabled=True,
                           status="published", approval="waiting")
    with database.session() as s:
        appr = s.exec(database.select(PostApproval).where(PostApproval.post_id == pid)).first()
        appr_id, was_scheduled_at = appr.id, s.get(Post, pid).scheduled_at

    await tasks._requeue_unconfirmed_post(appr_id, pid, review_chat_id=1, review_message_id=None)

    with database.session() as s:
        assert s.get(PostApproval, appr_id).status == "done"
        p = s.get(Post, pid)
        assert p.status == "published", "опубликованный пост вернулся в очередь"
        assert p.scheduled_at == was_scheduled_at, "опубликованному посту сдвинули время"


def test_pause_stops_both_writing_and_publishing():
    """«Пока канал на паузе, новые посты не создаются и ничего не
    публикуется» (app.part11.js). До 02.08 верной была только первая
    половина: пауза выключала генерацию, а уже стоящие в очереди посты
    продолжали уходить подписчикам.
    """
    cid, pid = _make_state("paused_promise", auto_publish=True, enabled=False,
                           status="scheduled", approval=None)
    with database.session() as s:
        due = {p.id for p in database.due_scheduled_posts(s, datetime.utcnow())}
    assert pid not in due, "на паузе опубликовался уже стоявший в очереди пост"
