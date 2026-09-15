"""RD-92: the ``ui://`` widget resources and the ``_meta.ui`` that renders them.

Everything here is asserted against what a **real client session receives**,
never against the objects we constructed. That is not stylistic. Two of RD-82's
six silent protocol bugs were exactly "the server built the right thing and the
client got something else":

* a non-schema key stuffed into an MCP model is accepted at construction and
  then discarded — no error, no data (the ``_meta`` alias trap under mcp 1.x,
  inverted by the 2.x port into "any *other* extra key evaporates");
* a resource content with no explicit ``mime_type`` advertises ``text/plain``,
  and the host stops treating the document as an MCP App at all.

Neither is visible from the server side, so a test that inspects
``types.Tool(...)`` before it goes on the wire proves nothing about either.
"""

import re
from pathlib import Path

import httpx
import pytest

from autods_mcp_server.widgets import WIDGET_MIME_TYPE, WIDGETS
from tests.mcp_server.conftest import mcp_client_session

_PLAYBOOK_URI = "autods://playbook/product_import"


async def test_widgets_and_playbooks_are_both_listed(
    mcp_settings, make_mcp_app, bundled_manifest_dir: Path, access_token
) -> None:
    """The two kinds of resource share one handler and must both survive it.

    RD-100's playbook mirror is what declares the ``resources`` capability;
    RD-92 adds URIs beside it. The regression risk runs both ways — a change
    made for a widget can strand the playbooks — so the assertion covers both
    lists in one place.
    """
    settings = mcp_settings(manifest_dir=bundled_manifest_dir)
    app, runtime = make_mcp_app(settings)

    async with mcp_client_session(app, runtime, token=access_token) as session:
        listed = await session.list_resources()

    by_uri = {str(resource.uri): resource for resource in listed.resources}
    assert _PLAYBOOK_URI in by_uri
    assert by_uri[_PLAYBOOK_URI].mime_type == "text/markdown"
    for widget in WIDGETS:
        assert widget.uri in by_uri, f"{widget.uri} is not advertised"
        assert by_uri[widget.uri].mime_type == WIDGET_MIME_TYPE
        assert by_uri[widget.uri].title == widget.title


async def test_the_widget_list_entry_carries_the_csp(
    mcp_settings, make_mcp_app, bundled_manifest_dir: Path, access_token
) -> None:
    settings = mcp_settings(manifest_dir=bundled_manifest_dir)
    app, runtime = make_mcp_app(settings)

    async with mcp_client_session(app, runtime, token=access_token) as session:
        listed = await session.list_resources()

    entry = next(r for r in listed.resources if str(r.uri) == WIDGETS[0].uri)
    assert entry.meta is not None
    domains = entry.meta["ui"]["csp"]["resourceDomains"]
    assert "https://cdn.shopify.com" in domains
    assert "https://autods-scraper-images.s3-us-west-2.amazonaws.com" in domains


async def test_reading_a_widget_returns_mcp_app_html_with_the_csp(
    mcp_settings, make_mcp_app, bundled_manifest_dir: Path, access_token
) -> None:
    """The CSP on the *read* response is the load-bearing half.

    The host renders from the read. Declared only on the list entry,
    ``hostCapabilities.sandbox`` comes back ``{}`` and the default policy
    (``img-src 'self' data: blob: assets.claude.ai``) blocks every supplier
    image — with nothing reporting it anywhere. That is RD-82 bug 1, and it is
    why the declaration is deliberately duplicated.
    """
    settings = mcp_settings(manifest_dir=bundled_manifest_dir)
    app, runtime = make_mcp_app(settings)

    async with mcp_client_session(app, runtime, token=access_token) as session:
        result = await session.read_resource(WIDGETS[0].uri)

    contents = result.contents[0]
    assert contents.mime_type == WIDGET_MIME_TYPE
    assert "profile=mcp-app" in contents.mime_type
    assert contents.meta is not None
    assert "https://cdn.shopify.com" in contents.meta["ui"]["csp"]["resourceDomains"]
    assert contents.text.startswith("<!DOCTYPE html>")


