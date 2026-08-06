"""
Разброс времени публикации (`interval_jitter_minutes`).

Владелец 05.08: «ставлю разброс, а во всех постах в очереди всё равно
написано „опубликуется в 18:30“». И сам же назвал правильное решение: сдвиг
должен применяться к ВРЕМЕНИ ВЫКЛАДКИ поста, а не к длине шага.

Прежняя реализация прибавляла сдвиг к интервалу, и это давало две беды, обе
воспроизведены до правки:

1. Сдвиг НАКАПЛИВАЛСЯ: каждый следующий слот считался от уже сдвинутого
   предыдущего, и время уезжало в одну сторону — 18:27, 19:27, 20:03, 20:27,
   20:45, 21:00. «Раз в сутки» переставало значить «примерно в одно время».
2. Окно публикации его СТИРАЛО: слот, вышедший за окно, зажимался ровно в
   его начало, и все посты вставали на одну минуту. Ровно то, что владелец
   и увидел.
"""

from datetime import datetime, timedelta

import pytest

import tasks
from database import Channel


def _channel(**kw) -> Channel:
    base = dict(id=7, user_id=1, title="Канал", interval_hours=24,
                interval_jitter_minutes=60, schedule_kind="interval",
                publish_window_start="", publish_window_end="")
    base.update(kw)
    return Channel(**base)


def _chain(ch: Channel, start: datetime, n: int = 8) -> list:
    out, cur = [], start
    for _ in range(n):
        cur = tasks._next_slot_after(ch, cur)
        out.append(cur)
    return out


def test_posts_inside_a_window_do_not_all_land_on_its_first_minute():
    """Жалоба владельца дословно: «во всех постах написано 18:30». Окно
    зажимало слот ровно в своё начало и стирало весь разброс."""
    ch = _channel(publish_window_start="18:30", publish_window_end="20:00")
    times = _chain(ch, datetime(2026, 8, 5, 18, 40))
    distinct = {t.strftime("%H:%M") for t in times}
    assert len(distinct) >= len(times) - 1, (
        f"посты слиплись на одном времени: {sorted(distinct)}"
    )
    assert distinct != {"18:30"}, "всё ещё ровно начало окна"


def test_jitter_keeps_posts_inside_the_window():
    """Разброс не имеет права вытолкнуть пост за окно: окно — обещание,
    которое мы даём прямо на экране («публикуем только в это время»)."""
    ch = _channel(publish_window_start="18:30", publish_window_end="20:00")
    for t in _chain(ch, datetime(2026, 8, 5, 18, 40), n=20):
        minutes = t.hour * 60 + t.minute
        assert 18 * 60 + 30 <= minutes <= 20 * 60, f"пост вне окна: {t}"


def test_jitter_does_not_accumulate_over_time():
    """Главная скрытая беда. Сдвиг обязан быть шумом вокруг ровного времени,
    а не постоянной добавкой: иначе за неделю канал уезжает на часы, и
    «раз в сутки» перестаёт быть правдой."""
    ch = _channel(interval_hours=24, interval_jitter_minutes=60)
    times = _chain(ch, datetime(2026, 8, 5, 18, 0), n=14)
    minutes_of_day = [t.hour * 60 + t.minute for t in times]
    drift = max(minutes_of_day) - min(minutes_of_day)
    assert drift <= 60 + 5, (
        f"время публикации уехало на {drift} мин за две недели: "
        f"{[t.strftime('%d.%m %H:%M') for t in times]}"
    )


