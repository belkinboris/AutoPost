"""
Биллинг через YooKassa.

Поток оплаты:
  1. Пользователь выбирает пакет токенов — создаём локальный Payment(status=pending).
  2. Создаём платёж YooKassa через API /v3/payments.
  3. Отдаём пользователю confirmation_url для оплаты.
  4. YooKassa присылает webhook на /api/yookassa/notify.
  5. Для защиты от поддельных уведомлений повторно запрашиваем платёж в API YooKassa
     и начисляем токены только если статус платежа актуально succeeded, а paid == true.
"""

import logging
import uuid
from typing import Any

import httpx

import config

logger = logging.getLogger(__name__)

YOOKASSA_PAYMENTS_URL = "https://api.yookassa.ru/v3/payments"
YOOKASSA_REFUNDS_URL = "https://api.yookassa.ru/v3/refunds"


class YooKassaError(RuntimeError):
    """Ошибка при обращении к YooKassa."""


def is_configured() -> bool:
    return bool(config.YOOKASSA_SHOP_ID and config.YOOKASSA_SECRET_KEY)


def _auth() -> tuple[str, str]:
    return (config.YOOKASSA_SHOP_ID, config.YOOKASSA_SECRET_KEY)


def _amount(value: float | int) -> dict[str, str]:
    return {"value": f"{float(value):.2f}", "currency": "RUB"}


def _receipt(description: str, amount_rub: float, user_email: str | None) -> dict[str, Any] | None:
    """
    Опциональный чек для тех магазинов YooKassa, где включена фискализация.
    По умолчанию выключен, потому что настройка чеков зависит от юрлица/ИП и схемы работы магазина.
    """
    if not config.YOOKASSA_SEND_RECEIPT:
        return None
    if not user_email:
        raise YooKassaError("Для отправки чека YooKassa нужен email пользователя")

    return {
        "customer": {"email": user_email},
        "items": [
            {
                "description": description[:128],
                "quantity": "1.00",
                "amount": _amount(amount_rub),
                "vat_code": config.YOOKASSA_VAT_CODE,
                "payment_subject": "service",
                "payment_mode": "full_payment",
            }
        ],
    }


async def create_payment(
    *,
    label: str,
    amount_rub: float,
    description: str,
    user_id: int,
    package_id: str,
    user_email: str | None = None,
    save_payment_method: bool = False,
) -> dict[str, Any]:
    """
    Создаёт платёж YooKassa и возвращает объект платежа.

    save_payment_method=True -- первый платёж подписки: просим YooKassa
    сохранить метод оплаты, чтобы дальше списывать автоматически без участия
    пользователя (см. charge_recurring). Пользователь подтверждает привязку
    на стороне YooKassa в тот же момент, что и саму оплату.
    """
    if not is_configured():
        raise YooKassaError("YooKassa не настроена: задайте YOOKASSA_SHOP_ID и YOOKASSA_SECRET_KEY")

    payload: dict[str, Any] = {
        "amount": _amount(amount_rub),
        "capture": True,
        "confirmation": {
            "type": "redirect",
            "return_url": config.YOOKASSA_RETURN_URL,
        },
        "description": description[:128],
        "metadata": {
            "label": label,
            "user_id": str(user_id),
            "package_id": package_id,
        },
    }
    if save_payment_method:
        payload["save_payment_method"] = True

    receipt = _receipt(description, amount_rub, user_email)
    if receipt:
        payload["receipt"] = receipt

    headers = {
        "Idempotence-Key": str(uuid.uuid4()),
        "Content-Type": "application/json",
    }

    async with httpx.AsyncClient(timeout=20.0) as client:
        try:
            response = await client.post(
                YOOKASSA_PAYMENTS_URL,
                auth=_auth(),
                headers=headers,
                json=payload,
            )
        except httpx.HTTPError as exc:
            raise YooKassaError(f"Не удалось обратиться к YooKassa: {exc}") from exc

    if response.status_code >= 400:
        logger.warning("YooKassa create_payment error %s: %s", response.status_code, response.text)
        raise YooKassaError(_extract_error_message(response))

    data = response.json()
    confirmation_url = (data.get("confirmation") or {}).get("confirmation_url")
    if not confirmation_url:
        raise YooKassaError("YooKassa не вернула ссылку на оплату")
    return data


async def charge_recurring(
    *,
    payment_method_id: str,
    amount_rub: float,
    description: str,
    user_id: int,
    package_id: str,
    label: str,
    idempotence_key: str,
    user_email: str | None = None,
) -> dict[str, Any]:
    """
    Автосписание по сохранённому методу оплаты -- продление подписки без
    участия пользователя.

    Отличия от create_payment: передаём payment_method_id и НЕ передаём
    confirmation (подтверждать нечего, деньги списываются сразу).

    idempotence_key обязателен и должен быть детерминированным для конкретного
    периода подписки, а не случайным: YooKassa по этому ключу отдаёт уже
    созданный платёж вместо создания нового. Именно это защищает от двойного
    списания, если джоба продления упала после запроса, но до записи
    результата в БД, и запустилась повторно.
    """
    if not is_configured():
        raise YooKassaError("YooKassa не настроена")
    if not payment_method_id:
        raise YooKassaError("Нет сохранённого метода оплаты")
    if not idempotence_key:
        raise YooKassaError("Для автосписания обязателен idempotence_key")

    payload: dict[str, Any] = {
        "amount": _amount(amount_rub),
        "capture": True,
        "payment_method_id": payment_method_id,
        "description": description[:128],
        "metadata": {
            "label": label,
            "user_id": str(user_id),
            "package_id": package_id,
            "recurring": "1",
        },
    }

    receipt = _receipt(description, amount_rub, user_email)
    if receipt:
        payload["receipt"] = receipt

    headers = {
        "Idempotence-Key": idempotence_key,
        "Content-Type": "application/json",
    }

    async with httpx.AsyncClient(timeout=20.0) as client:
        try:
            response = await client.post(
                YOOKASSA_PAYMENTS_URL,
                auth=_auth(),
                headers=headers,
                json=payload,
            )
        except httpx.HTTPError as exc:
            raise YooKassaError(f"Не удалось обратиться к YooKassa: {exc}") from exc

    if response.status_code >= 400:
        logger.warning("YooKassa charge_recurring error %s: %s", response.status_code, response.text)
        raise YooKassaError(_extract_error_message(response))

    return response.json()


