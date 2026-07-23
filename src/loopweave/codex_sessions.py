from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional


class SessionDiscoveryError(RuntimeError):
    pass


class ThreadNotFound(SessionDiscoveryError):
    pass


class AmbiguousThread(SessionDiscoveryError):
    pass


@dataclass(frozen=True)
class CodexThread:
    thread_id: str
    cwd: str
    timestamp: datetime
    session_path: Path


def _parse_timestamp(value: str) -> datetime:
    if value.endswith("Z"):
        value = value[:-1] + "+00:00"
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _read_session_meta(path: Path) -> Optional[CodexThread]:
    try:
        with path.open("r", encoding="utf-8") as handle:
            first_line = handle.readline()
            first_record = json.loads(first_line)
            latest_timestamp = _parse_timestamp(first_record["timestamp"])
            for line in handle:
                try:
                    record = json.loads(line)
                    value = record.get("timestamp")
                    if value:
                        latest_timestamp = max(
                            latest_timestamp, _parse_timestamp(value)
                        )
                except (ValueError, TypeError, json.JSONDecodeError):
                    continue
        if first_record.get("type") != "session_meta":
            return None
        payload = first_record["payload"]
        return CodexThread(
            thread_id=payload["id"],
            cwd=str(Path(payload["cwd"]).resolve()),
            timestamp=latest_timestamp,
            session_path=path,
        )
    except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError):
        return None


def list_threads(sessions_dir: Path) -> List[CodexThread]:
    sessions = []
    for path in Path(sessions_dir).rglob("*.jsonl"):
        session = _read_session_meta(path)
        if session is not None:
            sessions.append(session)
    return sessions


def discover_thread(
    sessions_dir: Path,
    cwd: str,
    explicit_thread_id: Optional[str] = None,
    now: Optional[datetime] = None,
    max_age_seconds: int = 6 * 60 * 60,
) -> CodexThread:
    sessions = list_threads(sessions_dir)
    if explicit_thread_id:
        matches = [
            session
            for session in sessions
            if session.thread_id == explicit_thread_id
        ]
        if len(matches) == 1:
            return matches[0]
        raise ThreadNotFound(
            "Codex thread {} was not found".format(explicit_thread_id)
        )

    current_time = now or datetime.now(timezone.utc)
    resolved_cwd = str(Path(cwd).resolve())
    cwd_matches = [
        session
        for session in sessions
        if session.cwd == resolved_cwd
    ]
    recent_matches = [
        session
        for session in cwd_matches
        if 0
        <= (current_time - session.timestamp).total_seconds()
        <= max_age_seconds
    ]
    if len(recent_matches) == 1:
        return recent_matches[0]
    if len(recent_matches) > 1:
        ids = ", ".join(
            sorted(session.thread_id for session in recent_matches)
        )
        raise AmbiguousThread(
            "Multiple recent Codex threads match cwd {}: {}; pass --thread".format(
                resolved_cwd, ids
            )
        )
    if len(cwd_matches) == 1:
        return cwd_matches[0]
    if not cwd_matches:
        raise ThreadNotFound(
            "No Codex thread found for cwd {}; pass --thread explicitly".format(
                resolved_cwd
            )
        )
    ids = ", ".join(sorted(session.thread_id for session in cwd_matches))
    raise AmbiguousThread(
        "Multiple Codex threads match cwd {}: {}; pass --thread".format(
            resolved_cwd, ids
        )
    )
