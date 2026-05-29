"""A2 acceptance — /health endpoint returns 200 OK."""

from fastapi.testclient import TestClient

from autods_mcp_server.app import create_app


def test_health_returns_ok(env) -> None:
    env(
        MCP_ENV="local",
        COGNITO_USER_POOL_ID="staging_pool_id",
    )

    app = create_app()
    with TestClient(app) as client:
        response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
