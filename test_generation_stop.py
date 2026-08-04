"""
Остановка бесконечных попыток генерации (прод-инцидент 03.08).

Владелец удалил два поста, генерация пошла — и пять минут ничего не
появлялось. В логе: каждую минуту две генерации подряд, обе забракованы,
около 54 000 токенов в никуда, на экране мигало «генерируется…». Конкретную
причину (детектор дублей) убрали отдельно, но форма ошибки осталась бы:
в generate_for_channel есть ещё четыре пути, которые возвращают отказ и не
создают пост. Любой зациклился бы точно так же.

Здесь проверяется общий предохранитель: считаем неудачи подряд, после трёх
плановая генерация останавливается, причина показывается на экране, а
действия человека стоп снимают.
"""

from datetime import datetime

import pytest

import database
import tasks
from database import Channel, Payment, Post, User


def _make_channel(email: str, **kw) -> tuple[int, int]:
    with database.session() as s:
        u = User(email=email, password_hash="x", token_balance=100_000)
        s.add(u); s.commit(); s.refresh(u)
        defaults = dict(user_id=u.id, title="Канал", about="тема", tg_chat=f"@{email.split('@')[0]}",
                        verified=True, enabled=True)
        defaults.update(kw)
        ch = Channel(**defaults)
        s.add(ch); s.commit(); s.refresh(ch)
        return u.id, ch.id


def _streak(cid: int) -> int:
    with database.session() as s:
        return s.get(Channel, cid).gen_fail_streak or 0


def _stub_failing(monkeypatch, message="ИИ не смог определить тему"):
    """Генерация всегда возвращает отказ ТОГО ЖЕ ВИДА, что в инциденте:
    сама попытка состоялась, пост не создан."""
    calls = []

    async def _impl(channel_id, topic="", force_pending=False, target_scheduled_at=None):
        calls.append(channel_id)
        return {"ok": False, "message": message, "generation_failed": True}

    monkeypatch.setattr(tasks, "_generate_for_channel_impl", _impl)
    return calls


async def test_failures_accumulate_and_stop_scheduled_generation(monkeypatch):
    """Главный тест: очередь неполна, генерация падает -- и плановое
    пополнение обязано ЗАМОЛЧАТЬ после трёх попыток, а не долбить каждую
    минуту. Считаем именно вызовы: в инциденте их было пять за шесть минут и
    росло бы дальше."""
    calls = _stub_failing(monkeypatch)
    uid, cid = _make_channel("stop_streak@t.local")

    for _ in range(8):
        await tasks._refill_queue(cid)

    assert len(calls) == tasks.MAX_GEN_FAIL_STREAK, (
        f"плановая генерация не остановилась: попыток {len(calls)}"
    )
    assert _streak(cid) == tasks.MAX_GEN_FAIL_STREAK


async def test_reason_is_stored_for_the_screen(monkeypatch):
    """Молча остановиться ничем не лучше, чем молча повторять: причина
    обязана сохраниться, иначе показать на экране будет нечего."""
    _stub_failing(monkeypatch, message="Не удалось написать пост на нужном языке")
    uid, cid = _make_channel("stop_reason@t.local")

    await tasks.generate_for_channel(cid, respect_queue_depth=False)

    with database.session() as s:
        assert "нужном языке" in (s.get(Channel, cid).gen_fail_reason or "")


async def test_success_resets_the_streak(monkeypatch):
    """Две неудачи, потом удача -- счётчик обнуляется, иначе канал доедет до
    стопа за неделю случайных сбоев провайдера."""
    uid, cid = _make_channel("stop_reset_ok@t.local")
    _stub_failing(monkeypatch)
    for _ in range(2):
        await tasks.generate_for_channel(cid, respect_queue_depth=False)
    assert _streak(cid) == 2

    async def _ok(channel_id, topic="", force_pending=False, target_scheduled_at=None):
        return {"ok": True, "post_id": 1}

    monkeypatch.setattr(tasks, "_generate_for_channel_impl", _ok)
    await tasks.generate_for_channel(cid, respect_queue_depth=False)
    assert _streak(cid) == 0
    with database.session() as s:
        assert s.get(Channel, cid).gen_fail_reason == ""


