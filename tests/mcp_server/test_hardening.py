"""Phase F acceptance at the transport seam.

Exercises the full middleware → auth → transport → call_tool path with a mocked
upstream:

* **F0** — the session manager is stateless; no session is retained between
  requests.
* **F1** — the per-user rate limit short-circuits the call with a ``rate_limited``
  error once the bucket is exhausted.
* **F2** — one structured ``tool_call`` audit line per call, all fields present,
  no payload body.
* **F3** — upstream 401/403/4xx/5xx map to typed, sanitized MCP errors.
"""

import json
from pathlib import Path

import httpx
import pytest
import structlog
from structlog.testing import capture_logs

from autods_mcp_server import mcp_transport
from autods_mcp_server.logging import configure_logging
from autods_mcp_server.ratelimit import BucketSpec, InMemoryRateLimiter
from autods_mcp_server.settings import Settings
from tests.mcp_server.conftest import mcp_client_session


def _ok_upstream(request: httpx.Request) -> httpx.Response:
    return httpx.Response(200, json={"task_id": "abc"})


# A request body that satisfies upload_products' body_schema (required integer
# enums + one product source). The mocked upstream ignores the content; it just
# has to pass input validation so these tests reach the dispatcher.
_VALID_UPLOAD_BODY = {"region": 1, "status": 1, "buy_site_id": 1, "new_products": [{"asin": "B0TEST123"}]}


# --- F0: stateless transport ------------------------------------------------


async def test_transport_is_stateless_and_retains_no_sessions(
    mcp_settings, make_mcp_app, bundled_manifest_dir: Path, access_token
) -> None:
    settings = mcp_settings(manifest_dir=bundled_manifest_dir)
    app, runtime = make_mcp_app(settings, upstream_handler=_ok_upstream)

    assert runtime.session_manager.stateless is True

    async with mcp_client_session(app, runtime, token=access_token) as session:
        await session.call_tool("upload_products", {"store_ids": "s1", "body": _VALID_UPLOAD_BODY})

    # Nothing is kept between requests — the dict that pins stateful sessions
    # to a worker stays empty, so any replica/worker can serve any request.
    assert runtime.session_manager._server_instances == {}


# --- F1: per-user rate limiting ---------------------------------------------


async def test_rate_limit_blocks_after_capacity(
    mcp_settings, make_mcp_app, bundled_manifest_dir: Path, access_token
) -> None:
    settings = mcp_settings(manifest_dir=bundled_manifest_dir)
    # Capacity 1, slow refill → the second call within the test is blocked.
    limiter = InMemoryRateLimiter([BucketSpec("minute", capacity=1, refill_rate=1 / 60)])
    app, runtime = make_mcp_app(settings, upstream_handler=_ok_upstream, rate_limiter=limiter)

    async with mcp_client_session(app, runtime, token=access_token) as session:
        first = await session.call_tool("upload_products", {"store_ids": "s1", "body": _VALID_UPLOAD_BODY})
        second = await session.call_tool("upload_products", {"store_ids": "s1", "body": _VALID_UPLOAD_BODY})

    assert first.is_error is False
    assert second.is_error is True
    assert second.content[0].text.startswith("rate_limited: ")
    assert "Retry after" in second.content[0].text


# --- F2: audit logging ------------------------------------------------------


