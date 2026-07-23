from __future__ import annotations

import hashlib
import json
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable, List, Optional

from .models import RunRecord, RunState
from .protocol import append_event, utc_now
from . import terminal_host


MAX_TASK_FILE_BYTES = 128 * 1024

ASSIGNABLE_STATES = {
    RunState.RUNNING,
    RunState.WORKER_CONTINUING,
}


class AssignmentError(RuntimeError):
    pass


@dataclass(frozen=True)
class TaskPacket:
    path: Path
    text: str
    size: int
    sha256: str


@dataclass(frozen=True)
class AssignmentResult:
    run_id: str
    task_path: Path
    latest_path: Path
    size: int
    sha256: str
    duplicate: bool


def validate_task_file(path: Path, max_bytes: int = MAX_TASK_FILE_BYTES) -> TaskPacket:
    task_path = Path(path).expanduser().resolve()
    if not task_path.exists():
        raise AssignmentError("task file does not exist: {}".format(task_path))
    if not task_path.is_file():
        raise AssignmentError("task file must be a regular file: {}".format(task_path))
    size = task_path.stat().st_size
    if size <= 0:
        raise AssignmentError("task file is empty: {}".format(task_path))
    if size > max_bytes:
        raise AssignmentError(
            "task file is too large: {} bytes exceeds {} bytes".format(size, max_bytes)
        )
    data = task_path.read_bytes()
    if not data:
        raise AssignmentError("task file is empty: {}".format(task_path))
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as error:
        raise AssignmentError("task file must be valid UTF-8") from error
    digest = hashlib.sha256(data).hexdigest()
    return TaskPacket(path=task_path, text=text, size=len(data), sha256=digest)


def resolve_latest_assignable_run(runs: Iterable[RunRecord]) -> RunRecord:
    candidates = [run for run in runs if run.state in ASSIGNABLE_STATES]
    if not candidates:
        raise AssignmentError("no assignable live run found")
    if len(candidates) > 1:
        run_ids = ", ".join(run.run_id for run in candidates)
        raise AssignmentError(
            "multiple assignable live runs found; use --run-id: {}".format(run_ids)
        )
    return candidates[0]


def require_assignable_state(run: RunRecord) -> None:
    if run.state not in ASSIGNABLE_STATES:
        raise AssignmentError(
            "run {} is not assignable in state {}".format(run.run_id, run.state.value)
        )


def format_assignment_message(task_text: str) -> str:
    body = task_text.rstrip()
    return (
        "\n[LoopWeave assignment]\n"
        "{}\n\n"
        "Begin executing this assignment in the authoritative workspace. "
        "Follow the completion markers and review rules in the packet.\n"
    ).format(body)


def assignment_input_sequence(task_text: str) -> List[str]:
    return [format_assignment_message(task_text), "\r"]


def _safe_timestamp() -> str:
    return utc_now().replace(":", "").replace("+", "Z").replace(".", "-")


