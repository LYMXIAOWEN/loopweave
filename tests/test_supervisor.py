from __future__ import annotations

import json
import sys
import tempfile
import time
import unittest
from pathlib import Path

from loopweave.supervisor import Supervisor, send_control_message
from loopweave.runtime_config import RunPolicy


@unittest.skipIf(sys.platform == "win32", "POSIX pty required")
class SupervisorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.socket_path = self.root / "control.sock"
        fixture = Path(__file__).parent / "fixtures" / "echo_agent.py"
        self.supervisor = Supervisor(
            run_id="run-1",
            command=[sys.executable, "-u", str(fixture)],
            cwd=self.root,
            run_dir=self.root / "run",
            socket_path=self.socket_path,
            control_token="secret",
            passthrough=False,
        )
        self.pid = self.supervisor.start()
        self._wait_for("READY pid={}".format(self.pid))

    def tearDown(self) -> None:
        self.supervisor.stop()
        self.temp_dir.cleanup()

    def test_user_input_reaches_managed_child(self) -> None:
        self.supervisor.send_input("hello\n")

        output = self._wait_for("text=hello")

        self.assertIn("pid={}".format(self.pid), output)

    def test_review_reaches_same_managed_child(self) -> None:
        response = send_control_message(
            self.socket_path,
            {"token": "secret", "action": "send", "text": "REVIEW VERDICT\n"},
        )

        self.assertEqual(response["status"], "ok")
        output = self._wait_for("text=REVIEW VERDICT")
        self.assertIn("pid={}".format(self.pid), output)

    def test_wrong_control_token_is_rejected(self) -> None:
        response = send_control_message(
            self.socket_path,
            {"token": "wrong", "action": "send", "text": "DO NOT SEND\n"},
        )

        self.assertEqual(response["status"], "error")
        time.sleep(0.1)
        self.assertNotIn("DO NOT SEND", self._output())

    def test_status_reports_terminal_output_activity_for_readiness(self) -> None:
        response = send_control_message(
            self.socket_path,
            {"token": "secret", "action": "status"},
        )

        self.assertEqual(response["status"], "ok")
        self.assertEqual(response["run_id"], "run-1")
        self.assertEqual(response["pid"], self.pid)
        self.assertGreater(response["terminal_output_bytes"], 0)
        self.assertIsInstance(response["terminal_idle_seconds"], float)
        self.assertGreaterEqual(response["terminal_idle_seconds"], 0)

    def test_transcript_is_recorded(self) -> None:
        self.supervisor.send_input("record-me\n")
        self._wait_for("text=record-me")

        self.assertFalse((self.root / "run" / "terminal.raw.log").exists())
        self.assertIn(
            "text=record-me",
            (self.root / "run" / "terminal.txt").read_text(
                encoding="utf-8", errors="replace"
            ),
        )

    def test_raw_transcript_requires_explicit_opt_in(self) -> None:
        self.supervisor.stop()
        fixture = Path(__file__).parent / "fixtures" / "echo_agent.py"
        self.supervisor = Supervisor(
            run_id="run-raw",
            command=[sys.executable, "-u", str(fixture)],
            cwd=self.root,
            run_dir=self.root / "raw-run",
            socket_path=self.root / "raw-control.sock",
            control_token="secret",
            passthrough=False,
            log_policy=RunPolicy(raw_log_enabled=True),
        )
        self.pid = self.supervisor.start()
        deadline = time.time() + 3
        raw_path = self.root / "raw-run" / "terminal.raw.log"
        while time.time() < deadline and not raw_path.exists():
            time.sleep(0.02)
        self.assertTrue(raw_path.exists())

    def test_terminal_log_rotation_is_bounded(self) -> None:
        path = self.root / "rotating.txt"
        policy = RunPolicy(terminal_log_max_bytes=1024, terminal_log_backups=2)
        probe = Supervisor(
            run_id="run-rotation",
            command=[sys.executable, "-c", "pass"],
            cwd=self.root,
            run_dir=self.root / "rotation-run",
            socket_path=self.root / "rotation.sock",
            control_token="secret",
            passthrough=False,
            log_policy=policy,
        )
        path.write_bytes(b"a" * 900)
        probe._append_rotating_log(
            path,
            b"b" * 200,
            max_bytes=policy.terminal_log_max_bytes,
            backups=policy.terminal_log_backups,
            log_kind="terminal.txt",
        )
        self.assertEqual(path.stat().st_size, 200)
        self.assertEqual(path.with_name("rotating.txt.1").stat().st_size, 900)

    def test_terminal_diagnostics_record_initial_size_when_available(self) -> None:
        diagnostics = self.root / "run" / "terminal-events.jsonl"
        self.assertTrue(diagnostics.exists())
        content = diagnostics.read_text(encoding="utf-8")
        self.assertIn("terminal_started", content)

    def test_record_resize_event_writes_terminal_diagnostic(self) -> None:
        self.supervisor._record_terminal_event(
            "terminal_resized",
            {"rows": 40, "columns": 120},
        )

        diagnostics = self.root / "run" / "terminal-events.jsonl"
        events = [
            json.loads(line)
            for line in diagnostics.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        self.assertEqual(events[-1]["event"], "terminal_resized")
        self.assertEqual(events[-1]["rows"], 40)
        self.assertEqual(events[-1]["columns"], 120)

    def test_resize_child_pty_records_resize_event_when_master_exists(self) -> None:
        self.supervisor._resize_child_pty(
            {"rows": 33, "columns": 101, "xpixel": 0, "ypixel": 0}
        )

        diagnostics = self.root / "run" / "terminal-events.jsonl"
        self.assertIn("terminal_resized", diagnostics.read_text(encoding="utf-8"))

    def _output(self) -> str:
        path = self.root / "run" / "terminal.txt"
        if not path.exists():
            return ""
        return path.read_text(encoding="utf-8", errors="replace")

    def _wait_for(self, text: str, timeout: float = 3.0) -> str:
        deadline = time.time() + timeout
        while time.time() < deadline:
            output = self._output()
            if text in output:
                return output
            time.sleep(0.02)
        self.fail("Timed out waiting for {!r}. Output: {!r}".format(text, self._output()))


if __name__ == "__main__":
    unittest.main()
