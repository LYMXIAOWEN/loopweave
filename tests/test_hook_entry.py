from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import loopweave.terminal_host as _terminal_host_module
from loopweave.hook_entry import extract_transcript_evidence, handle_claude_stop
from loopweave.models import ReviewBackend, RunMode, RunRecord, RunState
from loopweave.registry import Registry
from loopweave.visible_review import MAX_REVIEW_CARD_BYTES


class ClaudeStopHookTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.run_dir = self.root / "run-1"
        self.run_dir.mkdir()
        self.registry = Registry(self.root / "registry.sqlite")
        self.registry.create_run(
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
                run_dir=str(self.run_dir),
            )
        )
        self._identity_patcher = patch.object(
            _terminal_host_module,
            "default_process_identity_reader",
            return_value=lambda pid: "start",
        )
        self._identity_patcher.start()

    def tearDown(self) -> None:
        self._identity_patcher.stop()
        self.temp_dir.cleanup()

    def test_stop_hook_writes_request_and_dispatches_once(self) -> None:
        dispatched = []
        transcript_path = self._write_transcript(
            [
                {
                    "message": {
                        "content": [
                            {
                                "type": "tool_use",
                                "name": "Bash",
                                "input": {"command": "python3 -m unittest"},
                            },
                            {
                                "type": "tool_use",
                                "name": "Edit",
                                "input": {"file_path": "/tmp/project/app.py"},
                            },
                        ]
                    }
                }
            ]
        )
        payload = {
            "session_id": "claude-session",
            "transcript_path": str(transcript_path),
            "last_assistant_message": "Implemented and tested the feature.",
        }

        first = handle_claude_stop(
            "run-1", payload, self.registry, lambda run: dispatched.append(run.run_id)
        )
        second = handle_claude_stop(
            "run-1", payload, self.registry, lambda run: dispatched.append(run.run_id)
        )

        self.assertEqual(first, "dispatched")
        self.assertEqual(second, "duplicate")
        self.assertEqual(dispatched, ["run-1"])
        request = json.loads(
            (self.run_dir / "review-request.json").read_text(encoding="utf-8")
        )
        self.assertEqual(request["status"], "ready_for_review")
        self.assertEqual(
            request["change_summary"], "Implemented and tested the feature."
        )
        self.assertEqual(request["commands_run"], ["python3 -m unittest"])
        self.assertEqual(request["files_changed"], ["/tmp/project/app.py"])
        self.assertEqual(request["workspace_root"], "/tmp/project")
        self.assertEqual(request["thread_cwd"], "/tmp/project")
        self.assertEqual(request["mode"], "develop")
        self.assertEqual(request["review_round"], 1)
        self.assertRegex(request["evidence_fingerprint"], r"^[0-9a-f]{64}$")
        self.assertEqual(
            self.registry.get_run("run-1").state, RunState.READY_FOR_REVIEW
        )

    def test_visible_backend_queues_card_without_dispatch(self) -> None:
        with self.registry._connect() as connection:
            connection.execute(
                """
                UPDATE runs
                SET reviewer_backend = ?, reviewer_thread_id = ?,
                    reviewer_thread_cwd = ?, project_slug = ?,
                    project_root = ?, workspace_root = ?
                WHERE run_id = ?
                """,
                (
                    ReviewBackend.VISIBLE_THREAD.value,
                    "thread-1",
                    str(self.root),
                    "demo",
                    str(self.root),
                    str(self.root),
                    "run-1",
                ),
            )
        task = self.run_dir / "assigned-task-latest.md"
        task.write_text("# Task\n", encoding="utf-8")
        project_json = self.root / "project.json"
        project_json.write_text('{"schema_version": 1}\n', encoding="utf-8")
        changed_file = self.root / "app.py"
        changed_file.write_text("ok = True\n", encoding="utf-8")
        transcript_path = self._write_transcript(
            [
                {
                    "message": {
                        "content": [
                            {
                                "type": "tool_use",
                                "name": "Edit",
                                "input": {"file_path": str(changed_file)},
                            }
                        ]
                    }
                }
            ]
        )
        dispatched = []
        with patch(
            "loopweave.hook_entry.request_visible_review_wakeup",
            create=True,
        ) as wakeup:
            result = handle_claude_stop(
                "run-1",
                {
                    "last_assistant_message": (
                        "LOOPWEAVE_STAGE\n"
                        "Stage stage-1: Foundation\n"
                        "Implemented foundation files."
                    ),
                    "transcript_path": str(transcript_path),
                },
                self.registry,
                lambda run: dispatched.append(run.run_id),
            )

        self.assertEqual(result, "visible-review-pending")
        self.assertEqual(dispatched, [])
        self.assertTrue((self.run_dir / "review-inbox" / "pending").exists())
        wakeup.assert_not_called()
        pending_review_id = (
            self.run_dir / "review-inbox" / "pending"
        ).read_text(encoding="utf-8").strip()
        self.assertTrue(pending_review_id.startswith("review-request-"))
        self.assertFalse((self.run_dir / "visible-review-heartbeat.json").exists())
        self.assertFalse(
            (self.run_dir / "visible-review-wakeup-retry.json").exists()
        )
        events = (self.run_dir / "events.jsonl").read_text(encoding="utf-8")
        self.assertNotIn("visible_review_heartbeat_armed", events)

    def test_visible_backend_dispatches_explicit_stage_once_after_queue(self) -> None:
        with self.registry._connect() as connection:
            connection.execute(
                """
                UPDATE runs
                SET reviewer_backend = ?, reviewer_thread_id = ?,
                    reviewer_thread_cwd = ?, project_slug = ?,
                    project_root = ?, workspace_root = ?
                WHERE run_id = ?
                """,
                (
                    ReviewBackend.VISIBLE_THREAD.value,
                    "thread-1",
                    str(self.root),
                    "demo",
                    str(self.root),
                    str(self.root),
                    "run-1",
                ),
            )
        task = self.run_dir / "assigned-task-latest.md"
        task.write_text("# Task\n", encoding="utf-8")
        project_json = self.root / "project.json"
        project_json.write_text('{"schema_version": 1}\n', encoding="utf-8")
        transcript_path = self._write_transcript(
            [{"message": {"content": [{"type": "text", "text": "Done"}]}}]
        )
        wakeups = []

        payload = {
            "last_assistant_message": (
                "LOOPWEAVE_STAGE\n"
                "Stage 1: Entry Front Door\n"
                "Completed and ready for LoopWeave review."
            ),
            "transcript_path": str(transcript_path),
        }
        result = handle_claude_stop(
            "run-1",
            payload,
            self.registry,
            lambda run: None,
            visible_waker=lambda run, card: wakeups.append(
                (run.run_id, card["completion_scope"], card["work_summary"])
            )
            or True,
        )
        duplicate = handle_claude_stop(
            "run-1",
            payload,
            self.registry,
            lambda run: None,
            visible_waker=lambda run, card: wakeups.append(
                (run.run_id, card["completion_scope"], card["work_summary"])
            )
            or True,
        )

        self.assertEqual(result, "visible-review-pending")
        self.assertEqual(duplicate, "duplicate")
        self.assertEqual(
            wakeups,
            [
                (
                    "run-1",
                    "stage",
                    "Completed and ready for LoopWeave review.",
                )
            ],
        )
        self.assertTrue((self.run_dir / "review-inbox" / "pending").exists())
        events = (self.run_dir / "events.jsonl").read_text(encoding="utf-8")
        self.assertIn("visible_review_card_queued", events)
        self.assertNotIn("review_skipped_no_evidence", events)

    def test_visible_backend_uses_transcript_stage_marker_when_hook_message_missing(self) -> None:
        with self.registry._connect() as connection:
            connection.execute(
                """
                UPDATE runs
                SET reviewer_backend = ?, reviewer_thread_id = ?,
                    reviewer_thread_cwd = ?, project_slug = ?,
                    project_root = ?, workspace_root = ?
                WHERE run_id = ?
                """,
                (
                    ReviewBackend.VISIBLE_THREAD.value,
                    "thread-1",
                    str(self.root),
                    "demo",
                    str(self.root),
                    str(self.root),
                    "run-1",
                ),
            )
        task = self.run_dir / "assigned-task-latest.md"
        task.write_text("# Task\n", encoding="utf-8")
        project_json = self.root / "project.json"
        project_json.write_text('{"schema_version": 1}\n', encoding="utf-8")
        transcript_path = self._write_transcript(
            [
                {
                    "type": "assistant",
                    "message": {
                        "content": [
                            {
                                "type": "text",
                                "text": (
                                    "LOOPWEAVE_STAGE\n"
                                    "Stage 2: Sessions\n"
                                    "Completed and ready for LoopWeave review."
                                ),
                            }
                        ]
                    },
                }
            ]
        )
        wakeups = []

        result = handle_claude_stop(
            "run-1",
            {
                "last_assistant_message": "",
                "transcript_path": str(transcript_path),
            },
            self.registry,
            lambda run: None,
            visible_waker=lambda run, card: wakeups.append(
                (run.run_id, card["stage_id"], card["work_summary"])
            )
            or True,
        )

        self.assertEqual(result, "visible-review-pending")
        self.assertEqual(
            wakeups,
            [("run-1", "2", "Completed and ready for LoopWeave review.")],
        )
        events = (self.run_dir / "events.jsonl").read_text(encoding="utf-8")
        self.assertIn("visible_review_card_queued", events)
        self.assertNotIn("review_skipped_no_evidence", events)

    def test_transcript_stage_marker_before_current_human_turn_is_ignored(self) -> None:
        transcript_path = self._write_transcript(
            [
                {
                    "type": "assistant",
                    "message": {
                        "content": [
                            {
                                "type": "text",
                                "text": "LOOPWEAVE_STAGE\nOld stage card.",
                            }
                        ]
                    },
                },
                {
                    "type": "user",
                    "message": {"content": "Just answer this question."},
                },
                {
                    "type": "assistant",
                    "message": {
                        "content": [
                            {"type": "text", "text": "Plain answer, no review."}
                        ]
                    },
                },
            ],
            name="old-marker-transcript.jsonl",
        )
        dispatched = []

        result = handle_claude_stop(
            "run-1",
            {
                "last_assistant_message": "",
                "transcript_path": str(transcript_path),
            },
            self.registry,
            lambda run: dispatched.append(run.run_id),
        )

        self.assertEqual(result, "no-review")
        self.assertEqual(dispatched, [])
        self.assertFalse((self.run_dir / "review-request.json").exists())

    def test_visible_backend_bounds_oversized_stage_summary(self) -> None:
        with self.registry._connect() as connection:
            connection.execute(
                """
                UPDATE runs
                SET reviewer_backend = ?, reviewer_thread_id = ?,
                    reviewer_thread_cwd = ?, project_slug = ?,
                    project_root = ?, workspace_root = ?
                WHERE run_id = ?
                """,
                (
                    ReviewBackend.VISIBLE_THREAD.value,
                    "thread-1",
                    str(self.root),
                    "demo",
                    str(self.root),
                    str(self.root),
                    "run-1",
                ),
            )
        task = self.run_dir / "assigned-task-latest.md"
        task.write_text("# Task\n", encoding="utf-8")
        project_json = self.root / "project.json"
        project_json.write_text('{"schema_version": 1}\n', encoding="utf-8")
        transcript_path = self._write_transcript(
            [{"message": {"content": [{"type": "text", "text": "Done"}]}}]
        )
        huge_summary = "LOOPWEAVE_STAGE\nStage 1: Entry Front Door\n" + ("x" * 20000)

        result = handle_claude_stop(
            "run-1",
            {
                "last_assistant_message": huge_summary,
                "transcript_path": str(transcript_path),
            },
            self.registry,
            lambda run: None,
            visible_waker=lambda run, card: True,
        )

        self.assertEqual(result, "visible-review-pending")
        pending = self.run_dir / "review-inbox" / "pending"
        self.assertTrue(pending.exists())
        review_id = pending.read_text(encoding="utf-8").strip()
        card = json.loads(
            (self.run_dir / "review-inbox" / f"{review_id}.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertLessEqual(
            len(json.dumps(card, ensure_ascii=False).encode("utf-8")),
            MAX_REVIEW_CARD_BYTES,
        )
        self.assertIn("truncated", card["work_summary"])

    def test_visible_dispatch_failure_keeps_durable_pending_card(self) -> None:
        with self.registry._connect() as connection:
            connection.execute(
                """
                UPDATE runs
                SET reviewer_backend = ?, reviewer_thread_id = ?,
                    reviewer_thread_cwd = ?, project_slug = ?,
                    project_root = ?, workspace_root = ?
                WHERE run_id = ?
                """,
                (
                    ReviewBackend.VISIBLE_THREAD.value,
                    "thread-1",
                    str(self.root),
                    "demo",
                    str(self.root),
                    str(self.root),
                    "run-1",
                ),
            )
        (self.run_dir / "assigned-task-latest.md").write_text(
            "# Task\n", encoding="utf-8"
        )
        (self.root / "project.json").write_text(
            '{"schema_version": 1}\n', encoding="utf-8"
        )
        transcript_path = self._write_transcript(
            [{"message": {"content": [{"type": "text", "text": "Done"}]}}]
        )

        result = handle_claude_stop(
            "run-1",
            {
                "last_assistant_message": "LOOPWEAVE_STAGE\nStage complete.",
                "transcript_path": str(transcript_path),
            },
            self.registry,
            lambda run: None,
            visible_waker=lambda run, card: (_ for _ in ()).throw(
                RuntimeError("bridge unavailable")
            ),
        )

        self.assertEqual(result, "visible-review-pending")
        self.assertTrue((self.run_dir / "review-inbox" / "pending").exists())
        self.assertTrue((self.run_dir / "last-stop.sha256").exists())
        self.assertEqual(self.registry.get_run("run-1").review_loop, 1)
        events = (self.run_dir / "events.jsonl").read_text(encoding="utf-8")
        self.assertIn("visible_review_dispatch_failed", events)
        self.assertIn("queued_after_dispatch_failure", events)

    def test_visible_backend_compacts_large_evidence_without_worker_rewrite(self) -> None:
        with self.registry._connect() as connection:
            connection.execute(
                """
                UPDATE runs
                SET reviewer_backend = ?, reviewer_thread_id = ?,
                    reviewer_thread_cwd = ?, project_slug = ?,
                    project_root = ?, workspace_root = ?
                WHERE run_id = ?
                """,
                (
                    ReviewBackend.VISIBLE_THREAD.value,
                    "thread-1",
                    str(self.root),
                    "demo",
                    str(self.root),
                    str(self.root),
                    "run-1",
                ),
            )
        (self.run_dir / "assigned-task-latest.md").write_text(
            "# Task\n", encoding="utf-8"
        )
        (self.root / "project.json").write_text(
            '{"schema_version": 1}\n', encoding="utf-8"
        )
        blocks = []
        for index in range(400):
            changed = self.root / ("很长的文件名-{:04d}-{}.py".format(index, "界" * 12))
            changed.write_text("ok = True\n", encoding="utf-8")
            blocks.extend(
                [
                    {
                        "type": "tool_use",
                        "name": "Edit",
                        "input": {"file_path": str(changed)},
                    },
                    {
                        "type": "tool_use",
                        "name": "Bash",
                        "input": {
                            "command": "python -m pytest tests/test_{:04d}_{}.py -q".format(
                                index, "证据" * 10
                            )
                        },
                    },
                ]
            )
        transcript_path = self._write_transcript(
            [{"message": {"content": blocks}}]
        )

        result = handle_claude_stop(
            "run-1",
            {
                "last_assistant_message": (
                    "LOOPWEAVE_STAGE\nStage long: Long evidence\n" + "完成说明。" * 1000
                ),
                "transcript_path": str(transcript_path),
            },
            self.registry,
            lambda run: None,
            visible_waker=lambda run, card: True,
        )

        self.assertEqual(result, "visible-review-pending")
        review_id = (self.run_dir / "review-inbox" / "pending").read_text().strip()
        card = json.loads(
            (self.run_dir / "review-inbox" / f"{review_id}.json").read_text()
        )
        encoded = json.dumps(card, ensure_ascii=False).encode("utf-8")
        self.assertLessEqual(len(encoded), MAX_REVIEW_CARD_BYTES)
        self.assertIn("truncated", card["test_result_summary"])

    def test_design_request_contains_mode_and_round(self) -> None:
        design_run = self.registry.get_run("run-1")
        with self.registry._connect() as connection:
            connection.execute(
                "UPDATE runs SET mode = ? WHERE run_id = ?",
                (RunMode.DESIGN.value, design_run.run_id),
            )
        transcript_path = self._write_transcript(
            [
                {
                    "message": {
                        "content": [
                            {
                                "type": "tool_use",
                                "name": "Write",
                                "input": {"file_path": "/tmp/design.md"},
                            }
                        ]
                    }
                }
            ]
        )

        result = handle_claude_stop(
            "run-1",
            {
                "last_assistant_message": (
                    "LOOPWEAVE_STAGE\nDrafted the proposal."
                ),
                "transcript_path": str(transcript_path),
            },
            self.registry,
            lambda run: None,
        )

        request = json.loads(
            (self.run_dir / "review-request.json").read_text(encoding="utf-8")
        )
        self.assertEqual(result, "dispatched")
        self.assertEqual(request["mode"], "design")
        self.assertEqual(request["review_round"], 1)
        self.assertEqual(request["completion_scope"], "final")

    def test_evidence_fingerprint_changes_when_file_content_changes(self) -> None:
        changed_file = self.root / "app.py"
        changed_file.write_text("version = 1\n", encoding="utf-8")
        transcript_path = self._write_transcript(
            [
                {
                    "message": {
                        "content": [
                            {
                                "type": "tool_use",
                                "name": "Edit",
                                "input": {"file_path": str(changed_file)},
                            }
                        ]
                    }
                }
            ]
        )

        handle_claude_stop(
            "run-1",
            {
                "last_assistant_message": "First revision.",
                "transcript_path": str(transcript_path),
            },
            self.registry,
            lambda run: None,
        )
        first = json.loads(
            (self.run_dir / "review-request.json").read_text(encoding="utf-8")
        )["evidence_fingerprint"]

        self.registry.force_state("run-1", RunState.WORKER_CONTINUING)
        changed_file.write_text("version = 2\n", encoding="utf-8")
        handle_claude_stop(
            "run-1",
            {
                "last_assistant_message": "Second revision.",
                "transcript_path": str(transcript_path),
            },
            self.registry,
            lambda run: None,
        )
        second = json.loads(
            (self.run_dir / "review-request.json").read_text(encoding="utf-8")
        )["evidence_fingerprint"]

        self.assertNotEqual(first, second)

    def test_same_message_with_changed_evidence_dispatches_next_stage(self) -> None:
        changed_file = self.root / "stage.py"
        changed_file.write_text("stage = 1\n", encoding="utf-8")
        transcript_path = self._write_transcript(
            [
                {
                    "message": {
                        "content": [
                            {
                                "type": "tool_use",
                                "name": "Edit",
                                "input": {"file_path": str(changed_file)},
                            }
                        ]
                    }
                }
            ]
        )
        payload = {
            "last_assistant_message": (
                "LOOPWEAVE_STAGE\nTask complete and ready for review."
            ),
            "transcript_path": str(transcript_path),
        }
        dispatched = []

        first = handle_claude_stop(
            "run-1",
            payload,
            self.registry,
            lambda run: dispatched.append(run.review_loop),
        )
        self.registry.force_state("run-1", RunState.WORKER_CONTINUING)
        changed_file.write_text("stage = 2\n", encoding="utf-8")
        second = handle_claude_stop(
            "run-1",
            payload,
            self.registry,
            lambda run: dispatched.append(run.review_loop),
        )

        self.assertEqual(first, "dispatched")
        self.assertEqual(second, "dispatched")
        self.assertEqual(dispatched, [0, 1])
        self.assertEqual(self.registry.get_run("run-1").review_loop, 2)

    def test_unmarked_revision_inherits_stage_scope_after_changes_requested(
        self,
    ) -> None:
        self.registry.force_state("run-1", RunState.WORKER_CONTINUING)
        (self.run_dir / "review-request.json").write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "run_id": "run-1",
                    "status": "ready_for_review",
                    "task_summary": "Review Task 3",
                    "change_summary": "Task 3 complete.",
                    "files_changed": ["/tmp/project/store.py"],
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
        (self.run_dir / "reviewer-verdict.json").write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "run_id": "run-1",
                    "review_id": "review-1",
                    "verdict": "changes_requested",
                    "summary": "Fix the rollup.",
                    "review_file": "reviewer-verdict.md",
                    "continue": True,
                }
            ),
            encoding="utf-8",
        )
        changed_file = self.root / "store.py"
        changed_file.write_text("fixed = True\n", encoding="utf-8")
        transcript_path = self._write_transcript(
            [
                {
                    "message": {
                        "content": [
                            {
                                "type": "tool_use",
                                "name": "Edit",
                                "input": {"file_path": str(changed_file)},
                            }
                        ]
                    }
                }
            ]
        )

        result = handle_claude_stop(
            "run-1",
            {
                "last_assistant_message": "Fixed the requested issue.",
                "transcript_path": str(transcript_path),
            },
            self.registry,
            lambda run: None,
        )

        request = json.loads(
            (self.run_dir / "review-request.json").read_text(encoding="utf-8")
        )
        self.assertEqual(result, "dispatched")
        self.assertEqual(request["completion_scope"], "stage")

    def test_design_mode_does_not_dispatch_a_third_review(self) -> None:
        with self.registry._connect() as connection:
            connection.execute(
                """
                UPDATE runs
                SET mode = ?, review_loop = 2, state = ?
                WHERE run_id = ?
                """,
                (
                    RunMode.DESIGN.value,
                    RunState.WORKER_CONTINUING.value,
                    "run-1",
                ),
            )
        transcript_path = self._write_transcript(
            [
                {
                    "message": {
                        "content": [
                            {
                                "type": "tool_use",
                                "name": "Write",
                                "input": {"file_path": "/tmp/design.md"},
                            }
                        ]
                    }
                }
            ]
        )
        dispatched = []

        result = handle_claude_stop(
            "run-1",
            {
                "last_assistant_message": "Another design revision.",
                "transcript_path": str(transcript_path),
            },
            self.registry,
            lambda run: dispatched.append(run.run_id),
        )

        self.assertEqual(result, "design-round-limit")
        self.assertEqual(dispatched, [])
        self.assertEqual(
            self.registry.get_run("run-1").state, RunState.NEEDS_HUMAN
        )

    def test_human_marker_stops_automatic_review(self) -> None:
        dispatched = []

        result = handle_claude_stop(
            "run-1",
            {
                "last_assistant_message": (
                    "LOOPWEAVE_NEEDS_HUMAN Please choose the deployment target."
                )
            },
            self.registry,
            lambda run: dispatched.append(run.run_id),
        )

        self.assertEqual(result, "needs-human")
        self.assertEqual(dispatched, [])
        self.assertEqual(
            self.registry.get_run("run-1").state, RunState.NEEDS_HUMAN
        )

    def test_human_marker_mentioned_in_sentence_does_not_stop_review(self) -> None:
        transcript_path = self._write_transcript(
            [
                {
                    "message": {
                        "content": [
                            {
                                "type": "tool_use",
                                "name": "Write",
                                "input": {"file_path": "/tmp/project/app.py"},
                            }
                        ]
                    }
                }
            ]
        )
        dispatched = []

        result = handle_claude_stop(
            "run-1",
            {
                "last_assistant_message": (
                    "This turn has real output, so I will not mark "
                    "LOOPWEAVE_NEEDS_HUMAN and review should proceed."
                ),
                "transcript_path": str(transcript_path),
            },
            self.registry,
            lambda run: dispatched.append(run.run_id),
        )

        self.assertEqual(result, "dispatched")
        self.assertEqual(dispatched, ["run-1"])

    def test_failed_dispatch_can_be_retried(self) -> None:
        transcript_path = self._write_transcript(
            [
                {
                    "message": {
                        "content": [
                            {
                                "type": "tool_use",
                                "name": "Bash",
                                "input": {"command": "git status"},
                            }
                        ]
                    }
                }
            ]
        )
        payload = {
            "last_assistant_message": "Ready for review.",
            "transcript_path": str(transcript_path),
        }

        with self.assertRaises(RuntimeError):
            handle_claude_stop(
                "run-1",
                payload,
                self.registry,
                lambda run: (_ for _ in ()).throw(RuntimeError("dispatch failed")),
            )

        self.assertFalse((self.run_dir / "last-stop.sha256").exists())
        self.assertEqual(self.registry.get_run("run-1").review_loop, 0)
        dispatched = []
        result = handle_claude_stop(
            "run-1", payload, self.registry, lambda run: dispatched.append(run.run_id)
        )
        self.assertEqual(result, "dispatched")
        self.assertEqual(dispatched, ["run-1"])
        self.assertEqual(self.registry.get_run("run-1").review_loop, 1)

    def test_stop_hook_skips_chat_without_tool_evidence(self) -> None:
        transcript_path = self._write_transcript(
            [{"message": {"content": [{"type": "text", "text": "Hello"}]}}]
        )
        dispatched = []

        result = handle_claude_stop(
            "run-1",
            {
                "last_assistant_message": "Just chatting.",
                "transcript_path": str(transcript_path),
            },
            self.registry,
            lambda run: dispatched.append(run.run_id),
        )

        self.assertEqual(result, "no-review")
        self.assertEqual(dispatched, [])
        self.assertFalse((self.run_dir / "review-request.json").exists())
        self.assertEqual(self.registry.get_run("run-1").review_loop, 0)
        self.assertEqual(self.registry.get_run("run-1").state, RunState.RUNNING)

    def test_stop_hook_marks_explicit_intermediate_stage_review(self) -> None:
        transcript_path = self._write_transcript(
            [
                {
                    "message": {
                        "content": [
                            {
                                "type": "tool_use",
                                "name": "Bash",
                                "input": {"command": "python3 -m unittest"},
                            }
                        ]
                    }
                }
            ]
        )

        result = handle_claude_stop(
            "run-1",
            {
                "last_assistant_message": (
                    "LOOPWEAVE_STAGE\nTask 3 is complete and ready for review."
                ),
                "transcript_path": str(transcript_path),
            },
            self.registry,
            lambda run: None,
        )

        request = json.loads(
            (self.run_dir / "review-request.json").read_text(encoding="utf-8")
        )
        self.assertEqual(result, "dispatched")
        self.assertEqual(request["completion_scope"], "stage")

    def test_stop_hook_defaults_unmarked_develop_review_to_stage_scope(self) -> None:
        transcript_path = self._write_transcript(
            [
                {
                    "message": {
                        "content": [
                            {
                                "type": "tool_use",
                                "name": "Bash",
                                "input": {"command": "python3 -m unittest"},
                            }
                        ]
                    }
                }
            ]
        )

        handle_claude_stop(
            "run-1",
            {
                "last_assistant_message": "The requested work is complete.",
                "transcript_path": str(transcript_path),
            },
            self.registry,
            lambda run: None,
        )

        request = json.loads(
            (self.run_dir / "review-request.json").read_text(encoding="utf-8")
        )
        self.assertEqual(request["completion_scope"], "stage")

    def test_stop_hook_marks_explicit_final_completion(self) -> None:
        transcript_path = self._write_transcript(
            [
                {
                    "message": {
                        "content": [
                            {
                                "type": "tool_use",
                                "name": "Bash",
                                "input": {"command": "python3 -m unittest"},
                            }
                        ]
                    }
                }
            ]
        )

        handle_claude_stop(
            "run-1",
            {
                "last_assistant_message": (
                    "LOOPWEAVE_FINAL\nThe entire requested plan is complete."
                ),
                "transcript_path": str(transcript_path),
            },
            self.registry,
            lambda run: None,
        )

        request = json.loads(
            (self.run_dir / "review-request.json").read_text(encoding="utf-8")
        )
        self.assertEqual(request["completion_scope"], "final")

    def test_conflicting_scope_markers_require_human(self) -> None:
        dispatched = []

        result = handle_claude_stop(
            "run-1",
            {
                "last_assistant_message": (
                    "LOOPWEAVE_STAGE\nLOOPWEAVE_FINAL\nTask complete."
                ),
                "transcript_path": "",
            },
            self.registry,
            lambda run: dispatched.append(run.run_id),
        )

        self.assertEqual(result, "needs-human")
        self.assertEqual(dispatched, [])
        self.assertEqual(
            self.registry.get_run("run-1").state,
            RunState.NEEDS_HUMAN,
        )
        events = (self.run_dir / "events.jsonl").read_text(encoding="utf-8")
        self.assertIn("completion_scope_conflict", events)

    def test_stop_hook_ignores_approved_run_even_with_tool_evidence(self) -> None:
        self.registry.force_state("run-1", RunState.APPROVED)
        transcript_path = self._write_transcript(
            [
                {
                    "message": {
                        "content": [
                            {
                                "type": "tool_use",
                                "name": "Bash",
                                "input": {"command": "git status"},
                            }
                        ]
                    }
                }
            ]
        )
        dispatched = []

        result = handle_claude_stop(
            "run-1",
            {
                "last_assistant_message": "Acknowledged approval.",
                "transcript_path": str(transcript_path),
            },
            self.registry,
            lambda run: dispatched.append(run.run_id),
        )

        self.assertEqual(result, "terminal")
        self.assertEqual(dispatched, [])
        self.assertEqual(
            self.registry.get_run("run-1").state,
            RunState.APPROVED,
        )
        self.assertEqual(
            json.loads(
                (self.run_dir / "events.jsonl")
                .read_text(encoding="utf-8")
                .splitlines()[-1]
            )["event"],
            "stop_hook_ignored_terminal",
        )

    def test_stop_hook_ignores_owner_review_pending_run(self) -> None:
        self.registry.force_state("run-1", RunState.OWNER_REVIEW_PENDING)
        transcript_path = self._write_transcript(
            [
                {
                    "message": {
                        "content": [
                            {
                                "type": "tool_use",
                                "name": "Bash",
                                "input": {"command": "python3 -m unittest"},
                            }
                        ]
                    }
                }
            ]
        )
        dispatched = []

        result = handle_claude_stop(
            "run-1",
            {
                "last_assistant_message": "Waiting for owner final review.",
                "transcript_path": str(transcript_path),
            },
            self.registry,
            lambda run: dispatched.append(run.run_id),
        )

        self.assertEqual(result, "owner_review_pending")
        self.assertEqual(dispatched, [])
        self.assertEqual(
            self.registry.get_run("run-1").state,
            RunState.OWNER_REVIEW_PENDING,
        )
        self.assertEqual(
            json.loads(
                (self.run_dir / "events.jsonl")
                .read_text(encoding="utf-8")
                .splitlines()[-1]
            )["event"],
            "stop_hook_ignored_owner_review_pending",
        )

    def test_extract_transcript_evidence_deduplicates_tools(self) -> None:
        transcript_path = self._write_transcript(
            [
                {
                    "message": {
                        "content": [
                            {
                                "type": "tool_use",
                                "name": "Bash",
                                "input": {"command": "npm test"},
                            },
                            {
                                "type": "tool_use",
                                "name": "Write",
                                "input": {"file_path": "/tmp/a.js"},
                            },
                        ]
                    }
                },
                {
                    "message": {
                        "content": [
                            {
                                "type": "tool_use",
                                "name": "Bash",
                                "input": {"command": "npm test"},
                            }
                        ]
                    }
                },
            ]
        )

        evidence = extract_transcript_evidence(transcript_path)

        self.assertEqual(evidence.commands_run, ["npm test"])
        self.assertEqual(evidence.files_changed, ["/tmp/a.js"])

    def test_evidence_fingerprint_changes_when_command_result_changes(self) -> None:
        failing = self._write_transcript(
            [
                {
                    "type": "assistant",
                    "message": {
                        "content": [
                            {
                                "type": "tool_use",
                                "id": "tool-1",
                                "name": "Bash",
                                "input": {"command": "pytest -q"},
                            }
                        ]
                    },
                },
                {
                    "type": "user",
                    "message": {
                        "content": [
                            {
                                "type": "tool_result",
                                "tool_use_id": "tool-1",
                                "content": "1 failed",
                            }
                        ]
                    },
                },
            ],
            name="failing-transcript.jsonl",
        )
        passing = self._write_transcript(
            [
                {
                    "type": "assistant",
                    "message": {
                        "content": [
                            {
                                "type": "tool_use",
                                "id": "tool-1",
                                "name": "Bash",
                                "input": {"command": "pytest -q"},
                            }
                        ]
                    },
                },
                {
                    "type": "user",
                    "message": {
                        "content": [
                            {
                                "type": "tool_result",
                                "tool_use_id": "tool-1",
                                "content": "10 passed",
                            }
                        ]
                    },
                },
            ],
            name="passing-transcript.jsonl",
        )

        from loopweave.hook_entry import evidence_fingerprint

        self.assertNotEqual(
            evidence_fingerprint(extract_transcript_evidence(failing)),
            evidence_fingerprint(extract_transcript_evidence(passing)),
        )

    def test_extract_transcript_evidence_ignores_tools_before_current_human_turn(self) -> None:
        transcript_path = self._write_transcript(
            [
                {
                    "type": "assistant",
                    "message": {
                        "content": [
                            {
                                "type": "tool_use",
                                "name": "Edit",
                                "input": {"file_path": "/tmp/old.py"},
                            }
                        ]
                    },
                },
                {
                    "type": "user",
                    "message": {"content": "Just answer this question."},
                },
                {
                    "type": "assistant",
                    "message": {
                        "content": [{"type": "text", "text": "No tools needed."}]
                    },
                },
            ]
        )

        evidence = extract_transcript_evidence(transcript_path)

        self.assertEqual(evidence.commands_run, [])
        self.assertEqual(evidence.files_changed, [])

    def _write_transcript(self, records, name="claude-transcript.jsonl"):
        path = self.root / name
        path.write_text(
            "".join(json.dumps(record) + "\n" for record in records),
            encoding="utf-8",
        )
        return path

    def test_hook_bounds_oversized_single_command_before_submission(self) -> None:
        """reviewer Stage 2 re-review P1 #2: a transcript containing one Bash
        command over MAX_EVIDENCE_ITEM_BYTES (4096) must not raise
        EvidenceTooLarge from the shared submission validator. Before the
        bounded-conversion fix the hook only count-capped, so a single 4097+
        byte command made submit_stage reject the whole turn. Proven by
        asserting handle_claude_stop returns 'dispatched' and the persisted
        review-request command is bounded to <= 4096 UTF-8 bytes."""
        from loopweave.submission import MAX_EVIDENCE_ITEM_BYTES

        oversized_command = "echo " + ("x" * (MAX_EVIDENCE_ITEM_BYTES + 600))
        self.assertGreater(
            len(oversized_command.encode("utf-8")), MAX_EVIDENCE_ITEM_BYTES
        )
        transcript_path = self._write_transcript(
            [
                {
                    "message": {
                        "content": [
                            {
                                "type": "tool_use",
                                "name": "Bash",
                                "input": {"command": oversized_command},
                            },
                            {
                                "type": "tool_use",
                                "name": "Edit",
                                "input": {"file_path": "/tmp/project/app.py"},
                            },
                        ]
                    }
                }
            ]
        )
        payload = {
            "last_assistant_message": "LOOPWEAVE_STAGE\nDone.",
            "transcript_path": str(transcript_path),
        }

        result = handle_claude_stop(
            "run-1", payload, self.registry, lambda run: None
        )

        self.assertEqual(result, "dispatched")
        request = json.loads(
            (self.run_dir / "review-request.json").read_text(encoding="utf-8")
        )
        self.assertTrue(request["commands_run"])
        self.assertLessEqual(
            len(request["commands_run"][0].encode("utf-8")),
            MAX_EVIDENCE_ITEM_BYTES,
        )

    def test_hook_compacts_aggregate_oversize_before_submission(self) -> None:
        """reviewer Stage 2 re-review P1 #2: many commands whose aggregate canonical
        encoding exceeds MAX_EVIDENCE_TOTAL_BYTES (64 KiB) must be compacted
        by the hook before submission, not rejected. Before the fix this raised
        EvidenceTooLarge. Proven by a successful dispatch with a bounded
        review-request whose commands list is non-empty and under the total."""
        from loopweave.submission import MAX_EVIDENCE_TOTAL_BYTES

        big_command = "echo " + ("y" * 1500)
        many_commands = [
            {
                "type": "tool_use",
                "name": "Bash",
                "input": {"command": "{}_{:04d}".format(big_command, i)},
            }
            for i in range(60)
        ]
        many_commands.append(
            {
                "type": "tool_use",
                "name": "Edit",
                "input": {"file_path": "/tmp/project/app.py"},
            }
        )
        transcript_path = self._write_transcript(
            [{"message": {"content": many_commands}}]
        )
        full_aggregate = len(big_command) * 60
        self.assertGreater(full_aggregate, MAX_EVIDENCE_TOTAL_BYTES)
        payload = {
            "last_assistant_message": "LOOPWEAVE_STAGE\nDone.",
            "transcript_path": str(transcript_path),
        }

        result = handle_claude_stop(
            "run-1", payload, self.registry, lambda run: None
        )

        self.assertEqual(result, "dispatched")
        request = json.loads(
            (self.run_dir / "review-request.json").read_text(encoding="utf-8")
        )
        self.assertTrue(request["commands_run"])
        encoded_total = len(
            json.dumps(
                {
                    "files_changed": request["files_changed"],
                    "commands_run": request["commands_run"],
                    "tests": request["tests"],
                    "known_issues": request["known_issues"],
                    "questions_for_reviewer": request["questions_for_reviewer"],
                },
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        )
        self.assertLessEqual(encoded_total, MAX_EVIDENCE_TOTAL_BYTES)


if __name__ == "__main__":
    unittest.main()
