from __future__ import annotations

import json
import shlex
from pathlib import Path
from typing import List, Optional

from .base import AgentAdapter


CLAUDE_PROTOCOL_PROMPT = """
You are the worker Agent in a LoopWeave managed terminal session.
The reviewer checks each completed work turn in an isolated review session.
The bound Codex thread identifies the owning collaboration, but review
evidence is not appended to that thread. Keep changes and verification
evidence in the current project. When the reviewer's feedback arrives, continue in this
same session.
If you need a decision or clarification from the user instead of review, include
the exact marker LOOPWEAVE_NEEDS_HUMAN in your final message.
For staged development, when an intermediate task or stage is ready for review,
an unmarked final report remains nonterminal by default. You may include
LOOPWEAVE_STAGE on its own line to make that intent explicit. Only when the
entire requested plan is complete, include the exact marker LOOPWEAVE_FINAL on
its own line.
Completion markers must be a plain line containing only the marker: no Markdown
backticks, heading, bullet, or adjacent text. If the reviewer asks you to resubmit a
completion after review, emit the requested marker even without code changes.
Do not inspect unrelated historical Codex or Claude conversations.
""".strip()


class ClaudeAdapter(AgentAdapter):
    id = "claude"

    def __init__(
        self,
        run_id: str,
        extra_args: Optional[List[str]] = None,
        launcher: str = "loopweave",
        workspace_root: Optional[Path] = None,
        baseline_path: Optional[Path] = None,
    ) -> None:
        self.run_id = run_id
        self.extra_args = list(extra_args or [])
        self.launcher = launcher
        self.workspace_root = (
            Path(workspace_root).resolve() if workspace_root else None
        )
        self.baseline_path = (
            Path(baseline_path).resolve() if baseline_path else None
        )

    def build_command(self, run_dir: Path) -> List[str]:
        settings_path = Path(run_dir) / "claude-settings.json"
        hook_command = "{} hook claude-stop --run-id {}".format(
            shlex.quote(self.launcher), shlex.quote(self.run_id)
        )
        settings = {
            "hooks": {
                "Stop": [
                    {
                        "hooks": [
                            {
                                "type": "command",
                                "command": hook_command,
                                "timeout": 1800,
                            }
                        ]
                    }
                ]
            }
        }
        settings_path.parent.mkdir(parents=True, exist_ok=True)
        settings_path.write_text(
            json.dumps(settings, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        prompt = CLAUDE_PROTOCOL_PROMPT
        if self.workspace_root and self.baseline_path:
            prompt += """

The authoritative task workspace is `{workspace}`.
Preserve all pre-existing changes recorded in `{baseline}`.
Do not reset, clean, checkout, or overwrite unrelated files.
Constrain edits to the approved task and report touched files and tests.
""".format(
                workspace=self.workspace_root,
                baseline=self.baseline_path,
            ).rstrip()
        return [
            "claude",
            "--settings",
            str(settings_path),
            "--append-system-prompt",
            prompt,
        ] + self.extra_args
