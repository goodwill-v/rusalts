"""Vision-модуль: поиск по фото через VLM."""

from vision.recognizer import PhotoRecognition, PhotoRecognizer, parse_vision_response
from vision.stub import PHOTO_STUB_TEXT, photo_stub_message

__all__ = [
    "PHOTO_STUB_TEXT",
    "PhotoRecognition",
    "PhotoRecognizer",
    "parse_vision_response",
    "photo_stub_message",
]
