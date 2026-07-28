"""
Прогноз расписания для календаря (`tasks.project_upcoming_slots` +
`GET /api/channels/{id}/schedule_preview`).

Запрошено владельцем 28.07: если человеку подготовили посты на неделю раз в
день, а он поменял частоту (чаще или реже), календарь и очередь должны
перестроиться сразу же -- а не показывать расписание, посчитанное для
старой частоты. Смена самой частоты уже работала правильно (проверено
отдельно, без модели, до этого файла) -- не хватало только видимого
прогноза в календаре, который бы эту частоту отражал.

Главный риск здесь не в математике дат, а в честности: прогноз выглядит как
обещание "это опубликуется само". Для канала без автопилота такого
обещания нет и быть не должно -- решение всегда за пользователем. Поэтому
эндпоинт отдаёт слоты только при `auto_publish=True`; тесты закрепляют
именно это, а не только сами даты.
"""

from datetime import datetime, timedelta

import pytest
from sqlmodel import select

import database
import tasks
from database import Channel, User


def _make_channel(email: str, **kwargs) -> int:
    with database.session() as s:
        u = User(email=email, password_hash="x", token_balance=100_000)
        s.add(u); s.commit(); s.refresh(u)
        defaults = dict(user_id=u.id, title="Канал", about="тема", tg_chat="@demo",
                        verified=True, enabled=True, auto_publish=True,
                        schedule_kind="interval", interval_hours=24,
                        interval_jitter_minutes=0)
        defaults.update(kwargs)
        ch = Channel(**defaults)
        s.add(ch); s.commit(); s.refresh(ch)
        return ch.id


# ── Чистая математика расписания ───────────────────────────────────────────

def test_interval_slots_are_evenly_spaced():
    now = datetime(2026, 7, 28, 12, 0, 0)
    cid = _make_channel("interval@example.com", interval_hours=24, last_generated_at=now)
    with database.session() as s:
        ch = s.get(Channel, cid)
        slots = tasks.project_upcoming_slots(ch, now, count=5)
    assert len(slots) == 5
    gaps = {(slots[i + 1] - slots[i]).total_seconds() / 3600 for i in range(len(slots) - 1)}
    assert gaps == {24}


def test_changing_interval_immediately_changes_forecast():
    """Ключевой случай из просьбы владельца: смена частоты -- новый прогноз
    сразу, без пересчёта прошлого, без перезапуска чего-либо."""
    now = datetime(2026, 7, 28, 12, 0, 0)
    cid = _make_channel("resched@example.com", interval_hours=24, last_generated_at=now)
    with database.session() as s:
        ch = s.get(Channel, cid)
        before = tasks.project_upcoming_slots(ch, now, count=3)

        ch.interval_hours = 3  # ускорили -- несколько раз в день
        s.add(ch); s.commit(); s.refresh(ch)
        after_fast = tasks.project_upcoming_slots(ch, now, count=3)

        ch.interval_hours = 48  # передумали, раз в двое суток
        s.add(ch); s.commit(); s.refresh(ch)
        after_slow = tasks.project_upcoming_slots(ch, now, count=3)

    assert after_fast[0] == now + timedelta(hours=3)
    assert after_slow[0] == now + timedelta(hours=48)
    assert after_fast[-1] < before[-1] < after_slow[-1]


def test_daily_schedule_multiple_times_per_day():
    now = datetime(2026, 7, 28, 12, 0, 0)
    cid = _make_channel("daily@example.com", schedule_kind="daily",
                        daily_times='["09:00", "18:00"]', last_generated_at=None)
    with database.session() as s:
        ch = s.get(Channel, cid)
        slots = tasks.project_upcoming_slots(ch, now, count=4)
    assert [s.strftime("%H:%M") for s in slots] == ["18:00", "09:00", "18:00", "09:00"]
    assert all(s > now for s in slots)


def test_publish_window_clamps_interval_slots():
    now = datetime(2026, 7, 28, 12, 0, 0)
    cid = _make_channel("windowed@example.com", interval_hours=6, last_generated_at=now,
                        publish_window_start="09:00", publish_window_end="20:00")
    with database.session() as s:
        ch = s.get(Channel, cid)
        slots = tasks.project_upcoming_slots(ch, now, count=6)
    for slot in slots:
        minutes = slot.hour * 60 + slot.minute
        assert 9 * 60 <= minutes <= 20 * 60, f"слот {slot} вне окна публикации"


# ── Честность эндпоинта: прогноз только там, где есть на что рассчитывать ──

async def test_manual_channel_gets_empty_forecast(client, token):
    """Без автопилота обещать нечего -- решение всегда за пользователем."""
    r = await client.post("/api/channels", json={
        "title": "Ручной", "about": "тема", "tg_chat": "@manual_demo",
        "auto_publish": False, "interval_hours": 6,
    }, headers={"Authorization": f"Bearer {token}"})
    r.raise_for_status()
    cid = r.json()["id"]

    r = await client.get(f"/api/channels/{cid}/schedule_preview",
                         headers={"Authorization": f"Bearer {token}"})
    r.raise_for_status()
    assert r.json() == {"slots": []}


async def test_unverified_autopilot_gets_empty_forecast(client, token):
    """Найдено при ручной проверке 28.07: бот ещё не подтверждён -- значит
    tick() этот канал в due_ids вообще не возьмёт (см. `c.verified and
    _is_due(...)`), и рисовать прогноз публикаций, которые не наступят,
    было бы обманом, а не прогнозом."""
    r = await client.post("/api/channels", json={
        "title": "Не подтверждён", "about": "тема", "tg_chat": "@unverified_demo",
        "auto_publish": True, "interval_hours": 12,
    }, headers={"Authorization": f"Bearer {token}"})
    r.raise_for_status()
    cid = r.json()["id"]

    r = await client.get(f"/api/channels/{cid}/schedule_preview",
                         headers={"Authorization": f"Bearer {token}"})
    r.raise_for_status()
    assert r.json() == {"slots": []}


async def test_verified_autopilot_channel_gets_real_forecast(client, token):
    r = await client.post("/api/channels", json={
        "title": "Автопилот", "about": "тема", "tg_chat": "@auto_demo",
        "auto_publish": True, "interval_hours": 12,
    }, headers={"Authorization": f"Bearer {token}"})
    r.raise_for_status()
    cid = r.json()["id"]
    with database.session() as s:
        ch = s.get(Channel, cid)
        ch.verified = True
        s.add(ch); s.commit()

    r = await client.get(f"/api/channels/{cid}/schedule_preview",
                         headers={"Authorization": f"Bearer {token}"})
    r.raise_for_status()
    slots = r.json()["slots"]
    assert len(slots) == 30
    assert slots[0].endswith("Z")
