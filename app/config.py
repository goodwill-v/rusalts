"""Настройки из окружения (расширяйте по мере подключения БД, LLM, VK-подписи)."""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"
DOCUMENT_TEMPLATES_DIR = DATA_DIR / "document_templates"
UPLOADS_DIR = DATA_DIR / "uploads"
LOGS_DIR = DATA_DIR / "logs"
MONITORING_DIR = DATA_DIR / "monitoring"
CHANGES_DIR = DATA_DIR / "changes"
CONTENT_DIR = DATA_DIR / "content"
CONTENT_ITEMS_DIR = CONTENT_DIR / "items"
CONTENT_ARCHIVE_DIR = CONTENT_DIR / "archive"
CONTENT_SEQ_PATH = CONTENT_DIR / "seq.txt"
CONTENT_PUBLISHED_DIR = CONTENT_DIR / "published"
CONTENT_PUBLISHED_SITE_DIR = CONTENT_PUBLISHED_DIR / "site"
CONTENT_PUBLISHED_SITE_INDEX_PATH = CONTENT_PUBLISHED_SITE_DIR / "index.json"

PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL", "http://127.0.0.1:8000").rstrip("/")
VK_APP_ID = os.getenv("VK_APP_ID", "")
VK_SECURE_KEY = os.getenv("VK_SECURE_KEY", "")
VK_GROUP_ID = os.getenv("VK_GROUP_ID", "").strip()
VK_API_VERSION = os.getenv("VK_API_VERSION", "5.199").strip()
VK_WALL_ACCESS_TOKEN = os.getenv("VK_WALL_ACCESS_TOKEN", "").strip()

# RouterAI (OpenAI-compatible)
ROUTERAI_BASE_URL = os.getenv("ROUTERAI_BASE_URL", "").rstrip("/")
ROUTERAI_API_KEY = os.getenv("ROUTERAI_API_KEY", "")
ROUTERAI_CHAT_MODEL = os.getenv("ROUTERAI_CHAT_MODEL", "gpt-4o-mini")
ROUTERAI_CHEAP_MODEL = os.getenv("ROUTERAI_CHEAP_MODEL", "gpt-4o-mini")
ROUTERAI_REASONING_MODEL = os.getenv("ROUTERAI_REASONING_MODEL", "")
ROUTERAI_EMBEDDINGS_MODEL = os.getenv("ROUTERAI_EMBEDDINGS_MODEL", "")

# Agent-specific model routing (server-side services)
# Backend (chat / synthesis)
BACKEND_MODEL_MAIN = os.getenv("BACKEND_MODEL_MAIN", ROUTERAI_CHAT_MODEL).strip()
BACKEND_MODEL_HEAVY = os.getenv("BACKEND_MODEL_HEAVY", "").strip()

# Parser (diff/classify/KB updates) — LLM usage to be added later, currently heuristic-only
PARSER_MODEL_MAIN = os.getenv("PARSER_MODEL_MAIN", "").strip()
PARSER_MODEL_HEAVY = os.getenv("PARSER_MODEL_HEAVY", "").strip()

# Content (news/release generation)
CONTENT_MODEL_MAIN = os.getenv("CONTENT_MODEL_MAIN", ROUTERAI_CHAT_MODEL).strip()
CONTENT_MODEL_HEAVY = os.getenv("CONTENT_MODEL_HEAVY", "").strip()
CONTENT_LLM_REQUIRED = os.getenv("CONTENT_LLM_REQUIRED", "true").lower() in ("1", "true", "yes")

# Queue / inter-service messaging (Redis Streams)
QUEUE_REDIS_URL = os.getenv("QUEUE_REDIS_URL", "redis://redis:6379/0").strip()
QUEUE_STREAM_PARSER_JOBS = os.getenv("QUEUE_STREAM_PARSER_JOBS", "alt:parser:jobs").strip()
QUEUE_STREAM_CONTENT_JOBS = os.getenv("QUEUE_STREAM_CONTENT_JOBS", "alt:content:jobs").strip()
QUEUE_GROUP_PARSER = os.getenv("QUEUE_GROUP_PARSER", "parser").strip()
QUEUE_GROUP_CONTENT = os.getenv("QUEUE_GROUP_CONTENT", "content").strip()

