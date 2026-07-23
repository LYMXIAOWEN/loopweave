from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from loopweave.completion_notifier import CompletionNotifier
from loopweave.models import RunRecord, RunState


class CompletionNotifierTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.run_dir = self.root / "run-1"
        self.run_dir.mkdir()
        self.control_root = self.root / "control"
        self.control_root.mkdir()
        self.workspace_root = self.root / "workspace"
        self.workspace_root.mkdir()
        self.sent = []
        self.commands = []
        self.run = RunRecord(
            run_id="run-1",
            codex_thread_id="thread-1",
            cwd=str(self.workspace_root),
            thread_cwd=str(self.control_root),
            workspace_root=str(self.workspace_root),
            tty="/dev/test",
            agent="claude",
            agent_pid=123,
            agent_process_start="worker-start",
            control_token="secret",
            state=RunState.APPROVED,
            socket_path=str(self.root / "control.sock"),
            run_dir=str(self.run_dir),
        )
        self.review = {
            "schema_version": 1,
            "run_id": "run-1",
            "review_id": "review-1",
            "verdict": "approved",
            "summary": "All required checks passed.",
            "review_file": "reviewer-verdict.md",
            "continue": False,
        }

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_worker_notification_is_explicit_and_idempotent(self) -> None:
        notifier = self._notifier()
        inputs = [
            "[LoopWeave review: approved]\nThe task is complete.\n",
            "\r",
        ]

        first = notifier.notify_worker(self.run, self.review, inputs)
        second = notifier.notify_worker(self.run, self.review, inputs)

        self.assertTrue(first)
        self.assertTrue(second)
        self.assertEqual(
            [payload["text"] for _, payload in self.sent],
            inputs,
        )
        self.assertEqual(
            (self.run_dir / "worker-approved-review-id")
            .read_text(encoding="utf-8")
            .strip(),
            "review-1",
        )
        events = self._events()
        self.assertEqual(
            [event["event"] for event in events],
            ["worker_approval_notified"],
        )

    def test_worker_notification_rejects_changed_process_identity(self) -> None:
        notifier = self._notifier(process_start=lambda pid: "other-start")

        result = notifier.notify_worker(
            self.run,
            self.review,
            ["approved", "\r"],
        )

        self.assertFalse(result)
        self.assertEqual(self.sent, [])
        self.assertFalse(
            (self.run_dir / "worker-approved-review-id").exists()
        )
        self.assertEqual(
            self._events()[-1]["event"],
            "worker_approval_notification_failed",
        )

    def test_owner_notification_writes_bounded_pending_artifacts_only(self) -> None:
        (self.run_dir / "worker-approved-review-id").write_text(
            "review-1\n", encoding="utf-8"
        )
        notifier = self._notifier()

        first = notifier.notify_owner(self.run, self.review)
        second = notifier.notify_owner(self.run, self.review)

        self.assertTrue(first)
        self.assertTrue(second)
        self.assertEqual(self.commands, [])
        self.assertEqual(
            (self.run_dir / "owner-completion-review-id")
            .read_text(encoding="utf-8")
            .strip(),
            "review-1",
        )
        metadata = json.loads(
            (self.run_dir / "completion-notification.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertTrue(metadata["worker_notified"])
        self.assertFalse(metadata["owner_notified"])
        self.assertTrue(metadata["owner_review_pending"])
        self.assertIn("loopweave finalize --run-id run-1 --approve", metadata["owner_action_required"])
        self.assertNotIn("control_token", metadata)

    def test_notify_retries_only_missing_channel(self) -> None:
        notifier = self._notifier()
        inputs = ["approved", "\r"]

        first = notifier.notify(self.run, self.review, inputs)
        second = notifier.notify(self.run, self.review, inputs)

        self.assertEqual(
            first,
            {"worker_notified": True, "owner_notified": True},
        )
        self.assertEqual(
            second,
            {"worker_notified": True, "owner_notified": True},
        )
        self.assertEqual(len(self.sent), 2)
        self.assertEqual(self.commands, [])

    def _notifier(self, process_start=None):
        def sender(path, payload):
            self.sent.append((path, payload))
            return {"status": "ok"}

        return CompletionNotifier(
            sender=sender,
            process_start=process_start or (lambda pid: "worker-start"),
            sleep=lambda seconds: None,
        )

    def _events(self):
        path = self.run_dir / "events.jsonl"
        if not path.exists():
            return []
        return [
            json.loads(line)
            for line in path.read_text(encoding="utf-8").splitlines()
        ]


if __name__ == "__main__":
    unittest.main()
