"""Minimal Win32 named-pipe helpers used by LoopWeave on Windows.

Implemented with ``ctypes`` over ``kernel32`` so the Windows backend does not
require the heavier ``pywin32`` dependency.  The module intentionally stays
small: it exposes byte-stream connect/read/write and a single-instance
listener, which is everything the control channel and the Codex Desktop IPC
client need.
"""

from __future__ import annotations

import ctypes
import sys
import time
from ctypes import wintypes

if sys.platform != "win32":  # pragma: no cover - Windows-only module
    raise ImportError("win32_pipe is only importable on Windows")


class NamedPipeError(RuntimeError):
    pass


GENERIC_READ = 0x80000000
GENERIC_WRITE = 0x40000000
OPEN_EXISTING = 3
FILE_ATTRIBUTE_NORMAL = 0x80

PIPE_ACCESS_DUPLEX = 0x3
PIPE_TYPE_BYTE = 0x0
PIPE_READMODE_BYTE = 0x0
PIPE_WAIT = 0x0

ERROR_FILE_NOT_FOUND = 2
ERROR_PIPE_BUSY = 231
ERROR_BROKEN_PIPE = 109
ERROR_NO_DATA = 232
ERROR_PIPE_NOT_CONNECTED = 233
ERROR_PIPE_CONNECTED = 535
ERROR_INVALID_PARAMETER = 87
ERROR_SEM_TIMEOUT = 121

_INVALID_HANDLE_VALUE = wintypes.HANDLE(-1).value

_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)


def _bind(name: str, argtypes, restype):
    function = getattr(_kernel32, name)
    function.argtypes = argtypes
    function.restype = restype
    return function


_CreateFileW = _bind(
    "CreateFileW",
    [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    ],
    wintypes.HANDLE,
)
_WaitNamedPipeW = _bind(
    "WaitNamedPipeW",
    [wintypes.LPCWSTR, wintypes.DWORD],
    wintypes.BOOL,
)
_ReadFile = _bind(
    "ReadFile",
    [
        wintypes.HANDLE,
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.LPDWORD,
        wintypes.LPVOID,
    ],
    wintypes.BOOL,
)
_WriteFile = _bind(
    "WriteFile",
    [
        wintypes.HANDLE,
        wintypes.LPCVOID,
        wintypes.DWORD,
        wintypes.LPDWORD,
        wintypes.LPVOID,
    ],
    wintypes.BOOL,
)
_CloseHandle = _bind("CloseHandle", [wintypes.HANDLE], wintypes.BOOL)
_CreateNamedPipeW = _bind(
    "CreateNamedPipeW",
    [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.LPVOID,
    ],
    wintypes.HANDLE,
)
_ConnectNamedPipe = _bind(
    "ConnectNamedPipe",
    [wintypes.HANDLE, wintypes.LPVOID],
    wintypes.BOOL,
)
_DisconnectNamedPipe = _bind(
    "DisconnectNamedPipe",
    [wintypes.HANDLE],
    wintypes.BOOL,
)
_FlushFileBuffers = _bind(
    "FlushFileBuffers",
    [wintypes.HANDLE],
    wintypes.BOOL,
)
_SetNamedPipeHandleState = _bind(
    "SetNamedPipeHandleState",
    [wintypes.HANDLE, wintypes.LPDWORD, wintypes.LPDWORD, wintypes.LPDWORD],
    wintypes.BOOL,
)
_OpenProcess = _bind(
    "OpenProcess",
    [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD],
    wintypes.HANDLE,
)
_TerminateProcess = _bind(
    "TerminateProcess",
    [wintypes.HANDLE, wintypes.UINT],
    wintypes.BOOL,
)
_GetProcessTimes = _bind(
    "GetProcessTimes",
    [
        wintypes.HANDLE,
        ctypes.POINTER(wintypes.FILETIME),
        ctypes.POINTER(wintypes.FILETIME),
        ctypes.POINTER(wintypes.FILETIME),
        ctypes.POINTER(wintypes.FILETIME),
    ],
    wintypes.BOOL,
)


def normalize_pipe_name(name: str) -> str:
    """Return ``name`` in canonical ``\\\\.\\pipe\\...`` form."""
    value = str(name).strip()
    lowered = value.lower()
    if lowered.startswith(r"\\?\pipe" + "\\"):
        return value
    if lowered.startswith(r"\\.\pipe" + "\\"):
        return value
    return r"\\.\pipe" + "\\" + value


def is_pipe_name(name: str) -> bool:
    lowered = str(name).lower()
    return lowered.startswith(r"\\?\pipe" + "\\") or lowered.startswith(
        r"\\.\pipe" + "\\"
    )


def pipe_connect(name: str, timeout: float = 3.0) -> int:
    """Connect to an existing named pipe and return the handle."""
    pipe_name = normalize_pipe_name(name)
    deadline = time.monotonic() + max(0.0, timeout)
    while True:
        handle = _CreateFileW(
            pipe_name,
            GENERIC_READ | GENERIC_WRITE,
            0,
            None,
            OPEN_EXISTING,
            FILE_ATTRIBUTE_NORMAL,
            None,
        )
        if handle and handle != _INVALID_HANDLE_VALUE:
            return int(handle)
        error = ctypes.get_last_error()
        if error == ERROR_PIPE_BUSY:
            remaining_ms = max(1, int((deadline - time.monotonic()) * 1000))
            if not _WaitNamedPipeW(pipe_name, remaining_ms):
                if time.monotonic() >= deadline:
                    raise NamedPipeError(
                        f"named pipe busy: {pipe_name}"
                    )
                continue
            continue
        if error == ERROR_FILE_NOT_FOUND:
            if time.monotonic() >= deadline:
                raise NamedPipeError(
                    f"named pipe not found: {pipe_name}"
                )
            time.sleep(0.02)
            continue
        raise NamedPipeError(
            f"cannot open named pipe {pipe_name}: error {error}"
        )


