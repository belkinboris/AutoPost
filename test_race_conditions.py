"""
Гонки: два исполнителя делают одно и то же одновременно.

Класс ошибок, который весь существующий набор тестов пропускал по построению
(аудит 02.08): подделки генерации/отправки срабатывали мгновенно и вызывались
строго по очереди, поэтому окно между «прочитали состояние» и «записали
результат» в тестах просто не существовало. В проде это окно -- десятки
секунд (запрос к модели, к Telegram, к ЮKassa), и в него успевает второй
исполнитель.

Здесь подделки СПЕЦИАЛЬНО медленные (отдают управление циклу событий), а
вызовы идут через asyncio.gather -- то есть воспроизводят прод, а не
последовательный идеал.
"""

import asyncio
from datetime import datetime, timedelta

import database
import tasks
from database import (
    Channel, Payment, Post, PostApproval, User,
    claim_channel_for_generation, claim_payment_for_credit, claim_post_for_publish,
)


def _make_user(email: str, balance: int = 100_000) -> int:
    with database.session() as s:
        u = User(email=email, password_hash="x", token_balance=balance)
        s.add(u); s.commit(); s.refresh(u)
        return u.id


def _make_channel(uid: int, **kwargs) -> int:
    with database.session() as s:
        defaults = dict(user_id=uid, title="Канал", about="тема", tg_chat="@race",
                        verified=True, enabled=True, auto_publish=True)
        defaults.update(kwargs)
        ch = Channel(**defaults)
        s.add(ch); s.commit(); s.refresh(ch)
        return ch.id


# ── Двойная публикация ────────────────────────────────────────────────────

async def test_two_simultaneous_publishes_send_to_telegram_once(monkeypatch):
    """
    Кнопка «Опубликовать» на сайте и та же кнопка в карточке Telegram,
    нажатые почти одновременно, отправляли пост подписчикам ДВАЖДЫ:
    обе проверки `status == "published"` проходили до того, как первая
    успевала записать результат.
    """
    sends = []

    async def _slow_send(chat, text, **kwargs):
        sends.append(chat)
        await asyncio.sleep(0.05)   # окно, в которое влезал второй исполнитель
        return {"ok": True, "result": {"message_id": len(sends)}}

    monkeypatch.setattr(tasks.telegram_api, "send_message", _slow_send)

    uid = _make_user("race_pub@t.local")
    cid = _make_channel(uid)
    with database.session() as s:
        p = Post(channel_id=cid, user_id=uid, text="пост", status="scheduled",
                  scheduled_at=datetime.utcnow() - timedelta(minutes=1))
        s.add(p); s.commit(); s.refresh(p)
        pid = p.id

    results = await asyncio.gather(tasks.publish_post(pid), tasks.publish_post(pid))

    assert len(sends) == 1, f"пост ушёл в Telegram {len(sends)} раз(а) вместо одного"
    assert all(r.get("ok") for r in results), results
    with database.session() as s:
        post = s.get(Post, pid)
    assert post.status == "published"
    assert post.publishing_since is None, "захват должен быть снят после успеха"


async def test_failed_publish_releases_claim_so_retry_works(monkeypatch):
    """Неудачная отправка не должна запереть пост навсегда."""
    attempts = []

    async def _failing_send(chat, text, **kwargs):
        attempts.append(1)
        return {"ok": False, "description": "chat not found"}

    monkeypatch.setattr(tasks.telegram_api, "send_message", _failing_send)

    uid = _make_user("race_pub_fail@t.local")
    cid = _make_channel(uid)
    with database.session() as s:
        p = Post(channel_id=cid, user_id=uid, text="пост", status="scheduled",
                  scheduled_at=datetime.utcnow())
        s.add(p); s.commit(); s.refresh(p)
        pid = p.id

    r1 = await tasks.publish_post(pid)
    assert not r1["ok"]
    with database.session() as s:
        assert s.get(Post, pid).publishing_since is None, "захват не снят -- повтор станет невозможен"

    r2 = await tasks.publish_post(pid)
    assert len(attempts) == 2, "вторая попытка не дошла до Telegram -- захват заперт"


# ── Двойное начисление токенов (правило 7) ────────────────────────────────

