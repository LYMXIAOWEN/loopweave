from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from loopweave.cli import build_parser
from loopweave.models import ReviewBackend, RunMode, RunRecord, RunState
from loopweave.protocol import append_event, write_json_atomic
from loopweave.registry import Registry
from loopweave.task_continuity import (
    TaskContinuityError,
    adopt_task,
    recovery_guidance,
)


class TaskContinuityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.registry = Registry(self.root / "registry.sqlite")
        self.workspace = self.root / "workspace"
        self.workspace.mkdir()

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _run(
        self,
        run_id: str,
        *,
        state: RunState = RunState.RUNNING,
        **overrides: object,
    ) -> RunRecord:
        run_dir = self.root / run_id
        run_dir.mkdir()
        values = {
            "run_id": run_id,
            "codex_thread_id": "worker-thread",
            "cwd": str(self.workspace),
            "thread_cwd": str(self.workspace),
            "workspace_root": str(self.workspace),
            "project_slug": "example-project",
            "project_root": str(self.root / "project"),
            "tty": "/dev/ttys001",
            "agent": "codex",
            "agent_pid": 123,
            "agent_process_start": "process-start",
            "control_token": "secret",
            "state": state,
            "mode": RunMode.DEVELOP,
            "reviewer_backend": ReviewBackend.VISIBLE_THREAD,
            "reviewer_thread_id": "review-thread",
            "reviewer_thread_cwd": str(self.root),
            "socket_path": str(self.root / (run_id + ".sock")),
            "run_dir": str(run_dir),
        }
        values.update(overrides)
        run = RunRecord(**values)
        self.registry.create_run(run)
        write_json_atomic(
            run_dir / "run.json",
            {"schema_version": 1, "run_id": run_id},
        )
        return run

    def _record_task(self, run: RunRecord, text: str = "# Task\n\nContinue.\n") -> str:
        latest = Path(run.run_dir) / "assigned-task-latest.md"
        data = text.encode("utf-8")
        latest.write_bytes(data)
        digest = hashlib.sha256(data).hexdigest()
        append_event(
            Path(run.run_dir) / "events.jsonl",
            {
                "event": "task_assigned",
                "run_id": run.run_id,
                "latest_task_path": str(latest),
                "sha256": digest,
            },
        )
        return digest

    @staticmethod
    def _reader(_pid: int) -> str:
        return "process-start"

    @staticmethod
    def _sender(_socket: Path, payload: dict, _timeout: float = 2.0) -> dict:
        return {
            "status": "ok",
            "run_id": payload.get("_run_id", "run-target"),
            "pid": 123,
            "running": True,
        }

    def _adopt(self, target: RunRecord, source: RunRecord):
        return adopt_task(
            self.registry,
            target.run_id,
            source.run_id,
            process_start_reader=self._reader,
            control_sender=lambda _socket, _payload, _timeout=2.0: {
                "status": "ok",
                "run_id": target.run_id,
                "pid": target.agent_pid,
                "running": True,
            },
        )

    def test_adopt_task_copies_exact_packet_and_is_idempotent(self) -> None:
        source = self._run("run-source", state=RunState.STOPPED)
        target = self._run("run-target", state=RunState.NEEDS_HUMAN)
        digest = self._record_task(source)

        first = self._adopt(target, source)
        second = self._adopt(self.registry.get_run(target.run_id), source)

        self.assertFalse(first.duplicate)
        self.assertTrue(second.duplicate)
        self.assertEqual(first.sha256, digest)
        self.assertEqual(
            first.latest_path.read_bytes(),
            (Path(source.run_dir) / "assigned-task-latest.md").read_bytes(),
        )
        self.assertEqual(
            self.registry.get_run(target.run_id).state,
            RunState.WORKER_CONTINUING,
        )
        payload = json.loads(
            (Path(target.run_dir) / "run.json").read_text(encoding="utf-8")
        )
        self.assertEqual(payload["task_continuity"]["source_run_id"], source.run_id)
        events = [
            json.loads(line)
            for line in (Path(target.run_dir) / "events.jsonl")
            .read_text(encoding="utf-8")
            .splitlines()
        ]
        self.assertEqual(
            sum(event["event"] == "task_adopted" for event in events),
            1,
        )
        self.assertEqual(
            sum(event["event"] == "task_adoption_delivered" for event in events),
            1,
        )

    def test_adopt_task_delivers_packet_and_submit_key_exactly_once(self) -> None:
        source = self._run("run-source", state=RunState.STOPPED)
        target = self._run("run-target")
        self._record_task(source, "# Task\n\nContinue this exact task.\n")
        sent = []

        def sender(_socket: Path, payload: dict, _timeout: float = 2.0) -> dict:
            if payload["action"] == "status":
                return {
                    "status": "ok",
                    "run_id": target.run_id,
                    "pid": target.agent_pid,
                    "running": True,
                }
            sent.append(payload["text"])
            return {"status": "ok"}

        first = adopt_task(
            self.registry,
            target.run_id,
            source.run_id,
            process_start_reader=self._reader,
            control_sender=sender,
        )
        second = adopt_task(
            self.registry,
            target.run_id,
            source.run_id,
            process_start_reader=self._reader,
            control_sender=sender,
        )

        self.assertFalse(first.duplicate)
        self.assertTrue(second.duplicate)
        self.assertEqual(len(sent), 2)
        self.assertIn("[LoopWeave assignment]", sent[0])
        self.assertIn("Continue this exact task.", sent[0])
        self.assertEqual(sent[1], "\r")

    def test_adopt_task_resumes_delivery_after_submit_key_failure(self) -> None:
        source = self._run("run-source", state=RunState.STOPPED)
        target = self._run("run-target")
        self._record_task(source)
        first_attempt = []

        def failing_sender(
            _socket: Path, payload: dict, _timeout: float = 2.0
        ) -> dict:
            if payload["action"] == "status":
                return {
                    "status": "ok",
                    "run_id": target.run_id,
                    "pid": target.agent_pid,
                    "running": True,
                }
            first_attempt.append(payload["text"])
            if payload["text"] == "\r":
                raise RuntimeError("submit key unavailable")
            return {"status": "ok"}

        with self.assertRaisesRegex(
            TaskContinuityError, "submit key unavailable"
        ):
            adopt_task(
                self.registry,
                target.run_id,
                source.run_id,
                process_start_reader=self._reader,
                control_sender=failing_sender,
            )

        self.assertTrue(
            (Path(target.run_dir) / "assigned-task-latest.md").is_file()
        )
        self.assertEqual(len(first_attempt), 2)
        resumed = []

        def recovery_sender(
            _socket: Path, payload: dict, _timeout: float = 2.0
        ) -> dict:
            if payload["action"] == "status":
                return {
                    "status": "ok",
                    "run_id": target.run_id,
                    "pid": target.agent_pid,
                    "running": True,
                }
            resumed.append(payload["text"])
            return {"status": "ok"}

        result = adopt_task(
            self.registry,
            target.run_id,
            source.run_id,
            process_start_reader=self._reader,
            control_sender=recovery_sender,
        )

        self.assertTrue(result.duplicate)
        self.assertEqual(resumed, ["\r"])
        events = [
            json.loads(line)
            for line in (Path(target.run_dir) / "events.jsonl")
            .read_text(encoding="utf-8")
            .splitlines()
        ]
        self.assertEqual(
            sum(event["event"] == "task_adopted" for event in events),
            1,
        )
        self.assertEqual(
            sum(event["event"] == "task_adoption_delivered" for event in events),
            1,
        )

    def test_adopt_task_recovers_event_to_latest_file_crash_window(self) -> None:
        source = self._run("run-source", state=RunState.STOPPED)
        target = self._run("run-target")
        digest = self._record_task(source)
        append_event(
            Path(target.run_dir) / "events.jsonl",
            {
                "event": "task_adopted",
                "run_id": target.run_id,
                "source_run_id": source.run_id,
                "latest_task_path": str(
                    Path(target.run_dir) / "assigned-task-latest.md"
                ),
                "sha256": digest,
            },
        )

        result = self._adopt(target, source)

        self.assertTrue(result.duplicate)
        self.assertTrue(result.latest_path.is_file())

    def test_adopt_task_rejects_identity_and_control_failures(self) -> None:
        source = self._run("run-source", state=RunState.STOPPED)
        target = self._run("run-target")
        self._record_task(source)

        with self.assertRaisesRegex(TaskContinuityError, "identity changed"):
            adopt_task(
                self.registry,
                target.run_id,
                source.run_id,
                process_start_reader=lambda _pid: "different",
            )
        with self.assertRaisesRegex(TaskContinuityError, "control channel"):
            adopt_task(
                self.registry,
                target.run_id,
                source.run_id,
                process_start_reader=self._reader,
                control_sender=lambda *_args: {
                    "status": "ok",
                    "run_id": target.run_id,
                    "pid": target.agent_pid,
                    "running": False,
                },
            )

    def test_adopt_task_rejects_compatibility_or_packet_mismatch(self) -> None:
        source = self._run("run-source", state=RunState.STOPPED)
        target = self._run("run-target", agent="another-agent")
        self._record_task(source)
        with self.assertRaisesRegex(TaskContinuityError, "agent"):
            self._adopt(target, source)

        compatible = self._run("run-compatible")
        latest = Path(source.run_dir) / "assigned-task-latest.md"
        latest.write_text("changed", encoding="utf-8")
        with self.assertRaisesRegex(TaskContinuityError, "digest changed"):
            self._adopt(compatible, source)

    def test_recovery_guidance_never_guesses_between_sources(self) -> None:
        first = self._run("run-source-1", state=RunState.STOPPED)
        second = self._run("run-source-2", state=RunState.STOPPED)
        target = self._run("run-target")
        self._record_task(first, "first")
        self._record_task(second, "second")

        guidance = recovery_guidance(self.registry, target)

        self.assertIn("multiple compatible source runs found", guidance)
        self.assertIn("run-source-1", guidance)
        self.assertIn("run-source-2", guidance)
        self.assertIn("--from-run <source-run-id>", guidance)

    def test_cli_parses_continuity_without_forwarding_it(self) -> None:
        parser = build_parser()

        run_args = parser.parse_args(
            [
                "run",
                "codex",
                "--continue-run",
                "run-source",
                "--model",
                "example",
            ]
        )
        adopt_args = parser.parse_args(
            [
                "adopt-task",
                "--run-id",
                "run-target",
                "--from-run",
                "run-source",
            ]
        )

        self.assertEqual(run_args.continue_run, "run-source")
        self.assertEqual(run_args.agent_args, ["--model", "example"])
        self.assertEqual(adopt_args.run_id, "run-target")
        self.assertEqual(adopt_args.from_run, "run-source")

        with self.assertRaises(SystemExit):
            parser.parse_args(
                [
                    "run",
                    "codex",
                    "--task-file",
                    "/tmp/task.md",
                    "--continue-run",
                    "run-source",
                ]
            )


if __name__ == "__main__":
    unittest.main()
