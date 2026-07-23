from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

from .codex_sessions import discover_thread
from .dispatcher import DispatchLeaseState, inspect_dispatch_lease
from .models import RunRecord, RunState
from .protocol import append_event, read_json, write_json_atomic
from .registry import Registry
from . import terminal_host


class AttachError(RuntimeError):
    pass


class WorkerUnavailable(AttachError):
    pass


class RunNotAttachable(AttachError):
    pass


class ThreadCwdMismatch(AttachError):
    pass


@dataclass(frozen=True)
class AttachResult:
    status: str
    run_id: str
    old_thread_id: str
    new_thread_id: str
    binding_generation: int


DISALLOWED_ATTACH_STATES = {
    RunState.APPROVED,
    RunState.FAILED,
    RunState.STOPPED,
    RunState.ORPHANED,
}

REVIEW_IN_FLIGHT_STATES = {
    RunState.REVIEWING,
    RunState.REVIEW_READY,
    RunState.DELIVERING,
}


def refresh_run_snapshot(run: RunRecord) -> None:
    path = Path(run.run_dir) / "run.json"
    payload = read_json(path)
    payload.update(
        {
            "codex_thread_id": run.codex_thread_id,
            "pending_codex_thread_id": run.pending_codex_thread_id,
            "binding_generation": run.binding_generation,
            "thread_cwd": run.thread_cwd,
            "workspace_root": run.workspace_root,
            "project_slug": run.project_slug,
            "project_root": run.project_root,
        }
    )
    write_json_atomic(path, payload)


class ThreadTakeoverCoordinator:
    def __init__(
        self,
        registry: Registry,
        sessions_dir: Path,
        process_start: Callable[[int], str] = None,
        lease_inspector: Callable[[Path], DispatchLeaseState] = inspect_dispatch_lease,
    ) -> None:
        self.registry = registry
        self.sessions_dir = Path(sessions_dir)
        self.process_start = process_start if process_start is not None else terminal_host.default_process_identity_reader()
        self.lease_inspector = lease_inspector

    def attach(
        self, run_id: str, explicit_thread_id: Optional[str] = None
    ) -> AttachResult:
        run = self.registry.get_run(run_id)
        if run.state in DISALLOWED_ATTACH_STATES:
            raise RunNotAttachable(
                "run {} is {}".format(run_id, run.state.value)
            )
        from .liveness import authenticated_identity_check
        verdict = authenticated_identity_check(
            self.registry, run, reader=self.process_start, source="attach"
        )
        if verdict == "orphaned":
            raise WorkerUnavailable("managed Agent process identity changed")

        thread = discover_thread(
            self.sessions_dir,
            cwd=run.thread_cwd,
            explicit_thread_id=explicit_thread_id,
        )
        if Path(thread.cwd).resolve() != Path(run.thread_cwd).resolve():
            raise ThreadCwdMismatch(
                "Codex thread working directory does not match the run"
            )
        if thread.thread_id == run.codex_thread_id:
            return AttachResult(
                "unchanged",
                run_id,
                run.codex_thread_id,
                run.codex_thread_id,
                run.binding_generation,
            )

        lease_state = self.lease_inspector(Path(run.run_dir))
        if lease_state is DispatchLeaseState.STALE:
            lock_path = Path(run.run_dir) / "dispatch.lock"
            try:
                lock_path.unlink()
            except FileNotFoundError:
                pass
            append_event(
                Path(run.run_dir) / "events.jsonl",
                {"event": "stale_dispatch_lease_removed"},
            )

        if (
            run.state in REVIEW_IN_FLIGHT_STATES
            or lease_state is DispatchLeaseState.LIVE
        ):
            queued = self.registry.queue_thread_attach(
                run_id, thread.thread_id
            )
            refresh_run_snapshot(queued)
            append_event(
                Path(run.run_dir) / "events.jsonl",
                {
                    "event": "thread_attach_queued",
                    "old_thread_id": run.codex_thread_id,
                    "new_thread_id": thread.thread_id,
                    "binding_generation": run.binding_generation + 1,
                },
            )
            return AttachResult(
                "queued",
                run_id,
                run.codex_thread_id,
                thread.thread_id,
                run.binding_generation + 1,
            )

        updated = self.registry.attach_thread_now(
            run_id, thread.thread_id, reason="context_exhausted"
        )
        refresh_run_snapshot(updated)
        append_event(
            Path(run.run_dir) / "events.jsonl",
            {
                "event": "thread_attached",
                "old_thread_id": run.codex_thread_id,
                "new_thread_id": updated.codex_thread_id,
                "binding_generation": updated.binding_generation,
            },
        )
        return AttachResult(
            "attached",
            run_id,
            run.codex_thread_id,
            updated.codex_thread_id,
            updated.binding_generation,
        )

    def reconcile_pending(self, run_id: str) -> Optional[AttachResult]:
        run = self.registry.get_run(run_id)
        target = run.pending_codex_thread_id
        if target is None:
            return None
        if run.state in DISALLOWED_ATTACH_STATES:
            self.registry.cancel_pending_thread_attach(run_id)
            refreshed = self.registry.get_run(run_id)
            refresh_run_snapshot(refreshed)
            append_event(
                Path(run.run_dir) / "events.jsonl",
                {
                    "event": "thread_attach_cancelled_terminal",
                    "target_thread_id": target,
                    "state": run.state.value,
                },
            )
            return AttachResult(
                "cancelled_terminal",
                run_id,
                run.codex_thread_id,
                target,
                run.binding_generation,
            )
        if (
            run.state in REVIEW_IN_FLIGHT_STATES
            or self.lease_inspector(Path(run.run_dir))
            is DispatchLeaseState.LIVE
        ):
            return None
        updated = self.registry.apply_pending_thread_attach(
            run_id, reason="context_exhausted"
        )
        if updated is None:
            return None
        refresh_run_snapshot(updated)
        append_event(
            Path(updated.run_dir) / "events.jsonl",
            {
                "event": "thread_attached",
                "old_thread_id": run.codex_thread_id,
                "new_thread_id": updated.codex_thread_id,
                "binding_generation": updated.binding_generation,
            },
        )
        return AttachResult(
            "attached",
            run_id,
            run.codex_thread_id,
            updated.codex_thread_id,
            updated.binding_generation,
        )

    def reconcile_run(self, run_id: str) -> Optional[AttachResult]:
        run = self.registry.get_run(run_id)
        lease_state = self.lease_inspector(Path(run.run_dir))
        if lease_state is DispatchLeaseState.STALE:
            lock_path = Path(run.run_dir) / "dispatch.lock"
            try:
                lock_path.unlink()
            except FileNotFoundError:
                pass
            append_event(
                Path(run.run_dir) / "events.jsonl",
                {"event": "stale_dispatch_lease_removed"},
            )
        if (
            lease_state is DispatchLeaseState.LIVE
            and run.state not in DISALLOWED_ATTACH_STATES
        ):
            refresh_run_snapshot(run)
            return None

        result = self.reconcile_pending(run_id)
        refresh_run_snapshot(self.registry.get_run(run_id))
        return result
