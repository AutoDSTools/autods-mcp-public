"""E3 acceptance — end-to-end smoke test against staging.

Drives every registered MCP tool through the real Streamable HTTP transport
(real Cognito auth + real upstream calls) and asserts each op either returns a
2xx envelope or a *documented business error* (the upstream answered and the
mapping layer classified it) — never an infrastructure failure (transport
unreachable, internal error) or a schema rejection (which would mean the
manifest's inputSchema is wrong).

Opt-in: skipped unless ``RUN_STAGING_E2E=1`` plus the staging env vars in
``conftest._REQUIRED_VARS``. See ``tests/e2e/conftest.py``.
"""

from typing import Any

from mcp import types

from tests.mcp_server.conftest import mcp_client_session

# The full registered tool set (5 AutoDSApi ops + 5 ProductsResearch ops). Used
# both to assert tools/list and to drive the per-op smoke calls.
AUTODS_OPS = {
    "list_stores_api",
    "list_products",
    "get_bulk_action_items",
    "upload_products",
    "publish_drafts_to_marketplace",
}
PRODUCTS_RESEARCH_OPS = {
    "search_products",
    "get_winning_products",
    "get_product_by_id",
    "get_similar_products",
    "get_recommended_products",
}
ALL_OPS = AUTODS_OPS | PRODUCTS_RESEARCH_OPS

# Write ops: only exercised when E2E_INCLUDE_WRITES=1 (they mutate staging).
WRITE_OPS = {"upload_products", "publish_drafts_to_marketplace"}

# Error-type prefixes that mean "the upstream answered with a business
# response" — acceptable per the E3 contract. Any other error prefix
# (internal_error / upstream_unreachable / rate_limited / invalid_arguments)
# fails the smoke test: the stack itself misbehaved, or the manifest's
# inputSchema rejected arguments we believe are valid.
_BUSINESS_PREFIXES = {"unauthenticated", "forbidden", "upstream_client_error"}


def _error_prefix(result: types.CallToolResult) -> str:
    """The stable ``error_type`` token an error result is prefixed with."""
    text = result.content[0].text if result.content else ""
    return text.split(":", 1)[0].strip()


def _classify(result: types.CallToolResult, *, extra_ok_prefixes: frozenset[str]) -> tuple[str, Any]:
    """Map a tool result to ('ok', status) / ('business', prefix) / ('fail', detail)."""
    if not result.isError:
        status = (result.structuredContent or {}).get("status")
        return "ok", status
    prefix = _error_prefix(result)
    if prefix in _BUSINESS_PREFIXES or prefix in extra_ok_prefixes:
        return "business", prefix
    return "fail", prefix


def _first_product_id(data: Any) -> str | None:
    """Pull the first product id out of a list/results envelope, if any."""
    if isinstance(data, dict):
        data = data.get("results", data)
    if isinstance(data, list) and data:
        first = data[0]
        if isinstance(first, dict):
            for key in ("id", "_id", "product_id"):
                value = first.get(key)
                if isinstance(value, str) and value:
                    return value
    return None


async def test_tools_list_exposes_all_registered_ops(staging_app, access_token) -> None:
    """The staging-wired server advertises exactly the 10 registered tools."""
    app, runtime = staging_app
    async with mcp_client_session(app, runtime, token=access_token) as session:
        tools = await session.list_tools()
    names = {tool.name for tool in tools.tools}
    assert names == ALL_OPS
    # Every ProductsResearch op is advertised read-only.
    by_name = {tool.name: tool for tool in tools.tools}
    for op in PRODUCTS_RESEARCH_OPS:
        assert by_name[op].annotations.readOnlyHint is True