def pipe_server_available(name: str) -> bool:
    """True when a server instance exists (never consumes a client).

    A listening-but-idle instance answers immediately; a busy instance
    (client currently connected) is still "available" for our purposes.
    """
    pipe_name = normalize_pipe_name(name)
    if _WaitNamedPipeW(pipe_name, 0):
        return True
    error = ctypes.get_last_error()
    return error in (ERROR_PIPE_BUSY, ERROR_SEM_TIMEOUT)


def pipe_write_all(handle: int, data: bytes) -> None:
    total = len(data)
    offset = 0
    while offset < total:
        chunk = data[offset : offset + 65536]
        written = wintypes.DWORD(0)
        ok = _WriteFile(
            handle,
            ctypes.create_string_buffer(chunk),
            len(chunk),
            ctypes.byref(written),
            None,
        )
        if not ok:
            raise NamedPipeError(
                f"named pipe write failed: error {ctypes.get_last_error()}"
            )
        if written.value == 0:
            raise NamedPipeError("named pipe write stalled")
        offset += written.value


def pipe_read(handle: int, size: int, timeout_ms: int = 2000) -> bytes:
    """Read up to ``size`` bytes; returns ``b""`` on timeout or broken pipe."""
    timeout = wintypes.DWORD(max(0, int(timeout_ms)))
    _SetNamedPipeHandleState(handle, None, None, ctypes.byref(timeout))
    buffer = ctypes.create_string_buffer(size)
    read = wintypes.DWORD(0)
    ok = _ReadFile(handle, buffer, size, ctypes.byref(read), None)
    if ok:
        return buffer.raw[: read.value]
    error = ctypes.get_last_error()
    if error in (
        ERROR_BROKEN_PIPE,
        ERROR_NO_DATA,
        ERROR_PIPE_NOT_CONNECTED,
    ):
        return b""
    raise NamedPipeError(
        f"named pipe read failed: error {error}"
    )


def pipe_close(handle: int) -> None:
    if handle and handle != _INVALID_HANDLE_VALUE:
        _CloseHandle(handle)


def flush_pipe(handle: int) -> None:
    """Wait until the peer has consumed all buffered output.

    Required before ``DisconnectNamedPipe``: without it the disconnect can
    discard a response the client has not read yet (ERROR_PIPE_NOT_CONNECTED
    on the client's next read).
    """
    _FlushFileBuffers(handle)


def create_pipe_server(name: str) -> int:
    """Create a single-instance byte-mode duplex named-pipe server."""
    pipe_name = normalize_pipe_name(name)
    handle = _CreateNamedPipeW(
        pipe_name,
        PIPE_ACCESS_DUPLEX,
        PIPE_TYPE_BYTE | PIPE_READMODE_BYTE | PIPE_WAIT,
        1,
        65536,
        65536,
        0,
        None,
    )
    if not handle or handle == _INVALID_HANDLE_VALUE:
        raise NamedPipeError(
            f"CreateNamedPipeW failed for {pipe_name}: error {ctypes.get_last_error()}"
        )
    return int(handle)


def connect_pipe_server(handle: int) -> None:
    """Block until a client connects to the listening pipe instance."""
    ok = _ConnectNamedPipe(handle, None)
    if ok:
        return
    error = ctypes.get_last_error()
    if error == ERROR_PIPE_CONNECTED:
        return
    raise NamedPipeError(
        f"ConnectNamedPipe failed: error {error}"
    )


def disconnect_pipe(handle: int) -> None:
    _DisconnectNamedPipe(handle)


def process_alive(pid: int) -> bool:
    """True when a process with ``pid`` exists (query-only, never signals)."""
    if pid <= 0:
        return False
    handle = _OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
    if not handle:
        error = ctypes.get_last_error()
        if error == ERROR_INVALID_PARAMETER:
            return False
        # Access denied still means the process exists.
        return True
    _CloseHandle(handle)
    return True


def terminate_process(pid: int) -> None:
    """Hard-terminate ``pid`` (used after graceful shutdown fails)."""
    if pid <= 0:
        return
    handle = _OpenProcess(0x0001, False, pid)  # PROCESS_TERMINATE
    if not handle:
        return
    try:
        _TerminateProcess(handle, 1)
    finally:
        _CloseHandle(handle)


def process_start_time(pid: int) -> str | None:
    """ISO-8601 creation-time fingerprint for ``pid`` via GetProcessTimes."""
    if pid <= 0:
        raise OSError(f"invalid pid: {pid}")
    handle = _OpenProcess(0x1000, False, pid)
    if not handle:
        raise OSError(
            f"cannot query process {pid}: error {ctypes.get_last_error()}"
        )
    try:
        creation = wintypes.FILETIME()
        exit_time = wintypes.FILETIME()
        kernel_time = wintypes.FILETIME()
        user_time = wintypes.FILETIME()
        ok = _GetProcessTimes(
            handle,
            ctypes.byref(creation),
            ctypes.byref(exit_time),
            ctypes.byref(kernel_time),
            ctypes.byref(user_time),
        )
        if not ok:
            raise OSError(
                f"GetProcessTimes failed for pid {pid}: error {ctypes.get_last_error()}"
            )
        value = (creation.dwHighDateTime << 32) | creation.dwLowDateTime
        from datetime import datetime, timedelta, timezone

        epoch = datetime(1601, 1, 1, tzinfo=timezone.utc)
        return (epoch + timedelta(microseconds=value // 10)).isoformat()
    finally:
        _CloseHandle(handle)
