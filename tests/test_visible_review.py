from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from loopweave.protocol import ProtocolError
from loopweave.visible_review import (
    MAX_REVIEW_CARD_BYTES,
    create_review_card,
    latest_pending_card,
    queue_visible_review_card,
    render_review_next_instruction,
    submit_visible_review,
    validate_review_card,
)


class VisibleReviewTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.workspace = self.root / "workspace"
        self.workspace.mkdir()
        self.task = self.root / "task.md"
        self.task.write_text("# Task\n", encoding="utf-8")
        self.plan = self.root / "plan.md"
        self.plan.write_text("# Plan\n", encoding="utf-8")
        self.changed = self.workspace / "app.py"
        self.changed.write_text("ok = True\n", encoding="utf-8")

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_valid_card_round_trips(self) -> None:
        card = create_review_card(
            run_id="run-1",
            project_slug="demo",
            stage_id="stage-1",
            stage_title="Foundation",
            completion_scope="stage",
            workspace_root=self.workspace,
            task_packet_path=self.task,
            plan_path=self.plan,
            work_summary="Implemented foundation files.",
            completed_items=["Created app.py"],
            not_completed_items=[],
            changed_files=[self.changed],
            artifact_paths=[],
            test_commands=["python3 -m unittest"],
            test_result_summary="Unit tests passed: 1/1.",
            worker_claims=["Foundation file exists."],
            known_issues=[],
            questions_for_reviewer=[],
        )

        validated = validate_review_card(card)

        self.assertEqual(validated["run_id"], "run-1")
        self.assertEqual(validated["stage_id"], "stage-1")
        self.assertNotIn("acceptance_criteria", validated)

    def test_rejects_worker_owned_standards(self) -> None:
        card = self._minimal_card()
        card["acceptance_criteria"] = ["Worker says this is enough."]

        with self.assertRaises(ProtocolError):
            validate_review_card(card)

    def test_rejects_changed_file_outside_workspace(self) -> None:
        card = self._minimal_card()
        card["changed_files"] = [str(self.root / "outside.py")]

        with self.assertRaises(ProtocolError):
            validate_review_card(card)

    def test_rejects_oversized_card(self) -> None:
        card = self._minimal_card()
        card["work_summary"] = "x" * (MAX_REVIEW_CARD_BYTES + 1)

        with self.assertRaises(ProtocolError):
            validate_review_card(card)

    def test_accepts_twenty_kib_review_card(self) -> None:
        card = self._minimal_card()
        card["work_summary"] = "x" * (20 * 1024)

        validated = validate_review_card(card)

        self.assertEqual(validated["work_summary"], card["work_summary"])

    def test_compaction_cannot_hide_path_outside_workspace(self) -> None:
        changed_files = [
            self.workspace / ("long-name-{:04d}-{}.py".format(i, "x" * 80))
            for i in range(400)
        ]
        changed_files.append(self.root / "outside.py")

        with self.assertRaises(ProtocolError):
            create_review_card(
                run_id="run-1",
                project_slug="demo",
                stage_id="stage-1",
                stage_title="Foundation",
                completion_scope="stage",
                workspace_root=self.workspace,
                task_packet_path=self.task,
                plan_path=self.plan,
                work_summary="Implemented foundation files.",
                completed_items=["Changed files."],
                not_completed_items=[],
                changed_files=changed_files,
                artifact_paths=[],
                test_commands=[],
                test_result_summary="Not run.",
                worker_claims=[],
                known_issues=[],
                questions_for_reviewer=[],
            )

    def test_queue_writes_pending_card(self) -> None:
        run_dir = self.root / "run"
        card = self._minimal_card()

        path = queue_visible_review_card(run_dir, card)

        self.assertEqual(path.parent.name, "review-inbox")
        self.assertTrue(path.exists())
        self.assertTrue((run_dir / "review-inbox" / "pending").exists())

    def test_latest_pending_card_reads_queued_card(self) -> None:
        run_dir = self.root / "run"
        card = self._minimal_card()
        queue_visible_review_card(run_dir, card)

        pending = latest_pending_card(run_dir)

        self.assertEqual(pending["review_id"], card["review_id"])

    def test_review_next_instruction_derives_reviewer_owned_directive(self) -> None:
        card = self._minimal_card()

        instruction = render_review_next_instruction(card)

        self.assertIn("Reviewer-owned review directive", instruction)
        self.assertIn("stage-1", instruction)
        self.assertIn("Use the task packet and plan as authority", instruction)
        self.assertIn(
            "review-submit --run-id run-1 --review-file", instruction
        )
        self.assertNotIn("terminal.raw.log", instruction)

    def test_submit_visible_review_writes_protocol_files(self) -> None:
        run_dir = self.root / "run"
        run_dir.mkdir()
        card = self._minimal_card()
        queue_visible_review_card(run_dir, card)
        review_file = self.root / "verdict.md"
        review_file.write_text(
            (
                "---\n"
                "verdict: changes_requested\n"
                "summary: Fix validation.\n"
                "---\n\n"
                "Fix validation.\n"
            ),
            encoding="utf-8",
        )

        review = submit_visible_review(run_dir, "run-1", review_file)

        self.assertEqual(review["verdict"], "changes_requested")
        self.assertTrue((run_dir / "reviewer-verdict.json").exists())
        self.assertTrue((run_dir / "reviewer-verdict.md").exists())
        self.assertFalse((run_dir / "review-inbox" / "pending").exists())
        self.assertTrue((run_dir / "review-inbox" / "resolved").exists())

    def test_submit_visible_review_rejects_second_submit_after_resolved(self) -> None:
        run_dir = self.root / "run"
        run_dir.mkdir()
        card = self._minimal_card()
        queue_visible_review_card(run_dir, card)
        review_file = self.root / "verdict.md"
        review_file.write_text(
            (
                "---\n"
                "verdict: approved\n"
                "summary: Looks good.\n"
                "---\n\n"
                "Looks good.\n"
            ),
            encoding="utf-8",
        )
        submit_visible_review(run_dir, "run-1", review_file)

        with self.assertRaises(ProtocolError) as raised:
            submit_visible_review(run_dir, "run-1", review_file)

        self.assertIn("no pending visible review", str(raised.exception))

    def test_submit_visible_review_rejects_concurrent_submit_lock(self) -> None:
        run_dir = self.root / "run"
        run_dir.mkdir()
        card = self._minimal_card()
        queue_visible_review_card(run_dir, card)
        review_file = self.root / "verdict.md"
        review_file.write_text(
            (
                "---\n"
                "verdict: approved\n"
                "summary: Looks good.\n"
                "---\n\n"
                "Looks good.\n"
            ),
            encoding="utf-8",
        )
        lock = run_dir / "review-inbox" / "pending.submit.lock"
        fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        os.close(fd)
        try:
            with self.assertRaises(ProtocolError) as raised:
                submit_visible_review(run_dir, "run-1", review_file)
        finally:
            lock.unlink()

        self.assertIn("already being submitted", str(raised.exception))

    def test_submit_visible_review_releases_lock_after_invalid_review_file(self) -> None:
        run_dir = self.root / "run"
        run_dir.mkdir()
        queue_visible_review_card(run_dir, self._minimal_card())
        review_file = self.root / "invalid-verdict.md"
        review_file.write_text("approved\n", encoding="utf-8")

        with self.assertRaises(ProtocolError) as raised:
            submit_visible_review(run_dir, "run-1", review_file)

        self.assertIn("front matter", str(raised.exception))
        self.assertFalse(
            (run_dir / "review-inbox" / "pending.submit.lock").exists()
        )

    def test_submit_visible_review_recovers_dead_submitter_lock(self) -> None:
        run_dir = self.root / "run"
        run_dir.mkdir()
        queue_visible_review_card(run_dir, self._minimal_card())
        review_file = self.root / "verdict.md"
        review_file.write_text(
            "---\nverdict: approved\nsummary: Looks good.\n---\n\nLooks good.\n",
            encoding="utf-8",
        )
        (run_dir / "review-inbox" / "pending.submit.lock").write_text(
            "999999999\n", encoding="utf-8"
        )

        review = submit_visible_review(run_dir, "run-1", review_file)

        self.assertEqual(review["verdict"], "approved")
        self.assertFalse(
            (run_dir / "review-inbox" / "pending.submit.lock").exists()
        )

    def _minimal_card(self) -> dict:
        return create_review_card(
            run_id="run-1",
            project_slug="demo",
            stage_id="stage-1",
            stage_title="Foundation",
            completion_scope="stage",
            workspace_root=self.workspace,
            task_packet_path=self.task,
            plan_path=self.plan,
            work_summary="Implemented foundation files.",
            completed_items=["Created app.py"],
            not_completed_items=[],
            changed_files=[self.changed],
            artifact_paths=[],
            test_commands=[],
            test_result_summary="Not run.",
            worker_claims=[],
            known_issues=[],
            questions_for_reviewer=[],
        )


if __name__ == "__main__":
    unittest.main()
