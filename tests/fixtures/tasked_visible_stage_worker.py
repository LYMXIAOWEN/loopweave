"""Fixture worker for the integrated `--reviewer visible-thread --task-file`
atomic-path contract (ADR 0002 section 5).

The readiness gate is LoopWeave-owned, not worker-owned: this worker does NOT
poll LoopWeave's private registry or run directory. It waits for its assignment
to arrive on its managed terminal input (stdin) - exactly the input channel a
real interactive CLI agent receives. LoopWeave installs the run record and the
exact task packet, and only then delivers the assignment (the submit-readiness
signal) through that terminal input, so receiving the assignment proves the
record and packet already exist. The worker then submits one visible stage and
exits; the driving test asserts the first visible-review card queued and
references the exact packet.
"""
from __future__ import annotations

import os
import select
import sys
import time


def main() -> int:
    src_root = os.environ.get("LOOPWEAVE_TEST_SRC")
    if src_root:
        sys.path[:] = [src_root] + [p for p in sys.path if p != src_root]
    run_id = os.environ["LOOPWEAVE_RUN_ID"]

    from loopweave.submission import submit_stage

    # Wait for the assignment on the managed terminal input. LoopWeave delivers
    # it only after the run record and exact task packet are installed, so its
    # arrival is the readiness gate (no private-state polling).
    deadline = time.time() + 8
    saw_assignment = False
    while time.time() < deadline:
        ready, _, _ = select.select([sys.stdin], [], [], 0.2)
        if not ready:
            continue
        line = sys.stdin.readline()
        if not line:
            break
        if "[LoopWeave assignment]" in line:
            saw_assignment = True
            # Hold this live session briefly so the launcher finishes the full
            # assignment delivery (message then submit key, ~0.35s apart) before
            # the worker acts and exits.
            time.sleep(0.6)
            break
    if not saw_assignment:
        return 4  # LoopWeave never delivered an assignment (the RED, pre-fix state)

    submit_stage(
        run_id,
        "Tasked visible stage complete.",
        evidence={"files_changed": [], "commands_run": []},
    )
    print("TASKED_WORKER_DONE", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
