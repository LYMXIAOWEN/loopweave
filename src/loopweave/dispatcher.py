from __future__ import annotations

import json
import os
import subprocess
import uuid
from enum import Enum
from pathlib import Path
from typing import Callable, Optional

from .config import resolve_codex_bin
from .models import RunMode
from .protocol import ProtocolError, utc_now, validate_reviewer_verdict, write_json_atomic
from . import terminal_host


class DispatchError(RuntimeError):
    pass


class DispatchInFlight(DispatchError):
    pass


class DispatchLeaseState(str, Enum):
    ABSENT = "absent"
    LIVE = "live"
    STALE = "stale"


def _read_dispatch_lease(path: Path) -> dict:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if (
        not isinstance(payload, dict)
        or not isinstance(payload.get("pid"), int)
        or not isinstance(payload.get("process_start"), str)
        or not payload["process_start"]
        or not isinstance(payload.get("binding_generation"), int)
        or not isinstance(payload.get("created_at"), str)
        or not payload["created_at"]
        or not isinstance(payload.get("lease_id"), str)
        or not payload["lease_id"]
    ):
        raise ValueError("invalid dispatch lease")
    return payload


def inspect_dispatch_lease(run_dir: Path) -> DispatchLeaseState:
    path = Path(run_dir) / "dispatch.lock"
    if not path.exists():
        return DispatchLeaseState.ABSENT
    try:
        payload = _read_dispatch_lease(path)
        current_start = terminal_host.default_process_identity_reader()(payload["pid"])
    except (
        OSError,
        ValueError,
        KeyError,
        json.JSONDecodeError,
        subprocess.SubprocessError,
    ):
        return DispatchLeaseState.STALE
    return (
        DispatchLeaseState.LIVE
        if current_start == payload["process_start"]
        else DispatchLeaseState.STALE
    )


def _lease_file_signature(path: Path) -> tuple:
    stat = path.stat()
    return (
        stat.st_dev,
        stat.st_ino,
        stat.st_size,
        stat.st_mtime_ns,
    )


def build_review_prompt(
    run_id: str,
    run_dir: Path,
    workspace_root: Optional[Path] = None,
    project_root: Optional[Path] = None,
    origin_thread_id: Optional[str] = None,
    mode: RunMode = RunMode.DEVELOP,
    review_round: int = 1,
) -> str:
    run_dir = Path(run_dir).resolve()
    workspace = (
        Path(workspace_root).resolve() if workspace_root else run_dir
    )
    request_path = Path(run_dir) / "review-request.json"
    origin = (
        f"\nOrigin Codex thread for audit only: `{origin_thread_id}`."
        if origin_thread_id
        else ""
    )
    project = (
        f"\nProject control root: `{Path(project_root).resolve()}`."
        if project_root
        else ""
    )
    common = f"""LoopWeave review request.

Review run `{run_id}` only. Do not review any other run.{origin}
Workspace source root: `{workspace}`.
Control evidence root: `{run_dir}`.{project}

1. Read `{request_path}`.
2. Inspect the complete workspace for cross-file impact, including real files,
   diffs, callers, dependencies and related tests.
3. Use `workspace-baseline.json` to distinguish pre-existing changes.
4. Do not edit workspace files or inspect sibling projects or other run directories.
5. Lead with blocking correctness, safety and regression findings.
6. Return the review as structured JSON matching the supplied output schema.
7. Put the complete human-readable review in `review_markdown`.

Allowed verdicts: approved, changes_requested, needs_human, failed.
Set continue=true only for changes_requested. The bridge determines whether an approval is intermediate
or final from the validated review request and will canonicalize approved
continuation after your review.
Do not write protocol files yourself. The bridge will validate and persist the result.
Do not start or resume another terminal Agent. The bridge will deliver your review.
"""
    if mode is RunMode.DEVELOP:
        return common
    return (
        common
        + f"""
This is a proposal and architecture convergence review, round {review_round} of 2.
Review objectives, scope, architecture, boundaries, trade-offs, risks, and
unresolved decisions. Do not apply implementation scoring or require code
evidence that is irrelevant to the proposal.
Minor improvements are advisory and must not block approval.
Approve when the proposal is decision-ready and no unresolved decision would
materially change implementation direction.
Round 2 must not request another revision. If material decisions remain after
round 2, return needs_human and identify the decisions the user must make.
"""
    )


def review_output_schema(mode: RunMode = RunMode.DEVELOP) -> dict:
    properties = {
        "verdict": {
            "type": "string",
            "enum": [
                "approved",
                "changes_requested",
                "needs_human",
                "failed",
            ],
        },
        "summary": {"type": "string"},
        "review_markdown": {"type": "string"},
        "continue": {"type": "boolean"},
    }
    required = ["verdict", "summary", "review_markdown", "continue"]
    if mode is RunMode.DESIGN:
        properties.update(
            {
                "round": {"type": "integer", "enum": [1, 2]},
                "blocking_decisions": {
                    "type": "array",
                    "items": {"type": "string"},
                },
                "advisory_notes": {
                    "type": "array",
                    "items": {"type": "string"},
                },
                "consensus_summary": {"type": "string"},
                "next_action": {
                    "type": "string",
                    "enum": [
                        "revise_design",
                        "complete_design",
                        "await_human",
                        "stop_failed",
                    ],
                },
            }
        )
        required.extend(
            [
                "round",
                "blocking_decisions",
                "advisory_notes",
                "consensus_summary",
                "next_action",
            ]
        )
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": properties,
        "required": required,
    }


