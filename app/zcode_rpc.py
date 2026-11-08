"""Клиент WS-канала zcode-server (протокол VSCode channel-RPC).

Этап 0 ТЗ ВЭБ (`/opt/alt_docs/ALT_ZAI_devops/TZ_VEB_zcode_web.md`): штатный доступ
к сессиям агентов — WebSocket ingress zcode-server'а (127.0.0.1:42265, сценарий A).
Формат фреймов снят с runtime-бандла сервера v3.17.1 и проверен живьём (read-only):

- фрейм сокета: [type u8][id u32BE][ack u32BE][len u32BE][payload]; type 1 = Regular;
- RPC-сообщение: serialize([rpcType, rpcId, channelName, name]) + serialize(args);
  preset-байты: 0=undefined, 1=string, 2=buffer, 3=vsbuffer, 4=array, 5=object(JSON),
  6=int; длины — VLQ (7 бит, старший бит = продолжение);
- при подключении сервер присылает Initialize [200];
- канал "zcode-agent": listSessions, getSessionLifecycle, helloConversationV4 →
  initializeConversationV4 → conversationRowsRangeV4 (v4-транскрипт), sendPrompt,
  createSession, closeSession.

СТОП-условие ТЗ §1.4: этот клиент ТОЛЬКО читает/пишет сообщения сессий через
штатный ingress. Он не трогает процессы/конфиги/службы zcode-server.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import struct
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

import httpx
import websockets

logger = logging.getLogger("zcode_rpc")

# ── Конфигурация из окружения ────────────────────────────────────────────────

UPSTREAM_WS_URL = os.getenv("ZCODE_UPSTREAM_WS_URL", "ws://127.0.0.1:42265").rstrip("/")
UPSTREAM_HTTP_URL = os.getenv("ZCODE_UPSTREAM_HTTP_URL", "http://127.0.0.1:42265").rstrip("/")
# Политика подключения рантайма для чтения: existing-only не порождает процессы.
LIST_RUNTIME_POLICY = os.getenv("ZCODE_LIST_RUNTIME_POLICY", "existing-only")

# Дефолтные пути ingress-токена (контейнер — примонтированный файл; хост — runtime zcode)
_DEFAULT_TOKEN_PATHS = (
    "/run/secrets/zcode_ingress_token",
    "/root/.zcode/server/run/ingress-token",
)

V4_PROTOCOL_VERSION = 3
REQUEST_TIMEOUT_S = 30.0
SEND_TIMEOUT_S = 180.0


def _read_ingress_token() -> str:
    # Порядок важен: zcode-server меняет ingress-токен при каждом рестарте
    # (createIngressToken: randomBytes + rename), поэтому ПРИОРИТЕТ у файлов,
    # которые обновляются вживую (примонтированный секрет / синхронизация
    # systemd-юнитом — см. deploy/systemd/zcode-ingress-sync.*). Статичная
    # копия ZCODE_INGRESS_TOKEN из .env — только резерв для ручного запуска.
    paths: list[str] = []
    explicit = os.getenv("ZCODE_INGRESS_TOKEN_FILE", "").strip()
    if explicit:
        paths.append(explicit)
    paths.extend(_DEFAULT_TOKEN_PATHS)
    for path in paths:
        try:
            with open(path, "r", encoding="utf-8") as fh:
                token = fh.read().strip()
            if token:
                return token
        except OSError:
            continue
    return os.getenv("ZCODE_INGRESS_TOKEN", "").strip()


# ── Сериализация протокола (снята с server-core.js, см. шапку модуля) ────────

def _vlq(value: int) -> bytes:
    out = bytearray()
    v = value
    while True:
        byte = v & 0x7F
        v >>= 7
        if v > 0:
            byte |= 0x80
        out.append(byte)
        if v == 0:
            break
    return bytes(out)


def _serialize(data: Any) -> bytes:
    if data is None:
        return b"\x00"
    if isinstance(data, bool):
        raw = json.dumps(data).encode()
        return b"\x05" + _vlq(len(raw)) + raw
    if isinstance(data, int):
        return b"\x06" + _vlq(data)
    if isinstance(data, str):
        raw = data.encode()
        return b"\x01" + _vlq(len(raw)) + raw
    if isinstance(data, list):
        return b"\x04" + _vlq(len(data)) + b"".join(_serialize(x) for x in data)
    raw = json.dumps(data).encode()
    return b"\x05" + _vlq(len(raw)) + raw


def _read_vlq(buf: bytes, off: int) -> tuple[int, int]:
    value = 0
    shift = 0
    while True:
        byte = buf[off]
        off += 1
        value |= (byte & 0x7F) << shift
        if not (byte & 0x80):
            return value, off
        shift += 7


def _deserialize(buf: bytes, off: int) -> tuple[Any, int]:
    kind = buf[off]
    off += 1
    if kind == 0:
        return None, off
    if kind == 1:
        length, off = _read_vlq(buf, off)
        return buf[off:off + length].decode(), off + length
    if kind in (2, 3):
        length, off = _read_vlq(buf, off)
        return buf[off:off + length], off + length
    if kind == 4:
        length, off = _read_vlq(buf, off)
        items = []
        for _ in range(length):
            value, off = _deserialize(buf, off)
            items.append(value)
        return items, off
    if kind == 5:
        length, off = _read_vlq(buf, off)
        return json.loads(buf[off:off + length]), off + length
    if kind == 6:
        return _read_vlq(buf, off)
    raise ValueError(f"неизвестный preset-байт {kind}")


def _frame(payload: bytes) -> bytes:
    return struct.pack(">BIII", 1, 0, 0, len(payload)) + payload


def _parse_frames(data: bytes) -> list[bytes]:
    frames = []
    view = memoryview(data)
    while len(view) >= 13:
        length = struct.unpack(">I", view[9:13])[0]
        frames.append(bytes(view[13:13 + length]))
        view = view[13 + length:]
    return frames


class ZcodeRpcError(RuntimeError):
    """Ошибка RPC (PromiseError) или транспорта."""

    def __init__(self, message: str, code: str | None = None):
        super().__init__(message)
        self.code = code or ""


# ── Подключение ──────────────────────────────────────────────────────────────

@dataclass
class _PendingRequest:
    fut: asyncio.Future


@dataclass
class ZcodeChannel:
    """Одно WS-соединение к ingress zcode-server'а с мультиплексом запросов."""

    ws_url: str = field(default_factory=lambda: UPSTREAM_WS_URL)

    _ws: Any = None
    _next_id: int = 1
    _pending: dict[int, _PendingRequest] = field(default_factory=dict)
    _connect_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    _v4_ready: bool = False
    _server_id: str = ""

    async def connect(self) -> None:
        async with self._connect_lock:
            if self._ws is not None:
                return
            token = _read_ingress_token()
            if not token:
                raise ZcodeRpcError("ingress-токен zcode не найден (ZCODE_INGRESS_TOKEN/файл)")
            try:
                self._ws = await websockets.connect(
                    f"{self.ws_url}/ws?token={token}",
                    max_size=64 * 1024 * 1024,
                    open_timeout=10,
                    ping_interval=20,
                    ping_timeout=20,
                )
            except websockets.exceptions.InvalidStatus as e:
                # 401 от ingress — токен сменился (рестарт zcode-server), а файл
                # секрета ещё не обновился; следующий connect() перечитает файл.
                code = getattr(getattr(e, "response", None), "status_code", "?")
                raise ZcodeRpcError(
                    f"ingress zcode отверг подключение (HTTP {code}): токен неактуален",
                    code="ingress-auth",
                ) from e
            except (OSError, asyncio.TimeoutError) as e:
                raise ZcodeRpcError(
                    f"zcode-server недоступен ({e.__class__.__name__})", code="connect-failed"
                ) from e
            init_raw = await asyncio.wait_for(self._ws.recv(), timeout=10)
            rpc_type, _ = self._parse_rpc_header(init_raw)
            if rpc_type != 200:  # Initialize
                raise ZcodeRpcError("handshake zcode-server: ожидался Initialize(200)")
            self._v4_ready = False
            self._pending.clear()
            logger.info("zcode ingress подключён")

    async def close(self) -> None:
        if self._ws is not None:
            await self._ws.close()
            self._ws = None
            self._v4_ready = False

    def _parse_rpc_header(self, raw: bytes) -> tuple[int, int | None]:
        payload = _parse_frames(raw)[0]
        header, off = _deserialize(payload, 0)
        data = None
        if off < len(payload):
            data, _ = _deserialize(payload, off)
        # Initialize приходит как [200] (без id)
        return header[0], header[1] if len(header) > 1 else None

    async def _recv_loop_step(self) -> None:
        raw = await asyncio.wait_for(self._ws.recv(), timeout=5)
        for payload in _parse_frames(raw):
            header, off = _deserialize(payload, 0)
            rpc_type, rpc_id = header[0], header[1]
            pending = self._pending.get(rpc_id)
            if pending is None:
                continue
            data = None
            if off < len(payload):
                data, _ = _deserialize(payload, off)
            if rpc_type == 201:  # PromiseSuccess
                self._pending.pop(rpc_id, None)
                if not pending.fut.done():
                    pending.fut.set_result(data)
            elif rpc_type in (202, 203):  # PromiseError / PromiseErrorObj
                self._pending.pop(rpc_id, None)
                message = "zcode RPC error"
                code = None
                if isinstance(data, dict):
                    message = str(data.get("message") or message)
                    code = data.get("code") if isinstance(data.get("code"), str) else None
                if not pending.fut.done():
                    pending.fut.set_exception(ZcodeRpcError(message, code))
            # 204/205 (EventFire/EventError) здесь не используются

    async def request(self, name: str, args: list[Any], timeout: float = REQUEST_TIMEOUT_S) -> Any:
        """Вызов команды канала "zcode-agent"; args — массив позиционных аргументов."""
        if self._ws is None:
            await self.connect()
        rpc_id = self._next_id
        self._next_id += 1
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending[rpc_id] = _PendingRequest(fut=fut)
        payload = _serialize([100, rpc_id, "zcode-agent", name]) + _serialize(args)
        try:
            await self._ws.send(_frame(payload))
        except Exception as e:  # соединение умерло — переподключение и один ретрай
            await self._reset()
            if self._ws is None:
                await self.connect()
            await self._ws.send(_frame(payload))
            logger.warning("zcode ws: повторная отправка после разрыва (%s)", e.__class__.__name__)

        deadline = time.monotonic() + timeout
        while True:
            try:
                await asyncio.wait_for(self._recv_loop_step(), timeout=max(deadline - time.monotonic(), 0.1))
            except asyncio.TimeoutError:
                if time.monotonic() >= deadline:
                    self._pending.pop(rpc_id, None)
                    raise ZcodeRpcError(f"таймаут запроса {name}")
            except Exception as e:
                if rpc_id in self._pending:
                    self._pending.pop(rpc_id, None)
                    if not fut.done():
                        fut.set_exception(ZcodeRpcError(f"соединение потеряно: {e.__class__.__name__}"))
                raise
            if fut.done():
                exc = fut.exception()
                if exc is not None:
                    raise exc
                return fut.result()

    async def _reset(self) -> None:
        self._ws = None
        self._v4_ready = False
        for pending in self._pending.values():
            if not pending.fut.done():
                pending.fut.set_exception(ZcodeRpcError("соединение zcode закрыто"))
        self._pending.clear()

    # ── v4 handshake (один раз на соединение) ───────────────────────────

    async def ensure_v4(self) -> None:
        if self._v4_ready:
            return
        hello = await self.request("helloConversationV4", [])
        if not isinstance(hello, dict) or hello.get("protocolVersion") != V4_PROTOCOL_VERSION:
            raise ZcodeRpcError("v4 hello: неожиданный ответ сервера")
        client_hello = {
            "kind": "clientHello",
            "protocolVersion": V4_PROTOCOL_VERSION,
            "clientId": f"zcode-web-{uuid.uuid4().hex[:8]}",
            "clientKind": "web",
            "appVersion": "web/1.0",
            "capabilities": {},
        }
        await self.request("initializeConversationV4", [client_hello])
        self._v4_ready = True

    # ── Высокоуровневые операции ─────────────────────────────────────────

    async def server_id(self) -> str:
        """serverId из /api/server-info (для канонического workspaceIdentity)."""
        if self._server_id:
            return self._server_id
        token = _read_ingress_token()
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(
                f"{UPSTREAM_HTTP_URL}/api/server-info",
                headers={"Authorization": f"Bearer {token}"},
            )
            resp.raise_for_status()
            self._server_id = str(resp.json().get("serverId") or "")
        return self._server_id

    async def workspace_ref(self, workspace_path: str) -> dict:
        ref: dict[str, Any] = {"workspacePath": workspace_path}
        try:
            sid = await self.server_id()
            if sid:
                ref["workspaceIdentity"] = f"remote:server:{sid}:{workspace_path}"
        except Exception:
            pass
        return ref

    async def list_sessions(self, workspace_path: str) -> list[dict]:
        params = {
            **await self.workspace_ref(workspace_path),
            "limit": 100,
            "runtimePolicy": LIST_RUNTIME_POLICY,
        }
        result = await self.request("listSessions", [params])
        sessions = result.get("sessions") if isinstance(result, dict) else result
        return sessions if isinstance(sessions, list) else []

    async def session_lifecycle(self, session_id: str, workspace_path: str) -> dict:
        params = {**await self.workspace_ref(workspace_path), "sessionId": session_id}
        return await self.request("getSessionLifecycle", [params])

    async def conversation_rows(
        self, session_id: str, workspace_path: str,
        before_row_id: int | None = None, limit: int = 60,
    ) -> dict:
        await self.ensure_v4()
        params: dict[str, Any] = {
            **await self.workspace_ref(workspace_path),
            "sessionId": session_id,
            "limit": max(1, min(int(limit), 200)),
            "clientMode": "web-remote-replayable",
        }
        if before_row_id is not None:
            params["beforeRowId"] = int(before_row_id)
        return await self.request("conversationRowsRangeV4", [params], timeout=REQUEST_TIMEOUT_S)

    async def send_prompt(self, session_id: str, workspace_path: str, text: str) -> dict:
        params = {
            **await self.workspace_ref(workspace_path),
            "sessionId": session_id,
            "content": text,
            "inputId": uuid.uuid4().hex,
            "runtimePolicy": "start-if-needed",
        }
        return await self.request("sendPrompt", [params], timeout=SEND_TIMEOUT_S)

    async def create_session(self, workspace_path: str, title: str | None = None) -> dict:
        params: dict[str, Any] = {
            **await self.workspace_ref(workspace_path),
            "mode": "build",
            "runtimePolicy": "start-if-needed",
        }
        if title:
            params["title"] = title
        try:
            return await self.request("createSession", [params], timeout=SEND_TIMEOUT_S)
        except ZcodeRpcError:
            if not title:
                raise
            # Старые схемы могут не принимать title при создании — повтор без него.
            params.pop("title")
            return await self.request("createSession", [params], timeout=SEND_TIMEOUT_S)

    async def close_session(self, session_id: str, workspace_path: str) -> dict:
        params = {**await self.workspace_ref(workspace_path), "sessionId": session_id}
        return await self.request("closeSession", [params])


# Глобальное разделяемое подключение (одно на процесс; запросы мультиплексируются по id)
_channel: ZcodeChannel | None = None


def get_channel() -> ZcodeChannel:
    global _channel
    if _channel is None:
        _channel = ZcodeChannel()
    return _channel


async def reset_channel() -> None:
    global _channel
    if _channel is not None:
        await _channel.close()
    _channel = None
