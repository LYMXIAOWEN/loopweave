from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from .models import TERMINAL_STATES, ReviewBackend, RunRecord, RunState


def _pending_review_id(run: RunRecord) -> Optional[str]:
    pending_path = Path(run.run_dir) / "review-inbox" / "pending"
    if not pending_path.exists():
        return None
    review_id = pending_path.read_text(encoding="utf-8").strip()
    return review_id or None


def visible_review_heartbeat_status(
    runs: Iterable[RunRecord],
) -> Dict[str, Any]:
    pending: List[Dict[str, Any]] = []
    for run in runs:
        if run.reviewer_backend is not ReviewBackend.VISIBLE_THREAD:
            continue
        if run.state in TERMINAL_STATES:
            continue
        if run.state in {RunState.NEEDS_HUMAN, RunState.FAILED, RunState.STOPPED}:
            continue
        review_id = _pending_review_id(run)
        if review_id is not None:
            # The queued card is the source of truth until the visible owner
            # task submits a verdict.
            pending.append(
                {
                    "run_id": run.run_id,
                    "review_id": review_id,
                    "run_dir": run.run_dir,
                    "reviewer_thread_id": run.reviewer_thread_id,
                }
            )
            continue

    if len(pending) > 1:
        return {
            "status": "ambiguous",
            "reason": "multiple pending visible reviews",
            "run_ids": [item["run_id"] for item in pending],
        }
    if pending:
        item = pending[0]
        return {
            "status": "pending",
            "run_id": item["run_id"],
            "review_id": item["review_id"],
            "run_dir": item["run_dir"],
            "reviewer_thread_id": item["reviewer_thread_id"],
            "command": "loopweave review-next --run-id {}".format(
                item["run_id"]
            ),
            "submit_command": (
                "loopweave review-submit --run-id {} "
                "--review-file <review-file>"
            ).format(item["run_id"]),
        }
    return {
        "status": "idle",
        "reason": "no pending visible review",
    }
