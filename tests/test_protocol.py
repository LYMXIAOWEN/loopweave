from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from loopweave.protocol import (
    ProtocolError,
    append_event,
    read_json,
    validate_review_request,
    validate_reviewer_verdict,
    write_json_atomic,
)


class ProtocolTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_atomic_json_round_trip(self) -> None:
        path = self.root / "payload.json"

        write_json_atomic(path, {"schema_version": 1, "value": "ok"})

        self.assertEqual(
            read_json(path), {"schema_version": 1, "value": "ok"}
        )
        self.assertEqual(list(self.root.glob("*.tmp")), [])

    def test_append_event_writes_json_lines(self) -> None:
        path = self.root / "events.jsonl"

        append_event(path, {"event": "created"})
        append_event(path, {"event": "running"})

        events = [
            json.loads(line)
            for line in path.read_text(encoding="utf-8").splitlines()
        ]
        self.assertEqual([event["event"] for event in events], ["created", "running"])
        self.assertTrue(all("timestamp" in event for event in events))

    def test_review_request_requires_matching_schema_and_fields(self) -> None:
        payload = {
            "schema_version": 1,
            "run_id": "run-1",
            "status": "ready_for_review",
            "task_summary": "Implement feature",
            "change_summary": "Added feature",
            "files_changed": [],
            "commands_run": [],
            "tests": [],
            "known_issues": [],
            "questions_for_reviewer": [],
        }

        self.assertEqual(validate_review_request(payload), payload)
        contextual = dict(
            payload,
            project_slug="example-project",
            project_root="/tmp/projects/example-project",
            workspace_root="/tmp/example",
            thread_cwd="/tmp/control",
        )
        self.assertEqual(validate_review_request(contextual), contextual)

        del payload["task_summary"]
        with self.assertRaises(ProtocolError):
            validate_review_request(payload)

    def test_review_request_validates_mode_round_and_evidence_fingerprint(self) -> None:
        payload = {
            "schema_version": 1,
            "run_id": "run-1",
            "status": "ready_for_review",
            "task_summary": "Review design",
            "change_summary": "Updated proposal",
            "files_changed": [],
            "commands_run": [],
            "tests": [],
            "known_issues": [],
            "questions_for_reviewer": [],
            "mode": "design",
            "review_round": 1,
            "evidence_fingerprint": "a" * 64,
        }

        self.assertEqual(validate_review_request(payload), payload)

        for key, value in (
            ("mode", "unknown"),
            ("review_round", 0),
            ("evidence_fingerprint", "not-a-digest"),
            ("completion_scope", "unknown"),
        ):
            invalid = dict(payload, **{key: value})
            with self.subTest(key=key), self.assertRaises(ProtocolError):
                validate_review_request(invalid)

        staged = dict(payload, completion_scope="stage")
        final = dict(payload, completion_scope="final")
        self.assertEqual(validate_review_request(staged), staged)
        self.assertEqual(validate_review_request(final), final)

    def test_reviewer_verdict_validates_verdict(self) -> None:
        payload = {
            "schema_version": 1,
            "run_id": "run-1",
            "review_id": "review-1",
            "verdict": "changes_requested",
            "summary": "Fix one issue.",
            "review_file": "reviewer-verdict.md",
            "continue": True,
        }

        self.assertEqual(validate_reviewer_verdict(payload), payload)

        payload["verdict"] = "maybe"
        with self.assertRaises(ProtocolError):
            validate_reviewer_verdict(payload)

    def test_design_review_validates_mode_specific_fields(self) -> None:
        payload = {
            "schema_version": 1,
            "run_id": "run-1",
            "review_id": "review-1",
            "verdict": "changes_requested",
            "summary": "Resolve ownership.",
            "review_file": "reviewer-verdict.md",
            "continue": True,
            "round": 1,
            "blocking_decisions": ["Choose the state owner."],
            "advisory_notes": [],
            "consensus_summary": "The file protocol remains shared.",
            "next_action": "revise_design",
        }

        self.assertEqual(validate_reviewer_verdict(payload), payload)

        invalid = dict(payload, blocking_decisions=[])
        with self.assertRaises(ProtocolError):
            validate_reviewer_verdict(invalid)


if __name__ == "__main__":
    unittest.main()
