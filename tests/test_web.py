"""Tests for the public web surface: landing page, /health, auth boundary."""

from __future__ import annotations

import pytest
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from sugra_api_mcp import __version__
from sugra_api_mcp.auth import PUBLIC_GET_PATHS, Authenticator, AuthMiddleware
from sugra_api_mcp.config import AuthConfig
from sugra_api_mcp.web import health, landing


@pytest.fixture
def client() -> TestClient:
    """Mirror the prod app shape: routes + AuthMiddleware (inner layer)."""

    async def mcp_ok(_request):
        return JSONResponse({"ok": True})

    app = Starlette(
        routes=[
            Route("/mcp", mcp_ok, methods=["GET", "POST"]),
            Route("/", landing, methods=["GET"]),
            Route("/health", health, methods=["GET"]),
        ]
    )
    config = AuthConfig(
        app_url="https://app.sugra.ai",
        jwks_url="https://app.sugra.ai/oauth/jwks.json",
        internal_token="test-internal-token",
    )
    app.add_middleware(AuthMiddleware, authenticator=Authenticator(config))
    return TestClient(app, raise_server_exceptions=False)


def test_landing_serves_html_unauthenticated(client: TestClient) -> None:
    resp = client.get("/")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/html")
    assert "https://mcp.sugra.ai/mcp" in resp.text
    assert "Sugra API MCP" in resp.text


def test_health_serves_json_unauthenticated(client: TestClient) -> None:
    resp = client.get("/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert body["service"] == "sugra-api-mcp"
    assert body["version"] == __version__


def test_unknown_path_still_requires_auth(client: TestClient) -> None:
    resp = client.get("/anything-else")
    assert resp.status_code == 401
    assert resp.json() == {"error": "missing_bearer_token"}


def test_get_mcp_still_requires_auth(client: TestClient) -> None:
    resp = client.get("/mcp")
    assert resp.status_code == 401


def test_post_to_public_paths_not_exempt(client: TestClient) -> None:
    # The allowlist is GET-only: a POST to / or /health must hit auth.
    for path in ("/", "/health"):
        resp = client.post(path)
        assert resp.status_code == 401, path


def test_public_initialize_still_passes(client: TestClient) -> None:
    resp = client.post(
        "/mcp",
        json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
    )
    assert resp.status_code == 200


def test_allowlist_is_exactly_root_and_health() -> None:
    assert PUBLIC_GET_PATHS == frozenset({"", "/health"})
