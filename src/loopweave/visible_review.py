from __future__ import annotations

import json
import os
import uuid
from pathlib import Path
from typing import Any, Dict, Iterable

from .protocol import (
    ProtocolError,
    utc_now,
    validate_reviewer_verdict,
    write_json_atomic,
)


MAX_REVIEW_CARD_BYTES = 32 * 1024
REVIEW_CARD_TRUNCATED = "[truncated: full evidence remains on disk]"
_COMPACTABLE_LIST_FIELDS = (
    "changed_files",
    "artifact_paths",
    "test_commands",
    "known_issues",
    "questions_for_reviewer",
    "not_completed_items",
    "completed_items",
    "worker_claims",
)
FORBIDDEN_WORKER_FIELDS = {
    "review_objective",
    "review_focus",
    "acceptance_criteria",
    "out_of_scope",
    "risk_areas",
    "grading",
    "rubric",
}


def _as_abs(path: Path) -> str:
    return str(Path(path).expanduser().resolve())


def _require_non_empty_string(payload: Dict[str, Any], key: str) -> None:
    value = payload.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ProtocolError("{} must be a non-empty string".format(key))


def _require_string_list(payload: Dict[str, Any], key: str) -> None:
    value = payload.get(key)
    if not isinstance(value, list) or not all(
        isinstance(item, str) for item in value
    ):
        raise ProtocolError("{} must be a list of strings".format(key))


def _inside_workspace(path: str, workspace_root: str) -> bool:
    candidate = Path(path).resolve()
    workspace = Path(workspace_root).resolve()
    return candidate == workspace or workspace in candidate.parents


def _encoded_size(payload: Dict[str, Any]) -> int:
    return len(
        json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    )


def _bound_utf8(text: str, max_bytes: int) -> str:
    encoded = text.encode("utf-8")
    if len(encoded) <= max_bytes:
        return text
    suffix = "\n" + REVIEW_CARD_TRUNCATED
    suffix_bytes = suffix.encode("utf-8")
    limit = max(0, max_bytes - len(suffix_bytes))
    return encoded[:limit].decode("utf-8", errors="ignore") + suffix


