from __future__ import annotations

import os
import sys
from abc import ABC, abstractmethod
from pathlib import Path
from typing import List, Protocol, Tuple


class UnsupportedPlatformError(RuntimeError):
    pass


class TerminalHost(ABC):
    """Platform-neutral managed-session boundary (ADR 0001 section 1).

    No POSIX-only imports at module scope. Implementations own process
    identity, input delivery, the foreground lifecycle, resize, and
    shutdown for one managed child process.
    """

    @abstractmethod
    def start(self) -> int:
        raise NotImplementedError

    @abstractmethod
    def send_input(self, text: str) -> None:
        raise NotImplementedError

    @abstractmethod
    def run_foreground(self) -> int:
        raise NotImplementedError

    @abstractmethod
    def resize(self, rows: int, cols: int) -> None:
        raise NotImplementedError

    @abstractmethod
    def stop(self) -> None:
        raise NotImplementedError

    @abstractmethod
    def process_identity(self) -> Tuple[int, str]:
        raise NotImplementedError


class ControlSender(Protocol):
    def __call__(
        self, socket_path: Path, payload: dict, timeout: float = 3.0
    ) -> dict: ...


class ProcessIdentityReader(Protocol):
    def __call__(self, pid: int) -> str: ...


def create_terminal_host(
    *,
    run_id: str,
    command: List[str],
    cwd: Path,
    run_dir: Path,
    socket_path: Path,
    control_token: str,
    passthrough: bool = True,
) -> TerminalHost:
    if sys.platform == "win32":
        try:
            from .windows_terminal_host import WindowsConPtyHost
        except ImportError as error:
            raise UnsupportedPlatformError(
                "native Windows terminal hosting requires the 'windows' "
                "extra; install with: pip install 'loopweave[windows]'"
            ) from error
        return WindowsConPtyHost(
            run_id=run_id,
            command=command,
            cwd=cwd,
            run_dir=run_dir,
            socket_path=socket_path,
            control_token=control_token,
            passthrough=passthrough,
        )
    from .supervisor import Supervisor

    return Supervisor(
        run_id=run_id,
        command=command,
        cwd=cwd,
        run_dir=run_dir,
        socket_path=socket_path,
        control_token=control_token,
        passthrough=passthrough,
    )


def default_control_sender() -> ControlSender:
    if sys.platform == "win32":
        from .control_transport import send_control_message

        return send_control_message
    from .supervisor import send_control_message

    return send_control_message


def default_process_identity_reader() -> ProcessIdentityReader:
    if sys.platform == "win32":
        try:
            from .windows_terminal_host import process_start_time
        except ImportError as error:
            raise UnsupportedPlatformError(
                "native Windows process identity requires the 'windows' "
                "extra; install with: pip install 'loopweave[windows]'"
            ) from error
        return process_start_time
    from .supervisor import process_start_time

    return process_start_time


def pid_alive(pid: int) -> bool:
    """Portable existence probe that never signals the target process.

    ``os.kill(pid, 0)`` is unsafe on Windows because signal 0 is the
    ``CTRL_C_EVENT`` constant and would broadcast Ctrl+C to the console;
    the Windows backend therefore probes with ``OpenProcess`` instead.
    """
    if pid <= 0:
        return False
    if sys.platform == "win32":
        from .windows_terminal_host import process_alive

        return process_alive(pid)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True
