from __future__ import annotations

import json
import os
import plistlib
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional, Sequence

from .config import MAINTENANCE_DIR
from .models import RunState
from .protocol import utc_now, write_json_atomic
from .registry import Registry
from .run_governance import ApplyResult, RunGovernance
from .runtime_config import RunPolicy, load_run_policy


LAUNCH_AGENT_LABEL = "com.loopweave.maintenance"


class MaintenanceError(RuntimeError):
    pass


@dataclass(frozen=True)
class MaintenancePaths:
    maintenance: Path = MAINTENANCE_DIR
    launch_agent: Path = (
        Path.home()
        / "Library"
        / "LaunchAgents"
        / "{}.plist".format(LAUNCH_AGENT_LABEL)
    )


def _default_runner(command: Sequence[str]) -> subprocess.CompletedProcess:
    return subprocess.run(
        list(command),
        capture_output=True,
        text=True,
        check=False,
    )


def _loopweave_command() -> list[str]:
    executable = shutil.which("loopweave")
    if executable:
        return [str(Path(executable).resolve())]
    return [sys.executable, "-m", "loopweave.cli"]


def _domain() -> str:
    if hasattr(os, "getuid"):
        return "gui/{}".format(os.getuid())
    # Windows has no POSIX uid; launchd domains are macOS-only anyway, so
    # the value is informational here.
    return "gui/{}".format(os.environ.get("USERNAME", "loopweave"))


def render_launch_agent(
    *,
    policy: RunPolicy,
    command: Optional[Sequence[str]] = None,
    maintenance_dir: Path = MAINTENANCE_DIR,
) -> bytes:
    arguments = list(command or _loopweave_command()) + [
        "maintenance",
        "run",
        "--scheduled",
    ]
    payload = {
        "Label": LAUNCH_AGENT_LABEL,
        "ProgramArguments": arguments,
        "RunAtLoad": False,
        "StartCalendarInterval": {
            "Hour": policy.maintenance_hour,
            "Minute": policy.maintenance_minute,
        },
        "ProcessType": "Background",
        "LowPriorityIO": True,
        "Nice": 10,
        "StandardOutPath": str(maintenance_dir / "launchd.stdout.log"),
        "StandardErrorPath": str(maintenance_dir / "launchd.stderr.log"),
    }
    return plistlib.dumps(payload, fmt=plistlib.FMT_XML, sort_keys=True)


class MaintenanceManager:
    def __init__(
        self,
        registry: Registry,
        *,
        governance: Optional[RunGovernance] = None,
        policy: Optional[RunPolicy] = None,
        paths: Optional[MaintenancePaths] = None,
        command_runner: Callable[[Sequence[str]], subprocess.CompletedProcess] = _default_runner,
        loopweave_command: Optional[Sequence[str]] = None,
    ) -> None:
        self.registry = registry
        self.governance = (
            governance if governance is not None else RunGovernance(registry)
        )
        self.policy = policy if policy is not None else load_run_policy()
        self.paths = paths if paths is not None else MaintenancePaths()
        self.command_runner = command_runner
        self.loopweave_command = list(loopweave_command or _loopweave_command())
        self.paths.maintenance.mkdir(parents=True, exist_ok=True)

    def install(self) -> dict:
        launch_agent = self.paths.launch_agent
        launch_agent.parent.mkdir(parents=True, exist_ok=True)
        if launch_agent.exists():
            self.command_runner(
                ["launchctl", "bootout", _domain(), str(launch_agent)]
            )
        payload = render_launch_agent(
            policy=self.policy,
            command=self.loopweave_command,
            maintenance_dir=self.paths.maintenance,
        )
        temporary = launch_agent.with_name(".{}.tmp".format(launch_agent.name))
        temporary.write_bytes(payload)
        os.chmod(temporary, 0o600)
        os.replace(str(temporary), str(launch_agent))
        result = self.command_runner(
            ["launchctl", "bootstrap", _domain(), str(launch_agent)]
        )
        if result.returncode != 0:
            raise MaintenanceError(
                "launchctl bootstrap failed: {}".format(
                    (result.stderr or result.stdout).strip()
                )
            )
        return self.status()

    def status(self) -> dict:
        result = self.command_runner(
            ["launchctl", "print", "{}/{}".format(_domain(), LAUNCH_AGENT_LABEL)]
        )
        last_result_path = self.paths.maintenance / "maintenance-result-latest.json"
        last_result = None
        if last_result_path.is_file() and not last_result_path.is_symlink():
            try:
                last_result = json.loads(
                    last_result_path.read_text(encoding="utf-8")
                )
            except ValueError:
                last_result = {"status": "unreadable"}
        return {
            "label": LAUNCH_AGENT_LABEL,
            "installed": self.paths.launch_agent.is_file(),
            "loaded": result.returncode == 0,
            "launch_agent_path": str(self.paths.launch_agent),
            "schedule": {
                "hour": self.policy.maintenance_hour,
                "minute": self.policy.maintenance_minute,
            },
            "manual_command": "loopweave maintenance run",
            "stop_command": "loopweave maintenance uninstall",
            "last_result": last_result,
        }

    def uninstall(self) -> dict:
        launch_agent = self.paths.launch_agent
        result = self.command_runner(
            ["launchctl", "bootout", _domain(), str(launch_agent)]
        )
        if result.returncode not in {0, 3, 5, 113}:
            raise MaintenanceError(
                "launchctl bootout failed: {}".format(
                    (result.stderr or result.stdout).strip()
                )
            )
        try:
            launch_agent.unlink()
        except FileNotFoundError:
            pass
        return self.status()

    def run_once(self, *, scheduled: bool = False) -> dict:
        active = [
            run.run_id
            for run in self.registry.list_runs()
            if run.state
            in {
                RunState.CREATED,
                RunState.BINDING,
                RunState.RUNNING,
                RunState.READY_FOR_REVIEW,
                RunState.REVIEWING,
                RunState.REVIEW_READY,
                RunState.DELIVERING,
                RunState.WORKER_CONTINUING,
            }
        ]
        if scheduled and active:
            payload = {
                "schema_version": 1,
                "status": "deferred_active_runs",
                "active_run_ids": active,
                "occurred_at": utc_now(),
            }
            write_json_atomic(
                self.paths.maintenance / "maintenance-result-latest.json",
                payload,
            )
            return payload
        plan = self.governance.create_gc_plan(persist=True)
        result: ApplyResult = self.governance.apply_gc_plan(plan)
        payload = {
            "schema_version": 1,
            "status": "completed",
            "scheduled": scheduled,
            "occurred_at": utc_now(),
            **result.to_dict(),
        }
        write_json_atomic(
            self.paths.maintenance / "maintenance-result-latest.json",
            payload,
        )
        return payload


def record_run_end_hint(run_id: str, maintenance_dir: Path = MAINTENANCE_DIR) -> None:
    maintenance_dir.mkdir(parents=True, exist_ok=True)
    write_json_atomic(
        maintenance_dir / "run-end-latest.json",
        {
            "schema_version": 1,
            "run_id": run_id,
            "ended_at": utc_now(),
            "action": "candidate_check_requested",
        },
    )
