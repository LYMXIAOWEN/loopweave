from __future__ import annotations

import hashlib
import subprocess
from pathlib import Path
from typing import Dict, List

from .protocol import utc_now


MAX_PATCH_BYTES = 512 * 1024
MAX_UNTRACKED = 500


def _git(workspace: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(workspace), *args],
        text=True,
        capture_output=True,
        check=False,
    )


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _untracked_metadata(repo_root: Path, relative: str) -> Dict[str, object]:
    path = repo_root / relative
    resolved = path.resolve()
    if resolved != repo_root and repo_root not in resolved.parents:
        raise ValueError("untracked path escapes repository")
    if not resolved.is_file():
        return {
            "path": relative,
            "size": None,
            "sha256": None,
        }
    return {
        "path": relative,
        "size": resolved.stat().st_size,
        "sha256": _file_sha256(resolved),
    }


def capture_workspace_baseline(workspace: Path) -> Dict[str, object]:
    root = Path(workspace).expanduser().resolve()
    probe = _git(root, "rev-parse", "--show-toplevel")
    payload: Dict[str, object] = {
        "schema_version": 1,
        "captured_at": utc_now(),
        "workspace_root": str(root),
        "is_git": probe.returncode == 0,
    }
    if probe.returncode != 0:
        return payload

    repo_root = Path(probe.stdout.strip()).resolve()
    head = _git(repo_root, "rev-parse", "HEAD")
    branch = _git(repo_root, "branch", "--show-current")
    status = _git(
        repo_root,
        "status",
        "--porcelain=v1",
        "--untracked-files=no",
    )
    patch = _git(repo_root, "diff", "--binary", "--no-ext-diff")
    untracked_values: List[str] = [
        value
        for value in _git(
            repo_root,
            "ls-files",
            "--others",
            "--exclude-standard",
            "-z",
        ).stdout.split("\0")
        if value
    ]
    patch_bytes = patch.stdout.encode("utf-8")
    bounded_patch = patch_bytes[:MAX_PATCH_BYTES].decode(
        "utf-8", errors="replace"
    )
    payload.update(
        {
            "repo_root": str(repo_root),
            "head": head.stdout.strip() if head.returncode == 0 else None,
            "branch": branch.stdout.strip() or None,
            "tracked_status": status.stdout,
            "tracked_patch": bounded_patch,
            "tracked_patch_truncated": len(patch_bytes) > MAX_PATCH_BYTES,
            "untracked": [
                _untracked_metadata(repo_root, value)
                for value in untracked_values[:MAX_UNTRACKED]
            ],
            "untracked_truncated": len(untracked_values) > MAX_UNTRACKED,
        }
    )
    return payload
