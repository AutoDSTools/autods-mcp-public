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

import asyncio
from typing import Any

from mcp import types

from tests.mcp_server.conftest import mcp_client_session

# The full registered tool set (16 AutoDSApi ops + 6 ProductsResearch ops + 1 op
# this server answers itself). Used both to assert tools/list and to drive the
# per-op smoke calls, so it has to track the manifests by hand — the same
# hand-maintained count as the loader/transport assertions.
AUTODS_OPS = {
    "list_stores_api",
    "list_products",
    "get_bulk_action_items",
    "upload_products",
    "publish_drafts_to_marketplace",
    "delete_product",
    "get_current_user",
    "get_user_subscription",
    "list_store_quotes",
    "get_store_quote",
    "get_store_quote_versions",
    "list_store_quote_shipping_options",
    "create_1688_sourcing_request",
    "link_quoted_product",
    "set_store_quote_shipping_option",
    "request_manual_sourcing",
}
PRODUCTS_RESEARCH_OPS = {
    "search_products",
    "get_winning_products",
    "get_product_by_id",
    "get_similar_products",
    "get_recommended_products",
    "get_categories",
}
# Answered locally (RD-100), so it never reaches an upstream.
LOCAL_OPS = {"get_playbook"}
ALL_OPS = AUTODS_OPS | PRODUCTS_RESEARCH_OPS | LOCAL_OPS

# Write ops: only exercised when E2E_INCLUDE_WRITES=1 (they mutate staging).
WRITE_OPS = {"upload_products", "publish_drafts_to_marketplace", "delete_product"}

# RD-94's sourcing writes, behind a **second** flag of their own
# (``E2E_INCLUDE_SOURCING_WRITES=1``) rather than ``E2E_INCLUDE_WRITES``. The
# two are not the same kind of write: everything under ``WRITE_OPS`` mutates
# staging data this run can clean up, while ``create_1688_sourcing_request``
# and ``request_manual_sourcing`` each spend a real, non-refundable auto-order
# credit on the account and create a request nothing can cancel. A flag that
# turned both on together would spend credits on every run that only meant to
# exercise the upload path.
SOURCING_WRITE_OPS = {
    "create_1688_sourcing_request",
    "request_manual_sourcing",
    "link_quoted_product",
    "set_store_quote_shipping_option",
}

# RD-98: ``delete_product`` may only remove a product this run created, so the
# write block polls the bulk job ``upload_products`` started and deletes what it
# reports. The cadence is the documented one (``docs/polling-conventions.md``:
# first poll ~10 s, then every ~15 s), but the *ceiling* here is lower than the
# ten attempts an agent is told to use: this suite is a shape check and must
# stay runnable, and a job still unfinished after a minute leaves the delete
# skipped rather than failed.
_POLL_FIRST_DELAY_SECONDS = 10
_POLL_INTERVAL_SECONDS = 15
_POLL_ATTEMPTS = 4

# Store-quote statuses that have a supplier offer attached
# (``docs/polling-conventions.md``). Only these two can answer
# ``get_store_quote_versions`` and ``list_store_quote_shipping_options``; on
# ``new`` / ``in_progress`` there is nothing to read yet, and the two tools say
# so in their own notes.
_QUOTED_STORE_QUOTE_STATUSES = frozenset({"ready", "linked"})

# Terminal bulk-action item statuses (``docs/polling-conventions.md``); 1 and 2
# are the two that mean "keep polling".
_TERMINAL_ITEM_STATUSES = frozenset({3, 4, 99})  # finished / canceled / error

# The one terminal status that means the item *landed*. Only a ``3`` item's
# ``autods_product_id`` names a product this run created: AutoDSApi records that
# field on failed items too, so an item that errored because the asin was
# already in the store carries the id of the **pre-existing** product. Deleting
# by that id is the one thing this block may never do.
_FINISHED_ITEM_STATUS = 3

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


