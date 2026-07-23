from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from .protocol import ProtocolError, read_json, write_json_atomic


class ProjectWorkspaceError(RuntimeError):
    pass


class InvalidProjectSlug(ProjectWorkspaceError):
    pass


class WorkspaceNotFound(ProjectWorkspaceError):
    pass


class ProjectWorkspaceConflict(ProjectWorkspaceError):
    pass


@dataclass(frozen=True)
class ProjectWorkspace:
    project_slug: str
    project_root: Path
    workspace_root: Path


def validate_project_slug(value: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(
        r"[a-z0-9][a-z0-9._-]{0,63}", value
    ):
        raise InvalidProjectSlug(str(value))
    return value


def resolve_project_workspace(
    projects_dir: Path,
    project_slug: str,
    workspace: Optional[Path],
) -> ProjectWorkspace:
    slug = validate_project_slug(project_slug)
    root = Path(projects_dir).expanduser().resolve()
    project_root = (root / slug).resolve()
    if project_root.parent != root:
        raise InvalidProjectSlug(slug)
    project_root.mkdir(parents=True, exist_ok=True)
    workspace_root = (
        Path(workspace).expanduser().resolve() if workspace else project_root
    )
    if not workspace_root.is_dir():
        raise WorkspaceNotFound(str(workspace_root))

    expected = {
        "schema_version": 1,
        "project_slug": slug,
        "project_root": str(project_root),
        "workspace_root": str(workspace_root),
    }
    binding_path = project_root / "project.json"
    if binding_path.exists():
        try:
            actual = read_json(binding_path)
        except (OSError, ValueError, ProtocolError) as error:
            raise ProjectWorkspaceConflict(slug) from error
        if actual != expected:
            raise ProjectWorkspaceConflict(slug)
    else:
        write_json_atomic(binding_path, expected)
    return ProjectWorkspace(slug, project_root, workspace_root)
