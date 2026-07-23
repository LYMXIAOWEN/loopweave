from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from loopweave.health_check import check_run


def _good_run(run_id="run-test"):
    return {
        "schema_version": 1,
        "run_id": run_id,
        "agent_pid": 123,
        "codex_thread_id": "thread-1",
        "cwd": "/tmp",
        "thread_cwd": "/tmp/control",
        "workspace_root": "/tmp/workspace",
        "project_slug": "demo",
        "project_root": "/tmp/projects/demo",
        "tty": "/dev/ttys000",
        "agent": "claude",
        "agent_process_start": "Thu Jun 18 13:29:56 2026",
        "socket_path": "/tmp/x.sock",
    }


def _good_request(run_id="run-test"):
    return {
        "schema_version": 1,
        "run_id": run_id,
        "status": "ready_for_review",
        "task_summary": "t",
        "change_summary": "c",
        "files_changed": ["a.py"],
        "commands_run": [],
        "tests": [],
        "known_issues": [],
        "questions_for_reviewer": [],
    }


def _good_review(run_id="run-test", verdict="approved", cont=False,
                 review_file="reviewer-verdict.md"):
    return {
        "schema_version": 1,
        "run_id": run_id,
        "review_id": "rev-1",
        "verdict": verdict,
        "summary": "ok",
        "review_file": review_file,
        "continue": cont,
    }


def _write(d, *, run=None, request=None, review=None,
           review_text="# review\nok", skip_review_json=False):
    (d / "run.json").write_text(
        json.dumps(run if run is not None else _good_run()), encoding="utf-8")
    (d / "review-request.json").write_text(
        json.dumps(request if request is not None else _good_request()),
        encoding="utf-8")
    if skip_review_json:
        return
    rev = review if review is not None else _good_review()
    (d / "reviewer-verdict.json").write_text(json.dumps(rev), encoding="utf-8")
    target = Path(rev.get("review_file", "reviewer-verdict.md"))
    if not target.is_absolute():
        target = d / target
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(review_text, encoding="utf-8")


def _patch_review(d, **fields):
    rev = json.loads((d / "reviewer-verdict.json").read_text(encoding="utf-8"))
    rev.update(fields)
    (d / "reviewer-verdict.json").write_text(json.dumps(rev), encoding="utf-8")
    return rev


