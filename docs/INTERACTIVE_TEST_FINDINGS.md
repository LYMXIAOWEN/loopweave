# Interactive Test Findings

This is the running record for live, visible-terminal validation.  It is not
an implementation plan: each entry separates what was observed from the repair
that still needs design and verification.

## 2026-07-22 — generic Codex CLI worker

### LW-IT-001 — false `orphaned` state blocks a live worker

- **Observed:** `run-live-example` (redacted; the real session id is omitted
  per the public-privacy rule) changed to `orphaned` before its first
  `loopweave submit --stage`, although the managed PID, recorded process start
  time, and authenticated control-socket status all still matched.
- **Impact:** a healthy worker cannot submit, attach, or receive review.
- **Temporary test recovery:** the run was restored to `running` only after
  all three liveness checks passed. No replacement worker was created.
- **Root-cause class (ADR 0002):** every production orphan transition shared
  one defect shape — an identity-read exception was swallowed and turned
  directly into an irreversible, unaudited `force_state(ORPHANED)`. No orphan
  audit existed at the time, so the focused evidence only proves that the
  recorded identity and control status matched afterward and that the old code
  could irreversibly orphan on several unaudited paths; the observation is
  *consistent with* a transient identity-read failure, but the exact transient
  trigger cannot be proved.
- **Resolution:** `src/loopweave/liveness.py` centralizes every orphan
  transition behind `audit_orphan` (bounded, non-secret, schema-constrained
  audit + durable `orphan-provenance.json`), and `authenticated_identity_check`
  consults the authenticated control channel before orphaning — a transient
  reader failure with a matching live control does not orphan; a genuine
  missing/reused process, wrong token/socket, or mismatched run/PID stays
  fail-closed. A verified false-positive orphan is restored idempotently and
  auditably by `loopweave status`/`loopweave recover`. Focused regressions:
  transient-failure-no-orphan per route, the full fail-closed matrix, bounded
  audit, crash-consistent provenance ordering, and idempotent recovery.
- **Status:** resolved (automated). See ADR 0002 and
  `tests/test_live_reliability.py`.

### LW-IT-002 — verify submit-key delivery on the real review path

- **Observed:** a one-off recovery message sent through the raw control
  primitive appeared in the Codex CLI composer but remained unsent until the
  user pressed Enter.
- **Important distinction:** this manual diagnostic sent only the text. The
  production review-delivery path already requests two literal inputs -- the
  message followed by `"\r"` -- so this observation does not yet prove a
  product defect in review delivery.
- **Verification requirement:** exercise the complete visible-review path
  against a real Codex CLI and confirm the review is submitted and processed
  without a manual Enter.
- **Verification result:** the complete production path was exercised against
  a real managed Codex CLI. A stage verdict changed the run from
  `ready_for_review` to `worker_continuing`, the same managed PID immediately
  resumed work, and the worker submitted final without a manual Enter for the
  delivered review message.
- **Status:** passed (live and automated). The production path is unit-guarded
  (`VerdictDeliveryGuardTests::test_generic_changes_requested_sends_message_then_submit_key`
  pins the literal message-then-`"\r"` order), and the real-terminal cycle
  confirmed that order reaches the Codex composer and submits the message.

### LW-IT-003 — direct generic runs lack the required task-packet artifact

- **Observed:** after a healthy direct `loopweave run codex` worker retried
  `loopweave submit --stage`, card validation rejected it with
  `task_packet_path does not exist`.
- **Evidence:** the submission path always resolves the visible-review task
  packet to `runs/<run-id>/assigned-task-latest.md`, but direct `run` creates
  no such file. That file is currently written only by the separate legacy
  assignment flow.
- **Resolution:** `loopweave run <agent> --task-file <path>` reuses the
  assignment service to install the exact, byte-identical run-scoped packet
  and deliver the assignment deterministically. The managed child is started
  first; the run record and packet are then installed before the assignment is
  delivered as the child's readiness signal; a premature submit is rejected
  with actionable guidance. A taskless direct run stays valid and reports
  `awaiting task assignment` in `status --json`; a premature `submit` returns
  actionable guidance naming the assign command, without mutating review
  state. Same-task retry is idempotent; a different task is a conflict.
- **Status:** resolved (automated). See ADR 0002 section 5 and
  `tests/test_live_reliability.py::TaskProvenanceContractTests` +
  the integrated `tasked_visible_stage_worker` end-to-end test.

## Manual real-terminal acceptance procedure

This is the repeatable acceptance procedure for LW-IT-002 and the end-to-end
generic-worker visible-review cycle, for a reviewer/operator to run against a
real Codex CLI. `loopweave run` enters the foreground managed session and does **not**
print a run id, so the id is obtained from `loopweave runs` / `status --json`.
The run uses two terminals that must share one runtime root and workspace.

