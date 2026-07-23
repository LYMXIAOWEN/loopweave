from __future__ import annotations

import time
from pathlib import Path
from typing import Callable, Dict, List

from .models import RunRecord
from .protocol import append_event, write_json_atomic
from . import terminal_host


class CompletionNotifier:
    def __init__(
        self,
        sender: Callable = None,
        process_start: Callable[[int], str] = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.sender = sender if sender is not None else terminal_host.default_control_sender()
        self.process_start = process_start if process_start is not None else terminal_host.default_process_identity_reader()
        self.sleep = sleep

    def notify(
        self,
        run: RunRecord,
        review: Dict[str, object],
        worker_inputs: List[str],
    ) -> Dict[str, bool]:
        worker_notified = self.notify_worker(run, review, worker_inputs)
        owner_notified = self.notify_owner(run, review)
        return {
            "worker_notified": worker_notified,
            "owner_notified": owner_notified,
        }

    def notify_worker(
        self,
        run: RunRecord,
        review: Dict[str, object],
        inputs: List[str],
    ) -> bool:
        run_dir = Path(run.run_dir)
        marker = run_dir / "worker-approved-review-id"
        review_id = str(review["review_id"])
        if marker.exists() and marker.read_text(encoding="utf-8").strip() == review_id:
            return True
        try:
            if self.process_start(run.agent_pid) != run.agent_process_start:
                raise RuntimeError("managed Agent process identity changed")
            for index, text in enumerate(inputs):
                if index:
                    self.sleep(0.35)
                response = self.sender(
                    Path(run.socket_path),
                    {
                        "token": run.control_token,
                        "action": "send",
                        "text": text,
                    },
                )
                if response.get("status") != "ok":
                    raise RuntimeError(
                        response.get("message", "worker notification failed")
                    )
            marker.write_text(review_id + "\n", encoding="utf-8")
            self._clear_failure(
                run_dir, "worker_approval_notification_failed"
            )
            append_event(
                run_dir / "events.jsonl",
                {
                    "event": "worker_approval_notified",
                    "review_id": review_id,
                },
            )
            return True
        except Exception as error:
            self._record_failure(
                run_dir,
                "worker_approval_notification_failed",
                review_id,
                error,
            )
            return False

    def notify_owner(
        self,
        run: RunRecord,
        review: Dict[str, object],
    ) -> bool:
        run_dir = Path(run.run_dir)
        marker = run_dir / "owner-completion-review-id"
        review_id = str(review["review_id"])
        if marker.exists() and marker.read_text(encoding="utf-8").strip() == review_id:
            return True

        metadata_path = run_dir / "completion-notification.json"
        try:
            write_json_atomic(
                metadata_path,
                self._completion_metadata(run, review, owner_notified=False),
            )
            marker.write_text(review_id + "\n", encoding="utf-8")
            self._clear_failure(
                run_dir, "owner_completion_notification_failed"
            )
            append_event(
                run_dir / "events.jsonl",
                {
                    "event": "owner_review_pending",
                    "review_id": review_id,
                    "codex_thread_id": run.codex_thread_id,
                },
            )
            return True
        except Exception as error:
            self._record_failure(
                run_dir,
                "owner_completion_notification_failed",
                review_id,
                error,
            )
            return False

    @staticmethod
    def _completion_metadata(
        run: RunRecord,
        review: Dict[str, object],
        owner_notified: bool,
    ) -> Dict[str, object]:
        run_dir = Path(run.run_dir)
        worker_marker = run_dir / "worker-approved-review-id"
        review_id = str(review["review_id"])
        return {
            "schema_version": 1,
            "run_id": run.run_id,
            "review_id": review_id,
            "verdict": "approved",
            "summary": review["summary"],
            "review_file": review["review_file"],
            "codex_thread_id": run.codex_thread_id,
            "binding_generation": run.binding_generation,
            "project_slug": run.project_slug,
            "project_root": run.project_root,
            "workspace_root": run.workspace_root,
            "thread_cwd": run.thread_cwd,
            "worker_notified": (
                worker_marker.exists()
                and worker_marker.read_text(encoding="utf-8").strip()
                == review_id
            ),
            "owner_notified": owner_notified,
            "owner_review_pending": True,
            "owner_action_required": (
                "Review the bounded artifacts, then run "
                "loopweave finalize --run-id {run_id} --approve or "
                "loopweave finalize --run-id {run_id} --changes-requested "
                "--message-file <path> from the visible owner thread."
            ).format(run_id=run.run_id),
        }

    @staticmethod
    def _record_failure(
        run_dir: Path,
        event: str,
        review_id: str,
        error: Exception,
    ) -> None:
        message = "{}: {}".format(event, error)
        (run_dir / "completion-notification-error.txt").write_text(
            message + "\n", encoding="utf-8"
        )
        append_event(
            run_dir / "events.jsonl",
            {
                "event": event,
                "review_id": review_id,
                "error": str(error),
            },
        )

    @staticmethod
    def _clear_failure(run_dir: Path, event: str) -> None:
        path = run_dir / "completion-notification-error.txt"
        try:
            if path.read_text(encoding="utf-8").startswith(event + ":"):
                path.unlink()
        except FileNotFoundError:
            pass