class CheckRunTests(unittest.TestCase):
    def test_stage_approved_review_can_continue(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            run_dir = Path(directory)
            _write(
                run_dir,
                request=dict(_good_request(), completion_scope="stage"),
                review=_good_review(verdict="approved", cont=True),
            )

            self.assertEqual(check_run(run_dir), [])

    def test_design_approval_remains_final_even_with_stage_scope_metadata(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            run_dir = Path(directory)
            _write(
                run_dir,
                request=dict(
                    _good_request(),
                    mode="design",
                    completion_scope="stage",
                ),
                review=_good_review(verdict="approved", cont=False),
            )

            self.assertEqual(check_run(run_dir), [])

    def test_healthy_run_passes(self):
        with tempfile.TemporaryDirectory() as t:
            d = Path(t)
            _write(d)
            self.assertEqual(check_run(d), [])

    def test_legacy_cwd_only_run_still_passes(self):
        with tempfile.TemporaryDirectory() as t:
            d = Path(t)
            run = _good_run()
            for key in (
                "thread_cwd",
                "workspace_root",
                "project_slug",
                "project_root",
            ):
                del run[key]
            _write(d, run=run)
            self.assertEqual(check_run(d), [])

    def test_new_run_requires_valid_workspace_paths(self):
        with tempfile.TemporaryDirectory() as t:
            d = Path(t)
            run = _good_run()
            run["workspace_root"] = []
            _write(d, run=run)
            self.assertTrue(
                any("workspace_root" in failure for failure in check_run(d))
            )

    def test_missing_reviewer_verdict_json_fails(self):
        with tempfile.TemporaryDirectory() as t:
            d = Path(t)
            _write(d, skip_review_json=True)
            self.assertTrue(any("reviewer-verdict.json" in f for f in check_run(d)))

    def test_non_object_json_does_not_crash(self):
        with tempfile.TemporaryDirectory() as t:
            d = Path(t)
            _write(d)
            (d / "reviewer-verdict.json").write_text("[]", encoding="utf-8")
            failures = check_run(d)
            self.assertTrue(any("reviewer-verdict.json" in f for f in failures))

    def test_missing_required_review_field_fails(self):
        with tempfile.TemporaryDirectory() as t:
            d = Path(t)
            rev = _good_review()
            del rev["review_id"]
            _write(d, review=rev)
            self.assertTrue(any("missing" in f for f in check_run(d)))

    def test_bad_schema_version_fails(self):
        with tempfile.TemporaryDirectory() as t:
            d = Path(t)
            rev = _good_review()
            rev["schema_version"] = 2
            _write(d, review=rev)
            self.assertTrue(check_run(d))

    def test_run_id_mismatch_fails(self):
        with tempfile.TemporaryDirectory() as t:
            d = Path(t)
            _write(d, request=_good_request("run-a"), review=_good_review("run-b"))
            self.assertTrue(any("run_id mismatch" in f for f in check_run(d)))

    def test_continue_disagreeing_with_verdict_fails(self):
        with tempfile.TemporaryDirectory() as t:
            d = Path(t)
            _write(d, review=_good_review(verdict="approved", cont=True))
            self.assertTrue(any("continue=True disagrees" in f for f in check_run(d)))
        with tempfile.TemporaryDirectory() as t:
            d = Path(t)
            _write(d, review=_good_review(verdict="changes_requested", cont=False))
            self.assertTrue(any("changes_requested" in f for f in check_run(d)))

    def test_review_file_pointing_nowhere_fails(self):
        with tempfile.TemporaryDirectory() as t:
            d = Path(t)
            _write(d)
            _patch_review(d, review_file="ghost.md")
            self.assertTrue(any("ghost.md" in f for f in check_run(d)))

    def test_review_file_relative_escape_rejected(self):
        with tempfile.TemporaryDirectory() as t:
            root = Path(t)
            run_dir = root / "run"
            run_dir.mkdir()
            (root / "outside.md").write_text("escaped", encoding="utf-8")
            _write(run_dir)
            _patch_review(run_dir, review_file="../outside.md")
            self.assertTrue(any("escapes" in f for f in check_run(run_dir)))

    def test_review_file_absolute_escape_rejected(self):
        with tempfile.TemporaryDirectory() as t:
            root = Path(t)
            run_dir = root / "run"
            run_dir.mkdir()
            outside = root / "outside.md"
            outside.write_text("escaped", encoding="utf-8")
            _write(run_dir)
            _patch_review(run_dir, review_file=str(outside))
            self.assertTrue(any("escapes" in f for f in check_run(run_dir)))

    def test_integer_review_file_does_not_crash(self):
        with tempfile.TemporaryDirectory() as t:
            d = Path(t)
            _write(d)
            _patch_review(d, review_file=123)
            failures = check_run(d)
            self.assertIsInstance(failures, list)
            self.assertTrue(failures)

    def test_incomplete_run_json_fails(self):
        with tempfile.TemporaryDirectory() as t:
            d = Path(t)
            _write(d, run={"schema_version": 1, "run_id": "run-test"})
            self.assertTrue(any("run.json" in f for f in check_run(d)))

    def test_array_run_id_in_run_json_does_not_crash(self):
        with tempfile.TemporaryDirectory() as t:
            d = Path(t)
            run = _good_run()
            run["run_id"] = ["array", "id"]
            _write(d, run=run)
            failures = check_run(d)
            self.assertIsInstance(failures, list)
            self.assertTrue(any("run_id" in f for f in failures))

    def test_object_run_id_in_review_does_not_crash(self):
        with tempfile.TemporaryDirectory() as t:
            d = Path(t)
            _write(d)
            _patch_review(d, run_id={"nested": "object"})
            failures = check_run(d)
            self.assertIsInstance(failures, list)
            self.assertTrue(failures)

    def test_empty_review_markdown_fails(self):
        with tempfile.TemporaryDirectory() as t:
            d = Path(t)
            _write(d, review_text="")
            self.assertTrue(any("reviewer-verdict.md" in f for f in check_run(d)))


if __name__ == "__main__":
    unittest.main()
