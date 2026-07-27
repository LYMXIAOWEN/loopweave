from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Optional


class RunState(str, Enum):
    CREATED = "created"
    BINDING = "binding"
    RUNNING = "running"
    READY_FOR_REVIEW = "ready_for_review"
    REVIEWING = "reviewing"
    REVIEW_READY = "review_ready"
    DELIVERING = "delivering"
    WORKER_CONTINUING = "worker_continuing"
    OWNER_REVIEW_PENDING = "owner_review_pending"
    APPROVED = "approved"
    NEEDS_HUMAN = "needs_human"
    FAILED = "failed"
    STOPPED = "stopped"
    ORPHANED = "orphaned"


class RunMode(str, Enum):
    DEVELOP = "develop"
    DESIGN = "design"


class ReviewBackend(str, Enum):
    EPHEMERAL = "ephemeral"
    VISIBLE_THREAD = "visible-thread"


class StorageState(str, Enum):
    HOT = "hot"
    ARCHIVING = "archiving"
    ARCHIVED = "archived"
    TRASH = "trash"
    LEDGER_ONLY = "ledger_only"
    PURGED = "purged"
    RECOVERY_REQUIRED = "recovery_required"


TERMINAL_STATES = {
    RunState.APPROVED,
    RunState.NEEDS_HUMAN,
    RunState.FAILED,
    RunState.STOPPED,
    RunState.ORPHANED,
}


@dataclass(frozen=True)
class RunRecord:
    run_id: str
    codex_thread_id: str
    cwd: str
    tty: str
    agent: str
    agent_pid: int
    agent_process_start: str
    control_token: str
    state: RunState
    mode: RunMode = RunMode.DEVELOP
    thread_cwd: str = ""
    workspace_root: str = ""
    project_slug: Optional[str] = None
    project_root: Optional[str] = None
    reviewer_backend: ReviewBackend = ReviewBackend.EPHEMERAL
    reviewer_thread_id: Optional[str] = None
    reviewer_thread_cwd: Optional[str] = None
    reviewer_generation: int = 1
    review_loop: int = 0
    socket_path: str = ""
    run_dir: str = ""
    pending_codex_thread_id: Optional[str] = None
    binding_generation: int = 1

    def __post_init__(self) -> None:
        if not self.thread_cwd:
            object.__setattr__(self, "thread_cwd", self.cwd)
        if not self.workspace_root:
            object.__setattr__(self, "workspace_root", self.cwd)


@dataclass(frozen=True)
class ThreadBinding:
    run_id: str
    generation: int
    thread_id: str
    attached_at: str
    detached_at: Optional[str] = None
    detach_reason: Optional[str] = None


@dataclass(frozen=True)
class RunStorageRecord:
    run_id: str
    storage_state: StorageState = StorageState.HOT
    pinned: bool = False
    pin_reason: Optional[str] = None
    archive_path: Optional[str] = None
    archive_sha256: Optional[str] = None
    archive_size: Optional[int] = None
    archived_at: Optional[str] = None
    trash_path: Optional[str] = None
    trashed_at: Optional[str] = None
    ledger_path: Optional[str] = None
    policy_version: str = "1"
    generation: int = 1
    last_transition_at: Optional[str] = None
    recovery_note: Optional[str] = None


@dataclass(frozen=True)
class BridgeBinding:
    thread_id: str
    generation: int
    nonce_hash: str
    protocol_version: int