async def test_successful_call_emits_one_audit_line(
    mcp_settings, make_mcp_app, bundled_manifest_dir: Path, access_token, monkeypatch
) -> None:
    settings = mcp_settings(manifest_dir=bundled_manifest_dir)
    app, runtime = make_mcp_app(settings, upstream_handler=_ok_upstream)

    with capture_logs() as logs:
        # Fresh proxy so it binds to capture_logs' processor (defeats any
        # cached bound logger from an earlier test).
        monkeypatch.setattr(mcp_transport, "_audit_logger", structlog.get_logger("audit-test"))
        async with mcp_client_session(app, runtime, token=access_token) as session:
            await session.call_tool(
                "upload_products", {"store_ids": "s1", "body": {**_VALID_UPLOAD_BODY, "secret": "x"}}
            )

    audit = [line for line in logs if line.get("event") == "tool_call"]
    assert len(audit) == 1
    line = audit[0]
    assert line["cognito_username"] == "user-1"
    assert line["tool_name"] == "upload_products"
    assert line["op_id"] == "upload_products"
    assert line["upstream_url"] == "https://autods-api.test/products/s1/"
    assert line["upstream_status"] == 200
    assert "latency_ms" in line
    assert "error_type" not in line  # success → omitted
    # No payload bodies are ever logged (PII risk).
    assert "body" not in line
    assert "arguments" not in line
    assert "secret" not in json.dumps(line)


def test_audit_line_carries_request_id_and_timestamp(env, capsys) -> None:
    """Rendered through the real processor chain, the audit line has ts +
    request_id (from contextvars) alongside the explicit fields."""
    env(
        MCP_ENV="staging",
        COGNITO_USER_POOL_ID="us-west-2_TESTPOOL",
        COGNITO_DOMAIN="autods.auth.us-west-2.amazoncognito.com",
        COGNITO_PUBLIC_CLIENT_ID="c",
        ALLOWED_COGNITO_CLIENT_IDS='["c"]',
        FORCE_HTTPS="true",
        PUBLIC_HOSTNAME="mcp.autods.com",
        REDIS_URL="redis://localhost:6379/0",
    )
    configure_logging(Settings())  # staging → JSON renderer

    structlog.contextvars.bind_contextvars(request_id="req-xyz")
    try:
        # Unique name → fresh proxy → binds to the JSON config just set.
        structlog.get_logger("autods_mcp_server.audit.contract").info(
            "tool_call",
            cognito_username="u",
            tool_name="t",
            op_id="t",
            upstream_url="https://x",
            upstream_status=200,
            latency_ms=1.0,
        )
    finally:
        structlog.contextvars.clear_contextvars()

    line = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    for field in (
        "request_id",
        "timestamp",
        "cognito_username",
        "tool_name",
        "op_id",
        "upstream_url",
        "upstream_status",
        "latency_ms",
    ):
        assert field in line, f"missing audit field: {field}"
    assert line["request_id"] == "req-xyz"


async def test_upstream_5xx_audit_records_detail_separately(
    mcp_settings, make_mcp_app, bundled_manifest_dir: Path, access_token, monkeypatch
) -> None:
    def upstream(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, json={"detail": "db pool exhausted at pg-internal:5432"})

    settings = mcp_settings(manifest_dir=bundled_manifest_dir)
    app, runtime = make_mcp_app(settings, upstream_handler=upstream)

    with capture_logs() as logs:
        monkeypatch.setattr(mcp_transport, "_audit_logger", structlog.get_logger("audit-test2"))
        async with mcp_client_session(app, runtime, token=access_token) as session:
            result = await session.call_tool("upload_products", {"store_ids": "s1", "body": _VALID_UPLOAD_BODY})

    # User sees a generic error; internal hostname is not leaked.
    assert result.is_error is True
    assert result.content[0].text.startswith("upstream_error: ")
    assert "pg-internal" not in result.content[0].text

    audit = [line for line in logs if line.get("event") == "tool_call"]
    assert audit[0]["error_type"] == "upstream_error"
    assert audit[0]["upstream_status"] == 503
    # Full detail is preserved in a separate server-side log line.
    detail_lines = [line for line in logs if line.get("event") == "upstream_error_detail"]
    assert detail_lines and "pg-internal" in json.dumps(detail_lines[0]["detail"])


# --- F3: upstream error mapping (end-to-end) --------------------------------


