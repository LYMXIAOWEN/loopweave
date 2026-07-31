from __future__ import annotations
import sys

import json
import os
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from loopweave.bridge_protocol import (
    BridgeProtocol,
    BridgeProtocolError,
    LeaseUnavailable,
)
from loopweave.models import BridgeBinding, ReviewBackend, RunMode, RunRecord, RunState


THREAD = "11111111-1111-4111-8111-111111111111"
OTHER_THREAD = "22222222-2222-4222-8222-222222222222"


class FakeRegistry:
    def __init__(self, runs: list[RunRecord]) -> None:
        self.runs = runs

    def list_runs(self) -> list[RunRecord]:
        return list(self.runs)


class Clock:
    def __init__(self) -> None:
        self.value = datetime(2026, 7, 13, 4, 0, tzinfo=timezone.utc)

    def __call__(self) -> datetime:
        return self.value

    def advance(self, seconds: int) -> None:
        self.value += timedelta(seconds=seconds)


def _run(root: Path, run_id: str = "run-one") -> RunRecord:
    run_dir = root / run_id
    inbox = run_dir / "review-inbox"
    inbox.mkdir(parents=True)
    review_id = "review-request-" + run_id.removeprefix("run-")
    (inbox / "pending").write_text(review_id + "\n", encoding="utf-8")
    (inbox / f"{review_id}.json").write_text(
        json.dumps({"review_id": review_id, "run_id": run_id}),
        encoding="utf-8",
    )
    return RunRecord(
        run_id=run_id,
        codex_thread_id=THREAD,
        cwd=str(root),
        tty="/dev/null",
        agent="claude",
        agent_pid=1,
        agent_process_start="now",
        control_token="token",
        state=RunState.READY_FOR_REVIEW,
        mode=RunMode.DEVELOP,
        reviewer_backend=ReviewBackend.VISIBLE_THREAD,
        reviewer_thread_id=THREAD,
        reviewer_generation=3,
        run_dir=str(run_dir),
    )


def _binding() -> BridgeBinding:
    return BridgeBinding(
        thread_id=THREAD,
        generation=3,
        nonce_hash="a" * 64,
        protocol_version=1,
    )


def _protocol(tmp_path: Path, runs: list[RunRecord]):
    clock = Clock()
    protocol = BridgeProtocol(
        registry=FakeRegistry(runs),
        bridge_dir=tmp_path / "bridge",
        sessions_dir=tmp_path / "sessions",
        clock=clock,
        lease_seconds=30,
    )
    protocol.bind(_binding())
    return protocol, clock


def _write_session(sessions: Path, thread_id: str, text: str) -> Path:
    path = sessions / f"rollout-{thread_id}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    records = [
        {
            "timestamp": "2026-07-13T04:00:00Z",
            "type": "session_meta",
            "payload": {"id": thread_id, "cwd": "/tmp"},
        },
        {
            "timestamp": "2026-07-13T04:00:01Z",
            "type": "response_item",
            "payload": {"text": text},
        },
    ]
    path.write_text(
        "".join(json.dumps(record) + "\n" for record in records),
        encoding="utf-8",
    )
    return path


def _append_task_event(path: Path, event_type: str, turn_id: str = "turn-1") -> None:
    with path.open("a", encoding="utf-8") as handle:
        handle.write(
            json.dumps(
                {
                    "timestamp": "2026-07-13T04:00:02Z",
                    "type": "event_msg",
                    "payload": {"type": event_type, "turn_id": turn_id},
                }
            )
            + "\n"
        )


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX permission bits assumed")
def test_binding_is_owner_only_and_stale_generation_fails_closed(tmp_path: Path):
    run = _run(tmp_path)
    protocol, _ = _protocol(tmp_path, [run])

    assert protocol.load_binding() == _binding()
    assert os.stat(protocol.binding_path).st_mode & 0o777 == 0o600

    stale = replace(_binding(), generation=2)
    with pytest.raises(BridgeProtocolError, match="stale binding"):
        protocol.status(stale)


def test_binding_rebind_requires_strictly_new_generation(tmp_path: Path):
    protocol, _ = _protocol(tmp_path, [])

    protocol.bind(_binding())
    with pytest.raises(BridgeProtocolError, match="generation"):
        protocol.bind(replace(_binding(), thread_id=OTHER_THREAD))
    with pytest.raises(BridgeProtocolError, match="generation"):
        protocol.bind(replace(_binding(), generation=2))

    newer = replace(_binding(), thread_id=OTHER_THREAD, generation=4)
    protocol.bind(newer)
    assert protocol.load_binding() == newer


def test_status_reports_idle_queued_and_ambiguous_without_choosing(tmp_path: Path):
    protocol, _ = _protocol(tmp_path, [])
    assert protocol.status(_binding()).status == "idle"

    run_one = _run(tmp_path, "run-one")
    protocol.registry.runs = [run_one]
    queued = protocol.status(_binding())
    assert queued.status == "queued"
    assert queued.run_ids == ("run-one",)

    protocol.registry.runs.append(_run(tmp_path, "run-two"))
    ambiguous = protocol.status(_binding())
    assert ambiguous.status == "ambiguous"
    assert ambiguous.run_ids == ("run-one", "run-two")


