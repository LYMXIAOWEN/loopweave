from __future__ import annotations

import argparse
import hashlib
import json
import os
import secrets
import shutil
import sys
import time
import uuid
from pathlib import Path
from typing import Dict, Iterable, List, Optional, TextIO

from .adapters import get_adapter
from .adapters.claude import ClaudeAdapter
from .adapters.generic import GenericAdapter
from .assignment import (
    ASSIGNABLE_STATES,
    AssignmentError,
    assign_task,
    resolve_latest_assignable_run,
)
from .bridge_control import (
    BridgeControlError,
    BridgeController,
    VisibleReviewDispatcher,
)
from .bridge_plugin import BridgePluginError, BridgePluginManager
from .codex_sessions import SessionDiscoveryError, discover_thread
from .desktop_ipc import DesktopIpcClient
from .completion_notifier import CompletionNotifier
from .config import (
    CODEX_SESSIONS_DIR,
    SOURCE_ROOT,
    REGISTRY_PATH,
    RUNS_DIR,
    PROJECTS_DIR,
    VAR_DIR,
    ensure_runtime_dirs,
    resolve_codex_bin,
)
from .dispatcher import CodexDispatcher, DispatchError
from .hook_entry import handle_claude_stop, next_review_round
from .models import (
    ReviewBackend,
    RunMode,
    RunRecord,
    RunState,
    StorageState,
    TERMINAL_STATES,
)
from .project_workspace import resolve_project_workspace
from .protocol import (
    ProtocolError,
    append_event,
    read_json,
    utc_now,
    validate_review_request,
    validate_reviewer_verdict,
    write_json_atomic,
)
from .registry import Registry, RunNotFound
from .review_heartbeat import visible_review_heartbeat_status
from .review_policy import apply_review_policy
from . import terminal_host
from .terminal_host import create_terminal_host
from .submission import submit_final, submit_needs_human, submit_stage, SubmissionError
from .task_continuity import TaskContinuityError, adopt_task
from .liveness import (
    audit_orphan,
    authenticated_identity_check,
    reconcile_liveness,
    recover_orphaned,
)
from .maintenance import MaintenanceManager, record_run_end_hint
from .run_governance import (
    GovernanceError,
    RunDecision,
    RunGovernance,
    render_decisions,
)
from .thread_takeover import ThreadTakeoverCoordinator
from .visible_review import (
    create_review_card,
    latest_pending_card,
    queue_visible_review_card,
    render_review_next_instruction,
    submit_visible_review,
    validate_review_card,
)
from .workspace_baseline import capture_workspace_baseline


STOP_VERIFY_TIMEOUT_SECONDS = 2.0
STOP_VERIFY_INTERVAL_SECONDS = 0.1


class LoopWeaveArgumentParser(argparse.ArgumentParser):
    def parse_args(self, args=None, namespace=None):
        values = list(sys.argv[1:] if args is None else args)
        if values[:1] != ["run"] or len(values) < 2:
            return super().parse_args(values, namespace)
        if values[1] in {"-h", "--help"}:
            return super().parse_args(values, namespace)

        agent = values[1]
        bridge_values = values[2:]
        forced_agent_values = []
        if "--" in bridge_values:
            separator = bridge_values.index("--")
            forced_agent_values = bridge_values[separator + 1 :]
            bridge_values = bridge_values[:separator]

        thread = None
        cwd = os.getcwd()
        cwd_explicit = False
        project = None
        workspace = None
        mode = RunMode.DEVELOP.value
        reviewer = ReviewBackend.EPHEMERAL.value
        task_file = None
        continue_run = None
        agent_args = []
        index = 0
        while index < len(bridge_values):
            value = bridge_values[index]
            if value == "--task-file":
                index += 1
                if index >= len(bridge_values):
                    self.error("--task-file requires a value")
                task_file = bridge_values[index]
            elif value.startswith("--task-file="):
                task_file = value.split("=", 1)[1]
            elif value == "--continue-run":
                index += 1
                if index >= len(bridge_values):
                    self.error("--continue-run requires a value")
                continue_run = bridge_values[index]
            elif value.startswith("--continue-run="):
                continue_run = value.split("=", 1)[1]
            elif value == "--thread":
                index += 1
                if index >= len(bridge_values):
                    self.error("--thread requires a value")
                thread = bridge_values[index]
            elif value.startswith("--thread="):
                thread = value.split("=", 1)[1]
            elif value == "--cwd":
                index += 1
                if index >= len(bridge_values):
                    self.error("--cwd requires a value")
                cwd = bridge_values[index]
                cwd_explicit = True
            elif value.startswith("--cwd="):
                cwd = value.split("=", 1)[1]
                cwd_explicit = True
            elif value == "--project":
                index += 1
                if index >= len(bridge_values):
                    self.error("--project requires a value")
                project = bridge_values[index]
            elif value.startswith("--project="):
                project = value.split("=", 1)[1]
            elif value == "--workspace":
                index += 1
                if index >= len(bridge_values):
                    self.error("--workspace requires a value")
                workspace = bridge_values[index]
            elif value.startswith("--workspace="):
                workspace = value.split("=", 1)[1]
            elif value == "--mode":
                index += 1
                if index >= len(bridge_values):
                    self.error("--mode requires a value")
                mode = bridge_values[index]
            elif value.startswith("--mode="):
                mode = value.split("=", 1)[1]
            elif value == "--reviewer":
                index += 1
                if index >= len(bridge_values):
                    self.error("--reviewer requires a value")
                reviewer = bridge_values[index]
            elif value.startswith("--reviewer="):
                reviewer = value.split("=", 1)[1]
            else:
                agent_args.append(value)
            index += 1
        try:
            RunMode(mode)
        except ValueError:
            self.error("--mode must be one of: develop, design")
        try:
            ReviewBackend(reviewer)
        except ValueError:
            self.error("--reviewer must be one of: ephemeral, visible-thread")
        agent_args.extend(forced_agent_values)

        parsed = namespace or argparse.Namespace()
        parsed.command = "run"
        parsed.agent = agent
        parsed.thread = thread
        parsed.cwd = cwd
        parsed.cwd_explicit = cwd_explicit
        parsed.project = project
        parsed.workspace = workspace
        parsed.mode = mode
        parsed.reviewer = reviewer
        parsed.task_file = task_file
        parsed.continue_run = continue_run
        if task_file and continue_run:
            self.error("--task-file and --continue-run are mutually exclusive")
        parsed.agent_args = agent_args
        return parsed


