"""RD-90 — the server ``instructions`` channel: wiring, order, and the size cap.

``Manifest.instructions`` was parsed and then dropped for the whole life of the
server: ``Server(...)`` was constructed without ``instructions=``, so every
hand-written enum table and filter rule in the manifests reached exactly nobody.
These tests cover the channel end to end — concatenation order, the boot lint on
the total size, an empty block being legitimate, and the text actually arriving
at a real MCP client's ``InitializeResult``.

The P0 guards at the bottom are the other half: connecting the channel makes
existing manifest text *visible*, including text that describes operations this
server does not register (the ``update_product`` / ``update_product_note``
workflow, ``get_all_stores``). Those are now assertions rather than prose,
because the failure mode — an agent confidently calling a tool that does not
exist — is silent from the server's side.
"""

import json
import re
from pathlib import Path
from typing import Any

import pytest

from autods_mcp_server.manifests import (
    INSTRUCTIONS_HARD_LIMIT,
    INSTRUCTIONS_TARGET,
    InstructionsTooLargeError,
    Manifest,
    assert_instructions_within_limit,
    build_instructions,
    build_registry,
    load_manifests,
)
from autods_mcp_server.mcp_transport import build_runtime
from autods_mcp_server.tools import to_tool
from tests.mcp_server.conftest import mcp_client_session


def _manifest(instructions: str) -> Manifest:
    return Manifest(server_name="demo", instructions=instructions)


def _write(directory: Path, filename: str, manifest: dict) -> None:
    directory.mkdir(exist_ok=True)
    (directory / filename).write_text(json.dumps(manifest), encoding="utf-8")


# --- assembly ---------------------------------------------------------------


def test_instructions_are_concatenated_in_order() -> None:
    text = build_instructions([_manifest("first"), _manifest("second")])
    assert text == "first\n\nsecond"


def test_empty_blocks_contribute_nothing() -> None:
    """A manifest whose whole contract is tier 1/2 has nothing to say here, and
    must not even cost a separator."""
    text = build_instructions([_manifest(""), _manifest("only"), _manifest("   ")])
    assert text == "only"


def test_no_instructions_at_all_is_empty_string() -> None:
    assert build_instructions([_manifest(""), _manifest("")]) == ""
    assert build_instructions([]) == ""


def test_concatenation_follows_sorted_filename_order(tmp_path: Path) -> None:
    """The order is the loader's sorted-filename order, not directory order.

    Load-bearing: a replica-dependent order would give two replicas of the same
    server a different system prompt, and would break the client's prompt-cache
    reuse. Files are written here in deliberately reversed order.
    """
    directory = tmp_path / "manifests"
    for filename, text in (("z.json", "last"), ("a.json", "first"), ("m.json", "middle")):
        _write(directory, filename, {"server_name": filename, "instructions": text})

    assert build_instructions(load_manifests(directory)) == "first\n\nmiddle\n\nlast"


# --- the size lint ----------------------------------------------------------


def test_size_lint_accepts_text_at_the_limit() -> None:
    assert_instructions_within_limit("x" * INSTRUCTIONS_HARD_LIMIT)


def test_size_lint_rejects_text_over_the_limit() -> None:
    with pytest.raises(InstructionsTooLargeError) as excinfo:
        assert_instructions_within_limit("x" * (INSTRUCTIONS_HARD_LIMIT + 1))
    assert str(INSTRUCTIONS_HARD_LIMIT) in str(excinfo.value)


def test_oversized_instructions_refuse_to_boot(mcp_settings, tmp_path: Path) -> None:
    """The lint runs at boot, like the D5 lints — oversized text can never reach
    a client's system prompt."""
    directory = tmp_path / "manifests"
    _write(directory, "big.json", {"server_name": "big", "instructions": "x" * (INSTRUCTIONS_HARD_LIMIT + 1)})

    with pytest.raises(InstructionsTooLargeError):
        build_runtime(mcp_settings(manifest_dir=directory))


