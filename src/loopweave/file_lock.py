"""Cross-platform exclusive file lock with stable LoopWeave semantics.

POSIX uses ``fcntl.flock``; Windows uses ``msvcrt.locking``.  Both backends
expose the same context-manager contract so callers do not branch on the
platform:

* ``blocking=True``  -> blocking exclusive lock (BridgeProtocol lease state)
* ``blocking=False`` -> non-blocking exclusive lock; raises
  :class:`BlockingIOError` when another holder owns the lock
  (RunGovernance maintenance gate)

The lock file itself is created on demand, its content is never meaningful,
and it is never added to the repository.
"""

from __future__ import annotations

import contextlib
import os
import sys
from collections.abc import Iterator
from pathlib import Path


@contextlib.contextmanager
def exclusive_file_lock(
    path: Path,
    *,
    blocking: bool = True,
) -> Iterator[None]:
    """Acquire an exclusive advisory lock on ``path``.

    The lock is released when the context exits, when the owning file
    descriptor is closed, or when the owning process exits.
    """
    lock_path = Path(path)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(
        str(lock_path),
        os.O_CREAT | os.O_RDWR,
        0o600,
    )
    try:
        if sys.platform == "win32":
            _acquire_windows(descriptor, lock_path, blocking=blocking)
        else:
            _acquire_posix(descriptor, blocking=blocking)
        yield
    finally:
        if sys.platform == "win32":
            _release_windows(descriptor)
        else:
            _release_posix(descriptor)
        os.close(descriptor)


def _acquire_posix(descriptor: int, *, blocking: bool) -> None:
    import fcntl

    flags = fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB)
    try:
        fcntl.flock(descriptor, flags)
    except BlockingIOError:
        # Keep the exception type identical on both platforms.
        raise BlockingIOError("file lock is held elsewhere") from None


def _release_posix(descriptor: int) -> None:
    import fcntl

    try:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
    except OSError:
        pass


def _acquire_windows(descriptor: int, lock_path: Path, *, blocking: bool) -> None:
    import msvcrt

    # msvcrt.locking operates on a byte range; ensure the file holds at least
    # one byte so the range is well defined on every Windows filesystem.
    if os.fstat(descriptor).st_size == 0:
        os.write(descriptor, b"\0")
        os.lseek(descriptor, 0, os.SEEK_SET)
    try:
        if blocking:
            os.lseek(descriptor, 0, os.SEEK_SET)
            msvcrt.locking(descriptor, msvcrt.LK_LOCK, 1)
        else:
            os.lseek(descriptor, 0, os.SEEK_SET)
            msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)
    except OSError as error:
        # msvcrt raises EACCES/EDEADLK for a contended range; normalize to the
        # same BlockingIOError the POSIX non-blocking path raises.
        if not blocking:
            raise BlockingIOError("file lock is held elsewhere") from error
        raise
    # Best-effort: mirrors the 0600 permission intent of the POSIX path.
    try:
        os.chmod(lock_path, 0o600)
    except OSError:
        pass


def _release_windows(descriptor: int) -> None:
    import msvcrt

    try:
        os.lseek(descriptor, 0, os.SEEK_SET)
        msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)
    except OSError:
        pass
