from __future__ import annotations

import os
import shutil
from pathlib import Path


SOURCE_ROOT = Path(__file__).resolve().parents[2]


def _configured_path(name: str, default: Path) -> Path:
    value = os.environ.get(name, "").strip()
    return Path(value).expanduser().resolve() if value else default.resolve()


PROJECT_ROOT = _configured_path("LOOPWEAVE_HOME", SOURCE_ROOT)
PROJECTS_DIR = PROJECT_ROOT / "projects"
RUNS_DIR = PROJECT_ROOT / "runs"
VAR_DIR = PROJECT_ROOT / "var"
REGISTRY_PATH = VAR_DIR / "registry.sqlite"
CODEX_HOME = _configured_path("CODEX_HOME", Path.home() / ".codex")
CODEX_SESSIONS_DIR = CODEX_HOME / "sessions"
CODEX_BIN = Path("/Applications/Codex.app/Contents/Resources/codex")
DESKTOP_CODEX_BIN = Path(
    "/Applications/ChatGPT.app/Contents/Resources/codex"
)
SESSION_MAX_AGE_SECONDS = 6 * 60 * 60


def resolve_codex_bin() -> Path:
    """Find an executable Codex CLI without relying on one app bundle path."""
    override = os.environ.get("LOOPWEAVE_CODEX_BIN", "").strip()
    if override:
        candidate = Path(override).expanduser()
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return candidate
        raise RuntimeError(
            "LOOPWEAVE_CODEX_BIN is not an executable file: {}".format(candidate)
        )

    candidates = [
        DESKTOP_CODEX_BIN,
        CODEX_BIN,
        Path(shutil.which("codex") or ""),
    ]
    for candidate in candidates:
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return candidate
    raise RuntimeError(
        "Codex executable is unavailable; set LOOPWEAVE_CODEX_BIN or install codex"
    )


def ensure_runtime_dirs() -> None:
    PROJECTS_DIR.mkdir(parents=True, exist_ok=True)
    RUNS_DIR.mkdir(parents=True, exist_ok=True)
    VAR_DIR.mkdir(parents=True, exist_ok=True)