async def test_reading_a_playbook_still_returns_markdown(
    mcp_settings, make_mcp_app, bundled_manifest_dir: Path, access_token
) -> None:
    """The RD-100 regression guard. Cheap, and it is the half most likely to be
    broken by a change nobody made to it."""
    settings = mcp_settings(manifest_dir=bundled_manifest_dir)
    app, runtime = make_mcp_app(settings)

    async with mcp_client_session(app, runtime, token=access_token) as session:
        result = await session.read_resource(_PLAYBOOK_URI)

    contents = result.contents[0]
    assert contents.mime_type == "text/markdown"
    assert contents.text.startswith("#")


async def test_an_unknown_resource_uri_is_refused(
    mcp_settings, make_mcp_app, bundled_manifest_dir: Path, access_token
) -> None:
    settings = mcp_settings(manifest_dir=bundled_manifest_dir)
    app, runtime = make_mcp_app(settings)

    # The SDK maps the handler's ``ValueError`` to a generic JSON-RPC internal
    # error, so the assertion is that the URI is *refused* — the detail stays
    # server-side, which is the same posture as every other error path here.
    async with mcp_client_session(app, runtime, token=access_token) as session:
        with pytest.raises(Exception, match="Internal server error"):
            await session.read_resource("ui://autods/does-not-exist")


async def test_tool_meta_reaches_the_client(
    mcp_settings, make_mcp_app, bundled_manifest_dir: Path, access_token
) -> None:
    """``_meta.ui.resourceUri`` is what makes a host render the widget at all.

    Asserted off the descriptor a session receives, because a metadata key that
    never arrives is the exact failure mode mcp 2.x still produces silently for
    anything not attached through a real model field.
    """
    settings = mcp_settings(manifest_dir=bundled_manifest_dir)
    app, runtime = make_mcp_app(settings)

    async with mcp_client_session(app, runtime, token=access_token) as session:
        tools = await session.list_tools()

    by_name = {tool.name: tool for tool in tools.tools}
    grid = {"search_products", "get_winning_products", "get_similar_products", "get_recommended_products"}
    for name in grid:
        assert by_name[name].meta is not None, f"{name} lost its _meta"
        assert by_name[name].meta["ui"]["resourceUri"] == "ui://autods/product-grid"
    assert by_name["get_product_by_id"].meta["ui"]["resourceUri"] == "ui://autods/product-card"
    assert by_name["list_products"].meta["ui"]["resourceUri"] == "ui://autods/product-grid"
    # A tool with no image surface carries no UI metadata at all — a widget
    # attached to a bulk-action poll would render an empty frame.
    assert by_name["get_bulk_action_items"].meta is None
    assert by_name["upload_products"].meta is None


async def test_the_meta_survives_the_wire_under_its_alias(
    mcp_settings, make_mcp_app, bundled_manifest_dir: Path, access_token
) -> None:
    """The field is ``meta`` in Python and ``_meta`` on the wire.

    mcp 2.x dumps ``by_alias=True`` on its own outbound path, so this passes —
    but the 1.x version of this trap shipped a junk ``"meta"`` key that no host
    ever read, so the serialised spelling is worth pinning rather than trusting.
    """
    settings = mcp_settings(manifest_dir=bundled_manifest_dir)
    app, runtime = make_mcp_app(settings)

    async with mcp_client_session(app, runtime, token=access_token) as session:
        tools = await session.list_tools()

    tool = next(t for t in tools.tools if t.name == "search_products")
    wire = tool.model_dump(by_alias=True, mode="json", exclude_none=True)
    assert "_meta" in wire
    assert wire["_meta"]["ui"]["resourceUri"] == "ui://autods/product-grid"
    assert "meta" not in wire