async def test_payment_credited_only_once_under_race():
    """
    Вебхук ЮKassa ретраится, а /api/payments параллельно дёргает
    синхронизацию -- оба пути проходили `if pay.status != "paid"` и
    начисляли токены дважды.
    """
    uid = _make_user("race_pay@t.local", balance=0)
    with database.session() as s:
        pay = Payment(user_id=uid, package_id="p1", label="race", rub=490,
                      tokens=600_000, status="pending")
        s.add(pay); s.commit(); s.refresh(pay)
        pid = pay.id

    # Два «одновременных» захвата в разных сессиях, как два запроса
    def _try_credit():
        with database.session() as s:
            if claim_payment_for_credit(s, pid):
                u = s.get(User, uid)
                u.token_balance += 600_000
                s.add(u); s.commit()
                return True
            return False

    got = [_try_credit(), _try_credit()]

    assert got.count(True) == 1, "платёж захвачен дважды -- токены начислены бы дважды"
    with database.session() as s:
        assert s.get(User, uid).token_balance == 600_000


# ── Захват канала на генерацию ────────────────────────────────────────────

async def test_channel_generation_claim_is_exclusive():
    uid = _make_user("race_gen@t.local")
    cid = _make_channel(uid)
    with database.session() as s:
        first = claim_channel_for_generation(s, cid)
    with database.session() as s:
        second = claim_channel_for_generation(s, cid)
    assert first is True
    assert second is False, "две генерации по одному каналу одновременно"

    with database.session() as s:
        database.release_channel_generation_claim(s, cid)
    with database.session() as s:
        assert claim_channel_for_generation(s, cid) is True, "после снятия захват должен браться снова"


async def test_channel_generation_claim_expires_when_stale():
    """Процесс мог умереть посреди генерации -- канал не должен запереться навсегда."""
    uid = _make_user("race_gen_stale@t.local")
    cid = _make_channel(uid)
    with database.session() as s:
        ch = s.get(Channel, cid)
        ch.generating_since = datetime.utcnow() - timedelta(minutes=30)
        s.add(ch); s.commit()

    with database.session() as s:
        assert claim_channel_for_generation(s, cid, stale_after_minutes=3) is True


async def test_refill_queue_does_not_exceed_depth_under_race(monkeypatch):
    """
    Ключевой сценарий владельца: глубина 4, а в очереди оказалось 6.
    Два пополнения, запущенных одновременно (тик + клик/вебхук), читали
    одинаковое «в очереди 3» и оба генерировали.
    """
    created = []

    async def _slow_impl(channel_id, topic="", force_pending=False, target_scheduled_at=None):
        # Медленно, как настоящая генерация, и РЕАЛЬНО создаёт пост --
        # иначе окно между чтением и записью в тесте не воспроизводится.
        await asyncio.sleep(0.05)
        with database.session() as s:
            ch = s.get(Channel, channel_id)
            p = Post(channel_id=channel_id, user_id=ch.user_id, text=f"пост {len(created)}",
                      status="scheduled", scheduled_at=datetime.utcnow() + timedelta(hours=len(created) + 1))
            s.add(p); s.commit()
        created.append(channel_id)
        return {"ok": True, "post_id": None}

    # ВАЖНО: подменяем _generate_for_channel_impl, а НЕ generate_for_channel --
    # иначе подмена снесла бы саму обёртку с захватом канала и проверкой
    # глубины, то есть тест проверял бы отсутствующую защиту (на этом он уже
    # один раз попался).
    monkeypatch.setattr(tasks, "_generate_for_channel_impl", _slow_impl)

    uid = _make_user("race_refill@t.local")
    # Обязательно оплаченный: у бесплатного потолок MIN_QUEUE=3, queue_depth=4
    # зажался бы до 3, pending_count=3 >= target -- пополнение вышло бы сразу
    # и тест прошёл бы ВХОЛОСТУЮ, ничего не проверив.
    with database.session() as s:
        s.add(Payment(user_id=uid, package_id="p1", label="race", rub=490,
                      tokens=600_000, status="paid"))
        s.commit()
    cid = _make_channel(uid, queue_depth=4)
    with database.session() as s:
        for i in range(3):
            s.add(Post(channel_id=cid, user_id=uid, text=f"старый {i}", status="scheduled",
                        scheduled_at=datetime.utcnow() + timedelta(hours=i + 1)))
        s.commit()

    # Санитарная проверка самого теста: цель обязана быть 4, иначе он
    # ничего не проверяет (ровно на этом он и попался при написании).
    with database.session() as s:
        ch = s.get(Channel, cid)
        assert tasks.queue_target_for_user(s, uid, ch) == 4

    await asyncio.gather(tasks._refill_queue(cid), tasks._refill_queue(cid))

    with database.session() as s:
        total = len(s.exec(
            database.select(Post).where(Post.channel_id == cid, Post.status.in_(["pending", "scheduled"]))
        ).all())
    assert total <= 4, f"очередь выросла до {total} при заданной глубине 4"