def _compact_review_card(card: Dict[str, Any]) -> Dict[str, Any]:
    """Fit a generated card to the protocol budget without worker intervention."""

    if _encoded_size(card) <= MAX_REVIEW_CARD_BYTES:
        return card

    compacted = dict(card)
    summary = str(compacted.get("work_summary") or "")
    for field in ("completed_items", "worker_claims"):
        if compacted.get(field) == [summary]:
            compacted[field] = []

    result_summary = str(compacted.get("test_result_summary") or "").strip()
    if REVIEW_CARD_TRUNCATED not in result_summary:
        compacted["test_result_summary"] = (
            result_summary + " " + REVIEW_CARD_TRUNCATED
        ).strip()

    if _encoded_size(compacted) <= MAX_REVIEW_CARD_BYTES:
        return compacted

    source_lists = {
        field: list(compacted.get(field) or []) for field in _COMPACTABLE_LIST_FIELDS
    }
    for field in _COMPACTABLE_LIST_FIELDS:
        compacted[field] = []
    requires_completed_item = bool(
        source_lists["changed_files"] or source_lists["artifact_paths"]
    )
    if requires_completed_item:
        completed = source_lists["completed_items"]
        compacted["completed_items"] = [
            completed.pop(0) if completed else "See work_summary."
        ]

    # Keep enough room for structured evidence even when the worker wrote a very
    # long summary. Byte-aware clipping preserves valid UTF-8.
    compacted["work_summary"] = _bound_utf8(summary, MAX_REVIEW_CARD_BYTES // 2)
    if _encoded_size(compacted) > MAX_REVIEW_CARD_BYTES:
        raise ProtocolError("review card fixed fields exceed protocol budget")

    # Add evidence round-robin so one long list cannot starve every other field.
    max_items = max((len(items) for items in source_lists.values()), default=0)
    full = False
    for index in range(max_items):
        for field in _COMPACTABLE_LIST_FIELDS:
            items = source_lists[field]
            if index >= len(items):
                continue
            compacted[field].append(items[index])
            if _encoded_size(compacted) > MAX_REVIEW_CARD_BYTES:
                compacted[field].pop()
                full = True
                break
        if full:
            break
    return compacted


def create_review_card(
    *,
    run_id: str,
    project_slug: str | None,
    stage_id: str,
    stage_title: str,
    completion_scope: str,
    workspace_root: Path,
    task_packet_path: Path,
    plan_path: Path,
    work_summary: str,
    completed_items: Iterable[str],
    not_completed_items: Iterable[str],
    changed_files: Iterable[Path],
    artifact_paths: Iterable[Path],
    test_commands: Iterable[str],
    test_result_summary: str,
    worker_claims: Iterable[str],
    known_issues: Iterable[str],
    questions_for_reviewer: Iterable[str],
) -> Dict[str, Any]:
    card = {
        "schema_version": 1,
        "run_id": run_id,
        "review_id": "review-request-" + uuid.uuid4().hex[:12],
        "reviewer_backend": "visible-thread",
        "project_slug": project_slug,
        "stage_id": stage_id,
        "stage_title": stage_title,
        "completion_scope": completion_scope,
        "workspace_root": _as_abs(workspace_root),
        "task_packet_path": _as_abs(task_packet_path),
        "plan_path": _as_abs(plan_path),
        "work_summary": work_summary.strip(),
        "completed_items": list(completed_items),
        "not_completed_items": list(not_completed_items),
        "changed_files": [_as_abs(path) for path in changed_files],
        "artifact_paths": [_as_abs(path) for path in artifact_paths],
        "test_commands": list(test_commands),
        "test_result_summary": test_result_summary.strip(),
        "worker_claims": list(worker_claims),
        "known_issues": list(known_issues),
        "questions_for_reviewer": list(questions_for_reviewer),
        "created_at": utc_now(),
    }
    # Validate the complete worker evidence before compaction. Otherwise a
    # dropped tail item could hide an invalid path or another protocol error.
    _validate_review_card(card, enforce_size=False)
    return validate_review_card(_compact_review_card(card))


def _validate_review_card(
    payload: Dict[str, Any], *, enforce_size: bool
) -> Dict[str, Any]:
    if not isinstance(payload, dict):
        raise ProtocolError("review card must be an object")
    forbidden = sorted(FORBIDDEN_WORKER_FIELDS.intersection(payload))
    if forbidden:
        raise ProtocolError(
            "worker card must not define review standards: {}".format(
                ", ".join(forbidden)
            )
        )
    if enforce_size and _encoded_size(payload) > MAX_REVIEW_CARD_BYTES:
        raise ProtocolError("review card exceeds {} bytes".format(MAX_REVIEW_CARD_BYTES))
    for key in (
        "run_id",
        "review_id",
        "reviewer_backend",
        "stage_id",
        "stage_title",
        "completion_scope",
        "workspace_root",
        "task_packet_path",
        "plan_path",
        "work_summary",
        "test_result_summary",
        "created_at",
    ):
        _require_non_empty_string(payload, key)
    if payload.get("schema_version") != 1:
        raise ProtocolError("unsupported review card schema_version")
    if payload["reviewer_backend"] != "visible-thread":
        raise ProtocolError("reviewer_backend must be visible-thread")
    if payload["completion_scope"] not in {"stage", "final"}:
        raise ProtocolError("completion_scope must be stage or final")
    for key in (
        "completed_items",
        "not_completed_items",
        "changed_files",
        "artifact_paths",
        "test_commands",
        "worker_claims",
        "known_issues",
        "questions_for_reviewer",
    ):
        _require_string_list(payload, key)
    if not payload["completed_items"] and (
        payload["changed_files"] or payload["artifact_paths"]
    ):
        raise ProtocolError(
            "completed_items cannot be empty when files or artifacts changed"
        )
    workspace_root = payload["workspace_root"]
    for changed in payload["changed_files"]:
        if not _inside_workspace(changed, workspace_root):
            raise ProtocolError("changed_files must stay inside workspace_root")
    for key in ("task_packet_path", "plan_path"):
        if not Path(payload[key]).exists():
            raise ProtocolError("{} does not exist".format(key))
    return payload


def validate_review_card(payload: Dict[str, Any]) -> Dict[str, Any]:
    return _validate_review_card(payload, enforce_size=True)


def queue_visible_review_card(run_dir: Path, card: Dict[str, Any]) -> Path:
    validated = validate_review_card(card)
    inbox = Path(run_dir) / "review-inbox"
    inbox.mkdir(parents=True, exist_ok=True)
    path = inbox / "{}.json".format(validated["review_id"])
    write_json_atomic(path, validated)
    (inbox / "pending").write_text(
        validated["review_id"] + "\n", encoding="utf-8"
    )
    return path


def latest_pending_card(run_dir: Path) -> Dict[str, Any]:
    inbox = Path(run_dir) / "review-inbox"
    pending_path = inbox / "pending"
    review_id = pending_path.read_text(encoding="utf-8").strip()
    card_path = inbox / "{}.json".format(review_id)
    return validate_review_card(json.loads(card_path.read_text(encoding="utf-8")))


def render_review_next_instruction(card: Dict[str, Any]) -> str:
    validated = validate_review_card(card)
    changed = (
        "\n".join("- {}".format(path) for path in validated["changed_files"])
        or "- None"
    )
    artifacts = (
        "\n".join("- {}".format(path) for path in validated["artifact_paths"])
        or "- None"
    )
    tests = (
        "\n".join("- {}".format(command) for command in validated["test_commands"])
        or "- None reported"
    )
    return (
        "Reviewer-owned review directive\n\n"
        "Run: {run_id}\n"
        "Review request: {review_id}\n"
        "Project: {project}\n"
        "Stage: {stage_id} - {stage_title}\n"
        "Scope: {scope}\n\n"
        "Authority:\n"
        "- Task packet: {task_packet}\n"
        "- Plan: {plan}\n"
        "- Workspace: {workspace}\n"
        "- Use the task packet and plan as authority. Do not accept "
        "worker-defined criteria.\n\n"
        "Worker summary: {summary}\n\n"
        "Changed files:\n{changed}\n\n"
        "Artifacts:\n{artifacts}\n\n"
        "Tests run:\n{tests}\n\n"
        "Test summary: {test_summary}\n\n"
        "Review the real workspace, then write a verdict file and submit it "
        "with:\n"
        "loopweave review-submit --run-id {run_id} "
        "--review-file /tmp/loopweave-visible-review.md\n"
    ).format(
        run_id=validated["run_id"],
        review_id=validated["review_id"],
        project=validated.get("project_slug") or "-",
        stage_id=validated["stage_id"],
        stage_title=validated["stage_title"],
        scope=validated["completion_scope"],
        task_packet=validated["task_packet_path"],
        plan=validated["plan_path"],
        workspace=validated["workspace_root"],
        summary=validated["work_summary"],
        changed=changed,
        artifacts=artifacts,
        tests=tests,
        test_summary=validated["test_result_summary"],
    )


def _parse_front_matter(text: str) -> tuple[Dict[str, str], str]:
    if not text.startswith("---\n"):
        raise ProtocolError("review file must start with front matter")
    parts = text.split("---", 2)
    if len(parts) != 3:
        raise ProtocolError("review file front matter is incomplete")
    metadata_text = parts[1]
    body = parts[2].strip()
    metadata: Dict[str, str] = {}
    for line in metadata_text.splitlines():
        if not line.strip():
            continue
        key, separator, value = line.partition(":")
        if not separator:
            raise ProtocolError("invalid review front matter line")
        metadata[key.strip()] = value.strip()
    return metadata, body


def _submitter_lock_owner_is_dead(lock: Path) -> bool:
    try:
        owner_pid = int(lock.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return False
    if owner_pid <= 0:
        return False
    from .terminal_host import pid_alive

    return not pid_alive(owner_pid)


def _acquire_submit_lock(inbox: Path) -> Path:
    inbox.mkdir(parents=True, exist_ok=True)
    lock = inbox / "pending.submit.lock"
    for attempt in range(2):
        try:
            flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
            flags |= getattr(os, "O_BINARY", 0)
            fd = os.open(lock, flags, 0o600)
        except FileExistsError as error:
            if attempt == 0 and _submitter_lock_owner_is_dead(lock):
                try:
                    lock.unlink()
                except FileNotFoundError:
                    pass
                continue
            raise ProtocolError("visible review is already being submitted") from error
        try:
            os.write(fd, str(os.getpid()).encode("ascii"))
        finally:
            os.close(fd)
        return lock
    raise ProtocolError("visible review is already being submitted")


def submit_visible_review(
    run_dir: Path,
    run_id: str,
    review_file: Path,
) -> Dict[str, Any]:
    run_dir = Path(run_dir)
    inbox = run_dir / "review-inbox"
    pending = inbox / "pending"
    if not pending.exists():
        raise ProtocolError("no pending visible review")
    lock = _acquire_submit_lock(inbox)
    try:
        text = Path(review_file).read_text(encoding="utf-8")
        metadata, body = _parse_front_matter(text)
        verdict = metadata.get("verdict", "")
        if not pending.exists():
            raise ProtocolError("no pending visible review")
        pending_review_id = pending.read_text(encoding="utf-8").strip()
        if not pending_review_id:
            raise ProtocolError("pending visible review is empty")
        card_path = inbox / "{}.json".format(pending_review_id)
        if not card_path.exists():
            raise ProtocolError("pending visible review card is missing")
        card = validate_review_card(
            json.loads(card_path.read_text(encoding="utf-8"))
        )
        if card["run_id"] != run_id:
            raise ProtocolError("pending visible review run_id mismatch")
        review = validate_reviewer_verdict(
            {
                "schema_version": 1,
                "run_id": run_id,
                "review_id": "review-" + uuid.uuid4().hex[:12],
                "verdict": verdict,
                "summary": metadata.get("summary", ""),
                "review_file": "reviewer-verdict.md",
                "continue": verdict == "changes_requested",
            }
        )
        if not body:
            raise ProtocolError("review body cannot be empty")
        (run_dir / "reviewer-verdict.md").write_text(body + "\n", encoding="utf-8")
        write_json_atomic(run_dir / "reviewer-verdict.json", review)
        pending.unlink()
        (inbox / "resolved").write_text(
            review["review_id"] + "\n",
            encoding="utf-8",
        )
        return review
    finally:
        try:
            lock.unlink()
        except FileNotFoundError:
            pass
