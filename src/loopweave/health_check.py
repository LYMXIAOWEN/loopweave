"""Health checker for a LoopWeave run directory.

Reports whether the bridge artifacts (run.json, review-request.json,
reviewer-verdict.json, reviewer-verdict.md) are present, well-formed, and
internally consistent. Reuses the canonical protocol validators and the
same containment rule as production delivery, so the checker cannot
report healthy for artifacts the bridge itself would reject.

Run from the project root:

    PYTHONPATH=src python3 -m loopweave.health_check <run-dir>
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from loopweave.protocol import (
    ProtocolError,
    read_json,
    validate_review_request,
    validate_reviewer_verdict,
)

# Fields the bridge writes into run.json (see cli._run_agent).
RUN_REQUIRED_FIELDS = {
    "schema_version",
    "run_id",
    "codex_thread_id",
    "cwd",
    "tty",
    "agent",
    "agent_pid",
    "agent_process_start",
    "socket_path",
}


def _load(path):
    """Read+decode one artifact. Returns (payload, None) or (None, message)."""
    try:
        return read_json(path), None
    except FileNotFoundError:
        return None, "missing: {}".format(path.name)
    except ProtocolError as exc:
        return None, "{}: {}".format(path.name, exc)
    except ValueError as exc:
        return None, "invalid json in {}: {}".format(path.name, exc)


def _is_non_empty_file(path):
    return path.is_file() and path.stat().st_size > 0


def check_run(run_dir):
    """Return a sorted list of failure strings; empty means healthy."""
    run_dir = Path(run_dir)
    failures = []

    run_payload, run_err = _load(run_dir / "run.json")
    request, req_err = _load(run_dir / "review-request.json")
    review, rev_err = _load(run_dir / "reviewer-verdict.json")
    for err in (run_err, req_err, rev_err):
        if err:
            failures.append(err)

    if run_payload is not None:
        missing = sorted(RUN_REQUIRED_FIELDS - set(run_payload))
        if missing:
            failures.append("run.json missing fields: {}".format(", ".join(missing)))
        elif run_payload.get("schema_version") != 1:
            failures.append("run.json schema_version must be 1")
        has_explicit_paths = any(
            key in run_payload for key in ("thread_cwd", "workspace_root")
        )
        if has_explicit_paths:
            for key in ("thread_cwd", "workspace_root"):
                value = run_payload.get(key)
                if not isinstance(value, str) or not value:
                    failures.append(
                        "run.json {} must be a non-empty string".format(key)
                    )
        for key in ("project_slug", "project_root"):
            value = run_payload.get(key)
            if value is not None and (
                not isinstance(value, str) or not value
            ):
                failures.append(
                    "run.json {} must be null or a non-empty string".format(key)
                )

    request_valid = False
    if request is not None:
        try:
            validate_review_request(request)
            request_valid = True
        except ProtocolError as exc:
            failures.append("review-request.json: {}".format(exc))

    review_valid = False
    if review is not None:
        try:
            validate_reviewer_verdict(review)
            review_valid = True
        except ProtocolError as exc:
            failures.append("reviewer-verdict.json: {}".format(exc))

    # Cross-artifact linkage: only meaningful when all three are individually valid.
    if run_payload is not None and request_valid and review_valid:
        run_ids = {
            "run.json": run_payload.get("run_id"),
            "review-request.json": request.get("run_id"),
            "reviewer-verdict.json": review.get("run_id"),
        }
        bad_types = {
            src: value for src, value in run_ids.items()
            if not isinstance(value, str) or not value
        }
        if bad_types:
            failures.append("run_id not a non-empty string: {}".format(bad_types))
        elif len(set(run_ids.values())) != 1:
            failures.append("run_id mismatch: {}".format(run_ids))

        # review_file must resolve inside the run directory and be a real
        # non-empty file (matches production delivery in cli._deliver_review).
        run_resolved = run_dir.resolve()
        target = (run_dir / review["review_file"]).resolve()
        if target != run_resolved and run_resolved not in target.parents:
            failures.append(
                "review_file escapes run directory: {}".format(review["review_file"])
            )
        elif not _is_non_empty_file(target):
            failures.append(
                "review_file not a non-empty file: {}".format(review["review_file"])
            )

        completion_scope = request.get("completion_scope", "final")
        expected_continue = (
            review.get("verdict") == "changes_requested"
            or (
                review.get("verdict") == "approved"
                and request.get("mode", "develop") == "develop"
                and completion_scope == "stage"
            )
        )
        if review.get("continue") != expected_continue:
            failures.append(
                "continue={} disagrees with verdict={} (expected {})".format(
                    review.get("continue"), review.get("verdict"), expected_continue
                )
            )

    if not _is_non_empty_file(run_dir / "reviewer-verdict.md"):
        failures.append("reviewer-verdict.md missing or empty")

    return sorted(set(failures))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run_dir", help="path to a loopweave run directory")
    args = parser.parse_args(argv)

    run_dir = Path(args.run_dir)
    if not run_dir.is_dir():
        print("not a directory: {}".format(run_dir), file=sys.stderr)
        return 2

    failures = check_run(run_dir)
    if failures:
        print("FAIL  {}".format(run_dir))
        for item in failures:
            print("  - {}".format(item))
        return 1
    print("OK    {}".format(run_dir))
    print("  run.json, review-request.json, reviewer-verdict.json/md present, valid, consistent")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
