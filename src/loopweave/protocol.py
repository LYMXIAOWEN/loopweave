from __future__ import annotations

import json
import io
import os
import re
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable


class ProtocolError(ValueError):
    pass


REVIEW_REQUEST_FIELDS = {
    "schema_version",
    "run_id",
    "status",
    "task_summary",
    "change_summary",
    "files_changed",
    "commands_run",
    "tests",
    "known_issues",
    "questions_for_reviewer",
}

REVIEWER_VERDICT_FIELDS = {
    "schema_version",
    "run_id",
    "review_id",
    "verdict",
    "summary",
    "review_file",
    "continue",
}

REVIEWER_VERDICT_VERDICTS = {
    "approved",
    "changes_requested",
    "needs_human",
    "failed",
}

DESIGN_REVIEW_FIELDS = {
    "round",
    "blocking_decisions",
    "advisory_notes",
    "consensus_summary",
    "next_action",
}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def write_json_atomic(path: Path, payload: Dict[str, Any]) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_path = tempfile.mkstemp(
        prefix="." + path.name + ".", suffix=".tmp", dir=str(path.parent)
    )
    try:
        # Binary mode + TextIOWrapper: on Windows a text-mode fd would
        # translate LF to CRLF (harmless for JSON parsing but changes bytes);
        # binary mode keeps every platform byte-identical.
        with os.fdopen(descriptor, "wb") as raw_handle:
            with io.TextIOWrapper(
                raw_handle,
                encoding="utf-8",
                newline="\n",
            ) as handle:
                json.dump(
                    payload,
                    handle,
                    ensure_ascii=False,
                    indent=2,
                    sort_keys=True,
                )
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
        os.replace(temporary_path, path)
    finally:
        if os.path.exists(temporary_path):
            os.unlink(temporary_path)


def read_json(path: Path) -> Dict[str, Any]:
    with Path(path).open("r", encoding="utf-8") as handle:
        payload = json.load(handle)
    if not isinstance(payload, dict):
        raise ProtocolError("JSON payload must be an object")
    return payload


def append_event(path: Path, event: Dict[str, Any]) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = dict(event)
    payload.setdefault("timestamp", utc_now())
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, ensure_ascii=False, sort_keys=True))
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())


def _require_fields(payload: Dict[str, Any], fields: Iterable[str]) -> None:
    missing = sorted(set(fields) - set(payload))
    if missing:
        raise ProtocolError("missing required fields: {}".format(", ".join(missing)))
    if payload.get("schema_version") != 1:
        raise ProtocolError("unsupported schema_version")


def _require_non_empty_string(payload: Dict[str, Any], key: str) -> None:
    value = payload[key]
    if not isinstance(value, str) or not value:
        raise ProtocolError("{} must be a non-empty string".format(key))


def validate_review_request(payload: Dict[str, Any]) -> Dict[str, Any]:
    _require_fields(payload, REVIEW_REQUEST_FIELDS)
    _require_non_empty_string(payload, "run_id")
    if payload["status"] != "ready_for_review":
        raise ProtocolError("review request status must be ready_for_review")
    for key in (
        "files_changed",
        "commands_run",
        "tests",
        "known_issues",
        "questions_for_reviewer",
    ):
        if not isinstance(payload[key], list):
            raise ProtocolError("{} must be a list".format(key))
    for key in (
        "project_slug",
        "project_root",
        "workspace_root",
        "thread_cwd",
    ):
        if key in payload and payload[key] is not None:
            _require_non_empty_string(payload, key)
    if "mode" in payload and payload["mode"] not in {"develop", "design"}:
        raise ProtocolError("unsupported review mode")
    if "completion_scope" in payload and payload["completion_scope"] not in {
        "stage",
        "final",
    }:
        raise ProtocolError("unsupported completion_scope")
    if "review_round" in payload and (
        not isinstance(payload["review_round"], int)
        or isinstance(payload["review_round"], bool)
        or payload["review_round"] < 1
    ):
        raise ProtocolError("review_round must be a positive integer")
    if "evidence_fingerprint" in payload and (
        not isinstance(payload["evidence_fingerprint"], str)
        or re.fullmatch(r"[0-9a-f]{64}", payload["evidence_fingerprint"]) is None
    ):
        raise ProtocolError(
            "evidence_fingerprint must be a sha256 hex digest"
        )
    return payload


def validate_reviewer_verdict(payload: Dict[str, Any]) -> Dict[str, Any]:
    _require_fields(payload, REVIEWER_VERDICT_FIELDS)
    _require_non_empty_string(payload, "run_id")
    if payload["verdict"] not in REVIEWER_VERDICT_VERDICTS:
        raise ProtocolError("unsupported review verdict")
    if not isinstance(payload["continue"], bool):
        raise ProtocolError("continue must be boolean")
    if payload["continue"] and payload["verdict"] not in {
        "approved",
        "changes_requested",
    }:
        raise ProtocolError(
            "continue=true requires approved or changes_requested"
        )
    if payload["verdict"] == "changes_requested" and not payload["continue"]:
        raise ProtocolError("changes_requested requires continue=true")
    _require_non_empty_string(payload, "review_file")
    present_design_fields = DESIGN_REVIEW_FIELDS.intersection(payload)
    if present_design_fields:
        missing = DESIGN_REVIEW_FIELDS - set(payload)
        if missing:
            raise ProtocolError(
                "missing design review fields: {}".format(
                    ", ".join(sorted(missing))
                )
            )
        review_round = payload["round"]
        if (
            not isinstance(review_round, int)
            or isinstance(review_round, bool)
            or review_round not in {1, 2}
        ):
            raise ProtocolError("round must be 1 or 2")
        for key in ("blocking_decisions", "advisory_notes"):
            values = payload[key]
            if not isinstance(values, list) or not all(
                isinstance(value, str) and value.strip() for value in values
            ):
                raise ProtocolError(
                    "{} must be a list of non-empty strings".format(key)
                )
        _require_non_empty_string(payload, "consensus_summary")
        action = payload["next_action"]
        expected_actions = {
            "approved": "complete_design",
            "changes_requested": "revise_design",
            "needs_human": "await_human",
            "failed": "stop_failed",
        }
        if action != expected_actions[payload["verdict"]]:
            raise ProtocolError("design next_action does not match verdict")
        if payload["verdict"] == "approved" and payload["blocking_decisions"]:
            raise ProtocolError("approved design review cannot have blockers")
        if payload["verdict"] in {"changes_requested", "needs_human"} and not payload[
            "blocking_decisions"
        ]:
            raise ProtocolError(
                "{} design review requires blocking decisions".format(
                    payload["verdict"]
                )
            )
    return payload
