from __future__ import annotations

import asyncio
import uuid

from app.observability import json_log
from app.parser_agent import run_once
from app.queue_bus import (
    CONSUMER_NAME,
    GROUP_PARSER,
    STREAM_PARSER_JOBS,
    consume_one,
    ensure_groups,
    get_redis,
    publish_content_job,
)


async def handle_parser_run(*, payload: dict) -> None:
    limit = payload.get("limit")
    if limit is not None:
        try:
            limit = int(limit)
        except Exception:
            limit = None

    res = await run_once(limit=limit)
    await publish_content_job(
        payload={
            "ts_utc": res.get("ts_utc"),
            "changed": res.get("changed"),
            "change_package_path": res.get("change_package_path"),
            "items": res.get("items") or [],
        }
    )


async def main() -> None:
    r = await get_redis()
    await ensure_groups(r)

    consumer = f"{CONSUMER_NAME}-parser"
    json_log({"type": "worker_started", "worker": "parser", "consumer": consumer})

    while True:
        item = await consume_one(r=r, stream=STREAM_PARSER_JOBS, group=GROUP_PARSER, consumer=consumer)
        if item is None:
            continue
        msg_id, msg = item
        rid = uuid.uuid4().hex
        try:
            if msg.type == "parser.run":
                await handle_parser_run(payload=msg.payload)
            else:
                json_log({"type": "worker_unknown_msg", "worker": "parser", "request_id": rid, "msg_type": msg.type})
            await r.xack(STREAM_PARSER_JOBS, GROUP_PARSER, msg_id)
        except Exception as e:  # noqa: BLE001
            json_log(
                {
                    "type": "worker_failed",
                    "worker": "parser",
                    "request_id": rid,
                    "msg_id": msg_id,
                    "msg_type": msg.type,
                    "error": str(e),
                }
            )
            # leave unacked for retry; small backoff
            await asyncio.sleep(2.0)


if __name__ == "__main__":
    asyncio.run(main())

