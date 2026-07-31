from __future__ import annotations

import json
import os
import re
import socket
import stat
import struct
import sys
import tempfile
import uuid
from pathlib import Path
from typing import Any, Optional


MAX_FRAME_BYTES = 16 * 1024 * 1024
MAX_PROMPT_BYTES = 16 * 1024
DEFAULT_TIMEOUT_SECONDS = 8.0
DESKTOP_IPC_SOCKET_ENV = "LOOPWEAVE_DESKTOP_IPC_SOCKET"
_THREAD_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
)


class DesktopIpcError(RuntimeError):
    pass


def _legacy_desktop_ipc_socket_path() -> Path:
    uid = os.getuid() if hasattr(os, "getuid") else None
    name = f"ipc-{uid}.sock" if uid is not None else "ipc.sock"
    return Path(tempfile.gettempdir()) / "codex-ipc" / name


def desktop_ipc_socket_candidates() -> tuple[Path, ...]:
    explicit = os.environ.get(DESKTOP_IPC_SOCKET_ENV, "").strip()
    if explicit:
        return (Path(explicit).expanduser(),)
    if sys.platform == "win32":
        return (Path(r"\\.\pipe\codex-ipc"),)

    configured_home = os.environ.get("CODEX_HOME", "").strip()
    codex_home = (
        Path(configured_home).expanduser()
        if configured_home
        else Path.home() / ".codex"
    )
    candidates = (
        codex_home / "ipc" / "ipc.sock",
        _legacy_desktop_ipc_socket_path(),
    )
    return tuple(dict.fromkeys(candidates))


def _is_owned_socket(path: Path) -> bool:
    try:
        details = path.lstat()
    except OSError:
        return False
    if not stat.S_ISSOCK(details.st_mode):
        return False
    return not hasattr(os, "getuid") or details.st_uid == os.getuid()


def desktop_ipc_socket_path() -> Path:
    if sys.platform == "win32":
        return desktop_ipc_socket_candidates()[0]
    candidates = desktop_ipc_socket_candidates()
    for candidate in candidates:
        if _is_owned_socket(candidate):
            return candidate
    return candidates[0]


def encode_frame(payload: dict[str, Any]) -> bytes:
    try:
        body = json.dumps(
            payload,
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError) as error:
        raise DesktopIpcError("invalid IPC payload") from error
    if not body or len(body) > MAX_FRAME_BYTES:
        raise DesktopIpcError("invalid IPC frame length")
    return struct.pack("<I", len(body)) + body


def _read_exact(connection: Any, size: int) -> bytes:
    chunks: list[bytes] = []
    remaining = size
    while remaining:
        try:
            chunk = connection.recv(remaining)
        except Exception as error:
            raise DesktopIpcError("Desktop IPC read failed") from error
        if not chunk:
            raise DesktopIpcError("Desktop IPC connection closed")
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def read_frame(connection: Any) -> Any:
    header = _read_exact(connection, 4)
    length = struct.unpack("<I", header)[0]
    if length == 0 or length > MAX_FRAME_BYTES:
        raise DesktopIpcError(f"invalid IPC frame length: {length}")
    body = _read_exact(connection, length)
    try:
        return json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise DesktopIpcError("invalid Desktop IPC JSON") from error


