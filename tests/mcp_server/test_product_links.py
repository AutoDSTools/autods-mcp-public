"""RD-92: the web-app link each rendered product carries.

Two properties are worth more than the rest here, and both are about *not*
producing a link. A URL built from a missing piece still looks like a link, so
the user clicks it, lands on a plausible page, and concludes the product is
gone — which is strictly worse than an image that was never clickable. And a
link built in the wrong environment resolves perfectly: a staging widget would
walk the user into production.
"""

import pytest

from autods_mcp_server.manifests.schema import LinkBlock
from autods_mcp_server.product_links import build_link, gate_matches, route_names, route_needs_id
from autods_mcp_server.settings import McpEnv, Settings

_STAGING = "https://v2-staging.autods.com"
_ACTIVE_BODY = {"body": {"product_status": 2, "limit": 10}}


def _gated() -> LinkBlock:
    return LinkBlock(route="store_product", when_body_field="product_status", when_body_equals=2)


# --------------------------------------------------------------------------
# Building a URL
# --------------------------------------------------------------------------


def test_an_active_product_links_to_its_single_product_page() -> None:
    link = build_link(_gated(), identifier="4242", arguments=_ACTIVE_BODY, base_url=_STAGING)
    assert link == "https://v2-staging.autods.com/products/4242"


def test_a_trailing_slash_on_the_host_does_not_double_up() -> None:
    link = build_link(_gated(), identifier="4242", arguments=_ACTIVE_BODY, base_url=_STAGING + "/")
    assert link == "https://v2-staging.autods.com/products/4242"


@pytest.mark.parametrize(
    "arguments",
    [
        pytest.param({"body": {"product_status": 1}}, id="draft"),
        pytest.param({"body": {"product_status": 3}}, id="ended"),
        pytest.param({"body": {"product_status": "2"}}, id="status-as-a-string"),
        pytest.param({"body": {}}, id="no-status"),
        pytest.param({"store_ids": "7"}, id="no-body"),
        pytest.param(None, id="no-arguments-at-all"),
    ],
)
def test_a_call_the_gate_does_not_match_gets_no_link(arguments: object) -> None:
    """Only status 2 is linked. Everything else — including a status sent in the
    wrong type, which the upstream would reject anyway — falls through to no
    link rather than to a link built on an assumption."""
    assert build_link(_gated(), identifier="4242", arguments=arguments, base_url=_STAGING) is None  # type: ignore[arg-type]


def test_an_item_with_no_id_gets_no_link() -> None:
    """A route with an ``{id}`` and nothing to put in it must produce nothing —
    never a URL with a hole in it."""
    assert build_link(_gated(), identifier=None, arguments=_ACTIVE_BODY, base_url=_STAGING) is None
    assert build_link(_gated(), identifier="", arguments=_ACTIVE_BODY, base_url=_STAGING) is None


def test_no_host_means_no_link() -> None:
    assert build_link(_gated(), identifier="4242", arguments=_ACTIVE_BODY, base_url=None) is None
    assert build_link(_gated(), identifier="4242", arguments=_ACTIVE_BODY, base_url="") is None


def test_an_unknown_route_produces_nothing_rather_than_a_hostname() -> None:
    """The boot lint is what stops this shipping; the runtime still refuses,
    because half a URL is the failure this whole module is written against."""
    block = LinkBlock(route="not_a_route")
    assert build_link(block, identifier="4242", arguments=_ACTIVE_BODY, base_url=_STAGING) is None


def test_an_ungated_block_matches_every_call() -> None:
    assert gate_matches(LinkBlock(route="store_product"), None) is True
    assert gate_matches(LinkBlock(route="store_product"), {"body": {"product_status": 6}}) is True


def test_the_route_table_reports_what_it_serves() -> None:
    assert route_names() == ["store_product"]
    assert route_needs_id("store_product") is True
    assert route_needs_id("not_a_route") is False


# --------------------------------------------------------------------------
# The host is the environment's, never the caller's
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("deploy_env", "expected"),
    [
        (McpEnv.prod, "https://platform.autods.com"),
        (McpEnv.staging, "https://v2-staging.autods.com"),
        (McpEnv.local, "https://v2-staging.autods.com"),
    ],
)
def test_the_app_host_follows_the_environment(deploy_env: McpEnv, expected: str, mcp_settings) -> None:
    """A staging deploy must not be able to hand out a production link. Nothing
    about a link fails when it does — the id resolves, the page loads, and it is
    someone else's catalogue — so this is pinned rather than reviewed."""
    overrides = {"MCP_ENV": deploy_env.value}
    if deploy_env is not McpEnv.local:
        overrides |= {"FORCE_HTTPS": "true", "PUBLIC_HOSTNAME": "mcp.test", "REDIS_URL": "redis://redis.test:6379/0"}
    settings: Settings = mcp_settings(**overrides)
    assert settings.app_base_url == expected


def test_an_explicit_override_wins_and_loses_its_trailing_slash(mcp_settings) -> None:
    settings: Settings = mcp_settings(AUTODS_APP_BASE_URL="https://app.example.test/")
    assert settings.app_base_url == "https://app.example.test"
