# ADR 0001: Vendor-neutral worker protocol and terminal-host boundary

- Status: accepted (Package 1, Stage 1)
- Date: 2026-07-20

## Context

The baseline binds one managed terminal session (`Supervisor` in
`supervisor.py`) to one worker process using a POSIX PTY, a Unix-domain
control socket, and `os.killpg`-based shutdown. Three problems block the
vendor-neutral, cross-platform goal:

1. `adapters/__init__.get_adapter()` rejects any agent name other than
   `"claude"` with `UnknownAdapter`. `loopweave run codex` fails before a
   process is ever spawned, even though the generic runner
   (`GenericAdapter`) already knows how to launch an arbitrary command.
2. Automatic stage/final/needs-human submission only exists through
   `hook_entry.handle_claude_stop`, which parses a Claude-specific hook
   payload and Claude's transcript JSONL format for `LOOPWEAVE_STAGE` /
   `LOOPWEAVE_FINAL` / `LOOPWEAVE_NEEDS_HUMAN` markers and tool-call
   evidence. A non-Claude worker has no automatic submission path.
3. `cli._run_agent` (`cli.py:972`) constructs `Supervisor` directly and
   calls its `run_foreground()` method (`cli.py:1059`). Out-of-process
   control delivery (`cli._stop_run`, `cli._deliver_review`,
   `cli._finalize_owner_review`, `assignment.assign_task`,
   `completion_notifier.CompletionNotifier`) imports and calls
   `supervisor.send_control_message` directly. Neither has a platform
   seam.

`supervisor.py` also imports `pty`, `termios`, `tty`, `fcntl`
unconditionally, and `bridge_protocol.py` imports `fcntl` unconditionally.
Because `cli.py` imports both transitively, `import loopweave.cli`
already fails on a platform without those POSIX modules — confirmed by
blocking them via a `sys.meta_path` finder. This violates import safety
before any Windows backend exists.

## Decision

### 1. `TerminalHost` interface

A platform-neutral interface in `loopweave/terminal_host.py`, no
POSIX-only imports at module scope:

```python
class TerminalHost(ABC):
    def start(self) -> int: ...                        # spawn, return pid
    def send_input(self, text: str) -> None: ...
    def run_foreground(self) -> int: ...                # raw-mode session; see below
    def resize(self, rows: int, cols: int) -> None: ...
    def stop(self) -> None: ...
    def process_identity(self) -> tuple[int, str]: ...  # (pid, start_fingerprint)
```

`run_foreground()` is part of the interface, not an implementation
detail layered on top of it, because it is the method the real entry
point calls: `cli._run_agent` calls `supervisor.start()` once
(`cli.py:981`) and `supervisor.run_foreground()` once (`cli.py:1059`);
`run_foreground()` owns raw-mode stdin setup, the `SIGWINCH` resize
handler, stdin-to-child forwarding, and blocking wait-for-exit as one
unit (`supervisor.py:141-171`). A backend that implements the other four
methods but not this one cannot back `loopweave run`.

Responsibilities, each independently swappable per platform:

- **Process identity** — `(pid, start_fingerprint)`. POSIX fingerprint
  is `ps -o lstart=` (`process_start_time`), the same value
  `registry.process_identity_matches` already keys on.
- **Input delivery** — `send_input(text)`. POSIX writes to the PTY
  master fd.
- **Foreground session** — `run_foreground()`. POSIX puts the invoking
  terminal into raw mode, installs a `SIGWINCH` handler calling
  `resize()`, forwards stdin, blocks until exit, restores terminal
  attributes.
- **Resize** — `TIOCSWINSZ` via `fcntl.ioctl`, invoked from the
  `SIGWINCH` handler and available standalone.
- **Shutdown** — `os.killpg(SIGTERM)` then `SIGKILL` on timeout.
- **Control transport** — a separate, out-of-process concern; see
  section 2 below. Not folded into the instance-method interface because
  it is keyed by `run_id`/`socket_path` from the registry, not by
  holding a live `TerminalHost` object.

