"""Live reliability contract tests for the generic-worker follow-up (ADR 0002).

Stage 1 contract — intentionally RED where the behavior does not exist yet.
Stage 2 implements `loopweave.liveness`, the `--task-file` launch path,
the audited orphan routes, and the taskless guidance to turn these green.

The single VerdictDeliveryGuardTests case is a GREEN characterization test
for LW-IT-002: it pins the literal message-then-"\\r" delivery order that
must NOT change unless the full production path reproduces a defect.

See docs/decisions/0002-audited-liveness-recovery-and-run-scoped-task-provenance.md
and docs/INTERACTIVE_TEST_FINDINGS.md (LW-IT-001 / LW-IT-003).
"""
from __future__ import annotations

import ast
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from loopweave.models import ReviewBackend, RunRecord, RunState
from loopweave.registry import Registry
from loopweave.supervisor import Supervisor, process_start_time


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def _alive_managed_run(root: Path, *, run_id: str = "run-1", state: RunState = RunState.RUNNING):
    fixture = Path(__file__).parent / "fixtures" / "echo_agent.py"
    socket_path = root / "control.sock"
    run_dir = root / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    supervisor = Supervisor(
        run_id=run_id,
        command=[sys.executable, "-u", str(fixture)],
        cwd=root,
        run_dir=run_dir,
        socket_path=socket_path,
        control_token="secret",
        passthrough=False,
    )
    pid = supervisor.start()
    start = process_start_time(pid)
    registry = Registry(root / "registry.sqlite")
    registry.create_run(
        RunRecord(
            run_id=run_id,
            codex_thread_id="thread-1",
            cwd=str(root),
            tty="/dev/test",
            agent="generic",
            agent_pid=pid,
            agent_process_start=start,
            control_token="secret",
            state=state,
            socket_path=str(socket_path),
            run_dir=str(run_dir),
        )
    )
    return registry, supervisor, run_dir, pid


def _synthetic_run(root: Path, *, run_id="run-1", pid=123, start="start", state=RunState.RUNNING, socket_name="control.sock"):
    run_dir = root / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    registry = Registry(root / "registry.sqlite")
    registry.create_run(
        RunRecord(
            run_id=run_id,
            codex_thread_id="thread-1",
            cwd=str(root),
            tty="/dev/test",
            agent="generic",
            agent_pid=pid,
            agent_process_start=start,
            control_token="secret",
            state=state,
            socket_path=str(root / socket_name),
            run_dir=str(run_dir),
        )
    )
    return registry, run_dir


def _orphan_events(run_dir: Path):
    path = Path(run_dir) / "events.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


# canned control senders for the probe / reconcile / recover matrix
def _sender_ok(run_id, pid, running=True):
    return lambda path, payload, timeout=3.0: {"status": "ok", "run_id": run_id, "pid": pid, "running": running}


def _sender_raises():
    def _raise(path, payload, timeout=3.0):
        raise ConnectionRefusedError("socket gone")
    return _raise


def _sender_error():
    return lambda path, payload, timeout=3.0: {"status": "error", "message": "invalid control token"}


# --------------------------------------------------------------------------- #
# 1. authoritative (non-forgeable) orphan provenance
# --------------------------------------------------------------------------- #
class OrphanProvenanceContractTests(unittest.TestCase):
    def test_audit_orphan_reads_authoritative_state_not_stale_snapshot(self) -> None:
        """ADR 0002: prior_state is read from the registry row at transition
        time, not the caller's stale RunRecord."""
        from loopweave.liveness import audit_orphan

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            registry, run_dir = _synthetic_run(root, state=RunState.RUNNING)
            stale = registry.get_run("run-1")
            registry.force_state("run-1", RunState.WORKER_CONTINUING)
            audit_orphan(
                registry,
                stale,
                source="reconcile",
                reason_category="control_unreachable",
            )
            provenance = json.loads(
                (run_dir / "orphan-provenance.json").read_text(encoding="utf-8")
            )
            self.assertEqual(
                provenance["prior_state"],
                RunState.WORKER_CONTINUING.value,
                "prior_state must reflect the authoritative registry row, not "
                "the stale caller snapshot (running)",
            )

    def test_audit_orphan_derives_prior_state_authoritatively_not_from_caller(self) -> None:
        from loopweave.liveness import audit_orphan

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            registry, run_dir = _synthetic_run(root, state=RunState.REVIEW_READY)
            # audit_orphan takes NO prior_state argument; it must read the
            # actual current state (REVIEW_READY) itself.
            audit_orphan(
                registry,
                registry.get_run("run-1"),
                source="deliver",
                reason_category="identity_mismatch",
            )
            self.assertEqual(registry.get_run("run-1").state, RunState.ORPHANED)
            provenance = json.loads(
                (run_dir / "orphan-provenance.json").read_text(encoding="utf-8")
            )
            self.assertEqual(provenance["prior_state"], RunState.REVIEW_READY.value)
            self.assertEqual(provenance["reason_category"], "identity_mismatch")
            event = _orphan_events(run_dir)[-1]
            self.assertEqual(event["event"], "run_orphaned")
            self.assertEqual(event["prior_state"], RunState.REVIEW_READY.value)

    def test_audit_orphan_event_omits_control_token(self) -> None:
        from loopweave.liveness import audit_orphan

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            registry, run_dir = _synthetic_run(root, state=RunState.RUNNING)
            with registry._connect() as connection:
                connection.execute(
                    "UPDATE runs SET control_token = ? WHERE run_id = ?",
                    ("super-secret-token-value", "run-1"),
                )
            audit_orphan(
                registry,
                registry.get_run("run-1"),
                source="reconcile",
                reason_category="control_unreachable",
            )
            blob = (run_dir / "events.jsonl").read_text(encoding="utf-8") + (
                run_dir / "orphan-provenance.json"
            ).read_text(encoding="utf-8")
            self.assertNotIn("super-secret-token-value", blob)


# --------------------------------------------------------------------------- #
# 2. probe classification
# --------------------------------------------------------------------------- #
class LivenessProbeContractTests(unittest.TestCase):
    @unittest.skipIf(sys.platform == "win32", "POSIX pty required")
    def test_probe_alive_for_authenticated_matching_live_session(self) -> None:
        from loopweave.liveness import probe_control_liveness

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            registry, supervisor, _run_dir, pid = _alive_managed_run(root)
            try:
                probe = probe_control_liveness(registry.get_run("run-1"))
            finally:
                supervisor.stop()
            self.assertTrue(probe.alive)
            self.assertEqual(probe.pid, pid)

    def test_probe_unreachable_when_socket_dead(self) -> None:
        from loopweave.liveness import probe_control_liveness

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            registry, _run_dir = _synthetic_run(root, socket_name="missing.sock")
            probe = probe_control_liveness(registry.get_run("run-1"))
            self.assertFalse(probe.alive)
            self.assertEqual(probe.reason, "control_unreachable")

    def test_probe_unauthenticated_for_wrong_token(self) -> None:
        from loopweave.liveness import probe_control_liveness

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            registry, _run_dir = _synthetic_run(root)
            probe = probe_control_liveness(registry.get_run("run-1"), sender=_sender_error())
            self.assertFalse(probe.alive)
            self.assertEqual(probe.reason, "control_unauthenticated")

    def test_probe_pid_reused_when_returned_pid_differs(self) -> None:
        from loopweave.liveness import probe_control_liveness

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            registry, _run_dir = _synthetic_run(root, pid=123)
            probe = probe_control_liveness(
                registry.get_run("run-1"), sender=_sender_ok("run-1", 555555)
            )
            self.assertFalse(probe.alive)
            self.assertEqual(probe.reason, "pid_reused")

    def test_probe_run_mismatch_when_returned_run_id_differs(self) -> None:
        from loopweave.liveness import probe_control_liveness

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            registry, _run_dir = _synthetic_run(root, pid=123)
            probe = probe_control_liveness(
                registry.get_run("run-1"), sender=_sender_ok("run-other", 123)
            )
            self.assertFalse(probe.alive)
            self.assertEqual(probe.reason, "run_mismatch")

    def test_probe_child_exited_when_not_running(self) -> None:
        from loopweave.liveness import probe_control_liveness

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            registry, _run_dir = _synthetic_run(root, pid=123)
            probe = probe_control_liveness(
                registry.get_run("run-1"), sender=_sender_ok("run-1", 123, running=False)
            )
            self.assertFalse(probe.alive)
            self.assertEqual(probe.reason, "child_exited")


