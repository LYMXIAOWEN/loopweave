"""Windows ConPTY terminal-host tests.

Two layers:

* mock-based lifecycle tests run on every platform (fake ``winpty.PTY``);
* real ConPTY tests run only on Windows with the ``windows`` extra
  installed and exercise the actual pseudo console, named-pipe control
  channel, line-ending normalization, and stop/identity behavior.
"""

from __future__ import annotations

import sys
import tempfile
import time
import unittest
import uuid
from pathlib import Path
from unittest import mock


class _FakeBackend:
    ConPTY = 0
    WinPTY = 1


class _FakePTY:
    """Scriptable stand-in for ``winpty.PTY``."""

    def __init__(self, cols: int, rows: int, backend=None, **kwargs) -> None:
        self._fake_pid = 424242
        self.alive = False
        self.written: list[str] = []

    @property
    def pid(self) -> int:
        return self._fake_pid

    def spawn(self, appname, cmdline=None, cwd=None, env=None) -> bool:
        self.alive = True
        return True

    def read(self, blocking: bool = False) -> str:
        return ""

    def write(self, to_write: str) -> int:
        self.written.append(to_write)
        return len(to_write)

    def set_size(self, cols: int, rows: int) -> None:
        pass

    def isalive(self) -> bool:
        return self.alive

    def iseof(self) -> bool:
        return False

    def get_exitstatus(self) -> int:
        return 0

    def cancel_io(self) -> bool:
        return True


def _make_host(
    root: Path,
    *,
    command=None,
    run_id: str = "run-wtest",
    token: str = "secret",
) -> object:
    from loopweave.windows_terminal_host import WindowsConPtyHost

    return WindowsConPtyHost(
        run_id=run_id,
        command=command or [sys.executable, "-c", "pass"],
        cwd=root,
        run_dir=root / "run",
        socket_path=Path(
            rf"\\.\pipe\loopweave-control-{run_id}-{uuid.uuid4().hex[:8]}"
        ),
        control_token=token,
        passthrough=False,
    )


class WindowsHostContractTests(unittest.TestCase):
    """Mock-based lifecycle contract tests, runnable on any platform."""

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.pty_patch = mock.patch(
            "loopweave.windows_terminal_host.PTY", _FakePTY
        )
        self.backend_patch = mock.patch(
            "loopweave.windows_terminal_host.Backend", _FakeBackend
        )
        self.pty_patch.start()
        self.backend_patch.start()

    def tearDown(self) -> None:
        self.backend_patch.stop()
        self.pty_patch.stop()
        self.temp_dir.cleanup()

    def test_constructor_rejects_non_pipe_endpoint(self) -> None:
        from loopweave.windows_terminal_host import WindowsConPtyHost

        with self.assertRaises(ValueError):
            WindowsConPtyHost(
                run_id="run-1",
                command=[sys.executable],
                cwd=self.root,
                run_dir=self.root / "run",
                socket_path=self.root / "control.sock",
                control_token="secret",
            )

    def test_start_returns_pid_and_stop_is_idempotent(self) -> None:
        host = _make_host(self.root)
        pid = host.start()
        self.assertEqual(pid, 424242)
        host.stop()
        host.stop()  # second stop must be a no-op
        self.assertIsNone(host._pty)

    def test_send_input_normalizes_lf_to_crlf(self) -> None:
        host = _make_host(self.root)
        host.start()
        host.send_input("hello\n")
        host.send_input("world\r\n")
        pty = host._pty
        assert pty is not None
        self.assertEqual(pty.written, ["hello\r\n", "world\r\n"])
        host.stop()

    def test_send_input_after_exit_raises(self) -> None:
        from loopweave.windows_terminal_host import WindowsHostError

        host = _make_host(self.root)
        host.start()
        assert host._pty is not None
        host._pty.alive = False
        with self.assertRaises(WindowsHostError):
            host.send_input("nope\n")
        host.stop()

    def test_process_identity_returns_pid_and_start_time(self) -> None:
        host = _make_host(self.root)
        host.start()
        with mock.patch(
            "loopweave.windows_terminal_host.process_start_time",
            return_value="2026-07-31T00:00:00+00:00",
        ):
            identity = host.process_identity()
        self.assertEqual(identity, (424242, "2026-07-31T00:00:00+00:00"))
        host.stop()

    def test_resize_records_terminal_event(self) -> None:
        host = _make_host(self.root)
        host.start()
        host.resize(30, 120)
        events = (self.root / "run" / "terminal-events.jsonl").read_text(
            encoding="utf-8"
        )
        self.assertIn("terminal_resized", events)
        host.stop()