@pytest.mark.parametrize(
    ("status", "prefix"),
    [
        (401, "unauthenticated: "),
        (403, "forbidden: "),
        (422, "upstream_client_error: "),
    ],
)
async def test_upstream_client_errors_map_to_typed_results(
    mcp_settings, make_mcp_app, bundled_manifest_dir: Path, access_token, status: int, prefix: str
) -> None:
    def upstream(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, json={"detail": "store_id is required"})

    settings = mcp_settings(manifest_dir=bundled_manifest_dir)
    app, runtime = make_mcp_app(settings, upstream_handler=upstream)

    async with mcp_client_session(app, runtime, token=access_token) as session:
        result = await session.call_tool("upload_products", {"store_ids": "s1", "body": _VALID_UPLOAD_BODY})

    assert result.is_error is True
    assert result.content[0].text.startswith(prefix)


async def test_upstream_4xx_forwards_sanitized_detail(
    mcp_settings, make_mcp_app, bundled_manifest_dir: Path, access_token
) -> None:
    def upstream(request: httpx.Request) -> httpx.Response:
        return httpx.Response(422, json={"detail": "store_id is required"})

    settings = mcp_settings(manifest_dir=bundled_manifest_dir)
    app, runtime = make_mcp_app(settings, upstream_handler=upstream)

    async with mcp_client_session(app, runtime, token=access_token) as session:
        result = await session.call_tool("upload_products", {"store_ids": "s1", "body": _VALID_UPLOAD_BODY})

    assert "store_id is required" in result.content[0].text


# --- Body-schema validation (RD-58) -----------------------------------------


async def test_invalid_body_is_rejected_locally_without_upstream_call(
    mcp_settings, make_mcp_app, bundled_manifest_dir: Path, access_token
) -> None:
    """A body that violates the typed body_schema (here ``product_status`` as a
    string instead of the integer enum) is rejected with a typed
    ``invalid_arguments`` error and never reaches the upstream."""
    upstream_calls: list[str] = []

    def upstream(request: httpx.Request) -> httpx.Response:
        upstream_calls.append(str(request.url))
        return httpx.Response(200, json={})

    settings = mcp_settings(manifest_dir=bundled_manifest_dir)
    app, runtime = make_mcp_app(settings, upstream_handler=upstream)

    async with mcp_client_session(app, runtime, token=access_token) as session:
        result = await session.call_tool("list_products", {"store_ids": "s1", "body": {"product_status": "active"}})

    assert result.is_error is True
    assert result.content[0].text.startswith("invalid_arguments: ")
    assert "product_status" in result.content[0].text
    # The malformed call short-circuits before any upstream request.
    assert upstream_calls == []


async def test_search_products_without_filters_is_refused_locally(
    mcp_settings, make_mcp_app, bundled_manifest_dir: Path, access_token
) -> None:
    """RD-107: the one body shape the search upstream answers with a 500.

    ``{order_by, limit}`` with no ``filters`` key at all is refused upstream —
    and it is the first call an exploring agent makes, so it arrives as an
    opaque ``upstream_error`` on a body that looks reasonable. Fixing the status
    code belongs to the upstream, but *reaching* it does not have to happen:
    ``filters`` is required in the ``body_schema``, so the schema gate turns it
    into a typed ``invalid_arguments`` naming the missing field, before any
    request is sent. The empty list — the documented unfiltered form — still
    passes, which is the half a blanket "filters must be non-empty" would break.
    """
    upstream_calls: list[str] = []

    def upstream(request: httpx.Request) -> httpx.Response:
        upstream_calls.append(str(request.url))
        return httpx.Response(200, json={"results": [], "region": "US", "currency": "USD"})

    settings = mcp_settings(manifest_dir=bundled_manifest_dir)
    app, runtime = make_mcp_app(settings, upstream_handler=upstream)
    order_by = {"name": "created_at", "direction": "desc"}

    async with mcp_client_session(app, runtime, token=access_token) as session:
        omitted = await session.call_tool("search_products", {"body": {"order_by": order_by, "limit": 2}})
        empty = await session.call_tool("search_products", {"body": {"order_by": order_by, "limit": 2, "filters": []}})

    assert omitted.is_error is True
    assert omitted.content[0].text.startswith("invalid_arguments: ")
    assert "filters" in omitted.content[0].text
    # The refused shape never reaches the upstream; the documented one does.
    assert empty.is_error is False
    assert upstream_calls == ["https://products-research.test/products/"]


