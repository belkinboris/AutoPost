"""
Единый источник цен (аудит 05.08).

Таблица тарифов жила ЧЕТЫРЬМЯ копиями: config._DEFAULT_PACKAGES (по ней
реально списывают деньги), config.PLANS, захардкоженный массив в
renderBilling (app.part14.js) и статичный static/landing.html. Цены менялись
уже трижды (990→490, 2490→1290→990...), и каждый раз все копии надо было
не забыть руками. Показывать одну цену, а списывать другую -- нарушение
правила 5 и закона о защите прав потребителей, причём ровно в той точке,
где человек решает, доверять ли сервису деньги.

Кабинет теперь строит карточки из /api/config -- копии там больше нет
(закреплено тестом на текст файла). Лендинг намеренно остаётся статичным
HTML (правило 6: он обязан отрисоваться мгновенно и без единого запроса),
поэтому его синхронность закрепляется тестом: разъехались -- красный.
"""

import re
from pathlib import Path

import config

LANDING = Path(__file__).parent / "static" / "landing.html"
PART14 = Path(__file__).parent / "static" / "app.part14.js"


def test_landing_prices_match_config():
    """Каждая цена из конфига обязана стоять на лендинге, и наоборот --
    лендинг не должен рекламировать цену, по которой не спишут."""
    html = LANDING.read_text(encoding="utf-8")
    # JSON-LD блок: {"@type": "Offer", "price": "490", ... "name": "Старт"}
    jsonld_prices = dict(re.findall(
        r'"@type": "Offer", "price": "(\d+)", "priceCurrency": "RUB", "name": "([^"]+)"', html
    ))
    assert jsonld_prices, "JSON-LD с ценами пропал с лендинга -- SEO-разметка сломана"

    for pkg in config._DEFAULT_PACKAGES:
        title, rub = pkg["title"], pkg["rub"]
        assert str(rub) in jsonld_prices and jsonld_prices[str(rub)] == title, (
            f"JSON-LD лендинга разошёлся с конфигом: «{title}» должен стоить {rub} ₽"
        )
        # Видимая карточка тарифа: <h3>Старт</h3>...<div class="price">490 ₽
        card = re.search(rf"<h3>{re.escape(title)}</h3>.*?class=\"price\">([\d\s ]+)", html)
        assert card, f"на лендинге нет карточки тарифа «{title}»"
        shown = int(re.sub(r"\D", "", card.group(1)))
        assert shown == rub, (
            f"лендинг показывает «{title}» за {shown} ₽, а спишется {rub} ₽"
        )
        # Зачёркнутая «потом N ₽» тоже обязана совпадать с rub_regular.
        if pkg.get("rub_regular"):
            reg = re.search(
                rf"<h3>{re.escape(title)}</h3>.*?price-regular\">потом ([\d\s ]+)", html
            )
            assert reg, f"у «{title}» на лендинге пропала цена «потом»"
            shown_reg = int(re.sub(r"\D", "", reg.group(1)))
            assert shown_reg == pkg["rub_regular"], (
                f"лендинг обещает «потом {shown_reg} ₽» у «{title}», в конфиге {pkg['rub_regular']}"
            )


def test_cabinet_has_no_hardcoded_price_table():
    """renderBilling обязан строить тарифы из App.cfg.packages. Захардкоженная
    копия вида {id:"p1",...,price:490,...} -- ровно то, что разъезжается при
    смене цен; её возвращение должно ловиться до деплоя."""
    src = PART14.read_text(encoding="utf-8")
    assert not re.search(r"price\s*:\s*\d{3,}", src), (
        "в app.part14.js вернулась захардкоженная цена -- тарифы должны "
        "приходить из /api/config"
    )
    assert "App.cfg?.packages" in src or "App.cfg.packages" in src, (
        "renderBilling больше не читает пакеты из конфига сервера"
    )


def test_config_exposes_everything_the_cabinet_needs():
    """Карточке тарифа нужны каналы и пометка «Популярный» -- если их нет в
    ответе /api/config, фронт снова начнёт хардкодить."""
    import main
    cfg = main.get_config()
    packages = cfg["packages"]
    assert packages, "в /api/config пропали пакеты"
    for p in packages:
        assert "channels" in p, f"у пакета {p['id']} нет channels -- лимит каналов нечем показать"
        assert "popular" in p, f"у пакета {p['id']} нет popular"
    assert any(p["popular"] for p in packages), "ни один тариф не помечен «Популярный»"
    # Лимиты каналов -- те же, по которым сервер реально ограничивает
    # создание каналов (_channel_limit_with_plan использует CHANNELS_BY_PACKAGE).
    for p in packages:
        expected = config.CHANNELS_BY_PACKAGE.get(p["id"], config.FREE_CHANNELS)
        assert p["channels"] == expected, (
            f"пакет {p['id']}: на экран уйдёт {p['channels']} каналов, "
            f"а сервер ограничит по {expected}"
        )
    assert cfg["post_tokens_min"] == config.POST_TOKENS_MIN
    assert cfg["post_tokens_max"] == config.POST_TOKENS_MAX


def test_posts_range_math_matches_comments():
    """Диапазон «15–30 постов» на карточке считается из tokens и стоимости
    поста. Проверяем на «Старт»: 600к токенов при 20-40к за пост = 15-30."""
    p1 = config.package_by_id("p1")
    assert p1["tokens"] // config.POST_TOKENS_MAX == 15
    assert p1["tokens"] // config.POST_TOKENS_MIN == 30
