"""Этап 2 ТЗ v2: квоты фич, парсер сравнения, анти-клон вердикты, формат."""
from __future__ import annotations

import asyncio
from datetime import datetime
from zoneinfo import ZoneInfo

import services.anticlone as anticlone
import services.compare as cmp
import services.quotas as quotas
import services.sqlite_store as store
from bot.messages import check_result_message, compare_result_message
from models.product import SearchItem

MSK = ZoneInfo("Europe/Moscow")
DAY = datetime(2026, 11, 5, 12, 0, tzinfo=MSK)


# ─── Квоты фич ───────────────────────────────────────────────────────


def _tmp(monkeypatch, tmp_path):
    db = str(tmp_path / "state.db")
    monkeypatch.setattr(store.Config, "STATE_DB_PATH", db, raising=False)
    return db


def test_feature_limits_matrix():
    # матрица ТЗ §3.0: xvsy 0/10/40/150; check 1(день)/20/80/безлимит
    assert quotas.feature_limit("free", "xvsy") == (0, "month")
    assert quotas.feature_limit("base", "xvsy") == (10, "month")
    assert quotas.feature_limit("advanced", "xvsy") == (40, "month")
    assert quotas.feature_limit("marketing", "xvsy") == (150, "month")
    assert quotas.feature_limit("free", "check") == (1, "day")
    assert quotas.feature_limit("marketing", "check") == (None, "month")
    assert quotas.feature_limit("unknown-plan", "xvsy") == (0, "month")  # консервативно


def test_check_feature_daily_for_free(monkeypatch, tmp_path):
    _tmp(monkeypatch, tmp_path)
    ok, info = quotas.check_feature_quota(1, "free", "check", now=DAY)
    assert ok and info["limit"] == 1 and info["period"] == "day"
    quotas.consume_feature(1, "check", "free", now=DAY)
    ok, info = quotas.check_feature_quota(1, "free", "check", now=DAY)
    assert not ok  # вторая проверка за день free — нельзя


def test_xvsy_monthly_counter(monkeypatch, tmp_path):
    _tmp(monkeypatch, tmp_path)
    for i in range(10):
        ok, _ = quotas.check_feature_quota(2, "base", "xvsy", now=DAY)
        assert ok
        quotas.consume_feature(2, "xvsy", "base", now=DAY)
    ok, info = quotas.check_feature_quota(2, "base", "xvsy", now=DAY)
    assert not ok and info["used"] == 10 and info["limit"] == 10
    # другой пользователь — свой счётчик
    ok, _ = quotas.check_feature_quota(3, "base", "xvsy", now=DAY)
    assert ok


def test_marketing_check_unlimited(monkeypatch, tmp_path):
    _tmp(monkeypatch, tmp_path)
    quotas.consume_feature(4, "check", "marketing", now=DAY)
    quotas.consume_feature(4, "check", "marketing", now=DAY)
    ok, info = quotas.check_feature_quota(4, "marketing", "check", now=DAY)
    assert ok and info["limit"] is None


# ─── Парсер сравнения ────────────────────────────────────────────────


def test_parse_compare():
    assert cmp.parse_compare_query("сравни Redmi Note 14 и Realme C75") == (
        "Redmi Note 14", "Realme C75"
    )
    assert cmp.parse_compare_query("сравни iPhone 15 vs Galaxy S24") == (
        "iPhone 15", "Galaxy S24"
    )
    assert cmp.parse_compare_query("сравнить термопот и чайник") == ("термопот", "чайник")
    assert cmp.parse_compare_query("сравни") is None
    assert cmp.parse_compare_query("куплю iPhone 15") is None  # не «сравни»


def _item(name, price, rating, reviews, mp="ozon"):
    return SearchItem(
        product_id="1", name=name, price=price, rating=rating,
        reviews_count=reviews, url=f"https://x/{mp}", marketplace=mp,
    )


def test_pick_best_prefers_priced_and_rated():
    items = [
        _item("Без цены", 0, 4.5, 100),
        _item("С ценой", 2000, 4.2, 300),
    ]
    assert cmp.pick_best(items).name == "С ценой"


