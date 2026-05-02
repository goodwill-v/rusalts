from __future__ import annotations

import asyncio
import json
import os
import tempfile
from pathlib import Path
from typing import Any

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile, status
from starlette.datastructures import UploadFile as StarletteUploadFile

app = FastAPI(title="OpenClaw Talk Relay", version="0.1.0")

EXPECTED_APP_KEY = os.getenv("TALK_RELAY_APP_KEY", "").strip()
OKO_ADMIN_KEY = os.getenv("TALK_OKO_ADMIN_KEY", "").strip()
RELAY_SESSION_ID = os.getenv("OPENCLAW_TALK_SESSION_ID", "talk-relay").strip() or "talk-relay"
OPENCLAW_BIN = os.getenv("OPENCLAW_BIN", "/usr/bin/openclaw").strip() or "/usr/bin/openclaw"
TMP_DIR = Path(os.getenv("OPENCLAW_TALK_TMP_DIR", "/tmp/openclaw-talk-relay")).resolve()
TMP_DIR.mkdir(parents=True, exist_ok=True)


def _check_app_key(request: Request) -> None:
    if not EXPECTED_APP_KEY:
        raise HTTPException(status_code=500, detail="TALK_RELAY_APP_KEY is not configured on relay")
    provided = (request.headers.get("x-app-key") or "").strip()
    if not provided or provided != EXPECTED_APP_KEY:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid X-App-Key")


def _check_oko_admin(request: Request) -> None:
    _check_app_key(request)
    if not OKO_ADMIN_KEY:
        raise HTTPException(status_code=503, detail="TALK_OKO_ADMIN_KEY is not configured on relay host")
    got = (request.headers.get("x-oko-admin") or "").strip()
    if not got or got != OKO_ADMIN_KEY:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid X-Oko-Admin")


_UNIT_GATEWAY = "openclaw-gateway.service"


