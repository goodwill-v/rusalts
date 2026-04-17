from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.model_routing import backend_choice, is_alt_project_topic


def test_backend_choice_zero_kb_uses_main() -> None:
    c = backend_choice(text="просто текст про мессенджер макс", kb_hits_count=0, main="m-main", heavy="m-heavy")
    assert c.model == "m-main"
    assert c.reason == "main_default"


def test_backend_choice_one_kb_hit_escalates_when_heavy_set() -> None:
    c = backend_choice(text="просто текст про мессенджер макс", kb_hits_count=1, main="m-main", heavy="m-heavy")
    assert c.model == "m-heavy"
    assert c.reason == "backend_low_kb_hits"


def test_backend_choice_legal_escalates() -> None:
    c = backend_choice(text="что такое 152-фз", kb_hits_count=0, main="m-main", heavy="m-heavy")
    assert c.model == "m-heavy"
    assert c.reason == "backend_legal_query"


def test_is_alt_project_topic_max() -> None:
    assert is_alt_project_topic("Как зарегистрироваться в MAX?")


def test_is_alt_project_topic_unrelated() -> None:
    assert not is_alt_project_topic("Напиши рецепт борща")
