"""
Стиль из онбординга (06.08).

По данным воронки владельца (100 регистраций, 3 оплаты за 30 дней) главная
жалоба на первый пост -- «не тот стиль» (26 «плохо» против 11 «хорошо»,
wrong_style первым номером). Раньше стиль в онбординге не спрашивали вовсе:
канал создавался с пустым channel.style, и генерация падала в общий
ИИ-пресет, который сам код помечал как дающий «75% не тот стиль».

Теперь на экране первого поста человек описывает стиль своими словами. Здесь
проверяется сквозной путь: поле -> channel.style (сохранение через API) ->
блок «СТИЛЬ:» в промпте генерации -> работает и для следующих постов, не
только первого.
"""

import pytest
from sqlmodel import select

import database
import generator
from database import Channel, User


# ── 1. Стиль доходит до канала через тот же путь, что и онбординг ──────────

async def test_create_channel_saves_style(client, token):
    """Онбординг создаёт канал через POST /channels со style в теле
    (см. _qsGenerateImpl). Стиль обязан сохраниться -- иначе генерация его
    не увидит."""
    r = await client.post("/api/channels", json={
        "title": "Канал", "about": "криптоновости",
        "style": "коротко и по делу, без воды, с лёгкой иронией",
    }, headers={"Authorization": f"Bearer {token}"})
    r.raise_for_status()
    cid = r.json()["id"]
    with database.session() as s:
        assert s.get(Channel, cid).style == "коротко и по делу, без воды, с лёгкой иронией"


# ── 2. Сохранённый стиль реально попадает в промпт ────────────────────────

def _channel_with_style(style: str) -> Channel:
    return Channel(id=1, user_id=1, title="К", about="криптоновости", style=style,
                   use_web_search=False, channel_type="thematic")


async def test_style_reaches_the_generation_prompt(monkeypatch):
    """channel.style обязан оказаться в системном промпте генерации блоком
    «СТИЛЬ: ...». Ловим сам промпт, а не результат: важно, что модель ЭТУ
    инструкцию получает."""
    captured = {}

    async def _fake_llm(system, messages, max_tokens=700):
        captured["system"] = system
        return "<b>Заголовок</b>\n\nТекст поста.", 100

    monkeypatch.setattr(generator, "_call_yandex", _fake_llm)
    monkeypatch.setattr(generator, "FORCE_PROVIDER", "yandex")

    style = "тёплый личный тон, обращение на ты, короткие абзацы"
    await generator.generate_post(_channel_with_style(style), topic="криптоновости")

    assert "СТИЛЬ:" in captured["system"], "блок стиля пропал из промпта"
    assert style in captured["system"], (
        f"стиль пользователя не доехал до промпта:\n{captured['system'][:600]}"
    )


async def test_no_style_falls_back_to_tone_preset(monkeypatch):
    """Обратная сторона: без стиля поведение прежнее -- нет блока «СТИЛЬ:»,
    но есть пресет тона (не голый нейтральный ИИ). Проверяем, что пустое
    поле не ломает генерацию и не тащит пустой блок стиля."""
    captured = {}

    async def _fake_llm(system, messages, max_tokens=700):
        captured["system"] = system
        return "<b>Заголовок</b>\n\nТекст.", 100

    monkeypatch.setattr(generator, "_call_yandex", _fake_llm)
    monkeypatch.setattr(generator, "FORCE_PROVIDER", "yandex")

    await generator.generate_post(_channel_with_style(""), topic="криптоновости")

    assert "СТИЛЬ:" not in captured["system"], "пустой стиль не должен давать блок «СТИЛЬ:»"
    assert "ТОН (пресет" in captured["system"], "без стиля должен включаться пресет тона"


# ── 3. Стиль работает и для следующих постов, не только первого ───────────

async def test_saved_style_used_on_later_generation(client, token, monkeypatch):
    """Стиль сохранён на канал -> любая последующая генерация (перегенерация,
    автопилот) использует его автоматически, без повторного ввода."""
    r = await client.post("/api/channels", json={
        "title": "Канал", "about": "криптоновости",
        "style": "строго и экспертно, только факты",
    }, headers={"Authorization": f"Bearer {token}"})
    r.raise_for_status()
    cid = r.json()["id"]

    captured = {}

    async def _fake_llm(system, messages, max_tokens=700):
        captured["system"] = system
        return "<b>З</b>\n\nТекст.", 100

    monkeypatch.setattr(generator, "_call_yandex", _fake_llm)
    monkeypatch.setattr(generator, "FORCE_PROVIDER", "yandex")

    with database.session() as s:
        ch = s.get(Channel, cid)
        await generator.generate_post(ch, topic="криптоновости")

    assert "строго и экспертно, только факты" in captured["system"], (
        "сохранённый на канал стиль не применился к следующей генерации"
    )
