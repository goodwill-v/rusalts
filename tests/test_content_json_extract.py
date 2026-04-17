from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest

from app.workers.content_worker import _extract_json_object


def test_extract_json_direct() -> None:
    obj = _extract_json_object('{"title":"t","site_text_md":"a","vk_text":"b","internal_note":"n"}')
    assert obj["title"] == "t"


def test_extract_json_fenced() -> None:
    raw = """Вот пояснение (не должно мешать)
```json
{"title":"t","site_text_md":"a","vk_text":"b","internal_note":"n"}
```
ещё хвост
"""
    obj = _extract_json_object(raw)
    assert obj["vk_text"] == "b"


def test_extract_json_brace_window() -> None:
    raw = "preface {\"title\":\"t\",\"site_text_md\":\"a\",\"vk_text\":\"b\"} tail"
    obj = _extract_json_object(raw)
    assert obj["site_text_md"] == "a"


def test_extract_json_raises_on_empty() -> None:
    with pytest.raises(Exception):
        _extract_json_object("")

