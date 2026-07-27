from __future__ import annotations

import hashlib
import json
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, List, Optional, Tuple

from . import terminal_host
from .assignment import (
    MAX_TASK_FILE_BYTES,
    AssignmentError,
    assignment_input_sequence,
    wait_for_terminal_readiness,
)
from .liveness import probe_control_liveness
from .models import ReviewBackend, RunRecord, RunState
from .protocol import append_event, read_json, utc_now, write_json_atomic
from .registry import Registry


class TaskContinuityError(RuntimeError):
    pass


@dataclass(frozen=True)
class TaskAdoptionResult:
    run_id: str
    source_run_id: str
    task_path: Path
    latest_path: Path
    size: int
    sha256: str
    duplicate: bool


@dataclass(frozen=True)
class RecordedTaskPacket:
    path: Path
    data: bytes
    sha256: str


def _run_dir(run: RunRecord) -> Path:
    candidate = Path(run.run_dir)
    if not candidate.is_dir() or candidate.is_symlink():
        raise TaskContinuityError(
            "run directory is missing or unsafe for {}: {}".format(
                run.run_id, candidate
            )
        )
    return candidate.resolve()


def _recorded_digest(
    events_path: Path,
    latest_path: Path,
    run_id: str,
) -> Optional[str]:
    if not events_path.is_file() or events_path.is_symlink():
        return None
    recorded = None
    for line in events_path.read_text(
        encoding="utf-8", errors="replace"
    ).splitlines():
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if event.get("event") not in {"task_assigned", "task_adopted"}:
            continue
        if event.get("run_id") != run_id:
            continue
        recorded_path = event.get("latest_task_path")
        if not isinstance(recorded_path, str):
            continue
        if Path(recorded_path).resolve() != latest_path.resolve():
            continue
        digest = event.get("sha256")
        if isinstance(digest, str):
            recorded = digest
    return recorded


def load_recorded_task_packet(run: RunRecord) -> RecordedTaskPacket:
    run_dir = _run_dir(run)
    latest_path = run_dir / "assigned-task-latest.md"
    if latest_path.is_symlink() or not latest_path.is_file():
        raise TaskContinuityError(
            "source run {} has no safe assigned task packet".format(run.run_id)
        )
    resolved = latest_path.resolve(strict=True)
    if resolved.parent != run_dir or resolved != latest_path:
        raise TaskContinuityError(
            "source task packet escapes its run directory: {}".format(latest_path)
        )
    data = latest_path.read_bytes()
    if not data:
        raise TaskContinuityError("source task packet is empty")
    if len(data) > MAX_TASK_FILE_BYTES:
        raise TaskContinuityError(
            "source task packet exceeds {} bytes".format(MAX_TASK_FILE_BYTES)
        )
    digest = hashlib.sha256(data).hexdigest()
    recorded = _recorded_digest(
        run_dir / "events.jsonl",
        latest_path,
        run.run_id,
    )
    if recorded is None:
        raise TaskContinuityError(
            "source task packet has no authoritative assignment event"
        )
    if recorded != digest:
        raise TaskContinuityError(
            "source task packet digest changed: recorded {}, actual {}".format(
                recorded, digest
            )
        )
    return RecordedTaskPacket(path=latest_path, data=data, sha256=digest)


def _compatibility_mismatches(
    target: RunRecord, source: RunRecord
) -> List[str]:
    mismatches = []
    if target.project_slug != source.project_slug:
        mismatches.append("project_slug")
    if Path(target.workspace_root).resolve() != Path(source.workspace_root).resolve():
        mismatches.append("workspace_root")
    if target.agent != source.agent:
        mismatches.append("agent")
    if target.mode != source.mode:
        mismatches.append("mode")
    if target.reviewer_backend != source.reviewer_backend:
        mismatches.append("reviewer_backend")
    if target.reviewer_backend is ReviewBackend.VISIBLE_THREAD and (
        target.reviewer_thread_id != source.reviewer_thread_id
    ):
        mismatches.append("reviewer_thread_id")
    return mismatches


def _write_bytes_atomic(path: Path, data: bytes) -> None:
    temporary = path.with_name(".{}.tmp".format(path.name))
    temporary.write_bytes(data)
    os.chmod(temporary, 0o600)
    os.replace(str(temporary), str(path))


def _safe_timestamp() -> str:
    return utc_now().replace(":", "").replace("+", "Z").replace(".", "-")


def _existing_adoption(
    events_path: Path,
    *,
    source_run_id: str,
    digest: str,
) -> Optional[dict]:
    if not events_path.is_file() or events_path.is_symlink():
        return None
    for line in events_path.read_text(
        encoding="utf-8", errors="replace"
    ).splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if (
            event.get("event") == "task_adopted"
            and event.get("source_run_id") == source_run_id
            and event.get("sha256") == digest
        ):
            return event
    return None


