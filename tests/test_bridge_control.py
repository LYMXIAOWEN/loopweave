from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import pytest

from loopweave.bridge_control import (
    BridgeController,
    BridgeControlError,
    VisibleReviewDispatcher,
)
from loopweave.desktop_ipc import DesktopIpcError
from loopweave.models import ReviewBackend, RunRecord, RunState
from loopweave.registry import Registry


THREAD = "11111111-1111-4111-8111-111111111111"
OTHER = "22222222-2222-4222-8222-222222222222"


def _registry(tmp_path: Path) -> Registry:
    registry = Registry(tmp_path / "registry.sqlite")
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    registry.create_run(
        RunRecord(
            run_id="run-one",
            codex_thread_id=THREAD,
            cwd=str(tmp_path),
            tty="/dev/null",
            agent="claude",
            agent_pid=1,
            agent_process_start="now",
            control_token="token",
            state=RunState.RUNNING,
            reviewer_backend=ReviewBackend.VISIBLE_THREAD,
            reviewer_thread_id=THREAD,
            reviewer_thread_cwd=str(tmp_path),
            reviewer_generation=1,
            run_dir=str(run_dir),
        )
    )
    return registry


def _queue_review(
    registry: Registry,
    tmp_path: Path,
    *,
    run_id: str = "run-one",
    review_id: str = "review-request-one",
) -> dict[str, Any]:
    run = registry.get_run(run_id)
    inbox = Path(run.run_dir) / "review-inbox"
    inbox.mkdir(parents=True, exist_ok=True)
    card = {
        "review_id": review_id,
        "run_id": run.run_id,
        "completion_scope": "stage",
    }
    (inbox / "pending").write_text(card["review_id"] + "\n", encoding="utf-8")
    (inbox / f"{card['review_id']}.json").write_text(
        __import__("json").dumps(card),
        encoding="utf-8",
    )
    registry.force_state(run.run_id, RunState.READY_FOR_REVIEW)
    return card


def _write_reviewer_session(
    sessions_dir: Path,
    *,
    active: bool,
) -> None:
    sessions_dir.mkdir(parents=True, exist_ok=True)
    records = [
        {
            "timestamp": "2026-07-13T05:00:00Z",
            "type": "session_meta",
            "payload": {"id": THREAD, "cwd": "/tmp"},
        },
        {
            "timestamp": "2026-07-13T05:00:01Z",
            "type": "event_msg",
            "payload": {"type": "task_started", "turn_id": "turn-old"},
        },
    ]
    if not active:
        records.append(
            {
                "timestamp": "2026-07-13T05:00:02Z",
                "type": "event_msg",
                "payload": {"type": "task_complete", "turn_id": "turn-old"},
            }
        )
    path = sessions_dir / f"rollout-{THREAD}.jsonl"
    path.write_text(
        "".join(__import__("json").dumps(item) + "\n" for item in records),
        encoding="utf-8",
    )


class FakeDesktopIpc:
    def __init__(self, error: DesktopIpcError | None = None) -> None:
        self.error = error
        self.calls: list[tuple[str, str]] = []

    def start_visible_turn(self, *, thread_id: str, prompt: str) -> dict[str, str]:
        self.calls.append((thread_id, prompt))
        if self.error is not None:
            raise self.error
        return {"turnId": "turn-visible"}


def test_bind_writes_owner_only_secret_and_synchronizes_run_generation(tmp_path: Path):
    registry = _registry(tmp_path)
    controller = BridgeController(
        root=tmp_path,
        registry=registry,
        sessions_dir=tmp_path / "sessions",
    )

    binding = controller.bind(
        thread_id=THREAD,
        thread_cwd=str(tmp_path),
        run_id="run-one",
    )

    assert binding.thread_id == THREAD
    assert binding.generation == 2
    assert registry.get_run("run-one").reviewer_generation == 2
    assert registry.get_run("run-one").reviewer_thread_id == THREAD
    assert os.stat(controller.nonce_path).st_mode & 0o777 == 0o600
    assert os.stat(controller.protocol.binding_path).st_mode & 0o777 == 0o600


def test_preflight_requires_matching_healthy_binding(tmp_path: Path):
    registry = _registry(tmp_path)
    controller = BridgeController(
        root=tmp_path,
        registry=registry,
        sessions_dir=tmp_path / "sessions",
    )
    with pytest.raises(BridgeControlError, match="not bound"):
        controller.preflight(THREAD)

    controller.bind(thread_id=THREAD, thread_cwd=str(tmp_path), run_id="run-one")
    assert controller.preflight(THREAD).thread_id == THREAD
    with pytest.raises(BridgeControlError, match="different task"):
        controller.preflight(OTHER)


def test_unbind_invalidates_generation_and_removes_bridge_credentials(tmp_path: Path):
    registry = _registry(tmp_path)
    controller = BridgeController(
        root=tmp_path,
        registry=registry,
        sessions_dir=tmp_path / "sessions",
    )
    binding = controller.bind(
        thread_id=THREAD,
        thread_cwd=str(tmp_path),
        run_id="run-one",
    )
    lease = controller.bridge_dir / "lease-old.json"
    lease.write_text("{}\n", encoding="utf-8")

    controller.unbind()

    assert not controller.protocol.binding_path.exists()
    assert not controller.nonce_path.exists()
    assert not lease.exists()
    assert int(controller.generation_path.read_text(encoding="utf-8")) >= (
        binding.generation
    )


