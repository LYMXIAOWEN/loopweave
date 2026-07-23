import importlib.util
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PLUGIN_ROOT = ROOT / "plugins" / "loopweave-visible-bridge"
MARKETPLACE_PATH = ROOT / ".agents" / "plugins" / "marketplace.json"


def _load_server_module():
    server_path = PLUGIN_ROOT / "server" / "bridge_server.py"
    spec = importlib.util.spec_from_file_location("visible_bridge_server", server_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_plugin_manifest_declares_only_local_mcp_server():
    manifest = json.loads(
        (PLUGIN_ROOT / ".codex-plugin" / "plugin.json").read_text(encoding="utf-8")
    )
    assert manifest["name"] == "loopweave-visible-bridge"
    assert manifest["mcpServers"] == "./.mcp.json"
    assert "apps" not in manifest

    mcp = json.loads((PLUGIN_ROOT / ".mcp.json").read_text(encoding="utf-8"))
    assert set(mcp["mcpServers"]) == {"loopweave-visible-bridge"}
    server = mcp["mcpServers"]["loopweave-visible-bridge"]
    assert server["command"] == "python3"
    assert server["args"] == ["./server/bridge_server.py"]
    assert server["cwd"] == "."
    assert not any(key in server for key in ("url", "httpUrl", "headers"))


def test_repo_marketplace_points_to_repo_plugin_source():
    marketplace = json.loads(MARKETPLACE_PATH.read_text(encoding="utf-8"))
    assert marketplace["name"] == "loopweave-local"
    assert len(marketplace["plugins"]) == 1
    entry = marketplace["plugins"][0]
    assert entry == {
        "name": "loopweave-visible-bridge",
        "source": {"source": "local", "path": "./plugins/loopweave-visible-bridge"},
        "policy": {
            "installation": "AVAILABLE",
            "authentication": "ON_INSTALL",
        },
        "category": "Productivity",
    }


def test_tool_contracts_are_app_only_and_bounded():
    module = _load_server_module()
    contracts = module.tool_contracts()

    assert set(contracts) == {
        "open_visible_bridge",
        "bridge_status",
        "wait_for_review",
        "resume_visible_delivery",
    }
    bootstrap = contracts["open_visible_bridge"]
    assert "visibility" not in bootstrap["_meta"]["ui"]
    assert bootstrap["_meta"]["ui"]["resourceUri"] == module.WIDGET_RESOURCE_URI

    for name in ("bridge_status", "wait_for_review"):
        contract = contracts[name]
        assert contract["_meta"]["ui"]["visibility"] == ["app"]
        assert contract["annotations"] == {
            "readOnlyHint": True,
            "openWorldHint": False,
            "destructiveHint": False,
        }

    resume = contracts["resume_visible_delivery"]
    assert resume["_meta"]["ui"]["visibility"] == ["app"]
    assert resume["annotations"] == {
        "readOnlyHint": False,
        "openWorldHint": False,
        "destructiveHint": False,
        "idempotentHint": True,
    }
    assert resume["inputSchema"] == {
        "type": "object",
        "properties": {},
        "additionalProperties": False,
    }

    timeout = contracts["wait_for_review"]["inputSchema"]["properties"]["timeout_ms"]
    assert timeout["minimum"] == 0
    assert timeout["maximum"] <= 30_000
    assert timeout["default"] <= timeout["maximum"]


def test_bootstrap_tool_is_read_only_and_returns_no_production_data():
    module = _load_server_module()
    result = module.open_visible_bridge()

    assert result == {
        "mode": "visible-review-bridge",
        "host_identity_required": True,
        "review_content_exposed": False,
    }


def test_widget_resource_uses_mcp_app_mime_and_no_external_network():
    module = _load_server_module()
    resource = module.widget_resource()

    assert resource["mimeType"] == "text/html;profile=mcp-app"
    assert resource["uri"] == module.WIDGET_RESOURCE_URI
    assert resource["_meta"]["ui"]["csp"] == {
        "connectDomains": [],
        "resourceDomains": [],
    }

    html = resource["text"]
    assert "window.openai.callTool" in html
    assert "window.openai.sendFollowUpMessage" not in html
    assert "begin_dispatch" not in html
    assert "ack_dispatch" not in html
    assert 'callTool("resume_visible_delivery", {})' in html
    for forbidden in ("fetch(", "WebSocket(", "EventSource(", "XMLHttpRequest"):
        assert forbidden not in html


def test_host_identity_requires_both_desktop_thread_fields_to_match_binding():
    module = _load_server_module()
    thread_id = "11111111-1111-4111-8111-111111111111"

    proof = module.prove_host_identity(
        {"thread_id": thread_id, "threadId": thread_id},
        expected_thread_id=thread_id,
    )

    assert proof == {
        "identity_proven": True,
        "thread_id": thread_id,
        "reason": "matched",
    }


def test_host_identity_fails_closed_for_missing_malformed_or_mismatched_fields():
    module = _load_server_module()
    expected = "11111111-1111-4111-8111-111111111111"
    other = "22222222-2222-4222-8222-222222222222"
    cases = [
        {},
        {"thread_id": expected},
        {"threadId": expected},
        {"thread_id": expected, "threadId": other},
        {"thread_id": other, "threadId": other},
        {"thread_id": "not-a-thread", "threadId": "not-a-thread"},
    ]

    for metadata in cases:
        proof = module.prove_host_identity(
            metadata,
            expected_thread_id=expected,
        )
        assert proof["identity_proven"] is False
        assert proof["thread_id"] is None
        assert proof["reason"] != "matched"
