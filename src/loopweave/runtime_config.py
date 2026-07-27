from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict

from .config import CONFIG_PATH


POLICY_VERSION = "1"


@dataclass(frozen=True)
class RunPolicy:
    archive_after_days: int = 7
    orphan_after_days: int = 14
    trash_after_days: int = 7
    prune_after_days: int = 90
    keep_recent_per_project: int = 3
    terminal_log_max_bytes: int = 16 * 1024 * 1024
    terminal_log_backups: int = 2
    raw_log_enabled: bool = False
    raw_log_max_bytes: int = 16 * 1024 * 1024
    raw_log_backups: int = 1
    task_ready_quiet_ms: int = 500
    task_ready_fallback_ms: int = 5000
    task_ready_timeout_ms: int = 10000
    maintenance_hour: int = 4
    maintenance_minute: int = 15

    def __post_init__(self) -> None:
        nonnegative = (
            "archive_after_days",
            "orphan_after_days",
            "trash_after_days",
            "prune_after_days",
            "keep_recent_per_project",
            "terminal_log_backups",
            "raw_log_backups",
        )
        for name in nonnegative:
            if int(getattr(self, name)) < 0:
                raise ValueError("{} must be nonnegative".format(name))
        if self.terminal_log_max_bytes < 1024:
            raise ValueError("terminal_log_max_bytes must be at least 1024")
        if self.raw_log_max_bytes < 1024:
            raise ValueError("raw_log_max_bytes must be at least 1024")
        if self.task_ready_quiet_ms < 0:
            raise ValueError("task_ready_quiet_ms must be nonnegative")
        if self.task_ready_fallback_ms <= 0:
            raise ValueError("task_ready_fallback_ms must be positive")
        if self.task_ready_timeout_ms < self.task_ready_fallback_ms:
            raise ValueError(
                "task_ready_timeout_ms must be at least task_ready_fallback_ms"
            )
        if not 0 <= self.maintenance_hour <= 23:
            raise ValueError("maintenance_hour must be between 0 and 23")
        if not 0 <= self.maintenance_minute <= 59:
            raise ValueError("maintenance_minute must be between 0 and 59")


DEFAULT_CONFIG = """\
# LoopWeave local runtime policy.

[retention]
archive_after_days = 7
orphan_after_days = 14
trash_after_days = 7
prune_after_days = 90
keep_recent_per_project = 3

[logging]
terminal_log_max_bytes = 16777216
terminal_log_backups = 2
raw_log_enabled = false
raw_log_max_bytes = 16777216
raw_log_backups = 1

[delivery]
task_ready_quiet_ms = 500
task_ready_fallback_ms = 5000
task_ready_timeout_ms = 10000

[maintenance]
hour = 4
minute = 15
"""


def _parse_scalar(value: str) -> Any:
    text = value.strip()
    lowered = text.lower()
    if lowered == "true":
        return True
    if lowered == "false":
        return False
    try:
        return int(text)
    except ValueError as error:
        raise ValueError("unsupported configuration value: {!r}".format(text)) from error


def _parse_simple_toml(text: str) -> Dict[str, Dict[str, Any]]:
    payload: Dict[str, Dict[str, Any]] = {}
    section = None
    for line_number, raw_line in enumerate(text.splitlines(), start=1):
        line = raw_line.split("#", 1)[0].strip()
        if not line:
            continue
        if line.startswith("[") and line.endswith("]"):
            section = line[1:-1].strip()
            if not section:
                raise ValueError("empty configuration section on line {}".format(line_number))
            payload.setdefault(section, {})
            continue
        if section is None or "=" not in line:
            raise ValueError("invalid configuration line {}".format(line_number))
        key, value = line.split("=", 1)
        key = key.strip()
        if not key:
            raise ValueError("empty configuration key on line {}".format(line_number))
        payload[section][key] = _parse_scalar(value)
    return payload


def load_run_policy(path: Path = CONFIG_PATH) -> RunPolicy:
    config_path = Path(path).expanduser()
    if not config_path.exists():
        return RunPolicy()
    if config_path.is_symlink() or not config_path.is_file():
        raise ValueError("LoopWeave configuration path is unsafe")
    payload = _parse_simple_toml(config_path.read_text(encoding="utf-8"))
    retention = payload.get("retention", {})
    logging = payload.get("logging", {})
    delivery = payload.get("delivery", {})
    maintenance = payload.get("maintenance", {})
    known = {
        "retention": {
            "archive_after_days",
            "orphan_after_days",
            "trash_after_days",
            "prune_after_days",
            "keep_recent_per_project",
        },
        "logging": {
            "terminal_log_max_bytes",
            "terminal_log_backups",
            "raw_log_enabled",
            "raw_log_max_bytes",
            "raw_log_backups",
        },
        "delivery": {
            "task_ready_quiet_ms",
            "task_ready_fallback_ms",
            "task_ready_timeout_ms",
        },
        "maintenance": {"hour", "minute"},
    }
    for section, values in payload.items():
        unknown = set(values) - known.get(section, set())
        if section not in known or unknown:
            raise ValueError(
                "unknown LoopWeave configuration key: {}".format(
                    "{}.{}".format(section, sorted(unknown)[0])
                    if unknown
                    else section
                )
            )
    env_raw = os.environ.get("LOOPWEAVE_RAW_TERMINAL_LOG", "").strip().lower()
    raw_enabled = logging.get("raw_log_enabled", False)
    if env_raw:
        if env_raw not in {"0", "1", "false", "true"}:
            raise ValueError("LOOPWEAVE_RAW_TERMINAL_LOG must be true or false")
        raw_enabled = env_raw in {"1", "true"}
    return RunPolicy(
        archive_after_days=int(retention.get("archive_after_days", 7)),
        orphan_after_days=int(retention.get("orphan_after_days", 14)),
        trash_after_days=int(retention.get("trash_after_days", 7)),
        prune_after_days=int(retention.get("prune_after_days", 90)),
        keep_recent_per_project=int(retention.get("keep_recent_per_project", 3)),
        terminal_log_max_bytes=int(
            logging.get("terminal_log_max_bytes", 16 * 1024 * 1024)
        ),
        terminal_log_backups=int(logging.get("terminal_log_backups", 2)),
        raw_log_enabled=bool(raw_enabled),
        raw_log_max_bytes=int(
            logging.get("raw_log_max_bytes", 16 * 1024 * 1024)
        ),
        raw_log_backups=int(logging.get("raw_log_backups", 1)),
        task_ready_quiet_ms=int(delivery.get("task_ready_quiet_ms", 500)),
        task_ready_fallback_ms=int(delivery.get("task_ready_fallback_ms", 5000)),
        task_ready_timeout_ms=int(delivery.get("task_ready_timeout_ms", 10000)),
        maintenance_hour=int(maintenance.get("hour", 4)),
        maintenance_minute=int(maintenance.get("minute", 15)),
    )


def install_default_config(path: Path = CONFIG_PATH) -> Path:
    config_path = Path(path).expanduser()
    config_path.parent.mkdir(parents=True, exist_ok=True)
    if not config_path.exists():
        temporary = config_path.with_name(".{}.tmp".format(config_path.name))
        temporary.write_text(DEFAULT_CONFIG, encoding="utf-8")
        os.chmod(temporary, 0o600)
        os.replace(str(temporary), str(config_path))
    return config_path
