from __future__ import annotations

import hashlib
import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from loopweave.models import (
    ReviewBackend,
    RunMode,
    RunRecord,
    RunState,
    StorageState,
)
from loopweave.protocol import append_event, write_json_atomic
from loopweave.registry import Registry
from loopweave.run_governance import (
    GovernanceError,
    GovernancePaths,
    RunGovernance,
)
from loopweave.runtime_config import RunPolicy


class RunGovernanceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.paths = GovernancePaths(
            runs=self.root / "runs",
            archives=self.root / "archives",
            ledger=self.root / "ledger",
            trash=self.root / "trash",
            maintenance=self.root / "maintenance",
            var=self.root / "var",
        )
        self.paths.ensure()
        self.registry = Registry(self.paths.var / "registry.sqlite")
        self.now = datetime(2026, 7, 27, tzinfo=timezone.utc)
        self.policy = RunPolicy(
            archive_after_days=7,
            orphan_after_days=14,
            trash_after_days=7,
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _run(
        self,
        run_id: str,
        state: RunState,
        *,
        age_days: int = 30,
        pid: int = 999_999_999,
    ) -> RunRecord:
        run_dir = self.paths.runs / run_id
        run_dir.mkdir()
        run = RunRecord(
            run_id=run_id,
            codex_thread_id="review-task",
            cwd=str(self.root / "workspace"),
            thread_cwd=str(self.root / "workspace"),
            workspace_root=str(self.root / "workspace"),
            project_slug="example",
            project_root=str(self.root / "project"),
            tty="/dev/ttys001",
            agent="codex",
            agent_pid=pid,
            agent_process_start="recorded-start",
            control_token="secret",
            state=state,
            mode=RunMode.DEVELOP,
            reviewer_backend=ReviewBackend.VISIBLE_THREAD,
            reviewer_thread_id="review-task",
            reviewer_thread_cwd=str(self.root),
            socket_path=str(self.paths.var / (run_id + ".sock")),
            run_dir=str(run_dir),
        )
        self.registry.create_run(run)
        write_json_atomic(
            run_dir / "run.json",
            {"schema_version": 1, "run_id": run_id},
        )
        append_event(
            run_dir / "events.jsonl",
            {
                "event": "run_exited",
                "run_id": run_id,
                "timestamp": (self.now - timedelta(days=age_days)).isoformat(),
            },
        )
        return run

    def _manager(self, reader=None, now=None) -> RunGovernance:
        return RunGovernance(
            self.registry,
            paths=self.paths,
            policy=self.policy,
            process_start_reader=reader or (lambda _pid: (_ for _ in ()).throw(ProcessLookupError())),
            now_factory=lambda: now or self.now,
        )

    def test_policy_protects_active_pending_human_pinned_and_referenced_runs(self) -> None:
        active = self._run("run-active", RunState.RUNNING, pid=101)
        needs_human = self._run("run-human", RunState.NEEDS_HUMAN)
        owner = self._run("run-owner", RunState.OWNER_REVIEW_PENDING)
        pending = self._run("run-pending", RunState.APPROVED)
        pinned = self._run("run-pinned", RunState.STOPPED)
        source = self._run("run-source", RunState.APPROVED)
        dependent = self._run("run-dependent", RunState.STOPPED)
        pending_path = Path(pending.run_dir) / "review-inbox" / "pending"
        pending_path.parent.mkdir()
        pending_path.write_text("review", encoding="utf-8")
        self.registry.set_pin(
            pinned.run_id,
            pinned=True,
            reason="retain evidence",
            operator="test",
        )
        write_json_atomic(
            Path(dependent.run_dir) / "continuity.json",
            {"source_run_id": source.run_id},
        )
        append_event(
            Path(dependent.run_dir) / "events.jsonl",
            {"event": "task_adopted", "source_run_id": source.run_id},
        )

        manager = self._manager(
            reader=lambda pid: "recorded-start" if pid == 101 else (
                (_ for _ in ()).throw(ProcessLookupError())
            )
        )
        decisions = {item.run_id: item for item in manager.list_decisions()}

        self.assertIn("managed_process_live", decisions[active.run_id].reasons)
        self.assertIn(
            "protected_run_state:needs_human",
            decisions[needs_human.run_id].reasons,
        )
        self.assertIn(
            "protected_run_state:owner_review_pending",
            decisions[owner.run_id].reasons,
        )
        self.assertIn(
            "pending_review_or_delivery", decisions[pending.run_id].reasons
        )
        self.assertIn("pinned", decisions[pinned.run_id].reasons)
        self.assertIn(
            "referenced_by_task_continuity",
            decisions[source.run_id].reasons,
        )
        for run_id in (
            active.run_id,
            needs_human.run_id,
            owner.run_id,
            pending.run_id,
            pinned.run_id,
            source.run_id,
        ):
            self.assertEqual(decisions[run_id].action, "none")

    def test_uncertain_process_identity_fails_closed(self) -> None:
        run = self._run("run-uncertain", RunState.APPROVED, pid=202)
        manager = self._manager(
            reader=lambda _pid: (_ for _ in ()).throw(RuntimeError("blocked"))
        )

        with mock.patch(
            "loopweave.terminal_host.pid_alive", return_value=True
        ):
            decision = manager.list_decisions()[0]

        self.assertEqual(decision.run_id, run.run_id)
        self.assertIn("process_identity_uncertain", decision.reasons)
        self.assertEqual(decision.action, "none")

    def test_archive_is_verified_atomic_recoverable_and_restorable(self) -> None:
        run = self._run("run-archive", RunState.STOPPED)
        task = Path(run.run_dir) / "assigned-task-latest.md"
        task.write_text("# task\n", encoding="utf-8")
        original_digest = hashlib.sha256(task.read_bytes()).hexdigest()
        manager = self._manager()

        archive = manager.archive_run(run.run_id, reason="test archive")

        storage = self.registry.get_storage(run.run_id)
        self.assertEqual(storage.storage_state, StorageState.ARCHIVED)
        self.assertTrue(archive.is_file())
        self.assertEqual(hashlib.sha256(archive.read_bytes()).hexdigest(), storage.archive_sha256)
        self.assertFalse(Path(run.run_dir).exists())
        self.assertTrue(Path(storage.trash_path).is_dir())
        ledger = json.loads(Path(storage.ledger_path).read_text(encoding="utf-8"))
        self.assertEqual(ledger["task_sha256"], original_digest)
        self.assertNotIn("secret", json.dumps(ledger))

        restored = manager.restore_run(run.run_id, reason="test restore")

        self.assertEqual(restored, Path(run.run_dir))
        self.assertEqual(
            self.registry.get_storage(run.run_id).storage_state,
            StorageState.HOT,
        )
        self.assertEqual(hashlib.sha256(task.read_bytes()).hexdigest(), original_digest)
        self.assertFalse(Path(storage.trash_path).exists())

        new_archive = manager.archive_run(run.run_id, reason="rearchive")
        rearchived = self.registry.get_storage(run.run_id)
        self.assertTrue(new_archive.is_file())
        self.assertFalse(archive.exists())
        self.assertTrue(
            (
                Path(rearchived.trash_path)
                / ".previous-archives"
                / archive.name
            ).is_file()
        )

    def test_gc_plan_refuses_changed_snapshot(self) -> None:
        run = self._run("run-stale", RunState.FAILED)
        manager = self._manager()
        plan = manager.create_gc_plan()
        self.assertEqual(plan.decisions[0].action, "archive")
        (Path(run.run_dir) / "changed.txt").write_text("changed", encoding="utf-8")

        with self.assertRaisesRegex(GovernanceError, "changed since"):
            manager.apply_gc_plan(plan)

        self.assertEqual(
            self.registry.get_storage(run.run_id).storage_state,
            StorageState.HOT,
        )

    def test_archive_build_failure_rolls_back_to_hot_without_data_loss(
        self,
    ) -> None:
        run = self._run("run-build-failure", RunState.FAILED)
        marker = Path(run.run_dir) / "evidence.txt"
        marker.write_text("must survive", encoding="utf-8")
        manager = self._manager()

        with mock.patch.object(
            manager,
            "_write_archive",
            side_effect=OSError("simulated archive failure"),
        ):
            with self.assertRaisesRegex(OSError, "simulated"):
                manager.archive_run(run.run_id)

        storage = self.registry.get_storage(run.run_id)
        self.assertEqual(storage.storage_state, StorageState.HOT)
        self.assertTrue(marker.is_file())
        self.assertEqual(marker.read_text(encoding="utf-8"), "must survive")
        self.assertEqual(list(self.paths.archives.iterdir()), [])
        self.assertTrue(
            any(
                event["event"] == "storage_archiving_to_hot"
                for event in self.registry.list_maintenance_events(run.run_id)
            )
        )

    def test_post_commit_move_failure_enters_recovery_required(
        self,
    ) -> None:
        run = self._run("run-move-failure", RunState.STOPPED)
        manager = self._manager()
        real_replace = os.replace

        def fail_run_move(source, destination):
            if Path(source) == Path(run.run_dir):
                raise OSError("simulated run move failure")
            return real_replace(source, destination)

        with mock.patch(
            "loopweave.run_governance.os.replace",
            side_effect=fail_run_move,
        ):
            with self.assertRaisesRegex(OSError, "simulated"):
                manager.archive_run(run.run_id)

        storage = self.registry.get_storage(run.run_id)
        self.assertEqual(
            storage.storage_state, StorageState.RECOVERY_REQUIRED
        )
        self.assertTrue(Path(run.run_dir).is_dir())
        self.assertTrue(Path(storage.archive_path).is_file())
        self.assertTrue(Path(storage.ledger_path).is_file())
        self.assertIn("simulated run move failure", storage.recovery_note)

    def test_restore_rejects_corrupt_archive_without_changing_state(
        self,
    ) -> None:
        run = self._run("run-corrupt-archive", RunState.STOPPED)
        manager = self._manager()
        archive = manager.archive_run(run.run_id)
        archive.write_bytes(archive.read_bytes() + b"corrupt")

        with self.assertRaisesRegex(
            GovernanceError, "checksum verification failed"
        ):
            manager.restore_run(run.run_id)

        self.assertEqual(
            self.registry.get_storage(run.run_id).storage_state,
            StorageState.ARCHIVED,
        )
        self.assertFalse(Path(run.run_dir).exists())

    def test_maintenance_lock_rejects_concurrent_governance(self) -> None:
        manager = self._manager()

        with manager.maintenance_lock():
            with self.assertRaisesRegex(
                GovernanceError, "another LoopWeave maintenance"
            ):
                with manager.maintenance_lock():
                    self.fail("second lock must not be acquired")

    def test_gc_purges_only_expired_trash_copy_and_keeps_archive(self) -> None:
        run = self._run("run-trash", RunState.STOPPED)
        manager = self._manager()
        archive = manager.archive_run(run.run_id)
        storage = self.registry.get_storage(run.run_id)
        later = datetime.now(timezone.utc) + timedelta(days=8)
        later_manager = self._manager(now=later)

        plan = later_manager.create_gc_plan()
        decision = next(item for item in plan.decisions if item.run_id == run.run_id)
        self.assertEqual(decision.action, "purge_trash")
        result = later_manager.apply_gc_plan(plan)

        self.assertIn((run.run_id, "purge_trash"), result.applied)
        self.assertTrue(archive.is_file())
        self.assertFalse(Path(storage.trash_path).exists())
        self.assertIsNone(self.registry.get_storage(run.run_id).trash_path)

    def test_old_archive_prunes_heavy_logs_but_keeps_recent_project_evidence(self) -> None:
        old = self._run("run-old", RunState.STOPPED, age_days=100)
        recent = self._run("run-recent", RunState.STOPPED, age_days=1)
        (Path(old.run_dir) / "terminal.txt").write_bytes(b"x" * 4096)
        (Path(old.run_dir) / "reviewer-verdict.json").write_text(
            '{"verdict":"approved"}\n', encoding="utf-8"
        )
        (Path(recent.run_dir) / "terminal.txt").write_bytes(b"y" * 4096)
        manager = self._manager()
        manager.archive_run(old.run_id)
        manager.archive_run(recent.run_id)

        later = datetime.now(timezone.utc) + timedelta(days=100)
        policy = RunPolicy(
            archive_after_days=7,
            orphan_after_days=14,
            trash_after_days=7,
            prune_after_days=90,
            keep_recent_per_project=1,
        )
        later_manager = RunGovernance(
            self.registry,
            paths=self.paths,
            policy=policy,
            process_start_reader=lambda _pid: (
                (_ for _ in ()).throw(ProcessLookupError())
            ),
            now_factory=lambda: later,
        )
        purge_plan = later_manager.create_gc_plan()
        later_manager.apply_gc_plan(purge_plan)
        prune_plan = later_manager.create_gc_plan()
        decisions = {item.run_id: item for item in prune_plan.decisions}

        self.assertEqual(decisions[old.run_id].action, "prune_archive_logs")
        self.assertEqual(decisions[recent.run_id].action, "none")
        self.assertIn(
            "recent_project_evidence", decisions[recent.run_id].reasons
        )
        later_manager.apply_gc_plan(prune_plan)
        old_storage = self.registry.get_storage(old.run_id)
        ledger = json.loads(
            Path(old_storage.ledger_path).read_text(encoding="utf-8")
        )
        self.assertEqual(
            [item["path"] for item in ledger["pruned_files"]],
            ["terminal.txt"],
        )
        later_manager.restore_run(old.run_id)
        self.assertFalse((Path(old.run_dir) / "terminal.txt").exists())
        self.assertTrue((Path(old.run_dir) / "reviewer-verdict.json").exists())

    def test_unregistered_run_directory_is_reported_and_never_planned(self) -> None:
        unknown = self.paths.runs / "run-unregistered"
        unknown.mkdir()
        (unknown / "terminal.txt").write_text("private history", encoding="utf-8")
        manager = self._manager()

        plan = manager.create_gc_plan()

        self.assertEqual(plan.decisions, ())
        self.assertEqual(plan.unregistered_directories, (str(unknown.resolve()),))
        self.assertTrue(unknown.exists())


if __name__ == "__main__":
    unittest.main()
