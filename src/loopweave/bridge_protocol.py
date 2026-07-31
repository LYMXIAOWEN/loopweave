from __future__ import annotations

import hashlib
import json
import re
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterator, Protocol

from .codex_sessions import list_threads
from .models import BridgeBinding, ReviewBackend, RunRecord, TERMINAL_STATES
from .protocol import read_json, write_json_atomic


PROTOCOL_VERSION = 1
_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")
_THREAD_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
)
_NONCE_HASH_RE = re.compile(r"^[0-9a-f]{64}$")
_FINAL_STATES = {"visible", "reviewing", "resolved"}


class BridgeProtocolError(RuntimeError):
    pass


class LeaseUnavailable(BridgeProtocolError):
    pass


class RunRegistry(Protocol):
    def list_runs(self) -> list[RunRecord]: ...


@dataclass(frozen=True)
class BridgeStatus:
    status: str
    run_ids: tuple[str, ...] = ()
    review_id: str | None = None


@dataclass(frozen=True)
class DispatchLease:
    run_id: str
    review_id: str
    thread_id: str
    generation: int
    owner_id: str
    state: str
    attempt: int
    issued_at: str
    expires_at: str
    marker: str
    idempotency_key: str


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _parse_time(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _validate_binding(binding: BridgeBinding) -> None:
    if not _THREAD_RE.fullmatch(binding.thread_id):
        raise BridgeProtocolError("invalid reviewer thread id")
    if binding.generation < 1:
        raise BridgeProtocolError("invalid reviewer generation")
    if not _NONCE_HASH_RE.fullmatch(binding.nonce_hash):
        raise BridgeProtocolError("invalid binding nonce hash")
    if binding.protocol_version != PROTOCOL_VERSION:
        raise BridgeProtocolError("unsupported bridge protocol version")


def _binding_payload(binding: BridgeBinding) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "thread_id": binding.thread_id,
        "generation": binding.generation,
        "nonce_hash": binding.nonce_hash,
        "protocol_version": binding.protocol_version,
    }


def _binding_from_payload(payload: dict[str, Any]) -> BridgeBinding:
    if payload.get("schema_version") != 1:
        raise BridgeProtocolError("unsupported binding schema")
    try:
        binding = BridgeBinding(
            thread_id=payload["thread_id"],
            generation=payload["generation"],
            nonce_hash=payload["nonce_hash"],
            protocol_version=payload["protocol_version"],
        )
    except (KeyError, TypeError) as error:
        raise BridgeProtocolError("invalid bridge binding") from error
    _validate_binding(binding)
    return binding


