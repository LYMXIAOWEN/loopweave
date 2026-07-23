from __future__ import annotations

import json
import subprocess
from pathlib import Path

from loopweave.bridge_plugin import BridgePluginManager


def _runner(responses, calls):
    def run(command, **kwargs):
        calls.append(list(command))
        stdout = responses.pop(0)
        return subprocess.CompletedProcess(command, 0, stdout=stdout, stderr="")

    return run


def test_install_reuses_configured_marketplace_and_adds_plugin(tmp_path: Path):
    calls = []
    responses = [
        json.dumps({"marketplaces": [{"name": "loopweave-local"}]}),
        json.dumps({"pluginId": "loopweave-visible-bridge@loopweave-local"}),
    ]
    manager = BridgePluginManager(
        codex_bin=Path("/opt/codex"),
        marketplace_root=tmp_path,
        runner=_runner(responses, calls),
    )

    result = manager.install()

    assert result["installed"] is True
    assert calls == [
        ["/opt/codex", "plugin", "marketplace", "list", "--json"],
        [
            "/opt/codex",
            "plugin",
            "add",
            "loopweave-visible-bridge@loopweave-local",
            "--json",
        ],
    ]


def test_install_adds_missing_marketplace_before_plugin(tmp_path: Path):
    calls = []
    responses = [
        json.dumps({"marketplaces": []}),
        json.dumps({"name": "loopweave-local"}),
        json.dumps({"pluginId": "loopweave-visible-bridge@loopweave-local"}),
    ]
    manager = BridgePluginManager(
        codex_bin=Path("/opt/codex"),
        marketplace_root=tmp_path,
        runner=_runner(responses, calls),
    )

    manager.install()

    assert calls[1] == [
        "/opt/codex",
        "plugin",
        "marketplace",
        "add",
        str(tmp_path),
        "--json",
    ]


def test_dry_run_and_uninstall_do_not_touch_marketplace(tmp_path: Path):
    calls = []
    manager = BridgePluginManager(
        codex_bin=Path("/opt/codex"),
        marketplace_root=tmp_path,
        runner=_runner([json.dumps({"removed": True})], calls),
    )

    dry_run = manager.install(dry_run=True)
    removed = manager.uninstall()

    assert dry_run["installed"] is False
    assert dry_run["restart_required"] is True
    assert removed["removed"] is True
    assert calls == [
        [
            "/opt/codex",
            "plugin",
            "remove",
            "loopweave-visible-bridge@loopweave-local",
            "--json",
        ]
    ]


def test_status_reports_installed_plugin_version(tmp_path: Path):
    calls = []
    responses = [
        json.dumps(
            {
                "installed": [
                    {
                        "pluginId": "loopweave-visible-bridge@loopweave-local",
                        "installed": True,
                        "enabled": True,
                        "version": "0.1.0+codex.test",
                    }
                ]
            }
        )
    ]
    manager = BridgePluginManager(
        codex_bin=Path("/opt/codex"),
        marketplace_root=tmp_path,
        runner=_runner(responses, calls),
    )

    assert manager.status() == {
        "installed": True,
        "enabled": True,
        "version": "0.1.0+codex.test",
        "plugin_id": "loopweave-visible-bridge@loopweave-local",
    }
