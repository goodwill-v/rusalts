"""Sandbox-specific config overrides for ЮKassa. Merged with config.py at sandbox startup.
Keep in sync with the real config.py when deploying."""

from __future__ import annotations

import os

from dotenv import load_dotenv

# Re-read .env.sandbox — not needed, load_dotenv already called in config.py
# But we need YOOKASSA_* keys to be picked up from .env.sandbox

YOOKASSA_SHOP_ID = os.getenv("YOOKASSA_SHOP_ID", "").strip()
YOOKASSA_SECRET_KEY = os.getenv("YOOKASSA_SECRET_KEY", "").strip()
YOOKASSA_RETURN_URL = os.getenv(
    "PUBLIC_BASE_URL", "http://127.0.0.1:8002"
).rstrip("/") + "/techologis/"

# Sandbox mode flag: if keys are test, operate in demo mode
IS_SANDBOX = YOOKASSA_SHOP_ID in ("test_shop_id", "", "test")