async def test_the_images_block_rides_beside_data(
    mcp_settings, make_mcp_app, bundled_manifest_dir: Path, access_token
) -> None:
    """The placement rule, end to end: ``data`` stays the upstream payload
    verbatim and the extracted images are its sibling."""
    upstream_payload = {
        "results": [
            {
                "_id": "69bd58dcb8d927d58e27d1e5",
                "title": "Satin Silk Bed Sheets",
                "images": ["https://cdn.shopify.com/s/files/1/x/one.jpg?v=1774016595"],
            },
            {"_id": "6847ffa626c298ca83e1acb6", "title": "Denim Vest", "images": []},
        ],
        "region": "US",
        "currency": "USD",
    }

    def upstream(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=upstream_payload)

    settings = mcp_settings(manifest_dir=bundled_manifest_dir)
    app, runtime = make_mcp_app(settings, upstream_handler=upstream)

    async with mcp_client_session(app, runtime, token=access_token) as session:
        result = await session.call_tool(
            "search_products",
            {"body": {"order_by": {"name": "created_at", "direction": "desc"}, "filters": []}},
        )

    assert result.is_error is False
    envelope = result.structured_content
    assert envelope is not None
    assert envelope["data"] == upstream_payload
    images = envelope["images"]
    assert images["kind"] == "autods.images/1"
    assert images["widget"] == "product-grid"
    assert images["shown"] == 2
    assert images["items"][0]["id"] == "69bd58dcb8d927d58e27d1e5"
    # Rewritten to the Shopify thumbnail rung, with the original query kept.
    assert images["items"][0]["images"][0]["url"].endswith("&width=256")
    assert images["items"][1]["images"] == []
    # Nothing was truncated, so there is no note to explain.
    assert "note" not in images
    # No base64 was requested, so the result is text-only.
    assert [block.type for block in result.content] == ["text"]


async def test_an_operation_without_an_images_block_gets_an_untouched_envelope(
    mcp_settings, make_mcp_app, bundled_manifest_dir: Path, access_token
) -> None:
    """The regression guard the acceptance criteria ask for: an operation with
    no image surface must serialize exactly as it did before RD-92.

    ``publish_drafts_to_marketplace`` for the same reason
    ``test_success_result_shape_matches_the_1x_wire_format`` uses it — it is in
    no playbook, so this is the un-extended envelope with nothing else riding
    beside ``data`` to muddy what is being asserted.
    """

    def upstream(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"results": [{"id": 1}]})

    settings = mcp_settings(manifest_dir=bundled_manifest_dir)
    app, runtime = make_mcp_app(settings, upstream_handler=upstream)

    async with mcp_client_session(app, runtime, token=access_token) as session:
        result = await session.call_tool(
            "publish_drafts_to_marketplace",
            {"store_ids": "store-1", "body": {"product_status": 1}},
        )

    assert result.is_error is False
    assert result.structured_content == {
        "operation_id": "publish_drafts_to_marketplace",
        "status": 200,
        "ok": True,
        "data": {"results": [{"id": 1}]},
    }
    assert [block.type for block in result.content] == ["text"]


