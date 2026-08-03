"""
Индикатор "генерируется следующий пост" (C14, пункт 6 из видения владельца
01.08): Channel.generating_since выставляется до тяжёлой работы генерации
(классификация темы, поиск, запрос к модели) и снимается после -- независимо
от результата (tasks.generate_for_channel -- тонкая обёртка с try/finally
вокруг _generate_for_channel_impl). _channel_dict отдаёт булев "generating",
а не сырую метку времени, с защитой от "зависшего" флага (сервер мог упасть
посреди генерации -- finally не выполнится при kill -9).
"""

from datetime import datetime, timedelta

import pytest

import database
import generator
import tasks
from database import Channel, User


def _stub_generator(monkeypatch, text="Тестовый пост", fail=False):
    async def _classify(_topic):
        return "valid"

    async def _generate(channel, material, topic, rules_text, recent_titles, **kw):
        if fail:
            raise generator.GenerationError("боевая ошибка модели")
        return text, 100

    monkeypatch.setattr(generator, "classify_topic", _classify)
    monkeypatch.setattr(generator, "generate_post", _generate)


def _make_channel(email: str, **kwargs) -> tuple[int, int]:
    with database.session() as s:
        u = User(email=email, password_hash="x", token_balance=100_000)
        s.add(u); s.commit(); s.refresh(u)
        defaults = dict(user_id=u.id, title="Канал", about="тема",
                        tg_chat=f"@{email.split('@')[0]}", verified=True, enabled=True)
        defaults.update(kwargs)
        ch = Channel(**defaults)
        s.add(ch); s.commit(); s.refresh(ch)
        return u.id, ch.id


async def test_flag_is_set_during_generation_and_cleared_after(monkeypatch):
    """Мы не можем поймать флаг РОВНО в момент генерации в синхронном тесте --
    но можем проверить, что _set_generating действительно пишет и снимает
    его, вызывая ту же пару операций, что видит generate_for_channel."""
    uid, cid = _make_channel("gen_flag@t.local")

    await tasks._set_generating(cid, True)
    with database.session() as s:
        ch = s.get(Channel, cid)
        assert ch.generating_since is not None

    await tasks._set_generating(cid, False)
    with database.session() as s:
        ch = s.get(Channel, cid)
        assert ch.generating_since is None


async def test_flag_cleared_after_successful_generation(monkeypatch):
    _stub_generator(monkeypatch)
    uid, cid = _make_channel("gen_flag_ok@t.local")

    result = await tasks.generate_for_channel(cid)
    assert result["ok"], result

    with database.session() as s:
        ch = s.get(Channel, cid)
        assert ch.generating_since is None, "флаг должен сняться после успешной генерации"


async def test_flag_cleared_even_when_generation_fails(monkeypatch):
    """
    generator.GenerationError ловится внутри _generate_for_channel_impl и
    превращается в обычный return {"ok": False} -- он НЕ проверяет
    try/finally обёртки (та же ветка кода что и success). Настоящая
    проверка нужна для исключения, которое реально пробивает наверх --
    classify_topic вызывается без try/except вокруг себя, необработанная
    ошибка там долетит до вызывающей стороны.
    """
    async def _classify_boom(_topic):
        raise RuntimeError("непредвиденная ошибка классификации")
    monkeypatch.setattr(generator, "classify_topic", _classify_boom)

    uid, cid = _make_channel("gen_flag_fail@t.local")

    with pytest.raises(RuntimeError):
        await tasks.generate_for_channel(cid)

    with database.session() as s:
        ch = s.get(Channel, cid)
        assert ch.generating_since is None, "flag застрял после необработанного исключения -- finally не сработал"


async def test_flag_cleared_when_balance_is_zero():
    """Ранний выход (баланс исчерпан, до всякой тяжёлой работы) -- флаг тоже не должен зависать."""
    uid, cid = _make_channel("gen_flag_broke@t.local")
    with database.session() as s:
        u = s.get(User, uid)
        u.token_balance = 0
        s.add(u); s.commit()

    result = await tasks.generate_for_channel(cid)
    assert not result["ok"]

    with database.session() as s:
        ch = s.get(Channel, cid)
        assert ch.generating_since is None


# ── _channel_dict: булев generating с защитой от протухания ────────────────

async def test_channel_dict_reports_generating_true_when_recent():
    uid, cid = _make_channel("gen_dict_fresh@t.local")
    with database.session() as s:
        ch = s.get(Channel, cid)
        ch.generating_since = datetime.utcnow()
        s.add(ch); s.commit()

    import main
    with database.session() as s:
        ch = s.get(Channel, cid)
        d = main._channel_dict(s, ch)
    assert d["generating"] is True


async def test_channel_dict_reports_generating_false_when_stale():
    """Флаг старше 3 минут считаем зависшим (процесс мог упасть посреди генерации)."""
    uid, cid = _make_channel("gen_dict_stale@t.local")
    with database.session() as s:
        ch = s.get(Channel, cid)
        ch.generating_since = datetime.utcnow() - timedelta(minutes=10)
        s.add(ch); s.commit()

    import main
    with database.session() as s:
        ch = s.get(Channel, cid)
        d = main._channel_dict(s, ch)
    assert d["generating"] is False


async def test_channel_dict_reports_generating_false_by_default():
    uid, cid = _make_channel("gen_dict_none@t.local")
    import main
    with database.session() as s:
        ch = s.get(Channel, cid)
        d = main._channel_dict(s, ch)
    assert d["generating"] is False
