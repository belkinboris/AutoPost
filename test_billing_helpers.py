"""
billing.describe_payment_method -- человекочитаемое имя сохранённого способа
оплаты для кабинета. СБП добавлен 31.07 (ЮKassa подключила рекуррент и для
него, не только для карт) -- раньше падал в общее "Сохранённый способ
оплаты", хотя ответ ЮKassa вполне определённо говорит "type": "sbp".
"""

import billing


def test_card_shows_last4():
    payment = {"payment_method": {"type": "bank_card", "card": {"last4": "4242"}}}
    assert billing.describe_payment_method(payment) == "Банковская карта •••• 4242"


def test_sbp_is_named_explicitly():
    payment = {"payment_method": {"type": "sbp", "title": "СБП"}}
    assert billing.describe_payment_method(payment) == "СБП"


def test_sbp_with_bank_title_included():
    payment = {"payment_method": {"type": "sbp", "title": "Т-Банк"}}
    assert billing.describe_payment_method(payment) == "СБП (Т-Банк)"


def test_unknown_type_falls_back_to_title():
    payment = {"payment_method": {"type": "sberbank", "title": "SberPay"}}
    assert billing.describe_payment_method(payment) == "SberPay"


def test_no_data_falls_back_to_generic_label():
    assert billing.describe_payment_method({}) == "Сохранённый способ оплаты"