def test_registry_reviewer_binding_rejects_non_increasing_generation(tmp_path: Path):
    registry = _registry(tmp_path)

    with pytest.raises(ValueError, match="newer"):
        registry.set_reviewer_binding(
            "run-one", THREAD, str(tmp_path), generation=1
        )

    updated = registry.set_reviewer_binding(
        "run-one", OTHER, str(tmp_path), generation=2
    )
    assert updated.reviewer_thread_id == OTHER
    assert updated.reviewer_generation == 2


def test_dispatcher_starts_exactly_one_turn_for_idle_owner(tmp_path: Path):
    registry = _registry(tmp_path)
    sessions = tmp_path / "sessions"
    controller = BridgeController(
        root=tmp_path,
        registry=registry,
        sessions_dir=sessions,
    )
    binding = controller.bind(
        thread_id=THREAD,
        thread_cwd=str(tmp_path),
        run_id="run-one",
    )
    card = _queue_review(registry, tmp_path)
    _write_reviewer_session(sessions, active=False)
    ipc = FakeDesktopIpc()
    dispatcher = VisibleReviewDispatcher(controller=controller, ipc_client=ipc)

    first = dispatcher(registry.get_run("run-one"), card)
    second = dispatcher(registry.get_run("run-one"), card)

    assert first.status == "visible"
    assert second.status == "visible"
    assert len(ipc.calls) == 1
    thread_id, prompt = ipc.calls[0]
    assert thread_id == THREAD
    assert (
        f"[LOOPWEAVE_VISIBLE_REVIEW review_id={card['review_id']} "
        f"generation={binding.generation}]"
    ) in prompt
    assert "run_id: run-one" in prompt
    assert "review-submit --run-id run-one --review-file <file>" in prompt


def test_dispatcher_orphans_dead_pending_run_before_unique_delivery(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = _registry(tmp_path)
    sessions = tmp_path / "sessions"
    controller = BridgeController(
        root=tmp_path,
        registry=registry,
        sessions_dir=sessions,
    )
    binding = controller.bind(
        thread_id=THREAD,
        thread_cwd=str(tmp_path),
        run_id="run-one",
    )
    stale_card = _queue_review(registry, tmp_path)
    live_dir = tmp_path / "run-two"
    live_dir.mkdir()
    registry.create_run(
        RunRecord(
            run_id="run-two",
            codex_thread_id=THREAD,
            cwd=str(tmp_path),
            tty="/dev/null",
            agent="claude",
            agent_pid=2,
            agent_process_start="live-start",
            control_token="token-two",
            state=RunState.RUNNING,
            reviewer_backend=ReviewBackend.VISIBLE_THREAD,
            reviewer_thread_id=THREAD,
            reviewer_thread_cwd=str(tmp_path),
            reviewer_generation=binding.generation,
            run_dir=str(live_dir),
        )
    )
    live_card = _queue_review(
        registry,
        tmp_path,
        run_id="run-two",
        review_id="review-request-two",
    )
    _write_reviewer_session(sessions, active=False)

    def fake_start(pid: int) -> str:
        if pid == 1:
            raise ProcessLookupError(pid)
        return "live-start"

    monkeypatch.setattr(
        "loopweave.terminal_host.default_process_identity_reader",
        lambda: fake_start,
    )
    ipc = FakeDesktopIpc()
    outcome = VisibleReviewDispatcher(
        controller=controller,
        ipc_client=ipc,
    )(registry.get_run("run-two"), live_card)

    assert stale_card["review_id"] != live_card["review_id"]
    assert registry.get_run("run-one").state is RunState.ORPHANED
    assert outcome.status == "visible"
    assert outcome.run_ids == ("run-two",)
    assert len(ipc.calls) == 1
    assert "run_id: run-two" in ipc.calls[0][1]


def test_dispatcher_does_not_send_while_bound_task_is_active(tmp_path: Path):
    registry = _registry(tmp_path)
    sessions = tmp_path / "sessions"
    controller = BridgeController(
        root=tmp_path,
        registry=registry,
        sessions_dir=sessions,
    )
    controller.bind(
        thread_id=THREAD,
        thread_cwd=str(tmp_path),
        run_id="run-one",
    )
    card = _queue_review(registry, tmp_path)
    _write_reviewer_session(sessions, active=True)
    ipc = FakeDesktopIpc()

    outcome = VisibleReviewDispatcher(
        controller=controller,
        ipc_client=ipc,
    )(registry.get_run("run-one"), card)

    assert outcome.status == "active"
    assert ipc.calls == []
    assert list(controller.bridge_dir.glob("lease-*.json")) == []


def test_dispatcher_does_not_ack_when_desktop_has_no_owner(tmp_path: Path):
    registry = _registry(tmp_path)
    sessions = tmp_path / "sessions"
    controller = BridgeController(
        root=tmp_path,
        registry=registry,
        sessions_dir=sessions,
    )
    controller.bind(
        thread_id=THREAD,
        thread_cwd=str(tmp_path),
        run_id="run-one",
    )
    card = _queue_review(registry, tmp_path)
    _write_reviewer_session(sessions, active=False)
    ipc = FakeDesktopIpc(DesktopIpcError("no-client-found"))

    outcome = VisibleReviewDispatcher(
        controller=controller,
        ipc_client=ipc,
    )(registry.get_run("run-one"), card)

    assert outcome.status == "dispatching"
    lease = controller.protocol.current_lease(
        controller.protocol.load_binding(),
        card["review_id"],
    )
    assert lease.state == "dispatching"
    assert len(ipc.calls) == 1
