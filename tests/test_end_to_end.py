from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from loopweave.cli import (
    _deliver_review,
    _dispatch_and_deliver,
    _finalize_owner_review,
    _resolve_review,
    _run_agent,
    main,
)
from loopweave.completion_notifier import CompletionNotifier
from loopweave.models import ReviewBackend, RunMode, RunRecord, RunState
from loopweave.registry import Registry
from loopweave.supervisor import Supervisor, process_start_time
from loopweave.visible_review import (
    create_review_card,
    queue_visible_review_card,
    render_review_next_instruction,
    submit_visible_review,
)


class EndToEndTests(unittest.TestCase):
    def test_project_run_executes_in_external_workspace(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            control = root / "control"
            projects = control / "projects"
            runs = control / "runs"
            var = control / "var"
            workspace = root / "external-workspace"
            control.mkdir()
            workspace.mkdir()
            registry = Registry(var / "registry.sqlite")
            script = (
                "from pathlib import Path; import time; "
                "Path('result.txt').write_text('done\\n'); time.sleep(0.2)"
            )
            args = SimpleNamespace(
                cwd=str(control),
                cwd_explicit=False,
                project="example-project",
                workspace=str(workspace),
                thread="thread-1",
                agent="generic",
                agent_args=[sys.executable, "-c", script],
                mode="design",
            )

            with patch("loopweave.cli.PROJECTS_DIR", projects), patch(
                "loopweave.cli.RUNS_DIR", runs
            ), patch("loopweave.cli.VAR_DIR", var), patch(
                "loopweave.cli._registry", return_value=registry
            ), patch(
                "loopweave.cli.discover_thread",
                return_value=SimpleNamespace(
                    thread_id="thread-1",
                    cwd=str(control / "thread-metadata"),
                ),
            ), patch(
                "loopweave.cli.os.getcwd", return_value=str(control)
            ):
                exit_code = _run_agent(args)

            run = registry.list_runs()[0]
            self.assertEqual(exit_code, 0)
            self.assertEqual(
                (workspace / "result.txt").read_text(encoding="utf-8"),
                "done\n",
            )
            self.assertFalse(
                (projects / "example-project" / "result.txt").exists()
            )
            self.assertEqual(
                run.thread_cwd,
                str((control / "thread-metadata").resolve()),
            )
            self.assertEqual(run.workspace_root, str(workspace))
            self.assertEqual(run.project_slug, "example-project")
            self.assertEqual(run.mode, RunMode.DESIGN)
            run_snapshot = json.loads(
                (Path(run.run_dir) / "run.json").read_text(encoding="utf-8")
            )
            self.assertEqual(run_snapshot["mode"], "design")
            self.assertTrue(
                (
                    projects
                    / "example-project"
                    / "process-artifacts"
                    / "{}.json".format(run.run_id)
                ).exists()
            )

    @unittest.skipIf(sys.platform == "win32", "POSIX pty required")
    def test_reviewer_verdict_returns_to_original_worker_process(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            run_dir = root / "run-1"
            socket_path = root / "control.sock"
            fixture = Path(__file__).parent / "fixtures" / "echo_agent.py"
            supervisor = Supervisor(
                run_id="run-1",
                command=[sys.executable, "-u", str(fixture)],
                cwd=root,
                run_dir=run_dir,
                socket_path=socket_path,
                control_token="secret",
                passthrough=False,
            )
            pid = supervisor.start()
            registry = Registry(root / "registry.sqlite")
            registry.create_run(
                RunRecord(
                    run_id="run-1",
                    codex_thread_id="thread-1",
                    cwd=str(root),
                    tty="/dev/test",
                    agent="generic",
                    agent_pid=pid,
                    agent_process_start=process_start_time(pid),
                    control_token="secret",
                    state=RunState.REVIEW_READY,
                    socket_path=str(socket_path),
                    run_dir=str(run_dir),
                )
            )
            (run_dir / "reviewer-verdict.md").write_text(
                "Change the implementation and rerun tests.\n", encoding="utf-8"
            )
            (run_dir / "reviewer-verdict.json").write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "run_id": "run-1",
                        "review_id": "review-1",
                        "verdict": "changes_requested",
                        "summary": "One change required.",
                        "review_file": "reviewer-verdict.md",
                        "continue": True,
                    }
                ),
                encoding="utf-8",
            )
            try:
                _deliver_review(registry, registry.get_run("run-1"))
                output = self._wait_for(
                    run_dir / "terminal.txt",
                    "Change the implementation and rerun tests.",
                )
            finally:
                supervisor.stop()

            self.assertIn("pid={}".format(pid), output)
            self.assertEqual(
                registry.get_run("run-1").state, RunState.WORKER_CONTINUING
            )

    @unittest.skipIf(sys.platform == "win32", "POSIX pty required")
    def test_visible_review_submit_reaches_original_worker_process(self) -> None:
        registry, supervisor, run_dir = self._managed_review_fixture()
        root = run_dir.parent
        task = run_dir / "assigned-task-latest.md"
        task.write_text("# Task\n", encoding="utf-8")
        plan = root / "project.json"
        plan.write_text('{"schema_version": 1}\n', encoding="utf-8")
        changed = root / "app.py"
        changed.write_text("ok = True\n", encoding="utf-8")
        card = create_review_card(
            run_id="run-1",
            project_slug="demo",
            stage_id="stage-1",
            stage_title="Foundation",
            completion_scope="stage",
            workspace_root=root,
            task_packet_path=task,
            plan_path=plan,
            work_summary="Implemented foundation files.",
            completed_items=["Created app.py"],
            not_completed_items=[],
            changed_files=[changed],
            artifact_paths=[],
            test_commands=["python3 -m unittest"],
            test_result_summary="Focused tests passed.",
            worker_claims=["Foundation file exists."],
            known_issues=[],
            questions_for_reviewer=[],
        )
        queue_visible_review_card(run_dir, card)
        instruction = render_review_next_instruction(card)
        self.assertIn("Reviewer-owned review directive", instruction)
        self.assertNotIn("terminal.raw.log", instruction)
        review_file = root / "visible-review.md"
        review_file.write_text(
            (
                "---\n"
                "verdict: changes_requested\n"
                "summary: Add missing evidence.\n"
                "---\n\n"
                "Add the missing focused test evidence.\n"
            ),
            encoding="utf-8",
        )
        registry.force_state("run-1", RunState.READY_FOR_REVIEW)

        try:
            submit_visible_review(run_dir, "run-1", review_file)
            _resolve_review(registry, registry.get_run("run-1"))
            output = self._wait_for(
                run_dir / "terminal.txt",
                "Add the missing focused test evidence.",
            )
        finally:
            supervisor.stop()

        self.assertTrue((run_dir / "review-inbox" / "resolved").exists())
        self.assertTrue((run_dir / "reviewer-verdict.json").exists())
        self.assertIn("pid={}".format(registry.get_run("run-1").agent_pid), output)
        self.assertEqual(
            registry.get_run("run-1").state,
            RunState.WORKER_CONTINUING,
        )

    @unittest.skipIf(sys.platform == "win32", "POSIX pty required")
    def test_visible_stage_approval_returns_original_worker_to_continuing(
        self,
    ) -> None:
        registry, supervisor, run_dir = self._managed_review_fixture()
        root = run_dir.parent
        task = run_dir / "assigned-task-latest.md"
        task.write_text("# Task\n", encoding="utf-8")
        plan = root / "project.json"
        plan.write_text('{"schema_version": 1}\n', encoding="utf-8")
        changed = root / "number_summary.py"
        changed.write_text("ok = True\n", encoding="utf-8")
        card = create_review_card(
            run_id="run-1",
            project_slug="demo",
            stage_id="stage-1",
            stage_title="Foundation",
            completion_scope="stage",
            workspace_root=root,
            task_packet_path=task,
            plan_path=plan,
            work_summary="Implemented stage one.",
            completed_items=["Created number_summary.py"],
            not_completed_items=[],
            changed_files=[changed],
            artifact_paths=[],
            test_commands=["python3 -m unittest -v"],
            test_result_summary="All tests passed.",
            worker_claims=["Stage one is complete."],
            known_issues=[],
            questions_for_reviewer=[],
        )
        queue_visible_review_card(run_dir, card)
        review_file = root / "visible-review.md"
        review_file.write_text(
            (
                "---\n"
                "verdict: approved\n"
                "summary: Stage one approved.\n"
                "---\n\n"
                "Stage one is approved. Continue with the next stage.\n"
            ),
            encoding="utf-8",
        )
        registry.force_state("run-1", RunState.READY_FOR_REVIEW)

        try:
            submit_visible_review(run_dir, "run-1", review_file)
            _resolve_review(registry, registry.get_run("run-1"))
            output = self._wait_for(run_dir / "terminal.txt", "Stage one approved.")
        finally:
            supervisor.stop()

        review = json.loads((run_dir / "reviewer-verdict.json").read_text(encoding="utf-8"))
        self.assertTrue(review["continue"])
        self.assertIn("pid={}".format(registry.get_run("run-1").agent_pid), output)
        self.assertEqual(
            registry.get_run("run-1").state,
            RunState.WORKER_CONTINUING,
        )
        self.assertFalse((run_dir / "owner-completion-review-id").exists())

    @unittest.skipIf(sys.platform == "win32", "POSIX pty required")
    def test_visible_final_approval_auto_finalizes_and_notifies_worker(
        self,
    ) -> None:
        registry, supervisor, run_dir = self._managed_review_fixture()
        root = run_dir.parent
        task = run_dir / "assigned-task-latest.md"
        task.write_text("# Task\n", encoding="utf-8")
        plan = root / "project.json"
        plan.write_text('{"schema_version": 1}\n', encoding="utf-8")
        changed = root / "final.py"
        changed.write_text("ok = True\n", encoding="utf-8")
        card = create_review_card(
            run_id="run-1",
            project_slug="demo",
            stage_id="final",
            stage_title="Final acceptance",
            completion_scope="final",
            workspace_root=root,
            task_packet_path=task,
            plan_path=plan,
            work_summary="Completed final acceptance.",
            completed_items=["Created final.py"],
            not_completed_items=[],
            changed_files=[changed],
            artifact_paths=[],
            test_commands=["python3 -m unittest -v"],
            test_result_summary="All tests passed.",
            worker_claims=["Final task is complete."],
            known_issues=[],
            questions_for_reviewer=[],
        )
        queue_visible_review_card(run_dir, card)
        review_file = root / "visible-final-review.md"
        review_file.write_text(
            (
                "---\n"
                "verdict: approved\n"
                "summary: Final acceptance approved.\n"
                "---\n\n"
                "The full task is approved.\n"
            ),
            encoding="utf-8",
        )
        registry.force_state("run-1", RunState.READY_FOR_REVIEW)
        with registry._connect() as connection:
            connection.execute(
                """
                UPDATE runs
                SET reviewer_backend = ?, reviewer_thread_id = ?,
                    reviewer_thread_cwd = ?
                WHERE run_id = ?
                """,
                (
                    ReviewBackend.VISIBLE_THREAD.value,
                    "thread-1",
                    str(root),
                    "run-1",
                ),
            )
        coordinator = type(
            "Coordinator",
            (),
            {
                "reconcile_run": lambda self, run_id: None,
                "reconcile_pending": lambda self, run_id: None,
            },
        )()

        try:
            with patch("loopweave.cli._registry", return_value=registry), patch(
                "loopweave.cli._takeover_coordinator",
                return_value=coordinator,
            ):
                exit_code = main(
                    [
                        "review-submit",
                        "--run-id",
                        "run-1",
                        "--review-file",
                        str(review_file),
                    ]
                )
            output = self._wait_for(
                run_dir / "terminal.txt",
                "[LoopWeave review: approved]",
            )
        finally:
            supervisor.stop()

        self.assertEqual(exit_code, 0)
        self.assertIn("Final acceptance approved.", output)
        self.assertEqual(registry.get_run("run-1").state, RunState.APPROVED)
        self.assertTrue((run_dir / "owner-final-verdict.json").exists())
        self.assertEqual(
            json.loads(
                (run_dir / "owner-final-verdict.json").read_text(
                    encoding="utf-8"
                )
            )["verdict"],
            "approved",
        )

    @unittest.skipIf(sys.platform == "win32", "POSIX pty required")
    def test_review_is_not_delivered_after_process_identity_changes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            run_dir = root / "run-1"
            socket_path = root / "control.sock"
            fixture = Path(__file__).parent / "fixtures" / "echo_agent.py"
            supervisor = Supervisor(
                run_id="run-1",
                command=[sys.executable, "-u", str(fixture)],
                cwd=root,
                run_dir=run_dir,
                socket_path=socket_path,
                control_token="secret",
                passthrough=False,
            )
            pid = supervisor.start()
            registry = Registry(root / "registry.sqlite")
            registry.create_run(
                RunRecord(
                    run_id="run-1",
                    codex_thread_id="thread-1",
                    cwd=str(root),
                    tty="/dev/test",
                    agent="generic",
                    agent_pid=pid,
                    agent_process_start="wrong-start-time",
                    control_token="secret",
                    state=RunState.REVIEW_READY,
                    socket_path=str(socket_path),
                    run_dir=str(run_dir),
                )
            )
            (run_dir / "reviewer-verdict.md").write_text(
                "This must not be delivered.\n", encoding="utf-8"
            )
            (run_dir / "reviewer-verdict.json").write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "run_id": "run-1",
                        "review_id": "review-1",
                        "verdict": "changes_requested",
                        "summary": "Identity test.",
                        "review_file": "reviewer-verdict.md",
                        "continue": True,
                    }
                ),
                encoding="utf-8",
            )
            try:
                with self.assertRaises(RuntimeError):
                    _deliver_review(registry, registry.get_run("run-1"))
            finally:
                supervisor.stop()

            self.assertEqual(registry.get_run("run-1").state, RunState.ORPHANED)

    @unittest.skipIf(sys.platform == "win32", "POSIX pty required")
    def test_review_run_id_must_match(self) -> None:
        registry, supervisor, run_dir = self._managed_review_fixture(
            review_run_id="run-other"
        )
        try:
            with self.assertRaises(RuntimeError):
                _deliver_review(registry, registry.get_run("run-1"))
        finally:
            supervisor.stop()

    @unittest.skipIf(sys.platform == "win32", "POSIX pty required")
    def test_review_file_cannot_escape_run_directory(self) -> None:
        registry, supervisor, run_dir = self._managed_review_fixture(
            review_file="../outside.md"
        )
        (run_dir.parent / "outside.md").write_text("unsafe\n", encoding="utf-8")
        try:
            with self.assertRaises(RuntimeError):
                _deliver_review(registry, registry.get_run("run-1"))
        finally:
            supervisor.stop()

    @unittest.skipIf(sys.platform == "win32", "POSIX pty required")
    def test_same_review_is_delivered_only_once(self) -> None:
        registry, supervisor, run_dir = self._managed_review_fixture()
        try:
            _deliver_review(registry, registry.get_run("run-1"))
            _deliver_review(registry, registry.get_run("run-1"))
            output = self._wait_for(run_dir / "terminal.txt", "Review body.")
        finally:
            supervisor.stop()

        self.assertEqual(output.count("Review body."), 1)

    @unittest.skipIf(sys.platform == "win32", "POSIX pty required")
    def test_review_delivery_pauses_before_submitting_multiline_input(self) -> None:
        registry, supervisor, run_dir = self._managed_review_fixture()
        try:
            with patch("loopweave.cli.time.sleep") as sleep:
                _deliver_review(registry, registry.get_run("run-1"))
        finally:
            supervisor.stop()

        sleep.assert_called_once_with(0.35)

    @unittest.skipIf(sys.platform == "win32", "POSIX pty required")
    def test_pending_takeover_applies_after_exactly_once_delivery(self) -> None:
        registry, supervisor, run_dir = self._managed_review_fixture()
        registry.queue_thread_attach("run-1", "thread-2")
        try:
            _resolve_review(registry, registry.get_run("run-1"))
            output = self._wait_for(run_dir / "terminal.txt", "Review body.")
        finally:
            supervisor.stop()

        run = registry.get_run("run-1")
        self.assertEqual(output.count("Review body."), 1)
        self.assertEqual(run.codex_thread_id, "thread-2")
        self.assertEqual(run.binding_generation, 2)
        self.assertIsNone(run.pending_codex_thread_id)
        self.assertEqual(
            [
                binding.thread_id
                for binding in registry.list_thread_bindings("run-1")
            ],
            ["thread-1", "thread-2"],
        )

    @unittest.skipIf(sys.platform == "win32", "POSIX pty required")
    def test_final_approved_review_waits_for_owner_global_decision(self) -> None:
        registry, supervisor, run_dir = self._managed_review_fixture(
            verdict="approved",
            summary="All required checks passed.",
        )
        notifier = CompletionNotifier()
        try:
            _deliver_review(
                registry,
                registry.get_run("run-1"),
                completion_notifier=notifier,
            )
        finally:
            supervisor.stop()

        self.assertEqual(
            registry.get_run("run-1").state,
            RunState.OWNER_REVIEW_PENDING,
        )
        self.assertFalse((run_dir / "worker-approved-review-id").exists())
        self.assertTrue(
            (run_dir / "owner-completion-review-id").exists()
        )
        metadata = json.loads(
            (run_dir / "completion-notification.json").read_text(encoding="utf-8")
        )
        self.assertTrue(metadata["owner_review_pending"])
        self.assertFalse(metadata["owner_notified"])

    @unittest.skipIf(sys.platform == "win32", "POSIX pty required")
    def test_owner_approval_finalizes_pending_review_and_notifies_worker(self) -> None:
        registry, supervisor, run_dir = self._managed_review_fixture(
            verdict="approved",
            summary="All required checks passed.",
        )
        registry.force_state("run-1", RunState.OWNER_REVIEW_PENDING)

        try:
            _finalize_owner_review(
                registry,
                registry.get_run("run-1"),
                approved=True,
                message="reviewer global review passed.",
            )
            output = self._wait_for(
                run_dir / "terminal.txt",
                "[LoopWeave review: approved]",
            )
        finally:
            supervisor.stop()

        self.assertIn("task is complete", output.lower())
        self.assertIn("All required checks passed.", output)
        self.assertEqual(registry.get_run("run-1").state, RunState.APPROVED)
        self.assertEqual(
            (run_dir / "worker-approved-review-id").read_text(encoding="utf-8").strip(),
            "review-1",
        )
        verdict = json.loads(
            (run_dir / "owner-final-verdict.json").read_text(encoding="utf-8")
        )
        self.assertEqual(verdict["verdict"], "approved")

    @unittest.skipIf(sys.platform == "win32", "POSIX pty required")
    def test_owner_changes_requested_returns_run_to_worker_continuing(self) -> None:
        registry, supervisor, run_dir = self._managed_review_fixture(
            verdict="approved",
            summary="All required checks passed.",
        )
        registry.force_state("run-1", RunState.OWNER_REVIEW_PENDING)

        try:
            _finalize_owner_review(
                registry,
                registry.get_run("run-1"),
                approved=False,
                message="Re-check the cleanup path before final acceptance.",
            )
            output = self._wait_for(
                run_dir / "terminal.txt",
                "[Owner final review: changes requested]",
            )
        finally:
            supervisor.stop()

        self.assertIn("Re-check the cleanup path", output)
        self.assertEqual(
            registry.get_run("run-1").state,
            RunState.WORKER_CONTINUING,
        )
        self.assertFalse((run_dir / "worker-approved-review-id").exists())
        verdict = json.loads(
            (run_dir / "owner-final-verdict.json").read_text(encoding="utf-8")
        )
        self.assertEqual(verdict["verdict"], "changes_requested")

    @unittest.skipIf(sys.platform == "win32", "POSIX pty required")
    def test_owner_changes_requested_delivery_failure_remains_pending(self) -> None:
        registry, supervisor, run_dir = self._managed_review_fixture(
            verdict="approved",
            summary="All required checks passed.",
        )
        registry.force_state("run-1", RunState.OWNER_REVIEW_PENDING)

        try:
            def _raise_process_gone(pid):
                raise RuntimeError("process gone")

            with patch(
                "loopweave.terminal_host.default_process_identity_reader",
                return_value=_raise_process_gone,
            ):
                with self.assertRaises(RuntimeError):
                    _finalize_owner_review(
                        registry,
                        registry.get_run("run-1"),
                        approved=False,
                        message="Re-check the cleanup path before final acceptance.",
                    )
        finally:
            supervisor.stop()

        self.assertEqual(
            registry.get_run("run-1").state,
            RunState.OWNER_REVIEW_PENDING,
        )
        self.assertFalse((run_dir / "owner-final-verdict.json").exists())
        self.assertTrue((run_dir / "owner-final-delivery-error.txt").exists())

    @unittest.skipIf(sys.platform == "win32", "POSIX pty required")
    def test_stage_approved_review_returns_worker_to_continuing_without_owner_completion(
        self,
    ) -> None:
        registry, supervisor, run_dir = self._managed_review_fixture(
            verdict="approved",
            summary="Task 3 passed its quality gate.",
            should_continue=False,
        )
        (run_dir / "review-request.json").write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "run_id": "run-1",
                    "status": "ready_for_review",
                    "task_summary": "Review Task 3",
                    "change_summary": "Task 3 complete.",
                    "files_changed": ["store.py"],
                    "commands_run": ["python3 -m unittest"],
                    "tests": [],
                    "known_issues": [],
                    "questions_for_reviewer": [],
                    "mode": "develop",
                    "review_round": 1,
                    "completion_scope": "stage",
                    "evidence_fingerprint": "a" * 64,
                }
            ),
            encoding="utf-8",
        )

        class RejectingNotifier:
            def notify(self, *args, **kwargs):
                raise AssertionError("stage approval must not notify final completion")

        try:
            _deliver_review(
                registry,
                registry.get_run("run-1"),
                completion_notifier=RejectingNotifier(),
            )
            output = self._wait_for(
                run_dir / "terminal.txt",
                "[LoopWeave review: stage approved]",
            )
        finally:
            supervisor.stop()

        self.assertIn("continue to the next stage", output.lower())
        self.assertEqual(
            registry.get_run("run-1").state,
            RunState.WORKER_CONTINUING,
        )
        self.assertFalse((run_dir / "worker-approved-review-id").exists())
        self.assertFalse((run_dir / "owner-completion-review-id").exists())
        review = json.loads(
            (run_dir / "reviewer-verdict.json").read_text(encoding="utf-8")
        )
        self.assertTrue(review["continue"])

    @unittest.skipIf(sys.platform == "win32", "POSIX pty required")
    def test_legacy_approved_continue_without_scope_is_final(self) -> None:
        registry, supervisor, run_dir = self._managed_review_fixture(
            verdict="approved",
            summary="Legacy approval.",
            should_continue=True,
        )

        class FakeNotifier:
            def __init__(self):
                self.calls = []

            def notify_owner(self, run, review):
                self.calls.append((run.run_id, review["review_id"]))
                return True

        notifier = FakeNotifier()
        try:
            _deliver_review(
                registry,
                registry.get_run("run-1"),
                completion_notifier=notifier,
            )
        finally:
            supervisor.stop()

        self.assertEqual(
            registry.get_run("run-1").state,
            RunState.OWNER_REVIEW_PENDING,
        )
        self.assertEqual(len(notifier.calls), 1)
        review = json.loads(
            (run_dir / "reviewer-verdict.json").read_text(encoding="utf-8")
        )
        self.assertFalse(review["continue"])

    @unittest.skipIf(sys.platform == "win32", "POSIX pty required")
    def test_stage_scope_canonicalizes_approved_review_to_continue(self) -> None:
        registry, supervisor, run_dir = self._managed_review_fixture()
        (run_dir / "review-request.json").write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "run_id": "run-1",
                    "status": "ready_for_review",
                    "task_summary": "Review Task 3",
                    "change_summary": "Task 3 complete.",
                    "files_changed": ["store.py"],
                    "commands_run": ["python3 -m unittest"],
                    "tests": [],
                    "known_issues": [],
                    "questions_for_reviewer": [],
                    "mode": "develop",
                    "review_round": 1,
                    "completion_scope": "stage",
                    "evidence_fingerprint": "a" * 64,
                }
            ),
            encoding="utf-8",
        )

        def dispatch(*args, **kwargs):
            (run_dir / "reviewer-verdict.md").write_text(
                "# Review\n\nTask 3 passed.\n",
                encoding="utf-8",
            )
            (run_dir / "reviewer-verdict.json").write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "run_id": "run-1",
                        "review_id": "review-stage",
                        "verdict": "approved",
                        "summary": "Task 3 passed.",
                        "review_file": "reviewer-verdict.md",
                        "continue": False,
                    }
                ),
                encoding="utf-8",
            )

        coordinator = type(
            "Coordinator",
            (),
            {"reconcile_pending": lambda self, run_id: None},
        )()
        try:
            with patch(
                "loopweave.cli.CodexDispatcher"
            ) as dispatcher_class, patch(
                "loopweave.cli._takeover_coordinator",
                return_value=coordinator,
            ):
                dispatcher_class.return_value.dispatch.side_effect = dispatch
                _dispatch_and_deliver(registry, registry.get_run("run-1"))
            output = self._wait_for(
                run_dir / "terminal.txt",
                "[LoopWeave review: stage approved]",
            )
        finally:
            supervisor.stop()

        review = json.loads(
            (run_dir / "reviewer-verdict.json").read_text(encoding="utf-8")
        )
        self.assertTrue(review["continue"])
        self.assertIn("Task 3 passed.", output)
        self.assertEqual(
            registry.get_run("run-1").state,
            RunState.WORKER_CONTINUING,
        )

    @unittest.skipIf(sys.platform == "win32", "POSIX pty required")
    def test_approved_review_retries_missing_completion_channel(self) -> None:
        registry, supervisor, run_dir = self._managed_review_fixture(
            verdict="approved"
        )
        (run_dir / "delivered-review-id").write_text(
            "review-1\n", encoding="utf-8"
        )

        class FakeNotifier:
            def __init__(self):
                self.calls = []

            def notify_owner(self, run, review):
                self.calls.append((run.run_id, review["review_id"]))
                return True

        notifier = FakeNotifier()
        try:
            _deliver_review(
                registry,
                registry.get_run("run-1"),
                completion_notifier=notifier,
            )
        finally:
            supervisor.stop()

        self.assertEqual(len(notifier.calls), 1)
        self.assertEqual(notifier.calls[0], ("run-1", "review-1"))

    def test_design_round_two_revision_is_canonicalized_to_needs_human(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            run_dir = root / "run-1"
            run_dir.mkdir()
            registry = Registry(root / "registry.sqlite")
            registry.create_run(
                RunRecord(
                    run_id="run-1",
                    codex_thread_id="thread-1",
                    cwd=str(root),
                    tty="/dev/test",
                    agent="generic",
                    agent_pid=123,
                    agent_process_start="start",
                    control_token="secret",
                    state=RunState.READY_FOR_REVIEW,
                    mode=RunMode.DESIGN,
                    review_loop=2,
                    run_dir=str(run_dir),
                )
            )
            (run_dir / "review-request.json").write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "run_id": "run-1",
                        "status": "ready_for_review",
                        "task_summary": "Review design",
                        "change_summary": "Second proposal revision",
                        "files_changed": ["proposal.md"],
                        "commands_run": [],
                        "tests": [],
                        "known_issues": [],
                        "questions_for_reviewer": [],
                        "mode": "design",
                        "review_round": 2,
                        "evidence_fingerprint": "a" * 64,
                    }
                ),
                encoding="utf-8",
            )

            def dispatch(*args, **kwargs):
                (run_dir / "reviewer-verdict.md").write_text(
                    "# Review\n\nOwnership is unresolved.\n",
                    encoding="utf-8",
                )
                (run_dir / "reviewer-verdict.json").write_text(
                    json.dumps(
                        {
                            "schema_version": 1,
                            "run_id": "run-1",
                            "review_id": "review-1",
                            "verdict": "changes_requested",
                            "summary": "Resolve ownership.",
                            "review_file": "reviewer-verdict.md",
                            "continue": True,
                            "round": 2,
                            "blocking_decisions": [
                                "Choose the state owner."
                            ],
                            "advisory_notes": [],
                            "consensus_summary": (
                                "The file protocol remains shared."
                            ),
                            "next_action": "revise_design",
                        }
                    ),
                    encoding="utf-8",
                )

            coordinator = type(
                "Coordinator",
                (),
                {"reconcile_pending": lambda self, run_id: None},
            )()
            with patch(
                "loopweave.cli.CodexDispatcher"
            ) as dispatcher_class, patch(
                "loopweave.cli._takeover_coordinator",
                return_value=coordinator,
            ):
                dispatcher_class.return_value.dispatch.side_effect = dispatch
                _dispatch_and_deliver(registry, registry.get_run("run-1"))

            review = json.loads(
                (run_dir / "reviewer-verdict.json").read_text(encoding="utf-8")
            )
            self.assertEqual(review["verdict"], "needs_human")
            self.assertFalse(review["continue"])
            self.assertEqual(review["next_action"], "await_human")
            self.assertEqual(
                registry.get_run("run-1").state, RunState.NEEDS_HUMAN
            )

    def _managed_review_fixture(
        self,
        review_run_id: str = "run-1",
        review_file: str = "reviewer-verdict.md",
        verdict: str = "changes_requested",
        summary: str = "One change required.",
        should_continue=None,
    ):
        root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, root, True)
        run_dir = root / "run-1"
        socket_path = root / "control.sock"
        fixture = Path(__file__).parent / "fixtures" / "echo_agent.py"
        supervisor = Supervisor(
            run_id="run-1",
            command=[sys.executable, "-u", str(fixture)],
            cwd=root,
            run_dir=run_dir,
            socket_path=socket_path,
            control_token="secret",
            passthrough=False,
        )
        pid = supervisor.start()
        agent_process_start = process_start_time(pid)
        registry = Registry(root / "registry.sqlite")
        registry.create_run(
            RunRecord(
                run_id="run-1",
                codex_thread_id="thread-1",
                cwd=str(root),
                tty="/dev/test",
                agent="generic",
                agent_pid=pid,
                agent_process_start=agent_process_start,
                control_token="secret",
                state=RunState.REVIEW_READY,
                socket_path=str(socket_path),
                run_dir=str(run_dir),
            )
        )
        (run_dir / "run.json").write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "run_id": "run-1",
                    "codex_thread_id": "thread-1",
                    "cwd": str(root),
                    "tty": "/dev/test",
                    "agent": "generic",
                    "agent_pid": pid,
                    "agent_process_start": agent_process_start,
                    "socket_path": str(socket_path),
                }
            ),
            encoding="utf-8",
        )
        (run_dir / "reviewer-verdict.md").write_text("Review body.\n", encoding="utf-8")
        (run_dir / "reviewer-verdict.json").write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "run_id": review_run_id,
                    "review_id": "review-1",
                    "verdict": verdict,
                    "summary": summary,
                    "review_file": review_file,
                    "continue": (
                        verdict == "changes_requested"
                        if should_continue is None
                        else should_continue
                    ),
                }
            ),
            encoding="utf-8",
        )
        return registry, supervisor, run_dir

    def _wait_for(self, path: Path, text: str, timeout: float = 3.0) -> str:
        deadline = time.time() + timeout
        while time.time() < deadline:
            output = (
                path.read_text(encoding="utf-8", errors="replace")
                if path.exists()
                else ""
            )
            if text in output:
                return output
            time.sleep(0.02)
        self.fail("Timed out waiting for {!r}".format(text))

    @unittest.skipIf(sys.platform == "win32", "POSIX pty required")
    def test_generic_worker_completes_stage_review_and_final_in_same_session(
        self,
    ) -> None:
        """Package 1 Stage 3 integration proof: an arbitrary generic fixture
        CLI (no Claude Stop Hook, no Claude transcript parsing) submits a stage
        result, the owner's verdict is accepted and delivered back to the SAME
        managed session, and the worker resumes in that same session to submit a
        final result. Proves product invariants 1 (exact managed session) and 5
        (review returns to the same session) hold for the vendor-neutral path."""
        from loopweave.visible_review import submit_visible_review

        root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, root, True)
        control = root / "control"
        var = control / "var"
        var.mkdir(parents=True)
        workspace = root / "workspace"
        workspace.mkdir()
        run_dir = control / "run-1"
        run_dir.mkdir(parents=True)
        socket_path = control / "control.sock"
        registry = Registry(var / "registry.sqlite")
        worker = (
            Path(__file__).resolve().parent / "fixtures" / "generic_fixture_worker.py"
        )
        project_src = Path(__file__).resolve().parents[1] / "src"

        prior_home = os.environ.get("LOOPWEAVE_HOME")
        prior_test_src = os.environ.get("LOOPWEAVE_TEST_SRC")
        prior_ws = os.environ.get("LOOPWEAVE_FIXTURE_WORKSPACE")
        os.environ["LOOPWEAVE_HOME"] = str(control)
        os.environ["LOOPWEAVE_TEST_SRC"] = str(project_src)
        os.environ["LOOPWEAVE_FIXTURE_WORKSPACE"] = str(workspace)

        supervisor = Supervisor(
            run_id="run-1",
            command=[sys.executable, "-u", str(worker)],
            cwd=workspace,
            run_dir=run_dir,
            socket_path=socket_path,
            control_token="secret",
            passthrough=False,
        )
        try:
            pid = supervisor.start()
            agent_start = process_start_time(pid)
            registry.create_run(
                RunRecord(
                    run_id="run-1",
                    codex_thread_id="thread-1",
                    cwd=str(workspace),
                    thread_cwd=str(workspace),
                    workspace_root=str(workspace),
                    tty="/dev/test",
                    agent="generic",
                    agent_pid=pid,
                    agent_process_start=agent_start,
                    control_token="secret",
                    state=RunState.RUNNING,
                    socket_path=str(socket_path),
                    run_dir=str(run_dir),
                    reviewer_backend=ReviewBackend.VISIBLE_THREAD,
                    reviewer_thread_id="thread-1",
                    reviewer_thread_cwd=str(workspace),
                )
            )
            # Visible review cards require a task packet and a plan path.
            (run_dir / "assigned-task-latest.md").write_text(
                "# Task\n", encoding="utf-8"
            )
            (run_dir / "run.json").write_text(
                '{"schema_version": 1}\n', encoding="utf-8"
            )

            # The worker submits its stage; wait for the marker it writes.
            self._wait_for(workspace / "stage-marker", "done", timeout=30)
            self.assertEqual(
                registry.get_run("run-1").state, RunState.READY_FOR_REVIEW
            )
            self.assertTrue((run_dir / "review-inbox" / "pending").exists())
            self.assertEqual(registry.get_run("run-1").agent_pid, pid)

            # Owner accepts a verdict (changes_requested) and delivers it back.
            stage_review = root / "stage-review.md"
            stage_review.write_text(
                (
                    "---\n"
                    "verdict: changes_requested\n"
                    "summary: Proceed to the final stage.\n"
                    "---\n\n"
                    "Proceed to the final stage.\n"
                ),
                encoding="utf-8",
            )
            submit_visible_review(run_dir, "run-1", stage_review)
            registry.force_state("run-1", RunState.REVIEW_READY)
            _resolve_review(registry, registry.get_run("run-1"))

            # The same session received the review and resumed: the worker now
            # submits its final result.
            self._wait_for(workspace / "final-marker", "done", timeout=30)
            self.assertEqual(
                registry.get_run("run-1").state, RunState.READY_FOR_REVIEW
            )
            self.assertEqual(registry.get_run("run-1").agent_pid, pid)

            done_output = self._wait_for(
                run_dir / "terminal.txt", "WORKER_DONE", timeout=10
            )
        finally:
            supervisor.stop()
            for key, value in (
                ("LOOPWEAVE_HOME", prior_home),
                ("LOOPWEAVE_TEST_SRC", prior_test_src),
                ("LOOPWEAVE_FIXTURE_WORKSPACE", prior_ws),
            ):
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value

        self.assertIn("pid={}".format(pid), done_output)
        self.assertTrue((workspace / "stage1.txt").exists())
        self.assertTrue((workspace / "final.txt").exists())


if __name__ == "__main__":
    unittest.main()
