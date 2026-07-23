from __future__ import annotations

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from loopweave.cli import (
    _explicit_thread_id,
    _manual_completion_scope,
    _pending_visible_review_run,
    build_parser,
    doctor_checks,
    main,
    render_runs,
    render_runs_json,
    render_status_json,
)
from loopweave.models import ReviewBackend, RunMode, RunRecord, RunState
from loopweave.protocol import ProtocolError
from loopweave.registry import Registry
from loopweave.thread_takeover import AttachResult
from loopweave.visible_review import create_review_card, queue_visible_review_card


class CliTests(unittest.TestCase):
    def test_manual_review_scope_is_resolved_from_run_mode(self) -> None:
        parser = build_parser()

        default = parser.parse_args(
            ["request-review", "--run-id", "run-1"]
        )
        stage = parser.parse_args(
            ["request-review", "--run-id", "run-1", "--stage"]
        )
        final = parser.parse_args(
            ["request-review", "--run-id", "run-1", "--final"]
        )

        self.assertIsNone(default.completion_scope)
        self.assertEqual(stage.completion_scope, "stage")
        self.assertEqual(final.completion_scope, "final")
        self.assertEqual(
            _manual_completion_scope(RunMode.DEVELOP, None),
            "stage",
        )
        self.assertEqual(
            _manual_completion_scope(RunMode.DESIGN, None),
            "final",
        )
        self.assertEqual(
            _manual_completion_scope(RunMode.DEVELOP, "final"),
            "final",
        )

    def test_codex_native_thread_id_is_used_automatically(self) -> None:
        args = Mock(thread=None)

        with patch.dict(
            "os.environ",
            {
                "CODEX_THREAD_ID": "codex-thread",
                "LOOPWEAVE_THREAD_ID": "loopweave-thread",
            },
            clear=True,
        ):
            result = _explicit_thread_id(args)

        self.assertEqual(result, "codex-thread")

    def test_explicit_thread_overrides_environment(self) -> None:
        args = Mock(thread="explicit-thread")

        with patch.dict(
            "os.environ",
            {"CODEX_THREAD_ID": "codex-thread"},
            clear=True,
        ):
            result = _explicit_thread_id(args)

        self.assertEqual(result, "explicit-thread")

    def test_parser_accepts_standard_named_adapter_command(self) -> None:
        parser = build_parser()

        args = parser.parse_args(["run", "claude", "--thread", "thread-1"])

        self.assertEqual(args.command, "run")
        self.assertEqual(args.agent, "claude")
        self.assertEqual(args.thread, "thread-1")

    def test_parser_accepts_generic_command_after_separator(self) -> None:
        parser = build_parser()

        args = parser.parse_args(
            ["run", "generic", "--thread", "thread-1", "--", "custom-agent", "--flag"]
        )

        self.assertEqual(args.agent, "generic")
        self.assertEqual(args.agent_args, ["custom-agent", "--flag"])

    def test_parser_forwards_short_agent_option_without_separator(self) -> None:
        parser = build_parser()

        args = parser.parse_args(["run", "claude", "-r"])

        self.assertEqual(args.agent, "claude")
        self.assertEqual(args.mode, "develop")
        self.assertEqual(args.agent_args, ["-r"])

    def test_parser_consumes_explicit_design_mode(self) -> None:
        parser = build_parser()

        args = parser.parse_args(
            [
                "run",
                "claude",
                "--mode",
                "design",
                "--resume",
                "claude-session-1",
            ]
        )

        self.assertEqual(args.mode, "design")
        self.assertEqual(args.agent_args, ["--resume", "claude-session-1"])

    def test_separator_forces_mode_option_to_agent(self) -> None:
        parser = build_parser()

        args = parser.parse_args(
            ["run", "claude", "--", "--mode", "agent-owned-value"]
        )

        self.assertEqual(args.mode, "develop")
        self.assertEqual(args.agent_args, ["--mode", "agent-owned-value"])

    def test_parser_accepts_project_and_external_workspace(self) -> None:
        parser = build_parser()

        args = parser.parse_args(
            [
                "run",
                "claude",
                "--project",
                "example-project",
                "--workspace",
                "/workspace/example",
                "-r",
            ]
        )

        self.assertEqual(args.project, "example-project")
        self.assertEqual(args.workspace, "/workspace/example")
        self.assertEqual(args.agent_args, ["-r"])

    def test_parser_accepts_visible_thread_reviewer_backend(self) -> None:
        parser = build_parser()

        args = parser.parse_args(
            [
                "run",
                "claude",
                "--project",
                "demo",
                "--reviewer",
                "visible-thread",
            ]
        )

        self.assertEqual(args.reviewer, "visible-thread")

    def test_separator_forces_workspace_option_to_agent(self) -> None:
        parser = build_parser()

        args = parser.parse_args(
            ["run", "claude", "--", "--workspace", "agent-owned-value"]
        )

        self.assertIsNone(args.workspace)
        self.assertEqual(
            args.agent_args, ["--workspace", "agent-owned-value"]
        )

    def test_parser_forwards_resume_session_id_in_original_order(self) -> None:
        parser = build_parser()

        args = parser.parse_args(
            [
                "run",
                "claude",
                "--thread",
                "thread-1",
                "--resume",
                "claude-session-1",
            ]
        )

        self.assertEqual(args.thread, "thread-1")
        self.assertEqual(
            args.agent_args, ["--resume", "claude-session-1"]
        )

    def test_separator_forces_bridge_named_option_to_agent(self) -> None:
        parser = build_parser()

        args = parser.parse_args(
            ["run", "claude", "--", "--thread", "agent-owned-value"]
        )

        self.assertIsNone(args.thread)
        self.assertEqual(
            args.agent_args, ["--thread", "agent-owned-value"]
        )

    def test_parser_accepts_attach_with_optional_thread(self) -> None:
        parser = build_parser()

        automatic = parser.parse_args(["attach", "run-1"])
        explicit = parser.parse_args(
            ["attach", "run-1", "--thread", "thread-2"]
        )

        self.assertEqual(automatic.command, "attach")
        self.assertEqual(automatic.run_id, "run-1")
        self.assertIsNone(automatic.thread)
        self.assertEqual(explicit.thread, "thread-2")

    def test_parser_status_accepts_json_flag(self) -> None:
        parser = build_parser()

        plain = parser.parse_args(["status", "run-1"])
        flagged = parser.parse_args(["status", "run-1", "--json"])
        no_run_id = parser.parse_args(["status", "--json"])

        self.assertEqual(plain.command, "status")
        self.assertEqual(plain.run_id, "run-1")
        self.assertFalse(plain.json)
        self.assertTrue(flagged.json)
        self.assertEqual(flagged.run_id, "run-1")
        self.assertIsNone(no_run_id.run_id)
        self.assertTrue(no_run_id.json)

    def test_render_runs_is_scannable(self) -> None:
        runs = [
            RunRecord(
                run_id="run-1",
                codex_thread_id="thread-1",
                cwd="/tmp/project",
                tty="/dev/ttys001",
                agent="claude",
                agent_pid=123,
                agent_process_start="start",
                control_token="secret",
                state=RunState.RUNNING,
                pending_codex_thread_id="thread-2",
                binding_generation=3,
            )
        ]

        output = render_runs(runs)

        self.assertIn("GEN", output)
        self.assertIn("PENDING", output)
        self.assertIn("run-1", output)
        self.assertIn("claude", output)
        self.assertIn("running", output)
        self.assertIn("3", output)
        self.assertIn("thread-2", output)
        self.assertNotIn("secret", output)

    def test_attach_command_reports_result(self) -> None:
        registry = Mock()
        coordinator = Mock()
        coordinator.attach.return_value = AttachResult(
            status="queued",
            run_id="run-1",
            old_thread_id="thread-1",
            new_thread_id="thread-2",
            binding_generation=2,
        )
        output = io.StringIO()

        with patch("loopweave.cli._registry", return_value=registry), patch(
            "loopweave.cli._takeover_coordinator",
            return_value=coordinator,
        ), patch("sys.stdout", output):
            exit_code = main(
                ["attach", "run-1", "--thread", "thread-2"]
            )

        self.assertEqual(exit_code, 0)
        coordinator.reconcile_run.assert_called_once_with("run-1")
        coordinator.attach.assert_called_once_with("run-1", "thread-2")
        self.assertIn("queued", output.getvalue())
        self.assertIn("thread-2", output.getvalue())

    def test_status_reconciles_selected_run_before_rendering(self) -> None:
        run = RunRecord(
            run_id="run-1",
            codex_thread_id="thread-1",
            cwd="/tmp/project",
            tty="/dev/test",
            agent="claude",
            agent_pid=123,
            agent_process_start="start",
            control_token="secret",
            state=RunState.RUNNING,
        )
        registry = Mock()
        registry.get_run.return_value = run
        coordinator = Mock()
        output = io.StringIO()

        with patch("loopweave.cli._registry", return_value=registry), patch(
            "loopweave.cli._takeover_coordinator",
            return_value=coordinator,
        ), patch(
            "loopweave.terminal_host.default_process_identity_reader", return_value=lambda pid: "start"
        ), patch("sys.stdout", output):
            exit_code = main(["status", "run-1"])

        self.assertEqual(exit_code, 0)
        coordinator.reconcile_run.assert_called_once_with("run-1")
        self.assertIn("run-1", output.getvalue())

    def test_status_marks_missing_assignable_worker_orphaned(self) -> None:
        run = _status_run(state=RunState.RUNNING)
        orphaned = _status_run(state=RunState.ORPHANED)
        registry = Mock()
        registry.get_run.side_effect = [run, run, orphaned]
        coordinator = Mock()
        output = io.StringIO()

        def _raise_gone(pid):
            raise RuntimeError("gone")

        with patch("loopweave.cli._registry", return_value=registry), patch(
            "loopweave.cli._takeover_coordinator",
            return_value=coordinator,
        ), patch(
            "loopweave.terminal_host.default_process_identity_reader",
            return_value=_raise_gone,
        ), patch("sys.stdout", output):
            exit_code = main(["status", "run-1"])

        self.assertEqual(exit_code, 0)
        registry.force_state.assert_called_once_with(
            "run-1", RunState.ORPHANED
        )
        self.assertIn("orphaned", output.getvalue())

    def test_status_without_json_keeps_tabular_text_output(self) -> None:
        run = _status_run()
        registry = Mock()
        registry.get_run.return_value = run
        coordinator = Mock()
        output = io.StringIO()

        with patch("loopweave.cli._registry", return_value=registry), patch(
            "loopweave.cli._takeover_coordinator", return_value=coordinator
        ), patch("sys.stdout", output):
            exit_code = main(["status", "run-1"])

        self.assertEqual(exit_code, 0)
        expected = (
            "RUN ID\tAGENT\tSTATE\tPID\tGEN\tTHREAD\tPENDING\n"
            "run-1\tclaude\trunning\t123\t2\tthread-1\t-\n"
        )
        self.assertEqual(output.getvalue(), expected)

    def test_manual_design_review_honors_two_round_limit(self) -> None:
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
                    tty="",
                    agent="claude",
                    agent_pid=123,
                    agent_process_start="start",
                    control_token="secret",
                    state=RunState.RUNNING,
                    mode=RunMode.DESIGN,
                    run_dir=str(run_dir),
                )
            )
            rounds = []

            def dispatch(current_registry, run):
                request = json.loads(
                    (run_dir / "review-request.json").read_text(encoding="utf-8")
                )
                rounds.append(request["review_round"])

            coordinator = Mock()
            output = io.StringIO()
            with patch(
                "loopweave.cli._registry", return_value=registry
            ), patch(
                "loopweave.cli._takeover_coordinator",
                return_value=coordinator,
            ), patch(
                "loopweave.cli._dispatch_and_deliver",
                side_effect=dispatch,
            ), patch(
                "sys.stdout", output
            ):
                self.assertEqual(
                    main(
                        [
                            "request-review",
                            "--run-id",
                            "run-1",
                            "--summary",
                            "round one",
                        ]
                    ),
                    0,
                )
                self.assertEqual(
                    main(
                        [
                            "request-review",
                            "--run-id",
                            "run-1",
                            "--summary",
                            "round two",
                        ]
                    ),
                    0,
                )
                self.assertEqual(
                    main(
                        [
                            "request-review",
                            "--run-id",
                            "run-1",
                            "--summary",
                            "round three",
                        ]
                    ),
                    0,
                )

            self.assertEqual(rounds, [1, 2])
            self.assertEqual(registry.get_run("run-1").review_loop, 2)
            self.assertEqual(
                registry.get_run("run-1").state, RunState.NEEDS_HUMAN
            )

    def test_manual_visible_review_queues_card_without_dispatching(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            run_dir = root / "run-1"
            run_dir.mkdir()
            (run_dir / "assigned-task-latest.md").write_text(
                "# Task\n", encoding="utf-8"
            )
            (run_dir / "run.json").write_text("{}\n", encoding="utf-8")
            registry = Registry(root / "registry.sqlite")
            registry.create_run(
                RunRecord(
                    run_id="run-1",
                    codex_thread_id="thread-1",
                    cwd=str(root),
                    thread_cwd=str(root),
                    workspace_root=str(root),
                    tty="/dev/test",
                    agent="claude",
                    agent_pid=123,
                    agent_process_start="start",
                    control_token="secret",
                    state=RunState.RUNNING,
                    run_dir=str(run_dir),
                    reviewer_backend=ReviewBackend.VISIBLE_THREAD,
                    reviewer_thread_id="thread-1",
                    reviewer_thread_cwd=str(root),
                )
            )
            coordinator = Mock()

            with patch("loopweave.cli._registry", return_value=registry), patch(
                "loopweave.cli._takeover_coordinator",
                return_value=coordinator,
            ), patch(
                "loopweave.cli._dispatch_and_deliver",
                side_effect=AssertionError("visible review must not dispatch"),
            ):
                code = main(
                    [
                        "request-review",
                        "--run-id",
                        "run-1",
                        "--stage",
                        "--summary",
                        "manual visible stage",
                    ]
                )

            self.assertEqual(code, 0)
            self.assertTrue((run_dir / "review-inbox" / "pending").exists())
            self.assertEqual(registry.get_run("run-1").state, RunState.READY_FOR_REVIEW)
            self.assertEqual(registry.get_run("run-1").review_loop, 1)
            events = (run_dir / "events.jsonl").read_text(encoding="utf-8")
            self.assertIn("visible_review_card_queued", events)

    def test_doctor_reports_missing_and_present_commands(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            existing = Path(directory) / "tool"
            existing.write_text("", encoding="utf-8")
            output = io.StringIO()

            healthy = doctor_checks(
                output,
                commands={"present": str(existing), "missing": "/no/such/tool"},
            )

        self.assertFalse(healthy)
        text = output.getvalue()
        self.assertIn("present: ok", text)
        self.assertIn("missing: missing", text)

    def test_parser_accepts_assign_with_run_id_or_latest(self) -> None:
        parser = build_parser()

        by_id = parser.parse_args(
            ["assign", "--run-id", "run-1", "--task-file", "/tmp/task.md"]
        )
        latest = parser.parse_args(
            ["assign", "--latest", "--task-file", "/tmp/task.md"]
        )

        self.assertEqual(by_id.command, "assign")
        self.assertEqual(by_id.run_id, "run-1")
        self.assertFalse(by_id.latest)
        self.assertEqual(by_id.task_file, "/tmp/task.md")
        self.assertTrue(latest.latest)
        self.assertIsNone(latest.run_id)

    def test_parser_accepts_finalize_approval_or_changes(self) -> None:
        parser = build_parser()

        approved = parser.parse_args(
            ["finalize", "--run-id", "run-1", "--approve"]
        )
        changes = parser.parse_args(
            [
                "finalize",
                "--run-id",
                "run-1",
                "--changes-requested",
                "--message-file",
                "/tmp/review.md",
            ]
        )

        self.assertEqual(approved.command, "finalize")
        self.assertEqual(approved.run_id, "run-1")
        self.assertTrue(approved.approve)
        self.assertFalse(approved.changes_requested)
        self.assertTrue(changes.changes_requested)
        self.assertEqual(changes.message_file, "/tmp/review.md")

    def test_parser_accepts_review_next(self) -> None:
        parser = build_parser()

        args = parser.parse_args(["review-next", "--run-id", "run-1"])

        self.assertEqual(args.command, "review-next")
        self.assertEqual(args.run_id, "run-1")

    def test_parser_accepts_review_submit(self) -> None:
        parser = build_parser()

        args = parser.parse_args(
            [
                "review-submit",
                "--review-file",
                "/tmp/loopweave-visible-review.md",
            ]
        )

        self.assertEqual(args.command, "review-submit")
        self.assertIsNone(args.run_id)
        self.assertEqual(args.review_file, "/tmp/loopweave-visible-review.md")

        explicit = parser.parse_args(
            [
                "review-submit",
                "--run-id",
                "run-1",
                "--review-file",
                "/tmp/loopweave-visible-review.md",
            ]
        )
        self.assertEqual(explicit.run_id, "run-1")

    def test_parser_accepts_review_heartbeat(self) -> None:
        parser = build_parser()

        args = parser.parse_args(["review-heartbeat", "--json"])

        self.assertEqual(args.command, "review-heartbeat")
        self.assertTrue(args.json)

    def test_parser_accepts_reviewer_bind(self) -> None:
        parser = build_parser()

        args = parser.parse_args(
            [
                "reviewer",
                "bind",
                "--run-id",
                "run-1",
                "--thread",
                "thread-2",
            ]
        )

        self.assertEqual(args.command, "reviewer")
        self.assertEqual(args.reviewer_command, "bind")
        self.assertEqual(args.run_id, "run-1")
        self.assertEqual(args.thread, "thread-2")

    def test_parser_does_not_expose_visible_retry_controls(self) -> None:
        parser = build_parser()

        with self.assertRaises(SystemExit):
            parser.parse_args(["reviewer", "retry-start", "--run-id", "run-1"])

    def test_doctor_uses_dynamic_codex_resolution(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            binary = Path(directory) / "codex"
            binary.write_text("", encoding="utf-8")
            output = io.StringIO()

            with patch("loopweave.cli.resolve_codex_bin", return_value=binary):
                healthy = doctor_checks(output)

        self.assertTrue(healthy)
        self.assertIn("codex: ok", output.getvalue())

    def test_manual_visible_review_only_queues_card(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            registry = Registry(root / "registry.sqlite")
            run = self._visible_run(root, "run-1")
            registry.create_run(run)
            task = root / "task.md"
            task.write_text("# Task\n", encoding="utf-8")
            run_dir = Path(run.run_dir)
            (run_dir / "assigned-task-latest.md").parent.mkdir(
                parents=True,
                exist_ok=True,
            )
            (run_dir / "assigned-task-latest.md").write_text(
                "# Task\n", encoding="utf-8"
            )
            (run_dir / "run.json").write_text("{}\n", encoding="utf-8")
            coordinator = Mock()

            with patch("loopweave.cli._registry", return_value=registry), patch(
                "loopweave.cli._takeover_coordinator",
                return_value=coordinator,
            ):
                code = main(
                    [
                        "request-review",
                        "--run-id",
                        "run-1",
                        "--stage",
                        "--summary",
                        "manual visible stage",
                    ]
                )
                pending_exists = (run_dir / "review-inbox" / "pending").exists()

        self.assertEqual(code, 0)
        self.assertTrue(pending_exists)

    def test_main_finalize_forwards_owner_global_verdict(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            message_file = root / "owner-review.md"
            message_file.write_text("Fix the final cleanup path.\n", encoding="utf-8")
            run = RunRecord(
                run_id="run-1",
                codex_thread_id="thread-1",
                cwd=str(root),
                tty="/dev/test",
                agent="claude",
                agent_pid=123,
                agent_process_start="start",
                control_token="secret",
                state=RunState.OWNER_REVIEW_PENDING,
                run_dir=str(root / "run-1"),
            )
            registry = Mock()
            registry.get_run.return_value = run

            coordinator = Mock()
            with patch("loopweave.cli._registry", return_value=registry), patch(
                "loopweave.cli._takeover_coordinator",
                return_value=coordinator,
            ), patch("loopweave.cli._finalize_owner_review") as finalize:
                code = main(
                    [
                        "finalize",
                        "--run-id",
                        "run-1",
                        "--changes-requested",
                        "--message-file",
                        str(message_file),
                    ]
                )

        self.assertEqual(code, 0)
        coordinator.reconcile_run.assert_called_once_with("run-1")
        finalize.assert_called_once_with(
            registry,
            run,
            approved=False,
            message="Fix the final cleanup path.\n",
        )

    def test_pending_visible_review_run_selects_only_pending_card(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            registry = Registry(root / "registry.sqlite")
            first = self._visible_run(root, "run-1")
            second = self._visible_run(root, "run-2")
            registry.create_run(first)
            registry.create_run(second)
            self._queue_visible_card(root, "run-1")

            selected = _pending_visible_review_run(registry)

        self.assertEqual(selected.run_id, "run-1")

    def test_pending_visible_review_run_rejects_ambiguous_pending_cards(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            registry = Registry(root / "registry.sqlite")
            registry.create_run(self._visible_run(root, "run-1"))
            registry.create_run(self._visible_run(root, "run-2"))
            self._queue_visible_card(root, "run-1")
            self._queue_visible_card(root, "run-2")

            with self.assertRaises(ProtocolError) as raised:
                _pending_visible_review_run(registry)

        self.assertIn("multiple pending visible reviews", str(raised.exception))

    def test_review_next_without_run_id_uses_unique_pending_visible_card(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            registry = Registry(root / "registry.sqlite")
            registry.create_run(self._visible_run(root, "run-1"))
            self._queue_visible_card(root, "run-1")
            output = io.StringIO()

            with patch("loopweave.cli._registry", return_value=registry), patch(
                "sys.stdout", output
            ):
                code = main(["review-next"])

        self.assertEqual(code, 0)
        self.assertIn("Run: run-1", output.getvalue())
        self.assertIn(
            "loopweave review-submit --run-id run-1 --review-file",
            output.getvalue(),
        )

    def test_review_submit_without_run_id_uses_unique_pending_visible_card(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            registry = Registry(root / "registry.sqlite")
            registry.create_run(self._visible_run(root, "run-1"))
            self._queue_visible_card(root, "run-1")
            review_file = root / "verdict.md"
            review_file.write_text("placeholder\n", encoding="utf-8")
            coordinator = Mock()

            with patch("loopweave.cli._registry", return_value=registry), patch(
                "loopweave.cli._takeover_coordinator",
                return_value=coordinator,
            ), patch("loopweave.cli.submit_visible_review") as submit, patch(
                "loopweave.cli._resolve_review"
            ) as resolve:
                code = main(
                    [
                        "review-submit",
                        "--review-file",
                        str(review_file),
                    ]
                )

        self.assertEqual(code, 0)
        coordinator.reconcile_run.assert_called_once_with("run-1")
        submit.assert_called_once()
        self.assertEqual(submit.call_args.args[1], "run-1")
        resolve.assert_called_once()

    def test_review_heartbeat_reports_unique_pending_visible_review(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            registry = Registry(root / "registry.sqlite")
            registry.create_run(self._visible_run(root, "run-1"))
            self._queue_visible_card(root, "run-1")
            output = io.StringIO()

            with patch("loopweave.cli._registry", return_value=registry), patch(
                "sys.stdout", output
            ):
                code = main(["review-heartbeat", "--json"])

        self.assertEqual(code, 0)
        payload = json.loads(output.getvalue())
        self.assertEqual(payload["status"], "pending")
        self.assertEqual(payload["run_id"], "run-1")
        self.assertEqual(
            payload["command"], "loopweave review-next --run-id run-1"
        )
        self.assertEqual(
            payload["submit_command"],
            "loopweave review-submit --run-id run-1 "
            "--review-file <review-file>",
        )

    def test_review_heartbeat_ignores_legacy_dispatch_window(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            registry = Registry(root / "registry.sqlite")
            registry.create_run(self._visible_run(root, "run-1"))
            self._queue_visible_card(root, "run-1")
            legacy_heartbeat = root / "run-1" / "visible-review-heartbeat.json"
            legacy_heartbeat.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "run_id": "run-1",
                        "status": "armed",
                        "reviewer_thread_id": "thread-1",
                        "armed_at": "2026-07-02T00:00:00+00:00",
                        "expires_at": "2026-07-02T02:00:00+00:00",
                        "ttl_seconds": 7200,
                    }
                ),
                encoding="utf-8",
            )
            output = io.StringIO()

            with patch("loopweave.cli._registry", return_value=registry), patch(
                "sys.stdout", output
            ):
                code = main(["review-heartbeat", "--json"])

        self.assertEqual(code, 0)
        payload = json.loads(output.getvalue())
        self.assertEqual(payload["status"], "pending")
        self.assertEqual(payload["run_id"], "run-1")

    def test_review_heartbeat_reports_idle_without_pending_card(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            registry = Registry(root / "registry.sqlite")
            registry.create_run(self._visible_run(root, "run-1"))
            legacy_heartbeat = root / "run-1" / "visible-review-heartbeat.json"
            legacy_heartbeat.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "run_id": "run-1",
                        "status": "armed",
                        "reviewer_thread_id": "thread-1",
                        "armed_at": "2026-07-02T00:00:00+00:00",
                        "expires_at": "2999-01-01T00:00:00+00:00",
                        "ttl_seconds": 7200,
                    }
                ),
                encoding="utf-8",
            )
            output = io.StringIO()

            with patch("loopweave.cli._registry", return_value=registry), patch(
                "sys.stdout", output
            ):
                code = main(["review-heartbeat", "--json"])

        self.assertEqual(code, 0)
        payload = json.loads(output.getvalue())
        self.assertEqual(payload["status"], "idle")
        self.assertEqual(payload["reason"], "no pending visible review")

    def test_parser_rejects_assign_without_selector_or_with_both_selectors(self) -> None:
        parser = build_parser()

        with self.assertRaises(SystemExit):
            parser.parse_args(["assign", "--task-file", "/tmp/task.md"])

        with self.assertRaises(SystemExit):
            parser.parse_args(
                [
                    "assign",
                    "--run-id",
                    "run-1",
                    "--latest",
                    "--task-file",
                    "/tmp/task.md",
                ]
            )

    def test_main_assign_latest_delivers_task_and_warns_on_duplicate(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            registry = Registry(root / "registry.sqlite")
            run_dir = root / "run"
            run_dir.mkdir()
            socket_path = root / "control.sock"
            socket_path.write_text("", encoding="utf-8")
            task = root / "task.md"
            task.write_text("# Task\n", encoding="utf-8")
            run = RunRecord(
                run_id="run-1",
                codex_thread_id="thread-1",
                cwd=str(root),
                thread_cwd=str(root),
                workspace_root=str(root),
                tty="/dev/ttys001",
                agent="claude",
                agent_pid=1234,
                agent_process_start="start",
                control_token="secret",
                state=RunState.RUNNING,
                socket_path=str(socket_path),
                run_dir=str(run_dir),
            )
            registry.create_run(run)
            sent = []

            with patch("loopweave.cli._registry", return_value=registry), patch(
                "loopweave.terminal_host.default_process_identity_reader",
                return_value=lambda pid: "start",
            ), patch(
                "loopweave.terminal_host.default_control_sender",
                return_value=lambda path, payload, timeout=3.0: sent.append((path, payload)) or {"status": "ok"},
            ), patch(
                "sys.argv",
                ["loopweave", "assign", "--latest", "--task-file", str(task)],
            ):
                stdout = io.StringIO()
                with contextlib.redirect_stdout(stdout):
                    code = main()

            self.assertEqual(code, 0)
            self.assertIn("assigned task", stdout.getvalue())
            self.assertEqual(len(sent), 2)
            self.assertIn("[LoopWeave assignment]", sent[0][1]["text"])
            self.assertEqual(sent[1][1]["text"], "\r")

    def test_stop_returns_error_when_agent_process_survives(self) -> None:
        run = _status_run(state=RunState.RUNNING)
        registry = Mock()
        registry.get_run.return_value = run
        stderr = io.StringIO()

        with patch("loopweave.cli._registry", return_value=registry), patch(
            "loopweave.terminal_host.default_control_sender",
            return_value=lambda path, payload, timeout=3.0: {"status": "ok"},
        ), patch(
            "loopweave.terminal_host.default_process_identity_reader",
            return_value=lambda pid: run.agent_process_start,
        ), patch(
            "loopweave.cli.STOP_VERIFY_TIMEOUT_SECONDS",
            0,
            create=True,
        ), contextlib.redirect_stderr(stderr):
            code = main(["stop", "run-1"])

        self.assertEqual(code, 2)
        self.assertIn("still running", stderr.getvalue())

    def test_stop_marks_run_stopped_after_process_exits(self) -> None:
        run = _status_run(state=RunState.RUNNING)
        registry = Mock()
        registry.get_run.return_value = run

        def _raise_gone(pid):
            raise RuntimeError("gone")

        with patch("loopweave.cli._registry", return_value=registry), patch(
            "loopweave.terminal_host.default_control_sender",
            return_value=lambda path, payload, timeout=3.0: {"status": "ok"},
        ), patch(
            "loopweave.terminal_host.default_process_identity_reader",
            return_value=_raise_gone,
        ), patch(
            "loopweave.cli.STOP_VERIFY_TIMEOUT_SECONDS",
            0,
            create=True,
        ):
            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                code = main(["stop", "run-1"])

        self.assertEqual(code, 0)
        self.assertEqual(stdout.getvalue(), "ok\n")
        registry.force_state.assert_called_once_with("run-1", RunState.STOPPED)

    def test_assign_latest_marks_stale_run_orphaned_for_subsequent_selection(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            registry = Registry(root / "registry.sqlite")
            run_dir = root / "run"
            run_dir.mkdir()
            socket_path = root / "control.sock"
            socket_path.write_text("", encoding="utf-8")
            task = root / "task.md"
            task.write_text("# Task\n", encoding="utf-8")
            run = RunRecord(
                run_id="run-stale",
                codex_thread_id="thread-1",
                cwd=str(root),
                thread_cwd=str(root),
                workspace_root=str(root),
                tty="/dev/ttys001",
                agent="claude",
                agent_pid=1234,
                agent_process_start="original-start",
                control_token="secret",
                state=RunState.RUNNING,
                socket_path=str(socket_path),
                run_dir=str(run_dir),
            )
            registry.create_run(run)

            with patch("loopweave.cli._registry", return_value=registry), patch(
                "loopweave.terminal_host.default_process_identity_reader",
                return_value=lambda pid: "different-start",
            ), patch(
                "sys.argv",
                ["loopweave", "assign", "--latest", "--task-file", str(task)],
            ):
                stderr = io.StringIO()
                with contextlib.redirect_stderr(stderr):
                    code = main()

            self.assertEqual(code, 2)
            self.assertEqual(registry.get_run("run-stale").state, RunState.ORPHANED)
            assignable = [
                candidate
                for candidate in registry.list_runs()
                if candidate.state in (RunState.RUNNING, RunState.WORKER_CONTINUING)
            ]
            self.assertEqual(assignable, [])

    def test_assign_latest_prunes_stale_candidate_and_selects_live_run(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            registry = Registry(root / "registry.sqlite")

            def make_run_row(run_id: str, pid: int, start: str) -> RunRecord:
                run_dir = root / run_id
                run_dir.mkdir()
                socket_path = root / (run_id + ".sock")
                socket_path.write_text("", encoding="utf-8")
                return RunRecord(
                    run_id=run_id,
                    codex_thread_id="thread-" + run_id,
                    cwd=str(root),
                    thread_cwd=str(root),
                    workspace_root=str(root),
                    tty="/dev/ttys001",
                    agent="claude",
                    agent_pid=pid,
                    agent_process_start=start,
                    control_token="secret",
                    state=RunState.RUNNING,
                    socket_path=str(socket_path),
                    run_dir=str(run_dir),
                )

            registry.create_run(make_run_row("run-stale", 1234, "stale-start"))
            registry.create_run(make_run_row("run-live", 5678, "live-start"))
            task = root / "task.md"
            task.write_text("# Task\n", encoding="utf-8")
            sent = []

            def fake_start(pid: int) -> str:
                return {1234: "different", 5678: "live-start"}.get(pid, "unknown")

            with patch("loopweave.cli._registry", return_value=registry), patch(
                "loopweave.terminal_host.default_process_identity_reader",
                return_value=fake_start,
            ), patch(
                "loopweave.terminal_host.default_control_sender",
                return_value=lambda path, payload, timeout=3.0: sent.append((path, payload)) or {"status": "ok"},
            ), patch(
                "sys.argv",
                ["loopweave", "assign", "--latest", "--task-file", str(task)],
            ):
                stdout = io.StringIO()
                with contextlib.redirect_stdout(stdout):
                    code = main()

            self.assertEqual(code, 0)
            self.assertEqual(registry.get_run("run-stale").state, RunState.ORPHANED)
            self.assertEqual(registry.get_run("run-live").state, RunState.RUNNING)
            self.assertEqual(len(sent), 2)
            self.assertIn("run-live", str(sent[0][0]))

    def _visible_run(self, root: Path, run_id: str) -> RunRecord:
        run_dir = root / run_id
        run_dir.mkdir(parents=True, exist_ok=True)
        return RunRecord(
            run_id=run_id,
            codex_thread_id="thread-1",
            cwd=str(root),
            thread_cwd=str(root),
            workspace_root=str(root),
            tty="/dev/test",
            agent="claude",
            agent_pid=123,
            agent_process_start="start",
            control_token="secret",
            state=RunState.READY_FOR_REVIEW,
            run_dir=str(run_dir),
            reviewer_backend=ReviewBackend.VISIBLE_THREAD,
        )

    def _queue_visible_card(self, root: Path, run_id: str) -> None:
        task = root / "assigned-task-latest.md"
        task.write_text("# Task\n", encoding="utf-8")
        plan = root / "project.json"
        plan.write_text("{}\n", encoding="utf-8")
        changed = root / "{}.txt".format(run_id)
        changed.write_text("ok\n", encoding="utf-8")
        card = create_review_card(
            run_id=run_id,
            project_slug="demo",
            stage_id="stage-1",
            stage_title="Foundation",
            completion_scope="stage",
            workspace_root=root,
            task_packet_path=task,
            plan_path=plan,
            work_summary="Implemented stage one.",
            completed_items=["Created {}".format(changed.name)],
            not_completed_items=[],
            changed_files=[changed],
            artifact_paths=[],
            test_commands=["python3 -m unittest"],
            test_result_summary="Passed.",
            worker_claims=[],
            known_issues=[],
            questions_for_reviewer=[],
        )
        queue_visible_review_card(root / run_id, card)


REQUIRED_STATUS_JSON_KEYS = {
    "run_id",
    "agent",
    "state",
    "agent_pid",
    "project_slug",
    "project_root",
    "workspace_root",
    "thread_cwd",
    "codex_thread_id",
    "pending_codex_thread_id",
    "binding_generation",
    "review_loop",
    "mode",
    "reviewer_backend",
    "reviewer_thread_id",
    "reviewer_thread_cwd",
    "reviewer_generation",
    "task_assignment",
}


def _status_run(**overrides):
    fields = dict(
        run_id="run-1",
        codex_thread_id="thread-1",
        cwd="/tmp/project",
        thread_cwd="/tmp/control",
        workspace_root="/tmp/workspace",
        project_slug="demo",
        project_root="/tmp/projects/demo",
        tty="/dev/test",
        agent="claude",
        agent_pid=123,
        agent_process_start="start",
        control_token="do-not-leak-this-token",
        state=RunState.RUNNING,
        pending_codex_thread_id=None,
        binding_generation=2,
        review_loop=3,
        mode=RunMode.DESIGN,
        run_dir=str(Path(tempfile.mkdtemp(prefix="loopweave-status-"))),
    )
    fields.update(overrides)
    return RunRecord(**fields)


class StatusJsonTests(unittest.TestCase):
    def test_render_status_json_emits_only_required_fields_and_hides_secret(
        self,
    ) -> None:
        run = _status_run()

        rendered = render_status_json(run)
        payload = json.loads(rendered)

        self.assertEqual(payload["run_id"], "run-1")
        self.assertEqual(payload["agent"], "claude")
        self.assertEqual(payload["state"], "running")
        self.assertEqual(payload["agent_pid"], 123)
        self.assertEqual(payload["thread_cwd"], "/tmp/control")
        self.assertEqual(payload["workspace_root"], "/tmp/workspace")
        self.assertEqual(payload["project_slug"], "demo")
        self.assertEqual(payload["codex_thread_id"], "thread-1")
        self.assertIsNone(payload["pending_codex_thread_id"])
        self.assertEqual(payload["binding_generation"], 2)
        self.assertEqual(payload["review_loop"], 3)
        self.assertEqual(payload["mode"], "design")
        self.assertEqual(payload["reviewer_backend"], "ephemeral")
        self.assertIsNone(payload["reviewer_thread_id"])
        self.assertIsNone(payload["reviewer_thread_cwd"])
        self.assertEqual(payload["reviewer_generation"], 1)
        self.assertEqual(set(payload), REQUIRED_STATUS_JSON_KEYS)
        self.assertNotIn("control_token", payload)
        self.assertNotIn("do-not-leak-this-token", rendered)

    def test_render_status_json_emits_present_pending_thread(self) -> None:
        run = _status_run(pending_codex_thread_id="thread-pending")

        payload = json.loads(render_status_json(run))

        self.assertEqual(payload["pending_codex_thread_id"], "thread-pending")
        self.assertEqual(set(payload), REQUIRED_STATUS_JSON_KEYS)
        self.assertNotIn("do-not-leak-this-token", render_status_json(run))

    def test_status_json_explicit_run_id_emits_single_json_object(self) -> None:
        run = _status_run()
        registry = Mock()
        registry.get_run.return_value = run
        coordinator = Mock()
        output = io.StringIO()

        with patch("loopweave.cli._registry", return_value=registry), patch(
            "loopweave.cli._takeover_coordinator", return_value=coordinator
        ), patch("sys.stdout", output):
            exit_code = main(["status", "run-1", "--json"])

        self.assertEqual(exit_code, 0)
        coordinator.reconcile_run.assert_called_once_with("run-1")
        payload = json.loads(output.getvalue())
        self.assertEqual(payload["run_id"], "run-1")
        self.assertEqual(set(payload), REQUIRED_STATUS_JSON_KEYS)
        self.assertNotIn("do-not-leak-this-token", output.getvalue())

    def test_status_json_without_run_id_uses_active_run(self) -> None:
        run = _status_run()
        registry = Mock()
        coordinator = Mock()
        output = io.StringIO()

        with patch("loopweave.cli._registry", return_value=registry), patch(
            "loopweave.cli._takeover_coordinator", return_value=coordinator
        ), patch("loopweave.cli._latest_active_run") as latest_active, patch(
            "loopweave.terminal_host.default_process_identity_reader",
            return_value=lambda pid: "start",
        ), patch(
            "sys.stdout", output
        ):
            latest_active.return_value = run
            registry.list_runs.return_value = [run]
            registry.get_run.return_value = run
            exit_code = main(["status", "--json"])

        self.assertEqual(exit_code, 0)
        latest_active.assert_called_once()
        payload = json.loads(output.getvalue())
        self.assertEqual(payload["run_id"], "run-1")
        self.assertEqual(set(payload), REQUIRED_STATUS_JSON_KEYS)


class RunsJsonTests(unittest.TestCase):
    def test_render_runs_json_empty_is_empty_array(self) -> None:
        rendered = render_runs_json([])

        self.assertEqual(rendered, "[]")
        self.assertEqual(json.loads(rendered), [])

    def test_render_runs_json_serializes_multiple_runs_in_order(self) -> None:
        first = _status_run(run_id="run-a", pending_codex_thread_id=None)
        second = _status_run(
            run_id="run-b",
            agent="generic",
            agent_pid=999,
            pending_codex_thread_id="thread-pending",
            binding_generation=4,
            review_loop=1,
        )

        payload = json.loads(render_runs_json([first, second]))

        self.assertIsInstance(payload, list)
        self.assertEqual(len(payload), 2)
        self.assertEqual([item["run_id"] for item in payload], ["run-a", "run-b"])
        for item in payload:
            self.assertEqual(set(item), REQUIRED_STATUS_JSON_KEYS)
        self.assertIsNone(payload[0]["pending_codex_thread_id"])
        self.assertEqual(payload[1]["pending_codex_thread_id"], "thread-pending")
        self.assertEqual(payload[1]["agent"], "generic")
        self.assertEqual(payload[1]["binding_generation"], 4)
        self.assertEqual(payload[1]["review_loop"], 1)

    def test_render_runs_json_hides_secret_fields_and_values(self) -> None:
        protected_sentinels = {
            "control_token": "sentinel-control-token",
            "tty": "sentinel-tty",
            "agent_process_start": "sentinel-agent-process-start",
            "socket_path": "sentinel-socket-path",
            "run_dir": "sentinel-run-dir",
        }
        run = _status_run(**protected_sentinels)

        rendered = render_runs_json([run])

        for sentinel in protected_sentinels.values():
            self.assertNotIn(sentinel, rendered)
        payload = json.loads(rendered)
        self.assertEqual(len(payload), 1)
        for forbidden in protected_sentinels:
            self.assertNotIn(forbidden, payload[0])


class RunsCliTests(unittest.TestCase):
    def test_parser_runs_accepts_json_flag(self) -> None:
        parser = build_parser()

        plain = parser.parse_args(["runs"])
        flagged = parser.parse_args(["runs", "--json"])

        self.assertEqual(plain.command, "runs")
        self.assertFalse(plain.json)
        self.assertTrue(flagged.json)

    def test_runs_without_json_keeps_tabular_text_output(self) -> None:
        registry = Mock()
        registry.list_runs.return_value = [_status_run()]
        output = io.StringIO()

        with patch("loopweave.cli._registry", return_value=registry), patch(
            "loopweave.terminal_host.default_process_identity_reader",
            return_value=lambda pid: "start",
        ), patch(
            "sys.stdout", output
        ):
            exit_code = main(["runs"])

        self.assertEqual(exit_code, 0)
        expected = (
            "RUN ID\tAGENT\tSTATE\tPID\tGEN\tTHREAD\tPENDING\n"
            "run-1\tclaude\trunning\t123\t2\tthread-1\t-\n"
        )
        self.assertEqual(output.getvalue(), expected)

    def test_runs_json_emits_array_with_multiple_records(self) -> None:
        first = _status_run(run_id="run-a")
        second = _status_run(
            run_id="run-b", pending_codex_thread_id="thread-pending"
        )
        registry = Mock()
        registry.list_runs.return_value = [first, second]
        output = io.StringIO()

        with patch("loopweave.cli._registry", return_value=registry), patch(
            "loopweave.terminal_host.default_process_identity_reader",
            return_value=lambda pid: "start",
        ), patch(
            "sys.stdout", output
        ):
            exit_code = main(["runs", "--json"])

        self.assertEqual(exit_code, 0)
        payload = json.loads(output.getvalue())
        self.assertIsInstance(payload, list)
        self.assertEqual(len(payload), 2)
        self.assertEqual([item["run_id"] for item in payload], ["run-a", "run-b"])
        for item in payload:
            self.assertEqual(set(item), REQUIRED_STATUS_JSON_KEYS)
        self.assertIsNone(payload[0]["pending_codex_thread_id"])
        self.assertEqual(payload[1]["pending_codex_thread_id"], "thread-pending")

    def test_runs_json_empty_registry_emits_empty_array(self) -> None:
        registry = Mock()
        registry.list_runs.return_value = []
        output = io.StringIO()

        with patch("loopweave.cli._registry", return_value=registry), patch(
            "sys.stdout", output
        ):
            exit_code = main(["runs", "--json"])

        self.assertEqual(exit_code, 0)
        self.assertEqual(output.getvalue(), "[]\n")
        self.assertEqual(json.loads(output.getvalue()), [])


if __name__ == "__main__":
    unittest.main()