# Веб-поиск (DuckDuckGo API) — только как fallback для вопросов в тематике АЛТ, если БЗ и whitelist пусты
WEB_SEARCH_ENABLED = os.getenv("WEB_SEARCH_ENABLED", "true").lower() in ("1", "true", "yes")

# Knowledge base + templates
KNOWLEDGE_BASE_DIR = BASE_DIR / "knowledge_base"
KB_ARTICLES_DIR = KNOWLEDGE_BASE_DIR / "articles"
KB_TRIGGERS_PATH = KNOWLEDGE_BASE_DIR / "triggers.json"
TEMPLATES_BUNDLE_PATH = BASE_DIR / "templates" / "alt_expert_ru.json"

# Для CSP: источники, которым разрешено встраивать страницу (iframe на vk.com и т.д.)
_raw_fa = os.getenv(
    "FRAME_ANCESTORS",
    "'self' https://vk.com https://*.vk.com https://vk.ru https://*.vk.ru https://m.vk.com",
)
FRAME_ANCESTORS = " ".join(s.strip() for s in _raw_fa.replace(",", " ").split() if s.strip())

_cors = os.getenv(
    "CORS_ORIGINS",
    "http://127.0.0.1:8000,http://localhost:8000,https://vk.com,https://vk.ru,https://m.vk.com",
)
CORS_ORIGINS = [o.strip() for o in _cors.split(",") if o.strip()]

MAX_UPLOAD_BYTES = int(os.getenv("MAX_UPLOAD_BYTES", str(5 * 1024 * 1024)))
DEBUG = os.getenv("DEBUG", "false").lower() in ("1", "true", "yes")

# TALK (/talk) — отдельная скрытая страница для интеграций со сторонним приложением (ботом).
# Доступ по ключу; АЛТ проксирует запросы к одному заданному URL.
TALK_KEY = os.getenv("TALK_KEY", "").strip()
# URL приложения-бота (например: http://bot:9010/talk или http://host.docker.internal:19010/talk)
TALK_RELAY_URL = os.getenv("TALK_RELAY_URL", "").strip()
# Необязательный ключ до бота (передаётся в заголовке X-App-Key)
TALK_RELAY_APP_KEY = os.getenv("TALK_RELAY_APP_KEY", "").strip()
# Токен для сторонних приложений, которые ПУШат сообщения/файлы в /talk.
# Если не задан — по умолчанию используется TALK_KEY.
TALK_APP_TOKEN = os.getenv("TALK_APP_TOKEN", "").strip()

# Content approvals via email (Chief)
CHIEF_EMAIL_TO = os.getenv("CHIEF_EMAIL_TO", "v.devops@yandex.ru").strip()

SMTP_HOST = os.getenv("SMTP_HOST", "").strip()
SMTP_PORT = int(os.getenv("SMTP_PORT", "587"))
SMTP_USER = os.getenv("SMTP_USER", "").strip()
SMTP_PASSWORD = os.getenv("SMTP_PASSWORD", "").strip()
SMTP_FROM = os.getenv("SMTP_FROM", "").strip()  # if empty, falls back to SMTP_USER
SMTP_TLS = os.getenv("SMTP_TLS", "true").lower() in ("1", "true", "yes")

IMAP_HOST = os.getenv("IMAP_HOST", "").strip()
IMAP_PORT = int(os.getenv("IMAP_PORT", "993"))
IMAP_USER = os.getenv("IMAP_USER", "").strip()
IMAP_PASSWORD = os.getenv("IMAP_PASSWORD", "").strip()
IMAP_FOLDER = os.getenv("IMAP_FOLDER", "INBOX").strip()

# Content approvals mode:
# - "web" (default): очередь на /publapprov/ (без почты)
# - "local_autoapprove": для локального MVP/демо (auto approve + publish to site)
CONTENT_APPROVAL_MODE = os.getenv("CONTENT_APPROVAL_MODE", "web").strip().lower()

def ensure_data_dirs() -> None:
    DOCUMENT_TEMPLATES_DIR.mkdir(parents=True, exist_ok=True)
    UPLOADS_DIR.mkdir(parents=True, exist_ok=True)
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    MONITORING_DIR.mkdir(parents=True, exist_ok=True)
    CHANGES_DIR.mkdir(parents=True, exist_ok=True)
    CONTENT_ITEMS_DIR.mkdir(parents=True, exist_ok=True)
    CONTENT_ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
    CONTENT_PUBLISHED_SITE_DIR.mkdir(parents=True, exist_ok=True)