def _first_store_quote(data: Any) -> tuple[int, int, str, str, str] | None:
    """A sourcing request to read, as ``(store_id, id, status, default_country,
    product_id)``.

    ``store_id`` comes off the record and never off the configured list: the list
    call spans every configured store, so the request that comes back need not be
    on the first of them — and asking for one on a store it does not belong to
    answers not-found, which would be recorded as a failure.

    Prefers a request that already has a supplier offer (``ready`` / ``linked``),
    the way release check P10 does. Only those two states can answer
    ``get_store_quote_versions`` and ``list_store_quote_shipping_options``, so
    taking the newest record blindly would skip both reads whenever the newest
    request happens to be the one still running.

    Returns ``None`` rather than raising on anything unexpected: an account with
    no sourcing requests is the normal case on staging, and the three reads that
    need one are skipped, not failed.
    """
    results = data.get("results") if isinstance(data, dict) else None
    if not isinstance(results, list):
        return None
    candidates = [
        (
            quote["store_id"],
            quote["id"],
            str(quote.get("status") or ""),
            str(quote.get("default_country") or ""),
            str(quote.get("product_id") or ""),
        )
        for quote in results
        if isinstance(quote, dict) and isinstance(quote.get("id"), int) and isinstance(quote.get("store_id"), int)
    ]
    if not candidates:
        return None
    quoted = [candidate for candidate in candidates if candidate[2] in _QUOTED_STORE_QUOTE_STATUSES]
    return quoted[0] if quoted else candidates[0]


