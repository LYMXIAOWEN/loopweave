from __future__ import annotations

import json
import os
import select
import signal
import socket
import struct
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .protocol import append_event
from .terminal_host import TerminalHost

try:
    import fcntl
except ImportError:  # pragma: no cover - Unix-only project
    fcntl = None

try:
    import pty
    import termios
    import tty
except ImportError:  # pragma: no cover - Unix-only project
    pty = None  # type: ignore[assignment]
    termios = None  # type: ignore[assignment]
    tty = None  # type: ignore[assignment]


class SupervisorError(RuntimeError):
    pass


def process_start_time(pid: int) -> str:
    result = subprocess.run(
        ["ps", "-p", str(pid), "-o", "lstart="],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def send_control_message(
    socket_path: Path, payload: Dict[str, Any], timeout: float = 3.0
) -> Dict[str, Any]:
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.settimeout(timeout)
        client.connect(str(socket_path))
        client.sendall(
            (json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8")
        )
        response = bytearray()
        while b"\n" not in response:
            chunk = client.recv(65536)
            if not chunk:
                break
            response.extend(chunk)
    if not response:
        raise SupervisorError("control socket returned no response")
    return json.loads(bytes(response).split(b"\n", 1)[0].decode("utf-8"))


class Supervisor(TerminalHost):
    def __init__(
        self,
        run_id: str,
        command: List[str],
        cwd: Path,
        run_dir: Path,
        socket_path: Path,
        control_token: str,
        passthrough: bool = True,
    ) -> None:
        if not command:
            raise ValueError("command is required")
        self.run_id = run_id
        self.command = list(command)
        self.cwd = Path(cwd)
        self.run_dir = Path(run_dir)
        self.socket_path = Path(socket_path)
        self.control_token = control_token
        self.passthrough = passthrough
        self.process: Optional[subprocess.Popen] = None
        self.master_fd: Optional[int] = None
        self._reader_thread: Optional[threading.Thread] = None
        self._server_thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._write_lock = threading.Lock()
        self._server: Optional[socket.socket] = None

    def start(self) -> int:
        if self.process is not None:
            raise SupervisorError("supervisor already started")
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self.socket_path.parent.mkdir(parents=True, exist_ok=True)
        if self.socket_path.exists():
            self.socket_path.unlink()

        master_fd, slave_fd = pty.openpty()
        attributes = termios.tcgetattr(slave_fd)
        attributes[3] &= ~termios.ECHO
        termios.tcsetattr(slave_fd, termios.TCSANOW, attributes)
        self._copy_terminal_size(slave_fd)
        self._record_terminal_event(
            "terminal_started",
            {
                "stdin_isatty": bool(sys.stdin.isatty()),
                "stdout_isatty": bool(sys.stdout.isatty()),
                "size": self._terminal_size_from_stdin(),
            },
        )
        child_env = dict(os.environ)
        child_env["LOOPWEAVE_RUN_ID"] = self.run_id
        self.process = subprocess.Popen(
            self.command,
            cwd=str(self.cwd),
            stdin=slave_fd,
            stdout=slave_fd,
            stderr=slave_fd,
            close_fds=True,
            start_new_session=True,
            env=child_env,
        )
        os.close(slave_fd)
        self.master_fd = master_fd
        self._reader_thread = threading.Thread(
            target=self._read_output, name="loopweave-output", daemon=True
        )
        self._server_thread = threading.Thread(
            target=self._serve_control, name="loopweave-control", daemon=True
        )
        self._reader_thread.start()
        self._server_thread.start()
        self._wait_for_socket()
        return self.process.pid

    def send_input(self, text: str) -> None:
        if self.master_fd is None or self.process is None:
            raise SupervisorError("supervisor is not running")
        if self.process.poll() is not None:
            raise SupervisorError("managed process has exited")
        data = text.encode("utf-8")
        with self._write_lock:
            os.write(self.master_fd, data)

    def run_foreground(self) -> int:
        if self.process is None:
            self.start()
        assert self.process is not None
        if not sys.stdin.isatty():
            return self.process.wait()

        stdin_fd = sys.stdin.fileno()
        old_attributes = termios.tcgetattr(stdin_fd)
        old_winch_handler = signal.getsignal(signal.SIGWINCH)

        def handle_winch(signum: int, frame: object) -> None:
            self._resize_child_pty()

        try:
            signal.signal(signal.SIGWINCH, handle_winch)
            self._resize_child_pty()
            tty.setraw(stdin_fd)
            while self.process.poll() is None and not self._stop_event.is_set():
                readable, _, _ = select.select([stdin_fd], [], [], 0.1)
                if stdin_fd in readable:
                    data = os.read(stdin_fd, 4096)
                    if not data:
                        break
                    with self._write_lock:
                        if self.master_fd is not None:
                            os.write(self.master_fd, data)
        finally:
            signal.signal(signal.SIGWINCH, old_winch_handler)
            termios.tcsetattr(stdin_fd, termios.TCSADRAIN, old_attributes)
        return self.process.wait()

    def wait(self, timeout: Optional[float] = None) -> int:
        if self.process is None:
            raise SupervisorError("supervisor is not running")
        return self.process.wait(timeout=timeout)

    def resize(self, rows: int, cols: int) -> None:
        self._resize_child_pty({"rows": rows, "columns": cols, "xpixel": 0, "ypixel": 0})

    def process_identity(self) -> Tuple[int, str]:
        if self.process is None:
            raise SupervisorError("supervisor is not running")
        return self.process.pid, process_start_time(self.process.pid)

    def stop(self) -> None:
        self._stop_event.set()
        if self.process is not None and self.process.poll() is None:
            try:
                os.killpg(self.process.pid, signal.SIGTERM)
                self.process.wait(timeout=2)
            except (ProcessLookupError, subprocess.TimeoutExpired):
                if self.process.poll() is None:
                    os.killpg(self.process.pid, signal.SIGKILL)
        if self._server is not None:
            try:
                self._server.close()
            except OSError:
                pass
        if self.master_fd is not None:
            try:
                os.close(self.master_fd)
            except OSError:
                pass
            self.master_fd = None
        if self.socket_path.exists():
            try:
                self.socket_path.unlink()
            except OSError:
                pass

    def _read_output(self) -> None:
        assert self.master_fd is not None
        raw_path = self.run_dir / "terminal.raw.log"
        text_path = self.run_dir / "terminal.txt"
        with raw_path.open("ab", buffering=0) as raw_handle, text_path.open(
            "a", encoding="utf-8", errors="replace", buffering=1
        ) as text_handle:
            while not self._stop_event.is_set():
                try:
                    readable, _, _ = select.select([self.master_fd], [], [], 0.1)
                    if self.master_fd not in readable:
                        if self.process is not None and self.process.poll() is not None:
                            break
                        continue
                    data = os.read(self.master_fd, 65536)
                    if not data:
                        break
                except OSError:
                    break
                raw_handle.write(data)
                text_handle.write(data.decode("utf-8", errors="replace"))
                text_handle.flush()
                if self.passthrough:
                    try:
                        os.write(sys.stdout.fileno(), data)
                    except OSError:
                        pass

    def _serve_control(self) -> None:
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._server = server
        server.bind(str(self.socket_path))
        os.chmod(self.socket_path, 0o600)
        server.listen(4)
        server.settimeout(0.2)
        while not self._stop_event.is_set():
            try:
                connection, _ = server.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            with connection:
                response = self._handle_connection(connection)
                try:
                    connection.sendall(
                        (json.dumps(response) + "\n").encode("utf-8")
                    )
                except OSError:
                    pass

    def _handle_connection(self, connection: socket.socket) -> Dict[str, Any]:
        data = bytearray()
        while b"\n" not in data and len(data) < 1024 * 1024:
            chunk = connection.recv(65536)
            if not chunk:
                break
            data.extend(chunk)
        try:
            payload = json.loads(bytes(data).split(b"\n", 1)[0].decode("utf-8"))
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
            except SupervisorError as error:
                return {"status": "error", "message": str(error)}
            return {"status": "ok", "run_id": self.run_id}
        if action == "status":
            return {
                "status": "ok",
                "run_id": self.run_id,
                "pid": self.process.pid if self.process else None,
                "running": bool(self.process and self.process.poll() is None),
            }
        if action == "stop":
            self._stop_event.set()
            if self.process is not None and self.process.poll() is None:
                os.killpg(self.process.pid, signal.SIGTERM)
            return {"status": "ok", "run_id": self.run_id}
        return {"status": "error", "message": "unsupported action"}

    def _wait_for_socket(self, timeout: float = 2.0) -> None:
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.socket_path.exists():
                return
            if self._server_thread is not None and not self._server_thread.is_alive():
                break
            time.sleep(0.01)
        raise SupervisorError("control socket did not start")

    @staticmethod
    def _copy_terminal_size(target_fd: int) -> None:
        if fcntl is None or not sys.stdin.isatty():
            return
        try:
            size = fcntl.ioctl(sys.stdin.fileno(), termios.TIOCGWINSZ, b"\0" * 8)
            rows, columns, xpixel, ypixel = struct.unpack("HHHH", size)
            fcntl.ioctl(
                target_fd,
                termios.TIOCSWINSZ,
                struct.pack("HHHH", rows, columns, xpixel, ypixel),
            )
        except OSError:
            return

    def _record_terminal_event(
        self, event: str, payload: Optional[Dict[str, Any]] = None
    ) -> None:
        data = dict(payload or {})
        data["event"] = event
        data["run_id"] = self.run_id
        try:
            append_event(self.run_dir / "terminal-events.jsonl", data)
        except OSError:
            return

    @staticmethod
    def _terminal_size_from_stdin() -> Optional[Dict[str, int]]:
        if fcntl is None or not sys.stdin.isatty():
            return None
        try:
            size = fcntl.ioctl(sys.stdin.fileno(), termios.TIOCGWINSZ, b"\0" * 8)
            rows, columns, xpixel, ypixel = struct.unpack("HHHH", size)
            return {
                "rows": rows,
                "columns": columns,
                "xpixel": xpixel,
                "ypixel": ypixel,
            }
        except OSError:
            return None

    def _resize_child_pty(self, size: Optional[Dict[str, int]] = None) -> None:
        if fcntl is None or self.master_fd is None:
            return
        current = size or self._terminal_size_from_stdin()
        if current is None:
            return
        try:
            fcntl.ioctl(
                self.master_fd,
                termios.TIOCSWINSZ,
                struct.pack(
                    "HHHH",
                    int(current["rows"]),
                    int(current["columns"]),
                    int(current.get("xpixel", 0)),
                    int(current.get("ypixel", 0)),
                ),
            )
        except OSError:
            return
        self._record_terminal_event(
            "terminal_resized",
            {
                "rows": int(current["rows"]),
                "columns": int(current["columns"]),
                "xpixel": int(current.get("xpixel", 0)),
                "ypixel": int(current.get("ypixel", 0)),
            },
        )
