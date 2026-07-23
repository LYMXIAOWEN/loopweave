from __future__ import annotations

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
        raise UnsupportedPlatformError(
            "native Windows terminal hosting (ConPTY) is not implemented "
            "in this package; LoopWeave currently requires macOS or Linux"
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
        raise UnsupportedPlatformError(
            "control-message delivery to a managed session is not "
            "implemented on Windows in this package"
        )
    from .supervisor import send_control_message

    return send_control_message


def default_process_identity_reader() -> ProcessIdentityReader:
    if sys.platform == "win32":
        raise UnsupportedPlatformError(
            "process-identity lookup for a managed session is not "
            "implemented on Windows in this package"
        )
    from .supervisor import process_start_time

    return process_start_time