# --------------------------------------------------------------------------- #
# 3. reconcile transition matrix
# --------------------------------------------------------------------------- #
class ReconcileLivenessContractTests(unittest.TestCase):
    @unittest.skipIf(sys.platform == "win32", "POSIX pty required")
    def test_alive_identity_match_leaves_run_running(self) -> None:
        from loopweave.liveness import reconcile_liveness

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            registry, supervisor, _run_dir, _pid = _alive_managed_run(root)
            try:
                reconcile_liveness(registry, registry.get_run("run-1"))
                self.assertEqual(registry.get_run("run-1").state, RunState.RUNNING)
            finally:
                supervisor.stop()

    @unittest.skipIf(sys.platform == "win32", "POSIX pty required")
    def test_transient_reader_failure_with_alive_control_does_not_orphan(self) -> None:
        from loopweave.liveness import reconcile_liveness

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            registry, supervisor, _run_dir, _pid = _alive_managed_run(root)
            try:
                reconcile_liveness(
                    registry,
                    registry.get_run("run-1"),
                    reader=lambda pid: (_ for _ in ()).throw(RuntimeError("transient ps failure")),
                )
                self.assertEqual(registry.get_run("run-1").state, RunState.RUNNING)
            finally:
                supervisor.stop()

    @unittest.skipIf(sys.platform == "win32", "POSIX pty required")
    def test_identity_mismatch_with_alive_control_orphans_fail_closed(self) -> None:
        from loopweave.liveness import reconcile_liveness

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            registry, supervisor, _run_dir, _pid = _alive_managed_run(root)
            try:
                reconcile_liveness(
                    registry,
                    registry.get_run("run-1"),
                    reader=lambda pid: "a-different-start-time",
                )
                self.assertEqual(
                    registry.get_run("run-1").state,
                    RunState.ORPHANED,
                    "a start-time mismatch must orphan fail-closed even when "
                    "the control channel otherwise reports alive",
                )
            finally:
                supervisor.stop()

    def test_missing_process_orphans(self) -> None:
        from loopweave.liveness import reconcile_liveness

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            registry, _run_dir = _synthetic_run(root, pid=999999, socket_name="missing.sock")
            reconcile_liveness(
                registry,
                registry.get_run("run-1"),
                reader=lambda pid: (_ for _ in ()).throw(ProcessLookupError()),
            )
            self.assertEqual(registry.get_run("run-1").state, RunState.ORPHANED)

    def test_transient_failure_wrong_token_orphans(self) -> None:
        from loopweave.liveness import reconcile_liveness

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            registry, _run_dir = _synthetic_run(root, pid=123)
            reconcile_liveness(
                registry,
                registry.get_run("run-1"),
                reader=lambda pid: (_ for _ in ()).throw(RuntimeError()),
                sender=_sender_error(),
            )
            self.assertEqual(registry.get_run("run-1").state, RunState.ORPHANED)

    def test_transient_failure_wrong_socket_orphans(self) -> None:
        from loopweave.liveness import reconcile_liveness

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            registry, _run_dir = _synthetic_run(root, pid=123)
            reconcile_liveness(
                registry,
                registry.get_run("run-1"),
                reader=lambda pid: (_ for _ in ()).throw(RuntimeError()),
                sender=_sender_raises(),
            )
            self.assertEqual(registry.get_run("run-1").state, RunState.ORPHANED)

    def test_transient_failure_wrong_returned_run_id_orphans(self) -> None:
        from loopweave.liveness import reconcile_liveness

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            registry, _run_dir = _synthetic_run(root, pid=123)
            reconcile_liveness(
                registry,
                registry.get_run("run-1"),
                reader=lambda pid: (_ for _ in ()).throw(RuntimeError()),
                sender=_sender_ok("run-other", 123),
            )
            self.assertEqual(registry.get_run("run-1").state, RunState.ORPHANED)

    def test_transient_failure_wrong_returned_pid_orphans(self) -> None:
        from loopweave.liveness import reconcile_liveness

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            registry, _run_dir = _synthetic_run(root, pid=123)
            reconcile_liveness(
                registry,
                registry.get_run("run-1"),
                reader=lambda pid: (_ for _ in ()).throw(RuntimeError()),
                sender=_sender_ok("run-1", 555555),
            )
            self.assertEqual(registry.get_run("run-1").state, RunState.ORPHANED)

    def test_transient_failure_child_exited_orphans(self) -> None:
        from loopweave.liveness import reconcile_liveness

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            registry, _run_dir = _synthetic_run(root, pid=123)
            reconcile_liveness(
                registry,
                registry.get_run("run-1"),
                reader=lambda pid: (_ for _ in ()).throw(RuntimeError()),
                sender=_sender_ok("run-1", 123, running=False),
            )
            self.assertEqual(registry.get_run("run-1").state, RunState.ORPHANED)


# --------------------------------------------------------------------------- #
# 4. recovery matrix
# --------------------------------------------------------------------------- #
class RecoverOrphanedContractTests(unittest.TestCase):
    def _orphan(self, registry, run_dir, *, prior_state=RunState.RUNNING, reader=None):
        from loopweave.liveness import audit_orphan

        if registry.get_run("run-1").state != prior_state:
            registry.force_state("run-1", prior_state)
        audit_orphan(
            registry,
            registry.get_run("run-1"),
            source="reconcile",
            reason_category="identity_mismatch",
        )

    @unittest.skipIf(sys.platform == "win32", "POSIX pty required")
    def test_recover_restores_assignable_orphan_when_session_confirmed_alive(self) -> None:
        from loopweave.liveness import recover_orphaned

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            registry, supervisor, run_dir, _pid = _alive_managed_run(root)
            try:
                self._orphan(registry, run_dir, prior_state=RunState.RUNNING)
                recover_orphaned(registry, registry.get_run("run-1"))
                self.assertEqual(registry.get_run("run-1").state, RunState.RUNNING)
                self.assertTrue(any(e["event"] == "run_recovered" for e in _orphan_events(run_dir)))
            finally:
                supervisor.stop()

    def test_recover_refuses_wrong_token(self) -> None:
        from loopweave.liveness import recover_orphaned

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            registry, run_dir = _synthetic_run(root, state=RunState.RUNNING)
            self._orphan(registry, run_dir)
            recover_orphaned(registry, registry.get_run("run-1"), sender=_sender_error())
            self.assertEqual(registry.get_run("run-1").state, RunState.ORPHANED)

    def test_recover_refuses_wrong_socket(self) -> None:
        from loopweave.liveness import recover_orphaned

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            registry, run_dir = _synthetic_run(root, state=RunState.RUNNING)
            self._orphan(registry, run_dir)
            recover_orphaned(registry, registry.get_run("run-1"), sender=_sender_raises())
            self.assertEqual(registry.get_run("run-1").state, RunState.ORPHANED)

    def test_recover_refuses_wrong_returned_run_id(self) -> None:
        from loopweave.liveness import recover_orphaned

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            registry, run_dir = _synthetic_run(root, state=RunState.RUNNING)
            self._orphan(registry, run_dir)
            recover_orphaned(
                registry, registry.get_run("run-1"), sender=_sender_ok("run-other", 123)
            )
            self.assertEqual(registry.get_run("run-1").state, RunState.ORPHANED)

    def test_recover_refuses_wrong_returned_pid(self) -> None:
        from loopweave.liveness import recover_orphaned

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            registry, run_dir = _synthetic_run(root, state=RunState.RUNNING)
            self._orphan(registry, run_dir)
            recover_orphaned(
                registry, registry.get_run("run-1"), sender=_sender_ok("run-1", 555555)
            )
            self.assertEqual(registry.get_run("run-1").state, RunState.ORPHANED)

    def test_recover_refuses_child_exited(self) -> None:
        from loopweave.liveness import recover_orphaned

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            registry, run_dir = _synthetic_run(root, state=RunState.RUNNING)
            self._orphan(registry, run_dir)
            recover_orphaned(
                registry,
                registry.get_run("run-1"),
                sender=_sender_ok("run-1", 123, running=False),
            )
            self.assertEqual(registry.get_run("run-1").state, RunState.ORPHANED)

    @unittest.skipIf(sys.platform == "win32", "POSIX pty required")
    def test_recover_refuses_identity_mismatch_even_with_alive_control(self) -> None:
        from loopweave.liveness import recover_orphaned

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            registry, supervisor, run_dir, _pid = _alive_managed_run(root)
            try:
                self._orphan(registry, run_dir, prior_state=RunState.RUNNING)
                recover_orphaned(
                    registry,
                    registry.get_run("run-1"),
                    reader=lambda pid: "a-different-start-time",
                )
                self.assertEqual(
                    registry.get_run("run-1").state,
                    RunState.ORPHANED,
                    "a start-time mismatch must block recovery even if the "
                    "control channel is alive",
                )
            finally:
                supervisor.stop()

    @unittest.skipIf(
        sys.platform == "win32", "POSIX pty required"
    )
    def test_recover_refuses_every_in_flight_or_pending_prior_state(self) -> None:
        """ADR 0002 section 4: recovery is refused for EVERY in-flight or
        pending-review prior state, not only REVIEW_READY."""
        from loopweave.liveness import recover_orphaned

        in_flight_or_pending = [
            RunState.REVIEWING,
            RunState.REVIEW_READY,
            RunState.DELIVERING,
            RunState.READY_FOR_REVIEW,
            RunState.OWNER_REVIEW_PENDING,
        ]
        for prior in in_flight_or_pending:
            with self.subTest(prior_state=prior):
                with tempfile.TemporaryDirectory() as directory:
                    root = Path(directory)
                    registry, supervisor, run_dir, _pid = _alive_managed_run(root)
                    try:
                        self._orphan(registry, run_dir, prior_state=prior)
                        recover_orphaned(registry, registry.get_run("run-1"))
                        self.assertEqual(
                            registry.get_run("run-1").state,
                            RunState.ORPHANED,
                            "recovery must refuse a run orphaned from {}".format(
                                prior.value
                            ),
                        )
                    finally:
                        supervisor.stop()

    def test_recover_refuses_terminal_prior_state(self) -> None:
        from loopweave.liveness import recover_orphaned

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            registry, run_dir = _synthetic_run(root, state=RunState.RUNNING)
            self._orphan(registry, run_dir, prior_state=RunState.APPROVED)
            recover_orphaned(registry, registry.get_run("run-1"), sender=_sender_ok("run-1", 123))
            self.assertEqual(registry.get_run("run-1").state, RunState.ORPHANED)


