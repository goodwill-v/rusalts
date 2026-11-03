"""Guard болтовни: «привет!» не должен уходить в поиск маркетплейсов."""
from __future__ import annotations

from engine.query_guard import is_smalltalk


def test_greetings_are_smalltalk():
    assert is_smalltalk("Привет!")
    assert is_smalltalk("Здравствуйте")
    assert is_smalltalk("спасибо 👍")
    assert is_smalltalk("как дела")
    assert is_smalltalk("ок")


def test_product_queries_pass():
    assert not is_smalltalk("термос для школы до 2000")
    assert not is_smalltalk("найди андроид 8гб до 20000 руб")
    assert not is_smalltalk("чехол для iPhone 15")
    assert not is_smalltalk("Привет, найди чайник")  # приветствие + товар


def test_empty_and_noise():
    assert is_smalltalk("")
    assert is_smalltalk("😂😂😂")
    assert is_smalltalk("12345")
