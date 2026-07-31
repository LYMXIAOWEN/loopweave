"""Control-transport tests: endpoint resolution and named-pipe round trips.

The POSIX Unix-socket behavior is covered by the existing supervisor tests;
this file pins the platform-neutral endpoint contract and the Windows
named-pipe transport (real pipe round trip when running on Windows).
"""

from __future__ import annotations

import json
import sys
import tempfile
import threading
import time
import unittest
import uuid
from pathlib import Path

from loopweave import control_transport


class EndpointResolutionTests(unittest.TestCase):
    def test_pipe_endpoint_detection(self) -> None:
        self.assertTrue(
            control_transport.is_named_pipe_endpoint(
                r"\\.\pipe\loopweave-control-run-1"
            )
        )
        self.assertTrue(
            control_transport.is_named_pipe_endpoint(r"\\?\pipe\codex-ipc")
        )
        self.assertFalse(
            control_transport.is_named_pipe_endpoint("/var/run/x.sock")
        )
        self.assertFalse(
            control_transport.is_named_pipe_endpoint("C:\\var\\x.sock")
        )

    def test_resolve_control_endpoint_round_trips_strings(self) -> None:
        self.assertEqual(
            control_transport.resolve_control_endpoint(r"\\.\pipe\a-b"),
            r"\\.\pipe\a-b",
        )

    def test_control_endpoint_available_falls_back_to_filesystem(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "control.sock"
            self.assertFalse(control_transport.control_endpoint_available(path))
            path.write_text("", encoding="utf-8")
            self.assertTrue(control_transport.control_endpoint_available(path))


@unittest.skipUnless(sys.platform == "win32", "Windows named pipes only")
class NamedPipeControlRoundTripTests(unittest.TestCase):
    def setUp(self) -> None:
        self.pipe_name = rf"\\.\pipe\loopweave-test-{uuid.uuid4().hex[:12]}"
        self.token = "secret-token"
        self.stop = threading.Event()
        self.errors: list[Exception] = []
        self.server_thread = threading.Thread(
            target=self._serve, daemon=True
        )
        self.server_thread.start()
        self._wait_for_listener()

    def tearDown(self) -> None:
        self.stop.set()
        from loopweave.win32_pipe import pipe_close, pipe_connect

        try:
            handle = pipe_connect(self.pipe_name, timeout=0.3)
        except Exception:
            handle = None
        if handle is not None:
            pipe_close(handle)
        self.server_thread.join(timeout=3.0)

    def _wait_for_listener(self, timeout: float = 5.0) -> None:
        from loopweave.win32_pipe import pipe_server_available

        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if pipe_server_available(self.pipe_name):
                return
            time.sleep(0.02)
        self.fail("control pipe server never started listening")

    def _serve(self) -> None:
        from loopweave.win32_pipe import (
            NamedPipeError,
            connect_pipe_server,
            create_pipe_server,
            disconnect_pipe,
            flush_pipe,
            pipe_close,
            pipe_read,
            pipe_write_all,
        )

        handle = None
        try:
            handle = create_pipe_server(self.pipe_name)
        except NamedPipeError:
            return
        while not self.stop.is_set():
            try:
                connect_pipe_server(handle)
                data = bytearray()
                deadline = time.monotonic() + 3.0
                while b"\n" not in data and time.monotonic() < deadline:
                    chunk = pipe_read(handle, 65536, timeout_ms=100)
                    if not chunk:
                        break
                    data.extend(chunk)
                payload = json.loads(
                    bytes(data).split(b"\n", 1)[0].decode("utf-8")
                )
                if payload.get("token") != self.token:
                    response = {
                        "status": "error",
                        "message": "invalid control token",
                    }
                else:
                    response = {
                        "status": "ok",
                        "run_id": payload.get("run_id", "run-test"),
                        "echo": payload.get("text"),
                    }
                pipe_write_all(
                    handle,
                    (json.dumps(response) + "\n").encode("utf-8"),
                )
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

    def test_send_control_message_round_trips_over_pipe(self) -> None:
        response = control_transport.send_control_message(
            self.pipe_name,
            {
                "token": self.token,
                "action": "send",
                "text": "hello",
                "run_id": "run-test",
            },
            timeout=3.0,
        )
        self.assertEqual(response["status"], "ok")
        self.assertEqual(response["echo"], "hello")

    def test_wrong_token_is_rejected_over_pipe(self) -> None:
        response = control_transport.send_control_message(
            self.pipe_name,
            {"token": "wrong", "action": "status"},
            timeout=3.0,
        )
        self.assertEqual(response["status"], "error")
        self.assertIn("token", response["message"])

    def test_missing_pipe_raises_actionable_error(self) -> None:
        missing = rf"\\.\pipe\loopweave-test-missing-{uuid.uuid4().hex[:12]}"
        from loopweave.win32_pipe import NamedPipeError

        with self.assertRaises(NamedPipeError):
            control_transport.send_control_message(
                missing,
                {"token": self.token, "action": "status"},
                timeout=0.5,
            )

    def test_endpoint_available_detects_live_pipe(self) -> None:
        self.assertTrue(
            control_transport.control_endpoint_available(self.pipe_name)
        )
        missing = rf"\\.\pipe\loopweave-test-gone-{uuid.uuid4().hex[:12]}"
        self.assertFalse(
            control_transport.control_endpoint_available(missing)
        )


if __name__ == "__main__":
    unittest.main()
