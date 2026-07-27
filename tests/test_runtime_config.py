from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from loopweave.runtime_config import (
    RunPolicy,
    install_default_config,
    load_run_policy,
)


class RuntimeConfigTests(unittest.TestCase):
    def test_default_config_is_installed_and_loaded(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.toml"

            installed = install_default_config(path)
            policy = load_run_policy(path)

        self.assertEqual(installed, path)
        self.assertEqual(policy, RunPolicy())
        self.assertFalse(policy.raw_log_enabled)

    def test_policy_values_are_configurable(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.toml"
            path.write_text(
                """
[retention]
archive_after_days = 3
orphan_after_days = 5
trash_after_days = 8
prune_after_days = 120
keep_recent_per_project = 4

[logging]
terminal_log_max_bytes = 4096
terminal_log_backups = 1
raw_log_enabled = true
raw_log_max_bytes = 8192
raw_log_backups = 2

[delivery]
task_ready_quiet_ms = 250
task_ready_fallback_ms = 2000
task_ready_timeout_ms = 6000

[maintenance]
hour = 2
minute = 30
""",
                encoding="utf-8",
            )

            policy = load_run_policy(path)

        self.assertEqual(policy.archive_after_days, 3)
        self.assertEqual(policy.orphan_after_days, 5)
        self.assertEqual(policy.trash_after_days, 8)
        self.assertTrue(policy.raw_log_enabled)
        self.assertEqual(policy.task_ready_quiet_ms, 250)
        self.assertEqual(policy.task_ready_fallback_ms, 2000)
        self.assertEqual(policy.task_ready_timeout_ms, 6000)
        self.assertEqual(policy.maintenance_hour, 2)

    def test_unknown_configuration_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.toml"
            path.write_text("[retention]\nunsafe = 1\n", encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "unknown"):
                load_run_policy(path)


if __name__ == "__main__":
    unittest.main()
