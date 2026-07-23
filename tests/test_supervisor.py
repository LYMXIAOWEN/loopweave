from __future__ import annotations

import json
import sys
import tempfile
import time
import unittest
from pathlib import Path

from loopweave.supervisor import Supervisor, send_control_message


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

    def test_transcript_is_recorded(self) -> None:
        self.supervisor.send_input("record-me\n")
        self._wait_for("text=record-me")

        self.assertTrue((self.root / "run" / "terminal.raw.log").exists())
        self.assertIn(
            "text=record-me",
            (self.root / "run" / "terminal.txt").read_text(
                encoding="utf-8", errors="replace"
            ),
        )

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
