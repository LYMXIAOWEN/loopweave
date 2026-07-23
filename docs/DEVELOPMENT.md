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
`terminal_host`. Adding a Windows backend later means implementing one
`TerminalHost` plus Windows-specific providers — not editing the call sites.

Native Windows execution is **not supported** in this package. On a simulated or
real `win32` platform, starting a managed session, delivering a control
message, or checking worker liveness raises `UnsupportedPlatformError` before
any POSIX syscall. Do not describe Windows as working until a ConPTY package
passes its suite on real Windows.

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
