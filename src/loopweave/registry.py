from __future__ import annotations

import json
import sqlite3
from dataclasses import replace
from pathlib import Path
from typing import Dict, Optional, Set

from . import protocol
from .models import (
    ReviewBackend,
    RunMode,
    RunRecord,
    RunStorageRecord,
    RunState,
    StorageState,
    ThreadBinding,
)


class RegistryError(RuntimeError):
    pass


class RunNotFound(RegistryError):
    pass


class InvalidTransition(RegistryError):
    pass


class PendingAttachConflict(RegistryError):
    pass


ALLOWED_TRANSITIONS: Dict[RunState, Set[RunState]] = {
    RunState.CREATED: {RunState.BINDING, RunState.FAILED, RunState.STOPPED},
    RunState.BINDING: {RunState.RUNNING, RunState.FAILED, RunState.STOPPED},
    RunState.RUNNING: {
        RunState.READY_FOR_REVIEW,
        RunState.FAILED,
        RunState.STOPPED,
        RunState.ORPHANED,
    },
    RunState.READY_FOR_REVIEW: {
        RunState.REVIEWING,
        RunState.FAILED,
        RunState.STOPPED,
    },
    RunState.REVIEWING: {
        RunState.REVIEW_READY,
        RunState.NEEDS_HUMAN,
        RunState.FAILED,
        RunState.STOPPED,
    },
    RunState.REVIEW_READY: {
        RunState.DELIVERING,
        RunState.OWNER_REVIEW_PENDING,
        RunState.APPROVED,
        RunState.NEEDS_HUMAN,
        RunState.FAILED,
        RunState.STOPPED,
    },
    RunState.OWNER_REVIEW_PENDING: {
        RunState.DELIVERING,
        RunState.APPROVED,
        RunState.NEEDS_HUMAN,
        RunState.FAILED,
        RunState.STOPPED,
        RunState.ORPHANED,
    },
    RunState.DELIVERING: {
        RunState.WORKER_CONTINUING,
        RunState.ORPHANED,
        RunState.FAILED,
        RunState.STOPPED,
    },
    RunState.WORKER_CONTINUING: {
        RunState.READY_FOR_REVIEW,
        RunState.RUNNING,
        RunState.FAILED,
        RunState.STOPPED,
    },
}