`supervisor.Supervisor` becomes the POSIX implementation **in place** —
same file, same class name, no rename. `run_foreground` already matches
the interface; `Supervisor` gains two additive public methods,
`resize(rows, cols)` and `process_identity()`, delegating to the
existing private `_resize_child_pty` and `process_start_time`
respectively, so `isinstance(host, TerminalHost)` holds. Every existing
caller and test (`tests/test_supervisor.py`, `tests/test_end_to_end.py`)
is unaffected.

The only change inside `supervisor.py` is guarding `import pty`,
`import termios`, `import tty` the same way it already guards
`import fcntl` (`try/except ImportError`, lines 21-24). Only `fcntl` is
currently guarded; the other three are the confirmed cause of
`import loopweave.supervisor` failing when they are unavailable.
Guarding all four makes the module importable everywhere; only
*constructing and starting* a `Supervisor` on a non-POSIX platform fails,
and that path is gated by the factory below.

### 2. Platform selection and control transport

A factory chooses the backend at call time, not import time, and its
`win32` branch runs before the POSIX-only import:

```python
# loopweave/terminal_host.py
import sys

class UnsupportedPlatformError(RuntimeError):
    pass

def create_terminal_host(*args, **kwargs) -> "TerminalHost":
    if sys.platform == "win32":
        try:
            from .windows_terminal_host import WindowsConPtyHost
        except ImportError:
            raise UnsupportedPlatformError(
                "native Windows terminal hosting requires the 'windows' "
                "extra; install with: pip install 'loopweave[windows]'"
            )
        return WindowsConPtyHost(*args, **kwargs)
    from .supervisor import Supervisor
    return Supervisor(*args, **kwargs)
```

`cli._run_agent` calls `create_terminal_host(...)` instead of
constructing `Supervisor(...)` directly — same keyword arguments, same
call site, only the callee changes. This is required, not optional: an
unused factory is not a boundary the product actually has. The Windows
backend (`windows_terminal_host.WindowsConPtyHost`) implements the same
contract with a ConPTY pseudo console (winpty/pywinpty) and a per-run
named-pipe control channel; when the optional `windows` extra is not
installed the factory fails closed with an actionable install hint.

`bridge_protocol.py`'s unconditional `import fcntl` (used for the
advisory `flock` lock in `BridgeProtocol._lock`) is guarded the same way,
moved inside `_lock()` so it raises only when a caller actually takes the
lock, not on module import.

Out-of-process control delivery gets its own narrow seam, since a future
Windows backend using a named pipe instead of a socket would otherwise
require rewriting every one of `assignment.assign_task`,
`CompletionNotifier`, and `cli.py`'s three inline call sites
(`_stop_run`, `_deliver_review`, `_finalize_owner_review`):

```python
# loopweave/terminal_host.py
from typing import Protocol

class ControlSender(Protocol):
    def __call__(self, socket_path, payload: dict, timeout: float = 3.0) -> dict: ...

def default_control_sender() -> "ControlSender":
    if sys.platform == "win32":
        from .control_transport import send_control_message
        return send_control_message
    from .supervisor import send_control_message
    return send_control_message
```

`assign_task` and `CompletionNotifier.__init__` already take a
`sender`/callable parameter with a POSIX-specific default
(`assignment.py:137`, `completion_notifier.py:15`). Their defaults
change to `None`, resolved inside the function/method body — not as a
default-parameter *expression*, which is evaluated once at
function/class-definition time (i.e. at import time) and would raise
`UnsupportedPlatformError` on Windows merely from importing the module.
`assign_task` resolves at call time; `CompletionNotifier` resolves at
construction time.