async def test_queue_full_and_busy_are_not_failures(monkeypatch):
    """«Очередь полна» и «уже генерируется» -- штатные исходы, а не неудачи.
    Если считать их, канал с полной очередью упрётся в стоп на ровном месте и
    перестанет писать после первой же публикации."""
    uid, cid = _make_channel("stop_not_failures@t.local", queue_depth=1)
    with database.session() as s:
        s.add(Post(channel_id=cid, user_id=uid, text="пост", status="scheduled",
                   scheduled_at=datetime.utcnow()))
        s.commit()

    for _ in range(5):
        r = await tasks.generate_for_channel(cid)
        assert r.get("queue_full") is True, r
    assert _streak(cid) == 0


async def test_zero_balance_is_not_counted_as_a_failure(monkeypatch):
    """Про нулевой баланс экран очереди говорит отдельной красной плашкой с
    кнопкой пополнения. Считать его неудачей значило бы подменить понятную
    причину («кончились токены») размытой («мы не смогли написать»)."""
    uid, cid = _make_channel("stop_zero_balance@t.local")
    with database.session() as s:
        u = s.get(User, uid)
        u.token_balance = 0
        s.add(u); s.commit()

    for _ in range(5):
        await tasks.generate_for_channel(cid, respect_queue_depth=False)
    assert _streak(cid) == 0


@pytest.mark.parametrize("action", ["manual_generate", "reject", "delete"])
async def test_user_actions_lift_the_stop(client, token, monkeypatch, action):
    """Стоп обязан сниматься тем, что интерфейс человеку и предлагает: нажать
    «Написать пост сейчас», опубликовать или удалить пост. Иначе надпись на
    экране обещает рычаг, которого нет (правило 5 в CLAUDE.md)."""
    r = await client.post("/api/channels", json={
        "title": f"Стоп {action}", "about": "тема", "tg_chat": f"@stop_{action}",
    }, headers={"Authorization": f"Bearer {token}"})
    r.raise_for_status()
    cid = r.json()["id"]

    with database.session() as s:
        ch = s.get(Channel, cid)
        ch.verified = True
        ch.gen_fail_streak = tasks.MAX_GEN_FAIL_STREAK
        ch.gen_fail_reason = "тестовая причина"
        s.add(ch)
        p = Post(channel_id=cid, user_id=ch.user_id, text="пост", status="scheduled",
                 scheduled_at=datetime.utcnow())
        s.add(p); s.commit(); s.refresh(p)
        pid = p.id

    async def _noop_refill(_cid):
        return None

    monkeypatch.setattr(tasks, "_refill_queue", _noop_refill)

    attempted = []

    async def _impl(channel_id, topic="", force_pending=False, target_scheduled_at=None):
        attempted.append(channel_id)
        return {"ok": True, "post_id": None}

    monkeypatch.setattr(tasks, "_generate_for_channel_impl", _impl)

    hdr = {"Authorization": f"Bearer {token}"}
    if action == "manual_generate":
        # Тут проверяем не только счётчик: кнопка обязана ДОЙТИ до генерации,
        # даже когда плановая остановлена. Без этой проверки тест был бы
        # зелёным и без сброса -- удачная генерация обнуляет счётчик сама
        # (проверено мутацией: строку сброса убрал, тест не заметил).
        attempted.clear()
        await client.post(f"/api/channels/{cid}/generate", json={}, headers=hdr)
        assert attempted, "ручная кнопка не сработала при остановленной плановой генерации"
    elif action == "reject":
        await client.post(f"/api/posts/{pid}/reject", headers=hdr)
    else:
        await client.delete(f"/api/posts/{pid}", headers=hdr)

    assert _streak(cid) == 0, f"«{action}» не сняло стоп -- рычаг на экране не работает"


async def test_channel_payload_exposes_the_stop(client, token):
    """Фронту нужен и факт, и причина: без них плашку нечем нарисовать."""
    r = await client.post("/api/channels", json={
        "title": "Стоп в ответе", "about": "тема", "tg_chat": "@stop_payload",
    }, headers={"Authorization": f"Bearer {token}"})
    r.raise_for_status()
    cid = r.json()["id"]
    assert r.json()["generation_stopped"] is False

    with database.session() as s:
        ch = s.get(Channel, cid)
        ch.gen_fail_streak = tasks.MAX_GEN_FAIL_STREAK
        ch.gen_fail_reason = "ИИ не смог определить тему"
        s.add(ch); s.commit()

    r = await client.get(f"/api/channels/{cid}", headers={"Authorization": f"Bearer {token}"})
    r.raise_for_status()
    assert r.json()["generation_stopped"] is True
    assert r.json()["generation_stopped_reason"] == "ИИ не смог определить тему"
