from __future__ import annotations
import pytest
import sys

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


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX codex bundle path assumed")
def test_install_reuses_configured_marketplace_and_adds_plugin(tmp_path: Path):
    calls = []
    responses = [
        json.dumps({"marketplaces": [{"name": "loopweave-local"}]}),
        json.dumps({"installed": []}),
        json.dumps({"pluginId": "loopweave-visible-bridge@loopweave-local"}),
    ]
    manager = BridgePluginManager(
        codex_bin=Path("/opt/codex"),
        marketplace_root=tmp_path,
        runner=_runner(responses, calls),
    )

    result = manager.install()

    assert result["installed"] is True
    assert result["refreshed"] is False
    assert calls == [
        ["/opt/codex", "plugin", "marketplace", "list", "--json"],
        ["/opt/codex", "plugin", "list", "--json"],
        [
            "/opt/codex",
            "plugin",
            "add",
            "loopweave-visible-bridge@loopweave-local",
            "--json",
        ],
    ]


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX codex bundle path assumed")
def test_install_adds_missing_marketplace_before_plugin(tmp_path: Path):
    calls = []
    responses = [
        json.dumps({"marketplaces": []}),
        json.dumps({"name": "loopweave-local"}),
        json.dumps({"installed": []}),
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


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX codex bundle path assumed")
def test_install_refreshes_existing_plugin_cache(tmp_path: Path):
    calls = []
    responses = [
        json.dumps({"marketplaces": [{"name": "loopweave-local"}]}),
        json.dumps(
            {
                "installed": [
                    {
                        "pluginId": "loopweave-visible-bridge@loopweave-local",
                        "installed": True,
                    }
                ]
            }
        ),
        json.dumps({"removed": True}),
        json.dumps({"pluginId": "loopweave-visible-bridge@loopweave-local"}),
    ]
    manager = BridgePluginManager(
        codex_bin=Path("/opt/codex"),
        marketplace_root=tmp_path,
        runner=_runner(responses, calls),
    )

    result = manager.install()

    assert result["refreshed"] is True
    assert calls[-2:] == [
        [
            "/opt/codex",
            "plugin",
            "remove",
            "loopweave-visible-bridge@loopweave-local",
            "--json",
        ],
        [
            "/opt/codex",
            "plugin",
            "add",
            "loopweave-visible-bridge@loopweave-local",
            "--json",
        ],
    ]


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX codex bundle path assumed")
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


def test_run_json_decodes_codex_output_as_utf8(tmp_path: Path):
    """The codex CLI emits UTF-8 JSON (including non-ASCII paths). On a
    Chinese Windows console the default ANSI decode is GBK and crashes on
    multibyte output, so the runner must force UTF-8 with replacement."""
    calls = []

    def run(command, **kwargs):
        calls.append(kwargs)
        return subprocess.CompletedProcess(
            command,
            0,
            stdout='{"marketplaces": [{"name": "loopweave-可见桥"}]}',
            stderr="",
        )

    manager = BridgePluginManager(
        codex_bin=Path("/opt/codex"),
        marketplace_root=tmp_path,
        runner=run,
    )

    payload = manager._run_json(["plugin", "marketplace", "list", "--json"])

    assert payload["marketplaces"][0]["name"] == "loopweave-可见桥"
    assert calls[0]["encoding"] == "utf-8"
    assert calls[0]["errors"] == "replace"