def test_manifest_with_empty_instructions_boots_cleanly(mcp_settings, tmp_path: Path) -> None:
    """Empty ``instructions`` is legitimate, not a lint failure: the size cap is
    the only instructions lint there is."""
    directory = tmp_path / "manifests"
    _write(
        directory,
        "silent.json",
        {
            "server_name": "silent",
            "instructions": "",
            "operations": [
                {
                    "operation_id": "silent_op",
                    "method": "GET",
                    "path": "/silent",
                    "annotations": {"title": "Silent", "readOnlyHint": True},
                }
            ],
        },
    )

    runtime = build_runtime(mcp_settings(manifest_dir=directory))

    assert len(runtime.registry) == 1
    # Nothing to say → nothing advertised, rather than an empty-string block.
    assert runtime.server.instructions is None


def test_a_manifest_without_operations_is_loadable(bundled_manifest_dir: Path) -> None:
    """``_server.json`` carries the server-wide index and no operations at all;
    it must not disturb the registry."""
    manifests = load_manifests(bundled_manifest_dir)
    index = next(m for m in manifests if m.server_name == "autods-mcp")

    assert index.operations == []
    assert index.instructions.strip()


# --- delivery ---------------------------------------------------------------


async def test_instructions_reach_a_real_client(
    mcp_settings, make_mcp_app, bundled_manifest_dir: Path, access_token
) -> None:
    """The acceptance test for the whole ticket: the text arrives in
    ``InitializeResult.instructions`` of a real MCP client handshake."""
    settings = mcp_settings(manifest_dir=bundled_manifest_dir)
    app, runtime = make_mcp_app(settings)
    expected = build_instructions(load_manifests(bundled_manifest_dir))

    async with mcp_client_session(app, runtime, token=access_token) as session:
        assert session.instructions == expected

    assert expected.startswith("## AutoDS MCP — start here")


async def test_no_manifests_advertises_no_instructions(
    mcp_settings, make_mcp_app, empty_manifest_dir: Path, access_token
) -> None:
    settings = mcp_settings(manifest_dir=empty_manifest_dir)
    app, runtime = make_mcp_app(settings)

    async with mcp_client_session(app, runtime, token=access_token) as session:
        assert session.instructions is None


# --- P0 guards on the committed manifest text -------------------------------


def _all_text(manifests: list[Manifest]) -> str:
    """Every string the manifests ship to a client, in one blob."""
    parts: list[str] = []
    for manifest in manifests:
        parts.append(manifest.instructions)
        for operation in manifest.operations:
            parts.extend([operation.summary, operation.description, operation.notes or ""])
            parts.append(json.dumps(operation.body_schema or {}))
            parts.extend(parameter.description or "" for parameter in operation.parameters)
    return "\n".join(parts)


def _vocabulary(manifests: list[Manifest]) -> set[str]:
    """Tokens that are part of the wire contract rather than tool names.

    Collected from the manifests themselves — every parameter name, every
    ``body_schema`` property name, and every string ``enum`` value — so the
    guard below doesn't have to be told about ``upload_settings`` or
    ``list_int`` by hand.
    """
    names: set[str] = set()

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                if key == "properties" and isinstance(value, dict):
                    names.update(value)
                elif key == "enum" and isinstance(value, list):
                    names.update(item for item in value if isinstance(item, str))
                walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    for manifest in manifests:
        for operation in manifest.operations:
            names.update(parameter.name for parameter in operation.parameters)
            walk(operation.body_schema)
    return names


# Tool-shaped tokens that appear in prose but name a *field* or an enum value
# rather than a tool, and aren't recoverable from the schemas above: the
# ``bulk_action_type`` vocabulary, two filterable field names, and the one
# ``error_type`` name that reads like a verb (``upload_error``). Extend
# deliberately — the point of the guard is that an unexplained addition here is
# a phantom tool reference.
_NON_TOOL_TOKENS = frozenset(
    {
        "create_draft",
        "import_to_marketplace",
        "import_untracked",
        "products_update",
        "products_delete",
        "drafts_delete",
        "orders_update",
        "drafts_update",
        "products_relist",
        "conversations_update",
        "upload_date",
        "search_query",
        "upload_error",
    }
)

_TOOL_SHAPED = re.compile(r"\b(?:get|list|search|upload|publish|update|create|delete|request)_[a-z0-9_]+\b")


