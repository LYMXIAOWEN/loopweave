from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Dict

from .models import ReviewBackend, RunState
from .protocol import (
    append_event,
    read_json,
    validate_review_request,
    write_json_atomic,
)
from .registry import Registry, RunNotFound
from . import terminal_host


MAX_SUMMARY_BYTES = 12 * 1024
MAX_MESSAGE_BYTES = 12 * 1024
MAX_EVIDENCE_ITEMS_PER_FIELD = 200
MAX_EVIDENCE_ITEM_BYTES = 4 * 1024
MAX_EVIDENCE_TOTAL_BYTES = 64 * 1024

_EVIDENCE_FIELDS = frozenset(
    ["files_changed", "commands_run", "tests", "known_issues", "questions_for_reviewer"]
)


class SubmissionError(RuntimeError):
    pass


class EvidenceTooLarge(SubmissionError):
    pass


def validate_bounded_text(text: str, max_bytes: int, field_name: str) -> None:
    if len(text.encode("utf-8")) > max_bytes:
        raise SubmissionError(
            "{} exceeds {} bytes".format(field_name, max_bytes)
        )


def validate_evidence(evidence: Dict[str, Any]) -> Dict[str, Any]:
    unknown = sorted(set(evidence) - _EVIDENCE_FIELDS)
    if unknown:
        raise SubmissionError(
            "unknown evidence fields: {}".format(", ".join(unknown))
        )
    for field in _EVIDENCE_FIELDS:
        items = evidence.get(field, [])
        if not isinstance(items, list):
            raise SubmissionError("{} must be a list".format(field))
        if len(items) > MAX_EVIDENCE_ITEMS_PER_FIELD:
            raise EvidenceTooLarge(
                "{} has {} items; max is {}".format(
                    field, len(items), MAX_EVIDENCE_ITEMS_PER_FIELD
                )
            )
        for item in items:
            if not isinstance(item, str):
                raise SubmissionError(
                    "{} items must be strings".format(field)
                )
            if len(item.encode("utf-8")) > MAX_EVIDENCE_ITEM_BYTES:
                raise EvidenceTooLarge(
                    "{} item exceeds {} bytes".format(field, MAX_EVIDENCE_ITEM_BYTES)
                )
    total = len(
        json.dumps(
            evidence, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
    )
    if total > MAX_EVIDENCE_TOTAL_BYTES:
        raise EvidenceTooLarge(
            "evidence total {} bytes exceeds {} bytes".format(
                total, MAX_EVIDENCE_TOTAL_BYTES
            )
        )
    return evidence


def load_evidence_file(path: Path) -> Dict[str, Any]:
    path = Path(path)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise SubmissionError(
            "could not read evidence file {}: {}".format(path, error)
        ) from error
    if not isinstance(raw, dict):
        raise SubmissionError(
            "evidence file must be a JSON object: {}".format(path)
        )
    return validate_evidence(raw)


def _registry() -> Registry:
    from .config import REGISTRY_PATH, ensure_runtime_dirs
    ensure_runtime_dirs()
    return Registry(REGISTRY_PATH)


def _dispatch(run) -> None:
    """Call the real dispatch pipeline. Patched in tests."""
    from .config import REGISTRY_PATH, ensure_runtime_dirs
    ensure_runtime_dirs()
    from .registry import Registry as _Reg
    from .cli import _dispatch_and_deliver
    _dispatch_and_deliver(_Reg(REGISTRY_PATH), run)


def _get_review_round(run, registry: Registry, run_dir: Path) -> int:
    from .hook_entry import next_review_round
    result = next_review_round(run, registry, run_dir)
    if result is None:
        raise SubmissionError(
            "run {} has reached the review round limit".format(run.run_id)
        )
    return result


def _evidence_fingerprint(evidence: Dict[str, Any]) -> str:
    payload = json.dumps(evidence, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _check_run_identity(run, registry: Registry) -> None:
    reader = terminal_host.default_process_identity_reader()
    try:
        current_start = reader(run.agent_pid)
    except Exception as error:
        raise SubmissionError(
            "managed Agent process is no longer available"
        ) from error
    if not registry.process_identity_matches(
        run.run_id, run.agent_pid, current_start
    ):
        raise SubmissionError("managed Agent process identity changed")


_SUBMITTABLE_STATES = {RunState.RUNNING, RunState.WORKER_CONTINUING}


def _assert_submittable(run, registry: Registry) -> None:
    """Shared guard for every submission scope (ADR section 3 / product
    invariant 7). A run must be in an assignable worker state (running or
    continuing) with a live, matching process identity before any submission
    may mutate it. A run already in READY_FOR_REVIEW has queued work awaiting
    human review - a second stage/final/needs-human submission must NOT
    overwrite its pending card, change its scope, or bypass the human stop
    point. Failed dispatch is retried by re-dispatching the existing request
    (state rolled back atomically by _submit_ephemeral), never by a new
    submission that would clobber the queued review."""
    if run.state not in _SUBMITTABLE_STATES:
        raise SubmissionError(
            "run {} cannot submit from state {}".format(
                run.run_id, run.state.value
            )
        )
    _check_run_identity(run, registry)


def submit_stage(
    run_id: str,
    summary: str,
    *,
    evidence: Dict[str, Any],
    registry: Registry = None,
    stage_id: str = None,
    stage_title: str = None,
    visible_waker=None,
    _dispatch_fn=None,
    _fingerprint: str = None,
) -> str:
    validate_bounded_text(summary, MAX_SUMMARY_BYTES, "summary")
    full_evidence = {field: [] for field in _EVIDENCE_FIELDS}
    full_evidence.update(evidence or {})
    validate_evidence(full_evidence)
    _reg = registry if registry is not None else _registry()
    try:
        run = _reg.get_run(run_id)
    except RunNotFound as error:
        raise SubmissionError("run not found: {}".format(run_id)) from error
    _assert_submittable(run, _reg)
    run_dir = Path(run.run_dir)
    run_dir.mkdir(parents=True, exist_ok=True)
    review_round = _get_review_round(run, _reg, run_dir)
    fingerprint = _fingerprint if _fingerprint is not None else _evidence_fingerprint(full_evidence)
    if run.reviewer_backend is ReviewBackend.VISIBLE_THREAD:
        _submit_visible(
            run_id=run_id,
            run=run,
            run_dir=run_dir,
            summary=summary,
            evidence=full_evidence,
            completion_scope="stage",
            review_round=review_round,
            registry=_reg,
            stage_id=stage_id,
            stage_title=stage_title,
            visible_waker=visible_waker,
        )
    else:
        _submit_ephemeral(
            run_id=run_id,
            run=run,
            run_dir=run_dir,
            summary=summary,
            evidence=full_evidence,
            completion_scope="stage",
            review_round=review_round,
            fingerprint=fingerprint,
            registry=_reg,
            dispatch_fn=_dispatch_fn,
        )
    _reg.increment_review_loop(run_id)
    return run_id


def submit_final(
    run_id: str,
    summary: str,
    *,
    evidence: Dict[str, Any],
    registry: Registry = None,
    stage_id: str = None,
    stage_title: str = None,
    visible_waker=None,
    _dispatch_fn=None,
    _fingerprint: str = None,
) -> str:
    validate_bounded_text(summary, MAX_SUMMARY_BYTES, "summary")
    full_evidence = {field: [] for field in _EVIDENCE_FIELDS}
    full_evidence.update(evidence or {})
    validate_evidence(full_evidence)
    _reg = registry if registry is not None else _registry()
    try:
        run = _reg.get_run(run_id)
    except RunNotFound as error:
        raise SubmissionError("run not found: {}".format(run_id)) from error
    _assert_submittable(run, _reg)
    run_dir = Path(run.run_dir)
    run_dir.mkdir(parents=True, exist_ok=True)
    review_round = _get_review_round(run, _reg, run_dir)
    fingerprint = _fingerprint if _fingerprint is not None else _evidence_fingerprint(full_evidence)
    if run.reviewer_backend is ReviewBackend.VISIBLE_THREAD:
        _submit_visible(
            run_id=run_id,
            run=run,
            run_dir=run_dir,
            summary=summary,
            evidence=full_evidence,
            completion_scope="final",
            review_round=review_round,
            registry=_reg,
            stage_id=stage_id,
            stage_title=stage_title,
            visible_waker=visible_waker,
        )
    else:
        _submit_ephemeral(
            run_id=run_id,
            run=run,
            run_dir=run_dir,
            summary=summary,
            evidence=full_evidence,
            completion_scope="final",
            review_round=review_round,
            fingerprint=fingerprint,
            registry=_reg,
            dispatch_fn=_dispatch_fn,
        )
    _reg.increment_review_loop(run_id)
    return run_id


def submit_needs_human(
    run_id: str,
    message: str,
    *,
    registry: Registry = None,
) -> str:
    validate_bounded_text(message, MAX_MESSAGE_BYTES, "message")
    _reg = registry if registry is not None else _registry()
    try:
        run = _reg.get_run(run_id)
    except RunNotFound as error:
        raise SubmissionError("run not found: {}".format(run_id)) from error
    _assert_submittable(run, _reg)
    _reg.force_state(run_id, RunState.NEEDS_HUMAN)
    run = _reg.get_run(run_id)
    append_event(
        Path(run.run_dir) / "events.jsonl",
        {"event": "worker_needs_human", "run_id": run_id, "source": "submit"},
    )
    return run_id


def _submit_ephemeral(
    *,
    run_id,
    run,
    run_dir,
    summary,
    evidence,
    completion_scope,
    review_round,
    fingerprint,
    registry,
    dispatch_fn=None,
) -> None:
    new_request = validate_review_request({
        "schema_version": 1,
        "run_id": run_id,
        "status": "ready_for_review",
        "task_summary": summary,
        "change_summary": summary,
        "files_changed": evidence.get("files_changed", []),
        "commands_run": evidence.get("commands_run", []),
        "tests": evidence.get("tests", []),
        "known_issues": evidence.get("known_issues", []),
        "questions_for_reviewer": evidence.get("questions_for_reviewer", []),
        "mode": run.mode.value,
        "review_round": review_round,
        "completion_scope": completion_scope,
        "evidence_fingerprint": fingerprint,
        "project_slug": run.project_slug,
        "project_root": run.project_root,
        "workspace_root": run.workspace_root,
        "thread_cwd": run.thread_cwd,
    })
    request_path = run_dir / "review-request.json"
    # Idempotent redispatch during re-review: if a request for this
    # same run and review round already exists on disk - left by a prior
    # dispatch that failed before advancing state - reuse its identity and
    # payload verbatim instead of overwriting. A different review_round means
    # the worker advanced to a new turn, which legitimately writes a new
    # request.
    should_write = True
    if request_path.exists():
        try:
            existing = read_json(request_path)
            if (
                existing.get("run_id") == run_id
                and existing.get("review_round") == new_request["review_round"]
            ):
                validate_review_request(existing)
                should_write = False
        except Exception:
            pass
    if should_write:
        write_json_atomic(request_path, new_request)
    entry_state = run.state
    registry.force_state(run_id, RunState.READY_FOR_REVIEW)
    append_event(
        run_dir / "events.jsonl",
        {"event": "review_requested", "run_id": run_id, "source": "submit"},
    )
    runner = dispatch_fn if dispatch_fn is not None else _dispatch
    try:
        runner(registry.get_run(run_id))
    except Exception:
        # Roll back ONLY when dispatch never advanced past the state this
        # function set. The real dispatcher (_dispatch_and_deliver) runs the
        # review, parses the verdict, and delivers it - on failure it may have
        # already reached REVIEWING, REVIEW_READY, or marked a dead worker
        # ORPHANED. Those are authoritative downstream states that must stand:
        # rewinding them would resurrect a dead worker or rewind an in-flight
        # review. Only a failure that left the run still in READY_FOR_REVIEW
        # (dispatch aborted before its first state advance) is safe to roll
        # back so the existing request can be re-dispatched from an assignable
        # state.
        current = registry.get_run(run_id).state
        if current is RunState.READY_FOR_REVIEW:
            registry.force_state(run_id, entry_state)
        raise


def _submit_visible(
    *,
    run_id,
    run,
    run_dir,
    summary,
    evidence,
    completion_scope,
    review_round,
    registry,
    stage_id=None,
    stage_title=None,
    visible_waker=None,
) -> None:
    from .hook_entry import _visible_plan_path, _visible_task_packet_path
    from .visible_review import create_review_card, queue_visible_review_card
    task_packet = _visible_task_packet_path(run_dir)
    if not task_packet.exists():
        from .task_continuity import recovery_guidance

        raise SubmissionError(
            "run {} has no task assignment; recover a verified prior packet with "
            "`{guidance}`, assign a new one with "
            "`loopweave assign --run-id {rid} --task-file <path>` or relaunch "
            "with `loopweave run <agent> --task-file <path>`".format(
                run_id,
                rid=run_id,
                guidance=recovery_guidance(registry, run),
            )
        )
    card = create_review_card(
        run_id=run_id,
        project_slug=run.project_slug,
        stage_id=stage_id if stage_id is not None else completion_scope,
        stage_title=stage_title if stage_title is not None else (summary[:80] if summary else completion_scope),
        completion_scope=completion_scope,
        workspace_root=Path(run.workspace_root),
        task_packet_path=_visible_task_packet_path(run_dir),
        plan_path=_visible_plan_path(run, run_dir),
        work_summary=summary,
        completed_items=[summary] if summary else ["See work_summary."],
        not_completed_items=[],
        changed_files=[Path(p) for p in evidence.get("files_changed", [])],
        artifact_paths=[],
        test_commands=evidence.get("commands_run", []),
        test_result_summary="See worker summary; detailed logs remain on disk.",
        worker_claims=[],
        known_issues=evidence.get("known_issues", []),
        questions_for_reviewer=evidence.get("questions_for_reviewer", []),
    )
    queue_visible_review_card(run_dir, card)
    registry.force_state(run_id, RunState.READY_FOR_REVIEW)
    append_event(
        run_dir / "events.jsonl",
        {
            "event": "visible_review_card_queued",
            "run_id": run_id,
            "review_id": card["review_id"],
            "review_round": review_round,
            "source": "submit",
        },
    )
    if visible_waker is not None:
        try:
            outcome = visible_waker(registry.get_run(run_id), card)
        except Exception as error:
            append_event(
                run_dir / "events.jsonl",
                {
                    "event": "visible_review_dispatch_failed",
                    "run_id": run_id,
                    "review_id": card["review_id"],
                    "error_type": type(error).__name__,
                    "error": str(error)[:512],
                    "source": "submit",
                },
            )
            outcome = "queued_after_dispatch_failure"
        append_event(
            run_dir / "events.jsonl",
            {
                "event": "visible_review_dispatch_attempted",
                "run_id": run_id,
                "review_id": card["review_id"],
                "outcome": str(getattr(outcome, "value", outcome)),
                "source": "submit",
            },
        )
