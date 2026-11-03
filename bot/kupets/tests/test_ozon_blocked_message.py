"""Сообщения при блокировке Ozon."""

from bot.messages import format_results
from models.result import SearchFilters, SearchResponse


def test_format_ozon_blocked():
    resp = SearchResponse.blocked(
        SearchFilters(text="бокал хрустальный"),
        query_display="бокал хрустальный для вина",
        retry_sec=90,
    )
    text = format_results(resp)
    assert "временно недоступен" in text
    assert "не значит" in text
    assert "бокал" in text
    assert "90" in text
    assert "Ничего не нашёл" not in text


def test_format_truly_empty():
    resp = SearchResponse.empty(SearchFilters(text="абракадабра xyz"))
    text = format_results(resp)
    assert "Ничего не нашёл" in text
    assert "антибот" not in text.lower()
