from __future__ import annotations

import argparse
import asyncio
import json

from app.parser_agent.run import run_once


def main() -> int:
    p = argparse.ArgumentParser(description="ALT Parser agent (MVP): fetch sources, update KB, emit change_package.json")
    p.add_argument("--limit", type=int, default=None, help="Limit number of sources (debug only)")
    args = p.parse_args()

    res = asyncio.run(run_once(limit=args.limit))
    print(json.dumps(res, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

