from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, Optional

from . import terminal_host
from .models import RunState
from .protocol import append_event, read_json, utc_now, write_json_atomic

_REASON_ENUM = frozenset(
    [
        "identity_mismatch",
        "control_unreachable",
        "control_unauthenticated",
        "pid_reused",
        "child_exited",
        "run_mismatch",
    ]
)
_MAX_SOURCE_BYTES = 64
_RECOVERABLE_PRIOR_STATES = {RunState.RUNNING, RunState.WORKER_CONTINUING}
_PROVENANCE_NAME = "orphan-provenance.json"


@dataclass(frozen=True)
class LivenessProbe:
    alive: bool
    run_id: Optional[str]
    pid: Optional[int]
    running: bool
    reason: str


def _bound_source(source: str) -> str:
    text = str(source)
    encoded = text.encode("utf-8")
    if len(encoded) <= _MAX_SOURCE_BYTES:
        return text
    return encoded[:_MAX_SOURCE_BYTES].decode("utf-8", errors="ignore")


def _provenance_path(run_dir: Path) -> Path:
    return Path(run_dir) / _PROVENANCE_NAME


def _write_orphan_provenance(run_dir: Path, payload: Dict[str, Any]) -> None:
    write_json_atomic(_provenance_path(run_dir), payload)


def _read_orphan_provenance(run_dir: Path) -> Optional[Dict[str, Any]]:
    path = _provenance_path(run_dir)
    if not path.exists():
        return None
    try:
        return read_json(path)
    except Exception:
        return None


def _clear_orphan_provenance(run_dir: Path) -> None:
    try:
        _provenance_path(run_dir).unlink()
    except FileNotFoundError:
        pass


def _iter_events(run_dir: Path):
    path = run_dir / "events.jsonl"
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if not line.strip():
            continue
        try:
            yield json.loads(line)
        except ValueError:
            continue


def _has_transition_event(
    run_dir: Path, event_type: str, run_id: str, transition_id: Optional[str]
) -> bool:
    if transition_id is None:
        return False
    for event in _iter_events(run_dir):
        if (
            event.get("event") == event_type
            and event.get("run_id") == run_id
            and event.get("transition_id") == transition_id
        ):
            return True
    return False


def _append_event_raw(run_dir: Path, payload: Dict[str, Any]) -> bool:
    """Append an event; return True on success, False on failure (not swallowed)."""
    try:
        append_event(run_dir / "events.jsonl", payload)
        return True
    except Exception:
        return False


def probe_control_liveness(
    run, *, sender: Callable = None, timeout: float = 2.0
) -> LivenessProbe:
    """Authenticated control-channel liveness probe (ADR 0002 section 2)."""
    _sender = sender if sender is not None else terminal_host.default_control_sender()
    try:
        response = _sender(
            Path(run.socket_path),
            {"token": run.control_token, "action": "status"},
            timeout,
        )
    except Exception:
        return LivenessProbe(False, None, None, False, "control_unreachable")
    if not isinstance(response, dict) or response.get("status") != "ok":
        return LivenessProbe(False, None, None, False, "control_unauthenticated")
    run_id = response.get("run_id")
    pid = response.get("pid")
    running = bool(response.get("running"))
    if run_id != run.run_id:
        return LivenessProbe(False, run_id, pid, running, "run_mismatch")
    if pid != run.agent_pid:
        return LivenessProbe(False, run_id, pid, running, "pid_reused")
    if not running:
        return LivenessProbe(False, run_id, pid, running, "child_exited")
    return LivenessProbe(True, run_id, pid, running, "alive")


def authenticated_identity_check(
    registry, run, *, reader: Callable, sender: Callable = None, source: str
) -> str:
    """Shared authenticated liveness decision (ADR 0002 section 2).

    Every orphan route consults this so a transient identity-read failure does
    not orphan a session the authenticated control channel proves alive.
    Returns 'verified' | 'transient' | 'orphaned':
      - reader matches recorded start -> 'verified';
      - reader raises -> probe; alive -> 'transient' (do NOT orphan); not alive
        -> audit_orphan, 'orphaned';
      - reader mismatch -> audit_orphan(identity_mismatch), 'orphaned'.
    """
    try:
        current = reader(run.agent_pid)
    except Exception:
        probe = probe_control_liveness(run, sender=sender)
        if probe.alive:
            _append_event_raw(
                Path(run.run_dir),
                {
                    "event": "liveness_probe_passed",
                    "run_id": run.run_id,
                    "source": _bound_source(source),
                    "reason": "identity_read_failed_control_alive",
                },
            )
            return "transient"
        audit_orphan(registry, run, source=source, reason_category=probe.reason)
        return "orphaned"
    if current != run.agent_process_start:
        audit_orphan(registry, run, source=source, reason_category="identity_mismatch")
        return "orphaned"
    return "verified"


