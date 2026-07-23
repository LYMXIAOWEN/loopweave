#!/usr/bin/env python3
"""Codex Desktop host bridge for visible LoopWeave review delivery."""

from __future__ import annotations

import json
import os
import sys
import time
import uuid
from pathlib import Path
from typing import Any

from mcp.server.fastmcp import Context, FastMCP
from mcp.types import ToolAnnotations


LOOPWEAVE_SOURCE_ROOT = Path(
    os.environ.get("LOOPWEAVE_SOURCE_ROOT")
    or Path(__file__).resolve().parents[3]
).expanduser().resolve()
LOOPWEAVE_HOME = Path(
    os.environ.get("LOOPWEAVE_HOME") or LOOPWEAVE_SOURCE_ROOT
).expanduser().resolve()
LOOPWEAVE_SRC = LOOPWEAVE_SOURCE_ROOT / "src"
if str(LOOPWEAVE_SRC) not in sys.path:
    sys.path.insert(0, str(LOOPWEAVE_SRC))

from loopweave.bridge_protocol import (  # noqa: E402
    BridgeProtocol,
    BridgeProtocolError,
)
from loopweave.bridge_control import (  # noqa: E402
    BridgeController,
    VisibleReviewDispatcher,
)
from loopweave.config import CODEX_SESSIONS_DIR  # noqa: E402
from loopweave.registry import Registry  # noqa: E402


PLUGIN_ROOT = Path(__file__).resolve().parents[1]
WIDGET_RESOURCE_URI = "ui://loopweave-visible-bridge/bridge.html"
WIDGET_PATH = PLUGIN_ROOT / "widget" / "index.html"
SOURCE_WIDGET_PATH = (
    LOOPWEAVE_SOURCE_ROOT / "plugins" / "loopweave-visible-bridge" / "widget" / "index.html"
)
MAX_WAIT_MS = 30_000

READ_ONLY_ANNOTATIONS = {
    "readOnlyHint": True,
    "openWorldHint": False,
    "destructiveHint": False,
}
DELIVERY_ANNOTATIONS = {
    "readOnlyHint": False,
    "openWorldHint": False,
    "destructiveHint": False,
    "idempotentHint": True,
}
APP_ONLY_META = {
    "ui": {
        "resourceUri": WIDGET_RESOURCE_URI,
        "visibility": ["app"],
    }
}


def tool_contracts() -> dict[str, dict[str, Any]]:
    """Return transport-independent declarations used by tests and MCP setup."""
    base = {
        "annotations": dict(READ_ONLY_ANNOTATIONS),
        "_meta": {"ui": {"visibility": ["app"]}},
    }
    return {
        "open_visible_bridge": {
            "annotations": dict(READ_ONLY_ANNOTATIONS),
            "_meta": {"ui": {"resourceUri": WIDGET_RESOURCE_URI}},
            "inputSchema": {"type": "object", "properties": {}},
        },
        "bridge_status": {
            **base,
            "inputSchema": {"type": "object", "properties": {}},
        },
        "wait_for_review": {
            **base,
            "inputSchema": {
                "type": "object",
                "properties": {
                    "timeout_ms": {
                        "type": "integer",
                        "minimum": 0,
                        "maximum": MAX_WAIT_MS,
                        "default": 1_000,
                    }
                },
                "additionalProperties": False,
            },
        },
        "resume_visible_delivery": {
            "annotations": dict(DELIVERY_ANNOTATIONS),
            "_meta": {"ui": {"visibility": ["app"]}},
            "inputSchema": {
                "type": "object",
                "properties": {},
                "additionalProperties": False,
            },
        },
    }


def widget_resource() -> dict[str, Any]:
    widget_path = WIDGET_PATH if WIDGET_PATH.is_file() else SOURCE_WIDGET_PATH
    return {
        "uri": WIDGET_RESOURCE_URI,
        "mimeType": "text/html;profile=mcp-app",
        "text": widget_path.read_text(encoding="utf-8"),
        "_meta": {
            "ui": {
                "csp": {
                    "connectDomains": [],
                    "resourceDomains": [],
                }
            }
        },
    }


