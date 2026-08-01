"""Stage 2 acceptance contracts for Package 1.

See docs/decisions/0001-vendor-neutral-terminal-host-and-submission-protocol.md.

These tests encode the Stage 2 acceptance criteria from the approved task
packet before Stage 2 is implemented. They are expected to fail against the
Stage 1 baseline (missing modules, missing CLI subcommand, missing adapter
fallback). Stage 2 must make every test in this file pass without loosening
an assertion here. If a contract turns out to be wrong, fix the ADR and this
file together, not just the assertion.
"""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest.mock import patch

SRC_ROOT = Path(__file__).resolve().parents[1] / "src"


class _FakeBackend:
    ConPTY = 0
    WinPTY = 1


class _FakePTY:
    """Synthetic stand-in for winpty.PTY used by the Windows contract tests.

    Keeps the Windows backend reachable on any host (including POSIX CI)
    without touching a real ConPTY.  The fake child is always dead so the
    host lifecycle completes without a live process.
    """

    def __init__(self, cols: int, rows: int, backend=None, **kwargs) -> None:
        self._fake_pid = 424242

    @property
    def pid(self) -> int:
        return self._fake_pid

    def spawn(self, appname, cmdline=None, cwd=None, env=None) -> bool:
        return True

    def read(self, blocking: bool = False) -> str:
        return ""

    def write(self, to_write: str) -> int:
        return len(to_write)

    def set_size(self, cols: int, rows: int) -> None:
        pass

    def isalive(self) -> bool:
        return False

    def iseof(self) -> bool:
        return False

    def get_exitstatus(self) -> int:
        return 0

    def cancel_io(self) -> bool:
        return True


# Uses importlib.abc.MetaPathFinder's find_spec() protocol, not the legacy
# find_module()/load_module() fallback that Python 3.12 removed from
# sys.meta_path handling (see cpython commit 5c238225 "[3.12] gh-112419:
# Document removal of sys.meta_path's 'find_module' fallback"). This
# project declares requires-python = ">=3.10", so the blocker must work on
# 3.12+ as well as the interpreter running this file.
_POSIX_IMPORT_BLOCKER_PREAMBLE = textwrap.dedent(
    """
    import sys

    class _Blocker:
        blocked = {"pty", "termios", "tty", "fcntl"}

        def find_spec(self, name, path=None, target=None):
            if name in self.blocked:
                raise ImportError("simulated missing module: {}".format(name))
            return None

    sys.meta_path.insert(0, _Blocker())
    """
)


class AdapterGenericFallbackContractTests(unittest.TestCase):
    """ADR 0001 section 6: unknown CLI names must become generic workers."""

    def test_unrecognized_name_falls_back_to_generic_adapter(self) -> None:
        from loopweave.adapters import get_adapter
        from loopweave.adapters.generic import GenericAdapter

        adapter = get_adapter("codex", [], run_id="run-1")

        self.assertIsInstance(adapter, GenericAdapter)
        self.assertEqual(
            adapter.build_command(Path("/tmp/run")),
            ["codex"],
        )

    def test_unrecognized_name_forwards_extra_args(self) -> None:
        from loopweave.adapters import get_adapter

        for name in ("codex", "opencode", "kimi"):
            with self.subTest(name=name):
                adapter = get_adapter(name, ["--flag", "value"], run_id="run-1")
                self.assertEqual(
                    adapter.build_command(Path("/tmp/run")),
                    [name, "--flag", "value"],
                )

    def test_claude_still_resolves_to_named_adapter(self) -> None:
        from loopweave.adapters import get_adapter
        from loopweave.adapters.claude import ClaudeAdapter

        adapter = get_adapter("claude", [], run_id="run-1")

        self.assertIsInstance(adapter, ClaudeAdapter)

    def test_unknown_adapter_exception_type_is_removed_or_unused(self) -> None:
        """UnknownAdapter must not be raised by get_adapter once every name
        falls through to GenericAdapter (ADR 0001 section 6)."""
        import loopweave.adapters as adapters_module

        get_adapter = adapters_module.get_adapter
        for name in ("codex", "opencode", "kimi", "totally-made-up-cli"):
            with self.subTest(name=name):
                try:
                    get_adapter(name, [], run_id="run-1")
                except Exception as error:  # noqa: BLE001 - contract probe
                    self.fail(
                        "get_adapter({!r}) must not raise, got {!r}".format(
                            name, error
                        )
                    )


class PlatformImportSafetyContractTests(unittest.TestCase):
    """ADR 0001 sections 1-2: platform-neutral modules must import cleanly
    even when POSIX-only modules are unavailable, on any platform."""

    def _run_probe(self, probe_body: str) -> subprocess.CompletedProcess:
        script = _POSIX_IMPORT_BLOCKER_PREAMBLE + probe_body
        return subprocess.run(
            [sys.executable, "-c", script],
            cwd=str(SRC_ROOT),
            capture_output=True,
            text=True,
            timeout=30,
        )

    def test_cli_module_imports_without_posix_only_modules(self) -> None:
        result = self._run_probe(
            "import loopweave.cli\nprint('IMPORT_OK')\n"
        )
        self.assertEqual(
            result.returncode,
            0,
            "importing loopweave.cli must not require pty/termios/tty/"
            "fcntl; stderr:\n{}".format(result.stderr),
        )
        self.assertIn("IMPORT_OK", result.stdout)

    def test_bridge_control_module_imports_without_fcntl(self) -> None:
        result = self._run_probe(
            "import loopweave.bridge_control\nprint('IMPORT_OK')\n"
        )
        self.assertEqual(
            result.returncode,
            0,
            "importing loopweave.bridge_control must not require "
            "fcntl at module scope; stderr:\n{}".format(result.stderr),
        )
        self.assertIn("IMPORT_OK", result.stdout)

    def test_terminal_host_module_imports_without_posix_only_modules(self) -> None:
        result = self._run_probe(
            "import loopweave.terminal_host\nprint('IMPORT_OK')\n"
        )
        self.assertEqual(
            result.returncode,
            0,
            "loopweave.terminal_host must not import pty/termios/tty/"
            "fcntl at module scope; stderr:\n{}".format(result.stderr),
        )
        self.assertIn("IMPORT_OK", result.stdout)

    def test_supervisor_posix_module_still_imports_pty_directly(self) -> None:
        """The POSIX backend module itself is allowed - required - to use
        pty/termios/fcntl. Only the platform-neutral surface must avoid an
        unconditional dependency on them (ADR 0001 section 1)."""
        import loopweave.supervisor as supervisor_module

        self.assertTrue(hasattr(supervisor_module, "pty"))


class TerminalHostFactoryContractTests(unittest.TestCase):
    """ADR 0001 sections 1-2: a factory selects the backend at call time;
    unsupported platforms fail closed with an actionable error, not a
    fake/unverified ConPTY implementation."""

    def test_posix_platform_returns_posix_backend(self) -> None:
        from loopweave.terminal_host import create_terminal_host

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with patch("loopweave.terminal_host.sys.platform", "darwin"):
                host = create_terminal_host(
                    run_id="run-1",
                    command=[sys.executable, "-c", "pass"],
                    cwd=root,
                    run_dir=root / "run",
                    socket_path=root / "control.sock",
                    control_token="secret",
                )
            try:
                self.assertTrue(hasattr(host, "start"))
                self.assertTrue(hasattr(host, "send_input"))
                self.assertTrue(hasattr(host, "stop"))
                self.assertTrue(hasattr(host, "resize"))
                self.assertTrue(hasattr(host, "process_identity"))
                self.assertTrue(hasattr(host, "run_foreground"))
            finally:
                if hasattr(host, "stop"):
                    host.stop()

    def test_posix_platform_returns_a_formal_terminal_host_implementation(
        self,
    ) -> None:
        """ADR 0001 section 1 requires Supervisor to be a formal
        TerminalHost implementation, not merely a class that happens to
        have the same method names - hasattr checks alone (as in the test
        above) would pass for any unrelated object exposing five
        similarly-named methods. isinstance() only succeeds if
        TerminalHost is Supervisor's actual (possibly abstract) base."""
        from loopweave.terminal_host import TerminalHost, create_terminal_host

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with patch("loopweave.terminal_host.sys.platform", "darwin"):
                host = create_terminal_host(
                    run_id="run-1",
                    command=[sys.executable, "-c", "pass"],
                    cwd=root,
                    run_dir=root / "run",
                    socket_path=root / "control.sock",
                    control_token="secret",
                )
            try:
                self.assertIsInstance(host, TerminalHost)
            finally:
                host.stop()

    def test_win32_platform_returns_windows_backend_when_winpty_available(
        self,
    ) -> None:
        """On win32 the factory returns the Windows ConPTY host, never the
        POSIX Supervisor, when the optional ``windows`` extra is present."""
        from loopweave.terminal_host import TerminalHost, create_terminal_host

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with patch(
                "loopweave.terminal_host.sys.platform", "win32"
            ), patch(
                "loopweave.windows_terminal_host.PTY", _FakePTY
            ), patch(
                "loopweave.windows_terminal_host.Backend", _FakeBackend
            ):
                host = create_terminal_host(
                    run_id="run-1",
                    command=["cmd.exe"],
                    cwd=root,
                    run_dir=root / "run",
                    socket_path=Path(
                        r"\\.\pipe\loopweave-control-run-1-test"
                    ),
                    control_token="secret",
                )
            try:
                self.assertIsInstance(host, TerminalHost)
                self.assertEqual(
                    type(host).__name__, "WindowsConPtyHost"
                )
            finally:
                host.stop()

    def test_win32_platform_raises_actionable_capability_error_when_backend_missing(
        self,
    ) -> None:
        """Without the ``windows`` extra the factory must fail closed with an
        actionable UnsupportedPlatformError instead of a broken host."""
        from loopweave.terminal_host import (
            UnsupportedPlatformError,
            create_terminal_host,
        )

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with patch(
                "loopweave.terminal_host.sys.platform", "win32"
            ), patch("loopweave.windows_terminal_host.PTY", None):
                with self.assertRaises(UnsupportedPlatformError) as raised:
                    create_terminal_host(
                        run_id="run-1",
                        command=["cmd.exe"],
                        cwd=root,
                        run_dir=root / "run",
                        socket_path=Path(
                            r"\\.\pipe\loopweave-control-run-1-test"
                        ),
                        control_token="secret",
                    )
        message = str(raised.exception).lower()
        self.assertIn("windows", message)
        self.assertIn("extra", message)


