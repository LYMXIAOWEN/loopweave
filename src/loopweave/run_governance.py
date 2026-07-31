from __future__ import annotations

import hashlib
import io
import json
import os
import stat
import sys
import tarfile
import uuid
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Dict, Iterator, List, Optional, Sequence, Tuple

from . import terminal_host
from .config import (
    ARCHIVES_DIR,
    LEDGER_DIR,
    MAINTENANCE_DIR,
    RUNS_DIR,
    TRASH_DIR,
    VAR_DIR,
)
from .models import (
    RunRecord,
    RunState,
    RunStorageRecord,
    StorageState,
)
from .protocol import utc_now, write_json_atomic
from .registry import Registry
from .runtime_config import POLICY_VERSION, RunPolicy, load_run_policy


_ACTIVE_STATES = {
    RunState.CREATED,
    RunState.BINDING,
    RunState.RUNNING,
    RunState.READY_FOR_REVIEW,
    RunState.REVIEWING,
    RunState.REVIEW_READY,
    RunState.DELIVERING,
    RunState.WORKER_CONTINUING,
}
_ARCHIVABLE_TERMINAL_STATES = {
    RunState.APPROVED,
    RunState.STOPPED,
    RunState.FAILED,
}
_ALWAYS_PROTECTED_STATES = {
    RunState.NEEDS_HUMAN,
    RunState.OWNER_REVIEW_PENDING,
}
_HEAVY_LOG_NAMES = {
    "terminal.txt",
    "terminal.raw.log",
    "dispatch.stdout.log",
    "dispatch.stderr.log",
    "completion.stdout.log",
    "completion.stderr.log",
}
_MANIFEST_NAME = ".loopweave-manifest.json"


class GovernanceError(RuntimeError):
    pass


@dataclass(frozen=True)
class GovernancePaths:
    runs: Path = RUNS_DIR
    archives: Path = ARCHIVES_DIR
    ledger: Path = LEDGER_DIR
    trash: Path = TRASH_DIR
    maintenance: Path = MAINTENANCE_DIR
    var: Path = VAR_DIR

    def ensure(self) -> None:
        for path in (
            self.runs,
            self.archives,
            self.ledger,
            self.trash,
            self.maintenance,
            self.var,
        ):
            path.mkdir(parents=True, exist_ok=True)


@dataclass(frozen=True)
class RunDecision:
    run_id: str
    action: str
    reasons: Tuple[str, ...]
    run_state: str
    storage_state: str
    size_bytes: int
    last_activity: Optional[str]
    snapshot: str


@dataclass(frozen=True)
class GcPlan:
    plan_id: str
    created_at: str
    policy_version: str
    decisions: Tuple[RunDecision, ...]
    unregistered_directories: Tuple[str, ...]

    def to_dict(self) -> dict:
        return {
            "schema_version": 1,
            "plan_id": self.plan_id,
            "created_at": self.created_at,
            "policy_version": self.policy_version,
            "decisions": [asdict(decision) for decision in self.decisions],
            "unregistered_directories": list(self.unregistered_directories),
        }

    @classmethod
    def from_dict(cls, payload: dict) -> "GcPlan":
        if payload.get("schema_version") != 1:
            raise GovernanceError("unsupported GC plan schema")
        decisions = tuple(
            RunDecision(
                run_id=item["run_id"],
                action=item["action"],
                reasons=tuple(item.get("reasons", [])),
                run_state=item["run_state"],
                storage_state=item["storage_state"],
                size_bytes=int(item.get("size_bytes", 0)),
                last_activity=item.get("last_activity"),
                snapshot=item["snapshot"],
            )
            for item in payload.get("decisions", [])
        )
        return cls(
            plan_id=payload["plan_id"],
            created_at=payload["created_at"],
            policy_version=payload["policy_version"],
            decisions=decisions,
            unregistered_directories=tuple(
                payload.get("unregistered_directories", [])
            ),
        )


@dataclass(frozen=True)
class ApplyResult:
    plan_id: str
    applied: Tuple[Tuple[str, str], ...]
    skipped: Tuple[Tuple[str, str], ...]

    def to_dict(self) -> dict:
        return {
            "schema_version": 1,
            "plan_id": self.plan_id,
            "applied": [
                {"run_id": run_id, "action": action}
                for run_id, action in self.applied
            ],
            "skipped": [
                {"run_id": run_id, "reason": reason}
                for run_id, reason in self.skipped
            ],
        }


