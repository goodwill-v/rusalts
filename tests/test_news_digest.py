"""Тесты автоконтента v2: дедуп фактов, скоринг тем, лид 180–220, сборка дайджеста.

Без сети и БД: RSS-fetch и RouterAI подменяются моками, каталоги — tmp_path.
"""

from __future__ import annotations

import asyncio
import json
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import config
from app.news_dedup import (
    DigestState,
    FactDedup,
    add_to_max_pool,
    fact_hash,
    max_weekly_due,
    shingles,
    similarity,
)
from app.news_feeds import NewsItem, canonical_url, keyword_hit
from app.news_digest import (
    DigestBlock,
    assemble_site_text,
    assemble_vk_text,
    extract_blocks,
    fit_lead,
    source_lines,
    validate_lead,
)
from app.news_scoring import (
    ScoredCandidate,
    is_max_news,
    rank_candidates,
    score_candidate,
    select_for_digest,
)


# ---------------------------------------------------------------------------
# Дедуп (ТЗ 2.3)
# ---------------------------------------------------------------------------


def test_similarity_catches_paraphrase() -> None:
    a = "Сбер представил ИИ-модель для подбора вакансий, точность выросла на 12%"
    b = "Сбер представил ИИ-модель для подбора вакансий: точность подросла на 12%"
    c = "Роскомнадзор оштрафовал компанию за утечку персональных данных клиентов"
    assert similarity(a, b) >= 0.55
    assert similarity(a, c) < 0.2


def test_fact_hash_stable_and_sensitive() -> None:
    h1 = fact_hash("Заголовок", "Текст про ИИ")
    assert h1 == fact_hash("Заголовок", "Текст про ИИ")
    assert h1 != fact_hash("Другой заголовок", "Текст про ИИ")


def _now() -> datetime:
    return datetime(2026, 10, 26, 12, 0, tzinfo=timezone.utc)


def test_dedup_by_url_with_utm() -> None:
    d = FactDedup(published=[], state=DigestState(), now=_now())
    d.remember(title="Сбер представил ИИ", summary="Модель для бизнеса", url="https://habr.com/p/1/?utm_source=x")
    v = d.check(title="Сбер представил ИИ", summary="Модель для бизнеса", url="https://habr.com/p/1/")
    assert v.duplicate and v.reason == "url"


def test_dedup_by_fact_similarity() -> None:
    d = FactDedup(published=[], state=DigestState(), now=_now())
    d.remember(
        title="Сбер представил ИИ-модель для подбора вакансий",
        summary="Точность выросла на 12%",
        url="https://sber.pro/a/1",
    )
    v = d.check(
        title="Сбер представил ИИ-модель для подбора вакансий, точность выросла на 12%",
        summary="",
        url="https://vc.ru/p/999",
    )
    assert v.duplicate and v.reason in ("similar", "fact_hash")


def test_dedup_against_published_window() -> None:
    old_ts = (_now() - timedelta(days=30)).isoformat()
    published = [{"title": "Старая новость про ИИ", "excerpt": "", "url": "https://x.ru/1", "published_at_utc": old_ts}]
    d = FactDedup(published=published, state=DigestState(), now=_now())
    v = d.check(title="Старая новость про ИИ", summary="", url="https://y.ru/2")
    # Старше окна сравнения (14 дней) — повтором не считается
    assert not v.duplicate


# ---------------------------------------------------------------------------
# Скоринг тем и значимости (ТЗ 2.4–2.5)
# ---------------------------------------------------------------------------


def _item(title: str, *, origin: str = "ru", topics: list[str] | None = None, url: str = "") -> NewsItem:
    return NewsItem(
        uid=f"news-{abs(hash(title)) % 10**8}",
        source_id="test",
        source_title="Тест",
        title=title,
        url=url or f"https://example.com/{abs(hash(title)) % 1000}",
        summary="",
        published="",
        lang="ru",
        origin=origin,
        fetched_at_utc="2026-10-26T00:00:00Z",
        topics=topics or [],
    )