def test_compare_verdict_price_gap():
    a = _item("Дешёвый", 2000, 4.3, 200)
    b = _item("Дорогой", 2600, 4.3, 200)
    verdict = cmp.compare_verdict(a, b)
    assert "Дешевле" in verdict and "23%" in verdict
    assert "переформулируйте" in cmp.compare_verdict(None, None)


# ─── Анти-клон ───────────────────────────────────────────────────────


def test_clone_name_is_danger():
    v = anticlone.verdict_for("I15 Pro Max 256гб новый", [])
    assert v["level"] == "danger" and v["confidence"] == "высокая"


def test_normal_name_is_ok():
    v = anticlone.verdict_for("термос для школы 1 литр", [])
    assert v["level"] == "ok"


def test_suspicious_price_is_warn():
    items = [
        _item("iPhone 15 128", 9000, 4.5, 500, "ozon"),
        _item("iPhone 15 128", 31000, 4.6, 800, "wb"),
        _item("iPhone 15 128", 30500, 4.4, 300, "yam"),
    ]
    v = anticlone.verdict_for("iPhone 15 128гб", items)
    assert v["level"] == "warn"
    assert any("30% медианы" in r for r in v["reasons"])


# ─── Формат сообщений ────────────────────────────────────────────────


def test_compare_message_format():
    a = {"name": "Redmi Note 14", "price": 17000, "rating": 4.3,
         "reviews_count": 612, "marketplace": "ozon", "url": "https://oz"}
    b = {"name": "Realme C75", "price": 0, "rating": None,
         "reviews_count": 0, "marketplace": "wb", "url": "https://wb"}
    text = compare_result_message("Redmi", "Realme", a, b, "A дешевле.", "минус 2 поиска")
    assert "Сравнение" in text and "цена по ссылке" in text and "минус 2 поиска" in text
    empty = compare_result_message("X", "Y", None, None, "ничего", "")
    assert "не нашёл" in empty


def test_check_message_format():
    text = check_result_message("I15 Pro Max", {
        "level": "danger", "confidence": "высокая", "reasons": ["похоже на клон"],
    })
    assert "высокая вероятность" in text and "похоже на клон" in text


# ─── Этап 3: подарочный подбор ───────────────────────────────────────


def test_gift_parse():
    from services.gift import parse_gift_request

    assert parse_gift_request("маме до 2000") == ("маме", 2000)
    assert parse_gift_request("мальчику 7 лет до 3 000 руб") == ("мальчику 7 лет", 3000)
    assert parse_gift_request("маме") is None            # нет бюджета
    assert parse_gift_request("до 2000") is None          # нет описания


def test_gift_queries_from_llm():
    from services.gift import queries_from_llm

    text = "1) набор для рукоделия\n2) плед флисовый\n3) шкатулка для украшений\n4) книга любимому жанру"
    assert len(queries_from_llm(text)) == 4
    assert queries_from_llm("плед\nноски")[0] == "плед"
    assert queries_from_llm("") == []


def test_gift_message_format():
    from bot.messages import gift_message

    item = _item("Плед флисовый", 1500, 4.6, 210, "wb")
    text = gift_message("маме", 2000, [("плед", item), ("носки", None)])
    assert "Подарки: маме" in text and "1\u202f500 ₽" in text
    assert "не нашёл" in text
    empty = gift_message("маме", 2000, [("плед", None)])
    assert "Ничего не нашёл" in empty


# ─── Этап 4: сводки для продавцов ────────────────────────────────────


def test_report_build_stats():
    from services.report import build_report

    items = [
        _item("Термос A", 1000, 4.7, 500, "ozon"),
        _item("Термос B", 1300, 4.5, 300, "wb"),
        _item("Термос C", 1200, 4.4, 200, "yam"),
        _item("Термос демпинг", 200, None, 5, "wb"),
        _item("I15 Pro Max клон", 0, None, 0, "ozon"),
    ]
    stats = build_report("термос для школы", items)
    assert stats["total"] == 5
    assert stats["per_marketplace"]["ozon"] == 2
    assert stats["dump_count"] == 1          # 200 < 30% черновой медианы
    assert stats["clone_count"] == 1         # паттерн клона
    assert len(stats["top"]) == 5
    assert stats["top"][0].name == "Термос A"  # лучший рейтинг×отзывы
    assert stats["median"] == 1200  # демпинг 200₽ исключён из коридора