def build_parser() -> argparse.ArgumentParser:
    parser = LoopWeaveArgumentParser(prog="loopweave")
    subparsers = parser.add_subparsers(dest="command", required=True)

    run_parser = subparsers.add_parser("run", help="start a managed Agent")
    run_parser.add_argument("agent", nargs="?")
    run_parser.add_argument("--thread")
    run_parser.add_argument("--cwd", default=os.getcwd())
    run_parser.add_argument("--project")
    run_parser.add_argument("--workspace")
    run_parser.add_argument(
        "--mode",
        choices=[mode.value for mode in RunMode],
        default=RunMode.DEVELOP.value,
    )
    run_parser.add_argument(
        "--reviewer",
        choices=[backend.value for backend in ReviewBackend],
        default=ReviewBackend.EPHEMERAL.value,
    )
    run_parser.add_argument("agent_args", nargs="*", default=[])
    run_task_group = run_parser.add_mutually_exclusive_group()
    run_task_group.add_argument("--task-file", dest="task_file", default=None)
    run_task_group.add_argument(
        "--continue-run",
        dest="continue_run",
        default=None,
        help="adopt the verified task packet from a compatible earlier run",
    )

    status_parser = subparsers.add_parser("status", help="show one run")
    status_parser.add_argument("run_id", nargs="?")

    recover_parser = subparsers.add_parser(
        "recover",
        help="recover an exact live false-positive orphaned run",
    )
    recover_parser.add_argument("run_id")
    recover_parser.add_argument(
        "--json",
        action="store_true",
        help="emit a machine-readable JSON object",
    )
    status_parser.add_argument(
        "--json",
        action="store_true",
        help="emit a single machine-readable JSON object instead of the text table",
    )

    runs_parser = subparsers.add_parser("runs", help="list runs")
    runs_parser.add_argument(
        "--json",
        action="store_true",
        help="emit a machine-readable JSON array instead of the text table",
    )
    runs_scope = runs_parser.add_mutually_exclusive_group()
    runs_scope.add_argument(
        "--all",
        action="store_true",
        help="include hot, archived, ledger-only, and recovery records",
    )
    runs_scope.add_argument(
        "--archived",
        action="store_true",
        help="show archived and ledger-only runs",
    )

    attach_parser = subparsers.add_parser(
        "attach", help="move a live run to another Codex thread"
    )
    attach_parser.add_argument("run_id")
    attach_parser.add_argument("--thread")

    stop_parser = subparsers.add_parser("stop", help="stop a managed Agent")
    stop_parser.add_argument("run_id", nargs="?")

    deliver_parser = subparsers.add_parser(
        "deliver", help="deliver LoopWeave review to the managed Agent"
    )
    deliver_parser.add_argument("--run-id", required=True)

    request_parser = subparsers.add_parser(
        "request-review", help="manually request LoopWeave review"
    )
    request_parser.add_argument("--run-id", required=True)
    request_parser.add_argument(
        "--summary", default="The worker requested a manual review."
    )
    scope_group = request_parser.add_mutually_exclusive_group()
    scope_group.add_argument(
        "--stage",
        action="store_const",
        dest="completion_scope",
        const="stage",
        help="approve this review as an intermediate stage",
    )
    scope_group.add_argument(
        "--final",
        action="store_const",
        dest="completion_scope",
        const="final",
        help="approve this review as final completion",
    )
    request_parser.set_defaults(completion_scope=None)

    finalize_parser = subparsers.add_parser(
        "finalize", help="record owner global final review for a run"
    )
    finalize_parser.add_argument("--run-id", required=True)
    verdict_group = finalize_parser.add_mutually_exclusive_group(required=True)
    verdict_group.add_argument(
        "--approve",
        action="store_true",
        help="approve the pending final review and complete the task",
    )
    verdict_group.add_argument(
        "--changes-requested",
        action="store_true",
        help="return the pending final review to the worker for revision",
    )
    message_group = finalize_parser.add_mutually_exclusive_group()
    message_group.add_argument("--message", default="")
    message_group.add_argument("--message-file")

    review_next_parser = subparsers.add_parser(
        "review-next", help="show next visible review card"
    )
    review_next_parser.add_argument("--run-id")

    review_submit_parser = subparsers.add_parser(
        "review-submit", help="submit visible LoopWeave review"
    )
    review_submit_parser.add_argument("--run-id")
    review_submit_parser.add_argument("--review-file", required=True)

    review_heartbeat_parser = subparsers.add_parser(
        "review-heartbeat", help="check armed visible review handoff"
    )
    review_heartbeat_parser.add_argument(
        "--json",
        action="store_true",
        help="emit a machine-readable heartbeat status object",
    )

    bridge_parser = subparsers.add_parser(
        "bridge", help="manage the Codex Desktop visible-review bridge"
    )
    bridge_subparsers = bridge_parser.add_subparsers(
        dest="bridge_command",
        required=True,
    )
    bridge_install = bridge_subparsers.add_parser("install")
    bridge_install.add_argument("--dry-run", action="store_true")
    bridge_install.add_argument("--json", action="store_true")
    bridge_bind = bridge_subparsers.add_parser(
        "bind", help="bind the bridge to one visible Codex task"
    )
    bridge_bind.add_argument("--thread", required=True)
    bridge_bind.add_argument("--run-id")
    bridge_status = bridge_subparsers.add_parser("status")
    bridge_status.add_argument("--json", action="store_true")
    bridge_doctor = bridge_subparsers.add_parser("doctor")
    bridge_doctor.add_argument("--json", action="store_true")
    bridge_subparsers.add_parser("unbind")
    bridge_uninstall = bridge_subparsers.add_parser("uninstall")
    bridge_uninstall.add_argument("--dry-run", action="store_true")
    bridge_uninstall.add_argument("--json", action="store_true")

    reviewer_parser = subparsers.add_parser(
        "reviewer", help="manage visible reviewer binding"
    )
    reviewer_subparsers = reviewer_parser.add_subparsers(
        dest="reviewer_command",
        required=True,
    )
    reviewer_bind_parser = reviewer_subparsers.add_parser(
        "bind", help="bind visible reviewer thread"
    )
    reviewer_bind_parser.add_argument("--run-id", required=True)
    reviewer_bind_parser.add_argument("--thread")

    assign_parser = subparsers.add_parser(
        "assign", help="send a task packet to a live managed Agent"
    )
    assign_target = assign_parser.add_mutually_exclusive_group(required=True)
    assign_target.add_argument("--run-id")
    assign_target.add_argument(
        "--latest",
        action="store_true",
        help="select the only live assignable run",
    )
    assign_parser.add_argument("--task-file", required=True)
    assign_parser.add_argument(
        "--redeliver",
        action="store_true",
        help="re-send the same verified packet after the terminal is ready",
    )

    adopt_parser = subparsers.add_parser(
        "adopt-task",
        help="install a verified task packet from a compatible earlier run",
    )
    adopt_parser.add_argument("--run-id", required=True)
    adopt_parser.add_argument("--from-run", required=True)

    archive_parser = subparsers.add_parser(
        "archive", help="archive one dead, unprotected run"
    )
    archive_parser.add_argument("run_id")
    archive_parser.add_argument("--reason", default="manual archive")

    restore_parser = subparsers.add_parser(
        "restore", help="restore one verified archive into the hot run root"
    )
    restore_parser.add_argument("run_id")
    restore_parser.add_argument("--reason", default="manual restore")

    pin_parser = subparsers.add_parser(
        "pin", help="protect one run from automatic storage actions"
    )
    pin_parser.add_argument("run_id")
    pin_parser.add_argument("--reason", default="manual pin")

    unpin_parser = subparsers.add_parser(
        "unpin", help="remove an explicit run pin"
    )
    unpin_parser.add_argument("run_id")

    gc_parser = subparsers.add_parser(
        "gc", help="plan or apply lifecycle retention actions"
    )
    gc_action = gc_parser.add_mutually_exclusive_group(required=True)
    gc_action.add_argument("--dry-run", action="store_true")
    gc_action.add_argument("--apply", action="store_true")
    gc_parser.add_argument("--json", action="store_true")
    gc_parser.add_argument(
        "--plan",
        help="apply an exact persisted plan instead of gc-plan-latest.json",
    )

    maintenance_parser = subparsers.add_parser(
        "maintenance", help="manage daily one-shot lifecycle maintenance"
    )
    maintenance_subparsers = maintenance_parser.add_subparsers(
        dest="maintenance_command",
        required=True,
    )
    maintenance_subparsers.add_parser("install")
    maintenance_subparsers.add_parser("status")
    maintenance_run = maintenance_subparsers.add_parser("run")
    maintenance_run.add_argument("--scheduled", action="store_true")
    maintenance_subparsers.add_parser("uninstall")

    hook_parser = subparsers.add_parser("hook", help="internal Agent hook entry")
    hook_parser.add_argument("hook_name", choices=["claude-stop"])
    hook_parser.add_argument("--run-id", required=True)

    submit_parser = subparsers.add_parser(
        "submit", help="submit stage/final/needs-human from inside a managed session"
    )
    submit_scope_group = submit_parser.add_mutually_exclusive_group(required=True)
    submit_scope_group.add_argument("--stage", action="store_true")
    submit_scope_group.add_argument("--final", action="store_true")
    submit_scope_group.add_argument(
        "--needs-human", dest="needs_human", action="store_true"
    )
    submit_parser.add_argument("--run-id", default=None)
    submit_parser.add_argument("--summary-file", default=None)
    submit_parser.add_argument("--message-file", default=None)
    submit_parser.add_argument("--evidence-file", default=None)

    subparsers.add_parser("doctor", help="check local integration health")
    return parser