@pytest.fixture(scope="module")
def topic_cfg() -> dict:
    from app.news_feeds import load_topic_config

    cfg = load_topic_config()
    assert cfg, "parser/news_sources.json должен содержать topics"
    return cfg


def test_ru_priority_over_world(topic_cfg: dict) -> None:
    ru = score_candidate(_item("Сбер представил новую ИИ-модель"), topic_cfg)
    world = score_candidate(_item("Сбер представил новую ИИ-модель", origin="world"), topic_cfg)
    assert ru.score > world.score
    assert ru.topics and ru.topics[0] == "ai_dev"


def test_max_news_detected(topic_cfg: dict) -> None:
    it = _item("МАКС запустил боты для бизнеса", topics=["messengers"])
    assert is_max_news(it)
    other = _item("Максимум пользы от автоматизации", topics=["automation"])
    assert not is_max_news(other)


def test_high_significance_legal(topic_cfg: dict) -> None:
    it = _item(
        "Минцифры внесло законопроект о штрафах за утечки персональных данных для бизнеса",
        topics=["legal"],
    )
    s = score_candidate(it, topic_cfg)
    assert s.high_significance
    assert "legal_business" in s.reasons


def test_select_for_digest_excludes_max_and_caps_topics(topic_cfg: dict) -> None:
    items = [
        _item("Сбер представил ИИ-модель", topics=["ai_dev"]),
        _item("Яндекс выпустил нейросеть", topics=["ai_dev"]),
        _item("ВК представил ИИ-помощника", topics=["ai_dev"]),
        _item("МАКС обновил API ботов", topics=["messengers"]),
        _item("Закон о персональных данных", topics=["legal"]),
    ]
    scored = [score_candidate(i, topic_cfg) for i in items]
    assert any(s.is_max for s in scored)
    ranked = rank_candidates(scored)
    sel = select_for_digest(ranked, min_news=3, max_news=5, max_per_topic=2)
    assert sel and len(sel) <= 5
    assert all(not s.is_max for s in sel), "МАКС-новости не должны попадать в ежедневный дайджест"
    primaries = [s.topics[0] for s in sel]
    assert primaries.count("ai_dev") <= 2


# ---------------------------------------------------------------------------
# Лид 180–220 и сборка выпуска (ТЗ 2.4, 2.6)
# ---------------------------------------------------------------------------


def test_validate_lead_bounds() -> None:
    assert validate_lead("а" * 180)
    assert validate_lead("а" * 220)
    assert not validate_lead("а" * 179)
    assert not validate_lead("а" * 221)
    assert not validate_lead("   ")


def test_fit_lead_long_text() -> None:
    long = "Сбер представил ИИ-модель. " * 30
    lead = fit_lead(long)
    assert 180 <= len(lead) <= 220
    assert lead.endswith("…")


def test_fit_lead_short_text_padded() -> None:
    lead = fit_lead("Короткая новость.")
    assert 180 <= len(lead) <= 220


def test_assemble_site_text_structure() -> None:
    lead = "л" * 190
    blocks = [
        DigestBlock(headline="Первая новость", body="Суть. Подробности.", business="Экономия 30%.", url="https://habr.com/p/1"),
        DigestBlock(headline="Вторая новость", body="Суть вторая.", business="Рост скорости.", url="https://vc.ru/2"),
    ]
    txt = assemble_site_text(lead, blocks, [b.url for b in blocks])
    paras = txt.split("\n\n")
    assert paras[0] == lead, "лид — первый абзац (анонс на главной, первые ~200 знаков)"
    assert "1. Первая новость" in paras[1] and "Что для бизнеса: Экономия 30%." in paras[1]
    assert "2. Вторая новость" in paras[2]
    assert paras[-1].splitlines() == ["habr.com — https://habr.com/p/1", "vc.ru — https://vc.ru/2"], (
        "хвост «домен — url» превращается фронтендом в ссылки target=_blank"
    )
    vk = assemble_vk_text(lead, blocks)
    assert "rusalts.ru/news" in vk and "#АЛТ" in vk


