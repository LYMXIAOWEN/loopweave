"""Cross-platform exclusive file-lock tests (fcntl.flock / msvcrt.locking).

Lock contention is exercised across processes because Windows byte-range
locks are per-process: an in-process re-lock of the same range does not
contend the way two holders do.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
from pathlib import Path

from loopweave.file_lock import exclusive_file_lock


class FileLockTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.lock_path = self.root / "nested" / "governance.lock"

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_non_blocking_lock_is_acquirable_when_free(self) -> None:
        with exclusive_file_lock(self.lock_path, blocking=False):
            self.assertTrue(self.lock_path.exists())

    def test_blocking_lock_is_acquirable_when_free(self) -> None:
        with exclusive_file_lock(self.lock_path, blocking=True):
            self.assertTrue(self.lock_path.exists())

    def test_lock_release_allows_reacquisition(self) -> None:
        with exclusive_file_lock(self.lock_path, blocking=False):
            pass
        with exclusive_file_lock(self.lock_path, blocking=False):
            pass

    def test_non_blocking_contention_raises_blocking_io_error(self) -> None:
        holder = self._spawn_holder(hold_seconds=4)
        try:
            self._wait_until_locked()
            with self.assertRaises(BlockingIOError):
                with exclusive_file_lock(self.lock_path, blocking=False):
                    pass
        finally:
            holder.wait(timeout=10)

    def test_blocking_lock_waits_for_release(self) -> None:
        holder = self._spawn_holder(hold_seconds=2)
        try:
            self._wait_until_locked()
            started = time.monotonic()
            with exclusive_file_lock(self.lock_path, blocking=True):
                elapsed = time.monotonic() - started
            self.assertGreaterEqual(elapsed, 1.0)
        finally:
            holder.wait(timeout=10)

    def _spawn_holder(self, *, hold_seconds: int) -> subprocess.Popen:
        lock_s = str(self.lock_path).replace("\\", "\\\\")
        script = textwrap.dedent(
            f"""
            import sys, time
            from loopweave.file_lock import exclusive_file_lock
            with exclusive_file_lock(r"{lock_s}", blocking=True):
                print("LOCKED", flush=True)
                time.sleep({hold_seconds})
            """
        )
        env = dict(os.environ)
        src = str(Path(__file__).resolve().parents[1] / "src")
        env["PYTHONPATH"] = src + os.pathsep + env.get("PYTHONPATH", "")
        return subprocess.Popen(
            [sys.executable, "-c", script],
            stdout=subprocess.PIPE,
            text=True,
            env=env,
        )

    def _wait_until_locked(self, timeout: float = 10.0) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                with exclusive_file_lock(self.lock_path, blocking=False):
                    pass
                # Sleep AFTER releasing so the child holder is not starved.
                time.sleep(0.05)
                continue
            except BlockingIOError:
                return
        self.fail("child holder never acquired the lock")


if __name__ == "__main__":
    unittest.main()