def _adoption_was_delivered(
    events_path: Path,
    *,
    source_run_id: str,
    digest: str,
) -> bool:
    if not events_path.is_file() or events_path.is_symlink():
        return False
    for line in events_path.read_text(
        encoding="utf-8", errors="replace"
    ).splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if (
            event.get("event") == "task_adoption_delivered"
            and event.get("source_run_id") == source_run_id
            and event.get("sha256") == digest
        ):
            return True
    return False


def _delivered_adoption_steps(
    events_path: Path,
    *,
    source_run_id: str,
    digest: str,
) -> set[int]:
    delivered = set()
    if not events_path.is_file() or events_path.is_symlink():
        return delivered
    for line in events_path.read_text(
        encoding="utf-8", errors="replace"
    ).splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if (
            event.get("event") != "task_adoption_delivery_step"
            or event.get("source_run_id") != source_run_id
            or event.get("sha256") != digest
        ):
            continue
        index = event.get("step")
        if isinstance(index, int) and index >= 0:
            delivered.add(index)
    return delivered


def _deliver_adopted_task(
    target: RunRecord,
    packet: RecordedTaskPacket,
    *,
    source_run_id: str,
    events_path: Path,
    sender: Callable,
) -> bool:
    if _adoption_was_delivered(
        events_path,
        source_run_id=source_run_id,
        digest=packet.sha256,
    ):
        return False
    try:
        wait_for_terminal_readiness(
            target,
            sender,
            events_path=events_path,
        )
    except AssignmentError as error:
        raise TaskContinuityError(str(error)) from error
    try:
        task_text = packet.data.decode("utf-8")
    except UnicodeDecodeError as error:
        raise TaskContinuityError("source task packet must be valid UTF-8") from error
    delivered_steps = _delivered_adoption_steps(
        events_path,
        source_run_id=source_run_id,
        digest=packet.sha256,
    )
    for index, input_text in enumerate(assignment_input_sequence(task_text)):
        if index in delivered_steps:
            continue
        if index:
            time.sleep(0.35)
        try:
            response = sender(
                Path(target.socket_path),
                {
                    "token": target.control_token,
                    "action": "send",
                    "text": input_text,
                },
            )
        except Exception as error:
            raise TaskContinuityError(
                "task continuity delivery failed: {}".format(error)
            ) from error
        if not isinstance(response, dict) or response.get("status") != "ok":
            message = (
                response.get("message", "unknown")
                if isinstance(response, dict)
                else "invalid control response"
            )
            raise TaskContinuityError(
                "task continuity delivery failed: {}".format(message)
            )
        append_event(
            events_path,
            {
                "event": "task_adoption_delivery_step",
                "run_id": target.run_id,
                "source_run_id": source_run_id,
                "sha256": packet.sha256,
                "step": index,
            },
        )
    append_event(
        events_path,
        {
            "event": "task_adoption_delivered",
            "run_id": target.run_id,
            "source_run_id": source_run_id,
            "latest_task_path": str(_run_dir(target) / "assigned-task-latest.md"),
            "sha256": packet.sha256,
        },
    )
    return True


def _finish_adoption(registry: Registry, target: RunRecord, target_dir: Path) -> None:
    if target.state is RunState.NEEDS_HUMAN:
        registry.force_state(target.run_id, RunState.WORKER_CONTINUING)
    for blocked_path in (
        target_dir / "task-continuity-block.json",
        target_dir / "task-continuity-recovery.txt",
    ):
        try:
            blocked_path.unlink()
        except FileNotFoundError:
            pass