async def _systemctl(*args: str) -> tuple[int, str, str]:
    proc = await asyncio.create_subprocess_exec(
        "/usr/bin/systemctl",
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout_b, stderr_b = await asyncio.wait_for(proc.communicate(), timeout=120)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.communicate()
        return 124, "", "systemctl timeout"
    return (
        proc.returncode or 0,
        stdout_b.decode("utf-8", errors="replace").strip(),
        stderr_b.decode("utf-8", errors="replace").strip(),
    )


def _extract_reply_text(obj: Any) -> str:
    if isinstance(obj, str):
        return obj.strip()
    if isinstance(obj, dict):
        # Common OpenClaw agent JSON shape
        meta = obj.get("result") if isinstance(obj.get("result"), dict) else None
        if isinstance(obj.get("finalAssistantVisibleText"), str) and obj["finalAssistantVisibleText"].strip():
            return obj["finalAssistantVisibleText"].strip()
        if isinstance(obj.get("finalAssistantRawText"), str) and obj["finalAssistantRawText"].strip():
            return obj["finalAssistantRawText"].strip()
        if isinstance(meta, dict):
            payloads = meta.get("payloads")
            if isinstance(payloads, list) and payloads:
                first = payloads[0]
                if isinstance(first, dict) and isinstance(first.get("text"), str) and first["text"].strip():
                    return first["text"].strip()
        for key in ("reply", "output_text", "message", "text", "final"):
            val = obj.get(key)
            if isinstance(val, str) and val.strip():
                return val.strip()
        for key in ("data", "result", "response", "payload"):
            val = obj.get(key)
            extracted = _extract_reply_text(val)
            if extracted:
                return extracted
    if isinstance(obj, list):
        for item in obj:
            extracted = _extract_reply_text(item)
            if extracted:
                return extracted
    return ""


def _try_parse_json_from_mixed_output(raw: str) -> dict[str, Any] | None:
    start = raw.find("{")
    end = raw.rfind("}")
    if start != -1 and end != -1 and end > start:
        blob = raw[start : end + 1].strip()
        try:
            parsed = json.loads(blob)
            if isinstance(parsed, dict):
                return parsed
        except Exception:
            pass

    for line in reversed(raw.splitlines()):
        line = line.strip()
        if not line:
            continue
        if not (line.startswith("{") and line.endswith("}")):
            continue
        try:
            parsed = json.loads(line)
        except Exception:
            continue
        if isinstance(parsed, dict):
            return parsed
    return None


async def _run_openclaw(message: str) -> tuple[int, str, str]:
    env = os.environ.copy()
    env["PATH"] = "/usr/bin:/usr/local/bin:/bin:" + env.get("PATH", "")
    proc = await asyncio.create_subprocess_exec(
        OPENCLAW_BIN,
        "agent",
        "--session-id",
        RELAY_SESSION_ID,
        "--message",
        message,
        "--json",
        "--timeout",
        "180",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=env,
    )
    try:
        # LLM turns can take ~40-70s on real workloads; keep relay comfortably
        # below the backend proxy timeout.
        stdout_b, stderr_b = await asyncio.wait_for(proc.communicate(), timeout=90)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.communicate()
        return 124, "", "openclaw timeout after 90s"
    return proc.returncode, stdout_b.decode("utf-8", errors="replace"), stderr_b.decode("utf-8", errors="replace")


def _build_prompt(text: str, file_note: str) -> str:
    base = text.strip()
    if file_note:
        return (
            f"{base}\n\n{file_note}\n\n"
            "Use the attached file content above (if present) to answer. If the content looks truncated, say so."
        ).strip()
    return base


@app.get("/health")
async def health() -> dict[str, Any]:
    return {"ok": True, "service": "openclaw-talk-relay"}


@app.get("/oko/gateway/status")
async def oko_gateway_status(request: Request) -> dict[str, Any]:
    """Статус systemd openclaw-gateway (только чтение, нужен X-App-Key — как у /talk)."""
    _check_app_key(request)
    code, out, err = await _systemctl("is-active", _UNIT_GATEWAY)
    raw = (out or err or "").strip() or "unknown"
    state = raw.splitlines()[0].strip() if raw else "unknown"
    return {"ok": True, "unit": _UNIT_GATEWAY, "active": state, "exit_code": code}


@app.post("/oko/gateway/stop")
async def oko_gateway_stop(request: Request) -> dict[str, Any]:
    """Остановить OpenClaw Gateway на хосте (Telegram/дашборд перестанут работать до start)."""
    _check_oko_admin(request)
    code, out, err = await _systemctl("stop", _UNIT_GATEWAY)
    if code != 0:
        raise HTTPException(
            status_code=502,
            detail={"systemctl": "stop", "exit_code": code, "stdout": out[-2000:], "stderr": err[-2000:]},
        )
    return {"ok": True, "stopped": _UNIT_GATEWAY, "message": out or "stopped"}


@app.post("/oko/gateway/start")
async def oko_gateway_start(request: Request) -> dict[str, Any]:
    """Запустить OpenClaw Gateway на хосте."""
    _check_oko_admin(request)
    code, out, err = await _systemctl("start", _UNIT_GATEWAY)
    if code != 0:
        raise HTTPException(
            status_code=502,
            detail={"systemctl": "start", "exit_code": code, "stdout": out[-2000:], "stderr": err[-2000:]},
        )
    return {"ok": True, "started": _UNIT_GATEWAY, "message": out or "started"}


@app.post("/talk")
async def talk(request: Request) -> dict[str, Any]:
    _check_app_key(request)
    ct = (request.headers.get("content-type") or "").lower()

    text = ""
    file_note = ""

    if "application/json" in ct:
        payload = await request.json()
        text = str((payload or {}).get("text") or "").strip()
    else:
        form = await request.form()
        text = str(form.get("text") or "").strip()
        uploaded = form.get("file")
        if isinstance(uploaded, (UploadFile, StarletteUploadFile)):
            content = await uploaded.read()
            filename = getattr(uploaded, "filename", None) or "upload.bin"
            content_type = getattr(uploaded, "content_type", None) or "application/octet-stream"
            suffix = Path(filename).suffix
            with tempfile.NamedTemporaryFile(prefix="talk_", suffix=suffix, dir=TMP_DIR, delete=False) as tmp:
                tmp.write(content)
                tmp_path = tmp.name
            file_note = (
                f"User attached file: name={filename}, "
                f"content_type={content_type}, "
                f"size={len(content)} bytes, saved_to={tmp_path}."
            )
            decoded = content.decode("utf-8", errors="replace")
            if decoded.strip():
                file_note += "\n\nAttached file content (UTF-8 decoded, may be truncated):\n" + decoded[:8000]

    if not (text or file_note):
        raise HTTPException(status_code=400, detail="text or file is required")

    prompt = _build_prompt(text, file_note)
    code, stdout, stderr = await _run_openclaw(prompt)

    if code != 0:
        detail = (stderr or stdout).strip()
        raise HTTPException(status_code=502, detail={"relay_error": "openclaw_failed", "details": detail[-4000:]})

    parsed = _try_parse_json_from_mixed_output(stdout)
    if parsed:
        reply = _extract_reply_text(parsed)
        if reply:
            return {"reply": reply}

    plain = (stdout or stderr).strip()
    if plain:
        return {"reply": plain[-4000:]}

    return {"reply": "Готово, но пустой ответ от OpenClaw."}