def test_jitter_shifts_forward_from_the_even_time():
    """Сдвиг применяется ТОЛЬКО вперёд от ровного времени — пост не выходит
    раньше, чем обещало расписание.

    Важная оговорка, на которой я сам сначала написал неверный тест: это НЕ
    значит, что зазор между соседними постами всегда ≥ интервала. Зазор равен
    интервалу плюс разница сдвигов, то есть гуляет в пределах ±разброс. Пока
    разброс не больше половины интервала, зазор остаётся положительным и
    посты не могут поменяться местами — это и проверяем.
    """
    ch = _channel(interval_hours=24, interval_jitter_minutes=60)
    limit = tasks._jitter_limit(ch)
    times = _chain(ch, datetime(2026, 8, 5, 12, 0), n=12)

    for slot in times:
        assert tasks._jitter_offset(ch, tasks._strip_jitter(ch, slot)) >= 0

    assert times == sorted(times), "посты в очереди перемешались"
    for a, b in zip(times, times[1:]):
        gap_min = (b - a).total_seconds() / 60
        assert 24 * 60 - limit <= gap_min <= 24 * 60 + limit, (
            f"зазор {gap_min:.0f} мин вышел за интервал ± разброс"
        )


def test_jitter_is_deterministic():
    """Одна и та же очередь при пересчёте обязана дать те же времена —
    иначе обратный отсчёт на карточке дёргался бы при каждой перерисовке."""
    ch = _channel(publish_window_start="09:00", publish_window_end="21:00")
    first = _chain(ch, datetime(2026, 8, 5, 10, 0))
    second = _chain(ch, datetime(2026, 8, 5, 10, 0))
    assert first == second


def test_jitter_is_capped_by_half_the_interval():
    """Разброс 60 минут при интервале 15 минут перемешал бы посты между
    собой. Зажимаем, а на экране честно пишем, сколько применили."""
    ch = _channel(interval_hours=0.25, interval_jitter_minutes=60)
    assert tasks._jitter_limit(ch) <= 8
    times = _chain(ch, datetime(2026, 8, 5, 10, 0), n=10)
    assert times == sorted(times), "посты в очереди перемешались"


def test_jitter_is_capped_by_the_window_length():
    """Окно в полчаса не вмещает разброс в два часа."""
    ch = _channel(interval_jitter_minutes=120,
                  publish_window_start="09:00", publish_window_end="09:30")
    assert tasks._jitter_limit(ch) == 30


def test_zero_jitter_means_exactly_the_interval():
    """Разброс выключен — расписание работает ровно как раньше."""
    ch = _channel(interval_jitter_minutes=0)
    times = _chain(ch, datetime(2026, 8, 5, 12, 0), n=5)
    for a, b in zip(times, times[1:]):
        assert (b - a) == timedelta(hours=24)


def test_hand_picked_time_is_not_reinvented():
    """Человек выбрал время кнопкой «Написать на своё время». Снятие сдвига
    подбором не должно испортить чужое время: если подбор не сошёлся, берём
    как есть."""
    ch = _channel(interval_jitter_minutes=60)
    picked = datetime(2026, 8, 5, 13, 37)
    stripped = tasks._strip_jitter(ch, picked)
    assert (picked - stripped) <= timedelta(minutes=tasks._jitter_limit(ch))


async def test_effective_jitter_is_reported_to_the_screen(client, token):
    """Экран не должен показывать выбранное значение, если применяется
    другое: ровно из-за такого расхождения окно публикации полгода
    «работало» вхолостую (правило 5)."""
    r = await client.post("/api/channels", json={
        "title": "Разброс", "about": "тема", "tg_chat": "@jitter_http",
        "interval_hours": 24,
    }, headers={"Authorization": f"Bearer {token}"})
    r.raise_for_status()
    cid = r.json()["id"]

    r = await client.patch(f"/api/channels/{cid}", json={
        "interval_jitter_minutes": 120,
        "publish_window_start": "09:00", "publish_window_end": "09:30",
    }, headers={"Authorization": f"Bearer {token}"})
    r.raise_for_status()
    assert r.json()["interval_jitter_minutes"] == 120, "выбор человека не сохранился"
    assert r.json()["jitter_effective_minutes"] == 30, (
        f"экрану не сказали, что применяется меньше: {r.json()['jitter_effective_minutes']}"
    )
