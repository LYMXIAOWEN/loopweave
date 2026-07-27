from __future__ import annotations

import plistlib
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

from loopweave.maintenance import (
    LAUNCH_AGENT_LABEL,
    MaintenanceManager,
    MaintenancePaths,
    render_launch_agent,
)
from loopweave.models import RunRecord, RunState
from loopweave.registry import Registry
from loopweave.run_governance import ApplyResult, GcPlan
from loopweave.runtime_config import RunPolicy


def _completed(command, returncode=0):
    return subprocess.CompletedProcess(command, returncode, "", "")


class MaintenanceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.registry = Registry(self.root / "var" / "registry.sqlite")
        self.paths = MaintenancePaths(
            maintenance=self.root / "maintenance",
            launch_agent=self.root / "LaunchAgents" / "maintenance.plist",
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_launch_agent_is_daily_one_shot_with_explicit_stop_path(self) -> None:
        payload = plistlib.loads(
            render_launch_agent(
                policy=RunPolicy(maintenance_hour=3, maintenance_minute=45),
                command=["/opt/bin/loopweave"],
                maintenance_dir=self.root / "maintenance",
            )
        )

        self.assertEqual(payload["Label"], LAUNCH_AGENT_LABEL)
        self.assertEqual(
            payload["ProgramArguments"],
            ["/opt/bin/loopweave", "maintenance", "run", "--scheduled"],
        )
        self.assertEqual(
            payload["StartCalendarInterval"], {"Hour": 3, "Minute": 45}
        )
        self.assertNotIn("KeepAlive", payload)

    def test_install_status_and_uninstall_are_idempotent(self) -> None:
        commands = []

        def runner(command):
            commands.append(list(command))
            if command[1] == "print":
                return _completed(command, 0 if self.paths.launch_agent.exists() else 3)
            return _completed(command)

        manager = MaintenanceManager(
            self.registry,
            governance=Mock(),
            policy=RunPolicy(),
            paths=self.paths,
            command_runner=runner,
            loopweave_command=["/opt/bin/loopweave"],
        )

        installed = manager.install()
        uninstalled = manager.uninstall()

        self.assertTrue(installed["installed"])
        self.assertTrue(installed["loaded"])
        self.assertEqual(
            installed["stop_command"], "loopweave maintenance uninstall"
        )
        self.assertFalse(uninstalled["installed"])
        self.assertFalse(uninstalled["loaded"])
        self.assertTrue(any(command[1] == "bootstrap" for command in commands))
        self.assertTrue(any(command[1] == "bootout" for command in commands))

    def test_scheduled_run_defers_when_any_workflow_is_active(self) -> None:
        run_dir = self.root / "runs" / "run-active"
        run_dir.mkdir(parents=True)
        self.registry.create_run(
            RunRecord(
                run_id="run-active",
                codex_thread_id="thread",
                cwd=str(self.root),
                tty="",
                agent="codex",
                agent_pid=123,
                agent_process_start="start",
                control_token="secret",
                state=RunState.RUNNING,
                socket_path=str(self.root / "control.sock"),
                run_dir=str(run_dir),
            )
        )
        governance = Mock()
        manager = MaintenanceManager(
            self.registry,
            governance=governance,
            paths=self.paths,
            command_runner=lambda command: _completed(command, 3),
        )

        result = manager.run_once(scheduled=True)

        self.assertEqual(result["status"], "deferred_active_runs")
        governance.create_gc_plan.assert_not_called()

    def test_manual_run_creates_and_applies_one_exact_plan(self) -> None:
        plan = GcPlan(
            plan_id="gc-test",
            created_at="2026-07-27T00:00:00+00:00",
            policy_version="1",
            decisions=(),
            unregistered_directories=(),
        )
        governance = Mock()
        governance.create_gc_plan.return_value = plan
        governance.apply_gc_plan.return_value = ApplyResult(
            plan_id=plan.plan_id,
            applied=(),
            skipped=(),
        )
        manager = MaintenanceManager(
            self.registry,
            governance=governance,
            paths=self.paths,
            command_runner=lambda command: _completed(command, 3),
        )

        result = manager.run_once()

        self.assertEqual(result["status"], "completed")
        governance.apply_gc_plan.assert_called_once_with(plan)


if __name__ == "__main__":
    unittest.main()
