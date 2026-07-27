"""
Интеграционный тест полного quick start flow (Test cases A-F из ревью задачи).

В отличие от test_topic_validation.py (юнит-тесты generator.classify_topic/
generate_post напрямую), этот файл тестирует ПОЛНЫЙ HTTP-путь через реальные
эндпоинты:

    POST /api/validate-topic -> POST /api/channels -> POST /api/channels/{id}/generate

Это важно, потому что P0-баг был именно в том, что Channel создавался ДО
проверки темы — юнит-тест одной функции этого не поймает, нужен тест всей
цепочки вызовов как её делает фронт (qsGenerate в app.js).

Требует:
- запущенный сервер (локально или на Railway), URL в BASE_URL;
- переменная окружения ANTHROPIC_API_KEY на сервере (не здесь -- сервер сам
  обращается к Anthropic API);
- тестовый пользователь будет зарегистрирован автоматически на каждый запуск
  с уникальным email, чтобы не конфликтовать с реальными данными.

Запуск:
    BASE_URL=http://localhost:8000 python3 test_quickstart_flow.py
    BASE_URL=https://autopost26.up.railway.app python3 test_quickstart_flow.py

Делает реальные запросы (включая реальные вызовы Anthropic API на сервере) --
расходует токены тестового пользователя, не гонять в проде на каждый коммит.
"""

import asyncio
import os
import random
import string
import sys

import httpx
import pytest

BASE_URL = os.environ.get("BASE_URL", "http://localhost:8000")

# Единственный файл тестов, который тратит реальные деньги: сервер ходит в
# модель на каждый прогон (валидация темы + генерация поста). Гонять это на
# каждый коммит нельзя, поэтому по умолчанию пропускаем -- но пропускаем
# ЯВНО, с причиной в выводе pytest, а не делаем вид, что проверено.
pytestmark = pytest.mark.skipif(
    not os.environ.get("RUN_LLM_TESTS"),
    reason="тратит токены модели; запуск: RUN_LLM_TESTS=1 python3 -m pytest test_quickstart_flow.py",
)


def _random_email() -> str:
    suffix = "".join(random.choices(string.ascii_lowercase + string.digits, k=10))
    return f"test_quickstart_{suffix}@example.com"


async def _register(client: httpx.AsyncClient) -> str:
    """Регистрирует одноразового тестового пользователя, возвращает token."""
    email = _random_email()
    r = await client.post(f"{BASE_URL}/api/register", json={"email": email, "password": "test12345"})
    r.raise_for_status()
    return r.json()["token"]


async def _list_channels(client: httpx.AsyncClient, token: str) -> list:
    r = await client.get(f"{BASE_URL}/api/channels", headers={"Authorization": f"Bearer {token}"})
    r.raise_for_status()
    return r.json()


async def run_quickstart(client: httpx.AsyncClient, token: str, topic: str) -> dict:
    """
    Повторяет ровно ту последовательность вызовов, что делает qsGenerate()
    в app.js: validate-topic -> (если ok) create channel -> generate.
    Возвращает сводку результата для проверки в тестах.
    """
    headers = {"Authorization": f"Bearer {token}"}

    validation = await client.post(f"{BASE_URL}/api/validate-topic", json={"topic": topic}, headers=headers)
    validation.raise_for_status()
    validation_data = validation.json()

    if not validation_data["ok"]:
        return {
            "stage": "validate-topic",
            "channel_created": False,
            "post_created": False,
            "rejection_message": validation_data.get("message"),
            "classification": validation_data.get("classification"),
        }

    chan_resp = await client.post(
        f"{BASE_URL}/api/channels",
        json={"title": topic[:40], "about": topic},
        headers=headers,
    )
    chan_resp.raise_for_status()
    channel_id = chan_resp.json()["id"]

    gen_resp = await client.post(f"{BASE_URL}/api/channels/{channel_id}/generate", json={}, headers=headers)

    if gen_resp.status_code != 200:
        # Генерация отклонена (defense-in-depth: тема прошла validate-topic,
        # но generate_for_channel независимо тоже её классифицировал и
        # отклонил -- редкий, но валидный случай для погранично-неопределённых тем).
        gen_data = gen_resp.json()
        return {
            "stage": "generate",
            "channel_id": channel_id,
            "channel_created": True,
            "post_created": False,
            "rejection_message": gen_data.get("detail"),
        }

    post_data = gen_resp.json()
    return {
        "stage": "generate",
        "channel_id": channel_id,
        "channel_created": True,
        "post_created": True,
        "post_text": post_data.get("text", ""),
    }


async def test_case_A_valid_business_topic(client, token):
    """A: 'M&A сделки в России' -> канал создан, пост про M&A, first_post_generated."""
    result = await run_quickstart(client, token, "M&A сделки в России")
    ok = result["channel_created"] and result["post_created"]
    print(f"  [A] M&A сделки в России -> created={result['channel_created']} post={result['post_created']} {'✓' if ok else '✗'}")
    if result.get("post_text"):
        print(f"      текст (начало): {result['post_text'][:100]!r}")
    return [] if ok else ["A"]