def adopt_task(
    registry: Registry,
    target_run_id: str,
    source_run_id: str,
    *,
    process_start_reader: Optional[Callable[[int], str]] = None,
    control_sender: Optional[Callable] = None,
    operator_action: str = "loopweave adopt-task",
) -> TaskAdoptionResult:
    if target_run_id == source_run_id:
        raise TaskContinuityError("source and target run must be different")
    target = registry.get_run(target_run_id)
    source = registry.get_run(source_run_id)
    if target.state not in {
        RunState.RUNNING,
        RunState.WORKER_CONTINUING,
        RunState.NEEDS_HUMAN,
    }:
        raise TaskContinuityError(
            "target run {} is not continuable in state {}".format(
                target_run_id, target.state.value
            )
        )
    mismatches = _compatibility_mismatches(target, source)
    if mismatches:
        raise TaskContinuityError(
            "run continuity mismatch: {}".format(", ".join(mismatches))
        )

    reader = (
        process_start_reader
        if process_start_reader is not None
        else terminal_host.default_process_identity_reader()
    )
    try:
        current_start = reader(target.agent_pid)
    except Exception as error:
        raise TaskContinuityError(
            "target managed Agent process is no longer available"
        ) from error
    if not registry.process_identity_matches(
        target.run_id, target.agent_pid, current_start
    ):
        raise TaskContinuityError("target managed Agent process identity changed")
    sender = (
        control_sender
        if control_sender is not None
        else terminal_host.default_control_sender()
    )
    probe = probe_control_liveness(target, sender=sender)
    if not probe.alive:
        raise TaskContinuityError(
            "target managed Agent control channel is not live: {}".format(
                probe.reason
            )
        )

    packet = load_recorded_task_packet(source)
    target_dir = _run_dir(target)
    latest_path = target_dir / "assigned-task-latest.md"
    events_path = target_dir / "events.jsonl"
    run_json_path = target_dir / "run.json"
    if not run_json_path.is_file() or run_json_path.is_symlink():
        raise TaskContinuityError("target run.json is missing or unsafe")
    run_payload = read_json(run_json_path)
    if latest_path.exists() or latest_path.is_symlink():
        if latest_path.is_symlink() or not latest_path.is_file():
            raise TaskContinuityError("target task packet path is unsafe")
        existing_digest = hashlib.sha256(latest_path.read_bytes()).hexdigest()
        if existing_digest != packet.sha256:
            raise TaskContinuityError(
                "target run already has a different assigned task"
            )
        existing_adoption = _existing_adoption(
            events_path,
            source_run_id=source_run_id,
            digest=packet.sha256,
        )
        if existing_adoption is None:
            raise TaskContinuityError(
                "target run already has a task that was not adopted from {}".format(
                    source_run_id
                )
            )
        _deliver_adopted_task(
            target,
            packet,
            source_run_id=source_run_id,
            events_path=events_path,
            sender=sender,
        )
        _finish_adoption(registry, target, target_dir)
        return TaskAdoptionResult(
            run_id=target_run_id,
            source_run_id=source_run_id,
            task_path=latest_path,
            latest_path=latest_path,
            size=len(packet.data),
            sha256=packet.sha256,
            duplicate=True,
        )

    existing_adoption = _existing_adoption(
        events_path,
        source_run_id=source_run_id,
        digest=packet.sha256,
    )
    if existing_adoption is not None:
        _write_bytes_atomic(latest_path, packet.data)
        _deliver_adopted_task(
            target,
            packet,
            source_run_id=source_run_id,
            events_path=events_path,
            sender=sender,
        )
        _finish_adoption(registry, target, target_dir)
        return TaskAdoptionResult(
            run_id=target_run_id,
            source_run_id=source_run_id,
            task_path=latest_path,
            latest_path=latest_path,
            size=len(packet.data),
            sha256=packet.sha256,
            duplicate=True,
        )

    history_path = target_dir / "adopted-task-{}.md".format(_safe_timestamp())
    _write_bytes_atomic(history_path, packet.data)

    continuity = {
        "source_run_id": source_run_id,
        "source_task_path": str(packet.path),
        "sha256": packet.sha256,
        "operator_action": operator_action,
        "adopted_at": utc_now(),
    }
    run_payload["task_continuity"] = continuity
    write_json_atomic(run_json_path, run_payload)

    append_event(
        events_path,
        {
            "event": "task_adopted",
            "run_id": target_run_id,
            "source_run_id": source_run_id,
            "source_task_path": str(packet.path),
            "assigned_task_path": str(history_path),
            "latest_task_path": str(latest_path),
            "size": len(packet.data),
            "sha256": packet.sha256,
            "operator_action": operator_action,
        },
    )
    _write_bytes_atomic(latest_path, packet.data)
    _deliver_adopted_task(
        target,
        packet,
        source_run_id=source_run_id,
        events_path=events_path,
        sender=sender,
    )
    _finish_adoption(registry, target, target_dir)
    return TaskAdoptionResult(
        run_id=target_run_id,
        source_run_id=source_run_id,
        task_path=history_path,
        latest_path=latest_path,
        size=len(packet.data),
        sha256=packet.sha256,
        duplicate=False,
    )


def compatible_task_sources(
    registry: Registry, target: RunRecord
) -> List[Tuple[RunRecord, RecordedTaskPacket]]:
    candidates = []
    for source in registry.list_runs():
        if source.run_id == target.run_id:
            continue
        if _compatibility_mismatches(target, source):
            continue
        try:
            packet = load_recorded_task_packet(source)
        except TaskContinuityError:
            continue
        candidates.append((source, packet))
    return candidates


def recovery_guidance(registry: Registry, target: RunRecord) -> str:
    candidates = compatible_task_sources(registry, target)
    if len(candidates) == 1:
        source = candidates[0][0]
        return (
            "loopweave adopt-task --run-id {} --from-run {}".format(
                target.run_id, source.run_id
            )
        )
    if candidates:
        run_ids = ", ".join(source.run_id for source, _packet in candidates)
        return (
            "multiple compatible source runs found ({}); choose one and run: "
            "loopweave adopt-task --run-id {} --from-run <source-run-id>"
        ).format(run_ids, target.run_id)
    return (
        "loopweave adopt-task --run-id {} --from-run <source-run-id>".format(
            target.run_id
        )
    )