def audit_orphan(registry, run, *, source: str, reason_category: str) -> None:
    """Centralized, audited orphan transition (ADR 0002 sections 1, 6, 7).

    prior_state is read from the authoritative registry row at transition time,
    not the caller's (possibly stale) snapshot. Durable provenance (carrying a
    transition_id) is written before the database change; the event is last.
    """
    if reason_category not in _REASON_ENUM:
        raise ValueError("unknown reason_category: {!r}".format(reason_category))
    # prior_state is derived AUTHORITATIVELY from the registry row at transition
    # time. If that read cannot be completed, the transition aborts rather than
    # forging recoverable provenance from a stale caller snapshot.
    try:
        prior_state = registry.get_run(run.run_id).state.value
    except Exception as error:
        raise RuntimeError(
            "cannot read authoritative run state for orphan transition"
        ) from error
    if prior_state not in RunState._value2member_map_:
        raise RuntimeError("authoritative run state is not a valid run state")
    bounded_source = _bound_source(source)
    transition_id = uuid.uuid4().hex
    run_dir = Path(run.run_dir)
    payload = {
        "prior_state": prior_state,
        "reason_category": reason_category,
        "source": bounded_source,
        "run_id": run.run_id,
        "transition_id": transition_id,
        "orphaned_at": utc_now(),
    }
    _write_orphan_provenance(run_dir, payload)
    registry.force_state(run.run_id, RunState.ORPHANED)
    _append_event_raw(
        run_dir,
        {
            "event": "run_orphaned",
            "source": bounded_source,
            "reason_category": reason_category,
            "prior_state": prior_state,
            "run_id": run.run_id,
            "transition_id": transition_id,
        },
    )


def reconcile_liveness(
    registry, run, *, reader: Callable = None, sender: Callable = None, source: str = "reconcile"
) -> str:
    """Hardened liveness reconcile for assignable runs (ADR 0002 section 2)."""
    if run.state not in _RECOVERABLE_PRIOR_STATES:
        return "skipped"
    _reader = reader if reader is not None else terminal_host.default_process_identity_reader()
    verdict = authenticated_identity_check(
        registry, run, reader=_reader, sender=sender, source=source
    )
    if verdict == "verified":
        return "alive"
    if verdict == "transient":
        return "alive_probe_passed"
    return "orphaned"


def _validate_provenance(provenance: Dict[str, Any], run) -> bool:
    """The provenance must identify THIS run and a real, well-formed transition
    before recovery may trust it. Mismatched/malformed provenance is rejected
    (the row stays orphaned and the evidence stays intact)."""
    if not isinstance(provenance, dict):
        return False
    if provenance.get("run_id") != run.run_id:
        return False
    transition_id = provenance.get("transition_id")
    if (
        not isinstance(transition_id, str)
        or not transition_id
        or len(transition_id) > 128
    ):
        return False
    if provenance.get("reason_category") not in _REASON_ENUM:
        return False
    if provenance.get("prior_state") not in RunState._value2member_map_:
        return False
    source = provenance.get("source")
    if not isinstance(source, str) or len(source.encode("utf-8")) > _MAX_SOURCE_BYTES:
        return False
    return True


def recover_orphaned(
    registry, run, *, reader: Callable = None, sender: Callable = None, source: str = "recover"
) -> str:
    """Idempotent, audited recovery (ADR 0002 sections 3, 4, 7).

    Correlates the orphan/recovery journal to the durable provenance by
    transition_id so retries and separate orphan cycles never duplicate or
    suppress records. Completes a restored run whose recovery journal is
    unfinished (lingering provenance).
    """
    run_dir = Path(run.run_dir)
    provenance = _read_orphan_provenance(run_dir)
    if provenance is None:
        return "no_provenance"
    if not _validate_provenance(provenance, run):
        # Refuse: leave the row orphaned and the evidence intact.
        return "invalid_provenance"
    transition_id = provenance["transition_id"]
    restored_state = RunState(provenance["prior_state"])

    # Backfill the orphan audit for THIS transition; if it cannot be made
    # durable, recovery must not proceed (no silent clearing).
    if not _has_transition_event(
        run_dir, "run_orphaned", run.run_id, transition_id
    ):
        if not _append_event_raw(
            run_dir,
            {
                "event": "run_orphaned",
                "source": _bound_source(provenance.get("source") or "backfill"),
                "reason_category": provenance.get("reason_category"),
                "prior_state": provenance.get("prior_state"),
                "run_id": run.run_id,
                "transition_id": transition_id,
            },
        ):
            return "backfill_failed"

    current = registry.get_run(run.run_id)
    if current.state is RunState.ORPHANED:
        if restored_state not in _RECOVERABLE_PRIOR_STATES:
            return "refused_prior_state"
        probe = probe_control_liveness(current, sender=sender)
        if not probe.alive:
            return "refused_not_alive"
        _reader = reader if reader is not None else terminal_host.default_process_identity_reader()
        try:
            current_start = _reader(current.agent_pid)
        except Exception:
            return "refused_identity"
        if current_start != current.agent_process_start:
            return "refused_identity"
        registry.force_state(run.run_id, restored_state)
    elif current.state is restored_state:
        pass
    else:
        return "refused_state"

    # Completion event for THIS transition (idempotent across retries).
    if not _has_transition_event(
        run_dir, "run_recovered", run.run_id, transition_id
    ):
        if not _append_event_raw(
            run_dir,
            {
                "event": "run_recovered",
                "source": _bound_source(source),
                "restored_state": restored_state.value,
                "run_id": run.run_id,
                "transition_id": transition_id,
            },
        ):
            return "recovered_audit_failed"
    # Clear only once the orphan + recovery records for this transition durably exist.
    _clear_orphan_provenance(run_dir)
    return "recovered"