# --------------------------------------------------------------------------- #
# 5. every production orphan route is audited (exhaustive AST + behavioral)
# --------------------------------------------------------------------------- #
class OrphanRouteAuditContractTests(unittest.TestCase):
    def test_no_direct_orphan_force_state_outside_liveness_audit(self) -> None:
        src = Path(__file__).resolve().parents[1] / "src" / "loopweave"
        offenders = []
        for py in sorted(src.rglob("*.py")):
            module = py.relative_to(src).with_suffix("").as_posix()
            tree = ast.parse(py.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if (
                    isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and node.func.attr == "force_state"
                    and any(
                        isinstance(a, ast.Attribute) and a.attr == "ORPHANED"
                        for a in node.args
                    )
                ):
                    offenders.append(module)
        non_liveness = [m for m in offenders if m != "liveness"]
        self.assertEqual(
            non_liveness,
            [],
            "orphan transitions must route through liveness.audit_orphan; "
            "direct force_state(ORPHANED) remains in: {}".format(non_liveness),
        )

    def test_cli_reconcile_orphan_writes_audit_event(self) -> None:
        from loopweave.cli import _reconcile_run_liveness

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            registry, run_dir = _synthetic_run(root, pid=999999, socket_name="missing.sock")
            _reconcile_run_liveness(registry, registry.get_run("run-1"))
            self.assertEqual(registry.get_run("run-1").state, RunState.ORPHANED)
            self.assertTrue(
                any(e["event"] == "run_orphaned" for e in _orphan_events(run_dir)),
                "the CLI reconcile orphan route must write a run_orphaned audit event",
            )

    def test_deliver_review_identity_failure_writes_audit_event(self) -> None:
        import loopweave.terminal_host as terminal_host_module
        from loopweave.cli import _deliver_review

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            registry, run_dir = _synthetic_run(root, state=RunState.REVIEW_READY)
            (run_dir / "reviewer-verdict.md").write_text("body\n", encoding="utf-8")
            (run_dir / "reviewer-verdict.json").write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "run_id": "run-1",
                        "review_id": "review-1",
                        "verdict": "changes_requested",
                        "summary": "x",
                        "review_file": "reviewer-verdict.md",
                        "continue": True,
                    }
                ),
                encoding="utf-8",
            )
            with patch.object(
                terminal_host_module,
                "default_process_identity_reader",
                return_value=lambda pid: (_ for _ in ()).throw(RuntimeError("gone")),
            ):
                try:
                    _deliver_review(registry, registry.get_run("run-1"))
                except Exception:
                    pass
            self.assertEqual(registry.get_run("run-1").state, RunState.ORPHANED)
            self.assertTrue(
                any(e["event"] == "run_orphaned" for e in _orphan_events(run_dir))
            )

    def test_assign_stale_run_writes_audit_event(self) -> None:
        from loopweave.assignment import AssignmentError, assign_task

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            registry, run_dir = _synthetic_run(root, state=RunState.RUNNING)
            (root / "control.sock").write_text("", encoding="utf-8")
            task = root / "task.md"
            task.write_text("# Task\n", encoding="utf-8")
            try:
                assign_task(
                    registry.get_run("run-1"),
                    task,
                    registry=registry,
                    sender=lambda path, payload, timeout=3.0: {"status": "ok"},
                    process_start_reader=lambda pid: "different-start",
                )
            except AssignmentError:
                pass
            self.assertEqual(
                registry.get_run("run-1").state,
                RunState.ORPHANED,
                "the assignment stale-run orphan route must transition through audit_orphan",
            )
            self.assertTrue(
                any(e["event"] == "run_orphaned" for e in _orphan_events(run_dir)),
                "the assignment stale-run orphan route must be audited",
            )

    def test_takeover_attach_identity_failure_writes_audit_event(self) -> None:
        from loopweave.thread_takeover import ThreadTakeoverCoordinator

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            registry, run_dir = _synthetic_run(root, state=RunState.RUNNING)
            coordinator = ThreadTakeoverCoordinator(
                registry,
                root / "sessions",
                process_start=lambda pid: "different-start",
            )
            try:
                coordinator.attach("run-1")
            except Exception:
                pass
            self.assertEqual(registry.get_run("run-1").state, RunState.ORPHANED)
            self.assertTrue(
                any(e["event"] == "run_orphaned" for e in _orphan_events(run_dir))
            )

    def test_bridge_stale_review_dead_run_writes_audit_event(self) -> None:
        from loopweave.bridge_control import BridgeController

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            registry, run_dir = _synthetic_run(root, state=RunState.RUNNING)
            (run_dir / "review-inbox").mkdir(parents=True)
            (run_dir / "review-inbox" / "pending").write_text(
                "review-request-1\n", encoding="utf-8"
            )
            controller = BridgeController(
                root=root, registry=registry, sessions_dir=root / "sessions"
            )
            with patch(
                "loopweave.terminal_host.default_process_identity_reader",
                return_value=lambda pid: (_ for _ in ()).throw(RuntimeError("gone")),
            ):
                controller.reconcile_stale_pending_reviews(["run-1"])
            self.assertEqual(registry.get_run("run-1").state, RunState.ORPHANED)
            self.assertTrue(
                any(e["event"] == "run_orphaned" for e in _orphan_events(run_dir))
            )


