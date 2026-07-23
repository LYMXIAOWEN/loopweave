from __future__ import annotations

from pathlib import Path
from typing import List

from .base import AgentAdapter


class GenericAdapter(AgentAdapter):
    id = "generic"

    def __init__(self, command: List[str]) -> None:
        if not command:
            raise ValueError("generic adapter requires a command")
        self.command = list(command)

    def build_command(self, run_dir: Path) -> List[str]:
        return list(self.command)
