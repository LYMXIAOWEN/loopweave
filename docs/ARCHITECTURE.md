# Architecture

## Product boundary

LoopWeave coordinates an explicitly selected terminal session, a worker running
inside that session, and a reviewer. It does not select agents by brand and must
not inject input into a terminal chosen only by process-name heuristics.

## Target architecture

```text
Host task binding
    -> TerminalHost (platform-neutral boundary)
        -> POSIX PTY backend (macOS and Linux, today)
        -> Windows ConPTY backend (winpty/pywinpty, Windows 10 1809+)
    -> ControlTransport (platform-neutral control channel)
        -> POSIX Unix domain socket
        -> Windows named pipe (`\\.\pipe\loopweave-control-{run_id}-...`)
    -> WorkerProtocol
        -> assign
        -> submit stage / final / needs-human
    -> ReviewerBackend
        -> visible review
        -> isolated review
    -> verdict delivery to the same TerminalHost session
```

Any foreground program that can interact through the terminal may be a worker.
Agent-specific adapters may improve completion detection or input behavior, but
they must never become a core allowlist.

## Platform boundary

`loopweave/terminal_host.py` defines the platform-neutral boundary:

- `TerminalHost` declares the lifecycle the entry point actually drives:
  `start`, `send_input`, `run_foreground`, `resize`, `stop`, `process_identity`.
  `run_foreground` is part of the interface because `loopweave run` calls it.
- `create_terminal_host(...)` selects the backend at call time:
  `darwin`/`linux` return the POSIX `Supervisor`; `win32` returns
  `WindowsConPtyHost` (which raises `UnsupportedPlatformError` with an
  actionable install hint when the optional `windows` extra is missing).
- Out-of-process concerns that key on a stored `(pid, start_fingerprint)` pair
  rather than a live host object go through two lazily-resolved providers,
  `default_control_sender()` and `default_process_identity_reader()`, which
  resolve to the platform transport (Unix socket sender and `ps` lookup on
  POSIX; named-pipe sender and Win32 `GetProcessTimes` fingerprint on
  Windows). Every caller resolves them at call/construction time through the
  `terminal_host` module object, never a bare-name import.

`supervisor.Supervisor` is the POSIX backend in place: it formally implements
`TerminalHost` and gained additive `resize()`/`process_identity()` methods.
`windows_terminal_host.WindowsConPtyHost` implements the same contract on
Windows with a ConPTY pseudo console; the control channel is a per-run named
pipe served by the same newline-delimited JSON protocol. Cross-platform file
locks (`file_lock.py`) and control endpoints (`control_transport.py`) keep the
POSIX behavior unchanged while Windows runs the same state machines.

## Vendor-neutral worker submission

`loopweave/submission.py` is the single front door workers use to report
completion from inside a managed session:

- `submit_stage` / `submit_final` / `submit_needs_human` reuse the existing
  review-request, dispatch, and visible-card state machine — there is no
  parallel loop.
- Every scope shares one guard (`_assert_submittable`): the run must be in an
  assignable worker state (`running`/`worker_continuing`) with a live,
  matching process identity. Terminal, owner-pending, in-flight-review, and
  queued (`ready_for_review`) runs are rejected, so a stray submission cannot
  resurrect a finished run or overwrite a pending human review.
- Evidence is bounded (summary/message and per-item/aggregate byte caps) and
  rejected, not silently truncated. The Claude Stop hook bounds
  transcript-derived evidence to these limits before delegating, so a worker
  that cannot rewrite its transcript still submits successfully.
- A failed dispatch rolls the run back only when it never advanced past the
  state the submission set; a dispatch that already reached
  `reviewing`/`review_ready`/`orphaned` leaves that authoritative state
  standing. Retry redispatches the existing request without overwriting it.
- The managed child receives a bounded, non-secret `LOOPWEAVE_RUN_ID` at spawn;
  the control token authenticating socket writes is never passed to the child.

## Audited liveness, recovery, and run-scoped task provenance

`loopweave/liveness.py` (ADR 0002) owns worker liveness, the orphan
transition, and recovery:

- **Authenticated liveness decision.** `authenticated_identity_check` is the
  single decision every orphan route consults (`assign_task`,
  `_deliver_review`, `ThreadTakeoverCoordinator.attach`,
  `bridge_control.reconcile_stale_pending_reviews`, and the status/runs
  reconcile). A transient identity-read failure does **not** orphan a session
  the authenticated control channel proves alive (it records a bounded
  `liveness_probe_passed` audit and proceeds); a genuine missing/reused
  process, wrong token/socket, or mismatched run/PID stays fail-closed.
- **Centralized, audited orphans.** `audit_orphan` is the only path to
  `RunState.ORPHANED` (enforced by an AST guard test). It derives `prior_state`
  from the authoritative registry row at transition time, writes durable
  `orphan-provenance.json` (with a `transition_id`) **before** the database
  change, and appends a bounded, schema-constrained, non-secret `run_orphaned`
  event last. If the authoritative state read fails, the transition aborts.
- **Safe, idempotent recovery.** `recover_orphaned` restores a verified
  false-positive orphan only for an exact live assignable session, refusing
  terminal/in-flight prior states and any unauthenticated/mismatched control
  status. It correlates the orphan/recovery journal to the provenance by
  `transition_id` so retries and separate orphan cycles never duplicate or
  suppress records, and it completes a restored run whose recovery journal is
  unfinished. `loopweave status <run-id>` and `loopweave recover <run-id>` run
  this path; `loopweave runs` is list-only and never probes or mutates the
  archive.
- **Run-scoped task provenance.** `loopweave run <agent> --task-file <path>`
  installs the exact, byte-identical `assigned-task-latest.md` and delivers the
  assignment after the run record and packet are installed (the child is started
  first; the assignment is the readiness signal). A taskless
  run reports `awaiting task assignment`; a premature `submit` returns
  actionable guidance without mutating review state. Same-task retry is
  idempotent; a different task is a conflict.

## Current baseline

The repository contains:

- a POSIX PTY supervisor and local control socket behind the `TerminalHost`
  boundary;
- run state persisted in SQLite and bounded JSON artifacts;
- project/workspace binding and dirty-worktree baseline capture;
- staged and final review states;
- a generic command runner (any executable name resolves to a generic worker);
- a vendor-neutral submission service and `loopweave submit` CLI;
- a Claude Stop-hook adapter that translates its transcript into the shared
  submission service;
- isolated and visible Codex review backends;
- an optional Codex Desktop visible-review plugin.

Remaining architecture debt for later packages:

- a Windows ConPTY backend (Windows 10 1809+ / Windows 11, optional
  `loopweave[windows]` extra; without the extra the platform fails closed with
  an actionable install hint);
- Codex Desktop implementation details inside the review host layer;
- runtime-state placement coupled to a source checkout by default.

## Non-negotiable invariants

- One binding identifies one exact terminal session.
- Ambiguous or stale identity fails closed.
- Review evidence is bounded and does not include unrestricted terminal logs.
- Review delivery returns to the same managed session.
- Hidden replacement workers or reviewers are not created silently.
- Runtime state and user workspaces are never committed to this repository.