async def test_case_B_valid_gaming_topic(client, token):
    """B: 'Roblox' -> канал создан, пост про Roblox."""
    result = await run_quickstart(client, token, "Roblox")
    ok = result["channel_created"] and result["post_created"]
    print(f"  [B] Roblox -> created={result['channel_created']} post={result['post_created']} {'✓' if ok else '✗'}")
    if result.get("post_text"):
        print(f"      текст (начало): {result['post_text'][:100]!r}")
    return [] if ok else ["B"]


async def test_case_C_adult_topic_blocks_channel_creation(client, token):
    """
    C (ключевой регрессионный тест P0-бага): adult-тема -> канал НЕ создан,
    пост НЕ создан, русское сообщение об отказе.
    """
    topic = "соски твердые лучше чем мягкие"
    channels_before = await _list_channels(client, token)
    result = await run_quickstart(client, token, topic)
    channels_after = await _list_channels(client, token)

    no_channel_created = not result["channel_created"]
    no_new_channel_in_list = len(channels_after) == len(channels_before)
    has_russian_message = result.get("rejection_message") and any(ord(c) > 127 for c in result["rejection_message"])

    ok = no_channel_created and no_new_channel_in_list and has_russian_message
    print(f"  [C] «{topic}» -> channel_created={result['channel_created']} "
          f"channels_count_unchanged={no_new_channel_in_list} message={result.get('rejection_message')!r} "
          f"{'✓' if ok else '✗ РЕГРЕССИЯ P0-БАГА'}")
    return [] if ok else ["C"]


async def test_case_D_gibberish_topic(client, token):
    """D: 'ываыва' -> канал НЕ создан, просьба уточнить тему на русском."""
    topic = "ываыва"
    channels_before = await _list_channels(client, token)
    result = await run_quickstart(client, token, topic)
    channels_after = await _list_channels(client, token)

    no_channel_created = not result["channel_created"]
    no_new_channel_in_list = len(channels_after) == len(channels_before)
    has_russian_message = result.get("rejection_message") and any(ord(c) > 127 for c in result["rejection_message"])

    ok = no_channel_created and no_new_channel_in_list and has_russian_message
    print(f"  [D] «{topic}» -> channel_created={result['channel_created']} "
          f"channels_count_unchanged={no_new_channel_in_list} message={result.get('rejection_message')!r} "
          f"{'✓' if ok else '✗'}")
    return [] if ok else ["D"]


async def test_case_E_ambiguous_crude_topic(client, token):
    """
    E: 'какашки и пиписки' -> либо безопасный нейтральный пост, либо просьба
    уточнить; в любом случае НЕ крипта и не случайная старая тема.
    """
    topic = "какашки и пиписки"
    result = await run_quickstart(client, token, topic)

    if not result["channel_created"]:
        print(f"  [E] «{topic}» -> отклонено на этапе валидации: {result.get('rejection_message')!r} ✓ (допустимый исход)")
        return []

    post_text = (result.get("post_text") or "").lower()
    crypto_words = ["крипт", "биткоин", "bitcoin", "blockchain", "блокчейн", "эфир", "ethereum"]
    drifted = any(w in post_text for w in crypto_words)
    print(f"  [E] «{topic}» -> создан пост, drift_to_crypto={drifted} {'✗ РЕГРЕССИЯ' if drifted else '✓'}")
    if post_text:
        print(f"      текст (начало): {post_text[:100]!r}")
    return ["E"] if drifted else []


async def test_case_F_clean_state_after_rejection(client, token):
    """
    F: после отказа (тема C) пользователь вводит нормальную тему -> новый
    канал создаётся нормально, предыдущая неподходящая тема не мешает.
    """
    result = await run_quickstart(client, token, "новости криптовалют")
    ok = result["channel_created"] and result["post_created"]
    print(f"  [F] нормальная тема после отказа -> created={result['channel_created']} post={result['post_created']} {'✓' if ok else '✗'}")
    return [] if ok else ["F"]


async def main():
    print(f"BASE_URL = {BASE_URL}\n")
    async with httpx.AsyncClient(timeout=60) as client:
        token = await _register(client)
        print(f"Тестовый пользователь зарегистрирован.\n")

        failed = []
        print("=== Test A: валидная бизнес-тема ===")
        failed += await test_case_A_valid_business_topic(client, token)

        print("\n=== Test B: валидная игровая тема ===")
        failed += await test_case_B_valid_gaming_topic(client, token)

        print("\n=== Test C: adult-тема (P0-баг, ключевой тест) ===")
        failed += await test_case_C_adult_topic_blocks_channel_creation(client, token)

        print("\n=== Test D: бессмысленная тема ===")
        failed += await test_case_D_gibberish_topic(client, token)

        print("\n=== Test E: грубая неоднозначная тема ===")
        failed += await test_case_E_ambiguous_crude_topic(client, token)

        print("\n=== Test F: чистое состояние после отказа ===")
        failed += await test_case_F_clean_state_after_rejection(client, token)

        print(f"\n{'='*50}")
        if failed:
            print(f"ПРОВАЛЕНО: {len(failed)} тестов — {failed}")
            sys.exit(1)
        else:
            print("Все тесты пройдены ✓")


if __name__ == "__main__":
    asyncio.run(main())
