from __future__ import annotations

import json
import os
import time
import uuid
from dataclasses import dataclass
from typing import Any

import redis.asyncio as redis


@dataclass(frozen=True)
class QueueMsg:
    id: str
    type: str
    ts_utc: str
    payload: dict[str, Any]


def _utc_now_iso() -> str:
    # lightweight; ISO "Z" is enough for logs/ids
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _env(name: str, default: str) -> str:
    return str(os.getenv(name, default)).strip()


REDIS_URL = _env("QUEUE_REDIS_URL", "redis://redis:6379/0")

STREAM_PARSER_JOBS = _env("QUEUE_STREAM_PARSER_JOBS", "alt:parser:jobs")
STREAM_CONTENT_JOBS = _env("QUEUE_STREAM_CONTENT_JOBS", "alt:content:jobs")

GROUP_PARSER = _env("QUEUE_GROUP_PARSER", "parser")
GROUP_CONTENT = _env("QUEUE_GROUP_CONTENT", "content")

CONSUMER_NAME = _env("QUEUE_CONSUMER_NAME", f"c-{uuid.uuid4().hex[:8]}")


def _to_fields(msg: QueueMsg) -> dict[str, str]:
    return {
        "id": msg.id,
        "type": msg.type,
        "ts_utc": msg.ts_utc,
        "payload": json.dumps(msg.payload, ensure_ascii=False),
    }


def _from_fields(fields: dict[bytes, bytes]) -> QueueMsg:
    d = {k.decode("utf-8"): v.decode("utf-8") for k, v in fields.items()}
    payload_raw = d.get("payload") or "{}"
    try:
        payload = json.loads(payload_raw)
    except Exception:
        payload = {"_raw": payload_raw}
    return QueueMsg(
        id=str(d.get("id") or ""),
        type=str(d.get("type") or ""),
        ts_utc=str(d.get("ts_utc") or ""),
        payload=payload if isinstance(payload, dict) else {"value": payload},
    )


async def get_redis() -> redis.Redis:
    return redis.from_url(REDIS_URL, decode_responses=False)


async def ensure_groups(r: redis.Redis) -> None:
    # id="$" means "only new messages". We use consumer groups for durability/ack.
    for stream, group in (
        (STREAM_PARSER_JOBS, GROUP_PARSER),
        (STREAM_CONTENT_JOBS, GROUP_CONTENT),
    ):
        try:
            await r.xgroup_create(stream, group, id="$", mkstream=True)
        except Exception as e:
            # BUSYGROUP is ok (already exists)
            if "BUSYGROUP" not in str(e):
                raise


async def publish_parser_job(*, payload: dict[str, Any]) -> str:
    r = await get_redis()
    await ensure_groups(r)
    msg = QueueMsg(id=uuid.uuid4().hex, type="parser.run", ts_utc=_utc_now_iso(), payload=payload)
    await r.xadd(STREAM_PARSER_JOBS, _to_fields(msg), maxlen=10_000, approximate=True)
    return msg.id


async def publish_content_job(*, payload: dict[str, Any]) -> str:
    r = await get_redis()
    await ensure_groups(r)
    msg = QueueMsg(id=uuid.uuid4().hex, type="content.from_change_package", ts_utc=_utc_now_iso(), payload=payload)
    await r.xadd(STREAM_CONTENT_JOBS, _to_fields(msg), maxlen=10_000, approximate=True)
    return msg.id


async def consume_one(
    *,
    r: redis.Redis,
    stream: str,
    group: str,
    consumer: str,
    block_ms: int = 25_000,
) -> tuple[str, QueueMsg] | None:
    res = await r.xreadgroup(group, consumer, streams={stream: ">"}, count=1, block=block_ms)
    if not res:
        return None
    # res: [(stream_name, [(msg_id, {field: value})])]
    _stream_name, msgs = res[0]
    msg_id, fields = msgs[0]
    if isinstance(msg_id, (bytes, bytearray)):
        msg_id_s = msg_id.decode("utf-8", errors="replace")
    else:
        msg_id_s = str(msg_id)
    return msg_id_s, _from_fields(fields)