def test_plan_searches_per_month_field_from_vitrina():
    """Админ добавил searches_per_month — приоритет над локальным маппингом."""
    plans = [{"plan": "base", "name": "Базовый", "resource_limit": 3,
              "searches_per_month": 80}]
    monkey_saved = quotas._plans_state
    quotas._plans_state = {"at": 1.0, "plans": plans}
    try:
        limits = quotas.plan_limits("base")
        assert limits["searches"] == 80 and limits["period"] == "month"
    finally:
        quotas._plans_state = monkey_saved


def test_trial_subscription_gets_marketing():
    client = type("C", (), {})()
    client.get_plans = lambda: [
        {"plan": "base", "name": "Базовый", "resource_limit": 3,
         "searches_per_month": 80},
        {"plan": "marketing", "name": "Маркетинг", "resource_limit": 30,
         "searches_per_month": 800},
    ]
    client.get_subscription_state = lambda uid: {
        "present": True, "status": "active", "plan": "base", "trial": True,
    }
    code = asyncio.run(quotas.user_plan(9, client=client))
    assert code == "marketing"  # триал = все опции «Маркетинг» (решение Шефа)
    assert quotas.plan_limits("marketing")["searches"] == 800


def test_report_caption_detector():
    from services.report import should_report_caption

    assert should_report_caption("сводка")
    assert should_report_caption("Сводка по нише")
    assert should_report_caption("отчёт")
    assert should_report_caption("нужен анализ ниши")
    assert should_report_caption("report")
    assert not should_report_caption("термос")
    assert not should_report_caption(None)
    assert not should_report_caption("")


def test_trial_status_from_vitrina_is_marketing():
    """Баг «Лимит 0 из 0»: триальная подписка на витрине имеет status='trial',
    а не 'active' — должна давать тариф «Маркетинг» (решение Шефа 05.11)."""
    client = type("C", (), {})()
    client.get_plans = lambda: [
        {"plan": "marketing", "name": "Маркетинг", "resource_limit": 30,
         "searches_per_month": 800},
    ]
    client.get_subscription_state = lambda uid: {
        "present": True, "status": "trial", "plan": "marketing",
    }
    code = asyncio.run(quotas.user_plan(11, client=client))
    assert code == "marketing"
    assert quotas.plan_limits("marketing")["searches"] == 800
    ok, info = quotas.check_feature_quota(11, "marketing", "report", now=DAY)
    assert ok and info["limit"] == 100




# ─── Сводка v2: многозапросная сборка, аналитика, лимит 100 ──────────


def test_report_v2_full_stats():
    from services.report import build_report

    items = [
        _item("Термос A 1л", 1000, 4.7, 500, "ozon"),
        _item("Термос B", 1300, 4.5, 300, "wb"),
        _item("Термос C", 1200, 4.4, 200, "yam"),
        _item("Термос D", 1450, 4.8, 800, "wb"),
        _item("Термос демпинг", 200, None, 5, "wb"),
        _item("I15 Pro Max клон", 0, None, 0, "ozon"),
    ]
    stats = build_report("термос для школы", items)
    assert stats["total"] == 6
    assert stats["dump_count"] == 1         # 200₽ < 30% черновой медианы
    assert stats["median"] == 1250          # (1200+1300)/2 — демпинг не искажает
    assert stats["min"] == 1000 and stats["max"] == 1450  # коридор без демпинга
    assert stats["mp_medians"]["wb"]["median"] == 1375  # (1300+1450)/2
    assert stats["recommended_price"] == 1190  # 1250*0.95 → округление до 10
    assert stats["rated_count"] == 4 and stats["avg_rating"] > 4.4
    assert stats["rooted_count"] == 4       # отзывов >100: 500/300/200/800
    assert stats["dump_count"] == 1
    assert stats["clone_count"] == 1
    assert "низкая" in stats["competition"]  # 6 карточек — порог «средней» от 8
    buckets = stats["price_buckets"]
    assert buckets.get("1000–2000₽") == 4 and "менее 1000₽" not in buckets


