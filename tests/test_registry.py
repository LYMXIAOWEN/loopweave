from __future__ import annotations

import sqlite3
import tempfile
import threading
import unittest
from dataclasses import replace
from pathlib import Path
from queue import Queue
from unittest import mock

from loopweave.models import (
    ReviewBackend,
    RunMode,
    RunRecord,
    RunState,
    ThreadBinding,
)
from loopweave.registry import (
    InvalidTransition,
    PendingAttachConflict,
    Registry,
    RegistryError,
)


class RegistryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "registry.sqlite"
        self.registry = Registry(self.db_path)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_creates_and_reads_run(self) -> None:
        run = RunRecord(
            run_id="run-1",
            codex_thread_id="thread-1",
            cwd="/tmp/project",
            tty="/dev/ttys001",
            agent="claude",
            agent_pid=123,
            agent_process_start="2026-06-18T10:00:00Z",
            control_token="secret",
            state=RunState.CREATED,
        )

        self.registry.create_run(run)

        self.assertEqual(self.registry.get_run("run-1"), run)

    def test_new_run_defaults_to_develop(self) -> None:
        self.assertEqual(self._run().mode, RunMode.DEVELOP)

    def test_run_defaults_to_ephemeral_backend(self) -> None:
        self.assertEqual(
            self._run().reviewer_backend,
            ReviewBackend.EPHEMERAL,
        )

    def test_registry_persists_design_mode(self) -> None:
        run = replace(self._run(), mode=RunMode.DESIGN)

        self.registry.create_run(run)

        self.assertEqual(self.registry.get_run("run-1").mode, RunMode.DESIGN)

    def test_registry_persists_visible_reviewer_binding(self) -> None:
        run = replace(
            self._run(),
            reviewer_backend=ReviewBackend.VISIBLE_THREAD,
            reviewer_thread_id="review-thread-1",
            reviewer_thread_cwd="/workspace/loopweave",
            reviewer_generation=3,
        )

        self.registry.create_run(run)

        stored = self.registry.get_run("run-1")
        self.assertEqual(
            stored.reviewer_backend,
            ReviewBackend.VISIBLE_THREAD,
        )
        self.assertEqual(stored.reviewer_thread_id, "review-thread-1")
        self.assertEqual(
            stored.reviewer_thread_cwd,
            "/workspace/loopweave",
        )
        self.assertEqual(stored.reviewer_generation, 3)

    def test_bind_visible_reviewer_advances_generation(self) -> None:
        self.registry.create_run(self._run())

        updated = self.registry.bind_reviewer_thread(
            "run-1",
            "review-thread-2",
            "/workspace/loopweave",
        )

        self.assertEqual(
            updated.reviewer_backend,
            ReviewBackend.VISIBLE_THREAD,
        )
        self.assertEqual(updated.reviewer_thread_id, "review-thread-2")
        self.assertEqual(
            updated.reviewer_thread_cwd,
            "/workspace/loopweave",
        )
        self.assertEqual(updated.reviewer_generation, 2)

    def test_migrates_legacy_runs_table_idempotently(self) -> None:
        legacy_path = Path(self.temp_dir.name) / "legacy.sqlite"
        with sqlite3.connect(str(legacy_path)) as connection:
            connection.execute(
                """
                CREATE TABLE runs (
                    run_id TEXT PRIMARY KEY,
                    codex_thread_id TEXT NOT NULL,
                    cwd TEXT NOT NULL,
                    tty TEXT NOT NULL,
                    agent TEXT NOT NULL,
                    agent_pid INTEGER NOT NULL,
                    agent_process_start TEXT NOT NULL,
                    control_token TEXT NOT NULL,
                    state TEXT NOT NULL,
                    review_loop INTEGER NOT NULL DEFAULT 0,
                    socket_path TEXT NOT NULL DEFAULT '',
                    run_dir TEXT NOT NULL DEFAULT ''
                )
                """
            )
            connection.execute(
                """
                INSERT INTO runs (
                    run_id, codex_thread_id, cwd, tty, agent, agent_pid,
                    agent_process_start, control_token, state
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "legacy-run",
                    "legacy-thread",
                    "/tmp/legacy",
                    "/dev/ttys002",
                    "claude",
                    456,
                    "2026-06-18T09:00:00Z",
                    "legacy-secret",
                    RunState.RUNNING.value,
                ),
            )

        with mock.patch(
            "loopweave.protocol.utc_now",
            return_value="2026-06-19T02:03:04+00:00",
        ):
            migrated = Registry(legacy_path)
            migrated_again = Registry(legacy_path)

        self.assertTrue(hasattr(migrated, "run_columns"))
        self.assertEqual(
            migrated.run_columns(),
            {
                "run_id",
                "codex_thread_id",
                "pending_codex_thread_id",
                "binding_generation",
                "cwd",
                "thread_cwd",
                "workspace_root",
                "project_slug",
                "project_root",
                "mode",
                "reviewer_backend",
                "reviewer_thread_id",
                "reviewer_thread_cwd",
                "reviewer_generation",
                "tty",
                "agent",
                "agent_pid",
                "agent_process_start",
                "control_token",
                "state",
                "review_loop",
                "socket_path",
                "run_dir",
            },
        )
        self.assertEqual(migrated_again.run_columns(), migrated.run_columns())
        legacy_run = migrated.get_run("legacy-run")
        self.assertEqual(legacy_run.thread_cwd, "/tmp/legacy")
        self.assertEqual(legacy_run.workspace_root, "/tmp/legacy")
        self.assertIsNone(legacy_run.project_slug)
        self.assertIsNone(legacy_run.project_root)
        self.assertEqual(legacy_run.mode, RunMode.DEVELOP)
        self.assertIsNone(legacy_run.pending_codex_thread_id)
        self.assertEqual(legacy_run.binding_generation, 1)
        self.assertEqual(
            migrated_again.list_thread_bindings("legacy-run"),
            [
                ThreadBinding(
                    run_id="legacy-run",
                    generation=1,
                    thread_id="legacy-thread",
                    attached_at="2026-06-19T02:03:04+00:00",
                )
            ],
        )

    def test_create_run_records_initial_thread_binding(self) -> None:
        run = self._run()

        with mock.patch(
            "loopweave.protocol.utc_now",
            return_value="2026-06-19T01:02:03+00:00",
        ):
            self.registry.create_run(run)

        self.assertTrue(hasattr(self.registry, "list_thread_bindings"))
        bindings = self.registry.list_thread_bindings("run-1")
        self.assertEqual(
            bindings,
            [
                ThreadBinding(
                    run_id="run-1",
                    generation=1,
                    thread_id="thread-1",
                    attached_at="2026-06-19T01:02:03+00:00",
                )
            ],
        )

    def test_external_workspace_paths_round_trip(self) -> None:
        run = RunRecord(
            run_id="run-external",
            codex_thread_id="thread-1",
            cwd="/workspace/loopweave",
            thread_cwd="/workspace/loopweave",
            workspace_root="/workspace/example",
            project_slug="example-project",
            project_root=(
                "/workspace/loopweave/projects/example-project"
            ),
            tty="/dev/ttys001",
            agent="claude",
            agent_pid=123,
            agent_process_start="start",
            control_token="secret",
            state=RunState.RUNNING,
        )

        self.registry.create_run(run)

        self.assertEqual(self.registry.get_run(run.run_id), run)

    def test_create_run_normalizes_initial_binding_metadata(self) -> None:
        run = replace(
            self._run(),
            pending_codex_thread_id="pending-thread",
            binding_generation=7,
        )

        self.registry.create_run(run)

        stored = self.registry.get_run("run-1")
        self.assertIsNone(stored.pending_codex_thread_id)
        self.assertEqual(stored.binding_generation, 1)
        self.assertEqual(self.registry.list_thread_bindings("run-1")[0].generation, 1)

    def test_create_run_rolls_back_when_history_insert_fails(self) -> None:
        with sqlite3.connect(str(self.db_path)) as connection:
            connection.execute(
                """
                CREATE TRIGGER reject_thread_binding
                BEFORE INSERT ON run_thread_bindings
                BEGIN
                    SELECT RAISE(ABORT, 'history insert rejected');
                END
                """
            )

        with self.assertRaises(sqlite3.IntegrityError):
            self.registry.create_run(self._run())

        with sqlite3.connect(str(self.db_path)) as connection:
            run_count = connection.execute("SELECT COUNT(*) FROM runs").fetchone()[0]
            binding_count = connection.execute(
                "SELECT COUNT(*) FROM run_thread_bindings"
            ).fetchone()[0]
        self.assertEqual(run_count, 0)
        self.assertEqual(binding_count, 0)

    def test_attach_thread_now_advances_generation_and_closes_history(self) -> None:
        self.registry.create_run(self._run())
        initial_binding = self.registry.list_thread_bindings("run-1")[0]

        with mock.patch(
            "loopweave.protocol.utc_now",
            return_value="2026-06-19T03:04:05+00:00",
        ):
            updated = self.registry.attach_thread_now(
                "run-1", "thread-2", reason="context_exhausted"
            )

        self.assertEqual(updated.codex_thread_id, "thread-2")
        self.assertEqual(updated.binding_generation, 2)
        self.assertIsNone(updated.pending_codex_thread_id)
        self.assertEqual(
            self.registry.list_thread_bindings("run-1"),
            [
                ThreadBinding(
                    run_id="run-1",
                    generation=1,
                    thread_id="thread-1",
                    attached_at=initial_binding.attached_at,
                    detached_at="2026-06-19T03:04:05+00:00",
                    detach_reason="context_exhausted",
                ),
                ThreadBinding(
                    run_id="run-1",
                    generation=2,
                    thread_id="thread-2",
                    attached_at="2026-06-19T03:04:05+00:00",
                ),
            ],
        )

    def test_attach_rejects_already_detached_current_history(self) -> None:
        self.registry.create_run(self._run())
        with sqlite3.connect(str(self.db_path)) as connection:
            connection.execute(
                """
                UPDATE run_thread_bindings
                SET detached_at = ?, detach_reason = ?
                WHERE run_id = ? AND generation = ?
                """,
                ("2026-06-19T03:00:00+00:00", "corrupt", "run-1", 1),
            )

        with self.assertRaises(RegistryError):
            self.registry.attach_thread_now(
                "run-1", "thread-2", reason="manual"
            )

        self.assertEqual(self.registry.get_run("run-1"), self._run())
        bindings = self.registry.list_thread_bindings("run-1")
        self.assertEqual(len(bindings), 1)
        self.assertEqual(bindings[0].thread_id, "thread-1")
        self.assertEqual(
            bindings[0].detached_at,
            "2026-06-19T03:00:00+00:00",
        )

    def test_queue_same_target_is_idempotent_and_conflict_is_rejected(self) -> None:
        self.registry.create_run(self._run())

        first = self.registry.queue_thread_attach("run-1", "thread-2")
        second = self.registry.queue_thread_attach("run-1", "thread-2")

        self.assertEqual(first.pending_codex_thread_id, "thread-2")
        self.assertEqual(second, first)
        with self.assertRaises(PendingAttachConflict):
            self.registry.queue_thread_attach("run-1", "thread-3")
        self.assertEqual(
            self.registry.get_run("run-1").pending_codex_thread_id,
            "thread-2",
        )

    def test_concurrent_queue_targets_serialize_with_one_conflict(self) -> None:
        self.registry.create_run(self._run())
        registries = [Registry(self.db_path), Registry(self.db_path)]
        barrier = threading.Barrier(3)
        outcomes: Queue = Queue()

        def queue_target(registry: Registry, target: str) -> None:
            barrier.wait()
            try:
                outcomes.put(
                    ("success", registry.queue_thread_attach("run-1", target))
                )
            except Exception as exc:
                outcomes.put(("error", exc))

        threads = [
            threading.Thread(
                target=queue_target,
                args=(registry, target),
            )
            for registry, target in zip(
                registries, ("thread-2", "thread-3")
            )
        ]
        for thread in threads:
            thread.start()
        barrier.wait()
        for thread in threads:
            thread.join(timeout=5)

        self.assertTrue(all(not thread.is_alive() for thread in threads))
        results = [outcomes.get_nowait(), outcomes.get_nowait()]
        successes = [
            value for outcome, value in results if outcome == "success"
        ]
        errors = [value for outcome, value in results if outcome == "error"]
        self.assertEqual(len(successes), 1)
        self.assertEqual(len(errors), 1)
        self.assertIsInstance(errors[0], PendingAttachConflict)

        pending = self.registry.get_run("run-1").pending_codex_thread_id
        self.assertIn(pending, {"thread-2", "thread-3"})
        self.assertEqual(successes[0].pending_codex_thread_id, pending)
        self.assertEqual(
            self.registry.queue_thread_attach("run-1", pending),
            successes[0],
        )

    def test_queue_active_target_is_unchanged(self) -> None:
        self.registry.create_run(self._run())

        updated = self.registry.queue_thread_attach("run-1", "thread-1")

        self.assertEqual(updated, self._run())
        self.assertEqual(len(self.registry.list_thread_bindings("run-1")), 1)

    def test_apply_pending_attach_is_atomic_and_idempotent(self) -> None:
        self.registry.create_run(self._run())
        self.registry.queue_thread_attach("run-1", "thread-2")

        first = self.registry.apply_pending_thread_attach(
            "run-1", reason="context_exhausted"
        )
        second = self.registry.apply_pending_thread_attach(
            "run-1", reason="context_exhausted"
        )

        self.assertIsNotNone(first)
        self.assertEqual(first.codex_thread_id, "thread-2")
        self.assertEqual(first.binding_generation, 2)
        self.assertIsNone(first.pending_codex_thread_id)
        self.assertIsNone(second)
        self.assertEqual(len(self.registry.list_thread_bindings("run-1")), 2)

    def test_apply_pending_attach_rolls_back_when_history_insert_fails(self) -> None:
        self.registry.create_run(self._run())
        queued = self.registry.queue_thread_attach("run-1", "thread-2")
        with sqlite3.connect(str(self.db_path)) as connection:
            connection.execute(
                """
                CREATE TRIGGER reject_pending_thread_binding
                BEFORE INSERT ON run_thread_bindings
                WHEN NEW.generation = 2
                BEGIN
                    SELECT RAISE(ABORT, 'pending history insert rejected');
                END
                """
            )

        with self.assertRaises(sqlite3.IntegrityError):
            self.registry.apply_pending_thread_attach(
                "run-1", reason="manual"
            )

        self.assertEqual(self.registry.get_run("run-1"), queued)
        bindings = self.registry.list_thread_bindings("run-1")
        self.assertEqual(len(bindings), 1)
        self.assertIsNone(bindings[0].detached_at)
        self.assertIsNone(bindings[0].detach_reason)

    def test_attach_active_target_is_a_no_op(self) -> None:
        self.registry.create_run(self._run())

        updated = self.registry.attach_thread_now(
            "run-1", "thread-1", reason="manual"
        )

        self.assertEqual(updated, self._run())
        self.assertEqual(len(self.registry.list_thread_bindings("run-1")), 1)

    def test_attach_matching_pending_target_switches_immediately(self) -> None:
        self.registry.create_run(self._run())
        self.registry.queue_thread_attach("run-1", "thread-2")

        updated = self.registry.attach_thread_now(
            "run-1", "thread-2", reason="manual"
        )

        self.assertEqual(updated.codex_thread_id, "thread-2")
        self.assertIsNone(updated.pending_codex_thread_id)
        self.assertEqual(updated.binding_generation, 2)

    def test_attach_conflicting_pending_target_is_rejected(self) -> None:
        self.registry.create_run(self._run())
        queued = self.registry.queue_thread_attach("run-1", "thread-2")

        with self.assertRaises(PendingAttachConflict):
            self.registry.attach_thread_now(
                "run-1", "thread-3", reason="manual"
            )

        self.assertEqual(self.registry.get_run("run-1"), queued)
        self.assertEqual(len(self.registry.list_thread_bindings("run-1")), 1)

    def test_cancel_pending_attach_returns_target_then_none(self) -> None:
        self.registry.create_run(self._run())
        self.registry.queue_thread_attach("run-1", "thread-2")

        first = self.registry.cancel_pending_thread_attach("run-1")
        second = self.registry.cancel_pending_thread_attach("run-1")

        self.assertEqual(first, "thread-2")
        self.assertIsNone(second)
        self.assertIsNone(
            self.registry.get_run("run-1").pending_codex_thread_id
        )

    def test_attach_thread_now_rolls_back_when_history_insert_fails(self) -> None:
        self.registry.create_run(self._run())
        with sqlite3.connect(str(self.db_path)) as connection:
            connection.execute(
                """
                CREATE TRIGGER reject_next_thread_binding
                BEFORE INSERT ON run_thread_bindings
                WHEN NEW.generation = 2
                BEGIN
                    SELECT RAISE(ABORT, 'next history insert rejected');
                END
                """
            )

        with self.assertRaises(sqlite3.IntegrityError):
            self.registry.attach_thread_now(
                "run-1", "thread-2", reason="manual"
            )

        self.assertEqual(self.registry.get_run("run-1"), self._run())
        bindings = self.registry.list_thread_bindings("run-1")
        self.assertEqual(len(bindings), 1)
        self.assertIsNone(bindings[0].detached_at)
        self.assertIsNone(bindings[0].detach_reason)

    def test_accepts_valid_transition(self) -> None:
        self.registry.create_run(self._run())

        updated = self.registry.transition("run-1", RunState.BINDING)

        self.assertEqual(updated.state, RunState.BINDING)

    def test_rejects_invalid_transition(self) -> None:
        self.registry.create_run(self._run())

        with self.assertRaises(InvalidTransition):
            self.registry.transition("run-1", RunState.REVIEWING)

    def test_review_loop_is_an_unbounded_audit_counter(self) -> None:
        self.registry.create_run(self._run())

        for expected in range(1, 6):
            self.assertEqual(
                self.registry.increment_review_loop("run-1"), expected
            )

    def test_process_identity_includes_start_time(self) -> None:
        self.registry.create_run(self._run())

        self.assertTrue(
            self.registry.process_identity_matches(
                "run-1", pid=123, process_start="2026-06-18T10:00:00Z"
            )
        )
        self.assertFalse(
            self.registry.process_identity_matches(
                "run-1", pid=123, process_start="2026-06-18T10:01:00Z"
            )
        )

    def test_registry_file_is_private(self) -> None:
        mode = self.db_path.stat().st_mode & 0o777

        self.assertEqual(mode, 0o600)

    def _run(self) -> RunRecord:
        return RunRecord(
            run_id="run-1",
            codex_thread_id="thread-1",
            cwd="/tmp/project",
            tty="/dev/ttys001",
            agent="claude",
            agent_pid=123,
            agent_process_start="2026-06-18T10:00:00Z",
            control_token="secret",
            state=RunState.CREATED,
        )


if __name__ == "__main__":
    unittest.main()
