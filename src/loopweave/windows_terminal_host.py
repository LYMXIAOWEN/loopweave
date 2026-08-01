"""Windows ConPTY terminal-host backend for LoopWeave.

Implements the same :class:`~loopweave.terminal_host.TerminalHost` contract
as the POSIX :class:`~loopweave.supervisor.Supervisor`, backed by a ConPTY
pseudo console created through ``pywinpty`` (import name ``winpty``,
``Backend.ConPTY``).  The control channel is a per-run Windows named pipe
that speaks the same newline-delimited JSON protocol as the POSIX Unix
socket.
"""

from __future__ import annotations

import ctypes
import json
import os
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

from .protocol import append_event
from .runtime_config import RunPolicy, load_run_policy
from .terminal_host import TerminalHost, UnsupportedPlatformError

try:
    from winpty import PTY
    from winpty.enums import Backend
except ImportError:  # pragma: no cover - guarded by factory and constructor
    PTY = None  # type: ignore[assignment]
    Backend = None  # type: ignore[assignment]


class WindowsHostError(RuntimeError):
    pass


def process_start_time(pid: int) -> str:
    """ISO-8601 creation-time fingerprint for ``pid``."""
    from .win32_pipe import process_start_time as _windows_start_time

    value = _windows_start_time(pid)
    if value is None:
        raise OSError(f"cannot determine start time for pid {pid}")
    return value


def process_alive(pid: int) -> bool:
    """True when ``pid`` exists; never sends signals to the target."""
    from .win32_pipe import process_alive as _windows_alive

    return _windows_alive(pid)


def _resolve_command(command: list[str]) -> str:
    if not command:
        raise WindowsHostError("command is required")
    resolved = shutil.which(command[0])
    if not resolved:
        raise WindowsHostError(
            f"command not found in PATH: {command[0]}"
        )
    return resolved


def _console_size() -> tuple[int, int]:
    size = shutil.get_terminal_size(fallback=(80, 24))
    return (int(size.lines), int(size.columns))


# msvcrt reports extended keys (arrows, editing keys, F1-F10) as a lead
# byte ("\x00" or "\xe0") plus a scan-code byte. ConPTY consumes the same
# ANSI escape sequences the control channel already uses, so translate the
# two-character sequences before writing them to the pseudo console.
_CONSOLE_EXTENDED_KEYS: dict[str, str] = {
    "\x00H": "\x1b[A",  # Up
    "\xe0H": "\x1b[A",
    "\x00P": "\x1b[B",  # Down
    "\xe0P": "\x1b[B",
    "\x00M": "\x1b[C",  # Right
    "\xe0M": "\x1b[C",
    "\x00K": "\x1b[D",  # Left
    "\xe0K": "\x1b[D",
    "\x00G": "\x1b[H",  # Home
    "\xe0G": "\x1b[H",
    "\x00O": "\x1b[F",  # End
    "\xe0O": "\x1b[F",
    "\x00I": "\x1b[5~",  # Page Up
    "\xe0I": "\x1b[5~",
    "\x00Q": "\x1b[6~",  # Page Down
    "\xe0Q": "\x1b[6~",
    "\x00R": "\x1b[2~",  # Insert
    "\xe0R": "\x1b[2~",
    "\x00S": "\x1b[3~",  # Delete
    "\xe0S": "\x1b[3~",
    "\x00;": "\x1bOP",  # F1
    "\x00<": "\x1bOQ",  # F2
    "\x00=": "\x1bOR",  # F3
    "\x00>": "\x1bOS",  # F4
    "\x00?": "\x1b[15~",  # F5
    "\x00@": "\x1b[17~",  # F6
    "\x00A": "\x1b[18~",  # F7
    "\x00B": "\x1b[19~",  # F8
    "\x00C": "\x1b[20~",  # F9
    "\x00D": "\x1b[21~",  # F10
}


def _translate_console_key(sequence: str) -> str:
    """Map an msvcrt extended-key sequence to the ANSI escape sequence
    ConPTY expects; unrecognized input passes through unchanged."""
    return _CONSOLE_EXTENDED_KEYS.get(sequence, sequence)