def test_bundled_instructions_are_within_the_target(bundled_manifest_dir: Path) -> None:
    """Baseline before RD-90 was 7,828 chars across five manifests. The cap is
    what keeps the four manifests still to be added (RD-69, RD-89, RD-94, RD-95)
    from compounding it, and what RD-100's playbook index has to fit inside."""
    text = build_instructions(load_manifests(bundled_manifest_dir))

    assert 0 < len(text) <= INSTRUCTIONS_TARGET


def test_manifest_text_names_no_unregistered_tool(bundled_manifest_dir: Path) -> None:
    """Every tool the manifest text tells an agent to call must exist.

    ``products.json`` used to document an ``update_product`` (PUT) /
    ``update_product_note`` (PATCH) workflow for operations this server has
    never registered, and ``stores.json`` referred to ``get_all_stores`` for
    what is registered as ``list_stores_api``. Harmless while the text was
    dropped; a confident call to a missing tool the moment it ships.
    """
    manifests = load_manifests(bundled_manifest_dir)
    registered = {operation.operation_id for operation in build_registry(bundled_manifest_dir).list_operations()}

    mentioned = set(_TOOL_SHAPED.findall(_all_text(manifests))) - _NON_TOOL_TOKENS - _vocabulary(manifests)

    assert mentioned <= registered, f"manifest text names unregistered tools: {sorted(mentioned - registered)}"


def test_no_dangling_pointers_into_the_server_instructions(bundled_manifest_dir: Path) -> None:
    """The enum tables moved into the ``inputSchema`` of the parameters that take
    them, so every "see the … table in server instructions" pointer is stale."""
    assert "server instructions" not in _all_text(load_manifests(bundled_manifest_dir))


def test_list_products_carries_its_own_filter_enums(bundled_manifest_dir: Path) -> None:
    """A tool's filter enums live on that tool, not on a *different* tool.

    ``list_products`` used to resolve its enum-valued filters by pointing at
    ``upload_products``' body fields ("which list them in full") — the same
    indirection the "see the … table in server instructions" pointers were, one
    hop further out, and the guard above doesn't see it. It breaks tier 1 for a
    client that lazy-loads tool schemas (it may not hold `upload_products`' at
    decision time) and for any agent that only ever reads.

    So: whatever ``list_products`` tells the model it can filter on, the values
    must be reachable from ``list_products``' own descriptor.

    The sentinels are the last value of each set, checked against the upstream
    enums (``Region``, ``BuySites``, ``SellSites`` in ``general_enums.py``;
    ``ProductStatus``, ``InventoryStatus``, ``ErrorType`` in ``item.py``), so a
    set that is silently truncated on a later edit fails here.
    """
    tool = to_tool(build_registry(bundled_manifest_dir).get("list_products"))
    own_text = tool.description + json.dumps(tool.input_schema)

    assert "upload_products" not in own_text
    for sentinel in ("6=pre_draft", "3=on_hold", "9=selling_channel_changes", "22=ie", "8=tiktok", "39=kogan"):
        assert sentinel in own_text, f"list_products no longer carries {sentinel!r} itself"


def test_no_operation_promises_a_business_response_it_cannot_deliver(bundled_manifest_dir: Path) -> None:
    """ "Business response"/"business error" is only deliverable for a 2xx.

    ``business_errors`` renders a ``business_error`` field on an HTTP 200 only.
    A non-2xx goes through ``map_upstream_error``, which hands the client a
    generic typed error and logs the upstream detail server-side — so text
    telling the model to expect a readable business response on a redirect or a
    4xx describes something the client will never see. (``get_product_by_id``'s
    307 said exactly that.) If an operation's text makes the promise, it must
    declare the block that keeps it.
    """
    for manifest in load_manifests(bundled_manifest_dir):
        for operation in manifest.operations:
            text = f"{operation.summary} {operation.description} {operation.notes or ''}".lower()
            if "business response" in text or "business error" in text:
                assert operation.business_errors is not None, (
                    f"'{operation.operation_id}' promises a business response but declares no "
                    f"business_errors block; only a 2xx payload can carry one."
                )