Every module that resolves a default this way imports `terminal_host`
itself (`from . import terminal_host`) and calls
`terminal_host.default_control_sender()` through the module object —
**not** `from .terminal_host import default_control_sender` bound to a
bare name. A bare-name import binds a reference into the importing
module's own namespace at import time; patching the attribute on
`terminal_host` afterward (`patch.object(terminal_host_module,
"default_control_sender", ...)`, the mechanism every contract test in
this package uses) does not change a name already bound elsewhere —
confirmed directly: a `from X import Y` consumer keeps calling the
original `Y` even after `patch.object(X, "Y", ...)` runs, because the
consumer's `Y` and `X`'s `Y` are different bindings after the `from`
import completes. Qualified access (`terminal_host.default_control_sender()`)
looks up the attribute on `terminal_host` at call time, so the same
patch reaches every caller. `cli.py`'s three direct call sites change
from the module-scope-imported `send_control_message` symbol to
`terminal_host.default_control_sender()` invoked once per call site,
same qualified-access rule. On POSIX this is mechanical and
non-behavioral: `default_control_sender()` returns exactly today's
`send_control_message`.

### 2a. Platform-neutral process-identity provider

`TerminalHost.process_identity()` covers identity for a live instance
the caller is already holding, but every out-of-process liveness check —
run before a `TerminalHost` object exists, keyed only by a stored
`(pid, agent_process_start)` pair from the registry — imports and calls
`supervisor.process_start_time` directly:
`cli.py:65-68,506-508,653-659,799-805,995,1082-1087`,
`assignment.py:13,148`, `bridge_control.py:26,183`,
`completion_notifier.py:9,16`, `dispatcher.py:14,54,259`,
`thread_takeover.py:12,76`. Declaring `process_identity()` on
`TerminalHost` without also giving these ten call sites a platform-
neutral seam would leave the actual liveness-check code path exactly as
POSIX-coupled as `send_control_message` was before section 2's
`ControlSender` — a future Windows host would still require rewriting
every one of them around a POSIX `ps` implementation, the exact outcome
the package brief prohibits.

The fix mirrors `ControlSender` exactly, because the shape of the
problem is identical — a POSIX-only free function imported at module
scope by many otherwise-portable callers:

```python
# loopweave/terminal_host.py
from typing import Protocol

class ProcessIdentityReader(Protocol):
    def __call__(self, pid: int) -> str: ...

def default_process_identity_reader() -> "ProcessIdentityReader":
    if sys.platform == "win32":
        from .windows_terminal_host import process_start_time
        return process_start_time
    from .supervisor import process_start_time
    return process_start_time
```