class RunAgentUsesFactoryContractTests(unittest.TestCase):
    """ADR 0001 section 2a (closes reviewer's P1 #2, part A): the real CLI
    entry point (cli._run_agent, invoked by `loopweave run`) must call
    create_terminal_host(), not construct Supervisor directly - and an
    unsupported platform must fail before any Supervisor/pty operation,
    not merely be theoretically selectable in isolation."""

    def _run_agent_args(self, control_root: Path, workspace: Path):
        from types import SimpleNamespace

        return SimpleNamespace(
            cwd=str(control_root),
            cwd_explicit=False,
            project="stage2-contract-project",
            workspace=str(workspace),
            thread="thread-1",
            agent="generic",
            agent_args=[sys.executable, "-c", "pass"],
            mode="develop",
            reviewer="ephemeral",
        )

    def test_run_agent_calls_create_terminal_host_not_supervisor_directly(
        self,
    ) -> None:
        from types import SimpleNamespace
        from unittest.mock import MagicMock

        from loopweave.cli import _run_agent
        from loopweave.registry import Registry

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            control = root / "control"
            projects = control / "projects"
            runs = control / "runs"
            var = control / "var"
            workspace = root / "workspace"
            control.mkdir()
            workspace.mkdir()
            registry = Registry(var / "registry.sqlite")
            args = self._run_agent_args(control, workspace)

            fake_host = MagicMock()
            fake_pid = 424242
            fake_host.start.return_value = fake_pid
            fake_host.run_foreground.return_value = 0
            fake_process_start = "fake-process-start-fingerprint"

            def fake_reader(pid):
                return fake_process_start

            import loopweave.terminal_host as terminal_host_module

            with patch(
                "loopweave.cli.PROJECTS_DIR", projects
            ), patch("loopweave.cli.RUNS_DIR", runs), patch(
                "loopweave.cli.VAR_DIR", var
            ), patch(
                "loopweave.cli._registry", return_value=registry
            ), patch(
                "loopweave.cli.discover_thread",
                return_value=SimpleNamespace(
                    thread_id="thread-1",
                    cwd=str(control / "thread-metadata"),
                ),
            ), patch(
                "loopweave.cli.os.getcwd", return_value=str(control)
            ), patch(
                "loopweave.cli.create_terminal_host",
                return_value=fake_host,
            ) as factory, patch.object(
                terminal_host_module,
                "default_process_identity_reader",
                return_value=fake_reader,
            ) as identity_factory, patch(
                "loopweave.supervisor.Supervisor",
                side_effect=AssertionError(
                    "cli._run_agent must not construct Supervisor directly; "
                    "use create_terminal_host()"
                ),
            ):
                exit_code = _run_agent(args)

            self.assertEqual(exit_code, 0)
            factory.assert_called_once()
            fake_host.start.assert_called_once()
            fake_host.run_foreground.assert_called_once()
            identity_factory.assert_called()
            created_run = registry.list_runs()[0]
            self.assertEqual(created_run.agent_pid, fake_pid)
            self.assertEqual(created_run.agent_process_start, fake_process_start)

    def test_run_agent_routes_to_windows_backend_on_win32(self) -> None:
        """On win32 ``_run_agent`` must drive the Windows ConPTY backend and
        a named-pipe control endpoint, and must never touch the POSIX
        Supervisor."""
        from types import SimpleNamespace

        from loopweave.cli import _run_agent
        from loopweave.models import RunState
        from loopweave.registry import Registry
        from loopweave.windows_terminal_host import WindowsConPtyHost

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            control = root / "control"
            projects = control / "projects"
            runs = control / "runs"
            var = control / "var"
            workspace = root / "workspace"
            control.mkdir()
            workspace.mkdir()
            registry = Registry(var / "registry.sqlite")
            args = self._run_agent_args(control, workspace)

            with patch(
                "loopweave.cli.PROJECTS_DIR", projects
            ), patch("loopweave.cli.RUNS_DIR", runs), patch(
                "loopweave.cli.VAR_DIR", var
            ), patch(
                "loopweave.cli._registry", return_value=registry
            ), patch(
                "loopweave.cli.discover_thread",
                return_value=SimpleNamespace(
                    thread_id="thread-1",
                    cwd=str(control / "thread-metadata"),
                ),
            ), patch(
                "loopweave.cli.os.getcwd", return_value=str(control)
            ), patch(
                "loopweave.terminal_host.sys.platform", "win32"
            ), patch(
                "loopweave.windows_terminal_host.PTY", _FakePTY
            ), patch(
                "loopweave.windows_terminal_host.Backend", _FakeBackend
            ), patch(
                "loopweave.terminal_host.default_process_identity_reader",
                return_value=lambda pid: "fake-process-start",
            ), patch(
                "loopweave.windows_terminal_host._resolve_command",
                side_effect=lambda command: command[0],
            ), patch.object(
                WindowsConPtyHost,
                "_serve_control",
                lambda self: None,
            ), patch.object(
                WindowsConPtyHost,
                "_poke_control_server",
                lambda self: None,
            ), patch(
                "loopweave.supervisor.Supervisor",
                side_effect=AssertionError(
                    "no Supervisor/pty operation may run on win32; "
                    "the Windows backend owns the platform"
                ),
            ):
                exit_code = _run_agent(args)

            self.assertEqual(exit_code, 0)
            created_runs = registry.list_runs()
            self.assertEqual(len(created_runs), 1)
            created = created_runs[0]
            self.assertTrue(
                created.socket_path.startswith(r"\\.\pipe\loopweave-control-"),
                "run must advertise a Windows named-pipe control endpoint",
            )
            self.assertEqual(created.agent_pid, 424242)
            self.assertEqual(created.state, RunState.STOPPED)