def test_wait_defers_when_task_is_active_or_client_is_closed(tmp_path: Path):
    run = _run(tmp_path)
    protocol, _ = _protocol(tmp_path, [run])

    assert protocol.wait(_binding(), task_active=True).status == "active"
    assert protocol.wait(_binding(), client_connected=False).status == "offline"
    assert list(protocol.bridge_dir.glob("lease-*.json")) == []


def test_wait_derives_activity_only_from_bound_desktop_session(tmp_path: Path):
    run = _run(tmp_path)
    protocol, _ = _protocol(tmp_path, [run])
    session = _write_session(protocol.sessions_dir, THREAD, "old turn")
    _append_task_event(session, "task_started")

    assert protocol.wait(_binding()).status == "active"

    _append_task_event(session, "task_complete")
    assert protocol.wait(_binding()).status == "queued"


def test_wait_fails_closed_when_bound_session_is_missing(tmp_path: Path):
    run = _run(tmp_path)
    protocol, _ = _protocol(tmp_path, [run])

    assert protocol.wait(_binding()).status == "active"


def test_concurrent_callers_create_only_one_live_lease(tmp_path: Path):
    run = _run(tmp_path)
    protocol, _ = _protocol(tmp_path, [run])
    review_id = "review-request-one"

    def attempt(owner: str):
        try:
            return protocol.lease(_binding(), review_id, owner_id=owner)
        except LeaseUnavailable:
            return None

    with ThreadPoolExecutor(max_workers=8) as executor:
        leases = list(executor.map(attempt, [f"owner-{i}" for i in range(8)]))

    winners = [lease for lease in leases if lease is not None]
    assert len(winners) == 1
    assert winners[0].idempotency_key == (
        f"{review_id}:3:{THREAD}"
    )


def test_dispatch_requires_legal_transition_and_ack_exact_marker(tmp_path: Path):
    run = _run(tmp_path)
    protocol, _ = _protocol(tmp_path, [run])
    lease = protocol.lease(_binding(), "review-request-one", owner_id="host")

    with pytest.raises(BridgeProtocolError, match="dispatching"):
        protocol.ack_visible(lease, lease.marker)

    dispatching = protocol.begin_dispatch(lease)
    with pytest.raises(BridgeProtocolError, match="marker"):
        protocol.ack_visible(dispatching, dispatching.marker + "-wrong")
    forged = replace(dispatching, marker=dispatching.marker + "-forged")
    with pytest.raises(BridgeProtocolError, match="identity"):
        protocol.ack_visible(forged, forged.marker)

    visible = protocol.ack_visible(dispatching, dispatching.marker)
    assert visible.state == "visible"
    with pytest.raises(LeaseUnavailable, match="already visible"):
        protocol.lease(_binding(), "review-request-one", owner_id="again")


def test_expired_lease_cannot_begin_dispatch(tmp_path: Path):
    run = _run(tmp_path)
    protocol, clock = _protocol(tmp_path, [run])
    lease = protocol.lease(_binding(), "review-request-one", owner_id="host")
    clock.advance(31)

    with pytest.raises(LeaseUnavailable, match="expired"):
        protocol.begin_dispatch(lease)


def test_expired_lease_can_be_reissued_with_incremented_attempt(tmp_path: Path):
    run = _run(tmp_path)
    protocol, clock = _protocol(tmp_path, [run])
    first = protocol.lease(_binding(), "review-request-one", owner_id="first")
    clock.advance(31)

    second = protocol.lease(_binding(), "review-request-one", owner_id="second")

    assert second.attempt == first.attempt + 1
    assert second.owner_id == "second"


def test_recovery_searches_only_bound_task_for_exact_marker(tmp_path: Path):
    run = _run(tmp_path)
    protocol, clock = _protocol(tmp_path, [run])
    lease = protocol.begin_dispatch(
        protocol.lease(_binding(), "review-request-one", owner_id="host")
    )
    _write_session(protocol.sessions_dir, OTHER_THREAD, lease.marker)
    clock.advance(31)

    absent = protocol.recover(lease)
    assert absent.status == "queued"

    retried = protocol.begin_dispatch(
        protocol.lease(_binding(), "review-request-one", owner_id="host-2")
    )
    _write_session(protocol.sessions_dir, THREAD, retried.marker)
    recovered = protocol.recover(retried)
    assert recovered.status == "visible"


def test_recovery_does_not_accept_partial_marker(tmp_path: Path):
    run = _run(tmp_path)
    protocol, clock = _protocol(tmp_path, [run])
    lease = protocol.begin_dispatch(
        protocol.lease(_binding(), "review-request-one", owner_id="host")
    )
    _write_session(protocol.sessions_dir, THREAD, lease.marker[:-1])
    clock.advance(31)

    assert protocol.recover(lease).status == "queued"


def test_lease_recovers_expired_dispatch_marker_before_reissuing(tmp_path: Path):
    run = _run(tmp_path)
    protocol, clock = _protocol(tmp_path, [run])
    dispatching = protocol.begin_dispatch(
        protocol.lease(_binding(), "review-request-one", owner_id="host")
    )
    _write_session(protocol.sessions_dir, THREAD, dispatching.marker)
    clock.advance(31)

    with pytest.raises(LeaseUnavailable, match="already visible"):
        protocol.lease(_binding(), "review-request-one", owner_id="host-2")

    assert protocol.status(_binding()).status == "visible"
