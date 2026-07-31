from __future__ import annotations

import hashlib
import os
import secrets
from pathlib import Path
from typing import Any, Iterable

from .bridge_protocol import (
    BridgeProtocol,
    BridgeProtocolError,
    BridgeStatus,
    LeaseUnavailable,
    PROTOCOL_VERSION,
)
from .desktop_ipc import DesktopIpcClient, DesktopIpcError
from .models import (
    BridgeBinding,
    ReviewBackend,
    RunRecord,
    TERMINAL_STATES,
)
from .protocol import append_event
from .registry import Registry
from . import terminal_host


class BridgeControlError(RuntimeError):
    pass


def build_visible_review_prompt(
    run: RunRecord,
    card: dict[str, Any],
    marker: str,
) -> str:
    return "\n".join(
        [
            marker,
            "source: loopweave",
            f"run_id: {run.run_id}",
            f"review_id: {card['review_id']}",
            f"scope: {card.get('completion_scope', 'stage')}",
            f"workspace: {run.workspace_root}",
            f"run_dir: {run.run_dir}",
            "",
            "This is an automated loop event, not a user chat message.",
            f"Run `loopweave review-next --run-id {run.run_id}`.",
            "Review the pending card, task packet, real workspace changes, "
            "and focused evidence.",
            "Write a concise verdict file, then run "
            "`loopweave review-submit "
            f"--run-id {run.run_id} --review-file <file>`.",
            "Do not create another reviewer or worker. Do not advance a later "
            "package when requesting changes.",
        ]
    )


class VisibleReviewDispatcher:
    """Deliver one queued card to the bound owner task through Desktop IPC."""

    def __init__(
        self,
        *,
        controller: "BridgeController",
        ipc_client: DesktopIpcClient | None = None,
        owner_id: str | None = None,
    ) -> None:
        self.controller = controller
        self.ipc_client = ipc_client or DesktopIpcClient()
        self.owner_id = owner_id or f"desktop-ipc-{os.getpid()}"

    def __call__(
        self,
        run: RunRecord,
        card: dict[str, Any],
    ) -> BridgeStatus:
        if run.reviewer_backend is not ReviewBackend.VISIBLE_THREAD:
            raise BridgeControlError("run does not use visible-thread review")
        if not run.reviewer_thread_id:
            raise BridgeControlError("visible reviewer task is not bound")
        binding = self.controller.preflight(run.reviewer_thread_id)
        if run.reviewer_generation != binding.generation:
            raise BridgeControlError("run has a stale reviewer generation")

        outcome = self.controller.protocol.wait(binding)
        if outcome.status == "ambiguous":
            self.controller.reconcile_stale_pending_reviews(outcome.run_ids)
            outcome = self.controller.protocol.wait(binding)
        if outcome.status != "queued" or outcome.review_id is None:
            return outcome
        review_id = card.get("review_id")
        if review_id != outcome.review_id:
            raise BridgeControlError("pending review card identity mismatch")

        try:
            lease = self.controller.protocol.lease(
                binding,
                outcome.review_id,
                owner_id=self.owner_id,
            )
            dispatching = self.controller.protocol.begin_dispatch(lease)
        except LeaseUnavailable:
            current = self.controller.protocol.current_lease(
                binding,
                outcome.review_id,
            )
            return self.controller.protocol.recover(current)

        prompt = build_visible_review_prompt(run, card, dispatching.marker)
        try:
            self.ipc_client.start_visible_turn(
                thread_id=dispatching.thread_id,
                prompt=prompt,
            )
        except DesktopIpcError as error:
            append_event(
                Path(run.run_dir) / "events.jsonl",
                {
                    "event": "visible_review_desktop_dispatch_failed",
                    "run_id": run.run_id,
                    "review_id": dispatching.review_id,
                    "attempt": dispatching.attempt,
                    "error": str(error)[:512],
                },
            )
            return BridgeStatus(
                "dispatching",
                (dispatching.run_id,),
                dispatching.review_id,
            )

        visible = self.controller.protocol.ack_visible(
            dispatching,
            dispatching.marker,
        )
        append_event(
            Path(run.run_dir) / "events.jsonl",
            {
                "event": "visible_review_desktop_turn_started",
                "run_id": run.run_id,
                "review_id": visible.review_id,
                "attempt": visible.attempt,
            },
        )
        return BridgeStatus("visible", (visible.run_id,), visible.review_id)


