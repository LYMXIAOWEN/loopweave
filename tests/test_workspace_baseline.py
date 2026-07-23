from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path

from loopweave.workspace_baseline import capture_workspace_baseline


class WorkspaceBaselineTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name).resolve()

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def _git(self, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["git", "-C", str(self.root), *args],
            check=True,
            text=True,
            capture_output=True,
        )

    def _init_repo(self) -> None:
        self._git("init")
        self._git("config", "user.name", "LoopWeave Test")
        self._git("config", "user.email", "loopweave@example.invalid")
        (self.root / "tracked.txt").write_text("base\n", encoding="utf-8")
        self._git("add", "tracked.txt")
        self._git("commit", "-m", "base")

    def test_dirty_git_workspace_records_patch_and_untracked_hashes(self) -> None:
        self._init_repo()
        (self.root / "tracked.txt").write_text("changed\n", encoding="utf-8")
        (self.root / "new.txt").write_text("new\n", encoding="utf-8")
        before = self._git("status", "--porcelain=v1").stdout

        payload = capture_workspace_baseline(self.root)

        after = self._git("status", "--porcelain=v1").stdout
        self.assertTrue(payload["is_git"])
        self.assertEqual(payload["repo_root"], str(self.root))
        self.assertTrue(payload["head"])
        self.assertIn("tracked.txt", payload["tracked_status"])
        self.assertIn("tracked.txt", payload["tracked_patch"])
        self.assertEqual(payload["untracked"][0]["path"], "new.txt")
        self.assertEqual(payload["untracked"][0]["size"], 4)
        self.assertEqual(len(payload["untracked"][0]["sha256"]), 64)
        self.assertEqual(before, after)

    def test_clean_git_workspace_has_empty_change_evidence(self) -> None:
        self._init_repo()

        payload = capture_workspace_baseline(self.root)

        self.assertEqual(payload["tracked_status"], "")
        self.assertEqual(payload["tracked_patch"], "")
        self.assertEqual(payload["untracked"], [])

    def test_non_git_workspace_is_recorded_without_failure(self) -> None:
        payload = capture_workspace_baseline(self.root)

        self.assertFalse(payload["is_git"])
        self.assertEqual(payload["workspace_root"], str(self.root))
        self.assertNotIn("tracked_patch", payload)


if __name__ == "__main__":
    unittest.main()