### One-time preflight (run once in a throwaway shell, before either terminal)

Generate a fresh unique runtime, create all artifacts, obtain the reviewer
thread id, write a shared env file, and bind the bridge — all before either
Terminal A or B launches. Do NOT re-run the `rm -rf` after Terminal A has
launched.

```bash
RUNTIME="/tmp/loopweave-acceptance-$$"
WORKSPACE="/tmp/loopweave-acceptance-ws-$$"
rm -rf "$RUNTIME" "$WORKSPACE"
mkdir -p "$WORKSPACE"
cd "$WORKSPACE"

# Create the input artifacts (executable heredocs):
cat > task.md <<'TASK'
# Acceptance task

1. Do the work, then run:
   loopweave submit --stage --run-id "$LOOPWEAVE_RUN_ID" --summary-file stage.md
2. Wait for the reviewer's changes_requested verdict (delivered to this session).
3. Then run:
   loopweave submit --final --run-id "$LOOPWEAVE_RUN_ID" --summary-file final.md
TASK

echo "Stage one complete." > stage.md
echo "Final result complete." > final.md

cat > verdict.md <<'VERDICT'
---
verdict: changes_requested
summary: Proceed to the final stage.
---

Address the noted change, then submit the final result.
VERDICT

# Verify all artifacts are non-empty:
test -s task.md && test -s stage.md && test -s final.md && test -s verdict.md \
    && echo "artifacts ok" || { echo "artifact creation failed"; exit 1; }
```

Obtain the reviewer thread id: run `/status` in the Codex reviewer task you
intend to use; copy its `thread_id`, then paste it below:

```bash
REVIEWER_THREAD_ID=<paste the thread_id from /status>
```

Write a shared env file so both terminals inherit the resolved values (shell
variables are NOT inherited by separately opened terminals):

```bash
cat > /tmp/loopweave-acceptance-env <<EOF
export LOOPWEAVE_HOME="$RUNTIME"
export ACCEPTANCE_WS="$WORKSPACE"
export REVIEWER_THREAD_ID="$REVIEWER_THREAD_ID"
EOF

# Source it here and bind the bridge BEFORE Terminal A launches:
source /tmp/loopweave-acceptance-env
loopweave bridge bind --thread "$REVIEWER_THREAD_ID"
echo "preflight complete — bridge bound, env file at /tmp/loopweave-acceptance-env"
```

### Terminal A — managed worker (enters the foreground Codex session)

```bash
source /tmp/loopweave-acceptance-env
cd "$ACCEPTANCE_WS"
loopweave run codex --task-file task.md --reviewer visible-thread \
    --thread "$REVIEWER_THREAD_ID"
```

`--thread` passes the bound reviewer thread so `discover_thread` does not
guess by cwd. The managed child is started, the run record and task packet
are installed, and then the assignment is delivered to the child.

### Terminal B — operator/reviewer (wait for Terminal A to launch, then run)

```bash
source /tmp/loopweave-acceptance-env
cd "$ACCEPTANCE_WS"
loopweave runs                                     # find the new run's id
RUN_ID=<paste the run id>
loopweave status --json "$RUN_ID"                  # running; task_assignment: assigned
```

The Codex worker (Terminal A), following `task.md`, submits its stage:

```bash
loopweave submit --stage --run-id "$LOOPWEAVE_RUN_ID" --summary-file stage.md
```

Terminal B reviews and delivers the verdict:

```bash
loopweave status --json "$RUN_ID"                  # ready_for_review; visible card queued
loopweave review-next --run-id "$RUN_ID"
loopweave review-submit --run-id "$RUN_ID" --review-file verdict.md
```

The `changes_requested` verdict is delivered to the **same** managed session
(same PID). **LW-IT-002 check:** confirm the review message is submitted in
the Codex composer without a manual Enter.

The worker (Terminal A), after the delivered changes request, submits final:

```bash
loopweave submit --final --run-id "$LOOPWEAVE_RUN_ID" --summary-file final.md
```

### Expected transitions, stop path, residual risk

State transitions: `running` -> `ready_for_review` (stage submit) ->
`worker_continuing` (`changes_requested` delivered to the same PID) ->
`ready_for_review` (final submit). Stop path: `loopweave stop "$RUN_ID"`
(Terminal B), or the worker exiting. The live Codex acceptance passed this
sequence. Codex may still request local-command authorization before the
worker runs `loopweave submit`; that is a Codex sandbox approval boundary,
not a failure to submit the delivered review message.