# ── Зомби-посты на автопилоте ─────────────────────────────────────────────

async def test_backfill_closes_approvals_on_autopilot_channel():
    uid = _make_user("bf_appr@t.local")
    cid = _make_channel(uid, auto_publish=True)
    with database.session() as s:
        p = Post(channel_id=cid, user_id=uid, text="зомби", status="scheduled",
                  scheduled_at=datetime.utcnow() + timedelta(hours=1))
        s.add(p); s.commit(); s.refresh(p)
        s.add(PostApproval(post_id=p.id, channel_id=cid, review_chat_id=1,
                            deadline=datetime.utcnow() + timedelta(hours=1), status="waiting"))
        s.commit()
        pid = p.id

    await tasks.backfill_orphaned_posts()

    with database.session() as s:
        appr = s.exec(database.select(PostApproval).where(PostApproval.post_id == pid)).first()
    assert appr.status == "done", "подтверждение на автопилоте должно быть закрыто"


async def test_backfill_schedules_legacy_pending_posts_on_autopilot():
    """Наследие доC14: автопилот публиковал в момент генерации, при сбое пост навсегда оставался pending."""
    uid = _make_user("bf_pending@t.local")
    cid = _make_channel(uid, auto_publish=True)
    with database.session() as s:
        p = Post(channel_id=cid, user_id=uid, text="застрял", status="pending", scheduled_at=None)
        s.add(p); s.commit(); s.refresh(p)
        pid = p.id

    await tasks.backfill_orphaned_posts()

    with database.session() as s:
        post = s.get(Post, pid)
    assert post.status == "scheduled"
    assert post.scheduled_at is not None


async def test_backfill_leaves_confirm_mode_drafts_alone():
    """В режиме подтверждения «ждёт вашего решения» -- правда, трогать не надо."""
    uid = _make_user("bf_confirm@t.local")
    cid = _make_channel(uid, auto_publish=False)
    with database.session() as s:
        p = Post(channel_id=cid, user_id=uid, text="черновик", status="pending", scheduled_at=None)
        s.add(p); s.commit(); s.refresh(p)
        pid = p.id

    await tasks.backfill_orphaned_posts()

    with database.session() as s:
        assert s.get(Post, pid).status == "pending"


async def test_backfill_is_idempotent():
    uid = _make_user("bf_idem@t.local")
    cid = _make_channel(uid, auto_publish=True)
    with database.session() as s:
        s.add(Post(channel_id=cid, user_id=uid, text="p", status="pending", scheduled_at=None))
        s.commit()

    first = await tasks.backfill_orphaned_posts()
    second = await tasks.backfill_orphaned_posts()

    assert first["posts_scheduled"] == 1
    assert second == {"approvals_closed": 0, "posts_scheduled": 0, "claims_released": 0}


async def test_requeue_does_nothing_on_autopilot_channel(monkeypatch):
    """Дедлайн подтверждения на автопилоте не должен переносить пост в конец очереди."""
    async def _render(*a, **kw):
        return {"ok": True, "result": {"message_id": 1}}
    async def _edit(*a, **kw):
        return {"ok": True}
    monkeypatch.setattr(tasks, "_render_approval_card", _render)
    monkeypatch.setattr(tasks.telegram_api, "edit_message_text", _edit)

    uid = _make_user("requeue_auto@t.local")
    cid = _make_channel(uid, auto_publish=True)
    slot = datetime.utcnow() - timedelta(minutes=1)
    with database.session() as s:
        p = Post(channel_id=cid, user_id=uid, text="пост", status="scheduled", scheduled_at=slot)
        s.add(p); s.commit(); s.refresh(p)
        appr = PostApproval(post_id=p.id, channel_id=cid, review_chat_id=1,
                             review_message_id=1, deadline=slot, status="waiting")
        s.add(appr); s.commit(); s.refresh(appr)
        pid, aid = p.id, appr.id

    await tasks._requeue_unconfirmed_post(aid, pid, 1, 1)

    with database.session() as s:
        post = s.get(Post, pid)
        appr = s.get(PostApproval, aid)
    assert post.scheduled_at == slot, "время публикации на автопилоте меняться не должно"
    assert post.requeued_at is None, "красной плашки «не подтвердили» на автопилоте быть не может"
    assert appr.status == "done", "подтверждение должно закрыться, а не остаться крутиться"
