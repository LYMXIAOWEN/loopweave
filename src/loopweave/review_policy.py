from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any, Dict

from .models import RunMode
from .protocol import read_json, write_json_atomic


STAGNATION_LIMIT = 3


def blocker_fingerprint(review: Dict[str, Any], review_markdown: str) -> str:
    blocking_decisions = review.get("blocking_decisions")
    if isinstance(blocking_decisions, list) and blocking_decisions:
        source = "\n".join(
            str(decision).strip().lower()
            for decision in blocking_decisions
            if str(decision).strip()
        )
    else:
        source = str(review.get("summary", "")).strip().lower()
    normalized = " ".join(source.split())
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def apply_review_policy(
    mode: RunMode,
    review_round: int,
    review: Dict[str, Any],
    evidence_fingerprint: str,
    review_markdown: str,
    state_path: Path,
) -> Dict[str, Any]:
    resolved = dict(review)
    state_path = Path(state_path)

    if (
        mode is RunMode.DESIGN
        and review_round >= 2
        and resolved["verdict"] == "changes_requested"
    ):
        resolved["verdict"] = "needs_human"
        resolved["continue"] = False
        resolved["next_action"] = "await_human"
        resolved["summary"] = (
            "Design review did not converge within two rounds; "
            "human decisions are required."
        )
        return resolved

    if mode is not RunMode.DEVELOP:
        return resolved

    if resolved["verdict"] != "changes_requested":
        if state_path.exists():
            write_json_atomic(
                state_path,
                {
                    "blocker_fingerprint": "",
                    "evidence_fingerprint": "",
                    "stagnant_count": 0,
                },
            )
        return resolved

    current_blocker = blocker_fingerprint(resolved, review_markdown)
    previous = {}
    if state_path.exists():
        try:
            previous = read_json(state_path)
        except (OSError, ValueError):
            previous = {}
    unchanged = (
        previous.get("blocker_fingerprint") == current_blocker
        and previous.get("evidence_fingerprint") == evidence_fingerprint
    )
    stagnant_count = int(previous.get("stagnant_count", 0)) + 1 if unchanged else 1
    write_json_atomic(
        state_path,
        {
            "blocker_fingerprint": current_blocker,
            "evidence_fingerprint": evidence_fingerprint,
            "stagnant_count": stagnant_count,
        },
    )
    if stagnant_count >= STAGNATION_LIMIT:
        resolved["verdict"] = "needs_human"
        resolved["continue"] = False
        resolved["summary"] = (
            "The same development blocker remained unchanged across "
            "three consecutive reviews; human intervention is required."
        )
    return resolved
