from __future__ import annotations
import sys

import socket
import struct
import tempfile
import threading
from pathlib import Path
from typing import Any, Iterator

import pytest

import loopweave.desktop_ipc as desktop_ipc
from loopweave.desktop_ipc import (
    DesktopIpcClient,
    DesktopIpcError,
    desktop_ipc_socket_path,
    encode_frame,
    read_frame,
)


THREAD_ID = "11111111-1111-4111-8111-111111111111"


@pytest.fixture
def short_socket_path() -> Iterator[Path]:
    with tempfile.TemporaryDirectory(prefix="loopweave-ipc-", dir="/tmp") as directory:
        yield Path(directory) / "ipc.sock"


def _recv_frame(connection: socket.socket) -> dict[str, Any]:
    payload = read_frame(connection)
    assert isinstance(payload, dict)
    return payload


def _send_frame(connection: socket.socket, payload: dict[str, Any]) -> None:
    connection.sendall(encode_frame(payload))


class FakeDesktopRouter:
    def __init__(
        self,
        socket_path: Path,
        *,
        start_error: str | None = None,
        expect_start: bool = True,
    ) -> None:
        self.socket_path = socket_path
        self.start_error = start_error
        self.expect_start = expect_start
        self.messages: list[dict[str, Any]] = []
        self.failure: BaseException | None = None
        self.ready = threading.Event()
        self.thread = threading.Thread(target=self._serve, daemon=True)

    def __enter__(self) -> "FakeDesktopRouter":
        self.thread.start()
        assert self.ready.wait(timeout=2)
        return self

    def __exit__(self, *_: object) -> None:
        self.thread.join(timeout=2)
        assert not self.thread.is_alive()
        if self.failure is not None:
            raise self.failure

    def _serve(self) -> None:
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as server:
                server.bind(str(self.socket_path))
                server.listen(1)
                self.ready.set()
                connection, _ = server.accept()
                with connection:
                    initialize = _recv_frame(connection)
                    self.messages.append(initialize)
                    _send_frame(
                        connection,
                        {
                            "type": "response",
                            "requestId": initialize["requestId"],
                            "resultType": "success",
                            "method": "initialize",
                            "handledByClientId": "fake-bridge-client",
                            "result": {"clientId": "fake-bridge-client"},
                        },
                    )
                    if not self.expect_start:
                        return
                    start = _recv_frame(connection)
                    self.messages.append(start)
                    if self.start_error is not None:
                        response = {
                            "type": "response",
                            "requestId": start["requestId"],
                            "resultType": "error",
                            "error": self.start_error,
                        }
                    else:
                        response = {
                            "type": "response",
                            "requestId": start["requestId"],
                            "resultType": "success",
                            "method": "thread-follower-start-turn",
                            "handledByClientId": "desktop-owner",
                            "result": {"turnId": "turn-visible"},
                        }
                    _send_frame(connection, response)
        except BaseException as error:  # surfaced by __exit__ in the test thread
            self.failure = error
            self.ready.set()