Every call site above takes (or gains) an injectable
`process_start`/`process_start_time`/`process_start_reader` parameter
resolved the same way `sender` is resolved in section 2 — `None` default,
resolved lazily at call or construction time via qualified access
(`terminal_host.default_process_identity_reader()`, importing
`terminal_host` itself rather than binding the function name directly —
same rule as section 2, same reason: a bare-name import is not patchable
through `patch.object(terminal_host_module, ...)`), never as a
default-parameter expression. Several already take this parameter for
test injection (`completion_notifier.py:16`, `thread_takeover.py:76`,
`assignment.py`'s `process_start_reader` parameter on `assign_task`); for
those, only the *default value* changes, from the direct
`supervisor.process_start_time` import to the lazy-resolved provider —
callers that already pass an explicit reader are unaffected. The bare
call sites with no existing parameter (`cli.py`'s five internal call
sites, `bridge_control.py:183`, `dispatcher.py:54,259`) are changed to
call `terminal_host.default_process_identity_reader()` once and invoke
the result, the same mechanical pattern as section 2's `cli.py`
control-sender call sites. `submission.py`'s stale-identity check
(section 3 below) uses the same provider rather than importing
`process_start_time` directly, so it is included in this same fix, not
exempted from it.

On POSIX this is mechanical and non-behavioral:
`default_process_identity_reader()` returns exactly today's
`process_start_time`. On `win32` it returns the Windows implementation
(Win32 `GetProcessTimes` creation-time fingerprint, never `ps`); the
ten call sites above stay unchanged because they already go through
`terminal_host.default_process_identity_reader()`. The Windows process
probe must never use `os.kill(pid, 0)`: signal 0 is `CTRL_C_EVENT` on
Windows and would broadcast Ctrl+C to the console, so `pid_alive()` uses
`OpenProcess` instead.

### 3. Vendor-neutral worker submission service

Automatic submission today only exists through
`hook_entry.handle_claude_stop`, which parses Claude's hook payload and
transcript JSONL shape. A worker in any other language cannot use it.
Per invariant 3, hooks are optional translators into the protocol, not
the only door in:

```python
# loopweave/submission.py
def submit_stage(run_id: str, summary: str, *, evidence: dict) -> str: ...
def submit_final(run_id: str, summary: str, *, evidence: dict) -> str: ...
def submit_needs_human(run_id: str, message: str) -> str: ...
```

`handle_claude_stop` becomes a translator calling these instead of
duplicating dispatch logic inline. No second state machine is
introduced (invariant 8): `submission.py` is a thin front door onto the
existing `review-request.json` → `_dispatch_and_deliver` →
`reviewer-verdict.json` → `_deliver_review` pipeline `hook_entry.py` and the
manual `request-review` command already share.

### 4. Evidence bounds

`validate_review_request` (`protocol.py:111-153`) enforces shape (fields
present, lists are lists) but no size. The only existing size bound,
`hook_entry._bound_review_text` (`MAX_VISIBLE_SUMMARY_BYTES = 12 * 1024`),
applies only to the visible-review-card `work_summary` field, never to
the ephemeral `review-request.json` path, and never to evidence lists.
`submission.py` defines its own bounds, checked before
`validate_review_request` runs:

```python
# loopweave/submission.py
MAX_SUMMARY_BYTES = 12 * 1024          # matches hook_entry.MAX_VISIBLE_SUMMARY_BYTES
MAX_MESSAGE_BYTES = 12 * 1024          # needs_human message, same bound
MAX_EVIDENCE_ITEMS_PER_FIELD = 200     # files_changed / commands_run / tests / etc.
MAX_EVIDENCE_ITEM_BYTES = 4 * 1024     # each individual list entry
MAX_EVIDENCE_TOTAL_BYTES = 64 * 1024   # whole evidence dict, canonical encoding below

class SubmissionError(RuntimeError):
    pass

class EvidenceTooLarge(SubmissionError):
    pass

def validate_bounded_text(text: str, max_bytes: int, field_name: str) -> None:
    """Raise SubmissionError if len(text.encode('utf-8')) > max_bytes."""

def validate_evidence(evidence: dict) -> dict:
    """Validate an already-decoded evidence dict: reject unknown keys,
    non-string items, and any of the three limits above. Return it
    unchanged if valid. Exposed standalone so tests, and submit_stage/
    submit_final internally, can call it directly."""

def load_evidence_file(path) -> dict:
    """Read, JSON-decode, and validate_evidence() the file at `path`.
    Raises SubmissionError naming `path` if it is not valid JSON or not
    a JSON object."""
```

Enforcement is **reject, not silently truncate**. `loopweave submit`'s
input is a file the caller wrote and can fix, unlike
`hook_entry._bound_review_text`'s transcript-derived text, which cannot
be re-edited — so failing closed matches invariant 6. Unknown evidence
keys are rejected, not silently dropped, so a typo in `--evidence-file`
fails loudly rather than quietly submitting empty evidence.

The canonical encoding for `MAX_EVIDENCE_TOTAL_BYTES` is
`json.dumps(evidence, ensure_ascii=False, sort_keys=True,
separators=(",", ":")).encode("utf-8")` — the same three arguments
`protocol.append_event` and `visible_review._encoded_size` already use.
`ensure_ascii=False` matters: it means the byte cost of a non-ASCII
character is its real UTF-8 encoding (1-4 bytes), not an inflated
`\uXXXX` escape, so the bound reflects actual bytes occupied.

The three caps are independent backstops, not a set of numbers whose
worst-case product is guaranteed to fit together: 200 items at 4 KiB
each is 800 KiB, well over the 64 KiB total — a submission maxing out
per-item size *and* per-field count simultaneously is expected to be
rejected by the total cap, which is that cap doing its job. A single
whole-payload cap with no per-item or per-field limit was rejected as
insufficient: one oversized entry could starve every other field, with
no defense against many-small-items abuse (`visible_review.py`'s own
`_compact_review_card` already needed round-robin per-field logic for
this reason).

This package does not add a secret-scanning mechanism — none exists
anywhere in the codebase today, and adding one is outside this package's
scope. What is reused is the existing non-transmission boundary that
already protects the control token: `render_status_json`/
`render_runs_json` (`cli.py:380-399`) omit `control_token` from every
machine-readable payload, with an existing passing test
(`tests/test_cli.py::test_render_status_json_emits_only_required_fields_and_hides_secret`).
`submission.py` introduces no new field that could carry a credential —
evidence's five fields are worker-supplied paths and shell commands, the
same shape `hook_entry.py` already writes from transcript data today.

### 5. Run identity

The Claude path gets its `run_id` from an explicit `--run-id` flag baked
into the Stop hook command. A generic worker has no settings file to
inject a flag into, so the run id is passed as an environment variable
at spawn time, `LOOPWEAVE_RUN_ID`, set by `Supervisor` in
`subprocess.Popen`. This is bounded and non-secret — a `run-<12 hex
chars>` token already visible in `loopweave status` output and `runs/`
directory names. The control token, the actual secret authenticating
control-socket writes, is **not** passed to the child; `loopweave submit`
reads it from the registry keyed by `LOOPWEAVE_RUN_ID`, the same way
`loopweave request-review --run-id` does today, scoped by the existing
`0o600`/`0o700` permissions on `registry.sqlite` and the run directory.

The legacy `--run-id` flag on `loopweave hook claude-stop` is unchanged.
`loopweave submit` also accepts an explicit `--run-id`, falling back to
`$LOOPWEAVE_RUN_ID` when omitted.

### 6. `loopweave submit` CLI contract

```bash
loopweave submit --stage --run-id <id> --summary-file <path> [--evidence-file <path>]
loopweave submit --final --run-id <id> --summary-file <path> [--evidence-file <path>]
loopweave submit --needs-human --run-id <id> --message-file <path>
```

- `--run-id` optional, defaults to `$LOOPWEAVE_RUN_ID`.
- `--summary-file` / `--message-file` follow the existing `finalize
  --message-file` convention (`cli._finalize_message`): UTF-8 text,
  bounds enforced per section 4.
- `--evidence-file` is optional JSON matching
  `{"files_changed": [...], "commands_run": [...], "tests": [...],
  "known_issues": [...], "questions_for_reviewer": [...]}`, decoded and
  validated by `load_evidence_file()` before `submit_stage`/
  `submit_final` runs. A malformed file raises `SubmissionError` naming
  the path; `main()`'s existing exception handler (`cli.py:1425-1436`,
  which already catches `RuntimeError` and prints `"loopweave: {error}"`
  with exit code 2) reports it with no new error-handling path. Omitted
  lists default to `[]`.