@unittest.skipIf(sys.platform == "win32", "POSIX pty required")
class TerminalHostPosixParityContractTests(unittest.TestCase):
    """ADR 0001 section 1: the POSIX backend keeps its current PTY/socket
    behavior when reached through the new factory/base-class boundary."""

    def test_posix_backend_round_trips_input_through_managed_child(self) -> None:
        from loopweave.terminal_host import create_terminal_host

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fixture = Path(__file__).parent / "fixtures" / "echo_agent.py"
            host = create_terminal_host(
                run_id="run-1",
                command=[sys.executable, "-u", str(fixture)],
                cwd=root,
                run_dir=root / "run",
                socket_path=root / "control.sock",
                control_token="secret",
                passthrough=False,
            )
            pid = host.start()
            try:
                host.send_input("hello\n")
                output = self._wait_for(root / "run" / "terminal.txt", "text=hello")
                self.assertIn("pid={}".format(pid), output)
            finally:
                host.stop()

    @staticmethod
    def _wait_for(path: Path, text: str, timeout: float = 3.0) -> str:
        import time

        deadline = time.time() + timeout
        while time.time() < deadline:
            output = (
                path.read_text(encoding="utf-8", errors="replace")
                if path.exists()
                else ""
            )
            if text in output:
                return output
            time.sleep(0.02)
        raise AssertionError("timed out waiting for {!r}".format(text))

    def test_run_foreground_is_part_of_the_terminal_host_interface(self) -> None:
        """ADR 0001 section 1: TerminalHost must declare run_foreground(),
        the method cli._run_agent actually calls (cli.py:1059), not just
        start/send_input/resize/stop. A backend missing this method cannot
        back `loopweave run` even if it satisfies every other method."""
        from loopweave.terminal_host import TerminalHost

        self.assertTrue(hasattr(TerminalHost, "run_foreground"))

    def test_run_foreground_waits_for_child_exit_and_returns_its_code(self) -> None:
        """ADR 0001 section 1: run_foreground() blocks until the child exits
        and returns its exit code - the non-interactive branch
        (`if not sys.stdin.isatty(): return self.process.wait()`,
        supervisor.py:145-146) that this test actually exercises, since
        pytest's stdin is never a TTY. This narrowly proves exit-code
        parity, not the raw-mode/SIGWINCH/stdin-forwarding branch a real
        interactive `loopweave run` invocation takes - tests/test_supervisor.py
        does not cover that branch either (it drives input through the
        control socket instead), and building a PTY-backed harness to
        simulate a controlling terminal for THIS process is out of scope
        for a Stage 1 contract-test file whose job is gating Stage 2, not
        replacing manual/interactive verification of raw-mode behavior."""
        from loopweave.terminal_host import create_terminal_host

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fixture = Path(__file__).parent / "fixtures" / "echo_agent.py"
            host = create_terminal_host(
                run_id="run-1",
                command=[sys.executable, "-u", str(fixture)],
                cwd=root,
                run_dir=root / "run",
                socket_path=root / "control.sock",
                control_token="secret",
                passthrough=False,
            )
            try:
                host.start()
                self._wait_for(root / "run" / "terminal.txt", "READY pid=")
                host.send_input("EXIT\n")
                exit_code = host.run_foreground()
            finally:
                host.stop()

            self.assertEqual(exit_code, 0)
            output = (root / "run" / "terminal.txt").read_text(encoding="utf-8")
            self.assertIn("text=EXIT", output)

    def test_public_resize_delegates_to_the_existing_posix_resize_behavior(
        self,
    ) -> None:
        """ADR 0001 section 1: the new public resize(rows, cols) method
        must delegate to the same PTY resize behavior _resize_child_pty
        already implements (tests/test_supervisor.py already covers
        _resize_child_pty directly) - proven here by an observable side
        effect (a terminal_resized event recorded in
        terminal-events.jsonl with the given rows/columns), not merely
        that calling resize() raises no exception. A stub resize() that
        does nothing would fail this test."""
        from loopweave.terminal_host import create_terminal_host

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fixture = Path(__file__).parent / "fixtures" / "echo_agent.py"
            host = create_terminal_host(
                run_id="run-1",
                command=[sys.executable, "-u", str(fixture)],
                cwd=root,
                run_dir=root / "run",
                socket_path=root / "control.sock",
                control_token="secret",
                passthrough=False,
            )
            try:
                host.start()
                self._wait_for(root / "run" / "terminal.txt", "READY pid=")
                host.resize(33, 101)
            finally:
                host.stop()

            diagnostics_path = root / "run" / "terminal-events.jsonl"
            events = [
                json.loads(line)
                for line in diagnostics_path.read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
            resize_events = [
                event for event in events if event.get("event") == "terminal_resized"
            ]
            self.assertTrue(
                resize_events, "resize(33, 101) must record a terminal_resized event"
            )
            self.assertEqual(resize_events[-1]["rows"], 33)
            self.assertEqual(resize_events[-1]["columns"], 101)

    def test_process_identity_matches_the_live_started_process(self) -> None:
        """ADR 0001 section 1: process_identity() must return the (pid,
        start_fingerprint) of the actual process start() spawned, not a
        placeholder - proven by cross-checking against
        supervisor.process_start_time(pid), the same fingerprint function
        registry.process_identity_matches already relies on for identity
        checks elsewhere in this codebase. A stub returning a fixed or
        empty value would fail this test."""
        from loopweave.supervisor import process_start_time
        from loopweave.terminal_host import create_terminal_host

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fixture = Path(__file__).parent / "fixtures" / "echo_agent.py"
            host = create_terminal_host(
                run_id="run-1",
                command=[sys.executable, "-u", str(fixture)],
                cwd=root,
                run_dir=root / "run",
                socket_path=root / "control.sock",
                control_token="secret",
                passthrough=False,
            )
            try:
                pid = host.start()
                self._wait_for(root / "run" / "terminal.txt", "READY pid=")
                identity_pid, identity_start = host.process_identity()
                expected_start = process_start_time(pid)
            finally:
                host.stop()

            self.assertEqual(identity_pid, pid)
            self.assertEqual(identity_start, expected_start)


@unittest.skipIf(sys.platform == "win32", "POSIX pty required")
class SubmissionServiceContractTests(unittest.TestCase):
    """ADR 0001 sections 3-4: a structured submission path usable from
    inside a managed session, reusing the existing state/review machinery
    (not a parallel loop) and identified by a bounded run id."""

    def _make_run(self, root: Path, *, reviewer_backend=None):
        from loopweave.models import ReviewBackend, RunRecord, RunState
        from loopweave.registry import Registry
        from loopweave.supervisor import Supervisor, process_start_time

        run_dir = root / "run-1"
        socket_path = root / "control.sock"
        fixture = Path(__file__).parent / "fixtures" / "echo_agent.py"
        supervisor = Supervisor(
            run_id="run-1",
            command=[sys.executable, "-u", str(fixture)],
            cwd=root,
            run_dir=run_dir,
            socket_path=socket_path,
            control_token="secret",
            passthrough=False,
        )
        pid = supervisor.start()
        registry = Registry(root / "registry.sqlite")
        registry.create_run(
            RunRecord(
                run_id="run-1",
                codex_thread_id="thread-1",
                cwd=str(root),
                thread_cwd=str(root),
                workspace_root=str(root),
                tty="/dev/test",
                agent="generic",
                agent_pid=pid,
                agent_process_start=process_start_time(pid),
                control_token="secret",
                state=RunState.RUNNING,
                socket_path=str(socket_path),
                run_dir=str(run_dir),
                reviewer_backend=reviewer_backend or ReviewBackend.EPHEMERAL,
            )
        )
        return registry, supervisor, run_dir

    def test_submit_stage_writes_review_request_and_dispatches(self) -> None:
        from loopweave.models import RunState
        from loopweave.submission import submit_stage

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            registry, supervisor, run_dir = self._make_run(root)
            dispatched = []
            try:
                with patch(
                    "loopweave.submission._registry", return_value=registry
                ), patch(
                    "loopweave.submission._dispatch",
                    side_effect=lambda run: dispatched.append(run.run_id),
                ):
                    submit_stage(
                        "run-1",
                        "Implemented the fixture worker.",
                        evidence={
                            "files_changed": ["app.py"],
                            "commands_run": ["pytest -q"],
                        },
                    )
            finally:
                supervisor.stop()

            self.assertEqual(dispatched, ["run-1"])
            request = json.loads(
                (run_dir / "review-request.json").read_text(encoding="utf-8")
            )
            self.assertEqual(request["completion_scope"], "stage")
            self.assertEqual(request["files_changed"], ["app.py"])
            self.assertEqual(request["commands_run"], ["pytest -q"])
            self.assertEqual(
                registry.get_run("run-1").state, RunState.READY_FOR_REVIEW
            )

    def test_submit_final_marks_completion_scope_final(self) -> None:
        from loopweave.submission import submit_final

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            registry, supervisor, run_dir = self._make_run(root)
            try:
                with patch(
                    "loopweave.submission._registry", return_value=registry
                ), patch("loopweave.submission._dispatch"):
                    submit_final(
                        "run-1",
                        "The full task is complete.",
                        evidence={"files_changed": [], "commands_run": []},
                    )
            finally:
                supervisor.stop()

            request = json.loads(
                (run_dir / "review-request.json").read_text(encoding="utf-8")
            )
            self.assertEqual(request["completion_scope"], "final")

    def test_submit_needs_human_transitions_run_without_dispatch(self) -> None:
        from loopweave.models import RunState
        from loopweave.submission import submit_needs_human

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            registry, supervisor, _run_dir = self._make_run(root)
            try:
                with patch(
                    "loopweave.submission._registry", return_value=registry
                ), patch(
                    "loopweave.submission._dispatch",
                    side_effect=AssertionError(
                        "needs_human must not dispatch a review"
                    ),
                ):
                    submit_needs_human(
                        "run-1", "Please choose the deployment target."
                    )
            finally:
                supervisor.stop()

            self.assertEqual(
                registry.get_run("run-1").state, RunState.NEEDS_HUMAN
            )

    def test_submit_stage_rejects_stale_run_identity(self) -> None:
        """Patches terminal_host.default_process_identity_reader, not a
        supervisor.process_start_time import inside submission.py -
        submission.py's stale-identity check must go through the same
        platform-neutral provider every other liveness check uses (ADR
        section 2a), not its own direct POSIX import."""
        import loopweave.terminal_host as terminal_host_module
        from loopweave.submission import SubmissionError, submit_stage

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            registry, supervisor, _run_dir = self._make_run(root)
            try:
                with patch(
                    "loopweave.submission._registry", return_value=registry
                ), patch.object(
                    terminal_host_module,
                    "default_process_identity_reader",
                    return_value=lambda pid: "a-different-start-time",
                ):
                    with self.assertRaises(SubmissionError):
                        submit_stage(
                            "run-1",
                            "Stale submission.",
                            evidence={"files_changed": [], "commands_run": []},
                        )
            finally:
                supervisor.stop()

    def test_submit_stage_rejects_missing_run_id(self) -> None:
        from loopweave.submission import SubmissionError, submit_stage

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            registry, supervisor, _run_dir = self._make_run(root)
            try:
                with patch(
                    "loopweave.submission._registry", return_value=registry
                ):
                    with self.assertRaises(SubmissionError):
                        submit_stage(
                            "run-does-not-exist",
                            "Orphan submission.",
                            evidence={"files_changed": [], "commands_run": []},
                        )
            finally:
                supervisor.stop()

    def test_submit_stage_queues_visible_card_for_visible_thread_runs(self) -> None:
        from loopweave.models import ReviewBackend, RunState
        from loopweave.submission import submit_stage

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            registry, supervisor, run_dir = self._make_run(
                root, reviewer_backend=ReviewBackend.VISIBLE_THREAD
            )
            (run_dir / "assigned-task-latest.md").write_text(
                "# Task\n", encoding="utf-8"
            )
            # hook_entry._visible_plan_path falls back to run_dir/"run.json"
            # when run.project_root is None (as it is for this fixture's
            # RunRecord) - NOT root/"run.json". Writing to the wrong path
            # made this fixture pass only because Stage 1 has no real
            # submission.py to call _visible_plan_path/create_review_card
            # and hit validate_review_card's plan_path existence check.
            (run_dir / "run.json").write_text(
                '{"schema_version": 1}\n', encoding="utf-8"
            )
            try:
                with patch(
                    "loopweave.submission._registry", return_value=registry
                ), patch(
                    "loopweave.submission._dispatch",
                    side_effect=AssertionError(
                        "visible-thread runs must not use ephemeral dispatch"
                    ),
                ):
                    submit_stage(
                        "run-1",
                        "Implemented the fixture worker.",
                        evidence={"files_changed": [], "commands_run": []},
                    )
            finally:
                supervisor.stop()

            self.assertTrue((run_dir / "review-inbox" / "pending").exists())
            self.assertEqual(
                registry.get_run("run-1").state, RunState.READY_FOR_REVIEW
            )

    def test_submit_stage_rejects_overwriting_a_queued_visible_review(self) -> None:
        """reviewer Stage 2 re-review P1: a run already in READY_FOR_REVIEW has a
        pending review card queued for human review. A second stage submission
        must NOT overwrite it (generate a new card, clobber review-inbox/pending,
        or change scope). Before this fix the second submit_stage produced a
        fresh card and overwrote pending. Proven by asserting SubmissionError
        AND that the pending review_id is byte-identical after the rejected
        second submission."""
        from loopweave.models import ReviewBackend, RunState
        from loopweave.submission import SubmissionError, submit_stage

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            registry, supervisor, run_dir = self._make_run(
                root, reviewer_backend=ReviewBackend.VISIBLE_THREAD
            )
            (run_dir / "assigned-task-latest.md").write_text(
                "# Task\n", encoding="utf-8"
            )
            (run_dir / "run.json").write_text(
                '{"schema_version": 1}\n', encoding="utf-8"
            )
            try:
                with patch(
                    "loopweave.submission._registry", return_value=registry
                ):
                    submit_stage(
                        "run-1",
                        "First stage complete.",
                        evidence={"files_changed": [], "commands_run": []},
                    )
                    pending_path = run_dir / "review-inbox" / "pending"
                    first_review_id = pending_path.read_text(
                        encoding="utf-8"
                    ).strip()
                    self.assertTrue(first_review_id)
                    with self.assertRaises(SubmissionError):
                        submit_stage(
                            "run-1",
                            "Second stage clobber attempt.",
                            evidence={"files_changed": [], "commands_run": []},
                        )
            finally:
                supervisor.stop()

            self.assertEqual(
                registry.get_run("run-1").state, RunState.READY_FOR_REVIEW
            )
            self.assertEqual(
                (run_dir / "review-inbox" / "pending")
                .read_text(encoding="utf-8")
                .strip(),
                first_review_id,
                "pending review card must not be overwritten by a second "
                "submission",
            )

    def test_submit_needs_human_cannot_bypass_a_queued_visible_review(self) -> None:
        """reviewer Stage 2 re-review P1: submit_needs_human must not bypass a
        queued review by forcing the run to NEEDS_HUMAN while a pending card
        still exists. Before this fix needs_human had no state guard and turned
        a READY_FOR_REVIEW run into NEEDS_HUMAN without clearing pending."""
        from loopweave.models import ReviewBackend, RunState
        from loopweave.submission import (
            SubmissionError,
            submit_needs_human,
            submit_stage,
        )

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            registry, supervisor, run_dir = self._make_run(
                root, reviewer_backend=ReviewBackend.VISIBLE_THREAD
            )
            (run_dir / "assigned-task-latest.md").write_text(
                "# Task\n", encoding="utf-8"
            )
            (run_dir / "run.json").write_text(
                '{"schema_version": 1}\n', encoding="utf-8"
            )
            try:
                with patch(
                    "loopweave.submission._registry", return_value=registry
                ):
                    submit_stage(
                        "run-1",
                        "Stage complete.",
                        evidence={"files_changed": [], "commands_run": []},
                    )
                    with self.assertRaises(SubmissionError):
                        submit_needs_human("run-1", "Please decide.")
            finally:
                supervisor.stop()

            self.assertEqual(
                registry.get_run("run-1").state, RunState.READY_FOR_REVIEW
            )
            self.assertTrue(
                (run_dir / "review-inbox" / "pending").exists(),
                "pending review card must survive the rejected needs-human",
            )

    def test_failed_ephemeral_dispatch_rolls_back_and_redispatches_same_request(
        self,
    ) -> None:
        """reviewer Stage 2 re-review P1: the honest failed-dispatch recovery path.
        A dispatch that aborts before advancing state rolls the run back to its
        assignable entry state, and the retry REDISPATCHES THE EXISTING request
        - its identity and payload are NOT rewritten by the second submission
        (idempotent redispatch), proven by byte-comparing review-request.json
        before and after."""
        from loopweave.submission import submit_stage

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            registry, supervisor, run_dir = self._make_run(root)
            entry_state = registry.get_run("run-1").state
            dispatched = []
            try:
                with patch(
                    "loopweave.submission._registry", return_value=registry
                ):
                    with self.assertRaises(RuntimeError):
                        submit_stage(
                            "run-1",
                            "First attempt.",
                            evidence={
                                "files_changed": ["app.py"],
                                "commands_run": [],
                            },
                            _dispatch_fn=lambda run: (_ for _ in ()).throw(
                                RuntimeError("dispatch failed")
                            ),
                        )
                    self.assertEqual(
                        registry.get_run("run-1").state,
                        entry_state,
                        "failed dispatch must roll back to the entry state",
                    )
                    request_path = run_dir / "review-request.json"
                    first_payload = request_path.read_bytes()
                    submit_stage(
                        "run-1",
                        "Retry with different summary.",
                        evidence={
                            "files_changed": ["other.py"],
                            "commands_run": [],
                        },
                        _dispatch_fn=lambda run: dispatched.append(run.run_id),
                    )
                    self.assertEqual(
                        request_path.read_bytes(),
                        first_payload,
                        "retry must redispatch the existing request, not "
                        "overwrite its identity/payload",
                    )
            finally:
                supervisor.stop()

            self.assertEqual(dispatched, ["run-1"])

    def test_failed_dispatch_preserves_reviewing_state(self) -> None:
        """reviewer Stage 2 re-review P1: if dispatch advanced into REVIEWING
        before failing, the run must STAY in REVIEWING - the rollback must not
        rewind an in-flight review back to RUNNING."""
        from loopweave.models import RunState
        from loopweave.submission import submit_stage

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            registry, supervisor, run_dir = self._make_run(root)

            def advance_to_reviewing_then_fail(run):
                registry.force_state(run.run_id, RunState.REVIEWING)
                raise RuntimeError("codex dispatch failed mid-review")

            try:
                with patch(
                    "loopweave.submission._registry", return_value=registry
                ):
                    with self.assertRaises(RuntimeError):
                        submit_stage(
                            "run-1",
                            "Stage.",
                            evidence={"files_changed": ["app.py"], "commands_run": []},
                            _dispatch_fn=advance_to_reviewing_then_fail,
                        )
            finally:
                supervisor.stop()

            self.assertEqual(
                registry.get_run("run-1").state,
                RunState.REVIEWING,
                "downstream REVIEWING state must not be rolled back",
            )

    def test_failed_dispatch_preserves_review_ready_state(self) -> None:
        """reviewer Stage 2 re-review P1: a dispatch that reached REVIEW_READY
        before failing must preserve it, not rewind to the entry state."""
        from loopweave.models import RunState
        from loopweave.submission import submit_stage

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            registry, supervisor, run_dir = self._make_run(root)

            def advance_to_review_ready_then_fail(run):
                registry.force_state(run.run_id, RunState.REVIEW_READY)
                raise RuntimeError("delivery failed after review ready")

            try:
                with patch(
                    "loopweave.submission._registry", return_value=registry
                ):
                    with self.assertRaises(RuntimeError):
                        submit_stage(
                            "run-1",
                            "Stage.",
                            evidence={"files_changed": ["app.py"], "commands_run": []},
                            _dispatch_fn=advance_to_review_ready_then_fail,
                        )
            finally:
                supervisor.stop()

            self.assertEqual(
                registry.get_run("run-1").state,
                RunState.REVIEW_READY,
                "downstream REVIEW_READY state must not be rolled back",
            )

    def test_failed_dispatch_preserves_orphaned_terminal_state(self) -> None:
        """reviewer Stage 2 re-review P1: a dispatch whose delivery path marked the
        worker ORPHANED (dead/stale worker, fail-closed) must NOT be resurrected
        back to RUNNING by the rollback. Orphaned is an authoritative terminal
        state."""
        from loopweave.models import RunState
        from loopweave.submission import submit_stage

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            registry, supervisor, run_dir = self._make_run(root)

            def orphan_then_fail(run):
                registry.force_state(run.run_id, RunState.ORPHANED)
                raise RuntimeError("worker died during delivery")

            try:
                with patch(
                    "loopweave.submission._registry", return_value=registry
                ):
                    with self.assertRaises(RuntimeError):
                        submit_stage(
                            "run-1",
                            "Stage.",
                            evidence={"files_changed": ["app.py"], "commands_run": []},
                            _dispatch_fn=orphan_then_fail,
                        )
            finally:
                supervisor.stop()

            self.assertEqual(
                registry.get_run("run-1").state,
                RunState.ORPHANED,
                "ORPHANED terminal state must not be resurrected by rollback",
            )

    def test_submit_stage_rejects_approved_terminal_run(self) -> None:
        """ADR product invariant 7 / reviewer Stage 2 re-review P1 #1: a terminal
        run (APPROVED) must NOT be resurrected by a stray stage submission.
        Before this fix submit_stage force_state'd APPROVED->READY_FOR_REVIEW
        and dispatched. Proven by asserting SubmissionError is raised AND the
        run stays APPROVED (no resurrection, no dispatch side effect)."""
        from loopweave.models import RunState
        from loopweave.submission import SubmissionError, submit_stage

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            registry, supervisor, _run_dir = self._make_run(root)
            registry.force_state("run-1", RunState.APPROVED)
            dispatched = []
            try:
                with patch(
                    "loopweave.submission._registry", return_value=registry
                ), patch(
                    "loopweave.submission._dispatch",
                    side_effect=lambda run: dispatched.append(run.run_id),
                ):
                    with self.assertRaises(SubmissionError):
                        submit_stage(
                            "run-1",
                            "Stray late submission.",
                            evidence={"files_changed": [], "commands_run": []},
                        )
            finally:
                supervisor.stop()

            self.assertEqual(dispatched, [])
            self.assertEqual(
                registry.get_run("run-1").state, RunState.APPROVED
            )

    def test_submit_stage_rejects_stopped_terminal_run(self) -> None:
        """Same resurrection guard for STOPPED - the second terminal state reviewer
        probed directly."""
        from loopweave.models import RunState
        from loopweave.submission import SubmissionError, submit_stage

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            registry, supervisor, _run_dir = self._make_run(root)
            registry.force_state("run-1", RunState.STOPPED)
            dispatched = []
            try:
                with patch(
                    "loopweave.submission._registry", return_value=registry
                ), patch(
                    "loopweave.submission._dispatch",
                    side_effect=lambda run: dispatched.append(run.run_id),
                ):
                    with self.assertRaises(SubmissionError):
                        submit_stage(
                            "run-1",
                            "Stray late submission.",
                            evidence={"files_changed": [], "commands_run": []},
                        )
            finally:
                supervisor.stop()

            self.assertEqual(dispatched, [])
            self.assertEqual(
                registry.get_run("run-1").state, RunState.STOPPED
            )

    def test_submit_needs_human_rejects_approved_terminal_run(self) -> None:
        """needs_human must share the same terminal-state guard (reviewer P1 #1):
        before this fix it had no state check at all and could force a STOPPED
        or APPROVED run back to NEEDS_HUMAN. Proven on APPROVED."""
        from loopweave.models import RunState
        from loopweave.submission import (
            SubmissionError,
            submit_needs_human,
        )

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            registry, supervisor, _run_dir = self._make_run(root)
            registry.force_state("run-1", RunState.APPROVED)
            try:
                with patch(
                    "loopweave.submission._registry", return_value=registry
                ):
                    with self.assertRaises(SubmissionError):
                        submit_needs_human("run-1", "Please decide.")
            finally:
                supervisor.stop()

            self.assertEqual(
                registry.get_run("run-1").state, RunState.APPROVED
            )

    def test_submit_needs_human_rejects_stale_run_identity(self) -> None:
        """needs_human must also perform the live process-identity check (reviewer
        P1 #1), not only the state check - a dead worker cannot request human
        intervention through the protocol. Before this fix needs_human skipped
        identity validation entirely."""
        import loopweave.terminal_host as terminal_host_module
        from loopweave.submission import (
            SubmissionError,
            submit_needs_human,
        )

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            registry, supervisor, _run_dir = self._make_run(root)
            try:
                with patch(
                    "loopweave.submission._registry", return_value=registry
                ), patch.object(
                    terminal_host_module,
                    "default_process_identity_reader",
                    return_value=lambda pid: "a-different-start-time",
                ):
                    with self.assertRaises(SubmissionError):
                        submit_needs_human("run-1", "Please decide.")
            finally:
                supervisor.stop()


class SubmissionBoundsValidatorContractTests(unittest.TestCase):
    """ADR 0001 section 3a (closes reviewer's Stage 1 re-review P1 finding:
    isolate the bounds validator so a boundary-acceptance test cannot pass
    because of a wrongly-swallowed exception, and distinguish UTF-8 byte
    counts from Python character counts). These tests call
    validate_bounded_text()/validate_evidence() directly - the standalone
    validator functions ADR section 3a specifies exactly so tests can
    assert the validator's own behavior without also depending on run/
    registry lookup succeeding or failing for an unrelated reason. Every
    boundary-acceptance assertion here asserts "no exception was raised"
    by calling the validator with no try/except at all (a wrong rejection
    fails the test with its own traceback), never a bare
    "except Exception: pass" that could hide a wrongly-raised exception."""

    def test_bounded_text_under_limit_is_accepted(self) -> None:
        from loopweave.submission import MAX_SUMMARY_BYTES, validate_bounded_text

        validate_bounded_text("x" * (MAX_SUMMARY_BYTES - 1), MAX_SUMMARY_BYTES, "summary")

    def test_bounded_text_at_exact_byte_boundary_is_accepted(self) -> None:
        from loopweave.submission import MAX_SUMMARY_BYTES, validate_bounded_text

        validate_bounded_text("x" * MAX_SUMMARY_BYTES, MAX_SUMMARY_BYTES, "summary")

    def test_bounded_text_one_byte_over_limit_is_rejected(self) -> None:
        from loopweave.submission import MAX_SUMMARY_BYTES, SubmissionError, validate_bounded_text

        with self.assertRaises(SubmissionError):
            validate_bounded_text(
                "x" * (MAX_SUMMARY_BYTES + 1), MAX_SUMMARY_BYTES, "summary"
            )

    def test_bounded_text_counts_utf8_bytes_not_characters(self) -> None:
        """A multibyte string whose CHARACTER count is under the limit but
        whose UTF-8 BYTE count is over it must be rejected - an
        implementation that counts len(text) instead of
        len(text.encode('utf-8')) would wrongly accept this input, since
        every prior boundary test in this file used ASCII, where the two
        counts are always equal."""
        from loopweave.submission import MAX_SUMMARY_BYTES, SubmissionError, validate_bounded_text

        multibyte_char = "中"  # 1 code point, 3 UTF-8 bytes
        char_count = (MAX_SUMMARY_BYTES // 3) + 1
        text = multibyte_char * char_count
        self.assertLess(len(text), MAX_SUMMARY_BYTES)
        self.assertGreater(len(text.encode("utf-8")), MAX_SUMMARY_BYTES)

        with self.assertRaises(SubmissionError):
            validate_bounded_text(text, MAX_SUMMARY_BYTES, "summary")

    def test_bounded_text_error_names_the_field(self) -> None:
        from loopweave.submission import MAX_SUMMARY_BYTES, SubmissionError, validate_bounded_text

        with self.assertRaises(SubmissionError) as raised:
            validate_bounded_text(
                "x" * (MAX_SUMMARY_BYTES + 1),
                MAX_SUMMARY_BYTES,
                "distinctive_field_name",
            )
        self.assertIn("distinctive_field_name", str(raised.exception))

    def test_too_many_evidence_items_in_one_field_is_rejected(self) -> None:
        from loopweave.submission import EvidenceTooLarge, validate_evidence

        too_many = ["file-{}.py".format(i) for i in range(201)]

        with self.assertRaises(EvidenceTooLarge):
            validate_evidence({"files_changed": too_many, "commands_run": []})

    def test_evidence_item_exceeding_per_item_byte_limit_is_rejected(self) -> None:
        from loopweave.submission import (
            EvidenceTooLarge,
            MAX_EVIDENCE_ITEM_BYTES,
            validate_evidence,
        )

        oversized_item = "x" * (MAX_EVIDENCE_ITEM_BYTES + 1)

        with self.assertRaises(EvidenceTooLarge):
            validate_evidence(
                {"files_changed": [oversized_item], "commands_run": []}
            )

    def test_evidence_item_byte_limit_counts_utf8_bytes_not_characters(
        self,
    ) -> None:
        """A single evidence item whose character count is under
        MAX_EVIDENCE_ITEM_BYTES but whose UTF-8 byte count is over it must
        be rejected - the same chars-vs-bytes distinction as the summary
        test above, applied to one evidence list item."""
        from loopweave.submission import (
            EvidenceTooLarge,
            MAX_EVIDENCE_ITEM_BYTES,
            validate_evidence,
        )

        multibyte_char = "\U0001f600"  # 1 code point, 4 UTF-8 bytes
        char_count = (MAX_EVIDENCE_ITEM_BYTES // 4) + 1
        item = multibyte_char * char_count
        self.assertLess(len(item), MAX_EVIDENCE_ITEM_BYTES)
        self.assertGreater(len(item.encode("utf-8")), MAX_EVIDENCE_ITEM_BYTES)

        with self.assertRaises(EvidenceTooLarge):
            validate_evidence({"files_changed": [item], "commands_run": []})

    def test_evidence_total_size_over_budget_is_rejected_even_under_per_item_cap(
        self,
    ) -> None:
        """Many items, each individually under MAX_EVIDENCE_ITEM_BYTES and
        under MAX_EVIDENCE_ITEMS_PER_FIELD, must still be rejected once the
        whole evidence object's canonical JSON encoding exceeds
        MAX_EVIDENCE_TOTAL_BYTES - proving the total cap is enforced
        independently of the per-item and per-field caps, using a total
        that is over budget without either individual cap being violated."""
        from loopweave.submission import (
            EvidenceTooLarge,
            MAX_EVIDENCE_ITEM_BYTES,
            MAX_EVIDENCE_ITEMS_PER_FIELD,
            MAX_EVIDENCE_TOTAL_BYTES,
            validate_evidence,
        )

        item = "x" * (MAX_EVIDENCE_ITEM_BYTES // 2)
        item_count = (MAX_EVIDENCE_TOTAL_BYTES // len(item.encode("utf-8"))) + 1
        self.assertLessEqual(
            item_count,
            MAX_EVIDENCE_ITEMS_PER_FIELD,
            "this test must stay under the per-field item-count cap so "
            "only the total-size cap is exercised",
        )
        items = [item] * item_count
        total_item_bytes = len(item.encode("utf-8")) * len(items)
        self.assertGreater(total_item_bytes, MAX_EVIDENCE_TOTAL_BYTES)

        with self.assertRaises(EvidenceTooLarge):
            validate_evidence({"files_changed": items, "commands_run": []})

    def test_malformed_evidence_non_string_item_is_rejected(self) -> None:
        from loopweave.submission import SubmissionError, validate_evidence

        with self.assertRaises(SubmissionError):
            validate_evidence({"files_changed": [123], "commands_run": []})

    def test_malformed_evidence_unknown_key_is_rejected_not_dropped(self) -> None:
        from loopweave.submission import SubmissionError, validate_evidence

        with self.assertRaises(SubmissionError):
            validate_evidence(
                {
                    "files_changed": [],
                    "commands_run": [],
                    "totally_unrecognized_field": ["should not be silently dropped"],
                }
            )

    def test_evidence_item_count_at_exact_per_field_boundary_is_accepted(
        self,
    ) -> None:
        """Exactly MAX_EVIDENCE_ITEMS_PER_FIELD small items must not be
        rejected for item count - proves the per-field cap's edge is
        inclusive, independent of the per-item and total-size caps (each
        item here is tiny, so neither of those caps is anywhere near its
        own boundary). No try/except: a wrong rejection fails this test
        with its own traceback rather than being silently absorbed."""
        from loopweave.submission import MAX_EVIDENCE_ITEMS_PER_FIELD, validate_evidence

        small_item = "ok.py"
        items = [small_item] * MAX_EVIDENCE_ITEMS_PER_FIELD

        validate_evidence({"files_changed": items, "commands_run": []})

    def test_single_item_at_exact_per_item_byte_boundary_is_accepted(
        self,
    ) -> None:
        """A single item of exactly MAX_EVIDENCE_ITEM_BYTES (ASCII, so
        character count equals byte count here) must not be rejected for
        item size - proves the per-item cap's edge is inclusive,
        independent of the per-field and total-size caps."""
        from loopweave.submission import MAX_EVIDENCE_ITEM_BYTES, validate_evidence

        item_at_limit = "x" * MAX_EVIDENCE_ITEM_BYTES

        validate_evidence({"files_changed": [item_at_limit], "commands_run": []})

    def test_evidence_at_exact_total_byte_boundary_is_accepted(self) -> None:
        """An evidence object whose canonical JSON encoding
        (ensure_ascii=False, sort_keys=True, separators=(",", ":")) is
        exactly MAX_EVIDENCE_TOTAL_BYTES must not be rejected for total
        size, built from enough items that no single item approaches
        MAX_EVIDENCE_ITEM_BYTES and the item count stays far below
        MAX_EVIDENCE_ITEMS_PER_FIELD - proves the total-size cap's edge is
        inclusive, independent of the other two caps."""
        from loopweave.submission import (
            MAX_EVIDENCE_ITEM_BYTES,
            MAX_EVIDENCE_ITEMS_PER_FIELD,
            MAX_EVIDENCE_TOTAL_BYTES,
            validate_evidence,
        )

        item_count = 17
        self.assertLess(item_count, MAX_EVIDENCE_ITEMS_PER_FIELD)
        base_size = len(
            json.dumps(
                {"files_changed": ["x"] * item_count, "commands_run": []},
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        )
        remaining = MAX_EVIDENCE_TOTAL_BYTES - base_size
        extra_per_item, leftover = divmod(remaining, item_count)
        lengths = [1 + extra_per_item] * item_count
        lengths[-1] += leftover
        self.assertLess(max(lengths), MAX_EVIDENCE_ITEM_BYTES)
        evidence = {
            "files_changed": ["x" * length for length in lengths],
            "commands_run": [],
        }
        encoded_size = len(
            json.dumps(
                evidence,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        )
        self.assertEqual(encoded_size, MAX_EVIDENCE_TOTAL_BYTES)

        validate_evidence(evidence)


class SubmissionServiceBoundsIntegrationContractTests(unittest.TestCase):
    """ADR 0001 section 3a: submit_stage/submit_final/submit_needs_human
    must actually call the bounds validator - proven separately from
    SubmissionBoundsValidatorContractTests, which proves the validator
    itself is correct but never proves the public entry points invoke it.

    Every test patches submission._registry with a Mock and asserts
    registry.get_run was never called, alongside the expected exception -
    NOT merely that some SubmissionError was raised. A run_id that "does
    not need to exist" is not, by itself, a valid test design: if a real
    submit_stage looked up the run and a missing run also raises plain
    SubmissionError (a completely reasonable design - see
    SubmissionServiceContractTests.test_submit_stage_rejects_missing_run_id
    in this same file), then an implementation that skips text/evidence
    validation entirely and falls straight through to run lookup would
    raise the exact exception type these tests assert, for the wrong
    reason, and pass. assert_not_called() on registry.get_run closes that
    gap directly, independent of whatever exception type run-lookup
    failure happens to raise."""

    @staticmethod
    def _registry_mock():
        """A registry whose get_run() raises on any call - simulating a
        real "run not found" registry lookup, not merely a lenient Mock
        that would silently return a truthy stand-in object. This is what
        actually reproduces reviewer's exact failure mode: an implementation
        that skips validation and reaches a real run-not-found error,
        which is itself plausibly a SubmissionError."""
        from unittest.mock import Mock

        from loopweave.registry import RunNotFound

        registry = Mock()
        registry.get_run.side_effect = RunNotFound("run-does-not-need-to-exist")
        return registry

    def test_submit_stage_rejects_oversized_summary(self) -> None:
        from loopweave.submission import MAX_SUMMARY_BYTES, SubmissionError, submit_stage

        registry = self._registry_mock()
        with patch("loopweave.submission._registry", return_value=registry):
            with self.assertRaises(SubmissionError):
                submit_stage(
                    "run-does-not-need-to-exist",
                    "x" * (MAX_SUMMARY_BYTES + 1),
                    evidence={"files_changed": [], "commands_run": []},
                )
        registry.get_run.assert_not_called()

    def test_submit_final_rejects_oversized_summary(self) -> None:
        """submit_final gets its own oversized-summary test: proving the
        bound on submit_stage does not prove submit_final enforces the
        same rule, since Stage 2 could implement one without the other."""
        from loopweave.submission import MAX_SUMMARY_BYTES, SubmissionError, submit_final

        registry = self._registry_mock()
        with patch("loopweave.submission._registry", return_value=registry):
            with self.assertRaises(SubmissionError):
                submit_final(
                    "run-does-not-need-to-exist",
                    "x" * (MAX_SUMMARY_BYTES + 1),
                    evidence={"files_changed": [], "commands_run": []},
                )
        registry.get_run.assert_not_called()

    def test_submit_needs_human_rejects_oversized_message(self) -> None:
        from loopweave.submission import MAX_MESSAGE_BYTES, SubmissionError, submit_needs_human

        registry = self._registry_mock()
        with patch("loopweave.submission._registry", return_value=registry):
            with self.assertRaises(SubmissionError):
                submit_needs_human(
                    "run-does-not-need-to-exist", "x" * (MAX_MESSAGE_BYTES + 1)
                )
        registry.get_run.assert_not_called()

    def test_submit_stage_rejects_oversized_evidence_item(self) -> None:
        from loopweave.submission import (
            EvidenceTooLarge,
            MAX_EVIDENCE_ITEM_BYTES,
            submit_stage,
        )

        registry = self._registry_mock()
        with patch("loopweave.submission._registry", return_value=registry):
            with self.assertRaises(EvidenceTooLarge):
                submit_stage(
                    "run-does-not-need-to-exist",
                    "Implemented the feature.",
                    evidence={
                        "files_changed": ["x" * (MAX_EVIDENCE_ITEM_BYTES + 1)],
                        "commands_run": [],
                    },
                )
        registry.get_run.assert_not_called()

    def test_submit_stage_rejects_multibyte_summary_over_byte_limit(self) -> None:
        """The isolated validator tests in
        SubmissionBoundsValidatorContractTests prove
        validate_bounded_text() counts UTF-8 bytes, not characters - but
        submit_stage could still independently call len(text) instead of
        len(text.encode('utf-8')) internally and pass every ASCII test in
        this class, since ASCII bytes and characters are numerically
        equal. This test uses a multibyte value (character count under
        the limit, UTF-8 byte count over it) through the PUBLIC function
        itself, not the validator, to close that gap."""
        from loopweave.submission import MAX_SUMMARY_BYTES, SubmissionError, submit_stage

        multibyte_char = "中"  # 1 code point, 3 UTF-8 bytes
        char_count = (MAX_SUMMARY_BYTES // 3) + 1
        summary = multibyte_char * char_count
        self.assertLess(len(summary), MAX_SUMMARY_BYTES)
        self.assertGreater(len(summary.encode("utf-8")), MAX_SUMMARY_BYTES)

        registry = self._registry_mock()
        with patch("loopweave.submission._registry", return_value=registry):
            with self.assertRaises(SubmissionError):
                submit_stage(
                    "run-does-not-need-to-exist",
                    summary,
                    evidence={"files_changed": [], "commands_run": []},
                )
        registry.get_run.assert_not_called()

    def test_submit_final_rejects_multibyte_summary_over_byte_limit(self) -> None:
        """submit_final's own multibyte test - proving submit_stage counts
        UTF-8 bytes does not prove submit_final does, since Stage 2 could
        implement the two functions' validation independently."""
        from loopweave.submission import MAX_SUMMARY_BYTES, SubmissionError, submit_final

        multibyte_char = "中"
        char_count = (MAX_SUMMARY_BYTES // 3) + 1
        summary = multibyte_char * char_count
        self.assertLess(len(summary), MAX_SUMMARY_BYTES)
        self.assertGreater(len(summary.encode("utf-8")), MAX_SUMMARY_BYTES)

        registry = self._registry_mock()
        with patch("loopweave.submission._registry", return_value=registry):
            with self.assertRaises(SubmissionError):
                submit_final(
                    "run-does-not-need-to-exist",
                    summary,
                    evidence={"files_changed": [], "commands_run": []},
                )
        registry.get_run.assert_not_called()

    def test_submit_needs_human_rejects_multibyte_message_over_byte_limit(
        self,
    ) -> None:
        """submit_needs_human's own multibyte test, for the same reason -
        it validates a message, not a summary, through a separate code
        path from submit_stage/submit_final."""
        from loopweave.submission import (
            MAX_MESSAGE_BYTES,
            SubmissionError,
            submit_needs_human,
        )

        multibyte_char = "中"
        char_count = (MAX_MESSAGE_BYTES // 3) + 1
        message = multibyte_char * char_count
        self.assertLess(len(message), MAX_MESSAGE_BYTES)
        self.assertGreater(len(message.encode("utf-8")), MAX_MESSAGE_BYTES)

        registry = self._registry_mock()
        with patch("loopweave.submission._registry", return_value=registry):
            with self.assertRaises(SubmissionError):
                submit_needs_human("run-does-not-need-to-exist", message)
        registry.get_run.assert_not_called()

    def test_submit_stage_rejects_multibyte_evidence_item_over_byte_limit(
        self,
    ) -> None:
        """The same chars-vs-bytes distinction applied to a single
        evidence list item passed through the public function, not
        directly through validate_evidence()."""
        from loopweave.submission import (
            EvidenceTooLarge,
            MAX_EVIDENCE_ITEM_BYTES,
            submit_stage,
        )

        multibyte_char = "\U0001f600"  # 1 code point, 4 UTF-8 bytes
        char_count = (MAX_EVIDENCE_ITEM_BYTES // 4) + 1
        item = multibyte_char * char_count
        self.assertLess(len(item), MAX_EVIDENCE_ITEM_BYTES)
        self.assertGreater(len(item.encode("utf-8")), MAX_EVIDENCE_ITEM_BYTES)

        registry = self._registry_mock()
        with patch("loopweave.submission._registry", return_value=registry):
            with self.assertRaises(EvidenceTooLarge):
                submit_stage(
                    "run-does-not-need-to-exist",
                    "Implemented the feature.",
                    evidence={"files_changed": [item], "commands_run": []},
                )
        registry.get_run.assert_not_called()


class SubmitEvidenceFileLoadingContractTests(unittest.TestCase):
    """ADR 0001 sections 3a and 5 (closes reviewer's Stage 1 re-review P1
    finding: the previous version of this test only asserted `code != 0`,
    which any unrelated failure - such as the deliberately nonexistent
    run_id it used - would also satisfy). Proves causality: an invalid
    --evidence-file is rejected by name, before the submission service is
    ever reached, with a diagnostic naming the file, not merely "something
    went wrong later"."""

    def test_load_evidence_file_rejects_invalid_json(self) -> None:
        """Isolates the file-loading step from the CLI: load_evidence_file
        itself must raise on malformed JSON, naming the path."""
        from loopweave.submission import SubmissionError, load_evidence_file

        with tempfile.TemporaryDirectory() as directory:
            evidence_file = Path(directory) / "evidence.json"
            evidence_file.write_text("{not valid json at all", encoding="utf-8")

            with self.assertRaises(SubmissionError) as raised:
                load_evidence_file(evidence_file)
            self.assertIn(str(evidence_file), str(raised.exception))

    def test_load_evidence_file_rejects_non_object_json(self) -> None:
        from loopweave.submission import SubmissionError, load_evidence_file

        with tempfile.TemporaryDirectory() as directory:
            evidence_file = Path(directory) / "evidence.json"
            evidence_file.write_text("[1, 2, 3]", encoding="utf-8")

            with self.assertRaises(SubmissionError):
                load_evidence_file(evidence_file)

    def test_main_submit_rejects_invalid_evidence_file_before_calling_submission_service(
        self,
    ) -> None:
        """Proves causality through the real CLI: submit_stage is patched
        out and asserted never called, so the ONLY way this test can pass
        is if the invalid evidence file is rejected before submit_stage is
        ever reached. A version of this test that only checked the exit
        code (as the previous revision's did) could be satisfied by an
        implementation that ignored the bad file, decoded empty evidence,
        called submit_stage, and only then failed for an unrelated reason -
        assert_not_called() rules that out directly. The exit code and
        stderr message are also checked so a future refactor cannot
        silently drop the diagnostic."""
        import io

        from loopweave.cli import main

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            summary_file = root / "summary.md"
            summary_file.write_text("Implemented the feature.\n", encoding="utf-8")
            evidence_file = root / "evidence.json"
            evidence_file.write_text("{not valid json at all", encoding="utf-8")
            stderr = io.StringIO()

            with patch("loopweave.cli.submit_stage") as submit_stage, patch(
                "sys.argv",
                [
                    "loopweave",
                    "submit",
                    "--stage",
                    "--run-id",
                    "run-does-not-exist",
                    "--summary-file",
                    str(summary_file),
                    "--evidence-file",
                    str(evidence_file),
                ],
            ), patch("sys.stderr", stderr):
                code = main()

            self.assertNotEqual(code, 0)
            submit_stage.assert_not_called()
            self.assertIn(str(evidence_file), stderr.getvalue())


class SubmitCliContractTests(unittest.TestCase):
    """ADR 0001 section 5: `loopweave submit --stage|--final|--needs-human`
    is a registered subcommand with file-backed summary/evidence input."""

    def test_parser_registers_submit_stage(self) -> None:
        from loopweave.cli import build_parser

        parser = build_parser()

        args = parser.parse_args(
            [
                "submit",
                "--stage",
                "--run-id",
                "run-1",
                "--summary-file",
                "/tmp/summary.md",
            ]
        )

        self.assertEqual(args.command, "submit")
        self.assertTrue(args.stage)
        self.assertEqual(args.run_id, "run-1")
        self.assertEqual(args.summary_file, "/tmp/summary.md")

    def test_parser_registers_submit_final(self) -> None:
        from loopweave.cli import build_parser

        parser = build_parser()

        args = parser.parse_args(
            [
                "submit",
                "--final",
                "--run-id",
                "run-1",
                "--summary-file",
                "/tmp/summary.md",
                "--evidence-file",
                "/tmp/evidence.json",
            ]
        )

        self.assertTrue(args.final)
        self.assertEqual(args.evidence_file, "/tmp/evidence.json")

    def test_parser_registers_submit_needs_human(self) -> None:
        from loopweave.cli import build_parser

        parser = build_parser()

        args = parser.parse_args(
            [
                "submit",
                "--needs-human",
                "--run-id",
                "run-1",
                "--message-file",
                "/tmp/message.md",
            ]
        )

        self.assertTrue(args.needs_human)
        self.assertEqual(args.message_file, "/tmp/message.md")

    def test_submit_stage_and_final_are_mutually_exclusive(self) -> None:
        from loopweave.cli import build_parser

        parser = build_parser()

        with self.assertRaises(SystemExit):
            parser.parse_args(
                [
                    "submit",
                    "--stage",
                    "--final",
                    "--run-id",
                    "run-1",
                    "--summary-file",
                    "/tmp/summary.md",
                ]
            )

    def test_parser_leaves_run_id_none_when_omitted(self) -> None:
        """The parser itself must not resolve the environment fallback -
        that is main()'s job, proven separately below through the real
        entry point. This test only proves --run-id is optional at the
        argparse layer."""
        from loopweave.cli import build_parser

        parser = build_parser()

        args = parser.parse_args(
            ["submit", "--stage", "--summary-file", "/tmp/summary.md"]
        )

        self.assertIsNone(args.run_id)

    def test_main_submit_stage_invokes_submission_service(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            summary_file = root / "summary.md"
            summary_file.write_text("Implemented the feature.\n", encoding="utf-8")

            with patch("loopweave.cli.submit_stage") as submit_stage, patch(
                "sys.argv",
                [
                    "loopweave",
                    "submit",
                    "--stage",
                    "--run-id",
                    "run-1",
                    "--summary-file",
                    str(summary_file),
                ],
            ):
                from loopweave.cli import main

                code = main()

            self.assertEqual(code, 0)
            submit_stage.assert_called_once()
            call_args = submit_stage.call_args
            self.assertEqual(call_args.args[0], "run-1")
            self.assertIn("Implemented the feature.", call_args.args[1])

    def test_main_submit_needs_human_invokes_submission_service(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            message_file = root / "message.md"
            message_file.write_text(
                "Please choose the deployment target.\n", encoding="utf-8"
            )

            with patch(
                "loopweave.cli.submit_needs_human"
            ) as submit_needs_human, patch(
                "sys.argv",
                [
                    "loopweave",
                    "submit",
                    "--needs-human",
                    "--run-id",
                    "run-1",
                    "--message-file",
                    str(message_file),
                ],
            ):
                from loopweave.cli import main

                code = main()

            self.assertEqual(code, 0)
            submit_needs_human.assert_called_once()
            call_args = submit_needs_human.call_args
            self.assertEqual(call_args.args[0], "run-1")
            self.assertIn(
                "Please choose the deployment target.", call_args.args[1]
            )

    def test_main_submit_final_invokes_submission_service(self) -> None:
        """--final has its own main() routing test: proving --stage and
        --needs-human route correctly (the two tests above) does not prove
        --final does, since Stage 2 could wire it to the wrong service
        function or drop it silently while every other test in this file
        still passes."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            summary_file = root / "summary.md"
            summary_file.write_text("The full task is complete.\n", encoding="utf-8")
            evidence_file = root / "evidence.json"
            evidence_file.write_text(
                json.dumps({"files_changed": ["app.py"], "commands_run": []}),
                encoding="utf-8",
            )

            with patch("loopweave.cli.submit_final") as submit_final, patch(
                "sys.argv",
                [
                    "loopweave",
                    "submit",
                    "--final",
                    "--run-id",
                    "run-1",
                    "--summary-file",
                    str(summary_file),
                    "--evidence-file",
                    str(evidence_file),
                ],
            ):
                from loopweave.cli import main

                code = main()

            self.assertEqual(code, 0)
            submit_final.assert_called_once()
            call_args = submit_final.call_args
            self.assertEqual(call_args.args[0], "run-1")
            self.assertIn("The full task is complete.", call_args.args[1])
            self.assertEqual(
                call_args.kwargs.get("evidence", {}).get("files_changed"),
                ["app.py"],
            )

    def test_main_submit_uses_environment_run_id_when_flag_omitted(self) -> None:
        """Proves the $LOOPWEAVE_RUN_ID fallback through the real entry
        point - the previous version of this test parsed args and then
        computed `args.run_id or os.environ.get(...)` in the test body
        itself, which tests Python's `or` operator, not main()'s actual
        behavior. An implementation that ignores the environment variable
        entirely could still pass that version of the test."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            summary_file = root / "summary.md"
            summary_file.write_text("Implemented the feature.\n", encoding="utf-8")

            with patch("loopweave.cli.submit_stage") as submit_stage, patch.dict(
                "os.environ", {"LOOPWEAVE_RUN_ID": "run-from-env"}, clear=False
            ), patch(
                "sys.argv",
                [
                    "loopweave",
                    "submit",
                    "--stage",
                    "--summary-file",
                    str(summary_file),
                ],
            ):
                from loopweave.cli import main

                code = main()

            self.assertEqual(code, 0)
            submit_stage.assert_called_once()
            self.assertEqual(submit_stage.call_args.args[0], "run-from-env")


class ControlSenderLazyResolutionContractTests(unittest.TestCase):
    """ADR 0001 section 2b (closes reviewer's P1 #2, part B): assignment.py and
    completion_notifier.py must not import supervisor.send_control_message
    at module scope, and their default sender must resolve lazily (at call/
    construction time) via terminal_host.default_control_sender(), never at
    import time - a naive `sender=default_control_sender()` default-
    expression would call it while the module is being imported, which is
    exactly the bug this ADR exists to remove."""

    def _run_probe(self, probe_body: str) -> subprocess.CompletedProcess:
        script = _POSIX_IMPORT_BLOCKER_PREAMBLE + probe_body
        return subprocess.run(
            [sys.executable, "-c", script],
            cwd=str(SRC_ROOT),
            capture_output=True,
            text=True,
            timeout=30,
        )

    def test_assignment_module_imports_without_posix_only_modules(self) -> None:
        result = self._run_probe(
            "import loopweave.assignment\nprint('IMPORT_OK')\n"
        )
        self.assertEqual(
            result.returncode,
            0,
            "importing loopweave.assignment must not require pty/"
            "termios/tty/fcntl; stderr:\n{}".format(result.stderr),
        )
        self.assertIn("IMPORT_OK", result.stdout)

    def test_completion_notifier_module_imports_without_posix_only_modules(
        self,
    ) -> None:
        result = self._run_probe(
            "import loopweave.completion_notifier\nprint('IMPORT_OK')\n"
        )
        self.assertEqual(
            result.returncode,
            0,
            "importing loopweave.completion_notifier must not require "
            "pty/termios/tty/fcntl; stderr:\n{}".format(result.stderr),
        )
        self.assertIn("IMPORT_OK", result.stdout)

    def test_assign_task_default_sender_resolves_lazily_not_at_import(
        self,
    ) -> None:
        """Importing assignment.py must succeed on every platform; the
        default control sender resolves lazily at call time through
        terminal_host.default_control_sender(), and on win32 it is the
        Windows named-pipe transport, never the POSIX supervisor sender."""
        import loopweave.control_transport as control_transport_module
        from loopweave.terminal_host import default_control_sender

        with patch("loopweave.terminal_host.sys.platform", "win32"):
            sender = default_control_sender()
        self.assertIs(
            sender, control_transport_module.send_control_message
        )

    def test_completion_notifier_default_sender_resolves_at_construction_not_import(
        self,
    ) -> None:
        """CompletionNotifier() itself resolves its default sender when no
        explicit sender is passed (ADR 0001 section 2b: 'construction
        time'). Importing the module must succeed regardless of platform;
        on win32 construction resolves the Windows named-pipe sender."""
        import loopweave.completion_notifier as completion_notifier_module
        import loopweave.control_transport as control_transport_module

        with patch("loopweave.terminal_host.sys.platform", "win32"):
            notifier = completion_notifier_module.CompletionNotifier(
                process_start=lambda pid: "fake-start-time",
            )
        self.assertIs(
            notifier.sender,
            control_transport_module.send_control_message,
        )

    def test_completion_notifier_accepts_explicit_sender_on_any_platform(
        self,
    ) -> None:
        """An explicit sender bypasses platform resolution entirely, so
        constructing CompletionNotifier(sender=...) must not raise on that
        parameter's account even when simulated as win32 - only the
        *default* sender path is gated. An explicit process_start is also
        supplied here so this test isolates the sender behavior alone;
        process_start's own platform gating is covered separately by
        ProcessIdentityProviderContractTests."""
        import loopweave.completion_notifier as completion_notifier_module

        def explicit_sender(path, payload, timeout=3.0):
            return {"status": "ok"}

        def explicit_process_start(pid):
            return "explicit-start-time"

        with patch("loopweave.terminal_host.sys.platform", "win32"):
            notifier = completion_notifier_module.CompletionNotifier(
                sender=explicit_sender,
                process_start=explicit_process_start,
            )
        self.assertIs(notifier.sender, explicit_sender)
        self.assertIs(notifier.process_start, explicit_process_start)

    def test_assign_task_default_sender_consults_default_control_sender(
        self,
    ) -> None:
        """When no explicit sender is supplied, assign_task() must go
        through terminal_host.default_control_sender() rather than a
        module-scope-captured send_control_message binding."""
        import loopweave.terminal_host as terminal_host_module
        from loopweave.models import RunRecord, RunState

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            task = root / "task.md"
            task.write_text("# Task\n", encoding="utf-8")
            socket_path = root / "control.sock"
            socket_path.write_text("", encoding="utf-8")
            run = RunRecord(
                run_id="run-1",
                codex_thread_id="thread-1",
                cwd=str(root),
                thread_cwd=str(root),
                workspace_root=str(root),
                tty="/dev/test",
                agent="claude",
                agent_pid=123,
                agent_process_start="start",
                control_token="secret",
                state=RunState.RUNNING,
                run_dir=str(root / "run"),
                socket_path=str(socket_path),
            )

            def fake_sender(path, payload, timeout=3.0):
                return {"status": "ok"}

            def fake_reader(pid):
                return run.agent_process_start

            from loopweave.assignment import assign_task

            with patch.object(
                terminal_host_module,
                "default_control_sender",
                return_value=fake_sender,
            ) as factory:
                assign_task(run, task, process_start_reader=fake_reader)

            factory.assert_called()

    def test_completion_notifier_default_sender_consults_default_control_sender(
        self,
    ) -> None:
        """When no explicit sender is supplied, CompletionNotifier() must
        go through terminal_host.default_control_sender() at construction
        - proving the "both" claim this class's docstring makes, which the
        earlier (assign_task-only) version of this test did not actually
        demonstrate for CompletionNotifier."""
        import loopweave.terminal_host as terminal_host_module

        def fake_sender(path, payload, timeout=3.0):
            return {"status": "ok"}

        with patch.object(
            terminal_host_module,
            "default_control_sender",
            return_value=fake_sender,
        ) as factory:
            from loopweave.completion_notifier import CompletionNotifier

            notifier = CompletionNotifier()

        factory.assert_called()
        self.assertIs(notifier.sender, fake_sender)


class ProcessIdentityProviderContractTests(unittest.TestCase):
    """ADR 0001 section 2a (closes reviewer's Stage 1 re-review P1 finding:
    declaring TerminalHost.process_identity() alone does not put an
    out-of-process liveness check behind a platform-neutral seam, since
    every out-of-process check runs without a live TerminalHost object).
    Mirrors ControlSenderLazyResolutionContractTests exactly, because the
    coupling is the same shape (a POSIX-only free function imported at
    module scope by many otherwise-portable callers), for
    process_start_time instead of send_control_message."""

    def _run_probe(self, probe_body: str) -> subprocess.CompletedProcess:
        script = _POSIX_IMPORT_BLOCKER_PREAMBLE + probe_body
        return subprocess.run(
            [sys.executable, "-c", script],
            cwd=str(SRC_ROOT),
            capture_output=True,
            text=True,
            timeout=30,
        )

    def test_bridge_control_module_imports_without_posix_only_modules(
        self,
    ) -> None:
        result = self._run_probe(
            "import loopweave.bridge_control\nprint('IMPORT_OK')\n"
        )
        self.assertEqual(
            result.returncode,
            0,
            "importing loopweave.bridge_control must not require "
            "pty/termios/tty/fcntl; stderr:\n{}".format(result.stderr),
        )
        self.assertIn("IMPORT_OK", result.stdout)

    def test_bridge_controller_reconcile_stale_pending_reviews_consults_shared_provider(
        self,
    ) -> None:
        """BridgeController.reconcile_stale_pending_reviews
        (bridge_control.py:183) has no existing injectable parameter at
        all - it calls the bare process_start_time name directly. This
        test proves the runtime call goes through
        terminal_host.default_process_identity_reader() once Stage 2
        migrates it, using a minimal fixture: a run with a pending
        visible-review card and no live process, which the method must
        orphan when the identity check fails."""
        import loopweave.terminal_host as terminal_host_module
        from loopweave.bridge_control import BridgeController
        from loopweave.models import ReviewBackend, RunRecord, RunState
        from loopweave.registry import Registry

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            run_dir = root / "run-1"
            (run_dir / "review-inbox").mkdir(parents=True)
            (run_dir / "review-inbox" / "pending").write_text(
                "review-request-1\n", encoding="utf-8"
            )
            registry = Registry(root / "registry.sqlite")
            registry.create_run(
                RunRecord(
                    run_id="run-1",
                    codex_thread_id="thread-1",
                    cwd=str(root),
                    tty="/dev/test",
                    agent="claude",
                    agent_pid=123,
                    agent_process_start="worker-start",
                    control_token="secret",
                    state=RunState.RUNNING,
                    run_dir=str(run_dir),
                    reviewer_backend=ReviewBackend.VISIBLE_THREAD,
                )
            )

            def fake_reader(pid):
                raise RuntimeError("process gone")

            with patch.object(
                terminal_host_module,
                "default_process_identity_reader",
                return_value=fake_reader,
            ) as factory:
                controller = BridgeController(
                    root=root, registry=registry, sessions_dir=root / "sessions"
                )
                controller.reconcile_stale_pending_reviews(["run-1"])

            factory.assert_called()
            self.assertEqual(
                registry.get_run("run-1").state, RunState.ORPHANED
            )

    def test_dispatcher_module_imports_without_posix_only_modules(self) -> None:
        result = self._run_probe(
            "import loopweave.dispatcher\nprint('IMPORT_OK')\n"
        )
        self.assertEqual(
            result.returncode,
            0,
            "importing loopweave.dispatcher must not require "
            "pty/termios/tty/fcntl; stderr:\n{}".format(result.stderr),
        )
        self.assertIn("IMPORT_OK", result.stdout)

    def test_dispatcher_inspect_dispatch_lease_consults_shared_provider(
        self,
    ) -> None:
        """Import-only probes prove supervisor.py's guarded imports do not
        crash dispatcher.py on import - they do NOT prove
        inspect_dispatch_lease() stopped calling
        supervisor.process_start_time directly at the point of use. A
        module could still do `from .supervisor import process_start_time`
        at module scope (which succeeds fine once supervisor.py's imports
        are guarded) and never touch the shared provider at all. This test
        proves runtime consultation with a real dispatch.lock fixture,
        the same fixture shape tests/test_dispatcher.py already uses."""
        import json as json_module

        import loopweave.terminal_host as terminal_host_module
        from loopweave.dispatcher import DispatchLeaseState, inspect_dispatch_lease

        with tempfile.TemporaryDirectory() as directory:
            run_dir = Path(directory)
            (run_dir / "dispatch.lock").write_text(
                json_module.dumps(
                    {
                        "pid": 999999,
                        "process_start": "fake-lease-start",
                        "binding_generation": 1,
                        "created_at": "2026-06-19T00:00:00+00:00",
                        "lease_id": "lease-1",
                    }
                ),
                encoding="utf-8",
            )

            def fake_reader(pid):
                return "fake-lease-start"

            with patch.object(
                terminal_host_module,
                "default_process_identity_reader",
                return_value=fake_reader,
            ) as factory:
                state = inspect_dispatch_lease(run_dir)

            factory.assert_called()
            self.assertEqual(state, DispatchLeaseState.LIVE)

    def test_thread_takeover_module_imports_without_posix_only_modules(
        self,
    ) -> None:
        result = self._run_probe(
            "import loopweave.thread_takeover\nprint('IMPORT_OK')\n"
        )
        self.assertEqual(
            result.returncode,
            0,
            "importing loopweave.thread_takeover must not require "
            "pty/termios/tty/fcntl; stderr:\n{}".format(result.stderr),
        )
        self.assertIn("IMPORT_OK", result.stdout)

    def test_thread_takeover_coordinator_process_start_consults_shared_provider(
        self,
    ) -> None:
        """ThreadTakeoverCoordinator's existing process_start parameter
        (thread_takeover.py:76) must default to the shared provider, not
        a direct supervisor.process_start_time import - proven the same
        way section 2's CompletionNotifier sender test is proven, by
        constructing with no explicit process_start and calling attach(),
        which invokes self.process_start(run.agent_pid) before any
        Codex-thread-discovery machinery runs (thread_takeover.py:93) -
        so this test needs no session-file fixtures, only a registry and
        a run."""
        import loopweave.terminal_host as terminal_host_module
        from loopweave.models import RunRecord, RunState
        from loopweave.registry import Registry
        from loopweave.thread_takeover import ThreadTakeoverCoordinator, WorkerUnavailable

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            run_dir = root / "run-1"
            run_dir.mkdir()
            registry = Registry(root / "registry.sqlite")
            registry.create_run(
                RunRecord(
                    run_id="run-1",
                    codex_thread_id="thread-1",
                    cwd=str(root),
                    tty="/dev/test",
                    agent="claude",
                    agent_pid=123,
                    agent_process_start="worker-start",
                    control_token="secret",
                    state=RunState.RUNNING,
                    run_dir=str(run_dir),
                )
            )

            def fake_reader(pid):
                return "a-different-start-time"

            with patch.object(
                terminal_host_module,
                "default_process_identity_reader",
                return_value=fake_reader,
            ) as factory:
                coordinator = ThreadTakeoverCoordinator(registry, root / "sessions")
                with self.assertRaises(WorkerUnavailable):
                    coordinator.attach("run-1", explicit_thread_id="thread-2")

            factory.assert_called()
            self.assertEqual(
                registry.get_run("run-1").state, RunState.ORPHANED
            )

    def test_default_process_identity_reader_returns_process_start_time_on_posix(
        self,
    ) -> None:
        """On a POSIX platform, default_process_identity_reader() must
        return exactly supervisor.process_start_time - the mechanical,
        non-behavioral guarantee ADR section 2a makes."""
        from loopweave.supervisor import process_start_time
        from loopweave.terminal_host import default_process_identity_reader

        with patch("loopweave.terminal_host.sys.platform", "darwin"):
            reader = default_process_identity_reader()

        self.assertIs(reader, process_start_time)

    def test_default_process_identity_reader_resolves_windows_reader_on_win32(
        self,
    ) -> None:
        """On win32 the default process-identity reader is the Windows
        GetProcessTimes implementation, never the POSIX ``ps`` lookup."""
        import loopweave.windows_terminal_host as windows_host_module
        from loopweave.terminal_host import default_process_identity_reader

        with patch("loopweave.terminal_host.sys.platform", "win32"):
            reader = default_process_identity_reader()
        self.assertIs(reader, windows_host_module.process_start_time)

    def test_completion_notifier_process_start_resolves_through_shared_provider(
        self,
    ) -> None:
        """CompletionNotifier's existing process_start parameter
        (completion_notifier.py:16) must default to the shared provider,
        not a direct supervisor.process_start_time import - proven the
        same way section 2's CompletionNotifier sender test is proven."""
        import loopweave.terminal_host as terminal_host_module

        def fake_reader(pid):
            return "fake-start-time"

        with patch.object(
            terminal_host_module,
            "default_process_identity_reader",
            return_value=fake_reader,
        ) as factory:
            from loopweave.completion_notifier import CompletionNotifier

            notifier = CompletionNotifier()

        factory.assert_called()
        self.assertIs(notifier.process_start, fake_reader)

    def test_assign_task_process_start_reader_resolves_through_shared_provider(
        self,
    ) -> None:
        """assign_task's existing process_start_reader parameter
        (assignment.py:137) must default to the shared provider when the
        caller supplies none, resolved at call time - the same lazy-
        resolution pattern as its sender parameter."""
        import loopweave.terminal_host as terminal_host_module
        from loopweave.models import RunRecord, RunState

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            task = root / "task.md"
            task.write_text("# Task\n", encoding="utf-8")
            socket_path = root / "control.sock"
            socket_path.write_text("", encoding="utf-8")
            run = RunRecord(
                run_id="run-1",
                codex_thread_id="thread-1",
                cwd=str(root),
                thread_cwd=str(root),
                workspace_root=str(root),
                tty="/dev/test",
                agent="claude",
                agent_pid=123,
                agent_process_start="fake-start-time",
                control_token="secret",
                state=RunState.RUNNING,
                run_dir=str(root / "run"),
                socket_path=str(socket_path),
            )

            def fake_sender(path, payload, timeout=3.0):
                return {"status": "ok"}

            def fake_reader(pid):
                return "fake-start-time"

            from loopweave.assignment import assign_task

            with patch.object(
                terminal_host_module,
                "default_control_sender",
                return_value=fake_sender,
            ), patch.object(
                terminal_host_module,
                "default_process_identity_reader",
                return_value=fake_reader,
            ) as factory:
                assign_task(run, task)

            factory.assert_called()

    def test_reconcile_run_liveness_consults_shared_provider(self) -> None:
        """_reconcile_run_liveness (cli.py:1078) calls process_start_time
        directly in the baseline. After Stage 2 migrates it, the call must
        go through terminal_host.default_process_identity_reader(). Proven
        with the stale-process scenario: the fake reader raises RuntimeError,
        the function must orphan the run — factory consultation is both
        necessary and observable, and the resulting ORPHANED state proves
        the correct live/stale branch was taken."""
        import loopweave.terminal_host as terminal_host_module
        from loopweave.cli import _reconcile_run_liveness
        from loopweave.models import RunRecord, RunState
        from loopweave.registry import Registry

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            run_dir = root / "run-1"
            run_dir.mkdir()
            registry = Registry(root / "registry.sqlite")
            registry.create_run(
                RunRecord(
                    run_id="run-1",
                    codex_thread_id="thread-1",
                    cwd=str(root),
                    tty="/dev/test",
                    agent="claude",
                    agent_pid=123,
                    agent_process_start="worker-start",
                    control_token="secret",
                    state=RunState.RUNNING,
                    run_dir=str(run_dir),
                )
            )

            def fake_reader(pid):
                raise RuntimeError("process gone")

            with patch.object(
                terminal_host_module,
                "default_process_identity_reader",
                return_value=fake_reader,
            ) as factory:
                _reconcile_run_liveness(registry, registry.get_run("run-1"))

            factory.assert_called()
            self.assertEqual(
                registry.get_run("run-1").state, RunState.ORPHANED
            )


class CliDeliveryPathsUseSharedControlSenderContractTests(unittest.TestCase):
    """ADR 0001 section 2b (closes reviewer's Stage 1 re-review P1 finding: the
    original ControlSenderLazyResolutionContractTests never named or
    exercised cli._stop_run, cli._deliver_review, or
    cli._finalize_owner_review, even though ADR section 2b explicitly
    requires all three to stop calling the directly-imported POSIX
    send_control_message). Each of the three delivery paths gets its own
    test proving the default resolves through
    terminal_host.default_control_sender() - a bound proven on one path
    does not prove the other two, since Stage 2 could migrate one call
    site and miss the others."""

    @staticmethod
    def _make_run(root: Path, *, state):
        from loopweave.models import RunRecord

        run_dir = root / "run-1"
        run_dir.mkdir(parents=True, exist_ok=True)
        socket_path = root / "control.sock"
        socket_path.write_text("", encoding="utf-8")
        return RunRecord(
            run_id="run-1",
            codex_thread_id="thread-1",
            cwd=str(root),
            thread_cwd=str(root),
            workspace_root=str(root),
            tty="/dev/test",
            agent="generic",
            agent_pid=123,
            agent_process_start="start",
            control_token="secret",
            state=state,
            run_dir=str(run_dir),
            socket_path=str(socket_path),
        )

    def test_stop_run_uses_default_control_sender(self) -> None:
        import loopweave.terminal_host as terminal_host_module
        from loopweave.cli import _stop_run
        from loopweave.models import RunState
        from loopweave.registry import Registry

        def fake_sender(path, payload, timeout=3.0):
            return {"status": "ok"}

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            run = self._make_run(root, state=RunState.RUNNING)
            registry = Registry(root / "registry.sqlite")
            registry.create_run(run)

            with patch.object(
                terminal_host_module,
                "default_control_sender",
                return_value=fake_sender,
            ) as factory, patch.object(
                terminal_host_module,
                "default_process_identity_reader",
                return_value=lambda pid: (_ for _ in ()).throw(
                    RuntimeError("process gone")
                ),
            ) as identity_factory:
                _stop_run(registry, run, timeout=0)

            factory.assert_called()
            identity_factory.assert_called()

    def test_deliver_review_uses_default_control_sender(self) -> None:
        import json

        import loopweave.terminal_host as terminal_host_module
        from loopweave.cli import _deliver_review
        from loopweave.models import RunState
        from loopweave.registry import Registry

        sent = []

        def fake_sender(path, payload, timeout=3.0):
            sent.append(payload)
            return {"status": "ok"}

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            run = self._make_run(root, state=RunState.REVIEW_READY)
            run_dir = Path(run.run_dir)
            (run_dir / "reviewer-verdict.md").write_text(
                "Change the implementation.\n", encoding="utf-8"
            )
            (run_dir / "reviewer-verdict.json").write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "run_id": "run-1",
                        "review_id": "review-1",
                        "verdict": "changes_requested",
                        "summary": "One change required.",
                        "review_file": "reviewer-verdict.md",
                        "continue": True,
                    }
                ),
                encoding="utf-8",
            )
            registry = Registry(root / "registry.sqlite")
            registry.create_run(run)

            with patch.object(
                terminal_host_module,
                "default_control_sender",
                return_value=fake_sender,
            ) as factory, patch.object(
                terminal_host_module,
                "default_process_identity_reader",
                return_value=lambda pid: run.agent_process_start,
            ) as identity_factory:
                _deliver_review(registry, run)

            factory.assert_called()
            identity_factory.assert_called()
            self.assertTrue(sent)

    def test_finalize_owner_review_changes_requested_uses_default_control_sender(
        self,
    ) -> None:
        import json

        import loopweave.terminal_host as terminal_host_module
        from loopweave.cli import _finalize_owner_review
        from loopweave.models import RunState
        from loopweave.registry import Registry

        sent = []

        def fake_sender(path, payload, timeout=3.0):
            sent.append(payload)
            return {"status": "ok"}

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            run = self._make_run(root, state=RunState.OWNER_REVIEW_PENDING)
            run_dir = Path(run.run_dir)
            (run_dir / "reviewer-verdict.md").write_text(
                "Final review body.\n", encoding="utf-8"
            )
            (run_dir / "reviewer-verdict.json").write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "run_id": "run-1",
                        "review_id": "review-1",
                        "verdict": "approved",
                        "summary": "All checks passed.",
                        "review_file": "reviewer-verdict.md",
                        "continue": False,
                    }
                ),
                encoding="utf-8",
            )
            registry = Registry(root / "registry.sqlite")
            registry.create_run(run)

            with patch.object(
                terminal_host_module,
                "default_control_sender",
                return_value=fake_sender,
            ) as factory, patch.object(
                terminal_host_module,
                "default_process_identity_reader",
                return_value=lambda pid: run.agent_process_start,
            ) as identity_factory:
                _finalize_owner_review(
                    registry,
                    run,
                    approved=False,
                    message="Please fix the cleanup path.",
                )

            factory.assert_called()
            identity_factory.assert_called()
            self.assertTrue(sent)


@unittest.skipIf(sys.platform == "win32", "POSIX pty required")
class RunIdentityEnvironmentContractTests(unittest.TestCase):
    """ADR 0001 section 4: the managed child receives a bounded, non-secret
    run identity through LOOPWEAVE_RUN_ID at spawn time."""

    def test_posix_backend_sets_run_id_environment_variable_on_child(self) -> None:
        from loopweave.terminal_host import create_terminal_host

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            script = root / "print_env.py"
            script.write_text(
                "import os\n"
                "print('RUN_ID=' + os.environ.get('LOOPWEAVE_RUN_ID', ''))\n",
                encoding="utf-8",
            )
            host = create_terminal_host(
                run_id="run-abcdef123456",
                command=[sys.executable, "-u", str(script)],
                cwd=root,
                run_dir=root / "run",
                socket_path=root / "control.sock",
                control_token="secret",
                passthrough=False,
            )
            host.start()
            try:
                output = self._wait_for(
                    root / "run" / "terminal.txt", "RUN_ID=run-abcdef123456"
                )
                self.assertIn("RUN_ID=run-abcdef123456", output)
            finally:
                host.stop()

    def test_control_token_is_absent_from_the_child_environment(self) -> None:
        """ADR 0001 section 5: the control token is deliberately NOT
        passed to the child - proven by dumping the child's full
        environment (not just checking LOOPWEAVE_RUN_ID, which the
        previous version of this test did) and asserting neither the
        literal token value nor any control-token-named key is present.
        A distinctive, unlikely-to-collide token value is used so a
        substring match cannot pass by coincidence."""
        from loopweave.terminal_host import create_terminal_host

        distinctive_control_token = "totally-distinct-control-token-97531"

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            script = root / "dump_env.py"
            script.write_text(
                "import os\n"
                "print('ENV_DUMP_START')\n"
                "for key, value in sorted(os.environ.items()):\n"
                "    print('{}={}'.format(key, value))\n"
                "print('ENV_DUMP_END')\n",
                encoding="utf-8",
            )
            host = create_terminal_host(
                run_id="run-abcdef123456",
                command=[sys.executable, "-u", str(script)],
                cwd=root,
                run_dir=root / "run",
                socket_path=root / "control.sock",
                control_token=distinctive_control_token,
                passthrough=False,
            )
            host.start()
            try:
                output = self._wait_for(
                    root / "run" / "terminal.txt", "ENV_DUMP_END"
                )
            finally:
                host.stop()

            self.assertNotIn(distinctive_control_token, output)
            for line in output.splitlines():
                if "=" not in line:
                    continue
                key = line.split("=", 1)[0]
                self.assertNotIn(
                    "CONTROL_TOKEN",
                    key.upper(),
                    "child environment must not expose a control-token-"
                    "named key: {!r}".format(key),
                )

    @staticmethod
    def _wait_for(path: Path, text: str, timeout: float = 3.0) -> str:
        import time

        deadline = time.time() + timeout
        while time.time() < deadline:
            output = (
                path.read_text(encoding="utf-8", errors="replace")
                if path.exists()
                else ""
            )
            if text in output:
                return output
            time.sleep(0.02)
        raise AssertionError("timed out waiting for {!r}".format(text))


class ClaudeStopHookDelegationContractTests(unittest.TestCase):
    """ADR 0001 section 8: handle_claude_stop must delegate its stage/final/
    needs-human tail to submission.py rather than duplicating state-machine
    logic inline. These tests prove the delegation by patching the submission
    service functions and asserting they are called with the expected arguments,
    so the contract cannot silently break if hook_entry.py is refactored back
    to inline dispatch."""

    def _make_registry(self, root: Path, *, reviewer_backend=None):
        from loopweave.models import ReviewBackend, RunRecord, RunState
        from loopweave.registry import Registry

        run_dir = root / "run-1"
        run_dir.mkdir(parents=True, exist_ok=True)
        registry = Registry(root / "registry.sqlite")
        registry.create_run(
            RunRecord(
                run_id="run-1",
                codex_thread_id="thread-1",
                cwd=str(root),
                tty="/dev/test",
                agent="claude",
                agent_pid=123,
                agent_process_start="start",
                control_token="secret",
                state=RunState.RUNNING,
                run_dir=str(run_dir),
                reviewer_backend=reviewer_backend or ReviewBackend.EPHEMERAL,
            )
        )
        return registry, run_dir

    def _payload(self, root: Path, *, message: str) -> dict:
        transcript = root / "transcript.jsonl"
        transcript.write_text(
            json.dumps({
                "message": {
                    "content": [
                        {"type": "tool_use", "name": "Edit",
                         "input": {"file_path": str(root / "app.py")}},
                    ]
                }
            }) + "\n",
            encoding="utf-8",
        )
        return {
            "last_assistant_message": message,
            "transcript_path": str(transcript),
        }

    def test_hook_delegates_stage_to_submit_stage(self) -> None:
        """handle_claude_stop calls submit_stage (not inline dispatch) for
        LOOPWEAVE_STAGE marker turns."""
        from loopweave.hook_entry import handle_claude_stop

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            registry, run_dir = self._make_registry(root)
            payload = self._payload(root, message="LOOPWEAVE_STAGE\nStage summary.")
            dispatched = []

            with patch(
                "loopweave.hook_entry.submit_stage"
            ) as mock_submit, patch(
                "loopweave.hook_entry.submit_final"
            ), patch(
                "loopweave.hook_entry.submit_needs_human"
            ):
                mock_submit.side_effect = lambda *a, **kw: dispatched.append("stage")
                result = handle_claude_stop(
                    "run-1", payload, registry, lambda run: None
                )

            self.assertEqual(result, "dispatched")
            mock_submit.assert_called_once()
            call_args = mock_submit.call_args
            self.assertEqual(call_args.args[0], "run-1")
            self.assertEqual(dispatched, ["stage"])

    def test_hook_delegates_final_to_submit_final(self) -> None:
        """handle_claude_stop calls submit_final for LOOPWEAVE_FINAL turns."""
        from loopweave.hook_entry import handle_claude_stop

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            registry, run_dir = self._make_registry(root)
            payload = self._payload(root, message="LOOPWEAVE_FINAL\nFinal summary.")
            dispatched = []

            with patch(
                "loopweave.hook_entry.submit_final"
            ) as mock_submit, patch(
                "loopweave.hook_entry.submit_stage"
            ), patch(
                "loopweave.hook_entry.submit_needs_human"
            ):
                mock_submit.side_effect = lambda *a, **kw: dispatched.append("final")
                result = handle_claude_stop(
                    "run-1", payload, registry, lambda run: None
                )

            self.assertEqual(result, "dispatched")
            mock_submit.assert_called_once()
            self.assertEqual(dispatched, ["final"])

    def test_hook_delegates_needs_human_to_submit_needs_human(self) -> None:
        """handle_claude_stop calls submit_needs_human for LOOPWEAVE_NEEDS_HUMAN
        turns instead of directly force-state-ing the run."""
        from loopweave.hook_entry import handle_claude_stop

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            registry, run_dir = self._make_registry(root)
            payload = self._payload(
                root,
                message="LOOPWEAVE_NEEDS_HUMAN\nPlease choose the target.",
            )
            called = []

            with patch(
                "loopweave.hook_entry.submit_needs_human"
            ) as mock_submit, patch(
                "loopweave.hook_entry.submit_stage"
            ), patch(
                "loopweave.hook_entry.submit_final"
            ):
                mock_submit.side_effect = lambda *a, **kw: called.append("needs_human")
                result = handle_claude_stop(
                    "run-1", payload, registry, lambda run: None
                )

            self.assertEqual(result, "needs-human")
            mock_submit.assert_called_once()
            call_args = mock_submit.call_args
            self.assertEqual(call_args.args[0], "run-1")
            self.assertEqual(called, ["needs_human"])


class ExplicitGenericCommandContractTests(unittest.TestCase):
    """ADR 0001 section 7: loopweave run -- custom-agent --flag value must
    launch 'custom-agent' as the executable, not '--'. Proven by checking the
    command built by the adapter resolved from the parsed args — args.agent=='--'
    must be treated as the explicit-generic sentinel, not forwarded to
    get_adapter."""

    def test_explicit_double_dash_launches_first_remaining_arg(self) -> None:
        from loopweave.cli import LoopWeaveArgumentParser

        parser = LoopWeaveArgumentParser(prog="loopweave")
        args = parser.parse_args(["run", "--", "custom-agent", "--flag", "value"])

        self.assertEqual(args.agent, "--")
        self.assertEqual(args.agent_args, ["custom-agent", "--flag", "value"])

    def test_main_run_double_dash_with_no_command_raises_clear_error(self) -> None:
        """reviewer Stage 2 re-review P1 #3: `loopweave run --` is rewritten by
        main() to `run generic --`, so _run_agent receives agent='generic',
        agent_args=[] - the empty-command guard must fire on BOTH generic
        representations and surface a clear error through the real main()
        entry path, not silently fall through to GenericAdapter([])."""
        import io
        from types import SimpleNamespace
        from unittest.mock import MagicMock

        from loopweave.cli import main

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            control = root / "control"
            control.mkdir()
            stderr = io.StringIO()

            with patch(
                "loopweave.cli.RUNS_DIR", control / "runs"
            ), patch(
                "loopweave.cli.VAR_DIR", control / "var"
            ), patch(
                "loopweave.cli.PROJECTS_DIR", control / "projects"
            ), patch(
                "loopweave.cli._registry", return_value=MagicMock()
            ), patch(
                "loopweave.cli.discover_thread",
                return_value=SimpleNamespace(
                    thread_id="thread-1", cwd=str(control)
                ),
            ), patch(
                "loopweave.cli.os.getcwd", return_value=str(control)
            ), patch(
                "sys.argv", ["loopweave", "run", "--"]
            ), patch(
                "sys.stderr", stderr
            ):
                code = main()

            self.assertEqual(code, 2)
            self.assertIn("requires a command", stderr.getvalue())

    def test_explicit_double_dash_adapter_command_starts_with_custom_agent(
        self,
    ) -> None:
        """Proves the full path from parsed args to the GenericAdapter command:
        args.agent=='--' must NOT call get_adapter('--', ...) which would produce
        ['--', 'custom-agent', '--flag', 'value'] as the command."""
        from types import SimpleNamespace
        from unittest.mock import MagicMock
        import loopweave.terminal_host as terminal_host_module
        from loopweave.cli import _run_agent
        from loopweave.registry import Registry

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            control = root / "control"
            workspace = root / "workspace"
            control.mkdir()
            workspace.mkdir()
            projects = control / "projects"
            runs = control / "runs"
            var = control / "var"
            registry = Registry(var / "registry.sqlite")

            fake_host = MagicMock()
            fake_host.start.return_value = 99999
            fake_host.run_foreground.return_value = 0
            captured_command = []

            def fake_create_host(*, command, **kwargs):
                captured_command.extend(command)
                return fake_host

            from types import SimpleNamespace
            args = SimpleNamespace(
                cwd=str(control),
                cwd_explicit=False,
                project=None,
                workspace=None,
                thread=None,
                agent="--",
                agent_args=["custom-agent", "--flag", "value"],
                mode="develop",
                reviewer="ephemeral",
            )

            with patch(
                "loopweave.cli.PROJECTS_DIR", projects
            ), patch("loopweave.cli.RUNS_DIR", runs), patch(
                "loopweave.cli.VAR_DIR", var
            ), patch(
                "loopweave.cli._registry", return_value=registry
            ), patch(
                "loopweave.cli.discover_thread",
                return_value=SimpleNamespace(
                    thread_id="thread-1",
                    cwd=str(control),
                ),
            ), patch(
                "loopweave.cli.os.getcwd", return_value=str(control)
            ), patch(
                "loopweave.cli.create_terminal_host",
                side_effect=fake_create_host,
            ), patch.object(
                terminal_host_module,
                "default_process_identity_reader",
                return_value=lambda pid: "fake-start",
            ):
                _run_agent(args)

            self.assertTrue(
                captured_command[0] == "custom-agent",
                "command must start with 'custom-agent', got: {}".format(captured_command),
            )
            self.assertNotIn(
                "--",
                captured_command[:1],
                "command must not start with '--'",
            )


if __name__ == "__main__":
    unittest.main()
