"""A2 acceptance — /health endpoint returns 200 OK."""

from fastapi.testclient import TestClient

from autods_mcp_server.app import create_app


def test_health_returns_ok() -> None:
    app = create_app()
    with TestClient(app) as client:
        response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