def test_report_message_v2_numbers():
    from services.report import build_report
    from bot.messages import report_message

    items = [
        _item("Термос A", 1000, 4.7, 500, "ozon"),
        _item("Термос B", 1300, 4.5, 300, "wb"),
        _item("Термос C", 1200, 4.4, 200, "yam"),
    ]
    text = report_message(build_report("термос", items), ai_summary="Заходить, цена 1150₽.")
    assert "Цены (3 карточек)" in text
    assert "медиана *1\u202f200 ₽*" in text
    assert "Рекомендуемая цена входа: ~1\u202f140 ₽" in text
    assert "Резюме для продавца" in text and "Заходить" in text
    assert "100 сводок/мес" in text


def test_report_queries_count_and_dedupe():
    from services.report import niche_queries_count

    assert niche_queries_count() == 4


def test_review_pages_excluded():
    from parser.firecrawl_search import _parse_results

    data = {"web": [
        {"url": "https://www.ozon.ru/product/termos-2596659119/", "title": "Термос"},
        {"url": "https://www.ozon.ru/product/termos-2596659119/reviews/", "title": "267 отзыв на Термос"},
    ]}
    items = _parse_results(data)
    assert len(items) == 1  # страница отзывов не карточка


def test_report_limit_is_100(monkeypatch, tmp_path):
    _tmp(monkeypatch, tmp_path)
    assert quotas.feature_limit("marketing", "report") == (100, "month")
    for _ in range(100):
        quotas.consume_feature(13, "report", "marketing", now=DAY)
    ok, info = quotas.check_feature_quota(13, "marketing", "report", now=DAY)
    assert not ok and info["used"] == 100


def test_stats_for_ai_contains_numbers():
    from services.report import build_report, stats_for_ai

    items = [_item("Термос A", 1000, 4.7, 500, "ozon"), _item("Термос B", 1300, 4.5, 300, "wb")]
    text = stats_for_ai(build_report("термос", items))
    assert "медиана 1150₽" in text and "Карточек собрано: 2" in text


def test_wb_price_and_rating_formats():
    """WB: цена в заголовке «купить за 823», рейтинг «4,7 722 оценки»."""
    from parser.firecrawl_search import _parse_results

    data = {"web": [
        {"url": "https://www.wildberries.ru/catalog/907321076/detail.aspx",
         "title": "Термос детский для школы и прогулок 907321076 купить за 823",
         "description": "Артикул, 907321076. Цвет, зеленый."},
        {"url": "https://www.wildberries.ru/catalog/tags/termos-dlja-shkola",
         "title": "Термос для школа - Купить в интернет магазине",
         "description": "4.524 IntermoS / Термос детский 4,7 722 оценки. Послезавтра."},
        {"url": "https://global.wildberries.ru/catalog/tags/termos",
         "title": "Термос", "description": "3 750 ₸ 8 970 ₸"},
    ]}
    items = _parse_results(data)
    assert len(items) == 1 and items[0].price == 823.0
    assert items[0].reviews_count is None  # у detail-карточки рейтинга не было


def test_catalog_price_samples():
    """Сигнатуры цен из сниппетов каталогов попадают в коридор."""
    from services.report import build_report

    cards = [_item("Термос A", 1200, 4.5, 100, "ozon")]
    samples = [1087.0, 1306.0]  # «-75%. 1 087 ₽ 4 447 ₽» и «1 306 ₽ 1 810 ₽»
    stats = build_report("термос", cards, price_samples=samples)
    assert stats["sample_count"] == 2
    assert stats["median"] == 1200            # (1087, 1200, 1306) → 1200
    assert stats["min"] == 1087 and stats["max"] == 1306