class DesktopIpcClient:
    def __init__(
        self,
        *,
        socket_path: Optional[Path] = None,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        if timeout_seconds <= 0 or timeout_seconds > 30:
            raise ValueError("timeout_seconds must be between 0 and 30")
        self.socket_path = Path(socket_path or desktop_ipc_socket_path())
        self.timeout_seconds = timeout_seconds

    def start_visible_turn(
        self,
        *,
        thread_id: str,
        prompt: str,
    ) -> dict[str, Any]:
        if not _THREAD_RE.fullmatch(thread_id):
            raise DesktopIpcError("invalid Desktop thread id")
        if not isinstance(prompt, str) or not prompt.strip():
            raise DesktopIpcError("visible review prompt is empty")
        if len(prompt.encode("utf-8")) > MAX_PROMPT_BYTES:
            raise DesktopIpcError("visible review prompt exceeds IPC limit")

        connection, client_id = self._connect_initialized()
        try:
            return self._request(
                connection,
                method="thread-follower-start-turn",
                version=1,
                source_client_id=client_id,
                params={
                    "conversationId": thread_id,
                    "turnStartParams": {
                        "threadId": thread_id,
                        "input": [
                            {
                                "type": "text",
                                "text": prompt,
                                "text_elements": [],
                            }
                        ],
                    },
                },
            )
        finally:
            connection.close()

    def probe(self) -> dict[str, Any]:
        connection, client_id = self._connect_initialized()
        connection.close()
        return {"healthy": True, "client_id": client_id}

    def _connect_initialized(self) -> tuple[Any, str]:
        self._validate_socket()
        if sys.platform == "win32":
            from .win32_pipe import pipe_connect

            try:
                handle = pipe_connect(
                    str(self.socket_path), timeout=self.timeout_seconds
                )
            except Exception as error:
                raise DesktopIpcError(
                    "Desktop IPC connection failed"
                ) from error
            connection = _PipeStream(handle, timeout=self.timeout_seconds)
        else:
            connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            connection.settimeout(self.timeout_seconds)
            connection.connect(str(self.socket_path))
        try:
            initialized = self._request(
                connection,
                method="initialize",
                version=0,
                params={"clientType": "loopweave-visible-bridge"},
                source_client_id="initializing-client",
            )
            client_id = initialized.get("clientId")
            if not isinstance(client_id, str) or not client_id:
                raise DesktopIpcError(
                    "Desktop IPC initialize returned no client id"
                )
            return connection, client_id
        except DesktopIpcError:
            connection.close()
            raise
        except (OSError, socket.timeout) as error:
            connection.close()
            raise DesktopIpcError("Desktop IPC connection failed") from error

    def _validate_socket(self) -> None:
        if sys.platform == "win32":
            from .win32_pipe import is_pipe_name

            if not is_pipe_name(str(self.socket_path)):
                raise DesktopIpcError(
                    "Codex Desktop IPC path is not a named pipe"
                )
            return
        try:
            details = self.socket_path.lstat()
        except OSError as error:
            raise DesktopIpcError("Codex Desktop IPC socket is unavailable") from error
        if not stat.S_ISSOCK(details.st_mode):
            raise DesktopIpcError("Codex Desktop IPC path is not a socket")
        if hasattr(os, "getuid") and details.st_uid != os.getuid():
            raise DesktopIpcError("Codex Desktop IPC socket owner mismatch")

    def _request(
        self,
        connection: Any,
        *,
        method: str,
        version: int,
        params: dict[str, Any],
        source_client_id: str,
    ) -> dict[str, Any]:
        request_id = str(uuid.uuid4())
        connection.sendall(
            encode_frame(
                {
                    "type": "request",
                    "requestId": request_id,
                    "sourceClientId": source_client_id,
                    "version": version,
                    "method": method,
                    "params": params,
                }
            )
        )
        while True:
            response = read_frame(connection)
            if not isinstance(response, dict):
                raise DesktopIpcError("invalid Desktop IPC response")
            if response.get("type") == "client-discovery-request":
                self._reject_discovery(connection, response)
                continue
            if response.get("type") == "broadcast":
                continue
            if (
                response.get("type") != "response"
                or response.get("requestId") != request_id
            ):
                raise DesktopIpcError("mismatched Desktop IPC response")
            if response.get("resultType") != "success":
                detail = response.get("error")
                if not isinstance(detail, str) or not detail:
                    detail = "request-failed"
                raise DesktopIpcError(f"Desktop IPC {method} failed: {detail}")
            if response.get("method") != method:
                raise DesktopIpcError("Desktop IPC response method mismatch")
            result = response.get("result")
            if not isinstance(result, dict):
                raise DesktopIpcError("Desktop IPC response result is invalid")
            return result

    @staticmethod
    def _reject_discovery(
        connection: Any,
        request: dict[str, Any],
    ) -> None:
        request_id = request.get("requestId")
        if not isinstance(request_id, str):
            raise DesktopIpcError("invalid Desktop IPC discovery request")
        connection.sendall(
            encode_frame(
                {
                    "type": "client-discovery-response",
                    "requestId": request_id,
                    "response": {"canHandle": False},
                }
            )
        )


class _PipeStream:
    """Byte-stream adapter over a Win32 named-pipe handle."""

    def __init__(
        self,
        handle: int,
        *,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        self._handle = handle
        self._timeout_ms = int(max(1.0, timeout) * 1000)

    def sendall(self, data: bytes) -> None:
        from .win32_pipe import NamedPipeError, pipe_write_all

        try:
            pipe_write_all(self._handle, data)
        except NamedPipeError as error:
            raise DesktopIpcError("Desktop IPC write failed") from error

    def recv(self, size: int) -> bytes:
        from .win32_pipe import NamedPipeError, pipe_read

        try:
            return pipe_read(self._handle, size, timeout_ms=self._timeout_ms)
        except NamedPipeError as error:
            raise DesktopIpcError("Desktop IPC read failed") from error

    def close(self) -> None:
        from .win32_pipe import pipe_close

        pipe_close(self._handle)
