"""E3 acceptance — end-to-end smoke test against staging.

Drives every registered MCP tool through the real Streamable HTTP transport
(real Cognito auth + real upstream calls) and asserts each op either returns a
2xx envelope or a *documented business error* (the upstream answered and the
mapping layer classified it) — never an infrastructure failure (transport
unreachable, internal error) or a schema rejection (which would mean the
manifest's inputSchema is wrong).

Opt-in: skipped unless ``RUN_STAGING_E2E=1`` plus the staging env vars in
``conftest._REQUIRED_VARS``. See ``tests/e2e/conftest.py``.

**The op-name sets below are hand-maintained and must be updated whenever a
manifest operation is added or removed** — ``test_tools_list_exposes_all_registered_ops``
asserts ``tools/list`` equals them exactly. Because this suite is opt-in, a
forgotten entry fails nothing in CI; it just rots until the next staging run,
which is how ``get_current_user`` (RD-68) and ``get_playbook`` (RD-100) went
missing here and left the file broken until RD-89. The sibling assertions to keep
in step are the tool counts in ``tests/mcp_server/test_loader.py`` and
``tests/mcp_server/test_transport.py``.
"""

from typing import Any

from mcp import types

from tests.mcp_server.conftest import mcp_client_session

# The full registered tool set (7 AutoDSApi ops + 5 ProductsResearch ops + 1 op
# this server answers itself). Used both to assert tools/list and to drive the
# per-op smoke calls, so it has to track the manifests by hand — the same
# hand-maintained count as the loader/transport assertions.
AUTODS_OPS = {
    "list_stores_api",
    "list_products",
    "get_bulk_action_items",
    "upload_products",
    "publish_drafts_to_marketplace",
    "get_current_user",
    "get_user_subscription",
}
PRODUCTS_RESEARCH_OPS = {
    "search_products",
    "get_winning_products",
    "get_product_by_id",
    "get_similar_products",
    "get_recommended_products",
}
# Answered locally (RD-100), so it never reaches an upstream.
LOCAL_OPS = {"get_playbook"}
ALL_OPS = AUTODS_OPS | PRODUCTS_RESEARCH_OPS | LOCAL_OPS

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
    if not result.is_error:
        status = (result.structured_content or {}).get("status")
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
    """The staging-wired server advertises exactly the registered tool set."""
    app, runtime = staging_app
    async with mcp_client_session(app, runtime, token=access_token) as session:
        tools = await session.list_tools()
        # RD-90: the same handshake carries the concatenated manifest
        # instructions, which is what a client puts in the model's system prompt.
        assert session.instructions is not None
        assert session.instructions.startswith("## AutoDS MCP — start here")
    names = {tool.name for tool in tools.tools}
    assert names == ALL_OPS
    # Every ProductsResearch op is advertised read-only.
    by_name = {tool.name: tool for tool in tools.tools}
    for op in PRODUCTS_RESEARCH_OPS:
        assert by_name[op].annotations.read_only_hint is True


async def test_every_registered_op_smoke(staging_app, access_token, staging_config) -> None:
    """Call every registered op end-to-end and assert a 2xx or business error."""
    app, runtime = staging_app
    failures: list[str] = []
    skipped: list[str] = []

    async with mcp_client_session(app, runtime, token=access_token) as session:

        async def call(name: str, arguments: dict[str, Any]) -> types.CallToolResult:
            return await session.call_tool(name, arguments)

        # --- Parameterless reads (no fixture needed, so nothing can skip them) ---
        _record("get_current_user", await call("get_current_user", {}), failures, frozenset())
        # RD-89: entitlements + credit balances. Reconciles the credit balance
        # against a separate source on a short timeout, so it is the slowest read
        # here — that is expected, not a failure.
        subscription = await call("get_user_subscription", {})
        _record("get_user_subscription", subscription, failures, frozenset())
        if not subscription.is_error:
            data = (subscription.structured_content or {}).get("data")
            if not isinstance(data, dict) or "user_addons" not in data:
                failures.append("get_user_subscription: response carries no 'user_addons' list")
        _record("get_playbook", await call("get_playbook", {"name": "product_import"}), failures, frozenset())

        # --- ProductsResearch reads (also discover a product id to reuse) ---
        product_id: str | None = None

        search = await call(
            "search_products",
            {"body": {"order_by": {"name": "created_at", "direction": "desc"}, "limit": 5}},
        )
        _record("search_products", search, failures, frozenset())
        if not search.is_error:
            product_id = _first_product_id((search.structured_content or {}).get("data"))

        winning = await call("get_winning_products", {"offset": 0, "limit": 5, "sort": "-created_at"})
        _record("get_winning_products", winning, failures, frozenset())
        if product_id is None and not winning.is_error:
            product_id = _first_product_id((winning.structured_content or {}).get("data"))

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
        if store_ids is None and not stores.is_error:
            store_ids = _first_store_ids((stores.structured_content or {}).get("data"))

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
