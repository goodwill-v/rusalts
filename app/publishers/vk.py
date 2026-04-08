from __future__ import annotations

from datetime import datetime, timezone

import httpx

from app import config
from app.content_store import ContentItem


def _now_utc_iso_z() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


async def publish_to_vk(item: ContentItem) -> tuple[str, int, str]:
    """
    Posts to VK wall of a group. Returns (published_at_utc, post_id, post_url).
    Requires VK_GROUP_ID, VK_WALL_ACCESS_TOKEN.
    """
    if not config.VK_GROUP_ID or not config.VK_WALL_ACCESS_TOKEN:
        raise RuntimeError("VK не настроен: заполните VK_GROUP_ID и VK_WALL_ACCESS_TOKEN в .env")

    group_id = int(config.VK_GROUP_ID)
    owner_id = -group_id
    message = item.vk_text.strip()

    url = "https://api.vk.com/method/wall.post"
    params = {
        "owner_id": owner_id,
        "from_group": 1,
        "message": message,
        "v": config.VK_API_VERSION or "5.199",
        "access_token": config.VK_WALL_ACCESS_TOKEN,
    }

    async with httpx.AsyncClient(timeout=20) as client:
        r = await client.post(url, data=params)
        r.raise_for_status()
        data = r.json()

    if "error" in data:
        err = data["error"]
        raise RuntimeError(f"VK error {err.get('error_code')}: {err.get('error_msg')}")

    resp = data.get("response") or {}
    post_id = int(resp.get("post_id"))
    post_url = f"https://vk.com/wall-{group_id}_{post_id}"
    return _now_utc_iso_z(), post_id, post_url

