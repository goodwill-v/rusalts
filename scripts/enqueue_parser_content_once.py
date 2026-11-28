#!/usr/bin/env python3
"""Enqueue one parser.run job; workers handle parser then content (see docs)."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path


def _ensure_repo_root_on_path() -> None:
    root = Path(__file__).resolve().parent.parent
    s = str(root)
    if s not in sys.path:
        sys.path.insert(0, s)


# Дни автоконтента (решение Шефа, 21.11): парсинг/обработка/публикация —
# только Пн/Ср/Пт (3 раза в неделю). Двойная защита: график в
# alt-pipeline.timer (Пн/Ср/Пт) + этот guard в скрипте.
PUBLISH_WEEKDAYS = {0, 2, 4}  # Mon, Wed, Fri (0 = понедельник)


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Put a single parser.run message on Redis Streams. "
            "parser-worker runs monitoring/KB update and publishes content.from_change_package; "
            "content-worker generates text and may publish per CONTENT_APPROVAL_MODE."
        )
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        metavar="N",
        help="Optional: only first N sources (debug). Omit for full run.",
    )
    args = parser.parse_args()

    from datetime import datetime, timezone

    weekday = datetime.now(timezone.utc).weekday()
    if weekday not in PUBLISH_WEEKDAYS:
        print(
            f"Сегодня не день автоконтента (Пн/Ср/Пт). Пропуск (weekday={weekday}).",
            file=sys.stderr,
        )
        return 0

    payload: dict = {}
    if args.limit is not None:
        payload["limit"] = args.limit

    _ensure_repo_root_on_path()
    from app.queue_bus import publish_parser_job

    job_id = asyncio.run(publish_parser_job(payload=payload))
    print(json.dumps({"ok": True, "job_id": job_id, "stream": "parser.run"}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("\nInterrupted.", file=sys.stderr)
        raise SystemExit(130)