def render_runs(runs: Iterable[RunRecord]) -> str:
    rows = ["RUN ID\tAGENT\tSTATE\tPID\tGEN\tTHREAD\tPENDING"]
    for run in runs:
        rows.append(
            "{}\t{}\t{}\t{}\t{}\t{}\t{}".format(
                run.run_id,
                run.agent,
                run.state.value,
                run.agent_pid,
                run.binding_generation,
                run.codex_thread_id,
                run.pending_codex_thread_id or "-",
            )
        )
    return "\n".join(rows)


def render_status_json(run: RunRecord) -> str:
    return json.dumps(_status_payload(run))


def render_runs_json(runs: Iterable[RunRecord]) -> str:
    return json.dumps([_status_payload(run) for run in runs])


def _governed_payload(run: RunRecord, decision: RunDecision) -> Dict[str, object]:
    payload = _status_payload(run)
    payload.update(
        {
            "storage_state": decision.storage_state,
            "governance_action": decision.action,
            "protection_reasons": list(decision.reasons),
            "size_bytes": decision.size_bytes,
            "last_activity": decision.last_activity,
        }
    )
    return payload


def render_governed_runs(
    runs: Iterable[RunRecord], decisions: Dict[str, RunDecision]
) -> str:
    rows = [
        "RUN ID\tAGENT\tRUN STATE\tSTORAGE\tACTION\tSIZE\tPROTECTION / REASON"
    ]
    for run in runs:
        decision = decisions[run.run_id]
        rows.append(
            "{}\t{}\t{}\t{}\t{}\t{}\t{}".format(
                run.run_id,
                run.agent,
                run.state.value,
                decision.storage_state,
                decision.action,
                decision.size_bytes,
                ", ".join(decision.reasons) or "-",
            )
        )
    return "\n".join(rows)


def _status_payload(run: RunRecord) -> Dict[str, object]:
    task_packet = Path(run.run_dir) / "assigned-task-latest.md"
    return {
        "run_id": run.run_id,
        "agent": run.agent,
        "state": run.state.value,
        "agent_pid": run.agent_pid,
        "project_slug": run.project_slug,
        "project_root": run.project_root,
        "workspace_root": run.workspace_root,
        "thread_cwd": run.thread_cwd,
        "codex_thread_id": run.codex_thread_id,
        "pending_codex_thread_id": run.pending_codex_thread_id,
        "binding_generation": run.binding_generation,
        "review_loop": run.review_loop,
        "mode": run.mode.value,
        "reviewer_backend": run.reviewer_backend.value,
        "reviewer_thread_id": run.reviewer_thread_id,
        "reviewer_thread_cwd": run.reviewer_thread_cwd,
        "reviewer_generation": run.reviewer_generation,
        "task_assignment": (
            "assigned" if task_packet.exists() else "awaiting task assignment"
        ),
    }


def doctor_checks(
    output: TextIO,
    commands: Optional[Dict[str, str]] = None,
) -> bool:
    if commands is None:
        try:
            codex = str(resolve_codex_bin())
        except RuntimeError:
            codex = ""
    checks = commands or {
        "python3": shutil.which("python3") or "",
        "codex": codex,
        "claude": shutil.which("claude") or "",
    }
    healthy = True
    for name, path in checks.items():
        exists = bool(path and Path(path).exists())
        output.write(
            "{}: {}{}\n".format(
                name,
                "ok" if exists else "missing",
                " ({})".format(path) if path else "",
            )
        )
        healthy = healthy and exists
    output.write(
        "sessions: {} ({})\n".format(
            "ok" if CODEX_SESSIONS_DIR.exists() else "missing",
            CODEX_SESSIONS_DIR,
        )
    )
    return healthy and CODEX_SESSIONS_DIR.exists()


def _registry() -> Registry:
    ensure_runtime_dirs()
    return Registry(REGISTRY_PATH)


def _governance(registry: Registry) -> RunGovernance:
    return RunGovernance(registry)


def _latest_active_run(registry: Registry) -> RunRecord:
    for run in registry.list_runs():
        if run.state not in TERMINAL_STATES:
            return run
    raise RunNotFound("no active run")


def _pending_visible_review_run(
    registry: Registry,
    run_id: Optional[str] = None,
) -> RunRecord:
    if run_id:
        return registry.get_run(run_id)
    candidates = []
    for run in registry.list_runs():
        if run.reviewer_backend is not ReviewBackend.VISIBLE_THREAD:
            continue
        pending_path = Path(run.run_dir) / "review-inbox" / "pending"
        if pending_path.exists():
            candidates.append(run)
    if not candidates:
        raise RunNotFound("no pending visible review")
    if len(candidates) > 1:
        raise ProtocolError(
            "multiple pending visible reviews; pass --run-id: {}".format(
                ", ".join(run.run_id for run in candidates)
            )
        )
    return candidates[0]


def _adapter_for_run(run: RunRecord):
    if run.agent == "claude":
        return ClaudeAdapter(run_id=run.run_id)
    return GenericAdapter([run.agent])


def _manual_visible_plan_path(run: RunRecord) -> Path:
    run_dir = Path(run.run_dir)
    if run.project_root:
        candidate = Path(run.project_root) / "project.json"
        if candidate.exists():
            return candidate
    return run_dir / "run.json"


def _takeover_coordinator(registry: Registry) -> ThreadTakeoverCoordinator:
    return ThreadTakeoverCoordinator(registry, CODEX_SESSIONS_DIR)


def _bridge_controller(registry: Registry) -> BridgeController:
    return BridgeController(
        root=VAR_DIR.parent,
        registry=registry,
        sessions_dir=CODEX_SESSIONS_DIR,
    )


def _bridge_plugin_manager() -> BridgePluginManager:
    return BridgePluginManager(
        codex_bin=resolve_codex_bin(),
        marketplace_root=SOURCE_ROOT,
    )


def _process_identity_is_still_live(run: RunRecord) -> bool:
    try:
        return terminal_host.default_process_identity_reader()(run.agent_pid) == run.agent_process_start
    except Exception:
        return False