def _write_bytes_atomic(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(".{}.tmp".format(path.name))
    temporary.write_bytes(data)
    os.replace(str(temporary), str(path))


def _digest_was_assigned(events_path: Path, digest: str) -> bool:
    if not events_path.exists():
        return False
    for line in events_path.read_text(encoding="utf-8", errors="replace").splitlines():
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if event.get("event") == "task_assigned" and event.get("sha256") == digest:
            return True
    return False


def assign_task(
    run: RunRecord,
    task_file: Path,
    *,
    registry=None,
    sender: Callable[[Path, dict], dict] = None,
    process_start_reader: Optional[Callable[[int], str]] = None,
    timestamp_factory: Callable[[], str] = _safe_timestamp,
    on_stale_run: Optional[Callable[[RunRecord], None]] = None,
) -> AssignmentResult:
    require_assignable_state(run)
    packet = validate_task_file(task_file)
    run_dir = Path(run.run_dir)
    if not run_dir.exists():
        run_dir.mkdir(parents=True, mode=0o700)
    latest_path = run_dir / "assigned-task-latest.md"
    # Idempotent same-task retry (no-op) / different-task conflict (ADR 0002 s5).
    if latest_path.exists():
        existing_digest = hashlib.sha256(latest_path.read_bytes()).hexdigest()
        if existing_digest == packet.sha256:
            return AssignmentResult(
                run_id=run.run_id,
                task_path=latest_path,
                latest_path=latest_path,
                size=packet.size,
                sha256=packet.sha256,
                duplicate=True,
            )
        raise AssignmentError(
            "a different task is already assigned to run {}".format(run.run_id)
        )
    socket_path = Path(run.socket_path)
    _sender = sender if sender is not None else terminal_host.default_control_sender()
    reader = process_start_reader if process_start_reader is not None else terminal_host.default_process_identity_reader()
    # Identity check: registry path uses the shared authenticated decision (one
    # audited orphan, no callback); the legacy on_stale_run path is mutually
    # exclusive and never audits, so one transition never produces two orphans.
    if registry is not None:
        from .liveness import authenticated_identity_check
        verdict = authenticated_identity_check(
            registry, run, reader=reader, sender=_sender, source="assign"
        )
        if verdict == "orphaned":
            raise AssignmentError("managed Agent process identity changed")
    else:
        try:
            current_start = reader(run.agent_pid)
        except Exception as error:
            if on_stale_run is not None:
                on_stale_run(run)
            raise AssignmentError("managed Agent process is no longer available") from error
        if current_start != run.agent_process_start:
            if on_stale_run is not None:
                on_stale_run(run)
            raise AssignmentError("managed Agent process identity changed")
    if not socket_path.exists():
        raise AssignmentError("control socket does not exist: {}".format(socket_path))

    timestamp = timestamp_factory()
    history_path = run_dir / "assigned-task-{}.md".format(timestamp)
    events_path = run_dir / "events.jsonl"
    data = packet.text.encode("utf-8")
    duplicate = _digest_was_assigned(events_path, packet.sha256)
    # Install the immutable run-scoped packet BEFORE delivery so a worker that
    # submits immediately upon receiving its assignment finds the exact packet
    # already on disk (deterministic startup ordering, ADR 0002 section 5).
    _write_bytes_atomic(history_path, data)
    _write_bytes_atomic(latest_path, data)
    append_event(
        events_path,
        {
            "event": "assignment_attempted",
            "run_id": run.run_id,
            "source_task_path": str(packet.path),
            "assigned_task_path": str(history_path),
            "size": packet.size,
            "sha256": packet.sha256,
            "duplicate": duplicate,
        },
    )

    try:
        for index, input_text in enumerate(assignment_input_sequence(packet.text)):
            if index:
                time.sleep(0.35)
            try:
                response = _sender(
                    socket_path,
                    {
                        "token": run.control_token,
                        "action": "send",
                        "text": input_text,
                    },
                )
            except Exception as error:
                raise AssignmentError(
                    "assignment delivery failed: {}".format(error)
                ) from error
            if response.get("status") != "ok":
                raise AssignmentError(
                    "assignment delivery failed: {}".format(
                        response.get("message", "unknown")
                    )
                )
    except Exception:
        # Roll back the partial packet so a failed delivery records no
        # assignment (history + assignment_attempted remain as an audit of the
        # attempt; latest/task_assigned do not).
        try:
            latest_path.unlink()
        except FileNotFoundError:
            pass
        raise

    append_event(
        events_path,
        {
            "event": "task_assigned",
            "run_id": run.run_id,
            "source_task_path": str(packet.path),
            "assigned_task_path": str(history_path),
            "latest_task_path": str(latest_path),
            "size": packet.size,
            "sha256": packet.sha256,
            "duplicate": duplicate,
        },
    )
    return AssignmentResult(
        run_id=run.run_id,
        task_path=history_path,
        latest_path=latest_path,
        size=packet.size,
        sha256=packet.sha256,
        duplicate=duplicate,
    )