async def test_widget_assets_obey_the_measured_protocol_order(
    mcp_settings, make_mcp_app, bundled_manifest_dir: Path, access_token
) -> None:
    """Four of RD-82's silent bugs live in the HTML, not in Python.

    Each one produced a blank region or a dead widget with no error anywhere, so
    the asset is checked for the specific constructs that fix them: the
    handshake, the size notification (missing ⇒ a 150px default), the
    ``hostCapabilities.sandbox`` path (bug 6 — ``result.sandbox`` reads empty),
    and lazy loading (20 full-size originals in one frame otherwise).

    It also pins the *absence* of ``tools/call`` and ``ui/message``. RD-97
    measured the direct path as invisible to the model — it executed six calls
    and then insisted none had run, and ``ui/update-model-context`` returns
    success while doing nothing — so a mutating widget path would produce
    duplicate writes the moment a user retried. These widgets are read-only by
    construction, and this is what keeps them that way.
    """
    settings = mcp_settings(manifest_dir=bundled_manifest_dir)
    app, runtime = make_mcp_app(settings)

    async with mcp_client_session(app, runtime, token=access_token) as session:
        served = {widget.uri: (await session.read_resource(widget.uri)).contents[0].text for widget in WIDGETS}

    for uri, html in served.items():
        # Everything below is matched against JS *string literals* — the
        # quoted method name a postMessage would actually carry — because the
        # assets discuss these methods in prose and a bare substring match
        # would find the explanation rather than the call.
        methods = set(re.findall(r'method:\s*"([^"]+)"', html))
        assert "ui/initialize" in methods, uri
        assert "ui/notifications/initialized" in methods, uri
        assert "ui/notifications/size-changed" in methods, uri
        # Bug 6: the approved CSP hangs off hostCapabilities, not the top level.
        assert "hostCapabilities" in html or "host-context-changed" in html, uri
        assert 'loading = "lazy"' in html or 'loading="lazy"' in html, uri
        # Read-only: no invocation path of any kind, and no proposal of one.
        assert "tools/call" not in methods, uri
        assert "ui/message" not in methods, uri
        assert "ui/update-model-context" not in methods, uri
        # The payload marker both assets search for must match the server's.
        assert "autods.images/1" in html, uri


async def test_the_diagnostic_is_idle_armed_and_retractable(
    mcp_settings, make_mcp_app, bundled_manifest_dir: Path, access_token
) -> None:
    """The diagnostic must be able to take itself back.

    Staging showed the "No product payload reached this widget" box sitting
    under a grid full of pictures: the box was armed on a fixed timer from load,
    and claude.ai delivers the tool result only once the tool has actually run —
    which is later than any fixed timer worth having. So the timer is restarted
    by every host message (a host that is still talking has not stopped
    sending), and ``render`` clears the box if it printed anyway.

    Nothing in CI renders HTML in a host's sandbox, so this greps the served
    asset for the two constructs, the same way the protocol-order test above
    does. A fixed ``setTimeout(..., 3000)`` on the diagnostic is what it is
    written to keep out.
    """
    settings = mcp_settings(manifest_dir=bundled_manifest_dir)
    app, runtime = make_mcp_app(settings)

    async with mcp_client_session(app, runtime, token=access_token) as session:
        served = {widget.uri: (await session.read_resource(widget.uri)).contents[0].text for widget in WIDGETS}

    for uri, html in served.items():
        assert "armDiagnostic" in html, uri
        # Restarted on inbound host messages, not only armed once at boot.
        assert html.count("armDiagnostic()") >= 2, uri
        # And rendering retracts it.
        assert re.search(r"rendered = true;\s*\n\s*clearDiagnostic\(\);", html), uri
        # The wait is covered by a quiet line instead, so the diagnostic can
        # afford to wait out a genuinely long call before accusing the host.
        assert 'id="wait"' in html, uri
        assert "DIAG_IDLE_MS = 60000" in html, uri


async def test_the_grid_caption_is_not_truncated(
    mcp_settings, make_mcp_app, bundled_manifest_dir: Path, access_token
) -> None:
    """Product's call: a supplier title is shown in full, however tall it makes
    the cell.

    The words that tell two near-identical listings apart sit at the *end* of a
    keyword-stuffed title, so an ellipsis removes precisely the part a user is
    choosing on. A two-line ``-webkit-line-clamp`` was shipped first and is the
    thing this test keeps out — both because product rejected the truncation and
    because the clamp bounds the content box while letting the next line paint
    into the element's own padding, which rendered a sliced third line.
    """
    settings = mcp_settings(manifest_dir=bundled_manifest_dir)
    app, runtime = make_mcp_app(settings)

    async with mcp_client_session(app, runtime, token=access_token) as session:
        grid = (await session.read_resource("ui://autods/product-grid")).contents[0].text

    # Matched as CSS *declarations* — with the colon — because the asset
    # discusses the clamp in a comment saying why it is not there, and a bare
    # substring match would find the explanation rather than the rule.
    assert "-webkit-line-clamp:" not in grid
    assert "text-overflow:" not in grid
