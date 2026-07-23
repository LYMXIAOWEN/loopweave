# ADR 0002: Audited liveness, safe orphan recovery, and run-scoped task provenance

- Status: accepted (Package 1 live reliability follow-up)
- Date: 2026-07-22
- Follows: ADR 0001 (vendor-neutral worker protocol)

## Context

The first real `loopweave run codex` test (see
`docs/INTERACTIVE_TEST_FINDINGS.md`) found two confirmed defects:

- **LW-IT-001.** A live managed Codex run (`run-live-example`, redacted) was
  transitioned to `orphaned` before its first submission, even though the
  recorded process start time, and the authenticated control-socket status all
  matched afterward. A healthy worker could not submit, attach, or receive
  review until the row was hand-edited back to `running`.
- **LW-IT-003.** A direct `loopweave run codex` cannot queue its first
  visible-review card: the submission path always resolves the task packet to
  `runs/<run-id>/assigned-task-latest.md`, but a direct `run` never creates
  that file (only the separate legacy `assign` flow does), so
  `validate_review_card` rejects with `task_packet_path does not exist`.

LW-IT-002 (an unsent composer line) is a validation gate, not a confirmed bug:
the manual probe omitted the trailing `"\r"` that the production
review-delivery path already sends. It is left unconfirmed unless the full
production path reproduces it.

## Root-cause analysis (LW-IT-001)

Tracing every production transition to `RunState.ORPHANED` shows one shared
defect shape. The normal liveness reconcile used by `loopweave status`/`runs`
(`cli._reconcile_run_liveness`) is:

```python
try:
    current_start = <identity reader>(candidate.agent_pid)
except Exception:                       # ANY reader failure
    registry.force_state(..., ORPHANED) # irreversible, unaudited
    return
if not registry.process_identity_matches(...):
    registry.force_state(..., ORPHANED) # irreversible, unaudited
```

The `except Exception` swallows **any** identity-read failure (a transient
`ps` error, a permission glitch, a short race) and irreversibly orphans the
run, with no audit record and no recovery. Every sibling orphan site
(`_deliver_review`, `_finalize_owner_review`, `assign_task`,
`thread_takeover.attach`, `bridge_control.reconcile_stale_pending_reviews`)
shares this shape.

Because the observed run's recorded identity and authenticated control status
matched afterward, the orphan was not justified by a genuinely dead or
PID-reused process; it is consistent with a transient identity-read failure
flipping an irreversible, unrecoverable switch. We cannot prove which
transient cause fired (no audit existed), so the fix targets the whole defect
class: an orphan may only follow an **authenticated** liveness verdict, every
transition is audited, and a verified false positive is recoverable.

## Decision

### 1. Centralized, audited orphan transitions

A new `loopweave/liveness.py` owns every orphan and recovery transition
through two helpers:

- `audit_orphan(registry, run, *, source, reason_category)`:
  `force_state(ORPHANED)` and append a bounded event
  `{"event": "run_orphaned", "source", "reason_category", "prior_state",
  "run_id", "timestamp"}`. **The `prior_state` is derived authoritatively from
  the registry at transition time — callers may not supply or forge it.** It is
  durably retained in `runs/<run-id>/orphan-provenance.json` (the authoritative
  provenance recovery consumes). Reason categories are a fixed enum
  (`identity_mismatch`, `control_unreachable`, `control_unauthenticated`,
  `pid_reused`, `child_exited`, `run_mismatch`), identical to the enum in
  section 6. The control token, environment secrets, and unbounded exception
  text are never logged.
- `recover_orphaned(...)`: the inverse, appending `run_recovered` with the
  same shape plus `restored_state`, and clearing the provenance record.

All existing `force_state(..., ORPHANED)` sites route through `audit_orphan`.
The exhaustive contract is enforced by an AST guard test: **no production
module outside `liveness.py` may call `force_state` with `RunState.ORPHANED`
directly** — `liveness.audit_orphan` is the single chokepoint.

### 2. Authenticated liveness verdict

`probe_control_liveness(run)` opens an authenticated control `status` request
(`{token, action: status}`) to `run.socket_path` and classifies the reply:

| Condition | Verdict |
|---|---|
| connect refused / timeout | `control_unreachable` (not alive) |
| `{status: error}` (bad token) | `control_unauthenticated` (not alive) |
| `run_id` != recorded | not alive (wrong run) |
| `pid` != recorded `agent_pid` | `pid_reused` (not alive) |
| `running: false` | `child_exited` (not alive) |
| all match + `running: true` | **alive** (authenticated proof) |

`reconcile_liveness` decides from the identity reader **and** this probe:

- reader OK and matches recorded → alive, no action;
- reader **raises** (transient candidate — no value was returned) → probe; if
  the probe is **alive**, do **not** orphan (record `liveness_probe_passed`); if
  the probe is not alive, `audit_orphan` with the probe's reason;