async def test_valid_integer_enum_body_passes_validation(
    mcp_settings, make_mcp_app, bundled_manifest_dir: Path, access_token
) -> None:
    """The integer form of the same field validates and reaches the upstream."""
    upstream_calls: list[str] = []

    def upstream(request: httpx.Request) -> httpx.Response:
        upstream_calls.append(str(request.url))
        return httpx.Response(200, json={"results": []})

    settings = mcp_settings(manifest_dir=bundled_manifest_dir)
    app, runtime = make_mcp_app(settings, upstream_handler=upstream)

    async with mcp_client_session(app, runtime, token=access_token) as session:
        result = await session.call_tool("list_products", {"store_ids": "s1", "body": {"product_status": 2}})

    assert result.is_error is False
    assert len(upstream_calls) == 1


# --- The single-product delete (RD-98) ---------------------------------------


async def test_delete_product_without_remove_from_marketplace_is_refused_locally(
    mcp_settings, make_mcp_app, bundled_manifest_dir: Path, access_token
) -> None:
    """RD-98: the upstream has no default for ``remove_from_marketplace``.

    The two values do different things to the user's storefront — ``true``
    deletes the live listing, ``false`` leaves it selling with nothing watching
    it — so the caller has to choose, and a call that omits the flag must be
    refused rather than guessed at. mcp 2.x validates no arguments of its own
    (``validate_input`` is gone), so ``_build_validators`` /
    ``_validate_arguments`` are the only gate: this asserts the rejection at a
    real client rather than assuming the SDK provides one.
    """
    upstream_calls: list[httpx.Request] = []

    def upstream(request: httpx.Request) -> httpx.Response:
        upstream_calls.append(request)
        return httpx.Response(200, json={"status": "Product 1234 was deleted successfully"})

    settings = mcp_settings(manifest_dir=bundled_manifest_dir)
    app, runtime = make_mcp_app(settings, upstream_handler=upstream)

    async with mcp_client_session(app, runtime, token=access_token) as session:
        omitted = await session.call_tool("delete_product", {"store_id": 42, "product_id": "6512ab34cd56ef7890123456"})
        chosen = await session.call_tool(
            "delete_product",
            {"store_id": 42, "product_id": "6512ab34cd56ef7890123456", "remove_from_marketplace": True},
        )
        declined = await session.call_tool(
            "delete_product",
            {"store_id": 42, "product_id": "6512ab34cd56ef7890123456", "remove_from_marketplace": False},
        )

    assert omitted.is_error is True
    assert omitted.content[0].text.startswith("invalid_arguments: ")
    assert "remove_from_marketplace" in omitted.content[0].text
    # The refused shape never reaches the upstream; the explicit ones do, with
    # the flag as a query parameter and the single store id in the path.
    assert chosen.is_error is False
    assert declined.is_error is False
    assert len(upstream_calls) == 2
    request = upstream_calls[0]
    assert request.method == "DELETE"
    assert request.url.path == "/products/42/product/6512ab34cd56ef7890123456/"
    assert request.url.params["remove_from_marketplace"] == "true"
    # Both spellings are pinned because ``_build_request`` renders a boolean
    # itself (``dispatch._to_wire``), as the JSON ``true``/``false``. The
    # upstream reads it with marshmallow ``fields.Bool``, which accepts that
    # spelling (and the ``True``/``False`` that ``str()`` used to produce);
    # ``false`` is the value with the lasting consequence (a listing left live
    # with nothing monitoring it), so a change to how booleans are rendered has
    # to fail here rather than in someone's store.
    assert upstream_calls[1].url.params["remove_from_marketplace"] == "false"