def _strings(value: Any) -> Iterator[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for nested in value.values():
            yield from _strings(nested)
    elif isinstance(value, list):
        for nested in value:
            yield from _strings(nested)


class BridgeProtocol:
    def __init__(
        self,
        *,
        registry: RunRegistry,
        bridge_dir: Path,
        sessions_dir: Path,
        clock: Callable[[], datetime] = _utc_now,
        lease_seconds: int = 30,
    ) -> None:
        if lease_seconds < 1 or lease_seconds > 300:
            raise ValueError("lease_seconds must be between 1 and 300")
        self.registry = registry
        self.bridge_dir = Path(bridge_dir)
        self.sessions_dir = Path(sessions_dir)
        self.clock = clock
        self.lease_seconds = lease_seconds
        self.bridge_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.bridge_dir.chmod(0o700)
        self.binding_path = self.bridge_dir / "binding.json"
        self.lock_path = self.bridge_dir / "protocol.lock"

    def bind(self, binding: BridgeBinding) -> None:
        _validate_binding(binding)
        with self._lock():
            if self.binding_path.exists():
                current = self.load_binding()
                if binding == current:
                    return
                if binding.generation <= current.generation:
                    raise BridgeProtocolError(
                        "rebind requires a newer reviewer generation"
                    )
            write_json_atomic(self.binding_path, _binding_payload(binding))
            self.binding_path.chmod(0o600)

    def load_binding(self) -> BridgeBinding:
        if not self.binding_path.exists():
            raise BridgeProtocolError("bridge is not bound")
        return _binding_from_payload(read_json(self.binding_path))

    def status(self, binding: BridgeBinding) -> BridgeStatus:
        self._require_current_binding(binding)
        candidates = self._pending_runs(binding)
        if not candidates:
            return BridgeStatus("idle")
        run_ids = tuple(run.run_id for run, _ in candidates)
        if len(candidates) > 1:
            return BridgeStatus("ambiguous", run_ids=run_ids)
        run, review_id = candidates[0]
        state = self._read_state(binding, review_id)
        if state is not None and state.get("state") in _FINAL_STATES:
            return BridgeStatus(str(state["state"]), run_ids=(run.run_id,), review_id=review_id)
        return BridgeStatus("queued", run_ids=(run.run_id,), review_id=review_id)

    def wait(
        self,
        binding: BridgeBinding,
        *,
        task_active: bool | None = None,
        client_connected: bool = True,
    ) -> BridgeStatus:
        self._require_current_binding(binding)
        if not client_connected:
            return BridgeStatus("offline")
        active = (
            self._bound_task_active(binding.thread_id)
            if task_active is None
            else task_active
        )
        if active:
            return BridgeStatus("active")
        return self.status(binding)

    def lease(
        self,
        binding: BridgeBinding,
        review_id: str,
        *,
        owner_id: str,
    ) -> DispatchLease:
        self._require_current_binding(binding)
        self._validate_identifier(review_id, "review_id")
        self._validate_identifier(owner_id, "owner_id")
        with self._lock():
            candidates = {
                candidate_review: run
                for run, candidate_review in self._pending_runs(binding)
            }
            run = candidates.get(review_id)
            if run is None:
                raise LeaseUnavailable("review is not pending for this binding")
            current = self._read_state(binding, review_id)
            now = self.clock()
            attempt = 1
            if current is not None:
                state = str(current.get("state"))
                if state in _FINAL_STATES:
                    raise LeaseUnavailable(f"review is already {state}")
                expires_at = _parse_time(str(current["expires_at"]))
                if state == "dispatching" and now >= expires_at:
                    expired = _lease_from_payload(current)
                    if self._bound_session_has_marker(
                        expired.thread_id, expired.marker
                    ):
                        self._write_state(replace_lease(expired, state="visible"))
                        raise LeaseUnavailable("review is already visible")
                if state in {"leased", "dispatching"} and now < expires_at:
                    raise LeaseUnavailable("review already has a live lease")
                attempt = int(current.get("attempt", 0)) + 1
            lease = self._new_lease(
                run=run,
                binding=binding,
                review_id=review_id,
                owner_id=owner_id,
                attempt=attempt,
                now=now,
            )
            self._write_state(lease)
            return lease

    def begin_dispatch(self, lease: DispatchLease) -> DispatchLease:
        with self._lock():
            current = self._require_matching_lease(lease)
            if current.state != "leased":
                raise BridgeProtocolError("lease is not in leased state")
            if self.clock() >= _parse_time(current.expires_at):
                raise LeaseUnavailable("lease expired before dispatch")
            updated = replace_lease(current, state="dispatching")
            self._write_state(updated)
            return updated

    def ack_visible(self, lease: DispatchLease, marker: str) -> DispatchLease:
        if marker != lease.marker:
            raise BridgeProtocolError("visible marker does not match lease")
        with self._lock():
            current = self._require_matching_lease(lease)
            if current.state != "dispatching":
                raise BridgeProtocolError("lease must be dispatching before acknowledgement")
            updated = replace_lease(current, state="visible")
            self._write_state(updated)
            return updated

    def recover(self, lease: DispatchLease) -> BridgeStatus:
        with self._lock():
            current = self._require_matching_lease(lease)
            if current.state == "visible":
                return BridgeStatus("visible", (current.run_id,), current.review_id)
            if current.state != "dispatching":
                raise BridgeProtocolError("only dispatching leases can be recovered")
            if self._bound_session_has_marker(current.thread_id, current.marker):
                visible = replace_lease(current, state="visible")
                self._write_state(visible)
                return BridgeStatus("visible", (visible.run_id,), visible.review_id)
            if self.clock() >= _parse_time(current.expires_at):
                queued = replace_lease(current, state="queued")
                self._write_state(queued)
                return BridgeStatus("queued", (queued.run_id,), queued.review_id)
            return BridgeStatus("dispatching", (current.run_id,), current.review_id)

    def current_lease(
        self, binding: BridgeBinding, review_id: str
    ) -> DispatchLease:
        self._require_current_binding(binding)
        self._validate_identifier(review_id, "review_id")
        payload = self._read_state(binding, review_id)
        if payload is None:
            raise BridgeProtocolError("lease state is missing")
        return _lease_from_payload(payload)

    @contextmanager
    def _lock(self):
        from .file_lock import exclusive_file_lock

        with exclusive_file_lock(self.lock_path, blocking=True):
            yield

    def _require_current_binding(self, binding: BridgeBinding) -> None:
        _validate_binding(binding)
        if binding != self.load_binding():
            raise BridgeProtocolError("stale binding")

    def _pending_runs(self, binding: BridgeBinding) -> list[tuple[RunRecord, str]]:
        candidates: list[tuple[RunRecord, str]] = []
        for run in self.registry.list_runs():
            if run.reviewer_backend is not ReviewBackend.VISIBLE_THREAD:
                continue
            if run.state in TERMINAL_STATES:
                continue
            if run.reviewer_thread_id != binding.thread_id:
                continue
            if run.reviewer_generation != binding.generation:
                continue
            pending = Path(run.run_dir) / "review-inbox" / "pending"
            if not pending.is_file():
                continue
            review_id = pending.read_text(encoding="utf-8").strip()
            if not _ID_RE.fullmatch(review_id):
                raise BridgeProtocolError("invalid pending review id")
            card = pending.parent / f"{review_id}.json"
            if not card.is_file():
                raise BridgeProtocolError("pending review card is missing")
            candidates.append((run, review_id))
        return sorted(candidates, key=lambda item: item[0].run_id)

    @staticmethod
    def _validate_identifier(value: str, label: str) -> None:
        if not isinstance(value, str) or not _ID_RE.fullmatch(value):
            raise BridgeProtocolError(f"invalid {label}")

    def _new_lease(
        self,
        *,
        run: RunRecord,
        binding: BridgeBinding,
        review_id: str,
        owner_id: str,
        attempt: int,
        now: datetime,
    ) -> DispatchLease:
        idempotency_key = f"{review_id}:{binding.generation}:{binding.thread_id}"
        marker = (
            "[LOOPWEAVE_VISIBLE_REVIEW "
            f"review_id={review_id} generation={binding.generation}]"
        )
        return DispatchLease(
            run_id=run.run_id,
            review_id=review_id,
            thread_id=binding.thread_id,
            generation=binding.generation,
            owner_id=owner_id,
            state="leased",
            attempt=attempt,
            issued_at=_iso(now),
            expires_at=_iso(now + timedelta(seconds=self.lease_seconds)),
            marker=marker,
            idempotency_key=idempotency_key,
        )

    def _state_path(self, binding: BridgeBinding, review_id: str) -> Path:
        key = f"{review_id}:{binding.generation}:{binding.thread_id}"
        digest = hashlib.sha256(key.encode("utf-8")).hexdigest()[:24]
        return self.bridge_dir / f"lease-{digest}.json"

    def _read_state(
        self, binding: BridgeBinding, review_id: str
    ) -> dict[str, Any] | None:
        path = self._state_path(binding, review_id)
        return read_json(path) if path.is_file() else None

    def _write_state(self, lease: DispatchLease) -> None:
        binding = BridgeBinding(
            thread_id=lease.thread_id,
            generation=lease.generation,
            nonce_hash=self.load_binding().nonce_hash,
            protocol_version=PROTOCOL_VERSION,
        )
        payload = {
            "schema_version": 1,
            "run_id": lease.run_id,
            "review_id": lease.review_id,
            "thread_id": lease.thread_id,
            "generation": lease.generation,
            "owner_id": lease.owner_id,
            "state": lease.state,
            "attempt": lease.attempt,
            "issued_at": lease.issued_at,
            "expires_at": lease.expires_at,
            "marker": lease.marker,
            "idempotency_key": lease.idempotency_key,
        }
        path = self._state_path(binding, lease.review_id)
        write_json_atomic(path, payload)
        path.chmod(0o600)

    def _require_matching_lease(self, lease: DispatchLease) -> DispatchLease:
        binding = self.load_binding()
        if lease.thread_id != binding.thread_id or lease.generation != binding.generation:
            raise BridgeProtocolError("stale lease binding")
        payload = self._read_state(binding, lease.review_id)
        if payload is None:
            raise BridgeProtocolError("lease state is missing")
        current = _lease_from_payload(payload)
        identity = (
            "run_id",
            "review_id",
            "thread_id",
            "generation",
            "owner_id",
            "attempt",
            "issued_at",
            "expires_at",
            "marker",
            "idempotency_key",
        )
        if any(getattr(current, field) != getattr(lease, field) for field in identity):
            raise BridgeProtocolError("lease identity mismatch")
        return current

    def _bound_session_has_marker(self, thread_id: str, marker: str) -> bool:
        matches = [
            thread for thread in list_threads(self.sessions_dir)
            if thread.thread_id == thread_id
        ]
        if len(matches) != 1:
            return False
        try:
            with matches[0].session_path.open("r", encoding="utf-8") as handle:
                for line in handle:
                    try:
                        record = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    for text in _strings(record):
                        if marker in text.splitlines():
                            return True
        except OSError:
            return False
        return False

    def _bound_task_active(self, thread_id: str) -> bool:
        matches = [
            thread for thread in list_threads(self.sessions_dir)
            if thread.thread_id == thread_id
        ]
        if len(matches) != 1:
            return True
        active: bool | None = None
        try:
            with matches[0].session_path.open("r", encoding="utf-8") as handle:
                for line in handle:
                    try:
                        record = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if record.get("type") != "event_msg":
                        continue
                    event_type = record.get("payload", {}).get("type")
                    if event_type == "task_started":
                        active = True
                    elif event_type == "task_complete":
                        active = False
        except OSError:
            return True
        return True if active is None else active


def _lease_from_payload(payload: dict[str, Any]) -> DispatchLease:
    if payload.get("schema_version") != 1:
        raise BridgeProtocolError("unsupported lease schema")
    try:
        return DispatchLease(
            run_id=payload["run_id"],
            review_id=payload["review_id"],
            thread_id=payload["thread_id"],
            generation=payload["generation"],
            owner_id=payload["owner_id"],
            state=payload["state"],
            attempt=payload["attempt"],
            issued_at=payload["issued_at"],
            expires_at=payload["expires_at"],
            marker=payload["marker"],
            idempotency_key=payload["idempotency_key"],
        )
    except (KeyError, TypeError) as error:
        raise BridgeProtocolError("invalid lease state") from error


def replace_lease(lease: DispatchLease, *, state: str) -> DispatchLease:
    return DispatchLease(**{**lease.__dict__, "state": state})
