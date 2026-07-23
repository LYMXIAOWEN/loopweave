from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from loopweave.models import RunMode
from loopweave.review_policy import apply_review_policy


def _review(summary: str = "Resolve ownership.") -> dict:
    return {
        "schema_version": 1,
        "run_id": "run-1",
        "review_id": "review-1",
        "verdict": "changes_requested",
        "summary": summary,
        "review_file": "reviewer-verdict.md",
        "continue": True,
    }


class ReviewPolicyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.state_path = Path(self.temp_dir.name) / "review-policy-state.json"

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_round_two_changes_requested_becomes_needs_human(self) -> None:
        review = dict(
            _review(),
            round=2,
            blocking_decisions=["Choose the state owner."],
            advisory_notes=[],
            consensus_summary="The file protocol remains shared.",
            next_action="revise_design",
        )

        result = apply_review_policy(
            RunMode.DESIGN,
            review_round=2,
            review=review,
            evidence_fingerprint="a" * 64,
            review_markdown="# Review\n\nOwnership is unresolved.\n",
            state_path=self.state_path,
        )

        self.assertEqual(result["verdict"], "needs_human")
        self.assertFalse(result["continue"])
        self.assertEqual(result["next_action"], "await_human")
        self.assertEqual(
            result["blocking_decisions"], ["Choose the state owner."]
        )

    def test_develop_allows_more_than_three_improving_reviews(self) -> None:
        for index in range(4):
            result = apply_review_policy(
                RunMode.DEVELOP,
                review_round=index + 1,
                review=_review("Resolve ownership version {}.".format(index)),
                evidence_fingerprint="{:064x}".format(index + 1),
                review_markdown=(
                    "# Review\n\nOwnership version {} is unresolved.\n".format(
                        index
                    )
                ),
                state_path=self.state_path,
            )

            self.assertEqual(result["verdict"], "changes_requested")

    def test_develop_stops_after_three_unchanged_reviews(self) -> None:
        results = [
            apply_review_policy(
                RunMode.DEVELOP,
                review_round=index + 1,
                review=_review(),
                evidence_fingerprint="a" * 64,
                review_markdown="# Review\n\nOwnership is unresolved.\n",
                state_path=self.state_path,
            )
            for index in range(3)
        ]

        self.assertEqual(results[0]["verdict"], "changes_requested")
        self.assertEqual(results[1]["verdict"], "changes_requested")
        self.assertEqual(results[2]["verdict"], "needs_human")
        self.assertFalse(results[2]["continue"])

    def test_material_evidence_change_resets_stagnation(self) -> None:
        fingerprints = ["a" * 64, "a" * 64, "b" * 64, "b" * 64]
        results = [
            apply_review_policy(
                RunMode.DEVELOP,
                review_round=index + 1,
                review=_review(),
                evidence_fingerprint=fingerprint,
                review_markdown="# Review\n\nOwnership is unresolved.\n",
                state_path=self.state_path,
            )
            for index, fingerprint in enumerate(fingerprints)
        ]

        self.assertTrue(
            all(result["verdict"] == "changes_requested" for result in results)
        )

    def test_advisory_markdown_changes_do_not_reset_same_blocker(self) -> None:
        markdowns = [
            "# Review\n\nBlocking: ownership is unresolved.\n\nAdvisory: add diagram.",
            "# Review\n\nBlocking: ownership is unresolved.\n\nAdvisory: rename heading.",
            "# Review\n\nBlocking: ownership is unresolved.\n\nAdvisory: shorten intro.",
        ]
        results = [
            apply_review_policy(
                RunMode.DEVELOP,
                review_round=index + 1,
                review=_review(),
                evidence_fingerprint="a" * 64,
                review_markdown=markdown,
                state_path=self.state_path,
            )
            for index, markdown in enumerate(markdowns)
        ]

        self.assertEqual(results[-1]["verdict"], "needs_human")


if __name__ == "__main__":
    unittest.main()
