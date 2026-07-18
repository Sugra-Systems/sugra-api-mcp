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


def test_head_requests_allowed_on_public_paths(client: TestClient) -> None:
    # Uptime monitors and load balancers commonly probe with HEAD.
    for path in ("/", "/health"):
        resp = client.head(path)
        assert resp.status_code == 200, path


def test_public_initialize_still_passes(client: TestClient) -> None:
    resp = client.post(
        "/mcp",
        json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
    )
    assert resp.status_code == 200


def test_allowlist_is_exactly_root_and_health() -> None:
    assert frozenset({"/", "/health"}) == PUBLIC_GET_PATHS


def test_slash_variants_stay_behind_auth(client: TestClient) -> None:
    # STRICT matching: normalization tricks never widen the public surface.
    # (// and //// are exercised at the ASGI layer below - httpx normalizes
    # or rejects them client-side before the server ever sees them.)
    for path in ("/health/", "/health//", "/HEALTH"):
        resp = client.get(path)
        assert resp.status_code == 401, path


@pytest.mark.anyio
async def test_raw_slash_paths_stay_behind_auth() -> None:
    """Drive the middleware at the ASGI layer with paths httpx cannot send."""
    from starlette.applications import Starlette
    from starlette.responses import JSONResponse
    from starlette.routing import Route

    async def ok(_request):
        return JSONResponse({"ok": True})

    app = Starlette(routes=[Route("/", ok), Route("/health", ok)])
    config = AuthConfig(
        app_url="https://app.sugra.ai",
        jwks_url="https://app.sugra.ai/oauth/jwks.json",
        internal_token="test-internal-token",
    )
    app.add_middleware(AuthMiddleware, authenticator=Authenticator(config))

    def make_channel(statuses: list[int]):
        async def receive():
            return {"type": "http.request", "body": b"", "more_body": False}

        async def send(message, _statuses=statuses):
            if message["type"] == "http.response.start":
                _statuses.append(message["status"])

        return receive, send

    for raw_path in ("//", "////", "//health"):
        status_holder: list[int] = []
        receive, send = make_channel(status_holder)

        scope = {
            "type": "http",
            "method": "GET",
            "path": raw_path,
            "raw_path": raw_path.encode(),
            "query_string": b"",
            "headers": [],
        }
        await app(scope, receive, send)
        assert status_holder and status_holder[0] == 401, raw_path


def test_real_http_app_route_registration() -> None:
    """Pin the PROD app shape: streamable_http_app + appended public routes."""
    from starlette.routing import Route

    from sugra_api_mcp.server import mcp
    from sugra_api_mcp.web import health, landing

    app = mcp.streamable_http_app()
    app.router.routes.append(Route("/", landing, methods=["GET"]))
    app.router.routes.append(Route("/health", health, methods=["GET"]))
    paths = [getattr(r, "path", None) for r in app.router.routes]
    # /mcp stays FIRST (appended routes cannot shadow it); both new routes present.
    assert paths.index("/mcp") < paths.index("/")
    assert paths.index("/mcp") < paths.index("/health")
