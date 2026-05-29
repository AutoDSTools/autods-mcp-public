"""C2 acceptance — GET /.well-known/oauth-protected-resource."""


def test_prm_returns_resource_and_authorization_servers(local_settings, client_factory) -> None:
    with client_factory(local_settings) as client:
        response = client.get(
            "/.well-known/oauth-protected-resource",
            headers={"origin": "http://localhost:6274"},
        )
    assert response.status_code == 200
    body = response.json()
    assert body["resource"].startswith("http://")
    # resource is the canonical MCP URL; authorization_servers is the proxy
    # AS issuer (the bare origin the AS-metadata well-known path hangs off).
    assert body["resource"].endswith("/mcp")
    assert body["authorization_servers"] == [body["resource"].removesuffix("/mcp")]
    assert body["scopes_supported"] == ["email", "openid", "phone", "profile"]
    assert body["bearer_methods_supported"] == ["header"]


def test_prm_pins_to_public_hostname_in_staging(staging_settings, client_factory) -> None:
    """The resource URL must come from PUBLIC_HOSTNAME, ignoring X-Forwarded-Host.

    The Origin middleware's DNS-rebinding check enforces ``Host == PUBLIC_HOSTNAME``,
    but X-Forwarded-Host is a separate sneak path — Starlette's request.url
    construction can be influenced by it. The test sends an attacker-controlled
    X-Forwarded-Host while keeping Host honest, and asserts the response URL
    still pins to PUBLIC_HOSTNAME.
    """
    with client_factory(staging_settings) as client:
        response = client.get(
            "/.well-known/oauth-protected-resource",
            headers={
                "origin": "https://claude.ai",
                "host": "mcp.autods.com",
                "x-forwarded-host": "evil.example.com",
                "x-forwarded-proto": "https",
            },
        )
    assert response.status_code == 200
    body = response.json()
    assert body["resource"] == "https://mcp.autods.com/mcp"
    assert body["authorization_servers"] == ["https://mcp.autods.com"]
    assert "evil.example.com" not in response.text


def test_prm_requires_allowed_origin(staging_settings, client_factory) -> None:
    """PRM sits behind the Origin allowlist like every other /.well-known/ route."""
    with client_factory(staging_settings) as client:
        response = client.get(
            "/.well-known/oauth-protected-resource",
            headers={
                "origin": "https://evil.example",
                "host": "mcp.autods.com",
                "x-forwarded-proto": "https",
            },
        )
    assert response.status_code == 403
    assert response.json()["error"] == "origin_not_allowed"


def test_prm_inspector_origin_passes_in_staging(staging_settings, client_factory) -> None:
    """The hosted Inspector origin is allowlisted for staging."""
    with client_factory(staging_settings) as client:
        response = client.get(
            "/.well-known/oauth-protected-resource",
            headers={
                "origin": "https://inspector.modelcontextprotocol.io",
                "host": "mcp.autods.com",
                "x-forwarded-proto": "https",
            },
        )
    assert response.status_code == 200


def test_prm_scopes_reflect_settings(env, client_factory) -> None:
    """If the operator overrides MCP_OAUTH_SCOPES, the PRM document follows."""
    env(
        MCP_ENV="local",
        COGNITO_USER_POOL_ID="us-west-2_TESTPOOL",
        ALLOWED_COGNITO_CLIENT_IDS='["c"]',
        COGNITO_PUBLIC_CLIENT_ID="c",
        COGNITO_DOMAIN="autods.auth.us-west-2.amazoncognito.com",
        MCP_REGISTRATION_REDIRECT_URIS='["http://localhost:8000/callback"]',
        MCP_OAUTH_SCOPES='["openid","AutoDS/custom"]',
    )
    from autods_mcp_server.settings import Settings

    settings = Settings()  # type: ignore[call-arg]
    with client_factory(settings) as client:
        response = client.get(
            "/.well-known/oauth-protected-resource",
            headers={"origin": "http://localhost:6274"},
        )
    assert response.json()["scopes_supported"] == ["openid", "AutoDS/custom"]
