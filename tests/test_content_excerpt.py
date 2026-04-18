from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.content_excerpt import excerpt_for_list, first_paragraph_one_line, title_fallback_from_site_text


def test_first_paragraph_one_line_multiline_block() -> None:
    raw = "Первая строка.\nВторая в том же абзаце.\n\nДругой абзац."
    assert first_paragraph_one_line(raw) == "Первая строка."


def test_excerpt_for_list_truncates() -> None:
    long = "слово " * 80
    out = excerpt_for_list(long, max_chars=50)
    assert len(out) <= 51
    assert out.endswith("…")


def test_title_fallback_from_site_text() -> None:
    site = "Короткое начало важной новости.\n\nДальше текст."
    assert title_fallback_from_site_text(site) == "Короткое начало важной новости."