class BridgeService:
    def __init__(self, protocol: BridgeProtocol, dispatcher: Any = None) -> None:
        self.protocol = protocol
        self.dispatcher = dispatcher

    def _binding_and_proof(
        self, metadata: dict[str, Any]
    ) -> tuple[Any, dict[str, Any]]:
        binding = self.protocol.load_binding()
        proof = prove_host_identity(
            metadata,
            expected_thread_id=binding.thread_id,
        )
        return binding, proof

    def status(self, metadata: dict[str, Any]) -> dict[str, Any]:
        try:
            binding, proof = self._binding_and_proof(metadata)
        except BridgeProtocolError as error:
            return {
                "status": "unbound",
                "identity_proven": False,
                "reason": str(error),
            }
        if not proof["identity_proven"]:
            return {
                "status": "identity_rejected",
                "identity_proven": False,
                "reason": proof["reason"],
            }
        outcome = self.protocol.status(binding)
        result: dict[str, Any] = {
            "status": outcome.status,
            "identity_proven": True,
        }
        if outcome.run_ids:
            result["run_ids"] = list(outcome.run_ids)
        if outcome.review_id:
            result["review_id"] = outcome.review_id
        return result

    def wait(
        self, metadata: dict[str, Any], *, timeout_ms: int = 1_000
    ) -> dict[str, Any]:
        if not isinstance(timeout_ms, int) or not 0 <= timeout_ms <= MAX_WAIT_MS:
            raise BridgeProtocolError("timeout_ms is outside the allowed range")
        binding, proof = self._binding_and_proof(metadata)
        if not proof["identity_proven"]:
            return {
                "status": "identity_rejected",
                "identity_proven": False,
                "reason": proof["reason"],
            }
        deadline = time.monotonic() + timeout_ms / 1_000
        while True:
            outcome = self.protocol.wait(binding)
            if outcome.status != "idle" or time.monotonic() >= deadline:
                result: dict[str, Any] = {
                    "status": outcome.status,
                    "identity_proven": True,
                }
                if outcome.run_ids:
                    result["run_ids"] = list(outcome.run_ids)
                if outcome.review_id:
                    result["review_id"] = outcome.review_id
                return result
            time.sleep(min(0.25, max(0.0, deadline - time.monotonic())))

    def resume_delivery(self, metadata: dict[str, Any]) -> dict[str, Any]:
        binding, proof = self._binding_and_proof(metadata)
        if not proof["identity_proven"]:
            return {
                "status": "identity_rejected",
                "identity_proven": False,
                "reason": proof["reason"],
            }
        outcome = self.protocol.wait(binding)
        if outcome.status != "queued":
            return _status_payload(outcome, identity_proven=True)
        if len(outcome.run_ids) != 1 or outcome.review_id is None:
            raise BridgeProtocolError("queued review identity is incomplete")

        matches = [
            run
            for run in self.protocol.registry.list_runs()
            if run.run_id == outcome.run_ids[0]
        ]
        if len(matches) != 1:
            raise BridgeProtocolError("queued review run is unavailable")
        run = matches[0]
        card_path = (
            Path(run.run_dir)
            / "review-inbox"
            / f"{outcome.review_id}.json"
        )
        try:
            card = json.loads(card_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise BridgeProtocolError("queued review card is unreadable") from error
        if not isinstance(card, dict):
            raise BridgeProtocolError("queued review card is invalid")
        if card.get("review_id") != outcome.review_id:
            raise BridgeProtocolError("queued review card identity mismatch")
        if self.dispatcher is None:
            raise BridgeProtocolError("Desktop visible dispatcher is unavailable")
        delivered = self.dispatcher(run, card)
        return _status_payload(delivered, identity_proven=True)


def _status_payload(outcome: Any, *, identity_proven: bool) -> dict[str, Any]:
    result: dict[str, Any] = {
        "status": outcome.status,
        "identity_proven": identity_proven,
    }
    if outcome.run_ids:
        result["run_ids"] = list(outcome.run_ids)
    if outcome.review_id:
        result["review_id"] = outcome.review_id
    return result

def _request_meta(ctx: Context) -> dict[str, Any]:
    """Expose only bounded metadata during the identity feasibility spike."""
    try:
        meta = ctx.request_context.meta
    except (AttributeError, ValueError):
        return {}
    if meta is None:
        return {}
    if hasattr(meta, "model_dump"):
        value = meta.model_dump(mode="json")
    else:
        value = vars(meta)
    encoded = json.dumps(value, ensure_ascii=True, sort_keys=True)
    if len(encoded.encode("utf-8")) > 4_096:
        return {"truncated": True}
    return value


def _canonical_thread_id(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = uuid.UUID(value)
    except (ValueError, AttributeError):
        return None
    canonical = str(parsed)
    return canonical if value == canonical else None


def prove_host_identity(
    metadata: dict[str, Any], *, expected_thread_id: str | None
) -> dict[str, Any]:
    """Verify Desktop-owned task metadata against the owner binding."""
    expected = _canonical_thread_id(expected_thread_id)
    snake = _canonical_thread_id(metadata.get("thread_id"))
    camel = _canonical_thread_id(metadata.get("threadId"))
    if expected is None:
        reason = "binding_missing_or_invalid"
    elif snake is None or camel is None:
        reason = "host_identity_missing_or_invalid"
    elif snake != camel:
        reason = "host_identity_conflict"
    elif snake != expected:
        reason = "host_identity_mismatch"
    else:
        return {
            "identity_proven": True,
            "thread_id": expected,
            "reason": "matched",
        }
    return {"identity_proven": False, "thread_id": None, "reason": reason}


mcp = FastMCP(
    "loopweave-visible-bridge",
    instructions="Local same-task bridge for visible LoopWeave reviews.",
)
annotations = ToolAnnotations(**READ_ONLY_ANNOTATIONS)
delivery_annotations = ToolAnnotations(**DELIVERY_ANNOTATIONS)
_SERVICE: BridgeService | None = None


def _production_service() -> BridgeService:
    global _SERVICE
    if _SERVICE is None:
        registry = Registry(LOOPWEAVE_HOME / "var" / "registry.sqlite")
        controller = BridgeController(
            root=LOOPWEAVE_HOME,
            registry=registry,
            sessions_dir=CODEX_SESSIONS_DIR,
        )
        _SERVICE = BridgeService(
            controller.protocol,
            dispatcher=VisibleReviewDispatcher(controller=controller),
        )
    return _SERVICE


@mcp.resource(
    WIDGET_RESOURCE_URI,
    mime_type="text/html;profile=mcp-app",
    meta=widget_resource()["_meta"],
)
def visible_bridge_widget() -> str:
    return widget_resource()["text"]


@mcp.tool(
    annotations=annotations,
    meta={"ui": {"resourceUri": WIDGET_RESOURCE_URI}},
)
def open_visible_bridge() -> dict[str, Any]:
    """Mount the local bridge host in the current visible task."""
    return {
        "mode": "visible-review-bridge",
        "host_identity_required": True,
        "review_content_exposed": False,
    }


@mcp.tool(annotations=annotations, meta=APP_ONLY_META)
def bridge_status(ctx: Context) -> dict[str, Any]:
    """Return bounded bridge status for the verified host task."""
    return _production_service().status(_request_meta(ctx))


@mcp.tool(annotations=annotations, meta=APP_ONLY_META)
def wait_for_review(ctx: Context, timeout_ms: int = 1_000) -> dict[str, Any]:
    """Wait model-free for one eligible review or a fail-closed state."""
    return _production_service().wait(_request_meta(ctx), timeout_ms=timeout_ms)


@mcp.tool(annotations=delivery_annotations, meta=APP_ONLY_META)
def resume_visible_delivery(ctx: Context) -> dict[str, Any]:
    """Resume one queued review through the owner-verified Desktop IPC path."""
    return _production_service().resume_delivery(_request_meta(ctx))


if __name__ == "__main__":
    mcp.run(transport="stdio")
