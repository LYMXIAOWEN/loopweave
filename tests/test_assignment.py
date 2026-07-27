from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from loopweave.assignment import (
    AssignmentError,
    MAX_TASK_FILE_BYTES,
    format_assignment_message,
    resolve_latest_assignable_run,
    validate_task_file,
)
from loopweave.models import ReviewBackend, RunMode, RunRecord, RunState


def make_run(run_id: str, state: RunState, row: int) -> RunRecord:
    return RunRecord(
        run_id=run_id,
        codex_thread_id="thread-" + run_id,
        cwd="/tmp/thread",
        thread_cwd="/tmp/thread",
        workspace_root="/tmp/workspace",
        tty="/dev/ttys001",
        agent="claude",
        agent_pid=1000 + row,
        agent_process_start="Mon Jan  1 00:00:0{} 2024".format(row),
        control_token="token-" + run_id,
        state=state,
        mode=RunMode.DEVELOP,
        socket_path="/tmp/" + run_id + ".sock",
        run_dir="/tmp/" + run_id,
    )


class AssignmentValidationTests(unittest.TestCase):
    def test_validate_task_file_returns_content_size_and_digest(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "task.md"
            path.write_text("# Task\n\nDo the work.\n", encoding="utf-8")

            task = validate_task_file(path)

            self.assertEqual(task.path, path.resolve())
            self.assertEqual(task.text, "# Task\n\nDo the work.\n")
            self.assertEqual(task.size, len("# Task\n\nDo the work.\n".encode("utf-8")))
            self.assertEqual(len(task.sha256), 64)

    def test_validate_task_file_rejects_missing_directory_empty_and_oversized(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            empty = root / "empty.md"
            empty.write_text("", encoding="utf-8")
            oversized = root / "oversized.md"
            oversized.write_bytes(b"x" * (MAX_TASK_FILE_BYTES + 1))

            cases = [
                root / "missing.md",
                root,
                empty,
                oversized,
            ]

            for path in cases:
                with self.subTest(path=str(path)):
                    with self.assertRaises(AssignmentError):
                        validate_task_file(path)

    def test_resolve_latest_assignable_run_requires_exactly_one_candidate(self) -> None:
        selected = resolve_latest_assignable_run(
            [
                make_run("run-stopped", RunState.STOPPED, 1),
                make_run("run-live", RunState.RUNNING, 2),
                make_run("run-approved", RunState.APPROVED, 3),
            ]
        )

        self.assertEqual(selected.run_id, "run-live")

    def test_resolve_latest_assignable_run_rejects_no_or_multiple_candidates(self) -> None:
        with self.assertRaisesRegex(AssignmentError, "no assignable live run"):
            resolve_latest_assignable_run([make_run("run-stopped", RunState.STOPPED, 1)])

        with self.assertRaisesRegex(AssignmentError, "multiple assignable live runs"):
            resolve_latest_assignable_run(
                [
                    make_run("run-a", RunState.RUNNING, 1),
                    make_run("run-b", RunState.WORKER_CONTINUING, 2),
                ]
            )

    def test_assignment_message_is_delimited_and_contains_packet(self) -> None:
        message = format_assignment_message("Objective\nVerify.\n")

        self.assertIn("[LoopWeave assignment]", message)
        self.assertIn("Objective\nVerify.", message)
        self.assertTrue(message.endswith("\n"))

    def test_assign_task_writes_artifacts_event_and_sends_message(self) -> None:
        from loopweave.assignment import assign_task

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            task = root / "task.md"
            task.write_text("# Objective\n\nShip assign.\n", encoding="utf-8")
            run_dir = root / "run"
            socket_path = root / "control.sock"
            socket_path.write_text("", encoding="utf-8")
            run = make_run("run-live", RunState.RUNNING, 1)
            run = RunRecord(**{**run.__dict__, "run_dir": str(run_dir), "socket_path": str(socket_path)})
            sent = []

            result = assign_task(
                run,
                task,
                sender=lambda path, payload: sent.append((path, payload)) or {"status": "ok"},
                process_start_reader=lambda pid: run.agent_process_start,
                timestamp_factory=lambda: "20260627T010203Z",
            )

            assigned = run_dir / "assigned-task-20260627T010203Z.md"
            latest = run_dir / "assigned-task-latest.md"
            self.assertEqual(result.task_path, assigned)
            self.assertEqual(result.latest_path, latest)
            self.assertTrue(assigned.exists())
            self.assertEqual(latest.read_text(encoding="utf-8"), task.read_text(encoding="utf-8"))
            self.assertEqual(sent[0][1]["action"], "status")
            delivered = [entry for entry in sent if entry[1]["action"] == "send"]
            self.assertEqual(len(delivered), 2)
            self.assertEqual(delivered[0][0], socket_path)
            self.assertEqual(delivered[0][1]["token"], run.control_token)
            self.assertIn("[LoopWeave assignment]", delivered[0][1]["text"])
            events = [
                json.loads(line)
                for line in (run_dir / "events.jsonl").read_text(encoding="utf-8").splitlines()
            ]
            self.assertEqual(events[-1]["event"], "task_assigned")
            self.assertEqual(events[-1]["sha256"], result.sha256)

    def test_assign_task_does_not_arm_external_visible_review_wakeup(
        self,
    ) -> None:
        from loopweave.assignment import assign_task

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            task = root / "task.md"
            task.write_text("# Objective\n\nShip visible review.\n", encoding="utf-8")
            run_dir = root / "run"
            socket_path = root / "control.sock"
            socket_path.write_text("", encoding="utf-8")
            run = make_run("run-live", RunState.RUNNING, 1)
            run = RunRecord(
                **{
                    **run.__dict__,
                    "run_dir": str(run_dir),
                    "socket_path": str(socket_path),
                    "reviewer_backend": ReviewBackend.VISIBLE_THREAD,
                    "reviewer_thread_id": "thread-visible",
                    "reviewer_thread_cwd": str(root),
                }
            )

            assign_task(
                run,
                task,
                sender=lambda path, payload: {"status": "ok"},
                process_start_reader=lambda pid: run.agent_process_start,
                timestamp_factory=lambda: "20260702T010203Z",
            )

            self.assertFalse((run_dir / "visible-review-heartbeat.json").exists())
            events = [
                json.loads(line)
                for line in (run_dir / "events.jsonl")
                .read_text(encoding="utf-8")
                .splitlines()
            ]
            self.assertEqual(events[-1]["event"], "task_assigned")

    def test_assign_task_rejects_non_assignable_state_and_process_identity_mismatch(self) -> None:
        from loopweave.assignment import assign_task

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            task = root / "task.md"
            task.write_text("# Task\n", encoding="utf-8")
            stopped = make_run("run-stopped", RunState.STOPPED, 1)
            stopped = RunRecord(**{**stopped.__dict__, "run_dir": str(root / "run")})

            with self.assertRaisesRegex(AssignmentError, "not assignable"):
                assign_task(
                    stopped,
                    task,
                    sender=lambda path, payload: {"status": "ok"},
                    process_start_reader=lambda pid: stopped.agent_process_start,
                )

            live = make_run("run-live", RunState.RUNNING, 2)
            live = RunRecord(
                **{
                    **live.__dict__,
                    "run_dir": str(root / "live-run"),
                    "socket_path": str(root / "control.sock"),
                }
            )
            Path(live.socket_path).write_text("", encoding="utf-8")

            with self.assertRaisesRegex(AssignmentError, "process identity changed"):
                assign_task(
                    live,
                    task,
                    sender=lambda path, payload: {"status": "ok"},
                    process_start_reader=lambda pid: "different process",
                )

    def test_delivery_failure_leaves_artifacts_and_event(self) -> None:
        from loopweave.assignment import assign_task

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            task = root / "task.md"
            task.write_text("# Task\n", encoding="utf-8")
            run = make_run("run-live", RunState.RUNNING, 1)
            run = RunRecord(
                **{
                    **run.__dict__,
                    "run_dir": str(root / "run"),
                    "socket_path": str(root / "control.sock"),
                }
            )
            Path(run.socket_path).write_text("", encoding="utf-8")

            with self.assertRaisesRegex(AssignmentError, "assignment delivery failed"):
                assign_task(
                    run,
                    task,
                    sender=lambda path, payload: {"status": "error", "message": "closed"},
                    process_start_reader=lambda pid: run.agent_process_start,
                    timestamp_factory=lambda: "20260627T010203Z",
                )

            run_dir_path = Path(run.run_dir)
            self.assertTrue((run_dir_path / "assigned-task-20260627T010203Z.md").exists())
            events = [
                json.loads(line)
                for line in (run_dir_path / "events.jsonl")
                .read_text(encoding="utf-8")
                .splitlines()
                if line.strip()
            ]
            self.assertTrue(any(event["event"] == "assignment_attempted" for event in events))
            self.assertFalse(any(event["event"] == "task_assigned" for event in events))
            self.assertFalse((run_dir_path / "assigned-task-latest.md").exists())

    def test_duplicate_digest_is_reported_but_allowed(self) -> None:
        from loopweave.assignment import assign_task

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            task = root / "task.md"
            task.write_text("# Same Task\n", encoding="utf-8")
            run = make_run("run-live", RunState.RUNNING, 1)
            run = RunRecord(
                **{
                    **run.__dict__,
                    "run_dir": str(root / "run"),
                    "socket_path": str(root / "control.sock"),
                }
            )
            Path(run.socket_path).write_text("", encoding="utf-8")
            def sender(path, payload):
                return {"status": "ok"}

            first = assign_task(
                run,
                task,
                sender=sender,
                process_start_reader=lambda pid: run.agent_process_start,
                timestamp_factory=lambda: "20260627T010203Z",
            )
            second = assign_task(
                run,
                task,
                sender=sender,
                process_start_reader=lambda pid: run.agent_process_start,
                timestamp_factory=lambda: "20260627T010204Z",
            )

            self.assertFalse(first.duplicate)
            self.assertTrue(second.duplicate)

    def test_assign_task_sends_submit_key_after_packet(self) -> None:
        from loopweave.assignment import assign_task

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            task = root / "task.md"
            task.write_text("# Task\n", encoding="utf-8")
            run = make_run("run-live", RunState.RUNNING, 1)
            run = RunRecord(
                **{
                    **run.__dict__,
                    "run_dir": str(root / "run"),
                    "socket_path": str(root / "control.sock"),
                }
            )
            Path(run.socket_path).write_text("", encoding="utf-8")
            sent = []

            assign_task(
                run,
                task,
                sender=lambda path, payload: sent.append((path, payload)) or {"status": "ok"},
                process_start_reader=lambda pid: run.agent_process_start,
                timestamp_factory=lambda: "20260627T010203Z",
            )

            self.assertEqual(sent[0][1]["action"], "status")
            delivered = [entry for entry in sent if entry[1]["action"] == "send"]
            self.assertEqual(len(delivered), 2)
            self.assertIn("[LoopWeave assignment]", delivered[0][1]["text"])
            self.assertEqual(delivered[1][1]["text"], "\r")
            self.assertEqual(
                delivered[0][1]["token"], delivered[1][1]["token"]
            )

    def test_terminal_readiness_waits_for_real_output_quiet_period(self) -> None:
        from loopweave.assignment import wait_for_terminal_readiness
        from loopweave.runtime_config import RunPolicy

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            run = make_run("run-live", RunState.RUNNING, 1)
            run = RunRecord(
                **{
                    **run.__dict__,
                    "run_dir": str(root / "run"),
                    "socket_path": str(root / "control.sock"),
                }
            )
            responses = [
                {
                    "status": "ok",
                    "run_id": run.run_id,
                    "pid": run.agent_pid,
                    "running": True,
                    "terminal_output_bytes": 128,
                    "terminal_idle_seconds": 0.0,
                },
                {
                    "status": "ok",
                    "run_id": run.run_id,
                    "pid": run.agent_pid,
                    "running": True,
                    "terminal_output_bytes": 256,
                    "terminal_idle_seconds": 0.2,
                },
            ]

            result = wait_for_terminal_readiness(
                run,
                lambda _path, _payload: responses.pop(0),
                events_path=root / "events.jsonl",
                policy=RunPolicy(
                    task_ready_quiet_ms=100,
                    task_ready_fallback_ms=500,
                    task_ready_timeout_ms=1000,
                ),
            )

            self.assertEqual(result["terminal_output_bytes"], 256)
            event = json.loads(
                (root / "events.jsonl").read_text(encoding="utf-8").splitlines()[-1]
            )
            self.assertEqual(event["event"], "terminal_readiness_observed")
            self.assertEqual(event["reason"], "terminal_output_quiet")

    def test_explicit_redelivery_reuses_verified_packet_after_readiness(
        self,
    ) -> None:
        from loopweave.assignment import assign_task

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            task = root / "task.md"
            task.write_text("# Task\n", encoding="utf-8")
            run = make_run("run-live", RunState.RUNNING, 1)
            run = RunRecord(
                **{
                    **run.__dict__,
                    "run_dir": str(root / "run"),
                    "socket_path": str(root / "control.sock"),
                }
            )
            Path(run.socket_path).write_text("", encoding="utf-8")
            sent = []

            def sender(path, payload):
                sent.append((path, payload))
                return {"status": "ok"}

            assign_task(
                run,
                task,
                sender=sender,
                process_start_reader=lambda _pid: run.agent_process_start,
                timestamp_factory=lambda: "20260627T010203Z",
            )
            sent.clear()

            result = assign_task(
                run,
                task,
                sender=sender,
                process_start_reader=lambda _pid: run.agent_process_start,
                redeliver=True,
            )

            self.assertTrue(result.duplicate)
            self.assertTrue(result.redelivered)
            self.assertEqual(sent[0][1]["action"], "status")
            delivered = [entry for entry in sent if entry[1]["action"] == "send"]
            self.assertEqual(len(delivered), 2)
            events = [
                json.loads(line)
                for line in (Path(run.run_dir) / "events.jsonl")
                .read_text(encoding="utf-8")
                .splitlines()
            ]
            self.assertEqual(events[-1]["event"], "task_redelivered")

    def test_assign_task_wraps_sender_exception_as_delivery_failure(self) -> None:
        from loopweave.assignment import assign_task

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            task = root / "task.md"
            task.write_text("# Task\n", encoding="utf-8")
            run = make_run("run-live", RunState.RUNNING, 1)
            run = RunRecord(
                **{
                    **run.__dict__,
                    "run_dir": str(root / "run"),
                    "socket_path": str(root / "control.sock"),
                }
            )
            Path(run.socket_path).write_text("", encoding="utf-8")

            def raise_refused(path, payload):
                raise ConnectionRefusedError("stale control socket")

            with self.assertRaisesRegex(AssignmentError, "assignment delivery failed"):
                assign_task(
                    run,
                    task,
                    sender=raise_refused,
                    process_start_reader=lambda pid: run.agent_process_start,
                    timestamp_factory=lambda: "20260627T010203Z",
                )

            run_dir_path = Path(run.run_dir)
            self.assertTrue((run_dir_path / "assigned-task-20260627T010203Z.md").exists())
            events = [
                json.loads(line)
                for line in (run_dir_path / "events.jsonl")
                .read_text(encoding="utf-8")
                .splitlines()
                if line.strip()
            ]
            self.assertTrue(any(event["event"] == "assignment_attempted" for event in events))
            self.assertFalse(any(event["event"] == "task_assigned" for event in events))
            self.assertFalse((run_dir_path / "assigned-task-latest.md").exists())

    def test_failed_delivery_does_not_record_assignment_or_block_retry(self) -> None:
        from loopweave.assignment import assign_task

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            task = root / "task.md"
            task.write_text("# Task\n", encoding="utf-8")
            run = make_run("run-live", RunState.RUNNING, 1)
            run = RunRecord(
                **{
                    **run.__dict__,
                    "run_dir": str(root / "run"),
                    "socket_path": str(root / "control.sock"),
                }
            )
            Path(run.socket_path).write_text("", encoding="utf-8")

            with self.assertRaisesRegex(AssignmentError, "assignment delivery failed"):
                assign_task(
                    run,
                    task,
                    sender=lambda path, payload: {"status": "error", "message": "closed"},
                    process_start_reader=lambda pid: run.agent_process_start,
                    timestamp_factory=lambda: "20260627T010203Z",
                )

            run_dir_path = Path(run.run_dir)
            self.assertFalse((run_dir_path / "assigned-task-latest.md").exists())

            result = assign_task(
                run,
                task,
                sender=lambda path, payload: {"status": "ok"},
                process_start_reader=lambda pid: run.agent_process_start,
                timestamp_factory=lambda: "20260627T010204Z",
            )

            self.assertFalse(result.duplicate)
            self.assertTrue((run_dir_path / "assigned-task-latest.md").exists())

    def test_assign_task_marks_run_stale_on_process_identity_failure(self) -> None:
        from loopweave.assignment import assign_task

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            task = root / "task.md"
            task.write_text("# Task\n", encoding="utf-8")
            run = make_run("run-live", RunState.RUNNING, 1)
            run = RunRecord(
                **{
                    **run.__dict__,
                    "run_dir": str(root / "run"),
                    "socket_path": str(root / "control.sock"),
                }
            )
            Path(run.socket_path).write_text("", encoding="utf-8")
            marked = []

            with self.assertRaisesRegex(AssignmentError, "process identity changed"):
                assign_task(
                    run,
                    task,
                    sender=lambda path, payload: {"status": "ok"},
                    process_start_reader=lambda pid: "different process",
                    on_stale_run=marked.append,
                )

            self.assertEqual([stale.run_id for stale in marked], ["run-live"])

    def test_assign_task_checks_process_identity_before_socket_existence(self) -> None:
        from loopweave.assignment import assign_task

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            task = root / "task.md"
            task.write_text("# Task\n", encoding="utf-8")
            run = make_run("run-stale", RunState.RUNNING, 1)
            run = RunRecord(
                **{
                    **run.__dict__,
                    "run_dir": str(root / "run"),
                    "socket_path": str(root / "missing.sock"),
                }
            )
            marked = []

            with self.assertRaisesRegex(AssignmentError, "process identity changed"):
                assign_task(
                    run,
                    task,
                    sender=lambda path, payload: {"status": "ok"},
                    process_start_reader=lambda pid: "different process",
                    on_stale_run=marked.append,
                )

            self.assertEqual([stale.run_id for stale in marked], ["run-stale"])


if __name__ == "__main__":
    unittest.main()