def test_extract_blocks_by_id_and_fallback() -> None:
    cand = [SimpleNamespace(uid="news-1", item=SimpleNamespace(title="T1", summary="S1", url="https://a/1"))]
    raw = {"blocks": [{"id": "news-1", "headline": "", "body": "", "business": "B"}]}
    blocks = extract_blocks(raw, cand, max_blocks=5)  # type: ignore[arg-type]
    assert blocks and blocks[0].headline == "T1" and blocks[0].business == "B" and blocks[0].url == "https://a/1"


# ---------------------------------------------------------------------------
# Правило МАКС: еженедельный релиз (ТЗ 2.3)
# ---------------------------------------------------------------------------


def test_max_weekly_due_friday_only() -> None:
    friday = datetime(2026, 10, 30, 12, 0, tzinfo=timezone.utc)  # пятница
    monday = datetime(2026, 10, 26, 12, 0, tzinfo=timezone.utc)  # понедельник
    st = DigestState(max_pool=[{"uid": "x", "title": "t", "url": "https://max/1"}])
    assert max_weekly_due(st, now=friday)
    assert not max_weekly_due(st, now=monday)
    st.last_max_release_utc = friday.replace(hour=6).isoformat()
    assert not max_weekly_due(st, now=friday), "не чаще раза в день"


def test_add_to_max_pool_dedups() -> None:
    st = DigestState()
    a = _item("МАКС обновил API", topics=["messengers"], url="https://dev.max.ru/a")
    assert add_to_max_pool(st, [a]) == 1
    assert add_to_max_pool(st, [a]) == 0, "повторный запуск не дублирует пул"


# ---------------------------------------------------------------------------
# URL-канонизация и ключевые слова
# ---------------------------------------------------------------------------


def test_canonical_url_strips_tracking() -> None:
    assert canonical_url("https://www.habr.com/ru/p/1/?utm_source=rss&utm_medium=feed#comments") == "https://habr.com/ru/p/1"
    assert canonical_url("http://VC.ru/x?from=tg") == "https://vc.ru/x"


def test_keyword_hit_word_boundary() -> None:
    assert keyword_hit("в России вырос ИИ-рынок", "ии")
    assert not keyword_hit("России и итальянский рынок", "ии")


# ---------------------------------------------------------------------------
# Интеграционный тест конвейера (моки, без сети)
# ---------------------------------------------------------------------------

_LEAD = (
    "ИИ перестаёт быть экспериментом и начинает экономить деньги: за сутки в отрасли сразу три "
    "обновления, которые меняют правила для бизнеса — от новых моделей до штрафов за данные. "
    "Разбираем, что применять уже сегодня."
)
assert 180 <= len(_LEAD) <= 220


def _usage() -> SimpleNamespace:
    return SimpleNamespace(model="test-model", input_tokens=1, output_tokens=1, cost_usd=0.0)


def _fake_candidates() -> list[NewsItem]:
    return [
        _item("Сбер представил ИИ-модель для подбора персонала", topics=["ai_dev"], url="https://sber.pro/a/1"),
        _item("Яндекс выпустил нейросеть для бизнеса", topics=["ai_dev"], url="https://habr.com/p/2"),
        _item("ВК открыл ИИ-платформу для чат-ботов", topics=["ai_dev", "automation"], url="https://vc.ru/p/3"),
        _item("МАКС добавил оплату в ботах", topics=["messengers"], url="https://dev.max.ru/x"),
        _item(
            "Минцифры внесло законопроект о штрафах за утечки персональных данных бизнеса",
            topics=["legal"],
            url="https://rkn.gov.ru/z",
        ),
    ]