# --------------------------------------------------------------------------- #
# 6. run-scoped task provenance (LW-IT-003)
# --------------------------------------------------------------------------- #
class TaskProvenanceContractTests(unittest.TestCase):
    def test_parser_accepts_run_task_file_flag(self) -> None:
        from loopweave.cli import build_parser

        args = build_parser().parse_args(["run", "codex", "--task-file", "/tmp/task.md"])
        self.assertEqual(args.agent, "codex")
        self.assertEqual(args.task_file, "/tmp/task.md")

    def test_atomic_tasked_visible_run_queues_card_referencing_exact_packet(self) -> None:
        """Single end-to-end contract for the advertised atomic path: launch
        one run with `--reviewer visible-thread --task-file`, prove the exact
        packet is assigned to that same run, submit a stage FROM that run, and
        assert its first visible card queues referencing the exact packet."""
        from types import SimpleNamespace
        from unittest.mock import MagicMock
        from loopweave.cli import _run_agent

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            control = root / "control"
            workspace = root / "workspace"
            control.mkdir()
            workspace.mkdir()
            var = control / "var"
            var.mkdir(parents=True, exist_ok=True)
            runs = control / "runs"
            projects = control / "projects"
            task_text = "# The real tasked job\n\nSpecific tasked body.\n"
            task_file = root / "task.md"
            task_file.write_text(task_text, encoding="utf-8")
            worker = (
                Path(__file__).resolve().parent / "fixtures" / "tasked_visible_stage_worker.py"
            )
            project_src = Path(__file__).resolve().parents[1] / "src"
            registry = Registry(var / "registry.sqlite")
            prior_home = os.environ.get("LOOPWEAVE_HOME")
            prior_src = os.environ.get("LOOPWEAVE_TEST_SRC")
            os.environ["LOOPWEAVE_HOME"] = str(control)
            os.environ["LOOPWEAVE_TEST_SRC"] = str(project_src)
            args = SimpleNamespace(
                cwd=str(workspace),
                cwd_explicit=True,
                project=None,
                workspace=None,
                thread=None,
                agent="generic",
                agent_args=[sys.executable, "-u", str(worker)],
                mode="develop",
                reviewer="visible-thread",
                task_file=str(task_file),
            )
            bridge = MagicMock()
            bridge.preflight.return_value = MagicMock(generation=1)
            try:
                with patch("loopweave.cli.RUNS_DIR", runs), patch(
                    "loopweave.cli.VAR_DIR", var
                ), patch("loopweave.cli.PROJECTS_DIR", projects), patch(
                    "loopweave.cli._registry", return_value=registry
                ), patch(
                    "loopweave.cli.discover_thread",
                    return_value=SimpleNamespace(thread_id="thread-1", cwd=str(workspace)),
                ), patch(
                    "loopweave.cli.os.getcwd", return_value=str(workspace)
                ), patch(
                    "loopweave.cli._bridge_controller", return_value=bridge
                ):
                    _run_agent(args)
            finally:
                if prior_home is None:
                    os.environ.pop("LOOPWEAVE_HOME", None)
                else:
                    os.environ["LOOPWEAVE_HOME"] = prior_home
                if prior_src is None:
                    os.environ.pop("LOOPWEAVE_TEST_SRC", None)
                else:
                    os.environ["LOOPWEAVE_TEST_SRC"] = prior_src

            run = registry.list_runs()[0]
            run_dir = Path(run.run_dir)
            packet = run_dir / "assigned-task-latest.md"
            self.assertEqual(
                packet.read_text(encoding="utf-8"),
                task_text,
                "the exact user task file must become the run-scoped packet",
            )
            inbox = run_dir / "review-inbox"
            pending = inbox / "pending"
            self.assertTrue(pending.exists(), "the run's first visible card must queue")
            review_id = pending.read_text(encoding="utf-8").strip()
            card = json.loads((inbox / "{}.json".format(review_id)).read_text(encoding="utf-8"))
            self.assertEqual(
                card["task_packet_path"],
                str(packet.resolve()),
                "the visible card must reference the exact run-scoped packet",
            )

    def test_assignment_delivery_sends_task_message_then_submit_key(self) -> None:
        from loopweave.assignment import assign_task

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            registry, run_dir = _synthetic_run(root, state=RunState.RUNNING)
            (root / "control.sock").write_text("", encoding="utf-8")
            task_text = "# Task\n\nBody of the task.\n"
            task_file = root / "task.md"
            task_file.write_text(task_text, encoding="utf-8")
            captured = []

            def capturing(path, payload, timeout=3.0):
                captured.append(payload)
                return {"status": "ok"}

            assign_task(
                registry.get_run("run-1"),
                task_file,
                sender=capturing,
                process_start_reader=lambda pid: "start",
            )
            delivered = [
                payload for payload in captured if payload["action"] == "send"
            ]
            self.assertEqual(len(delivered), 2)
            self.assertIn("# Task", delivered[0]["text"])
            self.assertIn("Body of the task.", delivered[0]["text"])
            self.assertEqual(delivered[1]["text"], "\r")

    def test_same_task_retry_is_idempotent_no_redelivery(self) -> None:
        """ADR 0002 section 5: re-assigning the same task (identical digest)
        is a no-op. Today assign_task re-writes a fresh history file and
        re-delivers the task on retry, so this is RED until idempotence is
        implemented."""
        from loopweave.assignment import assign_task

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            registry, run_dir = _synthetic_run(root, state=RunState.RUNNING)
            (root / "control.sock").write_text("", encoding="utf-8")
            task_text = "# Task\n\nStable task body.\n"
            task_file = root / "task.md"
            task_file.write_text(task_text, encoding="utf-8")
            sent = []

            def capturing(path, payload, timeout=3.0):
                sent.append(payload)
                return {"status": "ok"}

            kwargs = dict(sender=capturing, process_start_reader=lambda pid: "start")
            assign_task(registry.get_run("run-1"), task_file, **kwargs)
            second = assign_task(registry.get_run("run-1"), task_file, **kwargs)

            self.assertEqual(
                (run_dir / "assigned-task-latest.md").read_text(encoding="utf-8"),
                task_text,
            )
            # Readiness may add authenticated status probes. One assignment
            # still delivers exactly [message, "\r"] = 2 send actions; a
            # same-task retry must NOT add another send action.
            delivered = [
                payload for payload in sent if payload["action"] == "send"
            ]
            self.assertEqual(
                len(delivered),
                2,
                "idempotent retry must not re-deliver the task to the worker",
            )
            # No fresh timestamped history file on retry; the second result is
            # flagged duplicate.
            self.assertEqual(
                len(list(run_dir.glob("assigned-task-2*.md"))),
                1,
                "idempotent retry must not write a second history file",
            )
            self.assertTrue(second.duplicate)
            assigned_events = [
                e for e in _orphan_events(run_dir) if e.get("event") == "task_assigned"
            ]
            self.assertEqual(
                len(assigned_events), 1, "idempotent retry must not record a second task_assigned"
            )

    def test_assign_different_task_after_assignment_is_conflict(self) -> None:
        """ADR 0002 section 5: assigning a different task (different digest) to
        a run that already has one is a conflict. It is rejected and leaves
        the immutable packet, history count, control-send count, and assignment
        events unchanged. Today assign_task silently overwrites, so this is RED."""
        from loopweave.assignment import AssignmentError, assign_task

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            registry, run_dir = _synthetic_run(root, state=RunState.RUNNING)
            (root / "control.sock").write_text("", encoding="utf-8")
            task_a = root / "task-a.md"
            task_a.write_text("# Task A\n\nOriginal task body.\n", encoding="utf-8")
            task_b = root / "task-b.md"
            task_b.write_text("# Task B\n\nDifferent task body.\n", encoding="utf-8")
            sent = []

            def capturing(path, payload, timeout=3.0):
                sent.append(payload)
                return {"status": "ok"}

            kwargs = dict(sender=capturing, process_start_reader=lambda pid: "start")
            assign_task(registry.get_run("run-1"), task_a, **kwargs)
            history_after_a = len(list(run_dir.glob("assigned-task-2*.md")))
            sends_after_a = len(sent)
            events_after_a = len(
                [e for e in _orphan_events(run_dir) if e.get("event") == "task_assigned"]
            )

            with self.assertRaises(AssignmentError):
                assign_task(registry.get_run("run-1"), task_b, **kwargs)

            self.assertEqual(
                (run_dir / "assigned-task-latest.md").read_text(encoding="utf-8"),
                "# Task A\n\nOriginal task body.\n",
                "a conflicting task must not overwrite the immutable packet",
            )
            self.assertEqual(
                len(list(run_dir.glob("assigned-task-2*.md"))),
                history_after_a,
                "a conflicting task must not add a history file",
            )
            self.assertEqual(
                len(sent),
                sends_after_a,
                "a conflicting task must not deliver to the worker",
            )
            self.assertEqual(
                len(
                    [e for e in _orphan_events(run_dir) if e.get("event") == "task_assigned"]
                ),
                events_after_a,
                "a conflicting task must not record another task_assigned event",
            )

    def test_assign_retry_does_not_bypass_pending_review(self) -> None:
        """Re-assignment requires an assignable state; a run with an in-flight
        or pending review cannot be re-assigned, so retry cannot overwrite
        provenance or bypass the pending review."""
        from loopweave.assignment import AssignmentError, assign_task

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            registry, run_dir = _synthetic_run(root, state=RunState.READY_FOR_REVIEW)
            (root / "control.sock").write_text("", encoding="utf-8")
            task_file = root / "task.md"
            task_file.write_text("# Task\n", encoding="utf-8")
            with self.assertRaises(AssignmentError):
                assign_task(
                    registry.get_run("run-1"),
                    task_file,
                    sender=lambda path, payload, timeout=3.0: {"status": "ok"},
                    process_start_reader=lambda pid: "start",
                )
            self.assertEqual(
                registry.get_run("run-1").state, RunState.READY_FOR_REVIEW
            )

    def test_taskless_status_reports_awaiting_task_assignment(self) -> None:
        import io
        from loopweave.cli import main

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            registry, run_dir = _synthetic_run(root, state=RunState.RUNNING)
            output = io.StringIO()
            with patch("loopweave.cli._registry", return_value=registry), patch(
                "loopweave.cli._takeover_coordinator"
            ), patch(
                "loopweave.terminal_host.default_process_identity_reader",
                return_value=lambda pid: "start",
            ), patch("sys.stdout", output):
                main(["status", "run-1", "--json"])
            payload = json.loads(output.getvalue())
            self.assertIn(
                "awaiting task assignment",
                json.dumps(payload).lower(),
                "a taskless run must expose an actionable awaiting-assignment "
                "marker in status output",
            )

    @unittest.skipIf(sys.platform == "win32", "POSIX pty required")
    def test_taskless_premature_submit_gives_actionable_guidance(self) -> None:
        from loopweave.submission import SubmissionError, submit_stage

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            registry, supervisor, run_dir, _pid = _alive_managed_run(root)
            with registry._connect() as connection:
                connection.execute(
                    "UPDATE runs SET reviewer_backend = ? WHERE run_id = ?",
                    (ReviewBackend.VISIBLE_THREAD.value, "run-1"),
                )
            try:
                with patch(
                    "loopweave.submission._registry", return_value=registry
                ):
                    with self.assertRaises(SubmissionError) as raised:
                        submit_stage(
                            "run-1",
                            "Premature stage.",
                            evidence={"files_changed": [], "commands_run": []},
                        )
            finally:
                supervisor.stop()
            message = str(raised.exception).lower()
            self.assertIn("assign", message)
            self.assertIn("task-file", message)
            self.assertFalse((run_dir / "review-inbox" / "pending").exists())
            self.assertEqual(registry.get_run("run-1").state, RunState.RUNNING)