class Registry:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(str(self.path))
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA foreign_keys=ON")
        return connection

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS runs (
                    run_id TEXT PRIMARY KEY,
                    codex_thread_id TEXT NOT NULL,
                    cwd TEXT NOT NULL,
                    tty TEXT NOT NULL,
                    agent TEXT NOT NULL,
                    agent_pid INTEGER NOT NULL,
                    agent_process_start TEXT NOT NULL,
                    control_token TEXT NOT NULL,
                    state TEXT NOT NULL,
                    review_loop INTEGER NOT NULL DEFAULT 0,
                    socket_path TEXT NOT NULL DEFAULT '',
                    run_dir TEXT NOT NULL DEFAULT '',
                    pending_codex_thread_id TEXT NULL,
                    binding_generation INTEGER NOT NULL DEFAULT 1
                )
                """
            )
            columns = {
                row["name"]
                for row in connection.execute("PRAGMA table_info(runs)").fetchall()
            }
            if "pending_codex_thread_id" not in columns:
                connection.execute(
                    "ALTER TABLE runs ADD COLUMN pending_codex_thread_id TEXT NULL"
                )
            if "binding_generation" not in columns:
                connection.execute(
                    """
                    ALTER TABLE runs
                    ADD COLUMN binding_generation INTEGER NOT NULL DEFAULT 1
                    """
                )
            if "thread_cwd" not in columns:
                connection.execute(
                    "ALTER TABLE runs ADD COLUMN thread_cwd TEXT NOT NULL DEFAULT ''"
                )
            if "workspace_root" not in columns:
                connection.execute(
                    """
                    ALTER TABLE runs
                    ADD COLUMN workspace_root TEXT NOT NULL DEFAULT ''
                    """
                )
            if "project_slug" not in columns:
                connection.execute(
                    "ALTER TABLE runs ADD COLUMN project_slug TEXT NULL"
                )
            if "project_root" not in columns:
                connection.execute(
                    "ALTER TABLE runs ADD COLUMN project_root TEXT NULL"
                )
            if "mode" not in columns:
                connection.execute(
                    """
                    ALTER TABLE runs
                    ADD COLUMN mode TEXT NOT NULL DEFAULT 'develop'
                    """
                )
            if "reviewer_backend" not in columns:
                connection.execute(
                    """
                    ALTER TABLE runs
                    ADD COLUMN reviewer_backend TEXT NOT NULL DEFAULT 'ephemeral'
                    """
                )
            if "reviewer_thread_id" not in columns:
                connection.execute(
                    "ALTER TABLE runs ADD COLUMN reviewer_thread_id TEXT NULL"
                )
            if "reviewer_thread_cwd" not in columns:
                connection.execute(
                    "ALTER TABLE runs ADD COLUMN reviewer_thread_cwd TEXT NULL"
                )
            if "reviewer_generation" not in columns:
                connection.execute(
                    """
                    ALTER TABLE runs
                    ADD COLUMN reviewer_generation INTEGER NOT NULL DEFAULT 1
                    """
                )
            connection.execute(
                "UPDATE runs SET thread_cwd = cwd WHERE thread_cwd = ''"
            )
            connection.execute(
                "UPDATE runs SET workspace_root = cwd WHERE workspace_root = ''"
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS run_thread_bindings (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id TEXT NOT NULL,
                    generation INTEGER NOT NULL,
                    thread_id TEXT NOT NULL,
                    attached_at TEXT NOT NULL,
                    detached_at TEXT NULL,
                    detach_reason TEXT NULL,
                    UNIQUE(run_id, generation),
                    FOREIGN KEY(run_id) REFERENCES runs(run_id)
                )
                """
            )
            connection.execute(
                """
                INSERT INTO run_thread_bindings (
                    run_id, generation, thread_id, attached_at
                )
                SELECT run_id, 1, codex_thread_id, ?
                FROM runs
                WHERE NOT EXISTS (
                    SELECT 1
                    FROM run_thread_bindings
                    WHERE run_thread_bindings.run_id = runs.run_id
                )
                """,
                (protocol.utc_now(),),
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS run_storage (
                    run_id TEXT PRIMARY KEY,
                    storage_state TEXT NOT NULL DEFAULT 'hot',
                    pinned INTEGER NOT NULL DEFAULT 0,
                    pin_reason TEXT NULL,
                    archive_path TEXT NULL,
                    archive_sha256 TEXT NULL,
                    archive_size INTEGER NULL,
                    archived_at TEXT NULL,
                    trash_path TEXT NULL,
                    trashed_at TEXT NULL,
                    ledger_path TEXT NULL,
                    policy_version TEXT NOT NULL DEFAULT '1',
                    generation INTEGER NOT NULL DEFAULT 1,
                    last_transition_at TEXT NULL,
                    recovery_note TEXT NULL,
                    FOREIGN KEY(run_id) REFERENCES runs(run_id)
                )
                """
            )
            connection.execute(
                """
                INSERT OR IGNORE INTO run_storage (
                    run_id, storage_state, pinned, policy_version,
                    generation, last_transition_at
                )
                SELECT run_id, 'hot', 0, '1', 1, ?
                FROM runs
                """,
                (protocol.utc_now(),),
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS maintenance_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id TEXT NULL,
                    event TEXT NOT NULL,
                    occurred_at TEXT NOT NULL,
                    operator TEXT NOT NULL,
                    reason TEXT NULL,
                    details_json TEXT NOT NULL DEFAULT '{}'
                )
                """
            )
        self.path.chmod(0o600)

    def create_run(self, run: RunRecord) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO runs (
                    run_id, codex_thread_id, cwd, tty, agent, agent_pid,
                    agent_process_start, control_token, state, review_loop,
                    socket_path, run_dir, pending_codex_thread_id,
                    binding_generation, thread_cwd, workspace_root,
                    project_slug, project_root, mode, reviewer_backend,
                    reviewer_thread_id, reviewer_thread_cwd,
                    reviewer_generation
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run.run_id,
                    run.codex_thread_id,
                    run.cwd,
                    run.tty,
                    run.agent,
                    run.agent_pid,
                    run.agent_process_start,
                    run.control_token,
                    run.state.value,
                    run.review_loop,
                    run.socket_path,
                    run.run_dir,
                    None,
                    1,
                    run.thread_cwd,
                    run.workspace_root,
                    run.project_slug,
                    run.project_root,
                    run.mode.value,
                    run.reviewer_backend.value,
                    run.reviewer_thread_id,
                    run.reviewer_thread_cwd,
                    run.reviewer_generation,
                ),
            )
            connection.execute(
                """
                INSERT INTO run_storage (
                    run_id, storage_state, pinned, policy_version,
                    generation, last_transition_at
                ) VALUES (?, ?, 0, '1', 1, ?)
                """,
                (run.run_id, StorageState.HOT.value, protocol.utc_now()),
            )
            connection.execute(
                """
                INSERT INTO run_thread_bindings (
                    run_id, generation, thread_id, attached_at
                ) VALUES (?, ?, ?, ?)
                """,
                (
                    run.run_id,
                    1,
                    run.codex_thread_id,
                    protocol.utc_now(),
                ),
            )

    def run_columns(self) -> Set[str]:
        with self._connect() as connection:
            rows = connection.execute("PRAGMA table_info(runs)").fetchall()
        return {row["name"] for row in rows}

    def list_thread_bindings(self, run_id: str) -> list[ThreadBinding]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT run_id, generation, thread_id, attached_at,
                       detached_at, detach_reason
                FROM run_thread_bindings
                WHERE run_id = ?
                ORDER BY generation
                """,
                (run_id,),
            ).fetchall()
        return [
            ThreadBinding(
                run_id=row["run_id"],
                generation=row["generation"],
                thread_id=row["thread_id"],
                attached_at=row["attached_at"],
                detached_at=row["detached_at"],
                detach_reason=row["detach_reason"],
            )
            for row in rows
        ]

    def get_run(self, run_id: str) -> RunRecord:
        with self._connect() as connection:
            return self._get_run(connection, run_id)

    def _get_run(
        self, connection: sqlite3.Connection, run_id: str
    ) -> RunRecord:
        row = connection.execute(
            "SELECT * FROM runs WHERE run_id = ?", (run_id,)
        ).fetchone()
        if row is None:
            raise RunNotFound(run_id)
        return self._row_to_record(row)

    @staticmethod
    def _begin_immediate(connection: sqlite3.Connection) -> None:
        connection.execute("BEGIN IMMEDIATE")

    def queue_thread_attach(
        self, run_id: str, target_thread_id: str
    ) -> RunRecord:
        with self._connect() as connection:
            self._begin_immediate(connection)
            run = self._get_run(connection, run_id)
            if target_thread_id == run.codex_thread_id:
                return run
            if target_thread_id == run.pending_codex_thread_id:
                return run
            if run.pending_codex_thread_id is not None:
                raise PendingAttachConflict(run.pending_codex_thread_id)
            connection.execute(
                """
                UPDATE runs
                SET pending_codex_thread_id = ?
                WHERE run_id = ?
                """,
                (target_thread_id, run_id),
            )
            return replace(run, pending_codex_thread_id=target_thread_id)

    def attach_thread_now(
        self, run_id: str, target_thread_id: str, reason: str
    ) -> RunRecord:
        with self._connect() as connection:
            self._begin_immediate(connection)
            run = self._get_run(connection, run_id)
            if target_thread_id == run.codex_thread_id:
                return run
            if (
                run.pending_codex_thread_id is not None
                and run.pending_codex_thread_id != target_thread_id
            ):
                raise PendingAttachConflict(run.pending_codex_thread_id)
            self._switch_thread_binding(
                connection, run, target_thread_id, reason
            )
            return self._get_run(connection, run_id)

    def apply_pending_thread_attach(
        self, run_id: str, reason: str
    ) -> Optional[RunRecord]:
        with self._connect() as connection:
            self._begin_immediate(connection)
            run = self._get_run(connection, run_id)
            target_thread_id = run.pending_codex_thread_id
            if target_thread_id is None:
                return None
            self._switch_thread_binding(
                connection, run, target_thread_id, reason
            )
            return self._get_run(connection, run_id)

    def cancel_pending_thread_attach(self, run_id: str) -> Optional[str]:
        with self._connect() as connection:
            self._begin_immediate(connection)
            run = self._get_run(connection, run_id)
            target_thread_id = run.pending_codex_thread_id
            if target_thread_id is None:
                return None
            connection.execute(
                """
                UPDATE runs
                SET pending_codex_thread_id = NULL
                WHERE run_id = ?
                """,
                (run_id,),
            )
            return target_thread_id

    @staticmethod
    def _switch_thread_binding(
        connection: sqlite3.Connection,
        run: RunRecord,
        target_thread_id: str,
        reason: str,
    ) -> None:
        next_generation = run.binding_generation + 1
        timestamp = protocol.utc_now()
        closed = connection.execute(
            """
            UPDATE run_thread_bindings
            SET detached_at = ?, detach_reason = ?
            WHERE run_id = ? AND generation = ? AND thread_id = ?
                AND detached_at IS NULL
            """,
            (
                timestamp,
                reason,
                run.run_id,
                run.binding_generation,
                run.codex_thread_id,
            ),
        )
        if closed.rowcount != 1:
            raise RegistryError(
                "active thread binding history is missing or already detached"
            )
        connection.execute(
            """
            UPDATE runs
            SET codex_thread_id = ?, pending_codex_thread_id = NULL,
                binding_generation = ?
            WHERE run_id = ?
            """,
            (target_thread_id, next_generation, run.run_id),
        )
        connection.execute(
            """
            INSERT INTO run_thread_bindings (
                run_id, generation, thread_id, attached_at
            ) VALUES (?, ?, ?, ?)
            """,
            (
                run.run_id,
                next_generation,
                target_thread_id,
                timestamp,
            ),
        )

    def list_runs(self) -> list:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM runs ORDER BY rowid DESC"
            ).fetchall()
        return [self._row_to_record(row) for row in rows]

    def get_storage(self, run_id: str) -> RunStorageRecord:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM run_storage WHERE run_id = ?", (run_id,)
            ).fetchone()
        if row is None:
            raise RunNotFound(run_id)
        return self._row_to_storage(row)

    def list_storage(self) -> list[RunStorageRecord]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM run_storage ORDER BY rowid DESC"
            ).fetchall()
        return [self._row_to_storage(row) for row in rows]

    def set_pin(
        self,
        run_id: str,
        *,
        pinned: bool,
        reason: Optional[str],
        operator: str,
    ) -> RunStorageRecord:
        timestamp = protocol.utc_now()
        with self._connect() as connection:
            self._begin_immediate(connection)
            self._get_run(connection, run_id)
            changed = connection.execute(
                """
                UPDATE run_storage
                SET pinned = ?, pin_reason = ?, generation = generation + 1,
                    last_transition_at = ?
                WHERE run_id = ?
                """,
                (1 if pinned else 0, reason if pinned else None, timestamp, run_id),
            )
            if changed.rowcount != 1:
                raise RegistryError("run storage row is missing")
            connection.execute(
                """
                INSERT INTO maintenance_events (
                    run_id, event, occurred_at, operator, reason, details_json
                ) VALUES (?, ?, ?, ?, ?, '{}')
                """,
                (
                    run_id,
                    "run_pinned" if pinned else "run_unpinned",
                    timestamp,
                    operator,
                    reason,
                ),
            )
        return self.get_storage(run_id)

    def transition_storage(
        self,
        run_id: str,
        *,
        expected_state: StorageState,
        expected_generation: int,
        new_state: StorageState,
        operator: str,
        reason: Optional[str] = None,
        updates: Optional[Dict[str, object]] = None,
    ) -> RunStorageRecord:
        allowed_columns = {
            "archive_path",
            "archive_sha256",
            "archive_size",
            "archived_at",
            "trash_path",
            "trashed_at",
            "ledger_path",
            "policy_version",
            "recovery_note",
        }
        values = dict(updates or {})
        unknown = set(values) - allowed_columns
        if unknown:
            raise RegistryError(
                "unsupported storage update columns: {}".format(
                    ", ".join(sorted(unknown))
                )
            )
        timestamp = protocol.utc_now()
        assignments = [
            "storage_state = ?",
            "generation = generation + 1",
            "last_transition_at = ?",
        ]
        parameters: list[object] = [new_state.value, timestamp]
        for key, value in values.items():
            assignments.append("{} = ?".format(key))
            parameters.append(value)
        parameters.extend([run_id, expected_state.value, expected_generation])
        with self._connect() as connection:
            self._begin_immediate(connection)
            changed = connection.execute(
                """
                UPDATE run_storage
                SET {}
                WHERE run_id = ? AND storage_state = ? AND generation = ?
                """.format(", ".join(assignments)),
                parameters,
            )
            if changed.rowcount != 1:
                raise RegistryError(
                    "run storage changed since the plan was created"
                )
            connection.execute(
                """
                INSERT INTO maintenance_events (
                    run_id, event, occurred_at, operator, reason, details_json
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    "storage_{}_to_{}".format(
                        expected_state.value, new_state.value
                    ),
                    timestamp,
                    operator,
                    reason,
                    json.dumps(values, ensure_ascii=False, sort_keys=True),
                ),
            )
        return self.get_storage(run_id)

    def list_maintenance_events(self, run_id: Optional[str] = None) -> list[dict]:
        with self._connect() as connection:
            if run_id is None:
                rows = connection.execute(
                    "SELECT * FROM maintenance_events ORDER BY id"
                ).fetchall()
            else:
                rows = connection.execute(
                    """
                    SELECT * FROM maintenance_events
                    WHERE run_id = ? ORDER BY id
                    """,
                    (run_id,),
                ).fetchall()
        return [dict(row) for row in rows]

    def transition(self, run_id: str, new_state: RunState) -> RunRecord:
        current = self.get_run(run_id)
        allowed = ALLOWED_TRANSITIONS.get(current.state, set())
        if new_state not in allowed:
            raise InvalidTransition(
                "{} -> {} is not allowed".format(current.state.value, new_state.value)
            )
        with self._connect() as connection:
            connection.execute(
                "UPDATE runs SET state = ? WHERE run_id = ?",
                (new_state.value, run_id),
            )
        return replace(current, state=new_state)

    def force_state(self, run_id: str, new_state: RunState) -> RunRecord:
        current = self.get_run(run_id)
        with self._connect() as connection:
            connection.execute(
                "UPDATE runs SET state = ? WHERE run_id = ?",
                (new_state.value, run_id),
            )
        return replace(current, state=new_state)

    def increment_review_loop(self, run_id: str) -> int:
        current = self.get_run(run_id)
        next_value = current.review_loop + 1
        with self._connect() as connection:
            connection.execute(
                "UPDATE runs SET review_loop = ? WHERE run_id = ?",
                (next_value, run_id),
            )
        return next_value

    def bind_reviewer_thread(
        self,
        run_id: str,
        thread_id: str,
        thread_cwd: str,
    ) -> RunRecord:
        current = self.get_run(run_id)
        next_generation = current.reviewer_generation + 1
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE runs
                SET reviewer_backend = ?, reviewer_thread_id = ?,
                    reviewer_thread_cwd = ?, reviewer_generation = ?
                WHERE run_id = ?
                """,
                (
                    ReviewBackend.VISIBLE_THREAD.value,
                    thread_id,
                    thread_cwd,
                    next_generation,
                    run_id,
                ),
            )
        return self.get_run(run_id)

    def set_reviewer_binding(
        self,
        run_id: str,
        thread_id: str,
        thread_cwd: str,
        *,
        generation: int,
    ) -> RunRecord:
        current = self.get_run(run_id)
        if generation <= current.reviewer_generation:
            raise ValueError("reviewer generation must be newer")
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE runs
                SET reviewer_backend = ?, reviewer_thread_id = ?,
                    reviewer_thread_cwd = ?, reviewer_generation = ?
                WHERE run_id = ?
                """,
                (
                    ReviewBackend.VISIBLE_THREAD.value,
                    thread_id,
                    thread_cwd,
                    generation,
                    run_id,
                ),
            )
        return self.get_run(run_id)

    def process_identity_matches(
        self, run_id: str, pid: int, process_start: str
    ) -> bool:
        current = self.get_run(run_id)
        return (
            current.agent_pid == pid
            and current.agent_process_start == process_start
        )

    @staticmethod
    def _row_to_record(row: sqlite3.Row) -> RunRecord:
        return RunRecord(
            run_id=row["run_id"],
            codex_thread_id=row["codex_thread_id"],
            cwd=row["cwd"],
            tty=row["tty"],
            agent=row["agent"],
            agent_pid=row["agent_pid"],
            agent_process_start=row["agent_process_start"],
            control_token=row["control_token"],
            state=RunState(row["state"]),
            thread_cwd=row["thread_cwd"],
            workspace_root=row["workspace_root"],
            project_slug=row["project_slug"],
            project_root=row["project_root"],
            mode=RunMode(row["mode"] or RunMode.DEVELOP.value),
            reviewer_backend=ReviewBackend(
                row["reviewer_backend"] or ReviewBackend.EPHEMERAL.value
            ),
            reviewer_thread_id=row["reviewer_thread_id"],
            reviewer_thread_cwd=row["reviewer_thread_cwd"],
            reviewer_generation=row["reviewer_generation"],
            review_loop=row["review_loop"],
            socket_path=row["socket_path"],
            run_dir=row["run_dir"],
            pending_codex_thread_id=row["pending_codex_thread_id"],
            binding_generation=row["binding_generation"],
        )

    @staticmethod
    def _row_to_storage(row: sqlite3.Row) -> RunStorageRecord:
        return RunStorageRecord(
            run_id=row["run_id"],
            storage_state=StorageState(row["storage_state"]),
            pinned=bool(row["pinned"]),
            pin_reason=row["pin_reason"],
            archive_path=row["archive_path"],
            archive_sha256=row["archive_sha256"],
            archive_size=row["archive_size"],
            archived_at=row["archived_at"],
            trash_path=row["trash_path"],
            trashed_at=row["trashed_at"],
            ledger_path=row["ledger_path"],
            policy_version=row["policy_version"],
            generation=row["generation"],
            last_transition_at=row["last_transition_at"],
            recovery_note=row["recovery_note"],
        )
