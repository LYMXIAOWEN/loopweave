from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path
from typing import List


class AgentAdapter(ABC):
    id = "base"

    @abstractmethod
    def build_command(self, run_dir: Path) -> List[str]:
        raise NotImplementedError

    def format_review_input(self, verdict: str, review_text: str) -> str:
        return (
            "\n[LoopWeave review: {verdict}]\n{review}\n"
            "Continue from this review. If a user decision is required, stop and explain it.\n"
        ).format(verdict=verdict, review=review_text.rstrip())

    def review_input_sequence(self, verdict: str, review_text: str) -> List[str]:
        return [self.format_review_input(verdict, review_text), "\r"]

    def approved_input_sequence(self, summary: str) -> List[str]:
        message = (
            "\n[LoopWeave review: approved]\n"
            "The reviewer has approved this work. The current task is complete.\n"
            "Stop revising it and preserve the final artifacts and verification "
            "evidence.\n\n"
            "{summary}\n"
        ).format(summary=summary.strip())
        return [message, "\r"]

    def stage_approved_input_sequence(self, summary: str) -> List[str]:
        message = (
            "\n[LoopWeave review: stage approved]\n"
            "The reviewer approved the current stage. Preserve its final artifacts and "
            "continue to the next stage in the approved plan.\n\n"
            "{summary}\n\n"
            "Intermediate reports are nonterminal by default. You may put "
            "LOOPWEAVE_STAGE on its own line to make that explicit. Only when "
            "the entire requested plan is complete, put LOOPWEAVE_FINAL on its "
            "own line.\n"
        ).format(summary=summary.strip())
        return [message, "\r"]