# --------------------------------------------------------------------------- #
# 7. LW-IT-002 guard (GREEN characterization)
# --------------------------------------------------------------------------- #
# --------------------------------------------------------------------------- #
# 8. recovery entry points in the public workflow (ADR section 4)
# --------------------------------------------------------------------------- #
class RecoveryEntryPointContractTests(unittest.TestCase):
    @unittest.skipIf(sys.platform == "win32", "POSIX pty required")
    def test_status_restores_an_exact_live_false_orphan_without_sqlite_edits(self) -> None:
        """ADR 0002 section 4: the normal public workflow (selected-run status,
        or an explicit recover command) must restore an exact live false
        positive orphan without a direct SQLite edit."""
        from loopweave.liveness import audit_orphan
        from loopweave.cli import main

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            registry, supervisor, run_dir, pid = _alive_managed_run(root)
            try:
                audit_orphan(
                    registry,
                    registry.get_run("run-1"),
                    source="reconcile",
                    reason_category="control_unreachable",
                )
                self.assertEqual(registry.get_run("run-1").state, RunState.ORPHANED)
                output = io.StringIO()
                with patch("loopweave.cli._registry", return_value=registry), patch(
                    "loopweave.cli._takeover_coordinator"
                ), patch("sys.stdout", output):
                    code = main(["status", "run-1"])
                self.assertEqual(code, 0)
                self.assertEqual(
                    registry.get_run("run-1").state,
                    RunState.RUNNING,
                    "status must recover an exact live false orphan, not "
                    "require a SQLite edit",
                )
                self.assertEqual(registry.get_run("run-1").agent_pid, pid)
            finally:
                supervisor.stop()

    def test_recover_command_is_registered(self) -> None:
        from loopweave.cli import build_parser

        args = build_parser().parse_args(["recover", "run-1"])
        self.assertEqual(args.command, "recover")
        self.assertEqual(args.run_id, "run-1")

    @unittest.skipIf(sys.platform == "win32", "POSIX pty required")
    def test_recover_command_restores_an_exact_live_false_orphan(self) -> None:
        from loopweave.liveness import audit_orphan
        from loopweave.cli import main

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            registry, supervisor, run_dir, pid = _alive_managed_run(root)
            try:
                audit_orphan(
                    registry,
                    registry.get_run("run-1"),
                    source="reconcile",
                    reason_category="control_unreachable",
                )
                output = io.StringIO()
                with patch("loopweave.cli._registry", return_value=registry), patch(
                    "loopweave.cli._takeover_coordinator"
                ), patch("sys.stdout", output):
                    code = main(["recover", "run-1"])
                self.assertEqual(code, 0)
                self.assertEqual(registry.get_run("run-1").state, RunState.RUNNING)
                self.assertEqual(registry.get_run("run-1").agent_pid, pid)
            finally:
                supervisor.stop()


# --------------------------------------------------------------------------- #
# 9. `loopweave runs` scope/performance (ADR section 4)
# --------------------------------------------------------------------------- #
class RunsScopeContractTests(unittest.TestCase):
    def test_runs_does_not_probe_or_mutate_terminal_or_orphaned_history(self) -> None:
        """Listing runs must stay bounded: it must not synchronously probe the
        control channel of every historical terminal/orphaned run, and must
        not mutate unrelated terminal history."""
        import io
        from loopweave.cli import main

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            registry = Registry(root / "registry.sqlite")
            probe_calls = []

            def counting_sender(path, payload, timeout=3.0):
                probe_calls.append(path)
                return {"status": "ok", "run_id": "x", "pid": 1, "running": True}

            # an archive of terminal + orphaned runs plus one assignable run
            for index, state in enumerate(
                [RunState.APPROVED, RunState.FAILED, RunState.STOPPED, RunState.ORPHANED]
            ):
                registry.create_run(
                    RunRecord(
                        run_id="run-{}".format(index),
                        codex_thread_id="t",
                        cwd=str(root),
                        tty="/dev/test",
                        agent="generic",
                        agent_pid=200 + index,
                        agent_process_start="start",
                        control_token="secret",
                        state=state,
                        run_dir=str(root / "run-{}".format(index)),
                    )
                )
            (root / "control.sock").write_text("", encoding="utf-8")
            registry.create_run(
                RunRecord(
                    run_id="run-live",
                    codex_thread_id="t",
                    cwd=str(root),
                    tty="/dev/test",
                    agent="generic",
                    agent_pid=4242,
                    agent_process_start="start",
                    control_token="secret",
                    state=RunState.RUNNING,
                    socket_path=str(root / "control.sock"),
                    run_dir=str(root / "run-live"),
                )
            )
            output = io.StringIO()
            with patch("loopweave.cli._registry", return_value=registry), patch(
                "loopweave.cli._takeover_coordinator"
            ), patch(
                "loopweave.terminal_host.default_control_sender",
                return_value=counting_sender,
            ), patch(
                "loopweave.terminal_host.default_process_identity_reader",
                return_value=lambda pid: "start",
            ), patch("sys.stdout", output):
                code = main(["runs"])
            self.assertEqual(code, 0)
            self.assertEqual(
                probe_calls,
                [],
                "runs must not synchronously probe the control channel of any "
                "run, terminal or otherwise, just to list the archive",
            )
            for state in (
                RunState.APPROVED,
                RunState.FAILED,
                RunState.STOPPED,
                RunState.ORPHANED,
            ):
                self.assertIn(
                    state,
                    [r.state for r in registry.list_runs()],
                    "runs must not mutate terminal/orphaned history",
                )


# --------------------------------------------------------------------------- #
# 10. bounded, schema-constrained audit data (ADR section 6)
# --------------------------------------------------------------------------- #
class BoundedAuditContractTests(unittest.TestCase):
    def test_audit_rejects_unrecognized_reason_category_leaving_state_and_events_unchanged(
        self,
    ) -> None:
        from loopweave.liveness import audit_orphan

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            registry, run_dir = _synthetic_run(root, state=RunState.RUNNING)
            with self.assertRaises(Exception):
                audit_orphan(
                    registry,
                    registry.get_run("run-1"),
                    source="reconcile",
                    reason_category="not-a-real-category",
                )
            self.assertEqual(
                registry.get_run("run-1").state,
                RunState.RUNNING,
                "an invalid reason_category must not orphan the run",
            )
            self.assertFalse(
                (run_dir / "orphan-provenance.json").exists(),
                "an invalid reason_category must not write provenance",
            )
            self.assertEqual(
                _orphan_events(run_dir),
                [],
                "an invalid reason_category must not write any event",
            )

    def test_audit_source_is_bounded_to_64_bytes_multibyte(self) -> None:
        from loopweave.liveness import audit_orphan

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            registry, run_dir = _synthetic_run(root)
            # multibyte source: each char is 3 UTF-8 bytes
            multibyte = "中" * 100  # 300 bytes, far over 64
            audit_orphan(
                registry,
                registry.get_run("run-1"),
                source=multibyte,
                reason_category="control_unreachable",
            )
            event = _orphan_events(run_dir)[-1]
            provenance = json.loads(
                (run_dir / "orphan-provenance.json").read_text(encoding="utf-8")
            )
            for record in (event, provenance):
                self.assertLessEqual(
                    len(str(record.get("source", "")).encode("utf-8")),
                    64,
                    "the source field must be bounded to <=64 UTF-8 bytes in "
                    "both the event and the provenance record",
                )

    def test_audit_bounds_an_oversized_source_label(self) -> None:
        from loopweave.liveness import audit_orphan

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            registry, run_dir = _synthetic_run(root)
            huge = "x" * 4096
            audit_orphan(
                registry,
                registry.get_run("run-1"),
                source=huge,
                reason_category="control_unreachable",
            )
            blob = (run_dir / "events.jsonl").read_text(encoding="utf-8") + (
                run_dir / "orphan-provenance.json"
            ).read_text(encoding="utf-8")
            self.assertNotIn(huge, blob)

    def test_exception_text_cannot_enter_audit_records(self) -> None:
        """A reader exception triggering an orphan must not write its (possibly
        huge, possibly secret-laden) str() into events.jsonl or
        orphan-provenance.json."""
        from loopweave.liveness import reconcile_liveness

        distinctive = "SECRET-FROM-EXCEPTION-{0}".format("z" * 2000)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            registry, run_dir = _synthetic_run(root, pid=999999, socket_name="missing.sock")

            def reader(pid):
                raise RuntimeError(distinctive)

            reconcile_liveness(registry, registry.get_run("run-1"), reader=reader)
            self.assertEqual(registry.get_run("run-1").state, RunState.ORPHANED)
            blob = (run_dir / "events.jsonl").read_text(encoding="utf-8")
            if (run_dir / "orphan-provenance.json").exists():
                blob += (run_dir / "orphan-provenance.json").read_text(encoding="utf-8")
            self.assertNotIn(distinctive, blob)