class CodexDispatcher:
    def __init__(
        self,
        codex_bin: Optional[str] = None,
        runner: Callable = subprocess.run,
    ) -> None:
        self.codex_bin = codex_bin or str(resolve_codex_bin())
        self.runner = runner

    def dispatch(
        self,
        thread_id: str,
        run_id: str,
        run_dir: Path,
        workspace_root: Optional[Path] = None,
        project_root: Optional[Path] = None,
        binding_generation: int = 1,
        mode: RunMode = RunMode.DEVELOP,
        review_round: int = 1,
    ) -> subprocess.CompletedProcess:
        if not thread_id:
            raise DispatchError("exact Codex thread id is required")
        run_dir = Path(run_dir)
        run_dir.mkdir(parents=True, exist_ok=True)
        lock_path = run_dir / "dispatch.lock"
        descriptor = None
        lease_id = uuid.uuid4().hex
        for attempt in range(2):
            try:
                descriptor = os.open(
                    str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600
                )
                break
            except FileExistsError as error:
                try:
                    existing_signature = _lease_file_signature(lock_path)
                except FileNotFoundError:
                    continue
                state = inspect_dispatch_lease(run_dir)
                if state is DispatchLeaseState.STALE and attempt == 0:
                    try:
                        if _lease_file_signature(lock_path) != existing_signature:
                            raise DispatchInFlight(
                                "dispatch lease changed during stale recovery"
                            )
                        lock_path.unlink()
                    except FileNotFoundError:
                        pass
                    continue
                raise DispatchInFlight(
                    "a review dispatch is already in flight"
                ) from error
        if descriptor is None:
            raise DispatchInFlight("a review dispatch is already in flight")

        try:
            lease = {
                "pid": os.getpid(),
                "process_start": terminal_host.default_process_identity_reader()(os.getpid()),
                "binding_generation": binding_generation,
                "created_at": utc_now(),
                "lease_id": lease_id,
            }
            os.write(
                descriptor,
                (json.dumps(lease, sort_keys=True) + "\n").encode("utf-8"),
            )
            os.fsync(descriptor)
            os.close(descriptor)
            descriptor = None
            schema_path = run_dir / "review-output.schema.json"
            output_path = run_dir / "codex-review-output.json"
            write_json_atomic(schema_path, review_output_schema(mode))
            command = [
                self.codex_bin,
                "exec",
                "--ephemeral",
                "--sandbox",
                "read-only",
                "--skip-git-repo-check",
                "--output-schema",
                str(schema_path),
                "--output-last-message",
                str(output_path),
                "-",
            ]
            result = self.runner(
                command,
                input=build_review_prompt(
                    run_id,
                    run_dir,
                    workspace_root=workspace_root,
                    project_root=project_root,
                    origin_thread_id=thread_id,
                    mode=mode,
                    review_round=review_round,
                ),
                text=True,
                capture_output=True,
                cwd=str(
                    Path(workspace_root).resolve()
                    if workspace_root
                    else run_dir.resolve()
                ),
            )
            (run_dir / "dispatch.stdout.log").write_text(
                result.stdout or "", encoding="utf-8"
            )
            (run_dir / "dispatch.stderr.log").write_text(
                result.stderr or "", encoding="utf-8"
            )
            if result.returncode != 0:
                error_text = result.stderr or result.stdout or "unknown dispatch error"
                (run_dir / "dispatch-error.txt").write_text(
                    error_text, encoding="utf-8"
                )
                raise DispatchError(
                    "Codex review dispatch failed with exit code {}".format(
                        result.returncode
                    )
                )
            try:
                raw_review = json.loads(output_path.read_text(encoding="utf-8"))
                if not isinstance(raw_review, dict):
                    raise ValueError("structured review must be a JSON object")
                verdict = raw_review.get("verdict")
                should_continue = raw_review.get("continue")
                if should_continue is not (verdict == "changes_requested"):
                    raise ValueError(
                        "continue must be true exactly for changes_requested"
                    )
                review_markdown = raw_review.get("review_markdown")
                if not isinstance(review_markdown, str) or not review_markdown.strip():
                    raise ValueError("review_markdown must be a non-empty string")
                review_payload = {
                        "schema_version": 1,
                        "run_id": run_id,
                        "review_id": "review-" + uuid.uuid4().hex[:12],
                        "verdict": verdict,
                        "summary": raw_review.get("summary"),
                        "review_file": "reviewer-verdict.md",
                        "continue": should_continue,
                    }
                if mode is RunMode.DESIGN:
                    review_payload.update(
                        {
                            key: raw_review.get(key)
                            for key in (
                                "round",
                                "blocking_decisions",
                                "advisory_notes",
                                "consensus_summary",
                                "next_action",
                            )
                        }
                    )
                review = validate_reviewer_verdict(review_payload)
                if not isinstance(review["summary"], str):
                    raise ValueError("summary must be a string")
                (run_dir / "reviewer-verdict.md").write_text(
                    review_markdown, encoding="utf-8"
                )
                write_json_atomic(run_dir / "reviewer-verdict.json", review)
            except (OSError, json.JSONDecodeError, ProtocolError, ValueError) as error:
                error_text = "invalid structured review output: {}".format(error)
                (run_dir / "dispatch-error.txt").write_text(
                    error_text, encoding="utf-8"
                )
                raise DispatchError(error_text) from error
            return result
        finally:
            if descriptor is not None:
                try:
                    os.close(descriptor)
                except OSError:
                    pass
            try:
                current = _read_dispatch_lease(lock_path)
                if current["lease_id"] == lease_id:
                    lock_path.unlink()
            except (FileNotFoundError, OSError, ValueError, json.JSONDecodeError):
                pass