class WindowsConPtyHost(TerminalHost):
    """ConPTY-based managed-session boundary for ``win32``."""

    def __init__(
        self,
        run_id: str,
        command: list[str],
        cwd: Path,
        run_dir: Path,
        socket_path: Path,
        control_token: str,
        passthrough: bool = True,
        log_policy: RunPolicy | None = None,
    ) -> None:
        if PTY is None:
            raise UnsupportedPlatformError(
                "native Windows terminal hosting requires the 'windows' "
                "extra; install with: pip install 'loopweave[windows]'"
            )
        if not command:
            raise ValueError("command is required")
        self.run_id = run_id
        self.command = list(command)
        self.cwd = Path(cwd)
        self.run_dir = Path(run_dir)
        self.control_token = control_token
        self.passthrough = passthrough
        self.log_policy = log_policy if log_policy is not None else load_run_policy()
        endpoint = str(socket_path)
        if not (
            endpoint.lower().startswith(r"\\?\pipe" + "\\")
            or endpoint.lower().startswith(r"\\.\pipe" + "\\")
        ):
            raise ValueError(
                "Windows control endpoint must be a named pipe, got: "
                f"{endpoint}"
            )
        self.pipe_name = endpoint
        self._pty: Any | None = None
        self._reader_thread: threading.Thread | None = None
        self._server_thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._write_lock = threading.Lock()
        self._output_stats_lock = threading.Lock()
        self._output_bytes = 0
        self._last_output_monotonic: float | None = None
        self._console_handler_ref: Any | None = None
        self._server_pipe_handle: int | None = None
        self._last_console_size: tuple[int, int] | None = None

    # ------------------------------------------------------------------
    # TerminalHost interface
    # ------------------------------------------------------------------

    def start(self) -> int:
        if self._pty is not None:
            raise WindowsHostError("windows host already started")
        self.run_dir.mkdir(parents=True, exist_ok=True)
        rows, cols = _console_size()
        self._last_console_size = (rows, cols)
        appname = _resolve_command(self.command)
        cmdline = (
            subprocess.list2cmdline(self.command[1:])
            if len(self.command) > 1
            else None
        )
        child_env = dict(os.environ)
        child_env["LOOPWEAVE_RUN_ID"] = self.run_id
        env_string = "".join(
            f"{key}={value}\0"
            for key, value in child_env.items()
        )
        pty = PTY(cols, rows, backend=Backend.ConPTY)
        try:
            pty.spawn(
                appname,
                cmdline=cmdline,
                cwd=str(self.cwd),
                env=env_string,
            )
        except Exception as error:
            try:
                pty.cancel_io()
            except Exception:
                pass
            raise WindowsHostError(
                f"ConPTY spawn failed for {appname}: {error}"
            ) from error
        self._pty = pty
        self._record_terminal_event(
            "terminal_started",
            {
                "stdin_isatty": bool(sys.stdin.isatty()),
                "stdout_isatty": bool(sys.stdout.isatty()),
                "size": {"rows": rows, "columns": cols},
                "conpty": True,
            },
        )
        self._reader_thread = threading.Thread(
            target=self._read_output,
            name="loopweave-output",
            daemon=True,
        )
        self._server_thread = threading.Thread(
            target=self._serve_control,
            name="loopweave-control",
            daemon=True,
        )
        self._reader_thread.start()
        self._server_thread.start()
        self._install_console_ctrl_handler()
        pid = pty.pid
        if not pid:
            raise WindowsHostError("ConPTY reported no child pid")
        return int(pid)

    def send_input(self, text: str) -> None:
        pty = self._pty
        if pty is None:
            raise WindowsHostError("windows host is not running")
        if not pty.isalive():
            raise WindowsHostError("managed process has exited")
        # Console line input completes on CR, not LF.  Normalize bare LF to
        # CRLF so control-channel text behaves like POSIX ``\n`` input while
        # raw-mode terminal agents still receive the CR a real Enter emits.
        normalized = text.replace("\r\n", "\n").replace("\n", "\r\n")
        with self._write_lock:
            pty.write(normalized)

    def run_foreground(self) -> int:
        if self._pty is None:
            self.start()
        if not sys.stdin.isatty():
            return self._wait()
        import msvcrt

        last_size_check = 0.0
        while self._is_alive() and not self._stop_event.is_set():
            if msvcrt.kbhit():
                char = msvcrt.getwch()
                # Function keys and arrows arrive as a two-character
                # sequence (lead byte + scan code).
                if char in ("\x00", "\xe0"):
                    try:
                        char += msvcrt.getwch()
                    except Exception:
                        pass
                    char = _translate_console_key(char)
                try:
                    self.send_input(char)
                except WindowsHostError:
                    break
            now = time.monotonic()
            if now - last_size_check >= 0.2:
                last_size_check = now
                self._sync_console_size()
            else:
                time.sleep(0.01)
        return self._wait()

    def _sync_console_size(self) -> None:
        """Follow the host console size while running in the foreground.

        Windows delivers no SIGWINCH, so poll the console size and push
        changes into ConPTY for terminal TUIs that reflow on resize.
        """
        pty = self._pty
        if pty is None:
            return
        rows, cols = _console_size()
        if (rows, cols) == self._last_console_size:
            return
        self._last_console_size = (rows, cols)
        self.resize(rows, cols)

    def resize(self, rows: int, cols: int) -> None:
        pty = self._pty
        if pty is None:
            return
        try:
            pty.set_size(int(cols), int(rows))
        except Exception:
            return
        self._record_terminal_event(
            "terminal_resized",
            {"rows": int(rows), "columns": int(cols)},
        )

    def process_identity(self) -> tuple[int, str]:
        pty = self._pty
        if pty is None or pty.pid is None:
            raise WindowsHostError("windows host is not running")
        return int(pty.pid), process_start_time(int(pty.pid))

    def stop(self) -> None:
        already_stopped = (
            self._stop_event.is_set() and self._pty is None
        )
        if already_stopped:
            return
        self._stop_event.set()
        pty = self._pty
        if pty is not None:
            self._graceful_stop(pty)
            self._hard_stop(pty)
            self._poke_control_server()
            try:
                pty.cancel_io()
            except Exception:
                pass
        for thread in (self._server_thread, self._reader_thread):
            if thread is not None:
                thread.join(timeout=2.0)
        self._restore_console_ctrl_handler()
        self._pty = None

    def wait(self, timeout: float | None = None) -> int:
        pty = self._pty
        if pty is None:
            raise WindowsHostError("windows host is not running")
        deadline = time.monotonic() + timeout if timeout is not None else None
        while pty.isalive():
            if deadline is not None and time.monotonic() >= deadline:
                raise WindowsHostError("managed process did not exit")
            time.sleep(0.05)
        return int(pty.get_exitstatus() or 0)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _is_alive(self) -> bool:
        pty = self._pty
        return bool(pty is not None and pty.isalive())

    def _wait(self) -> int:
        pty = self._pty
        if pty is None:
            return 0
        while pty.isalive() and not self._stop_event.is_set():
            time.sleep(0.05)
        return int(pty.get_exitstatus() or 0)

    def _graceful_stop(self, pty: Any) -> None:
        if not pty.isalive():
            return
        try:
            pty.write("\x03")  # Ctrl+C into the pseudo console
        except Exception:
            pass
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline and pty.isalive():
            time.sleep(0.05)

    def _hard_stop(self, pty: Any) -> None:
        if not pty.isalive() or pty.pid is None:
            return
        pid = int(pty.pid)
        try:
            subprocess.run(
                ["taskkill", "/PID", str(pid), "/T", "/F"],
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            try:
                from .win32_pipe import terminate_process
            except ImportError:
                return

            terminate_process(pid)
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline and pty.isalive():
            time.sleep(0.05)

    def _poke_control_server(self) -> None:
        """Unblock a pending ConnectNamedPipe so the server thread can exit."""
        try:
            from .win32_pipe import pipe_close, pipe_connect
        except ImportError:
            return

        try:
            handle = pipe_connect(self.pipe_name, timeout=0.5)
        except Exception:
            return
        try:
            pipe_close(handle)
        except Exception:
            pass

    def _read_output(self) -> None:
        pty = self._pty
        assert pty is not None
        raw_path = self.run_dir / "terminal.raw.log"
        text_path = self.run_dir / "terminal.txt"
        while not self._stop_event.is_set():
            try:
                chunk = pty.read(blocking=False)
            except Exception:
                break
            if chunk:
                self._process_output(chunk, raw_path, text_path)
            if pty.iseof():
                for _ in range(5):
                    try:
                        chunk = pty.read(blocking=False)
                    except Exception:
                        break
                    if not chunk:
                        break
                    self._process_output(chunk, raw_path, text_path)
                    time.sleep(0.01)
                break
            if not pty.isalive() and not chunk:
                break
            time.sleep(0.002)

    def _process_output(
        self, chunk: str, raw_path: Path, text_path: Path
    ) -> None:
        data = (
            chunk.encode("utf-8", errors="replace")
            if isinstance(chunk, str)
            else bytes(chunk)
        )
        with self._output_stats_lock:
            self._output_bytes += len(data)
            self._last_output_monotonic = time.monotonic()
        if self.log_policy.raw_log_enabled:
            self._append_rotating_log(
                raw_path,
                data,
                max_bytes=self.log_policy.raw_log_max_bytes,
                backups=self.log_policy.raw_log_backups,
                log_kind="terminal.raw.log",
            )
        self._append_rotating_log(
            text_path,
            data,
            max_bytes=self.log_policy.terminal_log_max_bytes,
            backups=self.log_policy.terminal_log_backups,
            log_kind="terminal.txt",
        )
        if self.passthrough:
            try:
                sys.stdout.buffer.write(data)
                sys.stdout.buffer.flush()
            except (OSError, AttributeError):
                pass

    def _append_rotating_log(
        self,
        path: Path,
        data: bytes,
        *,
        max_bytes: int,
        backups: int,
        log_kind: str,
    ) -> None:
        original_size = path.stat().st_size if path.exists() else 0
        rotated = original_size > 0 and original_size + len(data) > max_bytes
        if rotated:
            if backups > 0:
                oldest = path.with_name(f"{path.name}.{backups}")
                try:
                    oldest.unlink()
                except FileNotFoundError:
                    pass
                for index in range(backups - 1, 0, -1):
                    source = path.with_name(f"{path.name}.{index}")
                    target = path.with_name(
                        f"{path.name}.{index + 1}"
                    )
                    if source.exists():
                        os.replace(str(source), str(target))
                os.replace(
                    str(path),
                    str(path.with_name(f"{path.name}.1")),
                )
            else:
                path.unlink()
            self._record_terminal_event(
                "terminal_log_rotated",
                {
                    "log_kind": log_kind,
                    "original_size": original_size,
                    "max_bytes": max_bytes,
                    "backups": backups,
                },
            )
        bounded = data[-max_bytes:] if len(data) > max_bytes else data
        with path.open("ab", buffering=0) as handle:
            handle.write(bounded)

    def _serve_control(self) -> None:
        try:
            from .win32_pipe import (
                NamedPipeError,
                connect_pipe_server,
                create_pipe_server,
                disconnect_pipe,
                flush_pipe,
                pipe_close,
                pipe_write_all,
            )
        except ImportError:
            return

        handle = None
        try:
            handle = create_pipe_server(self.pipe_name)
        except NamedPipeError:
            return
        self._server_pipe_handle = handle
        while not self._stop_event.is_set():
            try:
                connect_pipe_server(handle)
                response = self._handle_control_connection(handle)
                try:
                    pipe_write_all(
                        handle,
                        (json.dumps(response) + "\n").encode("utf-8"),
                    )
                except NamedPipeError:
                    pass
                try:
                    flush_pipe(handle)
                except NamedPipeError:
                    pass
                disconnect_pipe(handle)
            except NamedPipeError:
                try:
                    disconnect_pipe(handle)
                except NamedPipeError:
                    pass
                if self._stop_event.is_set():
                    break
                time.sleep(0.05)
                continue
            except Exception:
                try:
                    disconnect_pipe(handle)
                except NamedPipeError:
                    pass
                if self._stop_event.is_set():
                    break
                time.sleep(0.05)
                continue
        if handle is not None:
            pipe_close(handle)
        self._server_pipe_handle = None

    def _handle_control_connection(self, handle: int) -> dict[str, Any]:
        data = bytearray()
        deadline = time.monotonic() + 5.0
        while b"\n" not in data and len(data) < 1024 * 1024:
            if self._stop_event.is_set():
                break
            if time.monotonic() >= deadline:
                break
            from .win32_pipe import pipe_read

            try:
                chunk = pipe_read(handle, 65536, timeout_ms=200)
            except Exception:
                break
            if not chunk:
                break
            data.extend(chunk)
        try:
            payload = json.loads(
                bytes(data).split(b"\n", 1)[0].decode("utf-8")
            )
        except (ValueError, UnicodeDecodeError):
            return {"status": "error", "message": "invalid JSON"}
        if payload.get("token") != self.control_token:
            return {"status": "error", "message": "invalid control token"}
        action = payload.get("action")
        if action == "send":
            text = payload.get("text")
            if not isinstance(text, str):
                return {"status": "error", "message": "text is required"}
            try:
                self.send_input(text)
            except WindowsHostError as error:
                return {"status": "error", "message": str(error)}
            return {"status": "ok", "run_id": self.run_id}
        if action == "status":
            with self._output_stats_lock:
                output_bytes = self._output_bytes
                last_output = self._last_output_monotonic
            pty = self._pty
            return {
                "status": "ok",
                "run_id": self.run_id,
                "pid": int(pty.pid) if pty is not None and pty.pid else None,
                "running": bool(pty is not None and pty.isalive()),
                "terminal_output_bytes": output_bytes,
                "terminal_idle_seconds": (
                    max(0.0, time.monotonic() - last_output)
                    if last_output is not None
                    else None
                ),
            }
        if action == "stop":
            self._stop_event.set()
            pty = self._pty
            if pty is not None and pty.isalive():
                try:
                    pty.write("\x03")
                except Exception:
                    pass
                # Graceful Ctrl+C is best-effort (a console app may ignore
                # it); enforce termination in a background thread so the
                # control response returns promptly while the agent is
                # killed and the control server is unblocked.
                threading.Thread(
                    target=self._enforce_stop,
                    args=(pty,),
                    daemon=True,
                ).start()
            return {"status": "ok", "run_id": self.run_id}
        return {"status": "error", "message": "unsupported action"}

    def _enforce_stop(self, pty: Any) -> None:
        # Short graceful window for the control-channel stop path: the CLI
        # stop command verifies process exit within 6 seconds on Windows
        # (STOP_VERIFY_TIMEOUT_SECONDS), so Ctrl+C gets a brief chance and
        # then process-tree termination guarantees the deadline is met.
        deadline = time.monotonic() + 0.8
        while time.monotonic() < deadline and pty.isalive():
            time.sleep(0.05)
        self._hard_stop(pty)
        self._poke_control_server()

    def _record_terminal_event(
        self, event: str, payload: dict[str, Any] | None = None
    ) -> None:
        data = dict(payload or {})
        data["event"] = event
        data["run_id"] = self.run_id
        try:
            append_event(self.run_dir / "terminal-events.jsonl", data)
        except OSError:
            return

    # ------------------------------------------------------------------
    # Console Ctrl+C forwarding
    # ------------------------------------------------------------------

    def _install_console_ctrl_handler(self) -> None:
        if sys.platform != "win32":
            return
        try:
            handler = _ConsoleCtrlHandler(self)
            if _set_console_ctrl_handler(handler.callback, True):
                # Keep the ctypes callback alive for the lifetime of the host.
                self._console_handler_ref = handler
        except Exception:
            self._console_handler_ref = None

    def _restore_console_ctrl_handler(self) -> None:
        if self._console_handler_ref is not None:
            try:
                _set_console_ctrl_handler(
                    self._console_handler_ref.callback, False
                )
            except Exception:
                pass
            self._console_handler_ref = None


class _ConsoleCtrlHandler:
    """Forwards console Ctrl+C / Ctrl+Break into the ConPTY child."""

    CTRL_C_EVENT = 0
    CTRL_BREAK_EVENT = 1

    def __init__(self, host: WindowsConPtyHost) -> None:
        self._host = host
        self.callback = ctypes.WINFUNCTYPE(
            ctypes.c_int, ctypes.c_uint
        )(self._on_control)

    def _on_control(self, control_type: int) -> int:
        if control_type in (self.CTRL_C_EVENT, self.CTRL_BREAK_EVENT):
            pty = self._host._pty
            if pty is not None and pty.isalive():
                try:
                    pty.write("\x03")
                except Exception:
                    pass
            return 1
        return 0


def _set_console_ctrl_handler(callback, add: bool) -> bool:
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    set_handler = kernel32.SetConsoleCtrlHandler
    set_handler.argtypes = [ctypes.c_void_p, ctypes.c_int]
    set_handler.restype = ctypes.c_int
    return bool(set_handler(callback, 1 if add else 0))
