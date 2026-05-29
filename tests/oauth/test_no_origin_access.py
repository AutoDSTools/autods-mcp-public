"""Discovery + DCR endpoints must work for non-browser MCP clients.

Server-side MCP clients (Claude's backend, Cursor) fetch the discovery
documents and call the DCR shim without an ``Origin`` header — that's a
browser concept. The Origin allowlist therefore only rejects a *present*
foreign Origin; a missing one is allowed (the DNS-rebinding Host check
still applies). These tests pin that behaviour at the wire level on the
production-like staging path (PUBLIC_HOSTNAME set, HTTPS required).
"""

from autods_mcp_server.settings import Settings

# Honest Host + scheme, but deliberately no Origin header.
_NO_ORIGIN_HEADERS = {"host": "mcp.autods.com", "x-forwarded-proto": "https"}


def test_prm_served_without_origin_header(staging_settings: Settings, client_factory) -> None:
    with client_factory(staging_settings) as client:
        response = client.get("/.well-known/oauth-protected-resource", headers=_NO_ORIGIN_HEADERS)
    assert response.status_code == 200
    assert response.json()["resource"] == "https://mcp.autods.com/mcp"


def test_as_metadata_served_without_origin_header(staging_settings: Settings, client_factory) -> None:
    with client_factory(staging_settings) as client:
        response = client.get("/.well-known/oauth-authorization-server", headers=_NO_ORIGIN_HEADERS)
    assert response.status_code == 200
    assert response.json()["registration_endpoint"] == "https://mcp.autods.com/oauth/register"


def test_register_accepts_request_without_origin_header(staging_settings: Settings, client_factory) -> None:
    with client_factory(staging_settings) as client:
        response = client.post(
            "/oauth/register",
            headers=_NO_ORIGIN_HEADERS,
            json={"redirect_uris": ["https://claude.ai/api/mcp/auth_callback"]},
        )
    assert response.status_code == 201


def test_foreign_origin_still_rejected_when_present(staging_settings: Settings, client_factory) -> None:
    """Relaxing *missing* Origin must not relax a *present* foreign Origin."""
    with client_factory(staging_settings) as client:
        response = client.get(
            "/.well-known/oauth-protected-resource",
            headers={**_NO_ORIGIN_HEADERS, "origin": "https://evil.example"},
        )
    assert response.status_code == 403
