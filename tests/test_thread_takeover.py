from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from loopweave.codex_sessions import AmbiguousThread
from loopweave.dispatcher import DispatchLeaseState
from loopweave.models import RunRecord, RunState
from loopweave.registry import PendingAttachConflict, Registry
from loopweave.thread_takeover import (
    RunNotAttachable,
    ThreadCwdMismatch,
    ThreadTakeoverCoordinator,
    WorkerUnavailable,
)


class ThreadTakeoverTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.project = self.root / "project"
        self.project.mkdir()
        self.sessions_dir = self.root / "sessions"
        self.sessions_dir.mkdir()
        self.run_dir = self.root / "run-1"
        self.run_dir.mkdir()
        self.registry = Registry(self.root / "registry.sqlite")
        self.registry.create_run(
            RunRecord(
                run_id="run-1",
                codex_thread_id="thread-1",
                cwd=str(self.project),
                tty="/dev/test",
                agent="claude",
                agent_pid=123,
                agent_process_start="worker-start",
                control_token="secret",
                state=RunState.RUNNING,
                run_dir=str(self.run_dir),
            )
        )
        (self.run_dir / "run.json").write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "run_id": "run-1",
                    "codex_thread_id": "thread-1",
                    "cwd": str(self.project),
                }
            ),
            encoding="utf-8",
        )
        self._session("thread-1", self.project)
        self._session("thread-2", self.project)
        self.coordinator = self._coordinator()

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_immediate_attach_switches_live_run_and_snapshot(self) -> None:
        result = self.coordinator.attach(
            "run-1", explicit_thread_id="thread-2"
        )

        run = self.registry.get_run("run-1")
        snapshot = json.loads(
            (self.run_dir / "run.json").read_text(encoding="utf-8")
        )
        self.assertEqual(result.status, "attached")
        self.assertEqual(result.old_thread_id, "thread-1")
        self.assertEqual(result.new_thread_id, "thread-2")
        self.assertEqual(result.binding_generation, 2)
        self.assertEqual(run.codex_thread_id, "thread-2")
        self.assertEqual(snapshot["codex_thread_id"], "thread-2")
        self.assertEqual(snapshot["binding_generation"], 2)
        self.assertIsNone(snapshot["pending_codex_thread_id"])

    def test_review_states_queue_takeover(self) -> None:
        for state in (
            RunState.REVIEWING,
            RunState.REVIEW_READY,
            RunState.DELIVERING,
        ):
            with self.subTest(state=state):
                registry, coordinator = self._fresh_run(state)

                result = coordinator.attach(
                    "run-1", explicit_thread_id="thread-2"
                )

                self.assertEqual(result.status, "queued")
                self.assertEqual(result.binding_generation, 2)
                self.assertEqual(
                    registry.get_run("run-1").pending_codex_thread_id,
                    "thread-2",
                )

    def test_live_dispatch_lease_queues_running_run(self) -> None:
        coordinator = self._coordinator(
            lease_inspector=lambda run_dir: DispatchLeaseState.LIVE
        )

        result = coordinator.attach(
            "run-1", explicit_thread_id="thread-2"
        )

        self.assertEqual(result.status, "queued")

    def test_stale_dispatch_lease_is_removed_before_immediate_attach(self) -> None:
        lock = self.run_dir / "dispatch.lock"
        lock.write_text("stale", encoding="utf-8")
        coordinator = self._coordinator(
            lease_inspector=lambda run_dir: DispatchLeaseState.STALE
        )

        result = coordinator.attach(
            "run-1", explicit_thread_id="thread-2"
        )

        self.assertEqual(result.status, "attached")
        self.assertFalse(lock.exists())

    def test_needs_human_live_worker_can_attach(self) -> None:
        self.registry.force_state("run-1", RunState.NEEDS_HUMAN)

        result = self.coordinator.attach(
            "run-1", explicit_thread_id="thread-2"
        )

        self.assertEqual(result.status, "attached")

    def test_disallowed_terminal_states_are_rejected(self) -> None:
        for state in (
            RunState.APPROVED,
            RunState.FAILED,
            RunState.STOPPED,
            RunState.ORPHANED,
        ):
            with self.subTest(state=state):
                registry, coordinator = self._fresh_run(state)
                with self.assertRaises(RunNotAttachable):
                    coordinator.attach(
                        "run-1", explicit_thread_id="thread-2"
                    )
                self.assertEqual(registry.get_run("run-1").state, state)

    def test_dead_or_reused_worker_becomes_orphaned(self) -> None:
        coordinator = self._coordinator(
            process_start=lambda pid: "different-start"
        )

        with self.assertRaises(WorkerUnavailable):
            coordinator.attach("run-1", explicit_thread_id="thread-2")

        self.assertEqual(
            self.registry.get_run("run-1").state, RunState.ORPHANED
        )

    def test_target_thread_cwd_must_match_run(self) -> None:
        other = self.root / "other"
        other.mkdir()
        self._session("thread-other", other)

        with self.assertRaises(ThreadCwdMismatch):
            self.coordinator.attach(
                "run-1", explicit_thread_id="thread-other"
            )

    def test_external_workspace_still_attaches_by_thread_cwd(self) -> None:
        workspace = self.root / "external-workspace"
        workspace.mkdir()
        registry = Registry(self.root / "external-registry.sqlite")
        run_dir = self.root / "external-run"
        run_dir.mkdir()
        registry.create_run(
            RunRecord(
                run_id="run-external",
                codex_thread_id="thread-1",
                cwd=str(workspace),
                thread_cwd=str(self.project),
                workspace_root=str(workspace),
                tty="/dev/test",
                agent="claude",
                agent_pid=123,
                agent_process_start="worker-start",
                control_token="secret",
                state=RunState.RUNNING,
                run_dir=str(run_dir),
            )
        )
        (run_dir / "run.json").write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "run_id": "run-external",
                    "codex_thread_id": "thread-1",
                    "thread_cwd": str(self.project),
                    "workspace_root": str(workspace),
                }
            ),
            encoding="utf-8",
        )
        coordinator = ThreadTakeoverCoordinator(
            registry,
            self.sessions_dir,
            process_start=lambda pid: "worker-start",
            lease_inspector=lambda run_dir: DispatchLeaseState.ABSENT,
        )

        result = coordinator.attach(
            "run-external", explicit_thread_id="thread-2"
        )

        self.assertEqual(result.status, "attached")
        self.assertEqual(
            registry.get_run("run-external").workspace_root,
            str(workspace),
        )

    def test_active_and_same_pending_targets_are_idempotent(self) -> None:
        current = self.coordinator.attach(
            "run-1", explicit_thread_id="thread-1"
        )
        self.registry.force_state("run-1", RunState.REVIEWING)
        first = self.coordinator.attach(
            "run-1", explicit_thread_id="thread-2"
        )
        second = self.coordinator.attach(
            "run-1", explicit_thread_id="thread-2"
        )

        self.assertEqual(current.status, "unchanged")
        self.assertEqual(first.status, "queued")
        self.assertEqual(second.status, "queued")

    def test_conflicting_pending_target_is_rejected(self) -> None:
        self.registry.force_state("run-1", RunState.REVIEWING)
        self.coordinator.attach("run-1", explicit_thread_id="thread-2")
        self._session("thread-3", self.project)

        with self.assertRaises(PendingAttachConflict):
            self.coordinator.attach(
                "run-1", explicit_thread_id="thread-3"
            )

    def test_automatic_discovery_rejects_multiple_candidates(self) -> None:
        with self.assertRaises(AmbiguousThread):
            self.coordinator.attach("run-1")

    def test_reconcile_applies_pending_after_nonterminal_resolution(self) -> None:
        self.registry.queue_thread_attach("run-1", "thread-2")
        self.registry.force_state("run-1", RunState.WORKER_CONTINUING)

        result = self.coordinator.reconcile_pending("run-1")

        run = self.registry.get_run("run-1")
        self.assertEqual(result.status, "attached")
        self.assertEqual(run.codex_thread_id, "thread-2")
        self.assertEqual(run.binding_generation, 2)
        self.assertIsNone(run.pending_codex_thread_id)

    def test_reconcile_cancels_pending_after_terminal_resolution(self) -> None:
        self.registry.queue_thread_attach("run-1", "thread-2")
        self.registry.force_state("run-1", RunState.APPROVED)

        result = self.coordinator.reconcile_pending("run-1")

        run = self.registry.get_run("run-1")
        self.assertEqual(result.status, "cancelled_terminal")
        self.assertEqual(run.codex_thread_id, "thread-1")
        self.assertEqual(run.binding_generation, 1)
        self.assertIsNone(run.pending_codex_thread_id)

    def test_reconcile_without_pending_target_is_a_no_op(self) -> None:
        self.assertIsNone(self.coordinator.reconcile_pending("run-1"))

    def test_reconcile_removes_stale_lease_and_applies_pending_after_restart(
        self,
    ) -> None:
        self.registry.queue_thread_attach("run-1", "thread-2")
        lock = self.run_dir / "dispatch.lock"
        lock.write_text(
            json.dumps(
                {
                    "pid": 999,
                    "process_start": "gone",
                    "binding_generation": 1,
                }
            ),
            encoding="utf-8",
        )
        coordinator = self._coordinator(
            lease_inspector=lambda run_dir: DispatchLeaseState.STALE
        )

        result = coordinator.reconcile_run("run-1")

        self.assertEqual(result.status, "attached")
        self.assertFalse(lock.exists())
        self.assertEqual(
            self.registry.get_run("run-1").codex_thread_id,
            "thread-2",
        )

    def test_reconcile_repairs_run_snapshot_from_registry(self) -> None:
        self.registry.attach_thread_now(
            "run-1", "thread-2", reason="context_exhausted"
        )
        snapshot = json.loads(
            (self.run_dir / "run.json").read_text(encoding="utf-8")
        )
        snapshot["codex_thread_id"] = "thread-1"
        snapshot["binding_generation"] = 1
        (self.run_dir / "run.json").write_text(
            json.dumps(snapshot), encoding="utf-8"
        )

        result = self.coordinator.reconcile_run("run-1")

        repaired = json.loads(
            (self.run_dir / "run.json").read_text(encoding="utf-8")
        )
        self.assertIsNone(result)
        self.assertEqual(repaired["codex_thread_id"], "thread-2")
        self.assertEqual(repaired["binding_generation"], 2)

    def test_reconcile_terminal_run_cancels_pending_without_removing_live_lease(
        self,
    ) -> None:
        self.registry.queue_thread_attach("run-1", "thread-2")
        self.registry.force_state("run-1", RunState.APPROVED)
        lock = self.run_dir / "dispatch.lock"
        lock.write_text("live", encoding="utf-8")
        coordinator = self._coordinator(
            lease_inspector=lambda run_dir: DispatchLeaseState.LIVE
        )

        result = coordinator.reconcile_run("run-1")

        self.assertEqual(result.status, "cancelled_terminal")
        self.assertTrue(lock.exists())
        self.assertIsNone(
            self.registry.get_run("run-1").pending_codex_thread_id
        )

    def _coordinator(self, process_start=None, lease_inspector=None):
        return ThreadTakeoverCoordinator(
            self.registry,
            self.sessions_dir,
            process_start=process_start or (lambda pid: "worker-start"),
            lease_inspector=lease_inspector
            or (lambda run_dir: DispatchLeaseState.ABSENT),
        )

    def _fresh_run(self, state: RunState):
        root = self.root / ("fresh-" + state.value)
        root.mkdir()
        run_dir = root / "run"
        run_dir.mkdir()
        registry = Registry(root / "registry.sqlite")
        registry.create_run(
            RunRecord(
                run_id="run-1",
                codex_thread_id="thread-1",
                cwd=str(self.project),
                tty="/dev/test",
                agent="claude",
                agent_pid=123,
                agent_process_start="worker-start",
                control_token="secret",
                state=state,
                run_dir=str(run_dir),
            )
        )
        (run_dir / "run.json").write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "run_id": "run-1",
                    "codex_thread_id": "thread-1",
                    "cwd": str(self.project),
                }
            ),
            encoding="utf-8",
        )
        return registry, ThreadTakeoverCoordinator(
            registry,
            self.sessions_dir,
            process_start=lambda pid: "worker-start",
            lease_inspector=lambda run_dir: DispatchLeaseState.ABSENT,
        )

    def _session(self, thread_id: str, cwd: Path) -> None:
        timestamp = datetime.now(timezone.utc).isoformat()
        payload = {
            "timestamp": timestamp,
            "type": "session_meta",
            "payload": {
                "id": thread_id,
                "cwd": str(cwd),
            },
        }
        (self.sessions_dir / (thread_id + ".jsonl")).write_text(
            json.dumps(payload) + "\n", encoding="utf-8"
        )


if __name__ == "__main__":
    unittest.main()
