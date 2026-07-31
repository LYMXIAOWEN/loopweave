# Development

## Bootstrap

```bash
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -e '.[dev]'
pytest
```

The test configuration limits discovery to `tests/` and adds `src/` to the
import path, so unrelated directories cannot be collected accidentally.

## Runtime isolation

For local manual testing, keep state outside the repository when practical:

```bash
export LOOPWEAVE_HOME="$PWD/.local-runtime"
loopweave status
```

Never reuse or import runtime state from the original local prototype as public
fixtures. Build synthetic runs in temporary directories instead.

## Public identity

The public package, CLI, protocol markers, and environment variables use the
LoopWeave identity exclusively:

- Python package: `loopweave`
- CLI entry point: `loopweave`
- protocol and environment prefix: `LOOPWEAVE_`

Do not add historical product names or compatibility aliases to the public
tree.

## Platform boundary and Windows

The core imports cleanly on a non-POSIX platform because POSIX-only modules
(`pty`, `termios`, `tty`, `fcntl`) are guarded or resolved lazily through
`terminal_host`. The Windows backend implements the same `TerminalHost`
contract plus platform providers — the call sites are shared:

- `windows_terminal_host.WindowsConPtyHost` — ConPTY managed session via
  `pywinpty` (import name `winpty`, `Backend.ConPTY`). Requires the optional
  `windows` extra; without it the factory raises an actionable
  `UnsupportedPlatformError` instead of pretending to work.
- `control_transport` — control endpoints: Unix domain socket on POSIX,
  per-run named pipe (`\\.\pipe\loopweave-control-{run_id}-...`) on Windows.
  The registry/`run.json` `socket_path` field stores the endpoint string on
  both platforms, so no database migration is needed.
- `file_lock` — blocking/non-blocking exclusive locks (`fcntl.flock` on
  POSIX, `msvcrt.locking` on Windows).
- `win32_pipe` — minimal ctypes named-pipe client/server used by the control
  channel and by `desktop_ipc` for `\\.\pipe\codex-ipc`.

Windows notes for developers:

- `os.kill(pid, 0)` is **unsafe on Windows** (signal 0 is `CTRL_C_EVENT` and
  broadcasts Ctrl+C); use `terminal_host.pid_alive()` instead.
- `os.ttyname` and `os.getuid` do not exist on Windows; platform-guard them.
- text-mode `Path.write_text()`/`read_text()` default to the locale encoding
  on Windows; pass `encoding="utf-8"` (and `newline="\n"` when exact bytes
  matter).
- POSIX `with sqlite3.connect(...)` never closes the handle; Windows keeps the
  database file locked until close, so registry connections use a closing
  connection factory.
- Windows tests that need real ConPTY/named pipes are gated with
  `unittest.skipUnless(sys.platform == "win32", ...)`; POSIX-only tests
  (`pty`, `AF_UNIX`, permission-bit assertions, `/opt` bundle paths) are
  skipped on win32 and still run unchanged on POSIX CI.

Do not describe Windows as supported until the native acceptance matrix
passes on a real Windows machine.

## Generic-worker launch, assignment, and recovery

```bash
loopweave run codex --task-file task.md             # atomic launch + assign
loopweave run codex                                  # launch; assign later
loopweave assign --run-id <id> --task-file task.md  # two-step assignment
loopweave status --json <id>   # task_assignment: assigned / awaiting task assignment (JSON only)
loopweave recover <id>         # audited recovery of a verified live false-positive orphan
```

`--task-file` ordering: the managed child is started first, then the run record
and the exact run-scoped task packet are installed, and only then is the
assignment delivered to the child (the readiness signal it consumes); the
packet therefore exists before the worker can act on the assignment.

Liveness and orphan transitions are audited and centralized in
`loopweave/liveness.py` (see ADR 0002). A healthy worker should never need
a direct database edit: `status`/`recover` reconcile and recover verified live
sessions; `runs` stays list-only and does not probe historical or terminal
runs. The live acceptance procedure is documented in
`docs/INTERACTIVE_TEST_FINDINGS.md`.

## Testing the boundary

The contract suite in `tests/test_stage2_contracts.py` gates the boundary and
the submission service, and `tests/test_end_to_end.py` proves a generic fixture
worker completes a stage → review → resume → final cycle without Claude. Run:

```bash
PYTHONDONTWRITEBYTECODE=1 python3 -B -m pytest -q
ruff check src tests plugins/loopweave-visible-bridge/server
PYTHONPYCACHEPREFIX=/tmp/loopweave-pycache python3 -m compileall -q \
  src tests plugins/loopweave-visible-bridge/server
git diff --check
```

## Release gate

Do not publish a supported release until all of the following are true:

- the license is selected;
- macOS and native Windows acceptance suites pass;
- arbitrary CLI workers can use the vendor-neutral submission protocol;
- secret, identity, absolute-path, and generated-artifact scans are clean;
- installation and uninstall/stop paths are documented and verified.