@unittest.skipUnless(sys.platform == "win32", "Windows ConPTY required")
class WindowsConPtyRealTests(unittest.TestCase):
    """Real ConPTY + named-pipe control channel on Windows."""

    def setUp(self) -> None:
        try:
            import winpty  # noqa: F401
        except ImportError:
            self.skipTest("winpty (pywinpty) is not installed")
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def _fixture(self, body: str) -> Path:
        path = self.root / "fixture.py"
        path.write_text(body, encoding="utf-8")
        return path

    def _host(self, command):
        from loopweave.windows_terminal_host import WindowsConPtyHost

        return WindowsConPtyHost(
            run_id=f"run-real-{uuid.uuid4().hex[:6]}",
            command=command,
            cwd=self.root,
            run_dir=self.root / "run",
            socket_path=Path(
                rf"\\.\pipe\loopweave-control-real-{uuid.uuid4().hex[:10]}"
            ),
            control_token="secret-token",
            passthrough=False,
        )

    def _wait_for_text(self, path: Path, text: str, timeout: float = 10.0) -> str:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if path.exists():
                content = path.read_text(encoding="utf-8", errors="replace")
                if text in content:
                    return content
            time.sleep(0.05)
        self.fail(f"timed out waiting for {text!r} in {path}")

    def test_python_child_output_reaches_terminal_log(self) -> None:
        fixture = self._fixture(
            "import sys\nprint('READY_REAL', flush=True)\n"
        )
        host = self._host([sys.executable, "-u", str(fixture)])
        try:
            host.start()
            self._wait_for_text(self.root / "run" / "terminal.txt", "READY_REAL")
        finally:
            host.stop()

    def test_input_round_trip_via_control_channel(self) -> None:
        log = self.root / "child.log"
        log_s = str(log).replace("\\", "\\\\")
        fixture = self._fixture(
            "\n".join(
                [
                    "import sys",
                    f"out = open(r'{log_s}', 'w', encoding='utf-8')",
                    "out.write('STARTED\\n'); out.flush()",
                    "for line in sys.stdin:",
                    "    out.write('GOT=' + repr(line) + '\\n'); out.flush()",
                    "    if line.strip() == 'QUIT':",
                    "        break",
                    "out.write('DONE\\n'); out.flush()",
                ]
            )
        )
        host = self._host([sys.executable, "-u", str(fixture)])
        try:
            host.start()
            time.sleep(1.5)
            from loopweave.control_transport import send_control_message

            endpoint = host.pipe_name
            response = send_control_message(
                endpoint,
                {"token": "secret-token", "action": "send", "text": "HELLO\n"},
                timeout=3.0,
            )
            self.assertEqual(response["status"], "ok")
            send_control_message(
                endpoint,
                {"token": "secret-token", "action": "send", "text": "QUIT\n"},
                timeout=3.0,
            )
            deadline = time.monotonic() + 8
            while time.monotonic() < deadline and not log.exists():
                time.sleep(0.05)
            content = self._wait_for_text(log, "DONE")
            self.assertIn("GOT='HELLO\\n'", content)
        finally:
            host.stop()

    def test_wrong_control_token_is_rejected(self) -> None:
        fixture = self._fixture(
            "import time\nprint('READY', flush=True)\ntime.sleep(20)\n"
        )
        host = self._host([sys.executable, "-u", str(fixture)])
        try:
            host.start()
            time.sleep(1.5)
            from loopweave.control_transport import send_control_message

            response = send_control_message(
                host.pipe_name,
                {"token": "wrong", "action": "status"},
                timeout=3.0,
            )
            self.assertEqual(response["status"], "error")
        finally:
            host.stop()

    def test_stop_terminates_stubborn_child_and_is_idempotent(self) -> None:
        fixture = self._fixture(
            "import time\nprint('STARTED', flush=True)\ntime.sleep(600)\n"
        )
        host = self._host([sys.executable, "-u", str(fixture)])
        host.start()
        time.sleep(1.5)
        self.assertTrue(host._is_alive())
        started = time.monotonic()
        host.stop()
        self.assertLess(time.monotonic() - started, 15.0)
        self.assertFalse(host._is_alive())
        host.stop()

    def test_process_identity_matches_started_pid(self) -> None:
        fixture = self._fixture(
            "import time\nprint('READY', flush=True)\ntime.sleep(10)\n"
        )
        host = self._host([sys.executable, "-u", str(fixture)])
        try:
            pid = host.start()
            identity_pid, start = host.process_identity()
            self.assertEqual(identity_pid, pid)
            self.assertIn("2026", start)
        finally:
            host.stop()


if __name__ == "__main__":
    unittest.main()