def _parse_timestamp(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _safe_relative_files(root: Path) -> Iterator[Path]:
    if not root.is_dir() or root.is_symlink():
        raise GovernanceError("run directory is missing or unsafe: {}".format(root))
    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            raise GovernanceError("symlinked run artifact is not archivable: {}".format(path))
        if path.is_file():
            yield path.relative_to(root)
        elif not path.is_dir():
            raise GovernanceError("special run artifact is not archivable: {}".format(path))


def _directory_snapshot(path: Path) -> dict:
    if not path.exists():
        return {
            "exists": False,
            "size_bytes": 0,
            "file_count": 0,
            "latest_mtime_ns": 0,
        }
    if not path.is_dir() or path.is_symlink():
        return {
            "exists": True,
            "unsafe": True,
            "size_bytes": 0,
            "file_count": 0,
            "latest_mtime_ns": path.lstat().st_mtime_ns,
        }
    size = 0
    count = 0
    latest = path.stat().st_mtime_ns
    unsafe = False
    for item in path.rglob("*"):
        metadata = item.lstat()
        latest = max(latest, metadata.st_mtime_ns)
        if stat.S_ISLNK(metadata.st_mode) or not (
            stat.S_ISREG(metadata.st_mode) or stat.S_ISDIR(metadata.st_mode)
        ):
            unsafe = True
        if stat.S_ISREG(metadata.st_mode):
            count += 1
            size += metadata.st_size
    return {
        "exists": True,
        "unsafe": unsafe,
        "size_bytes": size,
        "file_count": count,
        "latest_mtime_ns": latest,
    }


def _event_timestamps(run_dir: Path) -> List[datetime]:
    events_path = run_dir / "events.jsonl"
    if not events_path.is_file() or events_path.is_symlink():
        return []
    timestamps = []
    for line in events_path.read_text(
        encoding="utf-8", errors="replace"
    ).splitlines():
        try:
            payload = json.loads(line)
        except ValueError:
            continue
        parsed = _parse_timestamp(payload.get("timestamp"))
        if parsed is not None:
            timestamps.append(parsed)
    return timestamps


class RunGovernance:
    def __init__(
        self,
        registry: Registry,
        *,
        paths: Optional[GovernancePaths] = None,
        policy: Optional[RunPolicy] = None,
        process_start_reader: Optional[Callable[[int], str]] = None,
        now_factory: Optional[Callable[[], datetime]] = None,
    ) -> None:
        self.registry = registry
        self.paths = paths if paths is not None else GovernancePaths()
        self.policy = policy if policy is not None else load_run_policy()
        self.process_start_reader = (
            process_start_reader
            if process_start_reader is not None
            else terminal_host.default_process_identity_reader()
        )
        self.now_factory = (
            now_factory
            if now_factory is not None
            else lambda: datetime.now(timezone.utc)
        )
        self.paths.ensure()

    @contextmanager
    def maintenance_lock(self) -> Iterator[None]:
        from .file_lock import exclusive_file_lock

        lock_path = self.paths.maintenance / "governance.lock"
        try:
            with exclusive_file_lock(lock_path, blocking=False):
                yield
        except BlockingIOError as error:
            raise GovernanceError(
                "another LoopWeave maintenance operation is active"
            ) from error

    def _process_status(self, run: RunRecord) -> str:
        if run.agent_pid <= 0:
            return "dead"
        try:
            observed = self.process_start_reader(run.agent_pid)
        except Exception:
            from .terminal_host import pid_alive

            if not pid_alive(run.agent_pid):
                return "dead"
            return "uncertain"
        if observed == run.agent_process_start:
            return "live"
        return "identity_mismatch"

    @staticmethod
    def _has_pending_review(run_dir: Path) -> bool:
        pending = run_dir / "review-inbox" / "pending"
        if pending.is_file() and pending.stat().st_size > 0:
            return True
        for name in (
            "visible-review-wakeup-state.json",
            "visible-review-heartbeat.json",
        ):
            path = run_dir / name
            if not path.is_file() or path.is_symlink():
                continue
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except (ValueError, OSError):
                return True
            if payload.get("status") in {
                "queued",
                "pending",
                "dispatching",
                "reviewing",
            }:
                return True
        return False

    def _referenced_run_ids(self) -> set[str]:
        references: set[str] = set()
        for run in self.registry.list_runs():
            run_dir = Path(run.run_dir)
            references.update(self._run_outbound_references(run_dir))
            storage = self.registry.get_storage(run.run_id)
            if storage.ledger_path:
                ledger_path = Path(storage.ledger_path)
                if ledger_path.is_file() and not ledger_path.is_symlink():
                    try:
                        ledger = json.loads(
                            ledger_path.read_text(encoding="utf-8")
                        )
                    except (ValueError, OSError):
                        ledger = {}
                    references.update(
                        value
                        for value in ledger.get("referenced_run_ids", [])
                        if isinstance(value, str)
                    )
        return references

    @staticmethod
    def _run_outbound_references(run_dir: Path) -> set[str]:
        references: set[str] = set()
        for path in (run_dir / "run.json", run_dir / "events.jsonl"):
            if not path.is_file() or path.is_symlink():
                continue
            text = path.read_text(encoding="utf-8", errors="replace")
            for line in text.splitlines():
                try:
                    payload = json.loads(line)
                except ValueError:
                    continue
                source = payload.get("source_run_id")
                if isinstance(source, str):
                    references.add(source)
                continuity = payload.get("task_continuity")
                if isinstance(continuity, dict):
                    source = continuity.get("source_run_id")
                    if isinstance(source, str):
                        references.add(source)
        return references

    @staticmethod
    def _latest_activity(run_dir: Path) -> Optional[datetime]:
        timestamps = _event_timestamps(run_dir)
        if timestamps:
            return max(timestamps)
        snapshot = _directory_snapshot(run_dir)
        if not snapshot.get("exists") or snapshot.get("latest_mtime_ns", 0) <= 0:
            return None
        return datetime.fromtimestamp(
            snapshot["latest_mtime_ns"] / 1_000_000_000,
            tz=timezone.utc,
        )

    def _decision(
        self,
        run: RunRecord,
        storage: RunStorageRecord,
        *,
        references: set[str],
        keep_full_ids: Optional[set[str]] = None,
        manual_archive: bool = False,
    ) -> RunDecision:
        run_dir = Path(run.run_dir)
        directory = _directory_snapshot(run_dir)
        process_status = self._process_status(run)
        pending = self._has_pending_review(run_dir) if directory.get("exists") else False
        last_activity = self._latest_activity(run_dir)
        reasons: List[str] = []
        action = "none"

        if storage.pinned:
            reasons.append("pinned")
        if run.run_id in references:
            reasons.append("referenced_by_task_continuity")
        if pending:
            reasons.append("pending_review_or_delivery")
        if run.state in _ALWAYS_PROTECTED_STATES:
            reasons.append("protected_run_state:{}".format(run.state.value))
        if run.state in _ACTIVE_STATES:
            reasons.append("active_run_state:{}".format(run.state.value))
        if process_status == "live":
            reasons.append("managed_process_live")
        elif process_status in {"uncertain", "identity_mismatch"}:
            reasons.append("process_identity_{}".format(process_status))
        if directory.get("unsafe"):
            reasons.append("unsafe_run_directory")

        now = self.now_factory().astimezone(timezone.utc)
        age_days = (
            (now - last_activity).total_seconds() / 86400
            if last_activity is not None
            else None
        )
        if storage.storage_state is StorageState.HOT and not reasons:
            if run.state in _ARCHIVABLE_TERMINAL_STATES:
                if manual_archive or (
                    age_days is not None
                    and age_days >= self.policy.archive_after_days
                ):
                    action = "archive"
                else:
                    reasons.append("retention_window")
            elif run.state is RunState.ORPHANED:
                if process_status != "dead":
                    reasons.append("orphan_process_not_confirmed_dead")
                elif manual_archive or (
                    age_days is not None
                    and age_days >= self.policy.orphan_after_days
                ):
                    action = "archive"
                else:
                    reasons.append("orphan_retention_window")
            else:
                reasons.append("run_state_not_archivable")
        elif storage.storage_state is StorageState.ARCHIVED:
            if storage.trash_path:
                trashed_at = _parse_timestamp(storage.trashed_at)
                trash_age = (
                    (now - trashed_at).total_seconds() / 86400
                    if trashed_at is not None
                    else None
                )
                if (
                    not reasons
                    and trash_age is not None
                    and trash_age >= self.policy.trash_after_days
                ):
                    action = "purge_trash"
                else:
                    reasons.append("trash_grace_window")
            else:
                archived_at = _parse_timestamp(storage.archived_at)
                archive_age = (
                    (now - archived_at).total_seconds() / 86400
                    if archived_at is not None
                    else None
                )
                if run.run_id in (keep_full_ids or set()):
                    reasons.append("recent_project_evidence")
                elif not storage.archive_path or not Path(storage.archive_path).is_file():
                    reasons.append("archive_missing_or_unreadable")
                elif (
                    not reasons
                    and archive_age is not None
                    and archive_age >= self.policy.prune_after_days
                ):
                    has_heavy_logs = self._archive_contains_heavy_logs(storage)
                    if has_heavy_logs is True:
                        action = "prune_archive_logs"
                    elif has_heavy_logs is False:
                        reasons.append("archive_has_no_heavy_logs")
                    else:
                        reasons.append("archive_manifest_unreadable")
                else:
                    reasons.append("archive_prune_retention_window")
        elif storage.storage_state is not StorageState.HOT:
            reasons.append("storage_state:{}".format(storage.storage_state.value))

        snapshot_payload = {
            "run_id": run.run_id,
            "run_state": run.state.value,
            "pid": run.agent_pid,
            "process_start": run.agent_process_start,
            "process_status": process_status,
            "storage": asdict(storage),
            "directory": directory,
            "pending": pending,
            "referenced": run.run_id in references,
            "action": action,
            "reasons": reasons,
        }
        snapshot = hashlib.sha256(
            json.dumps(
                snapshot_payload,
                ensure_ascii=False,
                sort_keys=True,
                default=lambda value: value.value,
            ).encode("utf-8")
        ).hexdigest()
        return RunDecision(
            run_id=run.run_id,
            action=action,
            reasons=tuple(reasons),
            run_state=run.state.value,
            storage_state=storage.storage_state.value,
            size_bytes=int(directory.get("size_bytes", 0)),
            last_activity=last_activity.isoformat() if last_activity else None,
            snapshot=snapshot,
        )

    @staticmethod
    def _archive_contains_heavy_logs(
        storage: RunStorageRecord,
    ) -> Optional[bool]:
        if not storage.archive_path:
            return None
        archive_path = Path(storage.archive_path)
        try:
            with tarfile.open(archive_path, "r:gz") as archive:
                handle = archive.extractfile(_MANIFEST_NAME)
                if handle is None:
                    return None
                manifest = json.loads(handle.read().decode("utf-8"))
        except (OSError, ValueError, tarfile.TarError, KeyError):
            return None
        return any(
            RunGovernance._is_heavy_log(item.get("path", ""))
            for item in manifest.get("files", [])
            if isinstance(item, dict)
        )

    def list_decisions(self) -> List[RunDecision]:
        references = self._referenced_run_ids()
        keep_full_ids = self._recent_full_evidence_ids()
        return [
            self._decision(
                run,
                self.registry.get_storage(run.run_id),
                references=references,
                keep_full_ids=keep_full_ids,
            )
            for run in self.registry.list_runs()
        ]

    def _recent_full_evidence_ids(self) -> set[str]:
        limit = self.policy.keep_recent_per_project
        if limit <= 0:
            return set()
        grouped: Dict[str, List[Tuple[datetime, str]]] = {}
        for run in self.registry.list_runs():
            if run.state not in (
                _ARCHIVABLE_TERMINAL_STATES | {RunState.ORPHANED}
            ):
                continue
            storage = self.registry.get_storage(run.run_id)
            activity = self._latest_activity(Path(run.run_dir))
            if activity is None and storage.ledger_path:
                ledger_path = Path(storage.ledger_path)
                if ledger_path.is_file() and not ledger_path.is_symlink():
                    try:
                        ledger = json.loads(
                            ledger_path.read_text(encoding="utf-8")
                        )
                    except (ValueError, OSError):
                        ledger = {}
                    times = [
                        _parse_timestamp(item.get("timestamp"))
                        for item in ledger.get("timeline", [])
                        if isinstance(item, dict)
                    ]
                    activity = max(
                        (item for item in times if item is not None),
                        default=None,
                    )
            if activity is None:
                activity = (
                    _parse_timestamp(storage.archived_at)
                    or datetime.min.replace(tzinfo=timezone.utc)
                )
            project = run.project_slug or "__unbound__"
            grouped.setdefault(project, []).append((activity, run.run_id))
        retained = set()
        for candidates in grouped.values():
            candidates.sort(reverse=True)
            retained.update(run_id for _activity, run_id in candidates[:limit])
        return retained

    def unregistered_directories(self) -> List[str]:
        registered = {
            Path(run.run_dir).resolve()
            for run in self.registry.list_runs()
        }
        return sorted(
            str(path.resolve())
            for path in self.paths.runs.iterdir()
            if path.is_dir() and path.resolve() not in registered
        )

    def create_gc_plan(self, *, persist: bool = True) -> GcPlan:
        plan = GcPlan(
            plan_id="gc-" + uuid.uuid4().hex[:12],
            created_at=utc_now(),
            policy_version=POLICY_VERSION,
            decisions=tuple(self.list_decisions()),
            unregistered_directories=tuple(self.unregistered_directories()),
        )
        if persist:
            path = self.paths.maintenance / "gc-plan-{}.json".format(plan.plan_id)
            write_json_atomic(path, plan.to_dict())
            latest = self.paths.maintenance / "gc-plan-latest.json"
            write_json_atomic(latest, plan.to_dict())
        return plan

    def load_gc_plan(self, path: Optional[Path] = None) -> GcPlan:
        plan_path = (
            Path(path)
            if path is not None
            else self.paths.maintenance / "gc-plan-latest.json"
        )
        if plan_path.is_symlink() or not plan_path.is_file():
            raise GovernanceError(
                "no safe GC plan is available; run `loopweave gc --dry-run` first"
            )
        return GcPlan.from_dict(
            json.loads(plan_path.read_text(encoding="utf-8"))
        )

    def _manifest(self, run: RunRecord, run_dir: Path) -> dict:
        files = []
        for relative in _safe_relative_files(run_dir):
            path = run_dir / relative
            files.append(
                {
                    "path": relative.as_posix(),
                    "size": path.stat().st_size,
                    "sha256": _hash_file(path),
                }
            )
        return {
            "schema_version": 1,
            "policy_version": POLICY_VERSION,
            "run_id": run.run_id,
            "created_at": utc_now(),
            "files": files,
        }

    @staticmethod
    def _write_archive(
        run_dir: Path,
        archive_path: Path,
        manifest: dict,
    ) -> None:
        with tarfile.open(archive_path, "w:gz") as archive:
            for item in manifest["files"]:
                relative = Path(item["path"])
                archive.add(
                    run_dir / relative,
                    arcname=relative.as_posix(),
                    recursive=False,
                )
            manifest_data = (
                json.dumps(
                    manifest,
                    ensure_ascii=False,
                    indent=2,
                    sort_keys=True,
                )
                + "\n"
            ).encode("utf-8")
            info = tarfile.TarInfo(_MANIFEST_NAME)
            info.size = len(manifest_data)
            info.mode = 0o600
            archive.addfile(info, io.BytesIO(manifest_data))

    @staticmethod
    def _verify_archive(archive_path: Path) -> dict:
        with tarfile.open(archive_path, "r:gz") as archive:
            members = archive.getmembers()
            for member in members:
                member_path = Path(member.name)
                if (
                    member_path.is_absolute()
                    or ".." in member_path.parts
                    or not member.isfile()
                ):
                    raise GovernanceError(
                        "archive contains an unsafe member: {}".format(member.name)
                    )
            manifest_member = archive.getmember(_MANIFEST_NAME)
            manifest_handle = archive.extractfile(manifest_member)
            if manifest_handle is None:
                raise GovernanceError("archive manifest is unreadable")
            manifest = json.loads(manifest_handle.read().decode("utf-8"))
            expected = {
                item["path"]: (int(item["size"]), item["sha256"])
                for item in manifest.get("files", [])
            }
            actual_names = {
                member.name for member in members if member.name != _MANIFEST_NAME
            }
            if actual_names != set(expected):
                raise GovernanceError("archive content does not match its manifest")
            for name, (size, digest) in expected.items():
                member = archive.getmember(name)
                handle = archive.extractfile(member)
                if handle is None:
                    raise GovernanceError("archive member is unreadable: {}".format(name))
                data = handle.read()
                if len(data) != size or hashlib.sha256(data).hexdigest() != digest:
                    raise GovernanceError(
                        "archive member checksum mismatch: {}".format(name)
                    )
        return manifest

    def _ledger_payload(
        self,
        run: RunRecord,
        manifest: dict,
        *,
        archive_path: Path,
        archive_sha256: str,
    ) -> dict:
        run_dir = Path(run.run_dir)
        task_path = run_dir / "assigned-task-latest.md"
        task_digest = (
            _hash_file(task_path)
            if task_path.is_file() and not task_path.is_symlink()
            else None
        )
        timeline = []
        events_path = run_dir / "events.jsonl"
        if events_path.is_file() and not events_path.is_symlink():
            for line in events_path.read_text(
                encoding="utf-8", errors="replace"
            ).splitlines()[-500:]:
                try:
                    event = json.loads(line)
                except ValueError:
                    continue
                timeline.append(
                    {
                        "event": str(event.get("event", ""))[:96],
                        "timestamp": str(event.get("timestamp", ""))[:64],
                    }
                )
        return {
            "schema_version": 1,
            "policy_version": POLICY_VERSION,
            "run_id": run.run_id,
            "project_slug": run.project_slug,
            "workspace_fingerprint": hashlib.sha256(
                str(Path(run.workspace_root).resolve()).encode("utf-8")
            ).hexdigest(),
            "agent": run.agent,
            "run_state": run.state.value,
            "storage_state": StorageState.ARCHIVED.value,
            "task_sha256": task_digest,
            "referenced_run_ids": sorted(
                self._run_outbound_references(run_dir)
            ),
            "archived_at": utc_now(),
            "archive_name": archive_path.name,
            "archive_sha256": archive_sha256,
            "archive_manifest_sha256": hashlib.sha256(
                json.dumps(
                    manifest, ensure_ascii=False, sort_keys=True
                ).encode("utf-8")
            ).hexdigest(),
            "timeline": timeline[-200:],
        }

    def _archive_locked(
        self,
        run_id: str,
        *,
        expected_snapshot: Optional[str],
        manual: bool,
        operator: str,
        reason: str,
    ) -> Path:
        run = self.registry.get_run(run_id)
        storage = self.registry.get_storage(run_id)
        references = self._referenced_run_ids()
        decision = self._decision(
            run,
            storage,
            references=references,
            manual_archive=manual,
        )
        if expected_snapshot is not None and decision.snapshot != expected_snapshot:
            raise GovernanceError(
                "run {} changed since the GC plan was created".format(run_id)
            )
        if decision.action != "archive":
            raise GovernanceError(
                "run {} is protected: {}".format(
                    run_id, ", ".join(decision.reasons) or "not eligible"
                )
            )
        run_dir = Path(run.run_dir)
        if run_dir.resolve().parent != self.paths.runs.resolve():
            raise GovernanceError(
                "run directory is outside the configured hot-run root"
            )
        archiving = self.registry.transition_storage(
            run_id,
            expected_state=StorageState.HOT,
            expected_generation=storage.generation,
            new_state=StorageState.ARCHIVING,
            operator=operator,
            reason=reason,
        )
        temporary = self.paths.archives / ".{}.{}.tmp".format(
            run_id, uuid.uuid4().hex
        )
        final = self.paths.archives / "{}-g{}.tar.gz".format(
            run_id, archiving.generation
        )
        ledger_path = self.paths.ledger / "{}.json".format(run_id)
        try:
            manifest = self._manifest(run, run_dir)
            self._write_archive(run_dir, temporary, manifest)
            self._verify_archive(temporary)
            archive_digest = _hash_file(temporary)
            os.replace(str(temporary), str(final))
            ledger_payload = self._ledger_payload(
                run,
                manifest,
                archive_path=final,
                archive_sha256=archive_digest,
            )
            write_json_atomic(ledger_path, ledger_payload)
            archived = self.registry.transition_storage(
                run_id,
                expected_state=StorageState.ARCHIVING,
                expected_generation=archiving.generation,
                new_state=StorageState.ARCHIVED,
                operator=operator,
                reason=reason,
                updates={
                    "archive_path": str(final),
                    "archive_sha256": archive_digest,
                    "archive_size": final.stat().st_size,
                    "archived_at": utc_now(),
                    "ledger_path": str(ledger_path),
                    "policy_version": POLICY_VERSION,
                    "recovery_note": None,
                },
            )
            trash_path = self.paths.trash / "{}-g{}".format(
                run_id, archived.generation
            )
            os.replace(str(run_dir), str(trash_path))
            if storage.archive_path:
                previous_archive = Path(storage.archive_path)
                if (
                    previous_archive != final
                    and previous_archive.is_file()
                    and previous_archive.resolve().parent
                    == self.paths.archives.resolve()
                ):
                    previous_dir = trash_path / ".previous-archives"
                    previous_dir.mkdir(mode=0o700)
                    os.replace(
                        str(previous_archive),
                        str(previous_dir / previous_archive.name),
                    )
            self.registry.transition_storage(
                run_id,
                expected_state=StorageState.ARCHIVED,
                expected_generation=archived.generation,
                new_state=StorageState.ARCHIVED,
                operator=operator,
                reason="original moved into trash grace period",
                updates={
                    "trash_path": str(trash_path),
                    "trashed_at": utc_now(),
                },
            )
            self._remove_stale_socket(run)
            if _hash_file(final) != archive_digest:
                raise GovernanceError("archive checksum changed after commit")
            return final
        except Exception as error:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass
            current = self.registry.get_storage(run_id)
            if current.storage_state is StorageState.ARCHIVING:
                for incomplete in (final, ledger_path):
                    try:
                        incomplete.unlink()
                    except FileNotFoundError:
                        pass
                self.registry.transition_storage(
                    run_id,
                    expected_state=StorageState.ARCHIVING,
                    expected_generation=current.generation,
                    new_state=StorageState.HOT,
                    operator=operator,
                    reason="archive rolled back after failure",
                    updates={"recovery_note": str(error)[:512]},
                )
            elif current.storage_state is StorageState.ARCHIVED:
                self.registry.transition_storage(
                    run_id,
                    expected_state=StorageState.ARCHIVED,
                    expected_generation=current.generation,
                    new_state=StorageState.RECOVERY_REQUIRED,
                    operator=operator,
                    reason="archive transaction needs recovery",
                    updates={"recovery_note": str(error)[:512]},
                )
            raise

    def archive_run(
        self,
        run_id: str,
        *,
        operator: str = "manual",
        reason: str = "manual archive",
    ) -> Path:
        with self.maintenance_lock():
            return self._archive_locked(
                run_id,
                expected_snapshot=None,
                manual=True,
                operator=operator,
                reason=reason,
            )

    def _remove_stale_socket(self, run: RunRecord) -> None:
        if sys.platform == "win32":
            # Windows control endpoints are named pipes, not socket files;
            # there is no filesystem artifact to remove.
            return
        socket_path = Path(run.socket_path)
        if not socket_path.exists():
            return
        try:
            if (
                socket_path.resolve().parent != self.paths.var.resolve()
                or not stat.S_ISSOCK(socket_path.lstat().st_mode)
            ):
                return
        except OSError:
            return
        socket_path.unlink()

    def _extract_verified_archive(self, archive_path: Path, target: Path) -> None:
        manifest = self._verify_archive(archive_path)
        expected = {item["path"]: item for item in manifest["files"]}
        target.mkdir(parents=True, mode=0o700)
        with tarfile.open(archive_path, "r:gz") as archive:
            for name in sorted(expected):
                destination = target / name
                destination.parent.mkdir(parents=True, exist_ok=True)
                handle = archive.extractfile(name)
                if handle is None:
                    raise GovernanceError("archive member is unreadable: {}".format(name))
                data = handle.read()
                destination.write_bytes(data)
                os.chmod(destination, 0o600)
        restored_manifest = self._manifest(
            RunRecord(
                run_id=manifest["run_id"],
                codex_thread_id="archive",
                cwd=str(target),
                tty="",
                agent="archive",
                agent_pid=0,
                agent_process_start="",
                control_token="",
                state=RunState.STOPPED,
                run_dir=str(target),
            ),
            target,
        )
        restored_files = {
            item["path"]: (item["size"], item["sha256"])
            for item in restored_manifest["files"]
        }
        expected_files = {
            item["path"]: (item["size"], item["sha256"])
            for item in manifest["files"]
        }
        if restored_files != expected_files:
            raise GovernanceError("restored files do not match archive manifest")

    def restore_run(
        self,
        run_id: str,
        *,
        operator: str = "manual",
        reason: str = "manual restore",
    ) -> Path:
        with self.maintenance_lock():
            run = self.registry.get_run(run_id)
            storage = self.registry.get_storage(run_id)
            if storage.storage_state not in {
                StorageState.ARCHIVED,
                StorageState.LEDGER_ONLY,
            }:
                raise GovernanceError(
                    "run {} is not archived".format(run_id)
                )
            if not storage.archive_path or not storage.archive_sha256:
                raise GovernanceError("archive metadata is incomplete")
            archive_path = Path(storage.archive_path)
            if (
                archive_path.is_symlink()
                or not archive_path.is_file()
                or _hash_file(archive_path) != storage.archive_sha256
            ):
                raise GovernanceError("archive checksum verification failed")
            target = Path(run.run_dir)
            if target.exists():
                raise GovernanceError("hot run directory already exists")
            temporary = self.paths.runs / ".restore-{}-{}".format(
                run_id, uuid.uuid4().hex
            )
            try:
                self._extract_verified_archive(archive_path, temporary)
                os.replace(str(temporary), str(target))
                self.registry.transition_storage(
                    run_id,
                    expected_state=storage.storage_state,
                    expected_generation=storage.generation,
                    new_state=StorageState.HOT,
                    operator=operator,
                    reason=reason,
                    updates={
                        "trash_path": None,
                        "trashed_at": None,
                        "recovery_note": None,
                    },
                )
                if storage.trash_path:
                    trash_copy = Path(storage.trash_path)
                    if (
                        trash_copy.exists()
                        and trash_copy.resolve().parent
                        == self.paths.trash.resolve()
                    ):
                        import shutil

                        shutil.rmtree(trash_copy)
                return target
            except Exception:
                if temporary.exists():
                    for child in sorted(
                        temporary.rglob("*"), reverse=True
                    ):
                        if child.is_file():
                            child.unlink()
                        elif child.is_dir():
                            child.rmdir()
                    temporary.rmdir()
                raise

    def _purge_trash_locked(
        self,
        run_id: str,
        *,
        expected_snapshot: str,
        operator: str,
    ) -> None:
        run = self.registry.get_run(run_id)
        storage = self.registry.get_storage(run_id)
        decision = self._decision(
            run,
            storage,
            references=self._referenced_run_ids(),
        )
        if decision.snapshot != expected_snapshot or decision.action != "purge_trash":
            raise GovernanceError(
                "run {} changed since the GC plan was created".format(run_id)
            )
        if not storage.trash_path:
            raise GovernanceError("trash path is missing")
        trash_path = Path(storage.trash_path)
        if trash_path.resolve().parent != self.paths.trash.resolve():
            raise GovernanceError("trash path is outside the configured root")
        if trash_path.exists():
            import shutil

            shutil.rmtree(trash_path)
        self.registry.transition_storage(
            run_id,
            expected_state=StorageState.ARCHIVED,
            expected_generation=storage.generation,
            new_state=StorageState.ARCHIVED,
            operator=operator,
            reason="trash grace period elapsed",
            updates={"trash_path": None, "trashed_at": None},
        )

    @staticmethod
    def _is_heavy_log(path: str) -> bool:
        name = Path(path).name
        return any(
            name == base or name.startswith(base + ".")
            for base in _HEAVY_LOG_NAMES
        )

    def _prune_archive_logs_locked(
        self,
        run_id: str,
        *,
        expected_snapshot: str,
        operator: str,
    ) -> None:
        run = self.registry.get_run(run_id)
        storage = self.registry.get_storage(run_id)
        decision = self._decision(
            run,
            storage,
            references=self._referenced_run_ids(),
            keep_full_ids=self._recent_full_evidence_ids(),
        )
        if (
            decision.snapshot != expected_snapshot
            or decision.action != "prune_archive_logs"
        ):
            raise GovernanceError(
                "run {} changed since the GC plan was created".format(run_id)
            )
        if not storage.archive_path or not storage.archive_sha256:
            raise GovernanceError("archive metadata is incomplete")
        archive_path = Path(storage.archive_path)
        if (
            archive_path.is_symlink()
            or not archive_path.is_file()
            or _hash_file(archive_path) != storage.archive_sha256
        ):
            raise GovernanceError("archive checksum verification failed")
        manifest = self._verify_archive(archive_path)
        retained = [
            item
            for item in manifest["files"]
            if not self._is_heavy_log(item["path"])
        ]
        pruned = [
            item
            for item in manifest["files"]
            if self._is_heavy_log(item["path"])
        ]
        if not pruned:
            raise GovernanceError("archive contains no heavy logs to prune")
        updated_manifest = {
            **manifest,
            "created_at": utc_now(),
            "files": retained,
            "pruned_files": [
                {
                    "path": item["path"],
                    "original_size": item["size"],
                    "sha256": item["sha256"],
                    "pruned_at": utc_now(),
                }
                for item in pruned
            ],
        }
        temporary = archive_path.with_name(
            ".{}.{}.tmp".format(archive_path.name, uuid.uuid4().hex)
        )
        try:
            with tarfile.open(archive_path, "r:gz") as source, tarfile.open(
                temporary, "w:gz"
            ) as target:
                for item in retained:
                    handle = source.extractfile(item["path"])
                    if handle is None:
                        raise GovernanceError(
                            "archive member is unreadable: {}".format(item["path"])
                        )
                    data = handle.read()
                    info = tarfile.TarInfo(item["path"])
                    info.size = len(data)
                    info.mode = 0o600
                    target.addfile(info, io.BytesIO(data))
                manifest_data = (
                    json.dumps(
                        updated_manifest,
                        ensure_ascii=False,
                        indent=2,
                        sort_keys=True,
                    )
                    + "\n"
                ).encode("utf-8")
                info = tarfile.TarInfo(_MANIFEST_NAME)
                info.size = len(manifest_data)
                info.mode = 0o600
                target.addfile(info, io.BytesIO(manifest_data))
            self._verify_archive(temporary)
            new_digest = _hash_file(temporary)
            new_size = temporary.stat().st_size
            os.replace(str(temporary), str(archive_path))
            ledger_path = Path(storage.ledger_path) if storage.ledger_path else None
            if ledger_path is None or not ledger_path.is_file():
                raise GovernanceError("archive ledger is missing")
            ledger = json.loads(ledger_path.read_text(encoding="utf-8"))
            ledger["logs_pruned_at"] = utc_now()
            ledger["pruned_files"] = updated_manifest["pruned_files"]
            ledger["archive_sha256"] = new_digest
            write_json_atomic(ledger_path, ledger)
            self.registry.transition_storage(
                run_id,
                expected_state=StorageState.ARCHIVED,
                expected_generation=storage.generation,
                new_state=StorageState.ARCHIVED,
                operator=operator,
                reason="heavy log retention elapsed",
                updates={
                    "archive_sha256": new_digest,
                    "archive_size": new_size,
                    "policy_version": POLICY_VERSION,
                },
            )
        except Exception as error:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass
            current = self.registry.get_storage(run_id)
            if current.storage_state is StorageState.ARCHIVED:
                actual_digest = (
                    _hash_file(archive_path) if archive_path.is_file() else None
                )
                if actual_digest != current.archive_sha256:
                    self.registry.transition_storage(
                        run_id,
                        expected_state=StorageState.ARCHIVED,
                        expected_generation=current.generation,
                        new_state=StorageState.RECOVERY_REQUIRED,
                        operator=operator,
                        reason="archive pruning needs recovery",
                        updates={"recovery_note": str(error)[:512]},
                    )
            raise

    def apply_gc_plan(self, plan: GcPlan) -> ApplyResult:
        if plan.policy_version != POLICY_VERSION:
            raise GovernanceError("GC plan policy version is stale")
        applied: List[Tuple[str, str]] = []
        skipped: List[Tuple[str, str]] = []
        with self.maintenance_lock():
            if (
                tuple(self.unregistered_directories())
                != plan.unregistered_directories
            ):
                raise GovernanceError(
                    "unregistered run directories changed since the plan was created"
                )
            for decision in plan.decisions:
                if decision.action == "none":
                    skipped.append(
                        (
                            decision.run_id,
                            ", ".join(decision.reasons) or "no action",
                        )
                    )
                    continue
                if decision.action == "archive":
                    self._archive_locked(
                        decision.run_id,
                        expected_snapshot=decision.snapshot,
                        manual=False,
                        operator="gc",
                        reason="retention policy",
                    )
                elif decision.action == "purge_trash":
                    self._purge_trash_locked(
                        decision.run_id,
                        expected_snapshot=decision.snapshot,
                        operator="gc",
                    )
                elif decision.action == "prune_archive_logs":
                    self._prune_archive_logs_locked(
                        decision.run_id,
                        expected_snapshot=decision.snapshot,
                        operator="gc",
                    )
                else:
                    raise GovernanceError(
                        "unsupported GC action: {}".format(decision.action)
                    )
                applied.append((decision.run_id, decision.action))
        result = ApplyResult(
            plan_id=plan.plan_id,
            applied=tuple(applied),
            skipped=tuple(skipped),
        )
        write_json_atomic(
            self.paths.maintenance / "gc-result-latest.json",
            result.to_dict(),
        )
        return result

    def pin(self, run_id: str, reason: str) -> RunStorageRecord:
        bounded = reason.strip()
        if not bounded:
            raise GovernanceError("pin reason is required")
        if len(bounded.encode("utf-8")) > 512:
            raise GovernanceError("pin reason exceeds 512 bytes")
        return self.registry.set_pin(
            run_id,
            pinned=True,
            reason=bounded,
            operator="manual",
        )

    def unpin(self, run_id: str) -> RunStorageRecord:
        return self.registry.set_pin(
            run_id,
            pinned=False,
            reason=None,
            operator="manual",
        )


def render_decisions(decisions: Sequence[RunDecision]) -> str:
    rows = [
        "RUN ID\tRUN STATE\tSTORAGE\tACTION\tSIZE\tPROTECTION / REASON"
    ]
    for decision in decisions:
        rows.append(
            "{}\t{}\t{}\t{}\t{}\t{}".format(
                decision.run_id,
                decision.run_state,
                decision.storage_state,
                decision.action,
                decision.size_bytes,
                ", ".join(decision.reasons) or "-",
            )
        )
    return "\n".join(rows)
