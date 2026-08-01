from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any, Callable, Dict, List


PLUGIN_NAME = "loopweave-visible-bridge"
MARKETPLACE_NAME = "loopweave-local"
PLUGIN_SELECTOR = f"{PLUGIN_NAME}@{MARKETPLACE_NAME}"


class BridgePluginError(RuntimeError):
    pass


class BridgePluginManager:
    def __init__(
        self,
        *,
        codex_bin: Path,
        marketplace_root: Path,
        runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
    ) -> None:
        self.codex_bin = Path(codex_bin)
        self.marketplace_root = Path(marketplace_root).resolve()
        self.runner = runner

    def install(self, *, dry_run: bool = False) -> Dict[str, Any]:
        if dry_run:
            return {
                "installed": False,
                "dry_run": True,
                "restart_required": True,
                "commands": [
                    self._marketplace_add_command(),
                    self._plugin_remove_command(),
                    self._plugin_add_command(),
                ],
            }
        marketplaces = self._run_json(
            ["plugin", "marketplace", "list", "--json"]
        )
        configured = {
            str(item.get("name", ""))
            for item in marketplaces.get("marketplaces", [])
            if isinstance(item, dict)
        }
        if MARKETPLACE_NAME not in configured:
            self._run_json(self._marketplace_add_command()[1:])
        installed = self._run_json(["plugin", "list", "--json"])
        refresh = any(
            isinstance(item, dict)
            and item.get("pluginId") == PLUGIN_SELECTOR
            and bool(item.get("installed", True))
            for item in installed.get("installed", [])
        )
        if refresh:
            self._run_json(self._plugin_remove_command()[1:])
        payload = self._run_json(self._plugin_add_command()[1:])
        return {
            "installed": True,
            "refreshed": refresh,
            "plugin_id": payload.get("pluginId", PLUGIN_SELECTOR),
            "restart_required": True,
        }

    def uninstall(self, *, dry_run: bool = False) -> Dict[str, Any]:
        command = self._plugin_remove_command()
        if dry_run:
            return {
                "removed": False,
                "dry_run": True,
                "restart_required": True,
                "commands": [command],
            }
        self._run_json(command[1:])
        return {"removed": True, "restart_required": True}

    def status(self) -> Dict[str, Any]:
        payload = self._run_json(["plugin", "list", "--json"])
        for item in payload.get("installed", []):
            if isinstance(item, dict) and item.get("pluginId") == PLUGIN_SELECTOR:
                return {
                    "installed": bool(item.get("installed", True)),
                    "enabled": bool(item.get("enabled", True)),
                    "version": item.get("version"),
                    "plugin_id": PLUGIN_SELECTOR,
                }
        return {
            "installed": False,
            "enabled": False,
            "version": None,
            "plugin_id": PLUGIN_SELECTOR,
        }

    def _run_json(self, arguments: List[str]) -> Dict[str, Any]:
        command = [str(self.codex_bin), *arguments]
        result = self.runner(
            command,
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        if result.returncode != 0:
            detail = (result.stderr or result.stdout or "command failed").strip()
            raise BridgePluginError(detail[:1024])
        try:
            payload = json.loads(result.stdout or "{}")
        except json.JSONDecodeError as error:
            raise BridgePluginError("Codex plugin command returned invalid JSON") from error
        if not isinstance(payload, dict):
            raise BridgePluginError("Codex plugin command returned invalid JSON")
        return payload

    def _marketplace_add_command(self) -> List[str]:
        return [
            str(self.codex_bin),
            "plugin",
            "marketplace",
            "add",
            str(self.marketplace_root),
            "--json",
        ]

    def _plugin_add_command(self) -> List[str]:
        return [
            str(self.codex_bin),
            "plugin",
            "add",
            PLUGIN_SELECTOR,
            "--json",
        ]

    def _plugin_remove_command(self) -> List[str]:
        return [
            str(self.codex_bin),
            "plugin",
            "remove",
            PLUGIN_SELECTOR,
            "--json",
        ]
