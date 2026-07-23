from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
WIDGET = ROOT / "plugins/loopweave-visible-bridge/widget/index.html"


def test_widget_uses_only_host_verified_desktop_dispatch():
    html = WIDGET.read_text(encoding="utf-8")

    assert "wait_for_review" in html
    assert "pollInFlight" in html
    assert "document.visibilityState" in html
    assert "setInterval" not in html
    assert 'callTool("resume_visible_delivery", {})' in html
    assert html.index('result.status === "queued"') < html.index(
        'callTool("resume_visible_delivery", {})'
    )
    for forbidden in (
        "begin_dispatch",
        "sendFollowUpMessage",
        "ack_dispatch",
        "dispatchInFlight",
        "ownerId",
    ):
        assert forbidden not in html


def test_widget_never_renders_card_contents_or_uses_external_transport():
    html = WIDGET.read_text(encoding="utf-8")

    for forbidden in (
        "work_summary",
        "changed_files",
        "artifact_paths",
        "workspace_root",
        "fetch(",
        "WebSocket(",
        "EventSource(",
        "XMLHttpRequest",
    ):
        assert forbidden not in html


def test_widget_is_status_only_and_has_no_manual_button():
    html = WIDGET.read_text(encoding="utf-8")

    assert "wait_for_review" in html
    assert "<button" not in html
    assert "setInterval" not in html
    for forbidden in (
        "begin_dispatch",
        "sendFollowUpMessage",
        "ack_dispatch",
        "work_summary",
        "changed_files",
        "workspace_root",
        "fetch(",
        "WebSocket(",
        "EventSource(",
    ):
        assert forbidden not in html


def test_unbound_retries_but_identity_mismatch_stops_widget():
    html = WIDGET.read_text(encoding="utf-8")
    assert 'result.status === "identity_rejected"' in html
    assert 'result.status === "unbound"' in html
    identity_branch = html.split(
        'result.status === "identity_rejected"', 1
    )[1].split("} else if", 1)[0]
    unbound_branch = html.split(
        'result.status === "unbound"', 1
    )[1].split("} else if", 1)[0]
    assert "stopped = true" in identity_branch
    assert "stopped = true" not in unbound_branch
