"""Проверка живости Composer API при старте."""

from __future__ import annotations

from config import Config
from parser.composer import ComposerClient
from utils import get_logger

logger = get_logger("parser.healthcheck")


async def run_healthcheck() -> bool:
    if Config.DRY_RUN:
        logger.info("healthcheck_skip_dry_run")
        return True
    if not Config.ENABLE_COMPOSER:
        # Composer отключён (антибот 403) — сетевой healthcheck не имеет смысла
        logger.info("healthcheck_skip_composer_disabled")
        return True

    client = ComposerClient()
    try:
        payload = await client.fetch_page(
            "/search/?text=%D1%81%D0%BC%D0%B0%D1%80%D1%82%D1%84%D0%BE%D0%BD&from_global=true"
        )
        if not payload or not payload.get("widgetStates"):
            logger.warning("healthcheck_fail", reason="empty_payload")
            return False
        ok = any(
            "tileGrid" in k or "searchResults" in k
            for k in payload["widgetStates"]
        )
        if ok:
            logger.info("healthcheck_ok")
        else:
            logger.warning("healthcheck_fail", reason="no_grid_widget")
        return ok
    except Exception as exc:
        logger.warning("healthcheck_error", error=str(exc))
        return False
    finally:
        await client.aclose()
