from __future__ import annotations

import hashlib
import json
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Optional

from .models import RunRecord, RunState
from .protocol import append_event, utc_now
from . import terminal_host
from .runtime_config import RunPolicy, load_run_policy


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
    redelivered: bool = False


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


def wait_for_terminal_readiness(
    run: RunRecord,
    sender: Callable,
    *,
    events_path: Optional[Path] = None,
    policy: Optional[RunPolicy] = None,
) -> Dict[str, object]:
    selected_policy = policy if policy is not None else load_run_policy()
    started = time.monotonic()
    deadline = started + (selected_policy.task_ready_timeout_ms / 1000.0)
    fallback_after = (
        started + (selected_policy.task_ready_fallback_ms / 1000.0)
    )
    quiet_seconds = selected_policy.task_ready_quiet_ms / 1000.0
    reason = None
    response: Dict[str, object] = {}
    while time.monotonic() < deadline:
        try:
            raw_response = sender(
                Path(run.socket_path),
                {"token": run.control_token, "action": "status"},
            )
        except Exception as error:
            raise AssignmentError(
                "assignment delivery failed: terminal readiness probe failed: "
                "{}".format(error)
            ) from error
        if not isinstance(raw_response, dict) or raw_response.get("status") != "ok":
            message = (
                raw_response.get("message", "unknown")
                if isinstance(raw_response, dict)
                else "invalid control response"
            )
            raise AssignmentError(
                "assignment delivery failed: terminal readiness probe failed: "
                "{}".format(message)
            )
        response = raw_response
        response_run_id = response.get("run_id")
        response_pid = response.get("pid")
        response_running = response.get("running")
        if response_run_id is not None and response_run_id != run.run_id:
            raise AssignmentError(
                "assignment delivery failed: terminal readiness probe "
                "returned another run"
            )
        if response_pid is not None and response_pid != run.agent_pid:
            raise AssignmentError(
                "assignment delivery failed: terminal readiness probe "
                "returned another process"
            )
        if response_running is False:
            raise AssignmentError(
                "assignment delivery failed: managed Agent exited before "
                "terminal became ready"
            )
        output_bytes = response.get("terminal_output_bytes")
        idle_seconds = response.get("terminal_idle_seconds")
        if output_bytes is None and idle_seconds is None:
            reason = "legacy_control_status"
            break
        if (
            isinstance(output_bytes, int)
            and output_bytes > 0
            and isinstance(idle_seconds, (int, float))
            and float(idle_seconds) >= quiet_seconds
        ):
            reason = "terminal_output_quiet"
            break
        if time.monotonic() >= fallback_after:
            reason = (
                "terminal_output_fallback"
                if isinstance(output_bytes, int) and output_bytes > 0
                else "no_output_fallback"
            )
            break
        time.sleep(0.05)
    if reason is None:
        raise AssignmentError(
            "assignment delivery failed: managed Agent terminal did not become ready"
        )
    if events_path is not None:
        append_event(
            events_path,
            {
                "event": "terminal_readiness_observed",
                "run_id": run.run_id,
                "reason": reason,
                "elapsed_ms": int((time.monotonic() - started) * 1000),
                "terminal_output_bytes": response.get("terminal_output_bytes"),
                "terminal_idle_seconds": response.get("terminal_idle_seconds"),
            },
        )
    return response


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
    redeliver: bool = False,
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
        if existing_digest != packet.sha256:
            raise AssignmentError(
                "a different task is already assigned to run {}".format(run.run_id)
            )
        if not redeliver:
            return AssignmentResult(
                run_id=run.run_id,
                task_path=latest_path,
                latest_path=latest_path,
                size=packet.size,
                sha256=packet.sha256,
                duplicate=True,
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
    from .control_transport import control_endpoint_available

    if not control_endpoint_available(socket_path):
        raise AssignmentError(
            "control endpoint is unavailable: {}".format(socket_path)
        )

    events_path = run_dir / "events.jsonl"
    if redeliver:
        append_event(
            events_path,
            {
                "event": "assignment_redelivery_attempted",
                "run_id": run.run_id,
                "source_task_path": str(packet.path),
                "latest_task_path": str(latest_path),
                "size": packet.size,
                "sha256": packet.sha256,
            },
        )
        wait_for_terminal_readiness(
            run,
            _sender,
            events_path=events_path,
        )
        _send_assignment_sequence(run, packet.text, _sender)
        append_event(
            events_path,
            {
                "event": "task_redelivered",
                "run_id": run.run_id,
                "source_task_path": str(packet.path),
                "latest_task_path": str(latest_path),
                "size": packet.size,
                "sha256": packet.sha256,
            },
        )
        return AssignmentResult(
            run_id=run.run_id,
            task_path=latest_path,
            latest_path=latest_path,
            size=packet.size,
            sha256=packet.sha256,
            duplicate=True,
            redelivered=True,
        )

    timestamp = timestamp_factory()
    history_path = run_dir / "assigned-task-{}.md".format(timestamp)
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
        wait_for_terminal_readiness(
            run,
            _sender,
            events_path=events_path,
        )
        _send_assignment_sequence(run, packet.text, _sender)
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


def _send_assignment_sequence(
    run: RunRecord,
    task_text: str,
    sender: Callable,
) -> None:
    for index, input_text in enumerate(assignment_input_sequence(task_text)):
        if index:
            time.sleep(0.35)
        try:
            response = sender(
                Path(run.socket_path),
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
        if not isinstance(response, dict) or response.get("status") != "ok":
            message = (
                response.get("message", "unknown")
                if isinstance(response, dict)
                else "invalid control response"
            )
            raise AssignmentError(
                "assignment delivery failed: {}".format(message)
            )