def _stop_run(
    registry: Registry,
    run: RunRecord,
    *,
    timeout: Optional[float] = None,
) -> str:
    verify_timeout = (
        STOP_VERIFY_TIMEOUT_SECONDS if timeout is None else timeout
    )
    response = terminal_host.default_control_sender()(
        Path(run.socket_path),
        {"token": run.control_token, "action": "stop"},
    )
    status = str(response.get("status", "error"))
    if status != "ok":
        return status

    deadline = time.monotonic() + verify_timeout
    while True:
        if not _process_identity_is_still_live(run):
            if run.state not in TERMINAL_STATES:
                registry.force_state(run.run_id, RunState.STOPPED)
            return "ok"
        if time.monotonic() >= deadline:
            break
        time.sleep(STOP_VERIFY_INTERVAL_SECONDS)
    raise RuntimeError(
        "managed Agent process is still running after stop: {}".format(
            run.run_id
        )
    )


def _manual_completion_scope(
    mode: RunMode,
    requested_scope: Optional[str],
) -> str:
    if requested_scope is not None:
        return requested_scope
    return "final" if mode is RunMode.DESIGN else "stage"


def _approved_review_is_stage(run: RunRecord, run_dir: Path) -> bool:
    if run.mode is not RunMode.DEVELOP:
        return False
    request_path = run_dir / "review-request.json"
    if not request_path.exists():
        return _visible_review_completion_scope(run, run_dir) == "stage"
    request = validate_review_request(read_json(request_path))
    if request["run_id"] != run.run_id:
        raise RuntimeError("review request run_id does not match managed run")
    return request.get("completion_scope", "final") == "stage"