# ─── Канальный поиск: Firecrawl → Serper (решение Шефа 06.11) ────────


class _FakeFC:
    def __init__(self, fail=False, items=None, raw=None):
        self.fail = fail
        self.items = items or []
        self.raw = raw or []
        self.calls = 0

    async def search_marketplaces(self, query, *, limit=10, marketplaces=None):
        self.calls += 1
        if self.fail:
            raise RuntimeError("firecrawl down")
        return self.items

    async def search_web(self, query, *, limit=10, marketplaces=None):
        self.calls += 1
        if self.fail:
            raise RuntimeError("firecrawl down")
        return self.raw


class _FakeSerper:
    def __init__(self):
        self.calls = 0
        self.organic = [
            {"title": "Термос из Google", "link": "https://www.ozon.ru/product/t-123456/",
             "snippet": "Термос для школы, 1 250 ₽. 4,8 320 оценок"},
        ]

    async def search_organic(self, query, num=10):
        self.calls += 1
        return self.organic


def _channel(monkeypatch, fc: _FakeFC, serper: _FakeSerper):
    import parser.search_channel as ch

    async def fake_fc(q, *, limit, mp):
        return await fc.search_marketplaces(q, limit=limit, marketplaces=mp)

    async def fake_fc_raw(q, *, limit, mp):
        return await fc.search_web(q, limit=limit, marketplaces=mp)

    async def fake_si(q, *, limit, mp=None):
        from parser.firecrawl_search import _parse_results
        return _parse_results({"web": [
            {"url": r["link"], "title": r["title"], "description": r["snippet"]}
            for r in await serper.search_organic(q, num=limit)
        ]})

    async def fake_sr(q, *, limit, mp=None):
        return [
            {"url": r["link"], "title": r["title"], "description": r["snippet"]}
            for r in await serper.search_organic(q, num=limit)
        ]

    monkeypatch.setattr(ch, "_firecrawl", fake_fc)
    monkeypatch.setattr(ch, "_firecrawl_raw", fake_fc_raw)
    monkeypatch.setattr(ch, "_serper_items", fake_si)
    monkeypatch.setattr(ch, "_serper_raw", fake_sr)
    monkeypatch.setattr(ch.Config, "FIRECRAWL_API_KEY", "x", raising=False)
    monkeypatch.setattr(ch.Config, "SERPER_API_KEY", "y", raising=False)
    monkeypatch.setattr(
        "services.credit_monitor.usage_snapshot", lambda: {"level": "ok"}, raising=False
    )
    monkeypatch.setattr(
        "parser.search_channel.credit_monitor.mark_provider_exhausted", lambda: None,
        raising=False,
    )


def test_channel_firecrawl_ok_serper_not_spent(monkeypatch):
    import parser.search_channel as ch

    fc = _FakeFC(items=[_item("Термос A", 1200, 4.5, 100, "ozon")])
    serper = _FakeSerper()
    _channel(monkeypatch, fc, serper)
    items = asyncio.run(ch.search_marketplaces("термос", limit=10))
    assert len(items) == 1
    assert serper.calls == 0  # Firecrawl здоров — резерв не тратится


def test_channel_falls_back_to_serper_on_firecrawl_failure(monkeypatch):
    import parser.search_channel as ch

    fc = _FakeFC(fail=True)
    serper = _FakeSerper()
    _channel(monkeypatch, fc, serper)
    items = asyncio.run(ch.search_marketplaces("термос", limit=10))
    assert fc.calls == 1 and serper.calls == 1
    assert items[0].name == "Термос из Google"
    assert items[0].price == 1250.0  # сниппет Google с ценой парсится
    assert items[0].reviews_count == 320  # «320 оценок» — тоже парсится


def test_channel_raw_falls_back(monkeypatch):
    import parser.search_channel as ch

    fc = _FakeFC(fail=True)
    serper = _FakeSerper()
    _channel(monkeypatch, fc, serper)
    raw = asyncio.run(ch.search_web_raw("термос", limit=10))
    assert raw[0]["url"] == "https://www.ozon.ru/product/t-123456/"