def _bind_socket(path: Path) -> socket.socket:
    path.parent.mkdir(parents=True, exist_ok=True)
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(str(path))
    return server


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX AF_UNIX socket required")
def test_socket_discovery_prefers_current_codex_home_socket(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with tempfile.TemporaryDirectory(prefix="loopweave-discovery-", dir="/tmp") as root:
        base = Path(root)
        current = base / "codex" / "ipc" / "ipc.sock"
        legacy = base / "tmp" / "codex-ipc" / "ipc-501.sock"
        current_server = _bind_socket(current)
        legacy_server = _bind_socket(legacy)
        try:
            monkeypatch.setenv("CODEX_HOME", str(base / "codex"))
            monkeypatch.delenv("LOOPWEAVE_DESKTOP_IPC_SOCKET", raising=False)
            monkeypatch.setattr(
                desktop_ipc,
                "_legacy_desktop_ipc_socket_path",
                lambda: legacy,
            )

            assert desktop_ipc_socket_path() == current
        finally:
            current_server.close()
            legacy_server.close()


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX AF_UNIX socket required")
def test_socket_discovery_falls_back_to_legacy_socket(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with tempfile.TemporaryDirectory(prefix="loopweave-discovery-", dir="/tmp") as root:
        base = Path(root)
        legacy = base / "tmp" / "codex-ipc" / "ipc-501.sock"
        legacy_server = _bind_socket(legacy)
        try:
            monkeypatch.setenv("CODEX_HOME", str(base / "codex"))
            monkeypatch.delenv("LOOPWEAVE_DESKTOP_IPC_SOCKET", raising=False)
            monkeypatch.setattr(
                desktop_ipc,
                "_legacy_desktop_ipc_socket_path",
                lambda: legacy,
            )

            assert desktop_ipc_socket_path() == legacy
        finally:
            legacy_server.close()


def test_socket_discovery_honors_explicit_override(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    explicit = Path("~/custom-codex-ipc.sock").expanduser()
    monkeypatch.setenv("LOOPWEAVE_DESKTOP_IPC_SOCKET", "~/custom-codex-ipc.sock")

    assert desktop_ipc_socket_path() == explicit


def test_frame_codec_handles_fragmented_reads() -> None:
    left, right = socket.socketpair()
    try:
        payload = {"type": "response", "text": "可见审查"}
        frame = encode_frame(payload)
        for byte in frame:
            left.sendall(bytes([byte]))
        assert read_frame(right) == payload
    finally:
        left.close()
        right.close()


@pytest.mark.parametrize("length", [0, 16 * 1024 * 1024 + 1])
def test_frame_reader_rejects_invalid_length(length: int) -> None:
    left, right = socket.socketpair()
    try:
        left.sendall(struct.pack("<I", length))
        with pytest.raises(DesktopIpcError, match="frame length"):
            read_frame(right)
    finally:
        left.close()
        right.close()


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX AF_UNIX socket required")
def test_start_visible_turn_uses_owner_routed_desktop_request(
    short_socket_path: Path,
) -> None:
    socket_path = short_socket_path
    marker = (
        "[LOOPWEAVE_VISIBLE_REVIEW "
        "review_id=review-request-test generation=3]"
    )
    prompt = marker + "\nsource: loopweave\nrun_id: run-test"

    with FakeDesktopRouter(socket_path) as router:
        response = DesktopIpcClient(socket_path=socket_path).start_visible_turn(
            thread_id=THREAD_ID,
            prompt=prompt,
        )

    assert response == {"turnId": "turn-visible"}
    initialize, start = router.messages
    assert initialize["type"] == "request"
    assert initialize["method"] == "initialize"
    assert initialize["version"] == 0
    assert initialize["params"] == {"clientType": "loopweave-visible-bridge"}

    assert start["type"] == "request"
    assert start["method"] == "thread-follower-start-turn"
    assert start["version"] == 1
    assert start["sourceClientId"] == "fake-bridge-client"
    assert start["params"] == {
        "conversationId": THREAD_ID,
        "turnStartParams": {
            "threadId": THREAD_ID,
            "input": [
                {
                    "type": "text",
                    "text": prompt,
                    "text_elements": [],
                }
            ],
        },
    }


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX AF_UNIX socket required")
def test_probe_performs_initialize_only_read_only_handshake(
    short_socket_path: Path,
) -> None:
    with FakeDesktopRouter(short_socket_path, expect_start=False) as router:
        result = DesktopIpcClient(socket_path=short_socket_path).probe()

    assert result == {
        "healthy": True,
        "client_id": "fake-bridge-client",
    }
    assert [message["method"] for message in router.messages] == ["initialize"]


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX AF_UNIX socket required")
def test_start_visible_turn_fails_closed_when_no_owner_can_handle(
    short_socket_path: Path,
) -> None:
    socket_path = short_socket_path
    with FakeDesktopRouter(socket_path, start_error="no-client-found"):
        with pytest.raises(DesktopIpcError, match="no-client-found"):
            DesktopIpcClient(socket_path=socket_path).start_visible_turn(
                thread_id=THREAD_ID,
                prompt="[LOOPWEAVE_VISIBLE_REVIEW review_id=r generation=1]",
            )


def test_start_visible_turn_rejects_oversized_prompt(tmp_path: Path) -> None:
    with pytest.raises(DesktopIpcError, match="prompt exceeds"):
        DesktopIpcClient(socket_path=tmp_path / "missing.sock").start_visible_turn(
            thread_id=THREAD_ID,
            prompt="x" * (16 * 1024 + 1),
        )


def test_start_visible_turn_rejects_invalid_thread_before_connect(
    tmp_path: Path,
) -> None:
    with pytest.raises(DesktopIpcError, match="thread id"):
        DesktopIpcClient(socket_path=tmp_path / "missing.sock").start_visible_turn(
            thread_id="not-a-thread",
            prompt="safe",
        )