def _visible_review_completion_scope(
    run: RunRecord,
    run_dir: Path,
) -> Optional[str]:
    inbox = run_dir / "review-inbox"
    if not inbox.exists():
        return None
    cards = sorted(
        inbox.glob("review-request-*.json"),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    for card_path in cards:
        card = validate_review_card(read_json(card_path))
        if card["run_id"] == run.run_id:
            return str(card["completion_scope"])
    return None


def _explicit_thread_id(args: argparse.Namespace) -> Optional[str]:
    return (
        args.thread
        or os.environ.get("CODEX_THREAD_ID")
        or os.environ.get("LOOPWEAVE_THREAD_ID")
    )


def _finalize_message(args: argparse.Namespace) -> str:
    if args.message_file:
        return Path(args.message_file).expanduser().read_text(encoding="utf-8")
    return args.message or ""


def _deliver_review(
    registry: Registry,
    run: RunRecord,
    completion_notifier: Optional[CompletionNotifier] = None,
) -> None:
    run_dir = Path(run.run_dir).resolve()
    review = validate_reviewer_verdict(read_json(run_dir / "reviewer-verdict.json"))
    if review["run_id"] != run.run_id:
        raise RuntimeError("review run_id does not match managed run")
    if review["verdict"] == "approved":
        should_continue = _approved_review_is_stage(run, run_dir)
        if review["continue"] != should_continue:
            review = dict(review)
            review["continue"] = should_continue
            write_json_atomic(run_dir / "reviewer-verdict.json", review)
    delivered_path = run_dir / "delivered-review-id"
    already_delivered = (
        delivered_path.exists()
        and delivered_path.read_text(encoding="utf-8").strip()
        == review["review_id"]
    )
    review_path = (run_dir / review["review_file"]).resolve()
    if review_path != run_dir and run_dir not in review_path.parents:
        raise RuntimeError("review_file must stay inside the run directory")
    review_text = review_path.read_text(encoding="utf-8")
    if review["verdict"] == "approved" and not review["continue"]:
        registry.force_state(run.run_id, RunState.OWNER_REVIEW_PENDING)
        if not already_delivered:
            delivered_path.write_text(
                review["review_id"] + "\n", encoding="utf-8"
            )
            append_event(
                run_dir / "events.jsonl",
                {
                    "event": "review_resolved",
                    "review_id": review["review_id"],
                    "verdict": "approved",
                },
            )
        notifier = completion_notifier or CompletionNotifier()
        notifier.notify_owner(run, review)
        return
    if already_delivered:
        return
    if review["verdict"] in {"needs_human", "failed"}:
        registry.force_state(
            run.run_id,
            RunState.NEEDS_HUMAN
            if review["verdict"] == "needs_human"
            else RunState.FAILED,
        )
        delivered_path.write_text(review["review_id"] + "\n", encoding="utf-8")
        return
    verdict = authenticated_identity_check(
        registry,
        run,
        reader=terminal_host.default_process_identity_reader(),
        sender=terminal_host.default_control_sender(),
        source="deliver",
    )
    if verdict == "orphaned":
        raise RuntimeError("managed Agent process identity changed")
    registry.force_state(run.run_id, RunState.DELIVERING)
    adapter = _adapter_for_run(run)
    if review["verdict"] == "approved":
        inputs = adapter.stage_approved_input_sequence(str(review["summary"]))
    else:
        inputs = adapter.review_input_sequence(review["verdict"], review_text)
    for index, input_text in enumerate(inputs):
        if index:
            time.sleep(0.35)
        response = terminal_host.default_control_sender()(
            Path(run.socket_path),
            {
                "token": run.control_token,
                "action": "send",
                "text": input_text,
            },
        )
        if response.get("status") != "ok":
            audit_orphan(
                registry, run, source="deliver", reason_category="control_unreachable"
            )
            raise RuntimeError(response.get("message", "review delivery failed"))
    registry.force_state(run.run_id, RunState.WORKER_CONTINUING)
    delivered_path.write_text(review["review_id"] + "\n", encoding="utf-8")
    append_event(
        Path(run.run_dir) / "events.jsonl",
        {"event": "review_delivered", "review_id": review["review_id"]},
    )


def _resolve_review(registry: Registry, run: RunRecord) -> None:
    try:
        _deliver_review(registry, run)
    finally:
        _takeover_coordinator(registry).reconcile_pending(run.run_id)


def _owner_changes_requested_input_sequence(message: str) -> List[str]:
    body = message.strip()
    text = (
        "\n[Owner final review: changes requested]\n"
        "{}\n\n"
        "Continue in this same managed session. Address this global final "
        "review, preserve unrelated work, and submit LOOPWEAVE_FINAL again "
        "only when the full task is ready for another final review.\n"
    ).format(body)
    return [text, "\r"]


def _write_owner_final_verdict(
    run: RunRecord,
    review: Dict[str, object],
    verdict: str,
    message: str,
) -> None:
    run_dir = Path(run.run_dir)
    write_json_atomic(
        run_dir / "owner-final-verdict.json",
        {
            "schema_version": 1,
            "run_id": run.run_id,
            "review_id": review["review_id"],
            "verdict": verdict,
            "message": message.strip(),
            "recorded_at": utc_now(),
        },
    )


def _record_owner_final_delivery_failure(
    run: RunRecord,
    review: Dict[str, object],
    error: Exception,
) -> None:
    run_dir = Path(run.run_dir)
    (run_dir / "owner-final-delivery-error.txt").write_text(
        "owner_final_delivery_failed: {}\n".format(error),
        encoding="utf-8",
    )
    append_event(
        run_dir / "events.jsonl",
        {
            "event": "owner_final_delivery_failed",
            "review_id": review["review_id"],
            "error": str(error),
        },
    )


def _finalize_owner_review(
    registry: Registry,
    run: RunRecord,
    *,
    approved: bool,
    message: str,
    completion_notifier: Optional[CompletionNotifier] = None,
) -> None:
    if run.state is not RunState.OWNER_REVIEW_PENDING:
        raise ValueError(
            "run {} is not awaiting owner review".format(run.run_id)
        )
    run_dir = Path(run.run_dir).resolve()
    review = validate_reviewer_verdict(read_json(run_dir / "reviewer-verdict.json"))
    if review["run_id"] != run.run_id:
        raise RuntimeError("review run_id does not match managed run")
    if review["verdict"] != "approved" or review["continue"]:
        raise ValueError("only final approved reviews can be finalized")

    if approved:
        adapter = _adapter_for_run(run)
        notifier = completion_notifier or CompletionNotifier()
        worker_notified = notifier.notify_worker(
            run,
            review,
            adapter.approved_input_sequence(str(review["summary"])),
        )
        if not worker_notified:
            raise RuntimeError("worker final approval notification failed")
        _write_owner_final_verdict(
            run,
            review,
            "approved",
            message or "Owner global review approved.",
        )
        registry.force_state(run.run_id, RunState.APPROVED)
        append_event(
            run_dir / "events.jsonl",
            {
                "event": "owner_finalized",
                "review_id": review["review_id"],
                "verdict": "approved",
            },
        )
        return

    if not message.strip():
        raise ValueError("--message or --message-file is required for changes")

    try:
        current_process_start = terminal_host.default_process_identity_reader()(run.agent_pid)
    except Exception as error:
        _record_owner_final_delivery_failure(run, review, error)
        raise RuntimeError("managed Agent process is no longer available") from error
    if not registry.process_identity_matches(
        run.run_id, run.agent_pid, current_process_start
    ):
        error = RuntimeError("managed Agent process identity changed")
        _record_owner_final_delivery_failure(run, review, error)
        raise error

    registry.force_state(run.run_id, RunState.DELIVERING)
    for index, input_text in enumerate(
        _owner_changes_requested_input_sequence(message)
    ):
        if index:
            time.sleep(0.35)
        response = terminal_host.default_control_sender()(
            Path(run.socket_path),
            {
                "token": run.control_token,
                "action": "send",
                "text": input_text,
            },
        )
        if response.get("status") != "ok":
            registry.force_state(run.run_id, RunState.OWNER_REVIEW_PENDING)
            error = RuntimeError(
                response.get("message", "owner review delivery failed")
            )
            _record_owner_final_delivery_failure(run, review, error)
            raise error
    _write_owner_final_verdict(
        run,
        review,
        "changes_requested",
        message,
    )
    registry.force_state(run.run_id, RunState.WORKER_CONTINUING)
    append_event(
        run_dir / "events.jsonl",
        {
            "event": "owner_finalized",
            "review_id": review["review_id"],
            "verdict": "changes_requested",
        },
    )


def _dispatch_and_deliver(registry: Registry, run: RunRecord) -> None:
    registry.force_state(run.run_id, RunState.REVIEWING)
    reviewing = registry.get_run(run.run_id)
    request = validate_review_request(
        read_json(Path(reviewing.run_dir) / "review-request.json")
    )
    CodexDispatcher().dispatch(
        reviewing.codex_thread_id,
        reviewing.run_id,
        Path(reviewing.run_dir),
        workspace_root=Path(reviewing.workspace_root),
        project_root=(
            Path(reviewing.project_root) if reviewing.project_root else None
        ),
        binding_generation=reviewing.binding_generation,
        mode=reviewing.mode,
        review_round=int(request.get("review_round", reviewing.review_loop)),
    )
    run_dir = Path(reviewing.run_dir)
    review = validate_reviewer_verdict(read_json(run_dir / "reviewer-verdict.json"))
    review_path = (run_dir / review["review_file"]).resolve()
    review_markdown = review_path.read_text(encoding="utf-8")
    resolved = apply_review_policy(
        reviewing.mode,
        int(request.get("review_round", reviewing.review_loop)),
        review,
        str(request.get("evidence_fingerprint", "")),
        review_markdown,
        run_dir / "review-policy-state.json",
    )
    if (
        reviewing.mode is RunMode.DEVELOP
        and resolved["verdict"] == "approved"
    ):
        resolved["continue"] = request.get("completion_scope", "final") == "stage"
    if resolved["verdict"] != review["verdict"]:
        append_event(
            run_dir / "events.jsonl",
            {
                "event": "review_policy_canonicalized",
                "run_id": reviewing.run_id,
                "from_verdict": review["verdict"],
                "to_verdict": resolved["verdict"],
                "mode": reviewing.mode.value,
            },
        )
    write_json_atomic(run_dir / "reviewer-verdict.json", resolved)
    registry.force_state(run.run_id, RunState.REVIEW_READY)
    _resolve_review(registry, registry.get_run(run.run_id))


def _run_agent(args: argparse.Namespace) -> int:
    ensure_runtime_dirs()
    registry = _registry()
    run_mode = RunMode(args.mode)
    reviewer_backend = ReviewBackend(
        getattr(args, "reviewer", ReviewBackend.EPHEMERAL.value)
    )
    invocation_cwd = Path(os.getcwd()).resolve()
    if args.project:
        workspace_value = args.workspace
        if workspace_value is None and getattr(args, "cwd_explicit", False):
            workspace_value = args.cwd
        binding = resolve_project_workspace(
            PROJECTS_DIR,
            args.project,
            Path(workspace_value) if workspace_value else None,
        )
        thread_cwd = invocation_cwd
        workspace_root = binding.workspace_root
        project_slug = binding.project_slug
        project_root = binding.project_root
    else:
        if args.workspace:
            raise ValueError("--workspace requires --project")
        legacy_cwd = Path(args.cwd).resolve()
        thread_cwd = legacy_cwd
        workspace_root = legacy_cwd
        project_slug = None
        project_root = None
    explicit_thread = _explicit_thread_id(args)
    thread = discover_thread(
        CODEX_SESSIONS_DIR,
        cwd=str(thread_cwd),
        explicit_thread_id=explicit_thread,
    )
    thread_cwd = Path(thread.cwd).resolve()
    bridge_binding = None
    if reviewer_backend is ReviewBackend.VISIBLE_THREAD:
        bridge_binding = _bridge_controller(registry).preflight(thread.thread_id)
    run_id = "run-" + uuid.uuid4().hex[:12]
    run_dir = RUNS_DIR / run_id
    run_dir.mkdir(parents=True, mode=0o700)
    os.chmod(run_dir, 0o700)
    socket_path = VAR_DIR / "{}.sock".format(run_id)
    control_token = secrets.token_urlsafe(32)
    baseline_path = run_dir / "workspace-baseline.json"
    write_json_atomic(
        baseline_path,
        capture_workspace_baseline(workspace_root),
    )

    if args.agent in ("generic", "--"):
        command = list(args.agent_args)
        # Strip a leading "--" separator that argparse may have retained for
        # the explicit-generic spelling. Both `loopweave run generic -- <cmd>`
        # (the main()-rewritten form of `loopweave run -- <cmd>`) and a direct
        # `run -- <cmd>` sentinel reach this branch.
        if command and command[0] == "--":
            command = command[1:]
        if not command:
            raise ValueError(
                "loopweave run requires a command; pass an agent name "
                "(`loopweave run <agent>`) or an explicit command "
                "(`loopweave run -- <command> <args>`)"
            )
        adapter = GenericAdapter(command)
        agent_name = command[0]
    else:
        if not args.agent:
            raise ValueError("agent name is required")
        extra_args = list(args.agent_args)
        if extra_args and extra_args[0] == "--":
            extra_args = extra_args[1:]
        adapter = get_adapter(
            args.agent,
            extra_args,
            run_id=run_id,
            workspace_root=workspace_root,
            baseline_path=baseline_path,
        )
        agent_name = args.agent

    supervisor = create_terminal_host(
        run_id=run_id,
        command=adapter.build_command(run_dir),
        cwd=workspace_root,
        run_dir=run_dir,
        socket_path=socket_path,
        control_token=control_token,
        passthrough=True,
    )
    pid = supervisor.start()
    record_created = False
    try:
        tty_path = os.ttyname(sys.stdin.fileno()) if sys.stdin.isatty() else ""
        record = RunRecord(
            run_id=run_id,
            codex_thread_id=thread.thread_id,
            cwd=str(thread_cwd),
            thread_cwd=str(thread_cwd),
            workspace_root=str(workspace_root),
            project_slug=project_slug,
            project_root=str(project_root) if project_root else None,
            tty=tty_path,
            agent=agent_name,
            agent_pid=pid,
            agent_process_start=terminal_host.default_process_identity_reader()(pid),
            control_token=control_token,
            state=RunState.RUNNING,
            mode=run_mode,
            reviewer_backend=reviewer_backend,
            reviewer_thread_id=(
                thread.thread_id
                if reviewer_backend is ReviewBackend.VISIBLE_THREAD
                else None
            ),
            reviewer_thread_cwd=(
                str(thread_cwd)
                if reviewer_backend is ReviewBackend.VISIBLE_THREAD
                else None
            ),
            reviewer_generation=(
                bridge_binding.generation if bridge_binding is not None else 1
            ),
            socket_path=str(socket_path),
            run_dir=str(run_dir),
        )
        registry.create_run(record)
        record_created = True
        write_json_atomic(
            run_dir / "run.json",
            {
                "schema_version": 1,
                "run_id": run_id,
                "codex_thread_id": thread.thread_id,
                "cwd": str(thread_cwd),
                "thread_cwd": str(thread_cwd),
                "workspace_root": str(workspace_root),
                "project_slug": project_slug,
                "project_root": str(project_root) if project_root else None,
                "tty": tty_path,
                "agent": agent_name,
                "agent_pid": pid,
                "agent_process_start": record.agent_process_start,
                "socket_path": str(socket_path),
                "mode": run_mode.value,
                "reviewer_backend": reviewer_backend.value,
                "reviewer_thread_id": record.reviewer_thread_id,
                "reviewer_thread_cwd": record.reviewer_thread_cwd,
                "reviewer_generation": record.reviewer_generation,
            },
        )
        if project_root:
            write_json_atomic(
                project_root / "process-artifacts" / "{}.json".format(run_id),
                {
                    "schema_version": 1,
                    "run_id": run_id,
                    "run_dir": str(run_dir),
                    "workspace_root": str(workspace_root),
                },
            )
        append_event(
            run_dir / "events.jsonl",
            {"event": "run_started", "pid": pid, "mode": run_mode.value},
        )
        task_file = getattr(args, "task_file", None)
        continue_run = getattr(args, "continue_run", None)
        if task_file:
            # Deterministic startup: the child is already spawned (supervisor
            # started above); install the run-scoped task packet and deliver
            # the assignment BEFORE run_foreground, so the run record and exact
            # packet exist before the assignment is delivered as the child's
            # readiness signal.
            assign_task(
                registry.get_run(run_id),
                Path(task_file),
                registry=registry,
            )
        elif continue_run:
            adopt_task(
                registry,
                run_id,
                continue_run,
                operator_action="loopweave run --continue-run",
            )
    except BaseException:
        supervisor.stop()
        if record_created:
            try:
                current = registry.get_run(run_id)
                if current.state not in TERMINAL_STATES:
                    registry.force_state(run_id, RunState.FAILED)
                append_event(
                    run_dir / "events.jsonl",
                    {
                        "event": "run_start_failed",
                        "run_id": run_id,
                        "reason": "startup_initialization_failed",
                    },
                )
                record_run_end_hint(
                    run_id, RUNS_DIR.parent / "maintenance"
                )
            except Exception:
                # Preserve the original startup failure. The registered run
                # remains fail-closed and later liveness reconciliation can
                # still prove that its managed process is gone.
                pass
        raise
    exit_code = 1
    try:
        exit_code = supervisor.run_foreground()
    finally:
        supervisor.stop()
    current = registry.get_run(run_id)
    if current.state not in {
        RunState.APPROVED,
        RunState.NEEDS_HUMAN,
        RunState.FAILED,
    }:
        registry.force_state(
            run_id, RunState.STOPPED if exit_code == 0 else RunState.FAILED
        )
    append_event(
        run_dir / "events.jsonl",
        {"event": "run_exited", "exit_code": exit_code},
    )
    try:
        record_run_end_hint(run_id, RUNS_DIR.parent / "maintenance")
    except OSError:
        pass
    return exit_code


def _reconcile_run_liveness(registry: Registry, candidate: RunRecord) -> None:
    # Selected-run reconcile (used by `status`/`recover`): complete any lingering
    # recovery provenance, recover an orphan, or reconcile an assignable run.
    if (
        Path(candidate.run_dir).joinpath("orphan-provenance.json").exists()
        or candidate.state is RunState.ORPHANED
    ):
        recover_orphaned(registry, candidate, source="status")
    elif candidate.state in {RunState.RUNNING, RunState.WORKER_CONTINUING}:
        reconcile_liveness(registry, candidate, source="status")


def _reconcile_assignable_liveness(registry: Registry) -> None:
    # List-only sweep used by `runs`/`assign`: reconcile assignable runs only.
    # It must NOT probe, recover, or mutate terminal/orphaned history.
    for candidate in registry.list_runs():
        if candidate.state in ASSIGNABLE_STATES:
            reconcile_liveness(registry, candidate, source="runs")


def main(argv: Optional[List[str]] = None) -> int:
    raw_argv = list(sys.argv[1:] if argv is None else argv)
    if len(raw_argv) >= 2 and raw_argv[0] == "run" and raw_argv[1] == "--":
        raw_argv.insert(1, "generic")
    parser = build_parser()
    args = parser.parse_args(raw_argv)
    try:
        if args.command == "run":
            return _run_agent(args)
        registry = _registry()
        if args.command == "bridge":
            controller = _bridge_controller(registry)
            if args.bridge_command == "install":
                payload = _bridge_plugin_manager().install(dry_run=args.dry_run)
                print(json.dumps(payload) if args.json else payload)
                return 0
            if args.bridge_command == "bind":
                run = registry.get_run(args.run_id) if args.run_id else None
                thread = discover_thread(
                    CODEX_SESSIONS_DIR,
                    cwd=run.thread_cwd if run else os.getcwd(),
                    explicit_thread_id=args.thread,
                )
                binding = controller.bind(
                    thread_id=thread.thread_id,
                    thread_cwd=thread.cwd,
                    run_id=args.run_id,
                )
                print(
                    "bridge bound: {} generation {}".format(
                        binding.thread_id, binding.generation
                    )
                )
                return 0
            if args.bridge_command == "status":
                binding = controller.protocol.load_binding()
                outcome = controller.protocol.status(binding)
                payload = {
                    "status": outcome.status,
                    "thread_id": binding.thread_id,
                    "generation": binding.generation,
                    "run_ids": list(outcome.run_ids),
                    "review_id": outcome.review_id,
                }
                print(json.dumps(payload) if args.json else payload)
                return 0
            if args.bridge_command == "doctor":
                binding = controller.protocol.load_binding()
                controller.preflight(binding.thread_id)
                plugin = _bridge_plugin_manager().status()
                desktop_ipc = DesktopIpcClient().probe()
                payload = {
                    "healthy": True,
                    "thread_id": binding.thread_id,
                    "generation": binding.generation,
                    "desktop_ipc": desktop_ipc,
                    "idle_observer": {
                        **plugin,
                        "required_for_immediate_delivery": False,
                    },
                }
                print(json.dumps(payload) if args.json else payload)
                return 0
            if args.bridge_command == "unbind":
                controller.unbind()
                print("visible-review bridge: unbound")
                return 0
            if args.bridge_command == "uninstall":
                payload = _bridge_plugin_manager().uninstall(dry_run=args.dry_run)
                if not args.dry_run:
                    controller.unbind()
                print(json.dumps(payload) if args.json else payload)
                return 0
        if args.command == "runs":
            _reconcile_assignable_liveness(registry)
            runs = registry.list_runs()
            governance = _governance(registry)
            decisions = {
                decision.run_id: decision
                for decision in governance.list_decisions()
            }
            if args.archived:
                runs = [
                    run
                    for run in runs
                    if decisions[run.run_id].storage_state
                    in {"archived", "ledger_only"}
                ]
            elif not args.all:
                runs = [
                    run
                    for run in runs
                    if decisions[run.run_id].storage_state == "hot"
                ]
            if args.json:
                print(
                    json.dumps(
                        [
                            _governed_payload(run, decisions[run.run_id])
                            for run in runs
                        ]
                    )
                )
            else:
                print(render_governed_runs(runs, decisions))
            return 0
        if args.command == "status":
            if args.run_id:
                run = registry.get_run(args.run_id)
            else:
                _reconcile_assignable_liveness(registry)
                run = _latest_active_run(registry)
            storage = registry.get_storage(run.run_id)
            non_hot_storage = {
                StorageState.ARCHIVING,
                StorageState.ARCHIVED,
                StorageState.TRASH,
                StorageState.LEDGER_ONLY,
                StorageState.PURGED,
                StorageState.RECOVERY_REQUIRED,
            }
            if storage.storage_state not in non_hot_storage:
                if args.run_id:
                    _reconcile_run_liveness(registry, run)
                _takeover_coordinator(registry).reconcile_run(run.run_id)
            run = registry.get_run(run.run_id)
            if args.json:
                print(render_status_json(run))
            else:
                print(render_runs([run]))
            return 0
        if args.command == "recover":
            run = registry.get_run(args.run_id)
            recover_orphaned(registry, run, source="recover")
            run = registry.get_run(args.run_id)
            if args.json:
                print(render_status_json(run))
            else:
                print(render_runs([run]))
            return 0
        if args.command == "attach":
            coordinator = _takeover_coordinator(registry)
            coordinator.reconcile_run(args.run_id)
            result = coordinator.attach(args.run_id, args.thread)
            print(
                "{}: {} -> {} (generation {})".format(
                    result.status,
                    result.old_thread_id,
                    result.new_thread_id,
                    result.binding_generation,
                )
            )
            return 0
        if args.command == "stop":
            run = (
                registry.get_run(args.run_id)
                if args.run_id
                else _latest_active_run(registry)
            )
            status = _stop_run(registry, run)
            print(status)
            return 0 if status == "ok" else 1
        if args.command == "deliver":
            _takeover_coordinator(registry).reconcile_run(args.run_id)
            _resolve_review(registry, registry.get_run(args.run_id))
            return 0
        if args.command == "assign":
            registry = _registry()
            if args.latest:
                _reconcile_assignable_liveness(registry)
                run = resolve_latest_assignable_run(registry.list_runs())
            else:
                run = registry.get_run(args.run_id)

            result = assign_task(
                run,
                Path(args.task_file),
                registry=registry,
                redeliver=args.redeliver,
            )
            print(
                "assigned task to {}: {} ({} bytes, sha256={})".format(
                    result.run_id,
                    result.task_path,
                    result.size,
                    result.sha256,
                )
            )
            if result.redelivered:
                print(
                    "warning: same task digest was explicitly redelivered",
                    file=sys.stderr,
                )
            elif result.duplicate:
                print(
                    "warning: same task digest was already assigned to this run",
                    file=sys.stderr,
                )
            return 0
        if args.command == "adopt-task":
            result = adopt_task(
                registry,
                args.run_id,
                args.from_run,
            )
            print(
                "adopted task from {} into {}: {} ({} bytes, sha256={})".format(
                    result.source_run_id,
                    result.run_id,
                    result.latest_path,
                    result.size,
                    result.sha256,
                )
            )
            if result.duplicate:
                print(
                    "warning: task continuity was already recorded for this run",
                    file=sys.stderr,
                )
            return 0
        if args.command == "archive":
            archive_path = _governance(registry).archive_run(
                args.run_id,
                reason=args.reason,
            )
            print("archived {}: {}".format(args.run_id, archive_path))
            return 0
        if args.command == "restore":
            run_path = _governance(registry).restore_run(
                args.run_id,
                reason=args.reason,
            )
            print("restored {}: {}".format(args.run_id, run_path))
            return 0
        if args.command == "pin":
            storage = _governance(registry).pin(args.run_id, args.reason)
            print(
                "pinned {}: {}".format(
                    args.run_id, storage.pin_reason
                )
            )
            return 0
        if args.command == "unpin":
            _governance(registry).unpin(args.run_id)
            print("unpinned {}".format(args.run_id))
            return 0
        if args.command == "gc":
            governance = _governance(registry)
            if args.dry_run:
                plan = governance.create_gc_plan(persist=True)
                if args.json:
                    print(json.dumps(plan.to_dict()))
                else:
                    print(
                        "GC PLAN {} ({})".format(
                            plan.plan_id, plan.created_at
                        )
                    )
                    print(render_decisions(plan.decisions))
                    if plan.unregistered_directories:
                        print("UNREGISTERED (never auto-delete)")
                        for path in plan.unregistered_directories:
                            print(path)
                return 0
            plan = governance.load_gc_plan(
                Path(args.plan).expanduser() if args.plan else None
            )
            result = governance.apply_gc_plan(plan)
            if args.json:
                print(json.dumps(result.to_dict()))
            else:
                print(
                    "applied GC plan {}: {} actions, {} protected/skipped".format(
                        result.plan_id,
                        len(result.applied),
                        len(result.skipped),
                    )
                )
            return 0
        if args.command == "maintenance":
            manager = MaintenanceManager(registry)
            if args.maintenance_command == "install":
                payload = manager.install()
            elif args.maintenance_command == "status":
                payload = manager.status()
            elif args.maintenance_command == "run":
                payload = manager.run_once(scheduled=args.scheduled)
            else:
                payload = manager.uninstall()
            print(json.dumps(payload, ensure_ascii=False, indent=2))
            return 0
        if args.command == "request-review":
            _takeover_coordinator(registry).reconcile_run(args.run_id)
            run = registry.get_run(args.run_id)
            completion_scope = _manual_completion_scope(
                run.mode,
                args.completion_scope,
            )
            if (
                run.mode is RunMode.DESIGN
                and completion_scope == "stage"
            ):
                raise ValueError(
                    "--stage is only valid for develop-mode runs"
                )
            review_round = next_review_round(
                run, registry, Path(run.run_dir)
            )
            if review_round is None:
                print("needs_human")
                return 0
            payload = {
                "schema_version": 1,
                "run_id": run.run_id,
                "status": "ready_for_review",
                "task_summary": "Manual review request",
                "change_summary": args.summary,
                "files_changed": [],
                "commands_run": [],
                "tests": [],
                "known_issues": [],
                "questions_for_reviewer": [],
                "mode": run.mode.value,
                "review_round": review_round,
                "completion_scope": completion_scope,
                "evidence_fingerprint": hashlib.sha256(
                    args.summary.encode("utf-8")
                ).hexdigest(),
            }
            if run.reviewer_backend is ReviewBackend.VISIBLE_THREAD:
                card = create_review_card(
                    run_id=run.run_id,
                    project_slug=run.project_slug,
                    stage_id=(
                        "final"
                        if completion_scope == "final"
                        else "manual-stage"
                    ),
                    stage_title="Manual review request",
                    completion_scope=completion_scope,
                    workspace_root=Path(run.workspace_root),
                    task_packet_path=Path(run.run_dir) / "assigned-task-latest.md",
                    plan_path=_manual_visible_plan_path(run),
                    work_summary=args.summary,
                    completed_items=[args.summary],
                    not_completed_items=[],
                    changed_files=[],
                    artifact_paths=[],
                    test_commands=[],
                    test_result_summary=(
                        "Manual request-review; inspect the real workspace."
                    ),
                    worker_claims=[args.summary],
                    known_issues=[],
                    questions_for_reviewer=[],
                )
                queue_visible_review_card(Path(run.run_dir), card)
                registry.force_state(run.run_id, RunState.READY_FOR_REVIEW)
                append_event(
                    Path(run.run_dir) / "events.jsonl",
                    {
                        "event": "visible_review_card_queued",
                        "run_id": run.run_id,
                        "review_id": card["review_id"],
                        "review_round": review_round,
                        "source": "manual_request_review",
                    },
                )
                registry.increment_review_loop(run.run_id)
                return 0
            write_json_atomic(Path(run.run_dir) / "review-request.json", payload)
            _dispatch_and_deliver(registry, run)
            registry.increment_review_loop(run.run_id)
            return 0
        if args.command == "finalize":
            _takeover_coordinator(registry).reconcile_run(args.run_id)
            run = registry.get_run(args.run_id)
            _finalize_owner_review(
                registry,
                run,
                approved=args.approve,
                message=_finalize_message(args),
            )
            return 0
        if args.command == "review-next":
            run = _pending_visible_review_run(registry, args.run_id)
            card = latest_pending_card(Path(run.run_dir))
            print(render_review_next_instruction(card))
            return 0
        if args.command == "review-submit":
            run = _pending_visible_review_run(registry, args.run_id)
            _takeover_coordinator(registry).reconcile_run(run.run_id)
            run = registry.get_run(run.run_id)
            run_dir = Path(run.run_dir)
            review = submit_visible_review(
                run_dir,
                run.run_id,
                Path(args.review_file),
            )
            registry.force_state(run.run_id, RunState.REVIEW_READY)
            _resolve_review(registry, registry.get_run(run.run_id))
            resolved = registry.get_run(run.run_id)
            if (
                review["verdict"] == "approved"
                and _visible_review_completion_scope(run, run_dir) == "final"
                and resolved.state is RunState.OWNER_REVIEW_PENDING
            ):
                _finalize_owner_review(
                    registry,
                    resolved,
                    approved=True,
                    message=str(review["summary"]),
                )
            return 0
        if args.command == "review-heartbeat":
            status = visible_review_heartbeat_status(registry.list_runs())
            if status["status"] == "ambiguous":
                _bridge_controller(registry).reconcile_stale_pending_reviews(
                    status["run_ids"]
                )
                status = visible_review_heartbeat_status(registry.list_runs())
            if args.json:
                print(json.dumps(status))
            elif status["status"] == "pending":
                print(
                    "pending visible review for {run_id}; run: {command}".format(
                        **status
                    )
                )
            else:
                print("{}: {}".format(status["status"], status["reason"]))
            return 0
        if args.command == "reviewer":
            if args.reviewer_command == "bind":
                run = registry.get_run(args.run_id)
                thread = discover_thread(
                    CODEX_SESSIONS_DIR,
                    cwd=run.thread_cwd,
                    explicit_thread_id=(
                        args.thread
                        or os.environ.get("CODEX_THREAD_ID")
                        or os.environ.get("LOOPWEAVE_THREAD_ID")
                    ),
                )
                updated = registry.bind_reviewer_thread(
                    args.run_id,
                    thread.thread_id,
                    thread.cwd,
                )
                print(
                    "reviewer bound: {} generation {}".format(
                        updated.reviewer_thread_id,
                        updated.reviewer_generation,
                    )
                )
                return 0
        if args.command == "hook":
            _takeover_coordinator(registry).reconcile_run(args.run_id)
            payload = json.load(sys.stdin)
            controller = _bridge_controller(registry)
            handle_claude_stop(
                args.run_id,
                payload,
                registry,
                lambda run: _dispatch_and_deliver(registry, run),
                visible_waker=VisibleReviewDispatcher(controller=controller),
            )
            return 0
        if args.command == "doctor":
            return 0 if doctor_checks(sys.stdout) else 1
        if args.command == "submit":
            run_id = args.run_id or os.environ.get("LOOPWEAVE_RUN_ID")
            if not run_id:
                print("loopweave: --run-id or $LOOPWEAVE_RUN_ID is required", file=sys.stderr)
                return 2
            if args.stage or args.final:
                summary_path = args.summary_file
                if not summary_path:
                    print("loopweave: --summary-file is required for --stage/--final", file=sys.stderr)
                    return 2
                summary = Path(summary_path).expanduser().read_text(encoding="utf-8")
                evidence: Dict[str, object] = {}
                if args.evidence_file:
                    from .submission import load_evidence_file
                    evidence = load_evidence_file(Path(args.evidence_file).expanduser())
                if args.stage:
                    submit_stage(run_id, summary, evidence=evidence)
                else:
                    submit_final(run_id, summary, evidence=evidence)
            else:
                message_path = args.message_file
                if not message_path:
                    print("loopweave: --message-file is required for --needs-human", file=sys.stderr)
                    return 2
                message = Path(message_path).expanduser().read_text(encoding="utf-8")
                submit_needs_human(run_id, message)
            return 0
    except (
        BridgeControlError,
        BridgePluginError,
        SessionDiscoveryError,
        DispatchError,
        ProtocolError,
        RunNotFound,
        AssignmentError,
        TaskContinuityError,
        GovernanceError,
        SubmissionError,
        ValueError,
        RuntimeError,
    ) as error:
        print("loopweave: {}".format(error), file=sys.stderr)
        return 2
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
