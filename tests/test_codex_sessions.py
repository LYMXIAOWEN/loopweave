from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from loopweave.codex_sessions import (
    AmbiguousThread,
    discover_thread,
)


class CodexSessionDiscoveryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.sessions_dir = Path(self.temp_dir.name)
        self.now = datetime(2026, 6, 18, 10, 0, tzinfo=timezone.utc)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_explicit_thread_id_is_exact(self) -> None:
        self._session("thread-a", "/tmp/a", self.now - timedelta(days=2))
        self._session("thread-b", "/tmp/a", self.now)

        result = discover_thread(
            self.sessions_dir,
            cwd="/tmp/a",
            explicit_thread_id="thread-a",
            now=self.now,
        )

        self.assertEqual(result.thread_id, "thread-a")

    def test_unique_recent_cwd_match_is_selected(self) -> None:
        self._session("thread-a", "/tmp/a", self.now - timedelta(minutes=2))
        self._session("thread-b", "/tmp/b", self.now)

        result = discover_thread(self.sessions_dir, cwd="/tmp/a", now=self.now)

        self.assertEqual(result.thread_id, "thread-a")

    def test_unique_stale_cwd_match_is_selected(self) -> None:
        self._session("thread-a", "/tmp/a", self.now - timedelta(hours=8))

        result = discover_thread(
            self.sessions_dir,
            cwd="/tmp/a",
            now=self.now,
            max_age_seconds=3600,
        )

        self.assertEqual(result.thread_id, "thread-a")

    def test_multiple_stale_cwd_matches_are_rejected(self) -> None:
        self._session("thread-a", "/tmp/a", self.now - timedelta(hours=8))
        self._session("thread-b", "/tmp/a", self.now - timedelta(hours=7))

        with self.assertRaises(AmbiguousThread):
            discover_thread(
                self.sessions_dir,
                cwd="/tmp/a",
                now=self.now,
                max_age_seconds=3600,
            )

    def test_multiple_recent_cwd_matches_are_rejected(self) -> None:
        self._session("thread-a", "/tmp/a", self.now - timedelta(minutes=2))
        self._session("thread-b", "/tmp/a", self.now - timedelta(minutes=1))

        with self.assertRaises(AmbiguousThread):
            discover_thread(self.sessions_dir, cwd="/tmp/a", now=self.now)

    def test_recent_activity_keeps_an_old_thread_discoverable(self) -> None:
        path = self._session(
            "thread-a", "/tmp/a", self.now - timedelta(days=2)
        )
        with path.open("a", encoding="utf-8") as handle:
            handle.write(
                json.dumps(
                    {
                        "timestamp": self.now.isoformat().replace("+00:00", "Z"),
                        "type": "event_msg",
                        "payload": {"type": "task_started"},
                    }
                )
                + "\n"
            )

        result = discover_thread(self.sessions_dir, cwd="/tmp/a", now=self.now)

        self.assertEqual(result.thread_id, "thread-a")

    def _session(self, thread_id: str, cwd: str, timestamp: datetime) -> Path:
        path = self.sessions_dir / "{}.jsonl".format(thread_id)
        payload = {
            "timestamp": timestamp.isoformat().replace("+00:00", "Z"),
            "type": "session_meta",
            "payload": {
                "id": thread_id,
                "timestamp": timestamp.isoformat().replace("+00:00", "Z"),
                "cwd": cwd,
                "originator": "Codex Desktop",
            },
        }
        path.write_text(json.dumps(payload) + "\n", encoding="utf-8")
        return path


if __name__ == "__main__":
    unittest.main()
