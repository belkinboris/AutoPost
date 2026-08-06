"""
Тесты на различие «тема не подошла» и «мы не смогли проверить тему».

Зачем. Раньше оба случая давали один статус classification_failed и одно
сообщение: «Не удалось проверить тему. Попробуйте переформулировать». Найдено
проходом первого входа с пустыми ключами модели: пользователь вводит тему,
получает предложение её переформулировать, переписывает, получает то же
самое -- и так сколько угодно, потому что дело не в теме, а в том, что до
модели не дозвонились. В логе у нас при этом честно стояло «Ошибка
авторизации ИИ».

Цена ошибки видна из бэклога: «зарегистрировались и не получили ни одного
поста» -- самая дорогая точка оттока, за неё уже заплачено рекламой.

Второе, что здесь закреплено: при нашем сбое нельзя удалять только что
созданный канал. Ветка удаления черновика рассчитана на отклонённую тему, а
недоступность модели -- не отказ, тема вообще не проверялась.
"""

import pytest

import generator
import tasks


# ── 1. Сбой обращения к модели -- отдельный статус ────────────────────────

async def test_call_failure_gives_unavailable_not_failed(monkeypatch):
    async def _boom(*a, **kw):
        raise RuntimeError("Ошибка авторизации ИИ")

    monkeypatch.setattr(generator, "_call_llm", _boom)
    assert await generator.classify_topic("криптоновости") == "classification_unavailable"


async def test_garbled_answer_still_gives_failed(monkeypatch):
    """
    Модель ответила, но не одним из ожидаемых слов. Здесь просьба
    переформулировать осмысленна -- статус должен остаться прежним.
    """
    async def _garbage(*a, **kw):
        return "мне кажется это про котиков", 10

    monkeypatch.setattr(generator, "_call_llm", _garbage)
    assert await generator.classify_topic("криптоновости") == "classification_failed"


# ── 2. Сообщение честное: наша вина, тему переписывать не надо ────────────

def test_message_blames_us_not_the_user():
    msg = generator.rejection_message("classification_unavailable")
    assert msg, "статус обязан быть блокирующим и иметь сообщение"
    assert "нашей стороне" in msg, f"сообщение не называет виноватого: {msg}"
    assert "переформулир" not in msg.lower(), (
        f"мы снова просим переписать тему при нашем же сбое: {msg}"
    )


def test_unavailable_still_blocks_generation():
    """
    Смысл блокировки: непроверенную тему нельзя пропускать в генерацию, даже
    когда проверка сломалась по нашей вине. Безопасность важнее удобства.
    """
    assert generator.rejection_message("classification_unavailable") is not None
    assert generator.rejection_message("valid_topic") is None


# ── 3. Наш сбой не стирает канал пользователя ─────────────────────────────

async def test_draft_channel_survives_model_outage(monkeypatch):
    import database
    from database import Channel, User
    from sqlmodel import select

    with database.session() as s:
        u = User(email="outage@t.local", password_hash="x", token_balance=100_000)
        s.add(u); s.commit(); s.refresh(u)
        ch = Channel(user_id=u.id, title="Черновик", about="криптоновости",
                     use_web_search=False, enabled=True)
        s.add(ch); s.commit(); s.refresh(ch)
        cid = ch.id

    async def _unavailable(_topic):
        return "classification_unavailable"

    monkeypatch.setattr(generator, "classify_topic", _unavailable)
    result = await tasks.generate_for_channel(cid, topic="криптоновости")

    assert result["ok"] is False
    assert "нашей стороне" in result["message"], f"пользователю показали не то: {result}"
    with database.session() as s:
        assert s.get(Channel, cid) is not None, (
            "канал удалён из-за нашего сбоя -- человек потерял работу за то, "
            "что у нас лежал провайдер"
        )


async def test_rejected_topic_still_removes_draft_channel(monkeypatch):
    """
    Обратная сторона: настоящую отклонённую тему по-прежнему подчищаем, иначе
    неподходящий канал останется висеть в кабинете.
    """
    import database
    from database import Channel, User

    with database.session() as s:
        u = User(email="rejected@t.local", password_hash="x", token_balance=100_000)
        s.add(u); s.commit(); s.refresh(u)
        ch = Channel(user_id=u.id, title="Черновик", about="что-то",
                     use_web_search=False, enabled=True)
        s.add(ch); s.commit(); s.refresh(ch)
        cid = ch.id

    async def _rejected(_topic):
        return "adult_or_sexual_topic"

    monkeypatch.setattr(generator, "classify_topic", _rejected)
    result = await tasks.generate_for_channel(cid, topic="что-то")

    assert result["ok"] is False
    with database.session() as s:
        assert s.get(Channel, cid) is None, "черновик с отклонённой темой должен удаляться"