- reader **returns a mismatch** (a value came back but differs from the
  recorded start time) → **fail-closed `audit_orphan(identity_mismatch)`,
  even if the control probe otherwise reports alive.** A start-time mismatch
  is a reuse/tamper signal that an alive socket cannot override — the recorded
  process and the live listener would not be the same process.

A genuine missing/reused process, wrong token/socket, mismatched run/PID, or
dead child therefore remains fail-closed.

### 3. Safe recovery for false-positive orphans

`recover_orphaned(run)` restores an already-`orphaned` run **only** when, in
addition to the probe being alive, the recorded identity re-reads exactly
(`agent_pid` + `agent_process_start` match) and the **authoritative provenance
record** (`orphan-provenance.json`, written by `audit_orphan`) names an
**assignable** prior state (`running`/`worker_continuing`). It restores that
prior state and audits `run_recovered`, then clears the provenance record. It
refuses to act for a terminal prior state, a reused PID (reader mismatch), an
unauthenticated/unreachable socket, a mismatched returned run/PID, a dead
child (`running: false`), or a run orphaned from an in-flight/pending review
state. It is **idempotent**: it does not only accept an `orphaned` run — if the
run is already restored but a provenance record still lingers (a recovery that
crashed after `force_state(restored)` but before the `run_recovered` audit/clear),
`recover_orphaned` completes the journal (appends the missing `run_recovered`)
and clears the provenance, converging to one `run_orphaned`, one `run_recovered`,
and no provenance file.

### 4. Recovery in the normal workflow

Recovery is bounded and never touches the historical archive:

- `loopweave runs` is **list-only**. It reconciles liveness for assignable
  runs but does **not** probe the control channel of, attempt to recover, or
  mutate terminal/orphaned history. Listing a large archive stays bounded.
- `loopweave status <run-id>` reconciles the single selected run and runs the
  recovery/reconcile path: it recovers a verified live false-positive orphan,
  and it completes a restored run whose recovery journal is unfinished
  (lingering `orphan-provenance.json`) — no SQLite edit.
- `loopweave recover <run-id>` is the explicit, audited, gated operator command
  for the same recovery (it is never a raw state edit).

A healthy worker therefore recovers through `status`/`recover` without direct
SQLite edits, while `runs` never sweeps the archive. Recovery is refused for
**every** in-flight or pending-review prior state — `reviewing`,
`review_ready`, `delivering`, `ready_for_review`, and `owner_review_pending` —
not only `review_ready`; only an assignable prior state (`running`/
`worker_continuing`) is restorable.

### 5. Run-scoped task provenance for direct runs

`loopweave run <agent> --task-file <path>` reuses the existing assignment
service to write the immutable, run-scoped
`runs/<run-id>/assigned-task-latest.md` from the user-supplied file and deliver
the assignment to the worker through the same control sequence. The two-step
`run` then `assign` workflow stays valid. No task text is ever synthesized; the
file is byte-identical to the user's task file and is the artifact the
visible-review card references.

**Deterministic startup ordering (no race).** The managed child is started
first; the run record and the exact run-scoped task packet are then installed,
and only then is the assignment delivered to the child as its readiness
signal. The fixed order is: create the run directory → start the supervisor
(child spawned) → create the run record → install the run-scoped task packet
→ deliver the assignment to the child. Because the packet is written and the
run record exists before the assignment is delivered, a worker that consumes
the assignment as its readiness signal cannot race the launcher. A worker that
submits before its run record exists is rejected by the submission guard; a
worker that submits before its packet is installed gets the actionable
awaiting-assignment guidance, not a half-installed state.

A direct run **without** `--task-file` launches valid and is queryable, but a
premature `submit` (visible) is rejected early with an actionable message
naming `loopweave assign --run-id <id> --task-file <path>` (or the
`--task-file` relaunch spelling), before any review state is mutated — it
never surfaces the internal `task_packet_path does not exist` error.

**Idempotent and conflict-safe assignment.** Re-assigning the *same* task
(identical digest) to a run is a no-op: it returns `AssignmentResult(duplicate=True)`
without writing a new history file, without re-delivering the task to the worker
(the control-send count does not grow), and without recording a second
`task_assigned` event. The immutable `assigned-task-latest.md` is not
overwritten. Assigning a **different** task (different digest) to a run that
already has one is a conflict: it is rejected and leaves
`assigned-task-latest.md`, the history-file count, the control-send count, and
the assignment events unchanged. Re-assignment is only permitted while the run
is assignable (`running`/`worker_continuing`); a run with an in-flight/pending
review is not assignable, so retry cannot overwrite
provenance or bypass a pending review.

### 6. Bounded, schema-constrained audit data

Audit records (`events.jsonl` `run_orphaned`/`run_recovered` and
`orphan-provenance.json`) carry only bounded, schema-constrained data:

- `reason_category` MUST be one of the fixed enum values
  (`identity_mismatch`, `control_unreachable`, `control_unauthenticated`,
  `pid_reused`, `child_exited`, `run_mismatch`); an unrecognized value is
  rejected (never written).
- `source` is bounded to a short fixed length (≤ 64 bytes); oversized input is
  truncated before writing.
- Exception text, environment secrets, and the control token are never written.
  An orphan triggered by a reader exception records only the enum category, not
  `str(exception)`.

### 7. Crash-consistency of the orphan transition

`audit_orphan` writes in a fixed order so recovery can never become impossible
when a later write fails, and so a failed provenance write cannot strand the
database row:

1. write the durable `orphan-provenance.json` (authoritative `prior_state`,
   `reason_category`, `source`, `run_id`). **If this write fails, `audit_orphan`
   MUST NOT proceed** — the database row is left un-orphaned (a row must never
   become `orphaned` without its durable provenance).
2. `force_state(ORPHANED)` (the database change) — runs only after step 1
   succeeded; the provenance file already exists when this runs.
3. append the `run_orphaned` event (best-effort).

This order is the opposite of the unsafe `force_state → provenance → event`,
which a provenance-write failure would leave orphaned without provenance. The
contract is locked by failure injection: a provenance-write failure leaves the
run un-orphaned, and `force_state(ORPHANED)` only ever runs after the
provenance file exists. Because the durable provenance precedes the database
change and the event is last, an event-write failure leaves the run orphaned
*with* its provenance intact — `recover_orphaned` reads `orphan-provenance.json`,
not the event, so recovery still succeeds. Recovery never depends on the event
stream.

Recovery uses the mirror ordering so a failure can never produce a false
completion or erase the only orphan audit:

1. read `orphan-provenance.json` (must exist; otherwise recovery is a no-op);
2. **backfill** the `run_orphaned` event if it is missing (reconstructed from
   the durable provenance), so a truthful orphan audit exists before anything
   is cleared;
3. verify the session is alive with exact recorded identity (refuse otherwise,
   leaving the provenance record in place);
4. `force_state(restored)` — the state change;
5. append the `run_recovered` completion event **only after** step 4 succeeds;
6. `_clear_orphan_provenance` (last).

Because the completion event follows the state change, a `force_state(restored)`
failure leaves **no** `run_recovered` event — the audit never claims a recovery
that did not happen. Because step 2 backfills a missing orphan audit from the
durable provenance before step 6 may clear it, an orphan-event failure followed
by recovery always leaves exactly one truthful `run_orphaned` and one truthful
`run_recovered`, without duplicates. A clear failure leaves a restored, audited
run with the provenance record still on disk — harmless, since the run is no
longer `orphaned`.

**Idempotent completion of a crashed recovery.** The real crash window is
`force_state(restored)` succeeding while the `run_recovered` append (or the
clear) fails: the run is restored, the provenance record remains, and the
completion event is missing. `recover_orphaned` is therefore idempotent — a
later `status`/`recover` call (or the audit reconciler) sees the lingering
provenance, appends exactly one truthful `run_recovered` if it is missing, and
only then clears the provenance record. Re-running it changes nothing. The
terminal state always converges to: run restored, exactly one `run_orphaned`,
exactly one `run_recovered`, and no provenance file.

**Injection contract (testability).** `liveness.py` writes the durable records
through named, patchable callables so failure-injection tests can prove the
failure actually fired: `_write_orphan_provenance(run_dir, payload)` (must run
before `force_state(ORPHANED)`), `append_event` (bound via
`from .protocol import append_event`), and `_clear_orphan_provenance(run_dir)`.
Failure-injection tests patch these names on `loopweave.liveness` and assert
the mock was called, so an implementation that bypasses a patch (e.g. a direct
re-import) cannot pass.

## Acceptance

Focused regressions in `tests/test_live_reliability.py` gate the contract
(intentionally failing at Stage 1, implemented at Stage 2):

- transient identity-reader failure + authenticated alive control → not orphaned;
- missing process, reused PID, wrong token/socket, mismatched run/PID, dead
  child → fail-closed orphan;
- every orphan/recovery transition writes bounded non-secret audit data;
- safe recovery accepts only the exact live assignable session and rejects
  unsafe states;
- direct run with `--task-file` creates exact provenance and queues its first
  visible review;
- direct run without a task file stays usable; a premature submit returns
  actionable guidance without mutating review state;
- retry does not duplicate/overwrite provenance or a pending review;
- generic verdict delivery sends message then `"\r"` to the same PID (guard
  for LW-IT-002, kept literal).

## Consequences

- Orphan transitions become slower by one authenticated socket round-trip only
  on the failure/recovery path; the happy path (identity matches) is
  unchanged.
- `orphaned` is no longer terminal-in-practice for verified false positives,
  but only via an audited, authenticated recovery — never a silent
  resurrection.
- No change to literal control `send` semantics (LW-IT-002 stays a validation
  gate).
