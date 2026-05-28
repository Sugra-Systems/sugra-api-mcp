"""Tests for CORS middleware on the HTTP transport.

Browser-based MCP clients (ChatGPT Connectors UI) send a CORS preflight on
`OPTIONS /mcp` before the actual call. Without an exact-match Origin and the
right exposed headers, the browser blocks the request and the connector add
flow fails before reaching the server's auth layer. Server-to-server clients
(claude.ai backend, Codex CLI, stdio transports) do not exercise this path.
"""

from __future__ import annotations

import pytest
from starlette.applications import Starlette
from starlette.middleware.cors import CORSMiddleware
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from sugra_api_mcp.auth import Authenticator, AuthMiddleware
from sugra_api_mcp.config import (
    DEFAULT_ALLOWED_ORIGINS,
    AuthConfig,
    load_allowed_origins,
)


@pytest.fixture
def auth_config() -> AuthConfig:
    return AuthConfig(
        app_url="https://app.sugra.ai",
        jwks_url="https://app.sugra.ai/oauth/jwks.json",
        internal_token="test-internal-token",
    )


def _build_app(auth_config: AuthConfig, allowed_origins: list[str]) -> Starlette:
    """Mirror the middleware order from sugra_api_mcp.__main__._run_server."""

    async def ok(_request):
        return JSONResponse({"ok": True})

    app = Starlette(routes=[Route("/mcp", ok, methods=["GET", "POST", "OPTIONS"])])
    app.add_middleware(AuthMiddleware, authenticator=Authenticator(auth_config))
    app.add_middleware(
        CORSMiddleware,
        allow_origins=allowed_origins,
        allow_methods=["GET", "POST", "DELETE", "OPTIONS"],
        allow_headers=[
            "Authorization",
            "Content-Type",
            "Mcp-Session-Id",
            "MCP-Protocol-Version",
            "Accept",
            "Last-Event-ID",
        ],
        expose_headers=[
            "WWW-Authenticate",
            "Mcp-Session-Id",
            "MCP-Protocol-Version",
        ],
        max_age=86400,
    )
    return app


@pytest.mark.parametrize(
    "origin",
    [
        "https://chatgpt.com",
        "https://chat.openai.com",
        "https://claude.ai",
        "https://cursor.sh",
    ],
)
def test_cors_preflight_allows_known_origins(auth_config, origin):
    app = _build_app(auth_config, list(DEFAULT_ALLOWED_ORIGINS))

    response = TestClient(app).options(
        "/mcp",
        headers={
            "Origin": origin,
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "authorization, content-type, mcp-session-id",
        },
    )

    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == origin
    allowed_methods = response.headers["access-control-allow-methods"]
    for method in ("GET", "POST", "DELETE", "OPTIONS"):
        assert method in allowed_methods
    allowed_headers = response.headers["access-control-allow-headers"].lower()
    for header in ("authorization", "content-type", "mcp-session-id"):
        assert header in allowed_headers
    assert response.headers["access-control-max-age"] == "86400"


def test_cors_preflight_blocks_unknown_origin(auth_config):
    app = _build_app(auth_config, list(DEFAULT_ALLOWED_ORIGINS))

    response = TestClient(app).options(
        "/mcp",
        headers={
            "Origin": "https://evil.example",
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "authorization",
        },
    )

    # Starlette's CORSMiddleware returns 400 for disallowed origins and omits
    # the Allow-Origin response header, which is what browsers need to see to
    # block the request.
    assert "access-control-allow-origin" not in response.headers


def test_cors_preflight_bypasses_auth_middleware(auth_config):
    """OPTIONS preflight must not return 401 from the auth layer.

    Browsers send preflight without Authorization. If AuthMiddleware ran first
    it would reply 401, which CORS-preflight handlers in browsers treat as
    failure and the actual request never fires.
    """
    app = _build_app(auth_config, list(DEFAULT_ALLOWED_ORIGINS))

    response = TestClient(app).options(
        "/mcp",
        headers={
            "Origin": "https://chatgpt.com",
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "authorization",
        },
    )

    assert response.status_code != 401


def test_cors_actual_response_exposes_www_authenticate_and_session_id(auth_config):
    """Browser JS needs explicit Expose-Headers to read these on a CORS response.

    The ChatGPT connector flow reads WWW-Authenticate from the 401 to discover
    the OAuth metadata URL, and Mcp-Session-Id from a 200 to keep the
    streamable HTTP session pinned to a single back-end.
    """
    app = _build_app(auth_config, list(DEFAULT_ALLOWED_ORIGINS))

    response = TestClient(app).get(
        "/mcp",
        headers={"Origin": "https://chatgpt.com"},
    )

    assert response.status_code == 401
    assert response.headers["access-control-allow-origin"] == "https://chatgpt.com"
    exposed = response.headers["access-control-expose-headers"].lower()
    for header in ("www-authenticate", "mcp-session-id", "mcp-protocol-version"):
        assert header in exposed


def test_cors_post_without_origin_still_works(auth_config):
    """Server-to-server callers (no Origin header) must keep working.

    claude.ai backend, Codex CLI, curl - none of these send Origin. Adding
    CORS must be transparent for them.
    """
    app = _build_app(auth_config, list(DEFAULT_ALLOWED_ORIGINS))

    response = TestClient(app).post(
        "/mcp",
        json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
    )

    assert response.status_code == 200
    assert "access-control-allow-origin" not in response.headers