def _root_categories(result: types.CallToolResult) -> list[str]:
    """Every top-level category id, in the order the tree came back.

    Deliberately strict about the node shape: the ``notes`` promise every node
    carries ``value``/``label``/``children``, and an agent that walks the tree
    breaks on the first node that doesn't — so a malformed root is dropped rather
    than trusted. Order *among* the roots is not pinned by anything: upstream
    sorts the roots ahead of the rest and nothing further, so which one lands
    first can move. That is why this returns all of them and the caller tries
    them in turn instead of asserting on one.
    """
    data = (result.structured_content or {}).get("data")
    results = data.get("results") if isinstance(data, dict) else None
    if not isinstance(results, list):
        return []
    roots = []
    for node in results:
        if not isinstance(node, dict) or not {"value", "label", "children"} <= node.keys():
            continue
        value = node["value"]
        if isinstance(value, str) and value:
            roots.append(value)
    return roots


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

        # RD-107: `filters` is a required property, and the unfiltered listing is
        # the empty *list* — a body with no `filters` key is refused by the schema
        # gate as `invalid_arguments` (which this suite classifies as a failure,
        # correctly: it would mean the inputSchema and the call disagree).
        search = await call(
            "search_products",
            {"body": {"order_by": {"name": "created_at", "direction": "desc"}, "limit": 5, "filters": []}},
        )
        _record("search_products", search, failures, frozenset())
        if not search.is_error:
            product_id = _first_product_id((search.structured_content or {}).get("data"))

        # RD-108: the category tree, and the reason it exists — a top-level id
        # has to come back with products under it, since the filter matches a
        # whole subtree.
        categories = await call("get_categories", {})
        _record("get_categories", categories, failures, frozenset())
        root_ids = [] if categories.is_error else _root_categories(categories)
        if not root_ids:
            skipped.append("get_categories -> search_products (no root category id)")
        else:
            # Stops at the first root that matches, so the healthy path is one
            # extra call. A single empty root is not a fault — nothing says every
            # top-level category is stocked, and which root comes back first can
            # move — but *every* root coming back empty means the tree or the
            # filter contract moved and the tool documents a dead end.
            empty_roots: list[str] = []
            for root_id in root_ids:
                filtered = await call(
                    "search_products",
                    {
                        "body": {
                            "order_by": {"name": "created_at", "direction": "desc"},
                            "limit": 5,
                            "filters": [
                                {
                                    "name": "categories.autods_category_id.$id",
                                    "value": root_id,
                                    "value_type": "objectId",
                                    "op": "=",
                                }
                            ],
                        }
                    },
                )
                _record("search_products (category filter)", filtered, failures, frozenset())
                if filtered.is_error:
                    break
                data = (filtered.structured_content or {}).get("data")
                results = data.get("results") if isinstance(data, dict) else None
                if results:
                    break
                empty_roots.append(root_id)
            else:
                failures.append(
                    f"search_products: none of the {len(empty_roots)} top-level categories matched a "
                    f"product; a parent id is documented to match its whole subtree"
                )

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

        # --- AutoDSApi: sourcing requests (RD-93) ---
        # ``list_store_quotes`` runs on any store: an account with no sourcing
        # requests answers an empty ``results``, which is a pass. The other three
        # need a request that exists, so they are skipped rather than faked —
        # and two of them need one that has reached ``ready`` or ``linked``,
        # because a request with no supplier offer yet has no shipping options
        # and no version history to read. The page is asked for several records
        # for that reason, matching release check P10: the newest request alone
        # is often still running, while an older one on the same store can
        # answer all three reads.
        if store_ids:
            quotes = await call("list_store_quotes", {"store_ids": store_ids, "body": {"limit": 5}})
            _record("list_store_quotes", quotes, failures, frozenset())
            quote = None if quotes.is_error else _first_store_quote((quotes.structured_content or {}).get("data"))
        else:
            skipped.append("list_store_quotes")
            quote = None

        if quote:
            # The store the request is actually on — see ``_first_store_quote``.
            quote_store_id, quote_id, quote_status, quote_country, _quote_product_id = quote
            _record(
                "get_store_quote",
                await call("get_store_quote", {"store_id": quote_store_id, "store_quote_id": quote_id}),
                failures,
                frozenset(),
            )
            if quote_status in _QUOTED_STORE_QUOTE_STATUSES:
                _record(
                    "get_store_quote_versions",
                    await call(
                        "get_store_quote_versions",
                        {"store_id": quote_store_id, "store_quote_id": quote_id, "limit": 1},
                    ),
                    failures,
                    frozenset(),
                )
                _record(
                    "list_store_quote_shipping_options",
                    await call(
                        "list_store_quote_shipping_options",
                        {
                            "store_id": quote_store_id,
                            "store_quote_id": quote_id,
                            "body": {"country": quote_country or "US"},
                        },
                    ),
                    failures,
                    frozenset(),
                )
            else:
                skipped += [
                    f"get_store_quote_versions (the request is at {quote_status!r}, not ready/linked)",
                    f"list_store_quote_shipping_options (the request is at {quote_status!r}, not ready/linked)",
                ]
        else:
            skipped += ["get_store_quote", "get_store_quote_versions", "list_store_quote_shipping_options"]

        # --- Writes: only when explicitly enabled ---
        if staging_config.include_writes and store_ids:
            upload = await call(
                "upload_products",
                {
                    "store_ids": store_ids,
                    "body": {
                        "region": 1,
                        "status": 1,
                        "buy_site_id": 1,
                        "new_products": [{"asin": staging_config.upload_asin}],
                    },
                },
            )
            _record("upload_products", upload, failures, frozenset())

            # RD-98: the delete removes a product **this run created** and
            # nothing else, so it is reachable only through the bulk job above.
            # With the default placeholder asin nothing lands and the delete is
            # skipped — set E2E_UPLOAD_ASIN to a real supplier product id to
            # exercise it for real.
            # It runs **before** the publish on purpose. The publish targets
            # every draft in the store, including the one this block just
            # created, so after it the product may be live on the sell channel —
            # and deleting it with remove_from_marketplace False would leave
            # that listing selling with nothing monitoring it, in someone's real
            # staging store. Deleting first keeps the flag below honest.
            # A single store, not the comma-separated list the other product
            # tools take — so the configured value has to be one number.
            first_store_id = store_ids.split(",")[0].strip()
            created = await _created_product_ids(call, upload, store_ids, failures) if first_store_id.isdigit() else []
            if created:
                _record(
                    "delete_product",
                    await call(
                        "delete_product",
                        {
                            "store_id": int(first_store_id),
                            "product_id": created[0],
                            # Still a draft — the publish below has not run yet,
                            # so it never reached a sell channel and there is no
                            # listing to remove there.
                            "remove_from_marketplace": False,
                        },
                    ),
                    failures,
                    frozenset(),
                )
            else:
                skipped.append(
                    "delete_product (the upload created no product to delete)"
                    if first_store_id.isdigit()
                    else "delete_product (E2E_STORE_IDS does not start with a numeric store id)"
                )

            _record(
                "publish_drafts_to_marketplace",
                await call("publish_drafts_to_marketplace", {"store_ids": store_ids, "body": {"product_status": 1}}),
                failures,
                frozenset(),
            )
        else:
            skipped += list(WRITE_OPS)

        # --- Sourcing writes: a gate of their own (RD-94) ---
        await _smoke_sourcing_writes(call, staging_config, store_ids, quote, failures, skipped)

    if skipped:
        # Surface what wasn't exercised so a "green" run can't masquerade as full
        # coverage (missing store ids / product id / writes disabled).
        print(f"e2e smoke skipped ops (insufficient fixtures): {sorted(set(skipped))}")
    assert not failures, "ops failed the smoke contract:\n" + "\n".join(failures)


