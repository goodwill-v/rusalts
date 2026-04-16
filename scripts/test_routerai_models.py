#!/usr/bin/env python3
"""
Smoke-test RouterAI connectivity and model IDs configured in .env.

Run on server (inside /opt/alt) or in docker:
  docker compose exec -T web python /app/scripts/test_routerai_models.py
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path


def _ensure_repo_root_on_path() -> None:
    root = Path(__file__).resolve().parent.parent
    s = str(root)
    if s not in sys.path:
        sys.path.insert(0, s)


def _env(name: str) -> str:
    return str(os.getenv(name, "")).strip()


def _collect_models() -> list[tuple[str, str]]:
    """
    Return list of (label, model_id) to test.
    Labels help find which setting is broken.
    """
    keys = [
        "ROUTERAI_CHAT_MODEL",
        "ROUTERAI_CHEAP_MODEL",
        "ROUTERAI_REASONING_MODEL",
        "BACKEND_MODEL_MAIN",
        "BACKEND_MODEL_HEAVY",
        "PARSER_MODEL_MAIN",
        "PARSER_MODEL_HEAVY",
        "CONTENT_MODEL_MAIN",
        "CONTENT_MODEL_HEAVY",
    ]
    out: list[tuple[str, str]] = []
    for k in keys:
        v = _env(k)
        if v:
            out.append((k, v))
    # de-dup by model id, keep first label for readability
    seen: set[str] = set()
    uniq: list[tuple[str, str]] = []
    for label, mid in out:
        if mid in seen:
            continue
        seen.add(mid)
        uniq.append((label, mid))
    return uniq


async def _test_one(*, base_url: str, api_key: str, model: str) -> tuple[bool, str]:
    from app.routerai import RouterAIError, chat_completion

    messages = [
        {"role": "system", "content": "You are a healthcheck bot. Reply with a single short word."},
        {"role": "user", "content": "ping"},
    ]
    try:
        text, usage, _raw = await chat_completion(
            base_url=base_url,
            api_key=api_key,
            model=model,
            messages=messages,
            timeout_s=25.0,
        )
        got = (text or "").strip().replace("\n", " ")[:80]
        meta = f"ok reply={got!r} tokens_in={usage.input_tokens} tokens_out={usage.output_tokens} model={usage.model!r}"
        return True, meta
    except RouterAIError as e:
        return False, str(e)
    except Exception as e:  # noqa: BLE001
        return False, f"{type(e).__name__}: {e!r}"


async def main() -> int:
    _ensure_repo_root_on_path()

    base_url = _env("ROUTERAI_BASE_URL")
    api_key = _env("ROUTERAI_API_KEY")
    if not base_url or not api_key:
        print("ERROR: ROUTERAI_BASE_URL / ROUTERAI_API_KEY not set", file=sys.stderr)
        return 2

    models = _collect_models()
    if not models:
        print("WARN: no model IDs found in env to test")
        return 0

    print(f"RouterAI base_url: {base_url}")
    print(f"Models to test: {len(models)}")
    bad = 0
    for label, model in models:
        ok, info = await _test_one(base_url=base_url, api_key=api_key, model=model)
        if ok:
            print(f"[OK]   {label} = {model} :: {info}")
        else:
            bad += 1
            print(f"[FAIL] {label} = {model} :: {info}")

    if bad:
        print(f"\nFAILED: {bad}/{len(models)} models failed", file=sys.stderr)
        return 1
    print("\nSUCCESS: all models passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))

