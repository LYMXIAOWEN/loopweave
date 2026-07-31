"""Windows Desktop-IPC tests: frame codec over a real named pipe and the
``DesktopIpcClient`` handshake against a synthetic frame router."""

from __future__ import annotations

import json
import struct
import sys
import threading
import time
import unittest
import uuid
from pathlib import Path
from unittest import mock

from loopweave.desktop_ipc import (
    DesktopIpcClient,
    DesktopIpcError,
    desktop_ipc_socket_candidates,
    encode_frame,
)


def _decode_frame(data: bytes) -> dict:
    length = struct.unpack("<I", data[:4])[0]
    return json.loads(data[4 : 4 + length].decode("utf-8"))


class _FakeDesktopRouter:
    """Synthetic Codex Desktop that answers the frame protocol over a pipe."""

    def __init__(self, pipe_name: str) -> None:
        self.pipe_name = pipe_name
        self.requests: list[dict] = []
        self.stop = threading.Event()
        self.errors: list[Exception] = []
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.thread.start()

    def close(self) -> None:
        self.stop.set()
        from loopweave.win32_pipe import pipe_close, pipe_connect

        try:
            handle = pipe_connect(self.pipe_name, timeout=0.3)
        except Exception:
            handle = None
        if handle is not None:
            pipe_close(handle)
        self.thread.join(timeout=3.0)

    def _read_exact(self, handle: int, size: int) -> bytes:
        from loopweave.win32_pipe import pipe_read

        chunks = []
        remaining = size
        deadline = time.monotonic() + 5.0
        empty_streak = 0
        while remaining > 0 and time.monotonic() < deadline:
            chunk = pipe_read(handle, remaining, timeout_ms=100)
            if not chunk:
                empty_streak += 1
                if empty_streak >= 10:
                    break
                time.sleep(0.01)
                continue
            empty_streak = 0
            chunks.append(chunk)
            remaining -= len(chunk)
        if remaining:
            raise DesktopIpcError("fake desktop read incomplete")
        return b"".join(chunks)

    def _serve(self) -> None:
        from loopweave.win32_pipe import (
            NamedPipeError,
            connect_pipe_server,
            create_pipe_server,
            disconnect_pipe,
            flush_pipe,
            pipe_close,
        )

        handle = None
        try:
            handle = create_pipe_server(self.pipe_name)
        except NamedPipeError:
            return
        while not self.stop.is_set():
            try:
                connect_pipe_server(handle)
                self._handle_connection(handle)
                flush_pipe(handle)
                disconnect_pipe(handle)
            except NamedPipeError:
                try:
                    disconnect_pipe(handle)
                except NamedPipeError:
                    pass
                if self.stop.is_set():
                    break
                time.sleep(0.05)
                continue
            except Exception as error:  # pragma: no cover - diagnostic
                self.errors.append(error)
                try:
                    disconnect_pipe(handle)
                except NamedPipeError:
                    pass
                if self.stop.is_set():
                    break
                time.sleep(0.05)
                continue
        if handle is not None:
            pipe_close(handle)

    def _handle_connection(self, handle: int) -> None:
        from loopweave.win32_pipe import pipe_write_all

        # One connection carries multiple requests (initialize first, then
        # thread-follower-start-turn), mirroring the real Desktop socket.
        while True:
            try:
                header = self._read_exact(handle, 4)
            except DesktopIpcError:
                return  # client closed the connection
            length = struct.unpack("<I", header)[0]
            body = self._read_exact(handle, length)
            request = json.loads(body.decode("utf-8"))
            self.requests.append(request)
            if request.get("method") == "initialize":
                result = {
                    "clientId": f"fake-desktop-client-{uuid.uuid4().hex[:8]}"
                }
                method = "initialize"
            elif request.get("method") == "thread-follower-start-turn":
                result = {"accepted": True}
                method = "thread-follower-start-turn"
            else:
                result = {}
                method = request.get("method", "unknown")
            response = {
                "type": "response",
                "requestId": request.get("requestId"),
                "resultType": "success",
                "method": method,
                "result": result,
            }
            pipe_write_all(handle, encode_frame(response))


@unittest.skipUnless(sys.platform == "win32", "Windows named pipes only")
class DesktopIpcWindowsPipeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.pipe_name = rf"\\.\pipe\loopweave-desktop-{uuid.uuid4().hex[:12]}"
        self.router = _FakeDesktopRouter(self.pipe_name)
        from loopweave.win32_pipe import pipe_server_available

        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            if pipe_server_available(self.pipe_name):
                return
            if self.router.errors:
                break
            time.sleep(0.02)
            continue
        self.fail("fake desktop pipe never became available")

    def tearDown(self) -> None:
        self.router.close()

    def test_desktop_ipc_socket_candidates_use_codex_pipe_on_windows(self) -> None:
        with mock.patch.dict(
            "os.environ",
            {"LOOPWEAVE_DESKTOP_IPC_SOCKET": ""},
            clear=False,
        ):
            candidates = desktop_ipc_socket_candidates()
        self.assertEqual(str(candidates[0]), r"\\.\pipe\codex-ipc")

    def test_probe_performs_initialize_only_handshake(self) -> None:
        client = DesktopIpcClient(socket_path=Path(self.pipe_name))
        result = client.probe()
        self.assertTrue(result["healthy"])
        self.assertIn("client_id", result)
        methods = [r.get("method") for r in self.router.requests]
        self.assertIn("initialize", methods)
        self.assertNotIn("thread-follower-start-turn", methods)

    def test_start_visible_turn_exchanges_owner_routed_request(self) -> None:
        client = DesktopIpcClient(socket_path=Path(self.pipe_name))
        thread_id = "019fb8b1-a0ef-73b1-bdbb-2c8891b4196e"
        result = client.start_visible_turn(
            thread_id=thread_id, prompt="Please review this stage."
        )
        self.assertEqual(result, {"accepted": True})
        methods = [r.get("method") for r in self.router.requests]
        self.assertIn("initialize", methods)
        self.assertIn("thread-follower-start-turn", methods)
        start = next(
            r
            for r in self.router.requests
            if r.get("method") == "thread-follower-start-turn"
        )
        self.assertEqual(start["params"]["conversationId"], thread_id)
        self.assertEqual(
            start["params"]["turnStartParams"]["threadId"], thread_id
        )
        prompt_text = start["params"]["turnStartParams"]["input"][0]["text"]
        self.assertEqual(prompt_text, "Please review this stage.")

    def test_invalid_thread_id_rejected_before_connect(self) -> None:
        client = DesktopIpcClient(socket_path=Path(self.pipe_name))
        with self.assertRaises(DesktopIpcError):
            client.start_visible_turn(thread_id="not-a-uuid", prompt="x")


if __name__ == "__main__":
    unittest.main()