# --- The sourcing writes (RD-94) ---------------------------------------------

# The AutoDS product this run pretends to source, and the 1688 offer it is
# sourced from. The variation id is the supplier's own: the offer id, an
# underscore, then the supplier variation — the shape that is easy to confuse
# with the bare offer id.
_SOURCING_PRODUCT_ID = "6512ab34cd56ef7890123456"
_OFFER_ID = "992906129243"
_OFFER_VARIATION_ID = "992906129243_6280495563187"


def _sourcing_body(variations_match: list[dict], **overrides) -> dict:
    body = {
        "alibaba_1688_id": _OFFER_ID,
        "add_to_unfulfilled_orders": False,
        "link": {"variations_match": variations_match},
    }
    body.update(overrides)
    return body


async def test_a_multi_variation_link_without_an_autods_id_is_refused_locally(
    mcp_settings, make_mcp_app, bundled_manifest_dir: Path, access_token
) -> None:
    """RD-94: the pairing rule the upstream enforces, enforced here first.

    An entry of ``variations_match`` says which supplier variation one AutoDS
    variation is sourced from, so with more than one entry each needs ``sku`` or
    ``variant_id_on_site`` to say *which* AutoDS variation it means. A single
    entry needs neither — there is only one variation for it to be about.

    Reaching the upstream with the ambiguous form costs the whole call: this
    operation spends a non-refundable auto-order credit, and a transport-level
    failure carries no answer saying whether it was charged. So the schema is
    where the shape is refused, and — since mcp 2.x validates no arguments of
    its own — ``_build_validators`` / ``_validate_arguments`` are the only gate
    there is. Asserted at a real client for that reason.

    The rule is expressed as ``if``/``then`` rather than as a two-branch
    ``anyOf`` over the whole object, and that is about the *message*, not the
    logic — the two accept exactly the same bodies. ``anyOf`` fails at the
    object that carries it, so the error names ``body/link`` and echoes the
    whole link object back with no clue which entry is wrong; ``if``/``then``
    fails inside the array and names ``variations_match/<index>``. An agent can
    act on the second one, which is the whole point of refusing locally instead
    of letting the upstream answer.
    """
    upstream_calls: list[httpx.Request] = []

    def upstream(request: httpx.Request) -> httpx.Response:
        upstream_calls.append(request)
        return httpx.Response(200, json={"status": "ok"})

    settings = mcp_settings(manifest_dir=bundled_manifest_dir)
    app, runtime = make_mcp_app(settings, upstream_handler=upstream)
    arguments = {"store_id": 42, "product_id": _SOURCING_PRODUCT_ID}

    async with mcp_client_session(app, runtime, token=access_token) as session:
        ambiguous = await session.call_tool(
            "create_1688_sourcing_request",
            {
                **arguments,
                "body": _sourcing_body(
                    [
                        {"item_id_on_site": _OFFER_VARIATION_ID, "sku": "a422542f"},
                        {"item_id_on_site": "992906129243_6280495563188"},
                    ]
                ),
            },
        )
        single = await session.call_tool(
            "create_1688_sourcing_request",
            {**arguments, "body": _sourcing_body([{"item_id_on_site": _OFFER_VARIATION_ID}])},
        )
        paired = await session.call_tool(
            "create_1688_sourcing_request",
            {
                **arguments,
                "body": _sourcing_body(
                    [
                        {"item_id_on_site": _OFFER_VARIATION_ID, "sku": "a422542f"},
                        {"item_id_on_site": "992906129243_6280495563188", "variant_id_on_site": "48992958513322"},
                    ]
                ),
            },
        )

    assert ambiguous.is_error is True
    message = ambiguous.content[0].text
    assert message.startswith("invalid_arguments: ")
    # The entry, by index — not just "the body is wrong somewhere".
    assert "body/link/variations_match/1" in message
    # And only that entry rides back in the message — not the rest of the body,
    # and not the sibling entry that was fine.
    assert "alibaba_1688_id" not in message
    assert "a422542f" not in message
    # The two documented shapes go through; the ambiguous one never leaves.
    assert single.is_error is False
    assert paired.is_error is False
    assert len(upstream_calls) == 2
    assert upstream_calls[0].url.path == f"/store_quotes/42/product/{_SOURCING_PRODUCT_ID}/alibaba-1688-request-async"
    assert json.loads(upstream_calls[0].content)["alibaba_1688_id"] == _OFFER_ID


