"""Сообщения, когда поиск по фото недоступен."""

from config import Config

PHOTO_STUB_TEXT = "📷 Поиск по фото — скоро!"


def photo_stub_message() -> str:
    if not Config.ROUTERAI_API_KEY:
        return (
            "📷 Поиск по фото сейчас недоступен: нет ключа RouterAI.\n"
            "Напишите запрос текстом."
        )
    if Config.DRY_RUN:
        return (
            "📷 Поиск по фото отключён в DRY_RUN.\n"
            "Напишите запрос текстом."
        )
    if not Config.VISION_ENABLED:
        return PHOTO_STUB_TEXT
    return (
        "📷 Не удалось разобрать фото. Попробуйте ещё раз "
        "или напишите запрос текстом."
    )
