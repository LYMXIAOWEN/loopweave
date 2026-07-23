from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from loopweave.dispatcher import (
    CodexDispatcher,
    DispatchError,
    DispatchInFlight,
    DispatchLeaseState,
    build_review_prompt,
    inspect_dispatch_lease,
)
from loopweave.models import RunMode


class DispatcherTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.run_dir = Path(self.temp_dir.name)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_builds_narrow_review_prompt(self) -> None:
        prompt = build_review_prompt("run-1", self.run_dir)

        self.assertIn("run-1", prompt)
        self.assertIn(str(self.run_dir / "review-request.json"), prompt)
        self.assertIn("Do not review any other run", prompt)
        self.assertIn("Return the review as structured JSON", prompt)

    def test_review_prompt_explains_bridge_owned_stage_continuation(self) -> None:
        prompt = build_review_prompt("run-1", self.run_dir)

        self.assertIn("bridge determines whether an approval is intermediate", prompt)

    def test_dispatch_runs_from_complete_workspace(self) -> None:
        workspace = self.run_dir / "workspace"
        workspace.mkdir()
        calls = []

        def runner(command, **kwargs):
            calls.append((command, kwargs))
            self._write_approved_output(command)
            return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

        dispatcher = CodexDispatcher(codex_bin="/usr/bin/codex", runner=runner)

        dispatcher.dispatch(
            "thread-1",
            "run-1",
            self.run_dir,
            workspace_root=workspace,
            project_root=self.run_dir / "project",
        )

        _, kwargs = calls[0]
        self.assertEqual(kwargs["cwd"], str(workspace.resolve()))
        self.assertIn(str(workspace.resolve()), kwargs["input"])
        self.assertIn(str(self.run_dir.resolve()), kwargs["input"])
        self.assertIn("Inspect the complete workspace", kwargs["input"])
        self.assertIn("Do not edit workspace files", kwargs["input"])

    def test_dispatch_requires_exact_thread_id(self) -> None:
        dispatcher = CodexDispatcher(codex_bin="/usr/bin/codex")

        with self.assertRaises(DispatchError):
            dispatcher.dispatch("", "run-1", self.run_dir)

    def test_dispatch_uses_ephemeral_review_and_materializes_protocol_files(self) -> None:
        calls = []

        def runner(command, **kwargs):
            calls.append((command, kwargs))
            output_path = Path(command[command.index("--output-last-message") + 1])
            output_path.write_text(
                json.dumps(
                    {
                        "verdict": "changes_requested",
                        "summary": "Fix the failing edge case.",
                        "review_markdown": "# Review\n\nThe edge case still fails.\n",
                        "continue": True,
                    }
                ),
                encoding="utf-8",
            )
            return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

        dispatcher = CodexDispatcher(codex_bin="/usr/bin/codex", runner=runner)

        result = dispatcher.dispatch("thread-1", "run-1", self.run_dir)

        command, kwargs = calls[0]
        self.assertEqual(
            command[:5],
            ["/usr/bin/codex", "exec", "--ephemeral", "--sandbox", "read-only"],
        )
        self.assertNotIn("resume", command)
        self.assertNotIn("thread-1", command)
        self.assertIn("--output-schema", command)
        self.assertEqual(
            kwargs["input"],
            build_review_prompt("run-1", self.run_dir, origin_thread_id="thread-1"),
        )
        self.assertEqual(result.returncode, 0)
        review = json.loads(
            (self.run_dir / "reviewer-verdict.json").read_text(encoding="utf-8")
        )
        self.assertEqual(review["run_id"], "run-1")
        self.assertEqual(review["verdict"], "changes_requested")
        self.assertTrue(review["continue"])
        self.assertEqual(
            (self.run_dir / "reviewer-verdict.md").read_text(encoding="utf-8"),
            "# Review\n\nThe edge case still fails.\n",
        )

    def test_design_dispatch_uses_convergence_prompt_and_persists_fields(self) -> None:
        calls = []

        def runner(command, **kwargs):
            calls.append((command, kwargs))
            output_path = Path(command[command.index("--output-last-message") + 1])
            output_path.write_text(
                json.dumps(
                    {
                        "verdict": "changes_requested",
                        "summary": "Resolve ownership.",
                        "review_markdown": (
                            "# Review\n\nState ownership is unresolved.\n"
                        ),
                        "continue": True,
                        "round": 1,
                        "blocking_decisions": ["Choose the state owner."],
                        "advisory_notes": ["Add one sequence diagram."],
                        "consensus_summary": (
                            "The file protocol remains shared."
                        ),
                        "next_action": "revise_design",
                    }
                ),
                encoding="utf-8",
            )
            return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

        dispatcher = CodexDispatcher(codex_bin="/usr/bin/codex", runner=runner)

        dispatcher.dispatch(
            "thread-1",
            "run-1",
            self.run_dir,
            mode=RunMode.DESIGN,
            review_round=1,
        )

        prompt = calls[0][1]["input"]
        self.assertIn("proposal and architecture", prompt)
        self.assertIn("round 1 of 2", prompt)
        self.assertIn("Minor improvements are advisory", prompt)
        self.assertIn("Round 2 must not request another revision", prompt)
        review = json.loads(
            (self.run_dir / "reviewer-verdict.json").read_text(encoding="utf-8")
        )
        self.assertEqual(review["round"], 1)
        self.assertEqual(
            review["blocking_decisions"], ["Choose the state owner."]
        )
        self.assertEqual(review["next_action"], "revise_design")

    def test_existing_dispatch_lock_is_rejected(self) -> None:
        (self.run_dir / "dispatch.lock").write_text(
            json.dumps(
                {
                    "pid": os.getpid(),
                    "process_start": "current-start",
                    "binding_generation": 1,
                    "created_at": "2026-06-19T00:00:00+00:00",
                    "lease_id": "live-lease",
                }
            ),
            encoding="utf-8",
        )
        dispatcher = CodexDispatcher(codex_bin="/usr/bin/codex")

        with patch(
            "loopweave.terminal_host.default_process_identity_reader",
            return_value=lambda pid: "current-start",
        ):
            with self.assertRaises(DispatchInFlight):
                dispatcher.dispatch("thread-1", "run-1", self.run_dir)

    def test_dispatch_lease_states(self) -> None:
        self.assertEqual(
            inspect_dispatch_lease(self.run_dir), DispatchLeaseState.ABSENT
        )
        lock = self.run_dir / "dispatch.lock"
        lock.write_text("not-json", encoding="utf-8")
        self.assertEqual(
            inspect_dispatch_lease(self.run_dir), DispatchLeaseState.STALE
        )
        lock.write_text(
            json.dumps(
                {
                    "pid": 123,
                    "process_start": "start",
                    "binding_generation": 2,
                    "created_at": "2026-06-19T00:00:00+00:00",
                    "lease_id": "lease-1",
                }
            ),
            encoding="utf-8",
        )
        with patch(
            "loopweave.terminal_host.default_process_identity_reader",
            return_value=lambda pid: "start",
        ):
            self.assertEqual(
                inspect_dispatch_lease(self.run_dir), DispatchLeaseState.LIVE
            )
        with patch(
            "loopweave.terminal_host.default_process_identity_reader",
            return_value=lambda pid: "different",
        ):
            self.assertEqual(
                inspect_dispatch_lease(self.run_dir), DispatchLeaseState.STALE
            )

    def test_dispatch_writes_identity_and_generation_to_lease(self) -> None:
        seen = {}

        def runner(command, **kwargs):
            seen.update(
                json.loads(
                    (self.run_dir / "dispatch.lock").read_text(encoding="utf-8")
                )
            )
            self._write_approved_output(command)
            return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

        dispatcher = CodexDispatcher(codex_bin="/usr/bin/codex", runner=runner)

        with patch(
            "loopweave.terminal_host.default_process_identity_reader",
            return_value=lambda pid: "dispatcher-start",
        ):
            dispatcher.dispatch(
                "thread-1",
                "run-1",
                self.run_dir,
                binding_generation=3,
            )

        self.assertEqual(seen["pid"], os.getpid())
        self.assertEqual(seen["process_start"], "dispatcher-start")
        self.assertEqual(seen["binding_generation"], 3)
        self.assertTrue(seen["created_at"])
        self.assertTrue(seen["lease_id"])
        self.assertFalse((self.run_dir / "dispatch.lock").exists())

    def test_stale_lease_is_removed_and_dispatch_succeeds(self) -> None:
        (self.run_dir / "dispatch.lock").write_text("invalid", encoding="utf-8")

        def runner(command, **kwargs):
            self._write_approved_output(command)
            return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

        dispatcher = CodexDispatcher(codex_bin="/usr/bin/codex", runner=runner)

        result = dispatcher.dispatch("thread-1", "run-1", self.run_dir)

        self.assertEqual(result.returncode, 0)
        self.assertFalse((self.run_dir / "dispatch.lock").exists())

    def test_stale_recovery_does_not_unlink_replacement_lease(self) -> None:
        lock = self.run_dir / "dispatch.lock"
        lock.write_text("invalid", encoding="utf-8")
        replacement = {
            "pid": 999,
            "process_start": "replacement-start",
            "binding_generation": 4,
            "created_at": "2026-06-19T00:00:00+00:00",
            "lease_id": "replacement-lease",
        }

        def replace_during_inspection(run_dir):
            lock.unlink()
            lock.write_text(json.dumps(replacement), encoding="utf-8")
            return DispatchLeaseState.STALE

        dispatcher = CodexDispatcher(codex_bin="/usr/bin/codex")

        with patch(
            "loopweave.dispatcher.inspect_dispatch_lease",
            side_effect=replace_during_inspection,
        ):
            with self.assertRaises(DispatchInFlight):
                dispatcher.dispatch("thread-1", "run-1", self.run_dir)

        remaining = json.loads(lock.read_text(encoding="utf-8"))
        self.assertEqual(remaining["lease_id"], "replacement-lease")

    def test_dispatch_does_not_remove_replacement_lease(self) -> None:
        replacement = {
            "pid": 999,
            "process_start": "replacement-start",
            "binding_generation": 4,
            "created_at": "2026-06-19T00:00:00+00:00",
            "lease_id": "replacement-lease",
        }

        def runner(command, **kwargs):
            self._write_approved_output(command)
            (self.run_dir / "dispatch.lock").write_text(
                json.dumps(replacement), encoding="utf-8"
            )
            return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

        dispatcher = CodexDispatcher(codex_bin="/usr/bin/codex", runner=runner)

        dispatcher.dispatch("thread-1", "run-1", self.run_dir)

        remaining = json.loads(
            (self.run_dir / "dispatch.lock").read_text(encoding="utf-8")
        )
        self.assertEqual(remaining["lease_id"], "replacement-lease")

    def test_failed_dispatch_records_error(self) -> None:
        def runner(command, **kwargs):
            return subprocess.CompletedProcess(
                command, 2, stdout="", stderr="resume failed"
            )

        dispatcher = CodexDispatcher(codex_bin="/usr/bin/codex", runner=runner)

        with self.assertRaises(DispatchError):
            dispatcher.dispatch("thread-1", "run-1", self.run_dir)

        self.assertIn(
            "resume failed",
            (self.run_dir / "dispatch-error.txt").read_text(encoding="utf-8"),
        )

    def test_invalid_structured_output_is_rejected(self) -> None:
        def runner(command, **kwargs):
            output_path = Path(command[command.index("--output-last-message") + 1])
            output_path.write_text("not json", encoding="utf-8")
            return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

        dispatcher = CodexDispatcher(codex_bin="/usr/bin/codex", runner=runner)

        with self.assertRaises(DispatchError):
            dispatcher.dispatch("thread-1", "run-1", self.run_dir)

    def _write_approved_output(self, command) -> None:
        output_path = Path(command[command.index("--output-last-message") + 1])
        output_path.write_text(
            json.dumps(
                {
                    "verdict": "approved",
                    "summary": "Approved.",
                    "review_markdown": "# Review\n\nApproved.\n",
                    "continue": False,
                }
            ),
            encoding="utf-8",
        )


if __name__ == "__main__":
    unittest.main()
