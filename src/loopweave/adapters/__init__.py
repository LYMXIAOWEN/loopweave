from __future__ import annotations

from pathlib import Path
from typing import List, Optional

from .base import AgentAdapter
from .claude import ClaudeAdapter
from .generic import GenericAdapter


class UnknownAdapter(ValueError):
    pass


def get_adapter(
    name: str,
    extra_args: List[str],
    run_id: str,
    workspace_root: Optional[Path] = None,
    baseline_path: Optional[Path] = None,
) -> AgentAdapter:
    if name == "claude":
        return ClaudeAdapter(
            run_id=run_id,
            extra_args=extra_args,
            workspace_root=workspace_root,
            baseline_path=baseline_path,
        )
    return GenericAdapter([name, *extra_args])