# --------------------------------------------------------------------------- #
# 11. crash-consistency of the orphan transition (ADR section 7)
# --------------------------------------------------------------------------- #
class CrashConsistencyContractTests(unittest.TestCase):
    @unittest.skipIf(sys.platform == "win32", "POSIX pty required")
    def test_orphan_remains_recoverable_when_event_write_fails(self) -> None:
        """ADR 0002 section 7: the durable provenance + state precede the event
        write, so an event-write failure must not make recovery impossible. The
        injection patches liveness's own append_event binding and asserts it
        fired, so a re-import bypass cannot pass."""
        from loopweave.liveness import audit_orphan, recover_orphaned

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            registry, supervisor, run_dir, _pid = _alive_managed_run(root)
            try:
                with patch(
                    "loopweave.liveness.append_event",
                    side_effect=OSError("disk full"),
                ) as mock_event:
                    try:
                        audit_orphan(
                            registry,
                            registry.get_run("run-1"),
                            source="reconcile",
                            reason_category="control_unreachable",
                        )
                    except OSError:
                        pass
                    self.assertTrue(
                        mock_event.called,
                        "the event-write failure injection must actually fire",
                    )
                self.assertEqual(registry.get_run("run-1").state, RunState.ORPHANED)
                self.assertTrue((run_dir / "orphan-provenance.json").exists())
                recover_orphaned(registry, registry.get_run("run-1"))
                self.assertEqual(
                    registry.get_run("run-1").state,
                    RunState.RUNNING,
                    "recovery must succeed from the durable provenance even "
                    "when the event write failed",
                )
                # Recovery must backfill the missing orphan audit from the
                # durable provenance before clearing it, so the final history
                # carries one truthful orphan transition and one truthful
                # recovery transition - never discard orphan records.
                events = _orphan_events(run_dir)
                self.assertEqual(
                    len([e for e in events if e.get("event") == "run_orphaned"]),
                    1,
                    "a backfilled orphan audit must leave exactly one run_orphaned",
                )
                self.assertEqual(
                    len([e for e in events if e.get("event") == "run_recovered"]),
                    1,
                    "recovery must leave exactly one run_recovered",
                )
            finally:
                supervisor.stop()

    @unittest.skipIf(sys.platform == "win32", "POSIX pty required")
    def test_recovery_force_state_failure_leaves_no_completed_recovered_event(
        self,
    ) -> None:
        """ADR 0002 section 7: run_recovered is a completion event written only
        after force_state(restored) succeeds. If the state transition fails, no
        run_recovered event may be left claiming a recovery that did not
        happen."""
        from loopweave.liveness import audit_orphan, recover_orphaned

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            registry, supervisor, run_dir, _pid = _alive_managed_run(root)
            try:
                audit_orphan(
                    registry,
                    registry.get_run("run-1"),
                    source="reconcile",
                    reason_category="control_unreachable",
                )
                original_force_state = registry.force_state

                def fail_restore(run_id, new_state):
                    if new_state is not RunState.ORPHANED:
                        raise RuntimeError("db update failed")
                    return original_force_state(run_id, new_state)

                with patch.object(
                    registry, "force_state", side_effect=fail_restore
                ):
                    try:
                        recover_orphaned(registry, registry.get_run("run-1"))
                    except Exception:
                        pass
                self.assertEqual(
                    registry.get_run("run-1").state,
                    RunState.ORPHANED,
                    "a failed restore must leave the run orphaned",
                )
                self.assertEqual(
                    [e for e in _orphan_events(run_dir) if e.get("event") == "run_recovered"],
                    [],
                    "no completed run_recovered event may be left for a failed "
                    "state transition",
                )
            finally:
                supervisor.stop()

    def test_provenance_write_failure_does_not_orphan_db_row(self) -> None:
        """ADR 0002 section 7: provenance is written BEFORE force_state via a
        named writer. Injecting a failure through that writer must leave the DB
        row un-orphaned, and the injection must be proven to fire (no chmod, no
        unused mock)."""
        from loopweave.liveness import audit_orphan

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            registry, run_dir = _synthetic_run(root, state=RunState.RUNNING)
            with patch(
                "loopweave.liveness._write_orphan_provenance",
                side_effect=OSError("cannot persist provenance"),
            ) as mock_write:
                try:
                    audit_orphan(
                        registry,
                        registry.get_run("run-1"),
                        source="reconcile",
                        reason_category="control_unreachable",
                    )
                except OSError:
                    pass
                self.assertTrue(
                    mock_write.called,
                    "the provenance-write failure injection must actually fire",
                )
            self.assertEqual(
                registry.get_run("run-1").state,
                RunState.RUNNING,
                "a provenance-write failure must not orphan the DB row",
            )

    def test_force_state_orphaned_runs_only_after_provenance_exists(self) -> None:
        """ADR 0002 section 7: when force_state(ORPHANED) runs, the durable
        provenance must already exist. Wraps force_state to assert that
        invariant at the moment of the state change."""
        from loopweave.liveness import audit_orphan

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            registry, run_dir = _synthetic_run(root, state=RunState.RUNNING)
            original_force_state = registry.force_state
            provenance_present_at_orphan = {"value": False}

            def guarding_force_state(run_id, new_state):
                if new_state is RunState.ORPHANED:
                    provenance_present_at_orphan["value"] = (
                        run_dir / "orphan-provenance.json"
                    ).exists()
                return original_force_state(run_id, new_state)

            with patch.object(registry, "force_state", side_effect=guarding_force_state):
                audit_orphan(
                    registry,
                    registry.get_run("run-1"),
                    source="reconcile",
                    reason_category="control_unreachable",
                )
            self.assertTrue(
                provenance_present_at_orphan["value"],
                "the durable provenance must exist before force_state(ORPHANED)",
            )

    @unittest.skipIf(sys.platform == "win32", "POSIX pty required")
    def test_recovery_event_failure_then_reconcile_completes_audit(self) -> None:
        """ADR 0002 section 7 (idempotent completion): the real crash window is
        force_state(restored) succeeding while the run_recovered append fails.
        The run is restored and provenance remains with no completion event; a
        later reconcile call appends exactly one truthful run_recovered and only
        then clears provenance. Inject the first event failure, prove it fired,
        run the reconcile path, and assert the terminal state converges."""
        from loopweave.liveness import audit_orphan, recover_orphaned

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            registry, supervisor, run_dir, _pid = _alive_managed_run(root)
            try:
                audit_orphan(
                    registry,
                    registry.get_run("run-1"),
                    source="reconcile",
                    reason_category="control_unreachable",
                )
                # First recovery: force_state(restored) succeeds, but the
                # run_recovered append fails (the crash window).
                with patch(
                    "loopweave.liveness.append_event",
                    side_effect=OSError("event write failed"),
                ) as mock_first:
                    try:
                        recover_orphaned(registry, registry.get_run("run-1"))
                    except OSError:
                        pass
                    self.assertTrue(
                        mock_first.called,
                        "the first recovery-event failure injection must fire",
                    )
                self.assertEqual(
                    registry.get_run("run-1").state,
                    RunState.RUNNING,
                    "the run is restored even though the completion event failed",
                )
                self.assertTrue(
                    (run_dir / "orphan-provenance.json").exists(),
                    "provenance must remain while the recovery audit is incomplete",
                )
                self.assertEqual(
                    [e for e in _orphan_events(run_dir) if e.get("event") == "run_recovered"],
                    [],
                    "no run_recovered may exist before the reconcile completes it",
                )
                # Second (reconcile) call via the PUBLIC CLI proves
                # status/recover invoke the completion path for a restored run
                # with lingering provenance - not just the internal helper.
                from loopweave.cli import main

                output = io.StringIO()
                with patch(
                    "loopweave.cli._registry", return_value=registry
                ), patch("loopweave.cli._takeover_coordinator"), patch(
                    "sys.stdout", output
                ):
                    code = main(["recover", "run-1"])
                self.assertEqual(code, 0)
                self.assertEqual(registry.get_run("run-1").state, RunState.RUNNING)
                events = _orphan_events(run_dir)
                self.assertEqual(
                    len([e for e in events if e.get("event") == "run_orphaned"]),
                    1,
                )
                self.assertEqual(
                    len([e for e in events if e.get("event") == "run_recovered"]),
                    1,
                )
                self.assertFalse(
                    (run_dir / "orphan-provenance.json").exists(),
                    "provenance must be cleared once the recovery audit completes",
                )
            finally:
                supervisor.stop()

    @unittest.skipIf(sys.platform == "win32", "POSIX pty required")
    def test_recovery_clear_failure_leaves_run_restored_audited_with_provenance(
        self,
    ) -> None:
        """ADR 0002 section 7 (recovery side): a failure clearing the provenance
        record (the last step) must leave the run restored AND audited, with the
        provenance record still on disk. The injection must be proven to fire."""
        from loopweave.liveness import audit_orphan, recover_orphaned

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            registry, supervisor, run_dir, _pid = _alive_managed_run(root)
            try:
                audit_orphan(
                    registry,
                    registry.get_run("run-1"),
                    source="reconcile",
                    reason_category="control_unreachable",
                )
                with patch(
                    "loopweave.liveness._clear_orphan_provenance",
                    side_effect=OSError("clear failed"),
                ) as mock_clear:
                    try:
                        recover_orphaned(registry, registry.get_run("run-1"))
                    except OSError:
                        pass
                    self.assertTrue(
                        mock_clear.called,
                        "the clear-failure injection must actually fire",
                    )
                self.assertEqual(
                    registry.get_run("run-1").state,
                    RunState.RUNNING,
                    "recovery must restore the run even if the clear fails",
                )
                self.assertTrue(
                    any(e["event"] == "run_recovered" for e in _orphan_events(run_dir)),
                    "the run_recovered audit must be present even if the clear fails",
                )
                self.assertTrue(
                    (run_dir / "orphan-provenance.json").exists(),
                    "the provenance record must remain after a failed clear",
                )
            finally:
                supervisor.stop()


