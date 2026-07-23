from __future__ import annotations

import importlib.util
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from loopweave.bridge_protocol import BridgeProtocol
from loopweave.models import BridgeBinding, ReviewBackend, RunRecord, RunState


ROOT = Path(__file__).resolve().parents[1]
SERVER_PATH = ROOT / "plugins/loopweave-visible-bridge/server/bridge_server.py"
THREAD = "11111111-1111-4111-8111-111111111111"
OTHER = "22222222-2222-4222-8222-222222222222"


class FakeRegistry:
    def __init__(self, runs: list[RunRecord]) -> None:
        self.runs = runs

    def list_runs(self) -> list[RunRecord]:
        return list(self.runs)


def _load_server_module():
    spec = importlib.util.spec_from_file_location("visible_bridge_server_prod", SERVER_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _run(tmp_path: Path, run_id: str = "run-one") -> RunRecord:
    run_dir = tmp_path / run_id
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
        cwd=str(tmp_path),
        tty="/dev/null",
        agent="claude",
        agent_pid=1,
        agent_process_start="now",
        control_token="token",
        state=RunState.READY_FOR_REVIEW,
        reviewer_backend=ReviewBackend.VISIBLE_THREAD,
        reviewer_thread_id=THREAD,
        reviewer_generation=1,
        run_dir=str(run_dir),
    )


def _session(path: Path, *, active: bool = False) -> None:
    records = [
        {
            "timestamp": "2026-07-13T04:00:00Z",
            "type": "session_meta",
            "payload": {"id": THREAD, "cwd": "/tmp"},
        },
        {
            "timestamp": "2026-07-13T04:00:01Z",
            "type": "event_msg",
            "payload": {"type": "task_started", "turn_id": "turn"},
        },
    ]
    if not active:
        records.append(
            {
                "timestamp": "2026-07-13T04:00:02Z",
                "type": "event_msg",
                "payload": {"type": "task_complete", "turn_id": "turn"},
            }
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(json.dumps(record) + "\n" for record in records),
        encoding="utf-8",
    )


def _service(tmp_path: Path, runs: list[RunRecord], *, active: bool = False):
    module = _load_server_module()
    sessions = tmp_path / "sessions"
    _session(sessions / "rollout.jsonl", active=active)
    protocol = BridgeProtocol(
        registry=FakeRegistry(runs),
        bridge_dir=tmp_path / "bridge",
        sessions_dir=sessions,
        clock=lambda: datetime(2026, 7, 13, 4, 1, tzinfo=timezone.utc),
    )
    protocol.bind(
        BridgeBinding(
            thread_id=THREAD,
            generation=1,
            nonce_hash="a" * 64,
            protocol_version=1,
        )
    )
    return module.BridgeService(protocol), protocol


def _meta(thread: str = THREAD) -> dict[str, str]:
    return {"thread_id": thread, "threadId": thread}


def test_service_rejects_unproven_host_without_disclosing_pending_runs(tmp_path: Path):
    service, _ = _service(tmp_path, [_run(tmp_path)])

    result = service.status(_meta(OTHER))

    assert result == {
        "status": "identity_rejected",
        "identity_proven": False,
        "reason": "host_identity_mismatch",
    }
    assert "run_ids" not in result


def test_service_reports_active_and_ambiguous_without_leasing(tmp_path: Path):
    active_service, active_protocol = _service(
        tmp_path / "active", [_run(tmp_path / "active")], active=True
    )
    assert active_service.wait(_meta(), timeout_ms=0)["status"] == "active"
    assert list(active_protocol.bridge_dir.glob("lease-*.json")) == []

    ambiguous_root = tmp_path / "ambiguous"
    runs = [_run(ambiguous_root, "run-one"), _run(ambiguous_root, "run-two")]
    ambiguous, protocol = _service(ambiguous_root, runs)
    result = ambiguous.wait(_meta(), timeout_ms=0)
    assert result == {
        "status": "ambiguous",
        "identity_proven": True,
        "run_ids": ["run-one", "run-two"],
    }
    assert list(protocol.bridge_dir.glob("lease-*.json")) == []


def test_service_argument_bounds_fail_closed(tmp_path: Path):
    service, _ = _service(tmp_path, [_run(tmp_path)])

    with pytest.raises(Exception, match="timeout"):
        service.wait(_meta(), timeout_ms=30_001)


def test_resume_delivery_rejects_wrong_host_without_dispatching(tmp_path: Path):
    calls = []
    service, _ = _service(tmp_path, [_run(tmp_path)])
    service.dispatcher = lambda run, card: calls.append((run, card))

    result = service.resume_delivery(_meta(OTHER))

    assert result == {
        "status": "identity_rejected",
        "identity_proven": False,
        "reason": "host_identity_mismatch",
    }
    assert calls == []


def test_resume_delivery_dispatches_exact_pending_card_once(tmp_path: Path):
    run = _run(tmp_path)
    service, protocol = _service(tmp_path, [run])
    calls = []

    class Dispatcher:
        def __call__(self, selected_run, card):
            calls.append((selected_run.run_id, card["review_id"]))
            binding = protocol.load_binding()
            lease = protocol.lease(
                binding,
                card["review_id"],
                owner_id="desktop-ipc-test",
            )
            dispatching = protocol.begin_dispatch(lease)
            visible = protocol.ack_visible(dispatching, dispatching.marker)
            return type(
                "Outcome",
                (),
                {
                    "status": "visible",
                    "run_ids": (visible.run_id,),
                    "review_id": visible.review_id,
                },
            )()

    service.dispatcher = Dispatcher()

    first = service.resume_delivery(_meta())
    second = service.resume_delivery(_meta())

    assert first == {
        "status": "visible",
        "identity_proven": True,
        "run_ids": ["run-one"],
        "review_id": "review-request-one",
    }
    assert second == first
    assert calls == [("run-one", "review-request-one")]


def test_resume_delivery_waits_while_owner_task_is_active(tmp_path: Path):
    calls = []
    service, protocol = _service(tmp_path, [_run(tmp_path)], active=True)
    service.dispatcher = lambda run, card: calls.append((run, card))

    result = service.resume_delivery(_meta())

    assert result == {
        "status": "active",
        "identity_proven": True,
    }
    assert calls == []
    assert list(protocol.bridge_dir.glob("lease-*.json")) == []


def test_widget_resource_falls_back_to_source_when_loaded_cache_is_removed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    module = _load_server_module()
    source_widget = tmp_path / "source" / "widget" / "index.html"
    source_widget.parent.mkdir(parents=True)
    source_widget.write_text("<main>source bridge</main>", encoding="utf-8")
    monkeypatch.setattr(module, "WIDGET_PATH", tmp_path / "deleted-cache.html")
    monkeypatch.setattr(module, "SOURCE_WIDGET_PATH", source_widget, raising=False)

    resource = module.widget_resource()

    assert resource["text"] == "<main>source bridge</main>"