async def _smoke_sourcing_writes(
    call: Any,
    staging_config: Any,
    store_ids: str | None,
    quote: tuple[int, int, str, str, str] | None,
    failures: list[str],
    skipped: list[str],
) -> None:
    """RD-94's four sourcing writes, each behind the fixture it actually needs.

    Two things make this block unlike the ``WRITE_OPS`` one above, and both are
    why it has a flag of its own.

    **Two of the four spend real money.** ``create_1688_sourcing_request`` and
    ``request_manual_sourcing`` each charge a non-refundable auto-order credit
    the moment the request is accepted, and the request cannot be cancelled
    afterwards — so neither runs off ``E2E_INCLUDE_SOURCING_WRITES`` alone. Each
    additionally needs the operator to *name the product* it may spend that
    credit on, and the flag is only the permission to use it. A product can
    carry at most one sourcing request, which is why the two take separate
    product ids rather than sharing one: pointed at the same product, the second
    call would create no request of its own. Both products must be in the first
    store of ``E2E_STORE_IDS``, and each is skipped when it already has a
    request (``_existing_sourcing_request``).

    **The other two rewrite a product's supplier configuration**, which is free
    but not harmless on a store someone else is using. ``link_quoted_product``
    therefore runs only against a request and a supplier variation the operator
    named (see ``_smoke_relink``), and
    ``set_store_quote_shipping_option`` re-chooses the option the request
    *already* carries — exercising the call shape while leaving the request as
    it found it.

    Everything unset is a ``skipped`` entry, never a failure: an account with no
    sourcing fixtures is the normal case.
    """
    if not staging_config.include_sourcing_writes:
        skipped += [f"{op} (E2E_INCLUDE_SOURCING_WRITES is not set)" for op in sorted(SOURCING_WRITE_OPS)]
        return

    first_store_id = (store_ids or "").split(",")[0].strip()
    if not first_store_id.isdigit():
        skipped += [f"{op} (E2E_STORE_IDS does not start with a numeric store id)" for op in sorted(SOURCING_WRITE_OPS)]
        return
    store_id = int(first_store_id)

    # --- the credit spender: the 1688 request ---
    if not (
        staging_config.sourcing_product_id
        and staging_config.sourcing_1688_offer_id
        and staging_config.sourcing_1688_variation_id
    ):
        skipped.append(
            "create_1688_sourcing_request (needs E2E_SOURCING_PRODUCT_ID, E2E_SOURCING_1688_OFFER_ID and "
            "E2E_SOURCING_1688_VARIATION_ID; it spends a credit)"
        )
    elif reason := await _existing_sourcing_request(call, store_id, staging_config.sourcing_product_id):
        skipped.append(f"create_1688_sourcing_request ({reason})")
    else:
        _record(
            "create_1688_sourcing_request",
            await call(
                "create_1688_sourcing_request",
                {
                    "store_id": store_id,
                    "product_id": staging_config.sourcing_product_id,
                    "body": {
                        "alibaba_1688_id": staging_config.sourcing_1688_offer_id,
                        "add_to_unfulfilled_orders": False,
                        "link": {
                            "variations_match": [
                                # One entry, so neither `sku` nor
                                # `variant_id_on_site` is required — the
                                # single-variation form the body schema allows.
                                {"item_id_on_site": staging_config.sourcing_1688_variation_id}
                            ]
                        },
                    },
                },
            ),
            failures,
            frozenset(),
        )

    # --- the other credit spender: manual sourcing ---
    if not staging_config.manual_sourcing_product_id:
        skipped.append("request_manual_sourcing (needs E2E_MANUAL_SOURCING_PRODUCT_ID; it spends a credit)")
    elif reason := await _existing_sourcing_request(call, store_id, staging_config.manual_sourcing_product_id):
        skipped.append(f"request_manual_sourcing ({reason})")
    else:
        _record(
            "request_manual_sourcing",
            await call(
                "request_manual_sourcing",
                {"store_id": store_id, "product_id": staging_config.manual_sourcing_product_id},
            ),
            failures,
            frozenset(),
        )

    # --- the re-link: only on a request the operator named ---
    await _smoke_relink(call, staging_config, store_ids, failures, skipped)

    # --- the shipping option, on a request that already has an offer ---
    if quote is None or quote[2] not in _QUOTED_STORE_QUOTE_STATUSES:
        skipped.append("set_store_quote_shipping_option (no sourcing request at ready/linked to configure)")
        return
    quote_store_id, quote_id, _status, quote_country, _quote_product_id = quote

    country = quote_country or "US"
    options = await call(
        "list_store_quote_shipping_options",
        {"store_id": quote_store_id, "store_quote_id": quote_id, "body": {"country": country}},
    )
    chosen = None if options.is_error else _chosen_shipping_option_id(options)
    if chosen is None:
        skipped.append("set_store_quote_shipping_option (the request has no chosen shipping option to re-choose)")
        return
    _record(
        "set_store_quote_shipping_option",
        await call(
            "set_store_quote_shipping_option",
            {
                "store_id": quote_store_id,
                "store_quote_id": quote_id,
                # The option already in force, so the request is left as it was
                # found. The point of the check is the call shape, not a change.
                "body": {"country": country, "shipping_option_id": chosen},
            },
        ),
        failures,
        frozenset(),
    )