async def test_every_registered_op_smoke(staging_app, access_token, staging_config) -> None:
    """Call every registered op end-to-end and assert a 2xx or business error."""
    app, runtime = staging_app
    failures: list[str] = []
    skipped: list[str] = []

    async with mcp_client_session(app, runtime, token=access_token) as session:

        async def call(name: str, arguments: dict[str, Any]) -> types.CallToolResult:
            return await session.call_tool(name, arguments)

        # --- ProductsResearch reads (also discover a product id to reuse) ---
        product_id: str | None = None

        search = await call(
            "search_products",
            {"body": {"order_by": {"name": "created_at", "direction": "desc"}, "limit": 5}},
        )
        _record("search_products", search, failures, frozenset())
        if not search.isError:
            product_id = _first_product_id((search.structuredContent or {}).get("data"))

        winning = await call("get_winning_products", {"offset": 0, "limit": 5, "sort": "-created_at"})
        _record("get_winning_products", winning, failures, frozenset())
        if product_id is None and not winning.isError:
            product_id = _first_product_id((winning.structuredContent or {}).get("data"))

        # These three need a real product id; a 307 (subscription-gated winning
        # product) is a documented business response for get_product_by_id.
        if product_id is not None:
            _record(
                "get_product_by_id",
                await call("get_product_by_id", {"product_id": product_id}),
                failures,
                frozenset({"upstream_error"}),
            )
            _record(
                "get_similar_products",
                await call("get_similar_products", {"product_id": product_id}),
                failures,
                frozenset(),
            )
            _record(
                "get_recommended_products",
                await call("get_recommended_products", {"product_id": product_id, "limit": 5}),
                failures,
                frozenset(),
            )
        else:
            skipped += ["get_product_by_id", "get_similar_products", "get_recommended_products"]

        # --- AutoDSApi: stores (also discover store ids to reuse) ---
        stores = await call("list_stores_api", {})
        _record("list_stores_api", stores, failures, frozenset())

        store_ids = staging_config.store_ids
        if store_ids is None and not stores.isError:
            store_ids = _first_store_ids((stores.structuredContent or {}).get("data"))

        # --- AutoDSApi: store-scoped reads ---
        if store_ids:
            _record(
                "list_products",
                await call(
                    "list_products",
                    {"store_ids": store_ids, "body": {"product_status": 2, "limit": 1, "projection": ["title"]}},
                ),
                failures,
                frozenset(),
            )
            # A bogus bulk_action_id is expected to yield empty results or a
            # documented 4xx — both acceptable.
            _record(
                "get_bulk_action_items",
                await call(
                    "get_bulk_action_items", {"store_ids": store_ids, "bulk_action_id": 1, "body": {"limit": 1}}
                ),
                failures,
                frozenset(),
            )
        else:
            skipped += ["list_products", "get_bulk_action_items"]

        # --- Writes: only when explicitly enabled ---
        if staging_config.include_writes and store_ids:
            _record(
                "upload_products",
                await call(
                    "upload_products",
                    {
                        "store_ids": store_ids,
                        "body": {"region": 1, "status": 1, "buy_site_id": 1, "new_products": [{"asin": "B0TEST0000"}]},
                    },
                ),
                failures,
                frozenset(),
            )
            _record(
                "publish_drafts_to_marketplace",
                await call("publish_drafts_to_marketplace", {"store_ids": store_ids, "body": {"product_status": 1}}),
                failures,
                frozenset(),
            )
        else:
            skipped += list(WRITE_OPS)

    if skipped:
        # Surface what wasn't exercised so a "green" run can't masquerade as full
        # coverage (missing store ids / product id / writes disabled).
        print(f"e2e smoke skipped ops (insufficient fixtures): {sorted(set(skipped))}")
    assert not failures, "ops failed the smoke contract:\n" + "\n".join(failures)


def _record(
    name: str,
    result: types.CallToolResult,
    failures: list[str],
    extra_ok_prefixes: frozenset[str],
) -> None:
    outcome, detail = _classify(result, extra_ok_prefixes=extra_ok_prefixes)
    if outcome == "ok":
        if not (isinstance(detail, int) and 200 <= detail < 300):
            failures.append(f"{name}: non-2xx success status {detail!r}")
    elif outcome == "fail":
        failures.append(f"{name}: {detail or 'unknown error'}")


def _first_store_ids(data: Any) -> str | None:
    """Best-effort extraction of a single store id from a stores response."""
    if isinstance(data, dict):
        for key in ("results", "stores", "data"):
            if isinstance(data.get(key), list):
                data = data[key]
                break
    if isinstance(data, list) and data:
        first = data[0]
        if isinstance(first, dict):
            for key in ("id", "store_id", "_id"):
                value = first.get(key)
                if value is not None:
                    return str(value)
    return None
