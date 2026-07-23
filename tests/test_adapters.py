from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from loopweave.adapters import get_adapter
from loopweave.adapters.claude import ClaudeAdapter
from loopweave.adapters.generic import GenericAdapter


class AdapterTests(unittest.TestCase):
    def test_claude_workspace_prompt_preserves_baseline(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            run_dir = Path(directory)
            workspace = run_dir / "workspace"
            workspace.mkdir()
            baseline = run_dir / "workspace-baseline.json"
            adapter = ClaudeAdapter(
                run_id="run-1",
                workspace_root=workspace,
                baseline_path=baseline,
            )

            command = adapter.build_command(run_dir)

        prompt = command[command.index("--append-system-prompt") + 1]
        self.assertIn(str(workspace), prompt)
        self.assertIn(str(baseline), prompt)
        self.assertIn("Do not reset, clean, checkout", prompt)

    def test_claude_workspace_prompt_requires_plain_completion_markers(self) -> None:
        adapter = ClaudeAdapter(run_id="run-1")

        with tempfile.TemporaryDirectory() as directory:
            command = adapter.build_command(Path(directory))

        prompt = command[command.index("--append-system-prompt") + 1]
        self.assertIn("no Markdown\nbackticks", prompt)
        self.assertIn("without code changes", prompt)

    def test_generic_adapter_preserves_command(self) -> None:
        adapter = GenericAdapter(["custom-agent", "--flag", "value"])

        self.assertEqual(
            adapter.build_command(Path("/tmp/run")),
            ["custom-agent", "--flag", "value"],
        )

    def test_unknown_named_adapter_is_rejected(self) -> None:
        result = get_adapter("missing-agent", [], run_id="run-1")
        self.assertIsInstance(result, GenericAdapter)

    def test_claude_adapter_builds_managed_command_and_settings(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            run_dir = Path(directory)
            adapter = ClaudeAdapter(
                run_id="run-1",
                extra_args=["--model", "sonnet"],
                launcher="/workspace/loopweave/bin/loopweave",
            )

            command = adapter.build_command(run_dir)

            self.assertEqual(command[0], "claude")
            self.assertIn("--settings", command)
            self.assertIn("--append-system-prompt", command)
            self.assertEqual(command[-2:], ["--model", "sonnet"])
            settings_path = Path(command[command.index("--settings") + 1])
            settings = json.loads(settings_path.read_text(encoding="utf-8"))
            hook = settings["hooks"]["Stop"][0]["hooks"][0]["command"]
            self.assertIn("hook claude-stop --run-id run-1", hook)
            self.assertEqual(
                settings["hooks"]["Stop"][0]["hooks"][0]["timeout"], 1800
            )

    def test_claude_review_format_is_explicit(self) -> None:
        adapter = ClaudeAdapter(run_id="run-1")

        inputs = adapter.review_input_sequence(
            verdict="changes_requested",
            review_text="Fix the unsafe delete.",
        )

        self.assertEqual(len(inputs), 2)
        message = inputs[0]
        self.assertIn("LoopWeave review", message)
        self.assertIn("changes_requested", message)
        self.assertIn("Fix the unsafe delete.", message)
        self.assertTrue(message.endswith("\n"))
        self.assertEqual(inputs[1], "\r")

    def test_claude_resume_flag_is_forwarded_unchanged(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            adapter = ClaudeAdapter(run_id="run-1", extra_args=["-r"])

            command = adapter.build_command(Path(directory))

        self.assertEqual(command[-1], "-r")

    def test_approved_format_tells_worker_to_stop_revising(self) -> None:
        adapter = ClaudeAdapter(run_id="run-1")

        inputs = adapter.approved_input_sequence(
            "All required checks passed."
        )

        self.assertEqual(len(inputs), 2)
        self.assertIn("[LoopWeave review: approved]", inputs[0])
        self.assertIn("task is complete", inputs[0].lower())
        self.assertIn("stop revising", inputs[0].lower())
        self.assertIn("All required checks passed.", inputs[0])
        self.assertEqual(inputs[1], "\r")

    def test_stage_approved_format_advances_worker_and_explains_scope_markers(
        self,
    ) -> None:
        adapter = ClaudeAdapter(run_id="run-1")

        inputs = adapter.stage_approved_input_sequence(
            "Task 3 passed its quality gate."
        )

        self.assertIn("[LoopWeave review: stage approved]", inputs[0])
        self.assertIn("continue to the next stage", inputs[0].lower())
        self.assertIn("LOOPWEAVE_STAGE", inputs[0])
        self.assertIn("LOOPWEAVE_FINAL", inputs[0])
        self.assertEqual(inputs[1], "\r")


if __name__ == "__main__":
    unittest.main()