async def test_a_sourcing_request_without_the_unfulfilled_orders_choice_is_refused_locally(
    mcp_settings, make_mcp_app, bundled_manifest_dir: Path, access_token
) -> None:
    """``add_to_unfulfilled_orders`` has no upstream default, and the two values
    do different things to orders the buyer is already waiting on — so the call
    that omits it is refused by name rather than guessed at, exactly as
    ``delete_product``'s ``remove_from_marketplace`` is."""

    def upstream(request: httpx.Request) -> httpx.Response:  # pragma: no cover - never reached
        raise AssertionError("the refused body must not reach the upstream")

    settings = mcp_settings(manifest_dir=bundled_manifest_dir)
    app, runtime = make_mcp_app(settings, upstream_handler=upstream)

    async with mcp_client_session(app, runtime, token=access_token) as session:
        omitted = await session.call_tool(
            "create_1688_sourcing_request",
            {
                "store_id": 42,
                "product_id": _SOURCING_PRODUCT_ID,
                "body": {
                    "alibaba_1688_id": _OFFER_ID,
                    "link": {"variations_match": [{"item_id_on_site": _OFFER_VARIATION_ID}]},
                },
            },
        )

    assert omitted.is_error is True
    assert omitted.content[0].text.startswith("invalid_arguments: ")
    assert "add_to_unfulfilled_orders" in omitted.content[0].text


async def test_the_relink_enforces_the_same_pairing_rule(
    mcp_settings, make_mcp_app, bundled_manifest_dir: Path, access_token
) -> None:
    """``link_quoted_product`` takes the same ``variations_match`` and the same
    rule applies to it — the two schemas are written against one upstream model,
    and a rule enforced on only one of them is the half an author forgets. It
    carries the ``if``/``then`` form too, so its message names the entry rather
    than the body."""
    upstream_calls: list[httpx.Request] = []

    def upstream(request: httpx.Request) -> httpx.Response:
        upstream_calls.append(request)
        return httpx.Response(200, json={"id": _SOURCING_PRODUCT_ID})

    settings = mcp_settings(manifest_dir=bundled_manifest_dir)
    app, runtime = make_mcp_app(settings, upstream_handler=upstream)
    arguments = {"store_id": 42, "product_id": _SOURCING_PRODUCT_ID}

    async with mcp_client_session(app, runtime, token=access_token) as session:
        ambiguous = await session.call_tool(
            "link_quoted_product",
            {
                **arguments,
                "body": {
                    "store_quote_id": 7,
                    "add_to_unfulfilled_orders": False,
                    "variations_match": [
                        {"item_id_on_site": _OFFER_VARIATION_ID, "sku": "a422542f"},
                        {"item_id_on_site": "992906129243_6280495563188"},
                    ],
                },
            },
        )
        single = await session.call_tool(
            "link_quoted_product",
            {
                **arguments,
                "body": {
                    "store_quote_id": 7,
                    "add_to_unfulfilled_orders": False,
                    "variations_match": [{"item_id_on_site": _OFFER_VARIATION_ID}],
                },
            },
        )

    assert ambiguous.is_error is True
    assert ambiguous.content[0].text.startswith("invalid_arguments: ")
    assert "body/variations_match/1" in ambiguous.content[0].text
    assert single.is_error is False
    assert len(upstream_calls) == 1
    assert upstream_calls[0].url.path == f"/products/42/product/{_SOURCING_PRODUCT_ID}/link_quoted_product"
