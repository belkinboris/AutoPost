"""
Тесты для GET /api/internal/user-events (Founder Live Feed, дискретные события).

Запуск:

    DATABASE_URL=sqlite:///test_ue.db TRUEPOST_INTERNAL_API_TOKEN=test-token SECRET_KEY=testsecret \\
        python3 -m uvicorn main:app --port 8307 --log-level error &
    sleep 3
    BASE_URL=http://localhost:8307 TRUEPOST_INTERNAL_API_TOKEN=test-token \\
        python3 test_user_events.py
"""

import asyncio
import os

import httpx

# Клиент берём из conftest: он ходит в приложение, поднятое в этом же процессе.
# Раньше здесь создавался обычный httpx.AsyncClient и запрос уходил в сеть на
# localhost:8000 -- под pytest это ConnectError, работало только с сервером,
# запущенным руками.
from conftest import make_client

BASE_URL = os.environ.get("BASE_URL", "http://localhost:8000")
INTERNAL_TOKEN = os.environ.get("TRUEPOST_INTERNAL_API_TOKEN", "test-token")

_counter = 0


def _email(prefix: str) -> str:
    global _counter
    _counter += 1
    return f"{prefix}_{_counter}@ue.test".lower()


async def _register(client: httpx.AsyncClient, email: str, **extra) -> dict:
    payload = {"email": email, "password": "test12345", **extra}
    r = await client.post(f"{BASE_URL}/api/register", json=payload)
    r.raise_for_status()
    return r.json()


async def _product_event(client: httpx.AsyncClient, token: str, event: str, package_id: str = ""):
    r = await client.post(
        f"{BASE_URL}/api/product-event",
        json={"event": event, "package_id": package_id},
        headers={"Authorization": f"Bearer {token}"},
    )
    r.raise_for_status()
    return r.json()


async def _events(client: httpx.AsyncClient, period_minutes: int = 120, limit: int | None = None) -> dict:
    params = {"period_minutes": period_minutes}
    if limit is not None:
        params["limit"] = limit
    r = await client.get(
        f"{BASE_URL}/api/internal/user-events",
        params=params,
        headers={"Authorization": f"Bearer {INTERNAL_TOKEN}"},
    )
    r.raise_for_status()
    return r.json()


def _events_for(data: dict, user_key: str) -> list[dict]:
    return [e for e in data["events"] if e["user_key"] == user_key]


def _find_journey_key(journeys: dict, email_marker_events: list[dict]) -> str:
    # user_key берём из событий этого теста (уникальный набор событий)
    return email_marker_events[0]["user_key"]


async def test_1_requires_token():
    async with make_client() as client:
        r = await client.get(f"{BASE_URL}/api/internal/user-events")
        assert r.status_code == 401, f"без токена должен быть 401, получили {r.status_code}"
        r = await client.get(
            f"{BASE_URL}/api/internal/user-events",
            headers={"Authorization": "Bearer wrong-token"},
        )
        assert r.status_code == 401, f"с неверным токеном должен быть 401, получили {r.status_code}"
    print("OK test_1_requires_token")


async def test_2_registration_event_present_no_pii():
    async with make_client() as client:
        email = _email("t2")
        await _register(client, email)
        data = await _events(client)
        assert data["ok"] is True
        reg_events = [e for e in data["events"] if e["event_type"] == "user_registered"]
        assert reg_events, "должно быть хотя бы одно событие user_registered"
        blob = str(data)
        assert email not in blob, "email не должен утекать в ответ"
        assert "password" not in blob.lower()
        for e in data["events"]:
            assert e["user_key"].startswith("u_"), f"user_key должен быть анонимным: {e['user_key']}"
            assert e["event_id"], "event_id обязателен"
            assert e["created_at"], "created_at обязателен"
            assert "journey_snapshot" in e
    print("OK test_2_registration_event_present_no_pii")


async def test_3_product_events_mapped():
    async with make_client() as client:
        token = (await _register(client, _email("t3")))["token"]
        await _product_event(client, token, "onboarding_choice_selected", "generate_first_post")
        await _product_event(client, token, "first_post_feedback", "bad")
        await _product_event(client, token, "first_post_feedback_reason", "too_generic")
        await _product_event(client, token, "pricing_viewed")
        await _product_event(client, token, "payment_cta_clicked")

        data = await _events(client)
        # Ищем юзера по наличию onboarding_choice + feedback_bad вместе
        by_user: dict[str, set] = {}
        for e in data["events"]:
            by_user.setdefault(e["user_key"], set()).add(e["event_type"])
        target = None
        for uk, types in by_user.items():
            if {"onboarding_choice", "first_post_feedback_bad", "pricing_viewed", "payment_cta_clicked"} <= types:
                target = uk
                break
        assert target, f"не найден юзер со всеми 4 событиями: {by_user}"

        types_all = {e["event_type"] for e in data["events"]}
        assert "first_post_feedback_reason" not in types_all, "reason -- не отдельное событие"

        # reason должен быть внутри snapshot
        snap = next(
            e["journey_snapshot"] for e in data["events"]
            if e["user_key"] == target and e["event_type"] == "first_post_feedback_bad"
        )
        assert snap["first_post_feedback"] == "bad"
        assert snap["first_post_feedback_reason"] == "too_generic"
        assert snap["pricing_viewed"] is True
        assert snap["stuck_at"] == "tariff_screen", f"stuck_at={snap['stuck_at']}"
    print("OK test_3_product_events_mapped")