def describe_payment_method(payment: dict[str, Any]) -> str:
    """
    Человекочитаемое описание сохранённого способа оплаты для кабинета:
    «Банковская карта •••• 4444», «СБП», «SberPay» и т.п.

    Полных реквизитов карты YooKassa нам не отдаёт и мы их не храним -- только
    последние 4 цифры из ответа, чтобы пользователь понимал, какую именно карту
    он отвязывает.

    create_payment() не ограничивает payment_method_data конкретным типом --
    страница оплаты YooKassa сама показывает все методы, подключённые магазину
    (карта, СБП, SberPay...), и save_payment_method относится к тому, что
    выберет плательщик. Поэтому extract_saved_method_id/charge_recurring уже
    работают с любым типом одинаково (id -- он и есть id); здесь только явно
    называем СБП по имени, а не молчаливым "Сохранённый способ оплаты", раз
    31.07 подключили рекуррент и для него.
    """
    method = payment.get("payment_method") or {}
    title = method.get("title") or ""
    card = method.get("card") or {}
    last4 = card.get("last4") or ""
    if last4:
        return f"Банковская карта •••• {last4}"
    if method.get("type") == "sbp":
        return "СБП" + (f" ({title})" if title and title != "СБП" else "")
    if title:
        return str(title)
    return "Сохранённый способ оплаты"


def extract_saved_method_id(payment: dict[str, Any]) -> str:
    """
    Достаёт id сохранённого метода оплаты из ответа YooKassa.

    Метод пригоден для автосписаний только если YooKassa явно пометила его
    saved=true -- обычный (не сохранённый) метод для рекуррентов не подходит,
    поэтому проверяем флаг, а не просто наличие id.
    """
    method = payment.get("payment_method") or {}
    if not method.get("saved"):
        return ""
    return method.get("id") or ""


async def get_payment(payment_id: str) -> dict[str, Any]:
    """Получает актуальный статус платежа из YooKassa."""
    if not is_configured():
        raise YooKassaError("YooKassa не настроена")
    if not payment_id:
        raise YooKassaError("Не передан payment_id")

    async with httpx.AsyncClient(timeout=20.0) as client:
        try:
            response = await client.get(f"{YOOKASSA_PAYMENTS_URL}/{payment_id}", auth=_auth())
        except httpx.HTTPError as exc:
            raise YooKassaError(f"Не удалось проверить платёж в YooKassa: {exc}") from exc

    if response.status_code >= 400:
        logger.warning("YooKassa get_payment error %s: %s", response.status_code, response.text)
        raise YooKassaError(_extract_error_message(response))

    return response.json()


async def refund_payment(*, payment_operation_id: str, amount_rub: float, idempotence_key: str) -> dict[str, Any]:
    """
    Возврат по уже проведённому платежу (POST /v3/refunds).

    idempotence_key обязателен и должен быть детерминированным (например,
    "refund-{наш Payment.id}") -- та же причина, что и у charge_recurring:
    повторный запрос (сеть оборвалась после отправки, но до ответа) должен
    вернуть тот же возврат, а не создать второй.
    """
    if not is_configured():
        raise YooKassaError("YooKassa не настроена")
    if not payment_operation_id:
        raise YooKassaError("Нет идентификатора платежа ЮKassa для возврата")
    if not idempotence_key:
        raise YooKassaError("Для возврата обязателен idempotence_key")

    payload = {
        "payment_id": payment_operation_id,
        "amount": _amount(amount_rub),
    }
    headers = {
        "Idempotence-Key": idempotence_key,
        "Content-Type": "application/json",
    }

    async with httpx.AsyncClient(timeout=20.0) as client:
        try:
            response = await client.post(
                YOOKASSA_REFUNDS_URL, auth=_auth(), headers=headers, json=payload,
            )
        except httpx.HTTPError as exc:
            raise YooKassaError(f"Не удалось обратиться к YooKassa: {exc}") from exc

    if response.status_code >= 400:
        logger.warning("YooKassa refund_payment error %s: %s", response.status_code, response.text)
        raise YooKassaError(_extract_error_message(response))

    return response.json()


def _extract_error_message(response: httpx.Response) -> str:
    try:
        data = response.json()
    except Exception:
        return f"Ошибка YooKassa: HTTP {response.status_code}"

    description = data.get("description") or data.get("parameter") or data.get("code")
    if description:
        return f"Ошибка YooKassa: {description}"
    return f"Ошибка YooKassa: HTTP {response.status_code}"