# ── Аудит 05.08: молчаливый сбой классификации ───────────────────────────

async def test_classification_failed_does_not_delete_channel(monkeypatch):
    """classification_failed -- НАШ сбой, а не тема пользователя: модель
    ответила невнятно, а она недетерминирована, на следующей попытке та же
    тема пройдёт. До правки такой ответ стирал только что созданный канал --
    случайная невнятность модели уносила онбординг."""
    import database
    import generator
    import tasks
    from database import Channel, User

    with database.session() as s:
        u = User(email="clsfail@t.local", password_hash="x", token_balance=100_000)
        s.add(u); s.commit(); s.refresh(u)
        ch = Channel(user_id=u.id, title="Черновик", about="криптоновости",
                     use_web_search=False, enabled=True)
        s.add(ch); s.commit(); s.refresh(ch)
        cid = ch.id

    async def _failed(_topic):
        return "classification_failed"

    monkeypatch.setattr(generator, "classify_topic", _failed)
    result = await tasks.generate_for_channel(cid, topic="криптоновости")

    assert result["ok"] is False
    assert result.get("channel_deleted") is False, "channel_deleted должен быть по факту"
    with database.session() as s:
        assert s.get(Channel, cid) is not None, "канал стёрт из-за нашего сбоя классификации"


async def test_classification_failures_set_generation_failed(monkeypatch):
    """Оба наших сбоя классификации обязаны нести generation_failed=True --
    иначе счётчик неудач (gen_fail_streak) их не видит, тик пробует каждую
    минуту вечно, жжёт токены, а на экране пусто (правило из CLAUDE.md)."""
    import database
    import generator
    import tasks
    from database import Channel, User

    for tag, verdict in [("unavail2", "classification_unavailable"),
                         ("failed2", "classification_failed")]:
        with database.session() as s:
            u = User(email=f"{tag}@t.local", password_hash="x", token_balance=100_000)
            s.add(u); s.commit(); s.refresh(u)
            ch = Channel(user_id=u.id, title="К", about="криптоновости",
                         use_web_search=False, enabled=True)
            # даём каналу пост, чтобы удаление черновика не мешало проверке флага
            s.add(ch); s.commit(); s.refresh(ch)
            from database import Post
            s.add(Post(channel_id=ch.id, user_id=u.id, text="уже есть", status="scheduled"))
            s.commit()
            cid = ch.id

        async def _v(_t, _verdict=verdict):
            return _verdict

        monkeypatch.setattr(generator, "classify_topic", _v)
        result = await tasks.generate_for_channel(cid, topic="криптоновости")
        assert result.get("generation_failed") is True, (
            f"{verdict}: без generation_failed счётчик неудач слеп -> вечный цикл"
        )


async def test_rejected_topic_reports_generation_failed_and_real_deletion(monkeypatch):
    """Честно плохая тема: канал-черновик удаляется (channel_deleted=True) и
    generation_failed=True (чтобы тик замолчал после 3 попыток, а не долбил
    отклонённую тему вечно). А канал С ПОСТАМИ не удаляется -- и это видно
    в channel_deleted."""
    import database
    import generator
    import tasks
    from database import Channel, Post, User

    # 1) черновик без постов -> удаляется
    with database.session() as s:
        u = User(email="rej_draft@t.local", password_hash="x", token_balance=100_000)
        s.add(u); s.commit(); s.refresh(u)
        ch = Channel(user_id=u.id, title="Ч", about="x", use_web_search=False, enabled=True)
        s.add(ch); s.commit(); s.refresh(ch)
        draft_cid = ch.id

    async def _rej(_t):
        return "adult_or_sexual_topic"

    monkeypatch.setattr(generator, "classify_topic", _rej)
    r1 = await tasks.generate_for_channel(draft_cid, topic="x")
    assert r1.get("channel_deleted") is True
    assert r1.get("generation_failed") is True

    # 2) канал с постом -> не удаляется, channel_deleted=False
    with database.session() as s:
        u = User(email="rej_existing@t.local", password_hash="x", token_balance=100_000)
        s.add(u); s.commit(); s.refresh(u)
        ch = Channel(user_id=u.id, title="Ч", about="x", use_web_search=False, enabled=True)
        s.add(ch); s.commit(); s.refresh(ch)
        s.add(Post(channel_id=ch.id, user_id=u.id, text="был пост", status="published"))
        s.commit()
        existing_cid = ch.id

    r2 = await tasks.generate_for_channel(existing_cid, topic="x")
    assert r2.get("channel_deleted") is False, "канал с постами удалять нельзя, и врать в поле тоже"
    with database.session() as s:
        assert s.get(Channel, existing_cid) is not None