- `--stage`/`--final`/`--needs-human` are mutually exclusive, mirroring
  the existing `request-review --stage`/`--final` group.
- `submit --stage`/`--final` call `_dispatch_and_deliver` or the
  visible-card queue path depending on the run's `reviewer_backend` —
  the same branch `handle_claude_stop` already takes. `submit
  --needs-human` calls `registry.force_state(run_id,
  RunState.NEEDS_HUMAN)`, the same transition `_requests_human` triggers
  today.

### 7. Adapter resolution: generic fallback instead of rejection

```python
def get_adapter(name, extra_args, run_id, workspace_root=None, baseline_path=None):
    if name == "claude":
        return ClaudeAdapter(...)
    return GenericAdapter([name, *extra_args])
```

`loopweave run codex`, `loopweave run opencode`, `loopweave run kimi`
each launch that literal executable, identically to today's `loopweave
run -- codex` spelling. `UnknownAdapter` is removed — nothing can trigger
it once every name falls through to `GenericAdapter`. `--` still works
for names colliding with a parser option. Only `claude` keeps a named
adapter, documented as the one optional enhanced adapter, not a gate
other agents must pass.

### 8. Claude Stop Hook as translator, not gate

No behavior change to `ClaudeAdapter` or `handle_claude_stop`'s
detection logic. Its tail — after computing `completion_scope`,
`evidence`, and the human-request check — calls `submission.py`'s
functions instead of duplicating dispatch logic inline. This is how
"Claude may remain an optional enhanced adapter" becomes concretely
true: Claude gets automatic completion detection via transcript parsing,
but a worker without that hook reaches the identical state machine via
`loopweave submit`.

### 9. Public identity policy

The public surface uses LoopWeave identifiers exclusively:

- The importable package and console script are both named `loopweave`.
- Environment variables and protocol markers use the `LOOPWEAVE_` prefix.
- No historical product-name aliases are registered or accepted.
- `docs/DEVELOPMENT.md` documents this identity boundary for contributors.

## Acceptance matrix

| Requirement | Proof mechanism |
|---|---|
| `TerminalHost` covers the real foreground lifecycle | `isinstance(host, TerminalHost)`; `run_foreground()` exit-code parity on the non-TTY branch; `resize()`/`process_identity()` checked against observable PTY-resize events and the live PID/fingerprint, not `hasattr` |
| `_run_agent` cannot bypass the platform boundary | Test patches `cli.create_terminal_host` and asserts it is called; a `win32`-simulated invocation (with the `windows` extra present) drives `WindowsConPtyHost` and a named-pipe control endpoint and proves `Supervisor` is never constructed; without the extra it asserts the actionable `UnsupportedPlatformError` |
| Control delivery has no direct POSIX dependency | `assignment.py`/`completion_notifier.py` import cleanly under a blocked-`pty` platform; `assign_task`/`CompletionNotifier` resolve the default sender only at call/construction time, not import time; `cli._stop_run`/`_deliver_review`/`_finalize_owner_review` each independently proven to consult `default_control_sender()` |
| Process-identity lookup has no direct POSIX dependency | `cli.py`/`assignment.py`/`bridge_control.py`/`completion_notifier.py`/`dispatcher.py`/`thread_takeover.py` import cleanly under a blocked-`pty` platform — a necessary but not sufficient check by itself, since a module could still bind `process_start_time` directly and import cleanly once `supervisor.py`'s own imports are guarded. Each of the ten call sites named in section 2a is *additionally* proven, with a runtime fixture (not an import probe), to actually invoke `default_process_identity_reader()` when called: `cli._stop_run`/`_deliver_review`/`_finalize_owner_review`/`_run_agent`/`_reconcile_run_liveness`, `assignment.assign_task`, `completion_notifier.CompletionNotifier`, `dispatcher.inspect_dispatch_lease`, `thread_takeover.ThreadTakeoverCoordinator.attach`, and `bridge_control.BridgeController.reconcile_stale_pending_reviews` each have a dedicated runtime-consultation test — import-cleanliness alone is never treated as proof of migration |
| Evidence bounds are real on both the validator and every public entry point | `validate_bounded_text`/`validate_evidence` tested in isolation for every rejection and boundary case, including UTF-8 multibyte inputs where character count and byte count diverge; `submit_stage`, `submit_final`, and `submit_needs_human` are *each* independently exercised with a multibyte-boundary value (character count under the limit, UTF-8 byte count over it), not only ASCII, so a public function that counts characters instead of bytes cannot pass by coincidence |
| `--evidence-file` is rejected before submission, not after | `load_evidence_file` tested directly on malformed JSON and non-object JSON; a `cli.main()` invocation with `submit_stage` patched out asserts the mock is never called and the file path appears in stderr |
| `--final` routes through the same service as `--stage`/`--needs-human` | A `cli.main(["submit", "--final", ...])` invocation with `submit_final` patched asserts it is called with the summary and evidence, mirroring the existing `--stage`/`--needs-human` `main()` tests — `--final` is not left as the only scope untested through the real entry point |
| `$LOOPWEAVE_RUN_ID` fallback is proven through `main()`, not reimplemented in the test | A `cli.main()` invocation with no `--run-id` and `$LOOPWEAVE_RUN_ID` set asserts the patched submission service receives that value as its `run_id` argument — not a test that computes `args.run_id or os.environ.get(...)` itself and checks Python's `or` operator |
| The control token never reaches the child process environment | A test asserts the literal control-token value, and the string `"CONTROL_TOKEN"` (or any control-token-named key), are absent from the actual environment observed inside the spawned child — not only that `LOOPWEAVE_RUN_ID` is present and correct |
| `codex`/`opencode`/`kimi`/arbitrary names are generic workers | `get_adapter` returns a `GenericAdapter` for every non-`claude` name; `UnknownAdapter` is unreachable |
| POSIX baseline unaffected | Full existing suite passes unchanged; `Supervisor`'s public surface only gains methods, no removals or signature changes |

## Consequences

- A Windows checkout can `import loopweave.cli`, `assignment`,
  `bridge_control`, `completion_notifier`, `dispatcher`, and
  `thread_takeover` without touching `pty`/`termios`/`fcntl`. With the
  optional `windows` extra installed it can run a managed ConPTY session,
  deliver control messages over a named pipe, and check process liveness
  with Win32 APIs; without the extra it fails closed with an actionable
  `UnsupportedPlatformError` before any POSIX syscall — never a silent
  fallback to an untested implementation.
- `codex`, `opencode`, `kimi`, and any other executable name become
  valid `loopweave run <name>` invocations without per-agent adapter code.
- A non-Claude, non-interactive worker can complete a full
  stage → review → verdict → resume cycle using only `loopweave submit`,
  with no transcript, no hook, and no Claude dependency, and cannot pass
  unbounded evidence through that path.
- Removing `cli.py`'s direct `process_start_time`/`send_control_message`
  imports means every existing baseline test that patches
  `loopweave.cli.process_start_time` or
  `loopweave.cli.send_control_message` directly (`test_cli.py`, 12
  patch sites across 10 distinct tests, counted directly against the
  Stage 1 baseline) breaks with `AttributeError` once that import is
  gone. This is a necessary, expected consequence of actually removing
  the coupling reviewer's finding identified, not an oversight — Stage 2
  updates those tests to patch `terminal_host.default_control_sender`/
  `terminal_host.default_process_identity_reader` instead, the same
  target this package's own contract tests already use. Stage 2's
  report should call out this test-file change explicitly rather than
  letting it look like an unrelated diff.

## Rejected alternatives

- **Branch on `sys.platform` inline inside `Supervisor` instead of a
  factory.** Interleaves POSIX and future Windows code in one file and
  removes the single seam a factory gives for swapping backends.
- **Ship a Windows backend now using `pywinpty` or similar.** Originally
  deferred to a future package with real Windows CI; adopted in this
  change as an optional extra (`loopweave[windows]`) with a native
  Windows test suite and acceptance matrix. The default install keeps
  zero Windows-only dependencies.
- **Keep `get_adapter` rejecting unknown names, require `run --
  <command>` for everything.** The package brief's acceptance criteria
  require `loopweave run codex` (no `--`) to work; the `--` spelling
  still works for names colliding with parser options.
- **Wrap `loopweave submit` around `hook claude-stop` with a synthesized
  payload.** Forces every non-Claude worker's evidence through a
  Claude-transcript-shaped adapter and keeps the transcript parser as
  the de facto only door, contradicting invariant 3.
- **Leave `create_terminal_host()` available but not required at the
  entry point.** An unused seam is not a boundary the product has.
- **A single whole-payload evidence cap with no per-item/per-field
  limit.** Insufficient against one oversized entry or many small ones;
  see section 4.
- **Fold process-identity lookup into `TerminalHost.process_identity()`
  and require every liveness check to hold a live `TerminalHost`
  instance.** Rejected: the ten call sites in section 2a run
  out-of-process, often long after the `TerminalHost` that started the
  session has gone out of scope in a different `loopweave` invocation —
  they only have a stored `(pid, agent_process_start)` pair from the
  registry, never a live object to call a method on. A free-function
  provider, resolved the same way as `ControlSender`, matches how these
  call sites are actually shaped.