async def _existing_sourcing_request(call: Any, store_id: int, product_id: str) -> str | None:
    """Why a credit-spending sourcing write must not run on ``product_id``, or ``None``.

    The pre-check the tools' own notes require. A second submission is not
    refused: the 1688 trigger re-runs and re-links, and against a request from
    another supplier its rollback deletes that paid-for request. The env vars
    that name these products usually stay set between runs, so without this
    every re-run would submit again. A read that fails is a skip too — no
    answer is no proof the product is free.
    """
    found = await call(
        "list_store_quotes",
        {
            "store_ids": str(store_id),
            "body": {"limit": 1, "filters": [{"name": "product_id", "op": "=", "value": product_id}]},
        },
    )
    if found.is_error:
        return f"could not check {product_id} for an existing sourcing request"
    data = (found.structured_content or {}).get("data")
    results = data.get("results") if isinstance(data, dict) else None
    if not isinstance(results, list):
        return f"could not check {product_id} for an existing sourcing request"
    if results:
        return f"{product_id} already has a sourcing request"
    return None


async def _smoke_relink(
    call: Any,
    staging_config: Any,
    store_ids: str | None,
    failures: list[str],
    skipped: list[str],
) -> None:
    """``link_quoted_product`` against the one request the operator named.

    Never against whichever request the read block happened to find first: the
    call rewrites that product's supplier, and the upstream does not check that
    the pairing belongs to the request's offer. So it needs both
    ``E2E_SOURCING_LINK_STORE_QUOTE_ID`` and ``E2E_SOURCING_LINK_VARIATION_ID``,
    and it runs only when the named variation is one of that request's own
    supplier variations.
    """
    quote_id_text = staging_config.sourcing_link_store_quote_id
    variation_id = staging_config.sourcing_link_variation_id
    if not (quote_id_text and quote_id_text.isdigit() and variation_id):
        skipped.append(
            "link_quoted_product (needs E2E_SOURCING_LINK_STORE_QUOTE_ID and E2E_SOURCING_LINK_VARIATION_ID)"
        )
        return

    found = await call(
        "list_store_quotes",
        {
            "store_ids": store_ids,
            "body": {"limit": 1, "filters": [{"name": "id", "op": "=", "value": int(quote_id_text)}]},
        },
    )
    data = None if found.is_error else (found.structured_content or {}).get("data")
    results = data.get("results") if isinstance(data, dict) else None
    record = results[0] if isinstance(results, list) and results and isinstance(results[0], dict) else None
    if record is None or not isinstance(record.get("store_id"), int) or not record.get("product_id"):
        skipped.append(f"link_quoted_product (request {quote_id_text} not found on E2E_STORE_IDS)")
        return
    if record.get("status") not in _QUOTED_STORE_QUOTE_STATUSES:
        skipped.append(f"link_quoted_product (request {quote_id_text} is not at ready/linked)")
        return
    buy_item = record.get("buy_item") if isinstance(record.get("buy_item"), dict) else {}
    offer_variations = {
        variation.get("item_id_on_site")
        for variation in buy_item.get("variations") or []
        if isinstance(variation, dict)
    }
    if variation_id not in offer_variations:
        skipped.append(f"link_quoted_product ({variation_id} is not a supplier variation of request {quote_id_text})")
        return

    _record(
        "link_quoted_product",
        await call(
            "link_quoted_product",
            {
                "store_id": record["store_id"],
                "product_id": str(record["product_id"]),
                "body": {
                    "store_quote_id": int(quote_id_text),
                    "add_to_unfulfilled_orders": False,
                    "variations_match": [{"item_id_on_site": variation_id}],
                },
            },
        ),
        failures,
        frozenset(),
    )


