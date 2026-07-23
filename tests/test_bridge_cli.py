import io
from pathlib import Path
from unittest.mock import Mock, patch

import pytest

from loopweave.bridge_control import BridgeControlError
from loopweave.cli import _run_agent, build_parser, main


def test_parser_exposes_bridge_lifecycle_without_managed_daemon_options():
    parser = build_parser()

    install = parser.parse_args(["bridge", "install", "--dry-run", "--json"])
    bind = parser.parse_args(
        ["bridge", "bind", "--thread", "thread-1", "--run-id", "run-1"]
    )
    status = parser.parse_args(["bridge", "status", "--json"])
    doctor = parser.parse_args(["bridge", "doctor", "--json"])
    unbind = parser.parse_args(["bridge", "unbind"])
    uninstall = parser.parse_args(["bridge", "uninstall", "--dry-run", "--json"])

    assert install.bridge_command == "install"
    assert install.dry_run is True
    assert install.json is True
    assert bind.bridge_command == "bind"
    assert bind.thread == "thread-1"
    assert bind.run_id == "run-1"
    assert status.json is True
    assert doctor.bridge_command == "doctor"
    assert doctor.json is True
    assert unbind.bridge_command == "unbind"
    assert uninstall.bridge_command == "uninstall"
    assert uninstall.dry_run is True
    assert uninstall.json is True


def test_visible_run_refuses_unbound_bridge_before_starting_worker(tmp_path: Path):
    args = build_parser().parse_args(
        [
            "run",
            "generic",
            "--cwd",
            str(tmp_path),
            "--reviewer",
            "visible-thread",
            "--",
            "/bin/echo",
            "ok",
        ]
    )
    controller = Mock()
    controller.preflight.side_effect = BridgeControlError("bridge is not bound")

    with patch("loopweave.cli.ensure_runtime_dirs"), patch(
        "loopweave.cli._registry",
        return_value=Mock(),
    ), patch(
        "loopweave.cli.discover_thread",
        return_value=Mock(thread_id="11111111-1111-4111-8111-111111111111", cwd=str(tmp_path)),
    ), patch(
        "loopweave.cli._bridge_controller",
        return_value=controller,
    ), patch("loopweave.cli.create_terminal_host") as supervisor:
        with pytest.raises(BridgeControlError, match="not bound"):
            _run_agent(args)

    supervisor.assert_not_called()


def test_bridge_install_and_uninstall_use_plugin_lifecycle_without_review_work():
    registry = Mock()
    controller = Mock()
    manager = Mock()
    manager.install.return_value = {"installed": True, "restart_required": True}
    manager.uninstall.return_value = {"removed": True, "restart_required": True}
    output = io.StringIO()

    with patch("loopweave.cli._registry", return_value=registry), patch(
        "loopweave.cli._bridge_controller", return_value=controller
    ), patch(
        "loopweave.cli._bridge_plugin_manager", return_value=manager
    ), patch("sys.stdout", output):
        assert main(["bridge", "install", "--json"]) == 0
        assert main(["bridge", "uninstall", "--json"]) == 0

    manager.install.assert_called_once_with(dry_run=False)
    manager.uninstall.assert_called_once_with(dry_run=False)
    controller.unbind.assert_called_once_with()


def test_bridge_doctor_checks_desktop_ipc_and_treats_plugin_as_observer():
    registry = Mock()
    controller = Mock()
    binding = Mock(
        thread_id="11111111-1111-4111-8111-111111111111",
        generation=3,
    )
    controller.protocol.load_binding.return_value = binding
    controller.preflight.return_value = binding
    manager = Mock()
    manager.status.return_value = {"installed": False, "enabled": False}
    ipc = Mock()
    ipc.probe.return_value = {
        "healthy": True,
        "client_id": "desktop-probe",
    }
    output = io.StringIO()

    with patch("loopweave.cli._registry", return_value=registry), patch(
        "loopweave.cli._bridge_controller", return_value=controller
    ), patch(
        "loopweave.cli._bridge_plugin_manager", return_value=manager
    ), patch(
        "loopweave.cli.DesktopIpcClient", return_value=ipc
    ), patch("sys.stdout", output):
        assert main(["bridge", "doctor", "--json"]) == 0

    payload = __import__("json").loads(output.getvalue())
    assert payload == {
        "healthy": True,
        "thread_id": binding.thread_id,
        "generation": 3,
        "desktop_ipc": {
            "healthy": True,
            "client_id": "desktop-probe",
        },
        "idle_observer": {
            "installed": False,
            "enabled": False,
            "required_for_immediate_delivery": False,
        },
    }
    ipc.probe.assert_called_once_with()


def test_claude_stop_hook_uses_desktop_visible_dispatcher():
    registry = Mock()
    coordinator = Mock()
    controller = Mock()
    dispatcher = Mock()
    run_id = "run-visible"

    with patch("loopweave.cli._registry", return_value=registry), patch(
        "loopweave.cli._takeover_coordinator",
        return_value=coordinator,
    ), patch(
        "loopweave.cli._bridge_controller",
        return_value=controller,
    ), patch(
        "loopweave.cli.VisibleReviewDispatcher",
        return_value=dispatcher,
    ) as dispatcher_type, patch(
        "loopweave.cli.handle_claude_stop",
    ) as handle_stop, patch(
        "sys.stdin",
        io.StringIO('{"last_assistant_message":"LOOPWEAVE_STAGE"}'),
    ):
        assert main(["hook", "claude-stop", "--run-id", run_id]) == 0

    coordinator.reconcile_run.assert_called_once_with(run_id)
    dispatcher_type.assert_called_once_with(controller=controller)
    assert handle_stop.call_args.args[:3] == (
        run_id,
        {"last_assistant_message": "LOOPWEAVE_STAGE"},
        registry,
    )
    assert handle_stop.call_args.kwargs["visible_waker"] is dispatcher
