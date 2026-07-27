from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional

from .models import ReviewBackend, RunMode, RunRecord, RunState
from .protocol import (
    append_event,
)
from .registry import Registry
from .submission import (
    submit_final,
    submit_needs_human,
    submit_stage,
)


@dataclass(frozen=True)
class TranscriptEvidence:
    files_changed: List[str]
    commands_run: List[str]
    command_results: List[str]


MAX_VISIBLE_SUMMARY_BYTES = 12 * 1024
TRUNCATED_SUFFIX = "\n[truncated]"


def _unique(values: Iterable[str]) -> List[str]:
    return list(dict.fromkeys(value for value in values if value))


def _hash_file(path: str) -> str:
    candidate = Path(path)
    try:
        if not candidate.is_file():
            return "missing"
        return hashlib.sha256(candidate.read_bytes()).hexdigest()
    except OSError:
        return "missing"


def evidence_fingerprint(evidence: TranscriptEvidence) -> str:
    payload = {
        "files": [
            {"path": path, "sha256": _hash_file(path)}
            for path in sorted(evidence.files_changed)
        ],
        "commands": sorted(evidence.commands_run),
        "command_results": sorted(evidence.command_results),
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _bound_evidence_for_submission(evidence: TranscriptEvidence) -> dict:
    """Adapter-side bounded/compacted conversion (ADR section 8). The Claude
    hook derives evidence from a transcript the worker cannot rewrite, so it
    must fit the shared submission limits itself - per-item byte cap, per-field
    count cap, and aggregate total cap - rather than handing raw transcript
    data to the rejecting validator. The full-evidence fingerprint (computed
    from the unbounded evidence) is retained separately for dedup, so bounding
    the submitted evidence does not weaken the identity of the turn."""
    from .submission import (
        MAX_EVIDENCE_ITEMS_PER_FIELD,
        MAX_EVIDENCE_ITEM_BYTES,
        MAX_EVIDENCE_TOTAL_BYTES,
    )

    candidate: dict = {
        "files_changed": [],
        "commands_run": [],
        "tests": [],
        "known_issues": [],
        "questions_for_reviewer": [],
    }
    for field in ("files_changed", "commands_run"):
        raw = list(getattr(evidence, field))[:MAX_EVIDENCE_ITEMS_PER_FIELD]
        for item in raw:
            encoded = item.encode("utf-8")
            if len(encoded) <= MAX_EVIDENCE_ITEM_BYTES:
                candidate[field].append(item)
            else:
                candidate[field].append(
                    encoded[:MAX_EVIDENCE_ITEM_BYTES].decode("utf-8", "ignore")
                )
    while (
        len(
            json.dumps(
                candidate,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        )
        > MAX_EVIDENCE_TOTAL_BYTES
    ):
        if candidate["commands_run"]:
            candidate["commands_run"].pop()
        elif candidate["files_changed"]:
            candidate["files_changed"].pop()
        else:
            break
    return candidate


def _is_human_message(record: Dict[str, Any]) -> bool:
    if record.get("type") != "user":
        return False
    content = (record.get("message") or {}).get("content")
    if isinstance(content, str):
        return True
    if not isinstance(content, list):
        return False
    return not any(
        isinstance(block, dict)
        and (block.get("type") == "tool_result" or "tool_use_id" in block)
        for block in content
    )


def _requests_human(message: str) -> bool:
    marker = "LOOPWEAVE_NEEDS_HUMAN"
    return any(
        line.strip() == marker or line.strip().startswith(marker + " ")
        for line in message.splitlines()
    )


def _completion_scope(
    message: str,
    default_scope: str = "stage",
) -> Optional[str]:
    markers = {line.strip() for line in message.splitlines()}
    if {"LOOPWEAVE_STAGE", "LOOPWEAVE_FINAL"}.issubset(markers):
        return None
    if "LOOPWEAVE_STAGE" in markers:
        return "stage"
    if "LOOPWEAVE_FINAL" in markers:
        return "final"
    return default_scope


def _has_completion_marker(message: str) -> bool:
    markers = {line.strip() for line in message.splitlines()}
    return bool({"LOOPWEAVE_STAGE", "LOOPWEAVE_FINAL"} & markers)


def _bound_review_text(text: str, max_bytes: int = MAX_VISIBLE_SUMMARY_BYTES) -> str:
    encoded = text.encode("utf-8")
    if len(encoded) <= max_bytes:
        return text
    suffix = TRUNCATED_SUFFIX.encode("utf-8")
    limit = max(0, max_bytes - len(suffix))
    return encoded[:limit].decode("utf-8", errors="ignore") + TRUNCATED_SUFFIX


def _stage_identity(message: str, completion_scope: str) -> tuple[str, str, str]:
    lines = [line.strip() for line in message.splitlines() if line.strip()]
    stage_id = "final" if completion_scope == "final" else "stage-unmarked"
    stage_title = (
        "Final completion"
        if completion_scope == "final"
        else "Unspecified stage"
    )
    body_lines = []
    for line in lines:
        if line in {"LOOPWEAVE_STAGE", "LOOPWEAVE_FINAL"}:
            continue
        if line.lower().startswith("stage "):
            head, _, tail = line.partition(":")
            parts = head.split(maxsplit=1)
            if len(parts) == 2 and parts[1].strip():
                stage_id = parts[1].strip()
            if tail.strip():
                stage_title = tail.strip()
            continue
        body_lines.append(line)
    work_summary = (
        " ".join(body_lines).strip()
        or "Worker submitted a visible review card."
    )
    return stage_id, stage_title, work_summary


def _visible_task_packet_path(run_dir: Path) -> Path:
    return Path(run_dir) / "assigned-task-latest.md"


def _visible_plan_path(run: RunRecord, run_dir: Path) -> Path:
    if run.project_root:
        candidate = Path(run.project_root) / "project.json"
        if candidate.exists():
            return candidate
    return Path(run_dir) / "run.json"


def extract_transcript_evidence(transcript_path: Path) -> TranscriptEvidence:
    path = Path(transcript_path)
    if not path.is_file():
        return TranscriptEvidence(
            files_changed=[], commands_run=[], command_results=[]
        )

    records = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(record, dict):
            records.append(record)

    current_turn_start = 0
    for index, record in enumerate(records):
        if _is_human_message(record):
            current_turn_start = index + 1

    commands = []
    command_results = []
    files = []
    bash_tools: Dict[str, str] = {}
    for record in records[current_turn_start:]:
        content = (record.get("message") or {}).get("content")
        if not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "tool_result":
                tool_use_id = block.get("tool_use_id")
                if isinstance(tool_use_id, str) and tool_use_id in bash_tools:
                    result_content = block.get("content")
                    if isinstance(result_content, str):
                        normalized_result = result_content
                    else:
                        normalized_result = json.dumps(
                            result_content,
                            ensure_ascii=False,
                            sort_keys=True,
                            separators=(",", ":"),
                        )
                    result_digest = hashlib.sha256(
                        normalized_result.encode("utf-8")
                    ).hexdigest()
                    command_results.append(
                        "{}\0{}".format(bash_tools[tool_use_id], result_digest)
                    )
                continue
            if block.get("type") != "tool_use":
                continue
            tool_name = str(block.get("name") or "")
            tool_input = block.get("input") or {}
            if not isinstance(tool_input, dict):
                continue
            if tool_name == "Bash":
                command = tool_input.get("command")
                if isinstance(command, str):
                    normalized_command = command.strip()
                    commands.append(normalized_command)
                    tool_id = block.get("id")
                    if isinstance(tool_id, str):
                        bash_tools[tool_id] = normalized_command
            elif tool_name in {"Edit", "Write", "NotebookEdit"}:
                file_path = tool_input.get("file_path")
                if isinstance(file_path, str):
                    files.append(file_path.strip())

    return TranscriptEvidence(
        files_changed=_unique(files),
        commands_run=_unique(commands),
        command_results=_unique(command_results),
    )


def _load_transcript_records(transcript_path: Path) -> List[Dict[str, Any]]:
    path = Path(transcript_path)
    if not path.is_file():
        return []
    records: List[Dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(record, dict):
            records.append(record)
    return records


def _current_turn_start(records: List[Dict[str, Any]]) -> int:
    current_turn_start = 0
    for index, record in enumerate(records):
        if _is_human_message(record):
            current_turn_start = index + 1
    return current_turn_start


def _assistant_text(record: Dict[str, Any]) -> str:
    record_type = record.get("type")
    if record_type not in {None, "assistant"}:
        return ""
    content = (record.get("message") or {}).get("content")
    if isinstance(content, str):
        return content.strip()
    if not isinstance(content, list):
        return ""
    parts: List[str] = []
    for block in content:
        if not isinstance(block, dict) or block.get("type") != "text":
            continue
        text = block.get("text")
        if isinstance(text, str) and text.strip():
            parts.append(text.strip())
    return "\n".join(parts).strip()


def extract_latest_assistant_message(transcript_path: Path) -> str:
    records = _load_transcript_records(transcript_path)
    if not records:
        return ""
    latest = ""
    for record in records[_current_turn_start(records) :]:
        text = _assistant_text(record)
        if text:
            latest = text
    return latest


def next_review_round(
    run: RunRecord,
    registry: Registry,
    run_dir: Path,
) -> Optional[int]:
    if run.mode is RunMode.DESIGN and run.review_loop >= 2:
        registry.force_state(run.run_id, RunState.NEEDS_HUMAN)
        append_event(
            Path(run_dir) / "events.jsonl",
            {
                "event": "design_round_limit_reached",
                "run_id": run.run_id,
                "review_loop": run.review_loop,
            },
        )
        return None
    return run.review_loop + 1


def handle_claude_stop(
    run_id: str,
    hook_payload: Dict[str, Any],
    registry: Registry,
    dispatch: Callable[[RunRecord], None],
    visible_waker: Optional[Callable[[RunRecord, Dict[str, Any]], object]] = None,
) -> str:
    run = registry.get_run(run_id)
    run_dir = Path(run.run_dir)
    run_dir.mkdir(parents=True, exist_ok=True)
    if run.state in {
        RunState.APPROVED,
        RunState.NEEDS_HUMAN,
        RunState.FAILED,
        RunState.STOPPED,
        RunState.ORPHANED,
    }:
        append_event(
            run_dir / "events.jsonl",
            {
                "event": "stop_hook_ignored_terminal",
                "run_id": run_id,
                "state": run.state.value,
            },
        )
        return "terminal"
    if run.state is RunState.OWNER_REVIEW_PENDING:
        append_event(
            run_dir / "events.jsonl",
            {
                "event": "stop_hook_ignored_owner_review_pending",
                "run_id": run_id,
                "state": run.state.value,
            },
        )
        return "owner_review_pending"
    if run.state is RunState.READY_FOR_REVIEW:
        if (run_dir / "last-stop.sha256").exists():
            return "duplicate"
        append_event(
            run_dir / "events.jsonl",
            {
                "event": "stop_hook_ignored_ready_for_review",
                "run_id": run_id,
                "state": run.state.value,
            },
        )
        return "ready_for_review"
    transcript_path = hook_payload.get("transcript_path")
    message = str(hook_payload.get("last_assistant_message") or "").strip()
    transcript_message = extract_latest_assistant_message(
        Path(str(transcript_path or ""))
    )
    if not message or (
        not _has_completion_marker(message)
        and _has_completion_marker(transcript_message)
    ):
        message = transcript_message
    if _requests_human(message):
        submit_needs_human(
            run_id,
            _bound_review_text(message) or "Worker requested human intervention.",
            registry=registry,
        )
        return "needs-human"
    completion_scope = (
        _completion_scope(message)
        if run.mode is RunMode.DEVELOP
        else "final"
    )
    if completion_scope is None:
        append_event(
            run_dir / "events.jsonl",
            {"event": "completion_scope_conflict", "run_id": run_id},
        )
        submit_needs_human(
            run_id,
            _bound_review_text(message) or "Conflicting completion scope markers.",
            registry=registry,
        )
        return "needs-human"
    evidence = extract_transcript_evidence(Path(str(transcript_path or "")))
    if (
        not evidence.files_changed
        and not evidence.commands_run
        and not _has_completion_marker(message)
    ):
        append_event(
            run_dir / "events.jsonl",
            {"event": "review_skipped_no_evidence", "run_id": run_id},
        )
        return "no-review"
    fingerprint = evidence_fingerprint(evidence)
    stop_identity = json.dumps(
        {
            "message": message,
            "completion_scope": completion_scope,
            "evidence_fingerprint": fingerprint,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    digest = hashlib.sha256(stop_identity.encode("utf-8")).hexdigest()
    digest_path = run_dir / "last-stop.sha256"
    if digest_path.exists() and digest_path.read_text(encoding="utf-8") == digest:
        return "duplicate"

    review_round = next_review_round(run, registry, run_dir)
    if review_round is None:
        return "design-round-limit"

    is_visible = run.reviewer_backend is ReviewBackend.VISIBLE_THREAD
    stage_id, stage_title, work_summary = _stage_identity(message, completion_scope)
    bounded_summary = _bound_review_text(work_summary)
    evidence_dict = _bound_evidence_for_submission(evidence)

    submit_fn = submit_final if completion_scope == "final" else submit_stage
    submit_fn(
        run_id,
        bounded_summary,
        evidence=evidence_dict,
        registry=registry,
        stage_id=stage_id,
        stage_title=stage_title,
        visible_waker=visible_waker if is_visible else None,
        _dispatch_fn=dispatch if not is_visible else None,
        _fingerprint=fingerprint,
    )
    digest_path.write_text(digest, encoding="utf-8")
    return "visible-review-pending" if is_visible else "dispatched"
