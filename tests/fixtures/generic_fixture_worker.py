"""Deterministic generic fixture worker (ADR 0001 / Package 1 Stage 3).

This worker is launched as a generic managed CLI (``loopweave run --`` style) -
NOT a Claude adapter, with no Claude transcript parsing. It exercises the
vendor-neutral worker submission service directly: it submits one stage result,
waits for the owner's review verdict to be delivered back to THIS SAME managed
session (it arrives on stdin through the control socket), then submits one
final result. That round trip is the Stage 3 integration proof that an
arbitrary CLI can complete a structured review cycle and resume the same
session without Claude.

The worker is intentionally dependency-light and deterministic: it writes small
marker/artifact files and coordinates with the driving test through them.
"""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path


def main() -> int:
    # Import the project's loopweave (not any installed copy). An editable
    # install of another loopweave may already be on sys.path ahead of the
    # project src, so force the project src to the front unconditionally rather
    # than skipping when it is merely present.
    src_root = os.environ.get("LOOPWEAVE_TEST_SRC")
    if src_root:
        sys.path[:] = [src_root] + [p for p in sys.path if p != src_root]

    run_id = os.environ["LOOPWEAVE_RUN_ID"]
    workspace = Path(os.environ["LOOPWEAVE_FIXTURE_WORKSPACE"])

    from loopweave.submission import (
        SubmissionError,
        submit_final,
        submit_stage,
    )

    # Stage 1: do deterministic work, then submit a stage result through the
    # vendor-neutral submission service.
    (workspace / "stage1.txt").write_text(
        "stage-one artifact\n", encoding="utf-8"
    )
    submit_stage(
        run_id,
        "Generic fixture worker completed stage one.",
        evidence={"files_changed": ["stage1.txt"], "commands_run": []},
    )
    (workspace / "stage-marker").write_text("done\n", encoding="utf-8")

    # Wait for the owner's review verdict to be delivered back to this same
    # managed session (delivered on stdin via the control socket). Receiving it
    # and then submitting the final result proves the same session resumed.
    deadline = time.time() + 30
    saw_review = False
    while time.time() < deadline:
        line = sys.stdin.readline()
        if not line:
            time.sleep(0.05)
            continue
        if "LoopWeave review" in line:
            saw_review = True
            break
    if not saw_review:
        return 2

    (workspace / "final.txt").write_text(
        "final artifact\n", encoding="utf-8"
    )
    # The owner's delivery sets WORKER_CONTINUING as its final step; there is a
    # brief window where the review text has arrived but the run is still
    # DELIVERING. Retry the final submission until the state machine has
    # settled into a submittable state (a real worker does work between turns;
    # this fixture is just faster, so it waits explicitly).
    final_deadline = time.time() + 30
    last_error = None
    while time.time() < final_deadline:
        try:
            submit_final(
                run_id,
                "Generic fixture worker completed the final result.",
                evidence={"files_changed": ["final.txt"], "commands_run": []},
            )
            break
        except SubmissionError as error:
            last_error = error
            time.sleep(0.05)
    else:
        raise last_error
    (workspace / "final-marker").write_text("done\n", encoding="utf-8")
    print("WORKER_DONE pid={}".format(os.getpid()), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