class AuditSchemaContractTests(unittest.TestCase):
    _REASON_ENUM = {
        "identity_mismatch",
        "control_unreachable",
        "control_unauthenticated",
        "pid_reused",
        "child_exited",
        "run_mismatch",
    }
    _STATE_VALUES = {s.value for s in RunState}

    @unittest.skipIf(sys.platform == "win32", "POSIX pty required")
    def test_orphan_and_recovery_events_carry_complete_schema(self) -> None:
        """The task packet's minimum audit contract: both run_orphaned and
        run_recovered events must carry a non-empty bounded source, a fixed
        reason_category (orphan only), an authoritative prior_state, the run_id,
        a timestamp, and a restored_state (recovery only). No optional/omitted
        field may slip through."""
        from loopweave.liveness import audit_orphan, recover_orphaned

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            registry, supervisor, run_dir, _pid = _alive_managed_run(root)
            run_id = "run-1"
            try:
                audit_orphan(
                    registry,
                    registry.get_run(run_id),
                    source="reconcile",
                    reason_category="control_unreachable",
                )
                recover_orphaned(registry, registry.get_run(run_id))
                events = _orphan_events(run_dir)
            finally:
                supervisor.stop()

        orphaned = [e for e in events if e.get("event") == "run_orphaned"][-1]
        recovered = [e for e in events if e.get("event") == "run_recovered"][-1]

        self.assertIn(orphaned["reason_category"], self._REASON_ENUM)
        self.assertGreater(len(orphaned["source"]), 0)
        self.assertLessEqual(len(str(orphaned["source"]).encode("utf-8")), 64)
        self.assertIn(orphaned["prior_state"], self._STATE_VALUES)
        self.assertEqual(orphaned["run_id"], run_id)
        self.assertTrue(orphaned.get("timestamp"))

        self.assertGreater(len(recovered["source"]), 0)
        self.assertLessEqual(len(str(recovered["source"]).encode("utf-8")), 64)
        self.assertIn(recovered["restored_state"], self._STATE_VALUES)
        self.assertEqual(recovered["run_id"], run_id)
        self.assertTrue(recovered.get("timestamp"))