@pytest.fixture()
def tmp_content_dirs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Каталоги контента → tmp, чтобы не задеть боевые data/."""
    data = tmp_path / "data"
    for name in ("uploads", "logs", "monitoring", "changes"):
        (data / name).mkdir(parents=True)
    content = data / "content"
    for name in ("items", "archive"):
        (content / name).mkdir(parents=True)
    (content / "published" / "site").mkdir(parents=True)
    monkeypatch.setattr(config, "DATA_DIR", data)
    monkeypatch.setattr(config, "MONITORING_DIR", data / "monitoring")
    monkeypatch.setattr(config, "CHANGES_DIR", data / "changes")
    monkeypatch.setattr(config, "CONTENT_DIR", content)
    monkeypatch.setattr(config, "CONTENT_ITEMS_DIR", content / "items")
    monkeypatch.setattr(config, "CONTENT_ARCHIVE_DIR", content / "archive")
    monkeypatch.setattr(config, "CONTENT_SEQ_PATH", content / "seq.txt")
    monkeypatch.setattr(config, "CONTENT_PUBLISHED_DIR", content / "published")
    monkeypatch.setattr(config, "CONTENT_PUBLISHED_SITE_DIR", content / "published" / "site")
    monkeypatch.setattr(config, "CONTENT_PUBLISHED_SITE_INDEX_PATH", content / "published" / "site" / "index.json")
    return data


def test_pipeline_creates_pending_items_without_publish(tmp_content_dirs: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import app.news_digest as nd

    calls: list[str] = []

    async def fake_fetch_all_feeds():
        calls.append("fetch")
        return _fake_candidates()

    async def fake_chat(**kwargs):
        messages = kwargs["messages"]
        user = messages[-1]["content"]
        if "лид-анонс" in user:
            return json.dumps({"lead": _LEAD}, ensure_ascii=False), _usage(), {}
        m = re.search(r"Входные данные:\n(\[.*\])\s*$", user, re.S)
        assert m, f"промпт без входных данных: {user[:120]}"
        rows = json.loads(m.group(1))
        blocks = [
            {"id": r["id"], "headline": f"Что случилось: {r['title'][:60]}", "body": "Суть в двух предложениях. Ещё детали.", "business": "Пересмотрите свои процессы."}
            for r in rows
        ]
        return json.dumps({"blocks": blocks}, ensure_ascii=False), _usage(), {}

    monkeypatch.setattr(nd, "fetch_all_feeds", fake_fetch_all_feeds)
    monkeypatch.setattr(nd, "chat_completion", fake_chat)

    res = asyncio.run(nd.run_news_digest_pipeline({"force": True, "auto_publish": False}))

    assert res.ok, res.reason
    assert res.digest_publication_id, "дайджест должен быть создан"
    assert res.standalone_publication_ids, "значимая новость (законопроект) должна выйти отдельным выпуском"

    items_dir = tmp_content_dirs / "content" / "items"
    files = sorted(items_dir.glob("*.json"))
    assert len(files) == 2, "дайджест + 1 отдельный выпуск"
    for f in files:
        data = json.loads(f.read_text(encoding="utf-8"))
        assert data["status"] == "pending", "без auto_publish ничего не публикуем"
        lead = data["site_text"].split("\n\n")[0]
        assert 180 <= len(lead) <= 220, f"лид вне диапазона: {len(lead)}"
        assert "Что для бизнеса:" in data["site_text"]
        assert data["site_text"].rstrip().splitlines()[-1].split(" — ")[0] in ("sber.pro", "habr.com", "vc.ru", "rkn.gov.ru")

    # Публикации нет: индекс и markdown сайта не создаются
    site_dir = tmp_content_dirs / "content" / "published" / "site"
    assert not (site_dir / "index.json").exists()
    assert not list((site_dir).glob("*.md"))

    # Состояние: МАКС в пуле (не в дайджесте), факты запомнены
    state = json.loads((tmp_content_dirs / "content" / "news_digest_state.json").read_text(encoding="utf-8"))
    assert len(state["max_pool"]) == 1
    assert state["max_pool"][0]["title"].startswith("МАКС")
    assert len(state["seen_facts"]) >= 4
    assert state["last_digest_utc"]

    digest_item = json.loads((items_dir / f"{res.digest_publication_id}.json").read_text(encoding="utf-8"))
    assert digest_item["title"].startswith("Обзор:")
    assert "МАКС" not in digest_item["site_text"], "правило МАКС: в ежедневный дайджест не попадает"

    # Повторный запуск без force — cooldown
    res2 = asyncio.run(nd.run_news_digest_pipeline({"auto_publish": False}))
    assert res2.reason == "cooldown" and not res2.ok