class BridgeController:
    def __init__(
        self,
        *,
        root: Path,
        registry: Registry,
        sessions_dir: Path,
    ) -> None:
        self.root = Path(root)
        self.registry = registry
        self.bridge_dir = self.root / "var" / "visible-review-bridge"
        self.protocol = BridgeProtocol(
            registry=registry,
            bridge_dir=self.bridge_dir,
            sessions_dir=sessions_dir,
        )
        self.nonce_path = self.bridge_dir / "binding.nonce"
        self.generation_path = self.bridge_dir / "generation"

    def reconcile_stale_pending_reviews(
        self,
        run_ids: Iterable[str],
    ) -> tuple[str, ...]:
        orphaned: list[str] = []
        for run_id in dict.fromkeys(run_ids):
            run = self.registry.get_run(run_id)
            if run.state in TERMINAL_STATES:
                continue
            pending = Path(run.run_dir) / "review-inbox" / "pending"
            if not pending.is_file():
                continue
            from .liveness import authenticated_identity_check
            verdict = authenticated_identity_check(
                self.registry,
                run,
                reader=terminal_host.default_process_identity_reader(),
                source="bridge",
            )
            if verdict in ("verified", "transient"):
                continue
            # verdict == "orphaned": audit_orphan already recorded the transition
            append_event(
                Path(run.run_dir) / "events.jsonl",
                {
                    "event": "stale_pending_review_orphaned",
                    "run_id": run.run_id,
                    "review_id": pending.read_text(encoding="utf-8").strip(),
                },
            )
            orphaned.append(run.run_id)
        return tuple(orphaned)

    def bind(
        self,
        *,
        thread_id: str,
        thread_cwd: str,
        run_id: str | None = None,
    ) -> BridgeBinding:
        previous_generation = self._last_generation()
        run_generation = 0
        if run_id is not None:
            run_generation = self.registry.get_run(run_id).reviewer_generation
        generation = max(previous_generation, run_generation) + 1
        nonce = secrets.token_bytes(32)
        binding = BridgeBinding(
            thread_id=thread_id,
            generation=generation,
            nonce_hash=hashlib.sha256(nonce).hexdigest(),
            protocol_version=PROTOCOL_VERSION,
        )
        if run_id is not None:
            self.registry.set_reviewer_binding(
                run_id,
                thread_id,
                thread_cwd,
                generation=generation,
            )
        self.protocol.bind(binding)
        self._write_secret(self.nonce_path, nonce)
        self._write_secret(
            self.generation_path,
            (str(generation) + "\n").encode("ascii"),
        )
        return binding

    def preflight(self, thread_id: str) -> BridgeBinding:
        try:
            binding = self.protocol.load_binding()
        except BridgeProtocolError as error:
            raise BridgeControlError("visible review bridge is not bound") from error
        if binding.thread_id != thread_id:
            raise BridgeControlError(
                "visible review bridge is bound to a different task"
            )
        if not self.nonce_path.is_file():
            raise BridgeControlError("visible review bridge credential is missing")
        nonce = self.nonce_path.read_bytes()
        if hashlib.sha256(nonce).hexdigest() != binding.nonce_hash:
            raise BridgeControlError("visible review bridge credential mismatch")
        return binding

    def unbind(self) -> None:
        generation = self._last_generation()
        for path in self.bridge_dir.glob("lease-*.json"):
            path.unlink(missing_ok=True)
        self.protocol.binding_path.unlink(missing_ok=True)
        self.nonce_path.unlink(missing_ok=True)
        self._write_secret(
            self.generation_path,
            (str(generation) + "\n").encode("ascii"),
        )

    def _last_generation(self) -> int:
        generations = [0]
        if self.generation_path.is_file():
            try:
                generations.append(
                    int(self.generation_path.read_text(encoding="utf-8").strip())
                )
            except ValueError as error:
                raise BridgeControlError("invalid bridge generation file") from error
        if self.protocol.binding_path.is_file():
            generations.append(self.protocol.load_binding().generation)
        return max(generations)

    @staticmethod
    def _write_secret(path: Path, value: bytes) -> None:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        path.parent.chmod(0o700)
        temporary = path.with_name("." + path.name + ".tmp")
        flags = os.O_CREAT | os.O_TRUNC | os.O_WRONLY
        flags |= getattr(os, "O_BINARY", 0)
        descriptor = os.open(
            temporary,
            flags,
            0o600,
        )
        try:
            os.write(descriptor, value)
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        os.replace(temporary, path)
        path.chmod(0o600)