# --------------------------------------------------------------------------- #
# 12a. provenance identity validation before recovery (ADR s3, fail-closed)
# --------------------------------------------------------------------------- #
class ProvenanceValidationContractTests(unittest.TestCase):
    def _orphan_then_corrupt(self, root, registry, run_dir, *, overrides):
        from loopweave.liveness import audit_orphan
        from loopweave.protocol import write_json_atomic
        audit_orphan(
            registry,
            registry.get_run("run-1"),
            source="reconcile",
            reason_category="control_unreachable",
        )
        provenance = json.loads(
            (run_dir / "orphan-provenance.json").read_text(encoding="utf-8")
        )
        provenance.update(overrides)
        write_json_atomic(run_dir / "orphan-provenance.json", provenance)

    def _assert_refused(self, registry, run_dir):
        from loopweave.liveness import recover_orphaned
        result = recover_orphaned(registry, registry.get_run("run-1"))
        self.assertEqual(result, "invalid_provenance")
        self.assertEqual(registry.get_run("run-1").state, RunState.ORPHANED)
        self.assertTrue(
            (run_dir / "orphan-provenance.json").exists(),
            "rejected provenance must be left intact",
        )

    @unittest.skipIf(sys.platform == "win32", "POSIX pty required")
    def test_recover_rejects_provenance_with_wrong_run_id(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            registry, supervisor, run_dir, _pid = _alive_managed_run(root)
            try:
                self._orphan_then_corrupt(root, registry, run_dir, overrides={"run_id": "another-run"})
                self._assert_refused(registry, run_dir)
            finally:
                supervisor.stop()

    @unittest.skipIf(sys.platform == "win32", "POSIX pty required")
    def test_recover_rejects_provenance_with_missing_transition_id(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            registry, supervisor, run_dir, _pid = _alive_managed_run(root)
            try:
                self._orphan_then_corrupt(root, registry, run_dir, overrides={"transition_id": None})
                self._assert_refused(registry, run_dir)
            finally:
                supervisor.stop()

    @unittest.skipIf(sys.platform == "win32", "POSIX pty required")
    def test_recover_rejects_provenance_with_invalid_reason(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            registry, supervisor, run_dir, _pid = _alive_managed_run(root)
            try:
                self._orphan_then_corrupt(root, registry, run_dir, overrides={"reason_category": "bogus"})
                self._assert_refused(registry, run_dir)
            finally:
                supervisor.stop()

    @unittest.skipIf(sys.platform == "win32", "POSIX pty required")
    def test_recover_rejects_provenance_with_malformed_state(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            registry, supervisor, run_dir, _pid = _alive_managed_run(root)
            try:
                self._orphan_then_corrupt(root, registry, run_dir, overrides={"prior_state": "not-a-state"})
                self._assert_refused(registry, run_dir)
            finally:
                supervisor.stop()


# --------------------------------------------------------------------------- #
# 12b. audit_orphan aborts (no mutation) when the authoritative read fails
# --------------------------------------------------------------------------- #
class AuthoritativeStateReadContractTests(unittest.TestCase):
    def test_audit_orphan_aborts_when_authoritative_state_read_fails(self) -> None:
        from loopweave.liveness import audit_orphan

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            registry, run_dir = _synthetic_run(root, state=RunState.RUNNING)
            broken = Mock()
            broken.get_run.side_effect = RuntimeError("registry unavailable")
            with self.assertRaises(RuntimeError):
                audit_orphan(
                    broken,
                    registry.get_run("run-1"),
                    source="reconcile",
                    reason_category="control_unreachable",
                )
            self.assertEqual(
                registry.get_run("run-1").state,
                RunState.RUNNING,
                "a failed authoritative read must not mutate state",
            )
            self.assertFalse(
                (run_dir / "orphan-provenance.json").exists(),
                "a failed authoritative read must not write provenance",
            )
            self.assertEqual(
                _orphan_events(run_dir), [], "a failed read must not write events"
            )


# --------------------------------------------------------------------------- #
# 12. every orphan route consults the authenticated control status (ADR s2)
# --------------------------------------------------------------------------- #
class RouteAuthenticatedDecisionContractTests(unittest.TestCase):
    """A transient identity-reader failure with a matching live control channel
    must NOT orphan on any route; mismatch/dead/wrong-token stay fail-closed."""

    @unittest.skipIf(sys.platform == "win32", "POSIX pty required")
    def test_assign_transient_reader_with_live_control_proceeds_and_audits(self) -> None:
        from loopweave.assignment import assign_task

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            registry, supervisor, run_dir, _pid = _alive_managed_run(root)
            task = root / "task.md"
            task.write_text("# Task\n", encoding="utf-8")
            try:
                assign_task(
                    registry.get_run("run-1"),
                    task,
                    registry=registry,
                    process_start_reader=lambda pid: (_ for _ in ()).throw(
                        RuntimeError("transient ps")
                    ),
                )
                # The verified-alive fallback lets the assignment PROCEED: the
                # exact packet is installed and a liveness_probe_passed audit is
                # recorded; the run is not orphaned.
                self.assertTrue((run_dir / "assigned-task-latest.md").exists())
                self.assertEqual(registry.get_run("run-1").state, RunState.RUNNING)
                self.assertTrue(
                    any(
                        e.get("event") == "liveness_probe_passed"
                        for e in _orphan_events(run_dir)
                    )
                )
            finally:
                supervisor.stop()

    @unittest.skipIf(sys.platform == "win32", "POSIX pty required")
    def test_deliver_review_transient_reader_with_live_control_proceeds_and_audits(
        self,
    ) -> None:
        import loopweave.terminal_host as terminal_host_module
        from loopweave.cli import _deliver_review

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            registry, supervisor, run_dir, _pid = _alive_managed_run(
                root, state=RunState.REVIEW_READY
            )
            (run_dir / "reviewer-verdict.md").write_text("body\n", encoding="utf-8")
            (run_dir / "reviewer-verdict.json").write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "run_id": "run-1",
                        "review_id": "review-1",
                        "verdict": "changes_requested",
                        "summary": "x",
                        "review_file": "reviewer-verdict.md",
                        "continue": True,
                    }
                ),
                encoding="utf-8",
            )
            try:
                with patch.object(
                    terminal_host_module,
                    "default_process_identity_reader",
                    return_value=lambda pid: (_ for _ in ()).throw(RuntimeError("transient")),
                ):
                    _deliver_review(registry, registry.get_run("run-1"))
                # Delivery proceeded to WORKER_CONTINUING (not orphaned) and
                # recorded the liveness_probe_passed audit.
                self.assertEqual(
                    registry.get_run("run-1").state, RunState.WORKER_CONTINUING
                )
                self.assertTrue(
                    any(
                        e.get("event") == "liveness_probe_passed"
                        for e in _orphan_events(run_dir)
                    )
                )
            finally:
                supervisor.stop()

    @unittest.skipIf(sys.platform == "win32", "POSIX pty required")
    def test_takeover_transient_reader_with_live_control_proceeds_past_identity(
        self,
    ) -> None:
        from loopweave.thread_takeover import ThreadTakeoverCoordinator

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            registry, supervisor, run_dir, _pid = _alive_managed_run(root)
            try:
                coordinator = ThreadTakeoverCoordinator(
                    registry,
                    root / "sessions",
                    process_start=lambda pid: (_ for _ in ()).throw(RuntimeError("transient")),
                )
                # attach proceeds past the identity check on the verified-alive
                # fallback (it may fail later at thread discovery, but never with
                # WorkerUnavailable/orphan for a live session).
                try:
                    coordinator.attach("run-1", explicit_thread_id="thread-2")
                except Exception as error:
                    self.assertNotIn(
                        "orphan", str(error).lower() + str(type(error).__name__).lower()
                    )
                self.assertEqual(registry.get_run("run-1").state, RunState.RUNNING)
                self.assertTrue(
                    any(
                        e.get("event") == "liveness_probe_passed"
                        for e in _orphan_events(run_dir)
                    )
                )
            finally:
                supervisor.stop()

    @unittest.skipIf(sys.platform == "win32", "POSIX pty required")
    def test_bridge_transient_reader_with_live_control_skips_orphan_and_audits(
        self,
    ) -> None:
        import loopweave.terminal_host as terminal_host_module
        from loopweave.bridge_control import BridgeController

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            registry, supervisor, run_dir, _pid = _alive_managed_run(root)
            (run_dir / "review-inbox").mkdir(parents=True)
            (run_dir / "review-inbox" / "pending").write_text(
                "review-request-1\n", encoding="utf-8"
            )
            controller = BridgeController(
                root=root, registry=registry, sessions_dir=root / "sessions"
            )
            try:
                with patch.object(
                    terminal_host_module,
                    "default_process_identity_reader",
                    return_value=lambda pid: (_ for _ in ()).throw(RuntimeError("transient")),
                ):
                    controller.reconcile_stale_pending_reviews(["run-1"])
                self.assertEqual(registry.get_run("run-1").state, RunState.RUNNING)
                self.assertFalse(
                    any(
                        e.get("event") == "run_orphaned"
                        for e in _orphan_events(run_dir)
                    )
                )
                self.assertTrue(
                    any(
                        e.get("event") == "liveness_probe_passed"
                        for e in _orphan_events(run_dir)
                    )
                )
            finally:
                supervisor.stop()

    def test_assign_records_single_orphan_event_per_transition(self) -> None:
        from loopweave.assignment import AssignmentError, assign_task

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            registry, run_dir = _synthetic_run(root, state=RunState.RUNNING)
            (root / "control.sock").write_text("", encoding="utf-8")
            task = root / "task.md"
            task.write_text("# Task\n", encoding="utf-8")
            try:
                assign_task(
                    registry.get_run("run-1"),
                    task,
                    registry=registry,
                    process_start_reader=lambda pid: "different-start",
                )
            except AssignmentError:
                pass
            orphan_events = [
                e for e in _orphan_events(run_dir) if e.get("event") == "run_orphaned"
            ]
            self.assertEqual(
                len(orphan_events), 1, "one transition must produce one orphan event"
            )

    @unittest.skipIf(sys.platform == "win32", "POSIX pty required")
    def test_recovery_clear_failure_retry_appends_a_single_recovered_event(
        self,
    ) -> None:
        from loopweave.liveness import audit_orphan, recover_orphaned

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            registry, supervisor, run_dir, _pid = _alive_managed_run(root)
            try:
                audit_orphan(
                    registry,
                    registry.get_run("run-1"),
                    source="reconcile",
                    reason_category="control_unreachable",
                )
                with patch(
                    "loopweave.liveness._clear_orphan_provenance",
                    side_effect=OSError,
                ):
                    try:
                        recover_orphaned(registry, registry.get_run("run-1"))
                    except Exception:
                        pass
                # retry completes the journal; must not append a second recovered.
                recover_orphaned(registry, registry.get_run("run-1"))
                self.assertEqual(
                    len(
                        [
                            e
                            for e in _orphan_events(run_dir)
                            if e.get("event") == "run_recovered"
                        ]
                    ),
                    1,
                )
            finally:
                supervisor.stop()

    @unittest.skipIf(sys.platform == "win32", "POSIX pty required")
    def test_recovery_backfill_failure_aborts_without_clearing(self) -> None:
        from loopweave.liveness import audit_orphan, recover_orphaned

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            registry, supervisor, run_dir, _pid = _alive_managed_run(root)
            try:
                # First, orphan with the event append failing so run_orphaned is
                # missing but provenance is durable.
                with patch("loopweave.liveness.append_event", side_effect=OSError):
                    audit_orphan(
                        registry,
                        registry.get_run("run-1"),
                        source="reconcile",
                        reason_category="control_unreachable",
                    )
                    recover_orphaned(registry, registry.get_run("run-1"))
                self.assertEqual(
                    registry.get_run("run-1").state,
                    RunState.ORPHANED,
                    "a backfill failure must not restore the run",
                )
                self.assertTrue(
                    (run_dir / "orphan-provenance.json").exists(),
                    "provenance must remain when the orphan audit cannot be backfilled",
                )
                self.assertFalse(
                    any(
                        e.get("event") == "run_recovered"
                        for e in _orphan_events(run_dir)
                    )
                )
            finally:
                supervisor.stop()

    @unittest.skipIf(sys.platform == "win32", "POSIX pty required")
    def test_two_orphan_recovery_cycles_produce_distinct_events(self) -> None:
        from loopweave.liveness import audit_orphan, recover_orphaned

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            registry, supervisor, run_dir, _pid = _alive_managed_run(root)
            try:
                for _ in range(2):
                    audit_orphan(
                        registry,
                        registry.get_run("run-1"),
                        source="reconcile",
                        reason_category="control_unreachable",
                    )
                    recover_orphaned(registry, registry.get_run("run-1"))
                orphaned = [
                    e for e in _orphan_events(run_dir) if e.get("event") == "run_orphaned"
                ]
                recovered = [
                    e for e in _orphan_events(run_dir) if e.get("event") == "run_recovered"
                ]
                self.assertEqual(len(orphaned), 2)
                self.assertEqual(len(recovered), 2)
                self.assertEqual(
                    len({e.get("transition_id") for e in orphaned}), 2
                )
            finally:
                supervisor.stop()


class VerdictDeliveryGuardTests(unittest.TestCase):
    @unittest.skipIf(sys.platform == "win32", "POSIX pty required")
    def test_generic_changes_requested_sends_message_then_submit_key(self) -> None:
        from loopweave.cli import _deliver_review
        import loopweave.terminal_host as terminal_host_module

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            registry, supervisor, run_dir, pid = _alive_managed_run(
                root, state=RunState.REVIEW_READY
            )
            (run_dir / "reviewer-verdict.md").write_text("Change the implementation.\n", encoding="utf-8")
            (run_dir / "reviewer-verdict.json").write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "run_id": "run-1",
                        "review_id": "review-1",
                        "verdict": "changes_requested",
                        "summary": "One change required.",
                        "review_file": "reviewer-verdict.md",
                        "continue": True,
                    }
                ),
                encoding="utf-8",
            )
            captured = []

            def capturing(path, payload, timeout=3.0):
                captured.append(payload)
                return {"status": "ok"}

            try:
                with patch.object(
                    terminal_host_module,
                    "default_control_sender",
                    return_value=capturing,
                ), patch.object(
                    terminal_host_module,
                    "default_process_identity_reader",
                    return_value=lambda p: registry.get_run("run-1").agent_process_start,
                ):
                    _deliver_review(registry, registry.get_run("run-1"))
            finally:
                supervisor.stop()
            self.assertEqual(registry.get_run("run-1").agent_pid, pid)
            self.assertGreaterEqual(len(captured), 2)
            self.assertIn("[LoopWeave review: changes_requested]", captured[0]["text"])
            self.assertEqual(captured[1]["text"], "\r")


if __name__ == "__main__":
    unittest.main()