def test_serper_parsing_quality():
    """Serper-сниппеты: цена и «оценки» извлекаются стандартными парсерами."""
    from parser.firecrawl_search import _parse_results

    data = {"web": [
        {"url": "https://www.wildberries.ru/catalog/tags/termos",
         "title": "Термос в школу - Купить", "description": "-27%. 1 306 ₽ 1 810 ₽ −27%"},
        {"url": "https://www.ozon.ru/product/termos-5034127580/",
         "title": "Детский термос", "description": "4,8 431 оценка. Послезавтра"},
    ]}
    items = _parse_results(data)
    # теги WB — не карточка, но Ozon-карточка с рейтингом «оценки» проходит
    assert len(items) == 1
    assert items[0].rating == 4.8 and items[0].reviews_count == 431


# ─── Качество выдачи: журнал, пробник, алерты ────────────────────────


def test_quality_log_and_compare(monkeypatch, tmp_path):
    import services.sqlite_store as st

    db = str(tmp_path / "q.db")
    st.quality_log("firecrawl", "термос", 6, 2, 3, path=db)
    st.quality_log("firecrawl", "термос 2", 8, 4, 5, path=db)
    st.quality_log("serper", "термос", 10, 9, 7, path=db)
    rows = st.quality_compare(7, path=db)
    by = {r["provider"]: r for r in rows}
    assert by["firecrawl"]["queries"] == 2
    assert by["firecrawl"]["avg_cards"] == 7.0
    assert by["serper"]["priced_share"] == 1.0


def test_probe_runs_both_and_logs(monkeypatch, tmp_path):
    import asyncio

    import services.quality_probe as qp
    import services.sqlite_store as st

    db = str(tmp_path / "q.db")
    monkeypatch.setattr(st.Config, "STATE_DB_PATH", db, raising=False)
    calls = []

    async def fake_collect(provider, query, limit):
        calls.append(provider)
        m = {"firecrawl": {"cards": 6, "priced": 2, "rated": 3},
             "serper": {"cards": 10, "priced": 9, "rated": 7}}[provider]
        return m

    monkeypatch.setattr(qp, "_collect", fake_collect)
    out = asyncio.run(qp.run_probe("термос для школы"))
    assert set(out) == {"firecrawl", "serper"}
    assert calls == ["firecrawl", "serper"]
    rows = st.quality_compare(7, path=db)
    assert {r["provider"] for r in rows} == {"firecrawl", "serper"}


def test_serper_quota_alerts_via_channel(monkeypatch):
    import parser.search_channel as ch

    class _Q(Exception):
        pass

    async def fail_fc(q, *, limit, mp):
        raise RuntimeError("down")

    async def fail_serper(q, *, limit):
        from parser.serper_search import SerperQuotaError

        raise SerperQuotaError("квота serper исчерпана")

    alerted = []
    async def fake_fc(q, *, limit, mp):
        await fail_fc(q, limit=limit, mp=mp)

    async def fake_si(q, *, limit, mp=None):
        await fail_serper(q, limit=limit)

    monkeypatch.setattr(ch, "_firecrawl", fake_fc)
    monkeypatch.setattr(ch, "_serper_items", fake_si)
    monkeypatch.setattr(ch.Config, "FIRECRAWL_API_KEY", "x", raising=False)
    monkeypatch.setattr(ch.Config, "SERPER_API_KEY", "y", raising=False)
    monkeypatch.setattr(
        "services.credit_monitor.usage_snapshot", lambda: {"level": "ok"}, raising=False
    )
    monkeypatch.setattr(
        "parser.search_channel.credit_monitor.mark_provider_exhausted",
        lambda provider="firecrawl": alerted.append(provider), raising=False,
    )
    import asyncio

    from parser.firecrawl_search import ProviderQuotaError

    try:
        asyncio.run(ch.search_marketplaces("термос", limit=10))
        raised = False
    except ProviderQuotaError:
        raised = True
    assert raised and alerted == ["serper"]
