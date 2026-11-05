"""Стандарт «ID без барьера» (решение Шефа 06.11): /id показывает ID пользователя.

Текст вынесен в bot.messages.id_message — чистая функция, CI-безопасно
(aiogram в CI не ставится, handlers исключаются из сбора через conftest).
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from bot.messages import id_message


def test_id_message_shows_user_id():
    text = id_message(433677408)
    assert "433677408" in text
    assert "админке АЛТ" in text
    assert "rusalts.ru" in text


def test_id_message_accepts_string_id():
    assert "abc-123" in id_message("abc-123")