def _chosen_shipping_option_id(result: types.CallToolResult) -> str | None:
    """The `id` of the option already marked ``is_chosen``, as a string.

    Returns ``None`` when no option is marked, which is a skip rather than a
    failure — a request can reach ``ready`` with no shipping quoted for the
    country asked for. There is deliberately no fallback to another option:
    choosing one would change the request, and this check must leave it as it
    found it.
    """
    data = (result.structured_content or {}).get("data")
    options = data.get("shipping_options") if isinstance(data, dict) else None
    if not isinstance(options, list):
        return None
    chosen = next(
        (
            option
            for option in options
            if isinstance(option, dict) and option.get("is_chosen") and option.get("id") is not None
        ),
        None,
    )
    return None if chosen is None else str(chosen["id"])


async def _created_product_ids(
    call: Any,
    upload: types.CallToolResult,
    store_ids: str,
    failures: list[str],
) -> list[str]:
    """The ids ``upload_products`` actually created, by polling its bulk job.

    Returns an empty list whenever nothing landed — the upload errored, it
    reported no ``bulk_action.id``, no item reached ``_FINISHED_ITEM_STATUS``, or
    the job was still running at the attempt ceiling. That is a *skip* for the
    delete, never a failure: only a product this run created may be deleted, so
    no product means nothing this suite is allowed to touch.
    """
    if upload.is_error:
        return []
    data = (upload.structured_content or {}).get("data")
    bulk_action = data.get("bulk_action") if isinstance(data, dict) else None
    bulk_action_id = bulk_action.get("id") if isinstance(bulk_action, dict) else None
    if not isinstance(bulk_action_id, int):
        return []

    await asyncio.sleep(_POLL_FIRST_DELAY_SECONDS)
    for attempt in range(_POLL_ATTEMPTS):
        if attempt:
            await asyncio.sleep(_POLL_INTERVAL_SECONDS)
        items = await call(
            "get_bulk_action_items",
            {"store_ids": store_ids, "bulk_action_id": bulk_action_id, "body": {"limit": 50}},
        )
        # A failed poll is a finding in its own right — the op is already
        # recorded above from the read block, so only report a new failure.
        if items.is_error:
            failures.append(f"get_bulk_action_items (polling the RD-98 upload): {_error_prefix(items)}")
            return []
        payload = (items.structured_content or {}).get("data")
        results = payload.get("results") if isinstance(payload, dict) else None
        if not isinstance(results, list):
            return []
        # No items yet is "the job has not started", not "the job is over" — the
        # documented rule ("no item left at 1 or 2") reads as terminal on an
        # empty list, which would skip the delete on a job that was about to
        # produce a product.
        if not results:
            continue
        statuses = [item.get("status") for item in results if isinstance(item, dict)]
        if any(status not in _TERMINAL_ITEM_STATUSES for status in statuses):
            continue
        # Finished items only — see ``_FINISHED_ITEM_STATUS``.
        return [
            product_id
            for item in results
            if isinstance(item, dict)
            and item.get("status") == _FINISHED_ITEM_STATUS
            and isinstance(product_id := item.get("autods_product_id"), str)
            and product_id
        ]
    return []


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