def test_load_allowed_origins_defaults(monkeypatch):
    monkeypatch.delenv("SUGRA_MCP_ALLOWED_ORIGINS", raising=False)
    origins = load_allowed_origins()
    assert "https://chatgpt.com" in origins
    assert "https://claude.ai" in origins


def test_load_allowed_origins_env_override(monkeypatch):
    monkeypatch.setenv(
        "SUGRA_MCP_ALLOWED_ORIGINS",
        "https://example.com, https://other.example",
    )
    origins = load_allowed_origins()
    assert origins == ["https://example.com", "https://other.example"]


def test_load_allowed_origins_wildcard(monkeypatch):
    monkeypatch.setenv("SUGRA_MCP_ALLOWED_ORIGINS", "*")
    origins = load_allowed_origins()
    assert origins == ["*"]


def test_load_allowed_origins_empty_string_falls_back_to_defaults(monkeypatch):
    monkeypatch.setenv("SUGRA_MCP_ALLOWED_ORIGINS", "   ")
    origins = load_allowed_origins()
    assert "https://chatgpt.com" in origins


def test_transport_security_includes_cors_origins(monkeypatch):
    """Inner FastMCP DNS rebinding middleware must accept the same Origins as outer CORS.

    Without this, the outer Starlette CORS layer would let preflight through
    only to have the actual POST rejected with 403 Invalid Origin by
    TransportSecurityMiddleware (mcp/server/transport_security.py).
    """
    from sugra_api_mcp.server import _build_transport_security

    monkeypatch.setenv("SUGRA_MCP_ALLOWED_HOSTS", "app.sugra.ai")
    monkeypatch.delenv("SUGRA_MCP_ALLOWED_ORIGINS", raising=False)

    settings = _build_transport_security()

    assert settings is not None
    assert settings.enable_dns_rebinding_protection is True
    assert "app.sugra.ai" in settings.allowed_hosts
    # Both same-origin (https://app.sugra.ai derived from hosts) and the CORS
    # browser-client origins must be present.
    assert "https://app.sugra.ai" in settings.allowed_origins
    assert "https://chatgpt.com" in settings.allowed_origins
    assert "https://claude.ai" in settings.allowed_origins


def test_transport_security_disabled_when_no_allowed_hosts(monkeypatch):
    """Local stdio / dev: no hosted reverse proxy means no transport security."""
    from sugra_api_mcp.server import _build_transport_security

    monkeypatch.delenv("SUGRA_MCP_ALLOWED_HOSTS", raising=False)
    assert _build_transport_security() is None


def test_transport_security_wildcard_disables_inner_protection(monkeypatch):
    """`SUGRA_MCP_ALLOWED_ORIGINS=*` opts out of the inner Origin check.

    FastMCP's middleware has no wildcard syntax (only exact match + `:*` port
    suffix), so the only way to honor the operator-stated intent of "trust
    outer CORS" is to disable DNS rebinding protection entirely. Bearer auth
    still gates tool calls. This is documented as self-hosted/dev only.
    """
    from sugra_api_mcp.server import _build_transport_security

    monkeypatch.setenv("SUGRA_MCP_ALLOWED_HOSTS", "app.sugra.ai")
    monkeypatch.setenv("SUGRA_MCP_ALLOWED_ORIGINS", "*")

    settings = _build_transport_security()

    assert settings is not None
    assert settings.enable_dns_rebinding_protection is False


def test_transport_security_honors_custom_origins_env(monkeypatch):
    from sugra_api_mcp.server import _build_transport_security

    monkeypatch.setenv("SUGRA_MCP_ALLOWED_HOSTS", "app.sugra.ai")
    monkeypatch.setenv("SUGRA_MCP_ALLOWED_ORIGINS", "https://custom.example")

    settings = _build_transport_security()

    assert settings is not None
    assert "https://custom.example" in settings.allowed_origins
    # Defaults must NOT leak in when an explicit override is given.
    assert "https://chatgpt.com" not in settings.allowed_origins


def test_transport_security_middleware_accepts_chatgpt_origin(monkeypatch):
    """End-to-end: simulate a POST with Origin: chatgpt.com through the actual
    FastMCP TransportSecurityMiddleware and confirm it does not 403.

    Without the cors_origins merge in _build_transport_security() this test
    would fail with HTTP 403 Invalid Origin, which is exactly the production
    bug this test pins.
    """
    import asyncio

    from mcp.server.transport_security import TransportSecurityMiddleware
    from starlette.requests import Request

    from sugra_api_mcp.server import _build_transport_security

    monkeypatch.setenv("SUGRA_MCP_ALLOWED_HOSTS", "app.sugra.ai")
    monkeypatch.delenv("SUGRA_MCP_ALLOWED_ORIGINS", raising=False)

    settings = _build_transport_security()
    middleware = TransportSecurityMiddleware(settings)

    request = Request(
        {
            "type": "http",
            "method": "POST",
            "headers": [
                (b"host", b"app.sugra.ai"),
                (b"origin", b"https://chatgpt.com"),
                (b"content-type", b"application/json"),
            ],
            "query_string": b"",
            "path": "/mcp",
        }
    )

    result = asyncio.run(middleware.validate_request(request, is_post=True))

    # validate_request returns None on success, a Response on failure.
    assert result is None, f"Inner transport security rejected ChatGPT Origin: {result!r}"