async def test_4_noise_events_filtered():
    async with make_client() as client:
        token = (await _register(client, _email("t4")))["token"]
        await _product_event(client, token, "quota_warning_seen")
        await _product_event(client, token, "limit_reached")
        await _product_event(client, token, "payment_returned")
        data = await _events(client)
        types_all = {e["event_type"] for e in data["events"]}
        for noise in ("quota_warning_seen", "limit_reached", "payment_returned"):
            assert noise not in types_all, f"{noise} не должен попадать в live feed"
    print("OK test_4_noise_events_filtered")


async def test_5_event_ids_stable_and_unique():
    async with make_client() as client:
        token = (await _register(client, _email("t5")))["token"]
        await _product_event(client, token, "pricing_viewed")
        d1 = await _events(client)
        d2 = await _events(client)
        ids1 = [e["event_id"] for e in d1["events"]]
        ids2 = [e["event_id"] for e in d2["events"]]
        assert len(ids1) == len(set(ids1)), "event_id должны быть уникальны"
        assert set(ids1) == set(ids2), "event_id должны быть стабильны между запросами"
    print("OK test_5_event_ids_stable_and_unique")


async def test_6_limit_and_period_validation():
    async with make_client() as client:
        data = await _events(client, limit=1)
        assert len(data["events"]) <= 1, "limit=1 должен ограничивать выдачу"
        r = await client.get(
            f"{BASE_URL}/api/internal/user-events",
            params={"period_minutes": 999999},
            headers={"Authorization": f"Bearer {INTERNAL_TOKEN}"},
        )
        assert r.status_code == 422, f"period_minutes > 24h должен отклоняться, получили {r.status_code}"
    print("OK test_6_limit_and_period_validation")


async def test_7_no_post_import():
    """Модуль не должен импортировать Post -- физически не может отдать
    тексты постов или post_generations как событие."""
    with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "internal_user_events.py")) as f:
        src = f.read()
    assert "Post," not in src.replace("ProductEvent", "") and " Post\n" not in src, \
        "internal_user_events.py не должен импортировать Post"
    import re
    imports = re.findall(r"from database import (.+)", src)
    for imp in imports:
        names = [n.strip() for n in imp.split(",")]
        assert "Post" not in names, f"Post найден в импортах: {imp}"
    print("OK test_7_no_post_import")


async def test_8_user_key_consistent_with_journeys():
    """user_key одного юзера должен совпадать между user-events и user-journeys."""
    async with make_client() as client:
        token = (await _register(client, _email("t8")))["token"]
        await _product_event(client, token, "onboarding_choice_selected", "skip")

        ev = await _events(client)
        by_user: dict[str, set] = {}
        for e in ev["events"]:
            by_user.setdefault(e["user_key"], set()).add(
                (e["event_type"], e["journey_snapshot"].get("onboarding_choice"))
            )
        target = None
        for uk, pairs in by_user.items():
            if ("onboarding_choice", "skip") in pairs:
                target = uk
        assert target, "юзер t8 не найден в events"

        r = await client.get(
            f"{BASE_URL}/api/internal/user-journeys",
            params={"period_hours": 1, "limit": 500},
            headers={"Authorization": f"Bearer {INTERNAL_TOKEN}"},
        )
        r.raise_for_status()
        jkeys = {j["user_key"] for j in r.json()["journeys"]}
        assert target in jkeys, f"user_key {target} из events не найден в journeys"
    print("OK test_8_user_key_consistent_with_journeys")


async def test_9_journeys_regression():
    """Регрессия: user-journeys продолжает работать после добавления модуля."""
    async with make_client() as client:
        r = await client.get(
            f"{BASE_URL}/api/internal/user-journeys",
            params={"period_hours": 24},
            headers={"Authorization": f"Bearer {INTERNAL_TOKEN}"},
        )
        r.raise_for_status()
        data = r.json()
        assert data["ok"] is True
        assert "journeys" in data
    print("OK test_9_journeys_regression")


async def main():
    await test_1_requires_token()
    await test_2_registration_event_present_no_pii()
    await test_3_product_events_mapped()
    await test_4_noise_events_filtered()
    await test_5_event_ids_stable_and_unique()
    await test_6_limit_and_period_validation()
    await test_7_no_post_import()
    await test_8_user_key_consistent_with_journeys()
    await test_9_journeys_regression()
    print("\nВСЕ 9 ТЕСТОВ user-events ПРОШЛИ")


if __name__ == "__main__":
    asyncio.run(main())
