"""Unit tests for sugra_api_mcp.auth."""

from __future__ import annotations

import time
from unittest.mock import ANY, AsyncMock, MagicMock, patch

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from sugra_api_mcp.auth import (
    MAX_PUBLIC_MCP_BATCH_ITEMS,
    MAX_PUBLIC_MCP_CONTENT_LENGTH,
    Authenticator,
    AuthError,
    AuthMiddleware,
    ResolvedAuth,
    _CachedKey,
    _JwtClaims,
)
from sugra_api_mcp.config import AuthConfig


@pytest.fixture
def rsa_keypair() -> tuple[rsa.RSAPrivateKey, str]:
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    public_pem = private_key.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    ).decode()
    return private_key, public_pem


@pytest.fixture
def auth_config() -> AuthConfig:
    return AuthConfig(
        app_url="https://app.sugra.ai",
        jwks_url="https://app.sugra.ai/oauth/jwks.json",
        internal_token="test-internal-token",
    )


def _make_jwt(private_key: rsa.RSAPrivateKey, payload: dict) -> str:
    private_pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    return jwt.encode(payload, private_pem, algorithm="RS256")


async def test_api_key_passthrough(auth_config):
    auth = Authenticator(auth_config)
    resolved = await auth.resolve("sugra_ao1_abc123xyz")
    assert resolved.api_key == "sugra_ao1_abc123xyz"
    assert resolved.user_id is None
    assert resolved.access_token_id is None
    assert resolved.method == "api_key"


async def test_empty_token_rejected(auth_config):
    auth = Authenticator(auth_config)
    with pytest.raises(AuthError, match="Empty"):
        await auth.resolve("   ")


async def test_jwt_with_valid_signature_and_cached_key(auth_config, rsa_keypair):
    private_key, public_pem = rsa_keypair
    now = int(time.time())
    token = _make_jwt(
        private_key,
        {
            "iss": "https://app.sugra.ai",
            "aud": "https://app.sugra.ai/mcp",
            "jti": "cached-token-id",
            "sub": "42",
            "exp": now + 3600,
            "iat": now,
            "scope": "sugra:read",
        },
    )

    auth = Authenticator(auth_config)
    mock_signing_key = MagicMock()
    mock_signing_key.key = public_pem

    auth._jwks.get_signing_key_from_jwt = MagicMock(return_value=mock_signing_key)
    # No-kid tokens route through the keys-list fallback now.
    auth._jwks.get_signing_keys = MagicMock(return_value=[mock_signing_key])
    auth._api_key_cache[42] = _CachedKey(
        api_key="sugra_cached_key",
        expires_at=time.time() + 60,
    )

    async def fake_post(self, url, headers=None, json=None, extensions=None):
        return httpx.Response(204)

    with patch("httpx.AsyncClient.post", new=fake_post):
        resolved = await auth.resolve(token)

    assert resolved.api_key == "sugra_cached_key"
    assert resolved.user_id == 42
    assert resolved.access_token_id == "cached-token-id"
    assert resolved.method == "oauth"


async def test_passport_jwt_without_issuer_with_mcp_audience_is_accepted(auth_config, rsa_keypair):
    private_key, public_pem = rsa_keypair
    now = int(time.time())
    token = _make_jwt(
        private_key,
        {
            "aud": "https://app.sugra.ai/mcp",
            "jti": "test-token-id",
            "sub": "42",
            "exp": now + 3600,
            "iat": now,
            "nbf": now,
            "scopes": ["sugra:read"],
        },
    )

    auth = Authenticator(auth_config)
    mock_signing_key = MagicMock()
    mock_signing_key.key = public_pem

    auth._jwks.get_signing_key_from_jwt = MagicMock(return_value=mock_signing_key)
    # No-kid tokens route through the keys-list fallback now.
    auth._jwks.get_signing_keys = MagicMock(return_value=[mock_signing_key])
    auth._api_key_cache[42] = _CachedKey(
        api_key="sugra_cached_key",
        expires_at=time.time() + 60,
    )

    posts = []

    async def fake_post(self, url, headers=None, json=None, extensions=None):
        posts.append((url, headers, json))
        return httpx.Response(204)

    with patch("httpx.AsyncClient.post", new=fake_post):
        resolved = await auth.resolve(token)

    assert resolved.api_key == "sugra_cached_key"
    assert resolved.user_id == 42
    assert resolved.access_token_id == "test-token-id"
    assert resolved.method == "oauth"
    assert resolved.platform is None
    assert posts == [
        (
            "https://app.sugra.ai/api/internal/mcp/activity",
            {"X-Internal-Token": "test-internal-token"},
            {"user_id": 42, "access_token_id": "test-token-id"},
        )
    ]


async def test_jwt_activity_pass_carries_allowlisted_platform(auth_config, rsa_keypair):
    private_key, public_pem = rsa_keypair
    now = int(time.time())
    token = _make_jwt(
        private_key,
        {
            "aud": "https://app.sugra.ai/mcp",
            "jti": "platform-token-id",
            "sub": "42",
            "exp": now + 3600,
            "iat": now,
            "nbf": now,
            "scopes": ["sugra:read"],
        },
    )
    auth = Authenticator(auth_config)
    mock_signing_key = MagicMock()
    mock_signing_key.key = public_pem
    auth._jwks.get_signing_key_from_jwt = MagicMock(return_value=mock_signing_key)
    auth._jwks.get_signing_keys = MagicMock(return_value=[mock_signing_key])
    auth._api_key_cache[42] = _CachedKey(
        api_key="sugra_cached_key",
        expires_at=time.time() + 60,
    )

    async def fake_post(self, url, headers=None, json=None, extensions=None):
        return httpx.Response(200, json={"ok": True, "platform": "anthropic", "connection_id": 9})

    with patch("httpx.AsyncClient.post", new=fake_post):
        resolved = await auth.resolve(token)
    assert resolved.platform == "anthropic"
    cached = await auth._validate_mcp_access(_JwtClaims(user_id=42, access_token_id="platform-token-id"))
    assert cached.platform == "anthropic"


async def test_jwt_activity_unknown_platform_is_omitted(auth_config, rsa_keypair):
    private_key, public_pem = rsa_keypair
    now = int(time.time())
    token = _make_jwt(
        private_key,
        {
            "aud": "https://app.sugra.ai/mcp",
            "jti": "unknown-platform-id",
            "sub": "42",
            "exp": now + 3600,
            "iat": now,
            "nbf": now,
            "scopes": ["sugra:read"],
        },
    )
    auth = Authenticator(auth_config)
    mock_signing_key = MagicMock()
    mock_signing_key.key = public_pem
    auth._jwks.get_signing_key_from_jwt = MagicMock(return_value=mock_signing_key)
    auth._jwks.get_signing_keys = MagicMock(return_value=[mock_signing_key])
    auth._api_key_cache[42] = _CachedKey(
        api_key="sugra_cached_key",
        expires_at=time.time() + 60,
    )

    async def fake_post(self, url, headers=None, json=None, extensions=None):
        return httpx.Response(200, json={"ok": True, "platform": "not-a-vendor"})

    with patch("httpx.AsyncClient.post", new=fake_post):
        resolved = await auth.resolve(token)
    assert resolved.platform is None


async def test_jwt_activity_validation_failure_denies_auth(auth_config, rsa_keypair):
    private_key, public_pem = rsa_keypair
    now = int(time.time())
    token = _make_jwt(
        private_key,
        {
            "aud": "https://app.sugra.ai/mcp",
            "jti": "activity-token-id",
            "sub": "42",
            "exp": now + 3600,
            "iat": now,
            "nbf": now,
            "scopes": ["sugra:read"],
        },
    )

    auth = Authenticator(auth_config)
    mock_signing_key = MagicMock()
    mock_signing_key.key = public_pem

    auth._jwks.get_signing_key_from_jwt = MagicMock(return_value=mock_signing_key)
    # No-kid tokens route through the keys-list fallback now.
    auth._jwks.get_signing_keys = MagicMock(return_value=[mock_signing_key])
    auth._api_key_cache[42] = _CachedKey(
        api_key="sugra_cached_key",
        expires_at=time.time() + 60,
    )

    call_count = 0

    async def fake_post(self, url, headers=None, json=None, extensions=None):
        nonlocal call_count
        call_count += 1
        return httpx.Response(403, json={"error": "connection_disconnected"})

    with patch("httpx.AsyncClient.post", new=fake_post), pytest.raises(AuthError) as exc:
        await auth.resolve(token)

    assert exc.value.status == 401
    assert "connection_disconnected" in str(exc.value)
    assert call_count == 1


async def test_jwt_activity_exception_denies_auth(auth_config, rsa_keypair, monkeypatch):
    monkeypatch.setattr("sugra_api_mcp.auth.ACCESS_RETRY_PAUSE_SECONDS", 0.0)
    private_key, public_pem = rsa_keypair
    now = int(time.time())
    token = _make_jwt(
        private_key,
        {
            "aud": "https://app.sugra.ai/mcp",
            "jti": "activity-token-id",
            "sub": "42",
            "exp": now + 3600,
            "iat": now,
            "nbf": now,
            "scopes": ["sugra:read"],
        },
    )

    auth = Authenticator(auth_config)
    mock_signing_key = MagicMock()
    mock_signing_key.key = public_pem

    auth._jwks.get_signing_key_from_jwt = MagicMock(return_value=mock_signing_key)
    # No-kid tokens route through the keys-list fallback now.
    auth._jwks.get_signing_keys = MagicMock(return_value=[mock_signing_key])
    auth._api_key_cache[42] = _CachedKey(
        api_key="sugra_cached_key",
        expires_at=time.time() + 60,
    )

    call_count = 0

    async def fake_post(self, url, headers=None, json=None, extensions=None):
        nonlocal call_count
        call_count += 1
        raise httpx.ConnectError("activity unavailable")

    with patch("httpx.AsyncClient.post", new=fake_post), pytest.raises(AuthError) as exc:
        await auth.resolve(token)

    assert exc.value.status == 502
    assert "validation" in str(exc.value)
    # One retry of a failure on the APP's side, then the 502 (never validated).
    assert call_count == 2


async def test_jwt_missing_jti_raises(auth_config, rsa_keypair):
    private_key, public_pem = rsa_keypair
    now = int(time.time())
    token = _make_jwt(
        private_key,
        {
            "aud": "https://app.sugra.ai/mcp",
            "sub": "42",
            "exp": now + 3600,
            "iat": now,
            "nbf": now,
            "scopes": ["sugra:read"],
        },
    )

    auth = Authenticator(auth_config)
    mock_signing_key = MagicMock()
    mock_signing_key.key = public_pem

    auth._jwks.get_signing_key_from_jwt = MagicMock(return_value=mock_signing_key)
    # No-kid tokens route through the keys-list fallback now.
    auth._jwks.get_signing_keys = MagicMock(return_value=[mock_signing_key])

    with pytest.raises(AuthError, match="jti"):
        await auth.resolve(token)


async def test_raw_api_key_does_not_report_oauth_activity(auth_config):
    auth = Authenticator(auth_config)

    async def fake_post(self, url, headers=None, json=None, extensions=None):
        raise AssertionError("raw API key should not report OAuth activity")

    with patch("httpx.AsyncClient.post", new=fake_post):
        resolved = await auth.resolve("sugra_ao1_abc123xyz")

    assert resolved.api_key == "sugra_ao1_abc123xyz"


async def test_jwt_wrong_audience_raises(auth_config, rsa_keypair):
    private_key, public_pem = rsa_keypair
    token = _make_jwt(
        private_key,
        {
            "aud": "test-client-id",
            "jti": "test-token-id",
            "sub": "42",
            "exp": int(time.time()) + 3600,
            "iat": int(time.time()),
            "nbf": int(time.time()),
            "scopes": ["sugra:read"],
        },
    )

    auth = Authenticator(auth_config)
    mock_signing_key = MagicMock()
    mock_signing_key.key = public_pem
    auth._jwks.get_signing_key_from_jwt = MagicMock(return_value=mock_signing_key)
    # No-kid tokens route through the keys-list fallback now.
    auth._jwks.get_signing_keys = MagicMock(return_value=[mock_signing_key])

    with pytest.raises(AuthError, match="Invalid token"):
        await auth.resolve(token)


async def test_jwt_expired_raises(auth_config, rsa_keypair):
    private_key, public_pem = rsa_keypair
    token = _make_jwt(
        private_key,
        {
            "iss": "https://app.sugra.ai",
            "aud": "https://app.sugra.ai/mcp",
            "sub": "42",
            "exp": int(time.time()) - 60,
            "iat": int(time.time()) - 120,
        },
    )

    auth = Authenticator(auth_config)
    mock_signing_key = MagicMock()
    mock_signing_key.key = public_pem
    auth._jwks.get_signing_key_from_jwt = MagicMock(return_value=mock_signing_key)
    # No-kid tokens route through the keys-list fallback now.
    auth._jwks.get_signing_keys = MagicMock(return_value=[mock_signing_key])

    with pytest.raises(AuthError, match="expired"):
        await auth.resolve(token)


async def test_jwt_missing_sub_raises(auth_config, rsa_keypair):
    private_key, public_pem = rsa_keypair
    token = _make_jwt(
        private_key,
        {
            "iss": "https://app.sugra.ai",
            "aud": "https://app.sugra.ai/mcp",
            "exp": int(time.time()) + 3600,
            "iat": int(time.time()),
        },
    )

    auth = Authenticator(auth_config)
    mock_signing_key = MagicMock()
    mock_signing_key.key = public_pem
    auth._jwks.get_signing_key_from_jwt = MagicMock(return_value=mock_signing_key)
    # No-kid tokens route through the keys-list fallback now.
    auth._jwks.get_signing_keys = MagicMock(return_value=[mock_signing_key])

    with pytest.raises(AuthError, match="sub claim"):
        await auth.resolve(token)


async def test_jwt_wrong_issuer_raises(auth_config, rsa_keypair):
    private_key, public_pem = rsa_keypair
    token = _make_jwt(
        private_key,
        {
            "iss": "https://attacker.example",
            "aud": "https://app.sugra.ai/mcp",
            "sub": "42",
            "exp": int(time.time()) + 3600,
            "iat": int(time.time()),
        },
    )

    auth = Authenticator(auth_config)
    mock_signing_key = MagicMock()
    mock_signing_key.key = public_pem
    auth._jwks.get_signing_key_from_jwt = MagicMock(return_value=mock_signing_key)
    # No-kid tokens route through the keys-list fallback now.
    auth._jwks.get_signing_keys = MagicMock(return_value=[mock_signing_key])

    with pytest.raises(AuthError, match="Invalid token"):
        await auth.resolve(token)


async def test_jwt_missing_read_scope_raises(auth_config, rsa_keypair):
    private_key, public_pem = rsa_keypair
    token = _make_jwt(
        private_key,
        {
            "iss": "https://app.sugra.ai",
            "aud": "https://app.sugra.ai/mcp",
            "sub": "42",
            "exp": int(time.time()) + 3600,
            "iat": int(time.time()),
            "scopes": ["profile"],
        },
    )

    auth = Authenticator(auth_config)
    mock_signing_key = MagicMock()
    mock_signing_key.key = public_pem
    auth._jwks.get_signing_key_from_jwt = MagicMock(return_value=mock_signing_key)
    # No-kid tokens route through the keys-list fallback now.
    auth._jwks.get_signing_keys = MagicMock(return_value=[mock_signing_key])

    with pytest.raises(AuthError, match="sugra:read"):
        await auth.resolve(token)


async def test_lookup_404_raises_with_user_message(auth_config, rsa_keypair):
    private_key, public_pem = rsa_keypair
    token = _make_jwt(
        private_key,
        {
            "iss": "https://app.sugra.ai",
            "aud": "https://app.sugra.ai/mcp",
            "jti": "lookup-404-token-id",
            "sub": "42",
            "exp": int(time.time()) + 3600,
            "iat": int(time.time()),
            "scopes": ["sugra:read"],
        },
    )

    auth = Authenticator(auth_config)
    mock_signing_key = MagicMock()
    mock_signing_key.key = public_pem
    auth._jwks.get_signing_key_from_jwt = MagicMock(return_value=mock_signing_key)
    # No-kid tokens route through the keys-list fallback now.
    auth._jwks.get_signing_keys = MagicMock(return_value=[mock_signing_key])

    mock_response = MagicMock(spec=httpx.Response)
    mock_response.status_code = 404
    mock_response.headers = {"content-type": "application/json"}
    mock_response.json = MagicMock(return_value={"error": "no_api_key", "message": "User has no API key"})

    async def fake_post(self, url, headers=None, json=None, extensions=None):
        return httpx.Response(204)

    async def fake_get(self, url, headers=None):
        return mock_response

    with patch("httpx.AsyncClient.post", new=fake_post), patch("httpx.AsyncClient.get", new=fake_get):
        with pytest.raises(AuthError) as exc:
            await auth.resolve(token)
        assert exc.value.status == 403
        assert "no API key" in str(exc.value)


async def test_lookup_success_caches_result(auth_config, rsa_keypair):
    private_key, public_pem = rsa_keypair
    token = _make_jwt(
        private_key,
        {
            "iss": "https://app.sugra.ai",
            "aud": "https://app.sugra.ai/mcp",
            "jti": "lookup-success-token-id",
            "sub": "7",
            "exp": int(time.time()) + 3600,
            "iat": int(time.time()),
            "scopes": ["sugra:read"],
        },
    )

    auth = Authenticator(auth_config)
    mock_signing_key = MagicMock()
    mock_signing_key.key = public_pem
    auth._jwks.get_signing_key_from_jwt = MagicMock(return_value=mock_signing_key)
    # No-kid tokens route through the keys-list fallback now.
    auth._jwks.get_signing_keys = MagicMock(return_value=[mock_signing_key])

    mock_response = MagicMock(spec=httpx.Response)
    mock_response.status_code = 200
    mock_response.json = MagicMock(
        return_value={"user_id": 7, "api_key": "sugra_test_key", "plan": "free", "daily_limit": 50}
    )

    call_count = 0

    async def fake_get(self, url, headers=None):
        nonlocal call_count
        call_count += 1
        return mock_response

    async def fake_post(self, url, headers=None, json=None, extensions=None):
        return httpx.Response(204)

    with patch("httpx.AsyncClient.post", new=fake_post), patch("httpx.AsyncClient.get", new=fake_get):
        k1 = await auth.resolve(token)
        k2 = await auth.resolve(token)
        assert k1.api_key == "sugra_test_key"
        assert k2.api_key == "sugra_test_key"
        assert call_count == 1


async def test_missing_internal_token_raises_500(auth_config, rsa_keypair):
    private_key, public_pem = rsa_keypair
    config_no_token = AuthConfig(
        app_url="https://app.sugra.ai",
        jwks_url="https://app.sugra.ai/oauth/jwks.json",
        internal_token=None,
    )
    token = _make_jwt(
        private_key,
        {
            "iss": "https://app.sugra.ai",
            "aud": "https://app.sugra.ai/mcp",
            "jti": "missing-internal-token-id",
            "sub": "1",
            "exp": int(time.time()) + 3600,
            "iat": int(time.time()),
            "scopes": ["sugra:read"],
        },
    )

    auth = Authenticator(config_no_token)
    mock_signing_key = MagicMock()
    mock_signing_key.key = public_pem
    auth._jwks.get_signing_key_from_jwt = MagicMock(return_value=mock_signing_key)
    # No-kid tokens route through the keys-list fallback now.
    auth._jwks.get_signing_keys = MagicMock(return_value=[mock_signing_key])

    with pytest.raises(AuthError) as exc:
        await auth.resolve(token)
    assert exc.value.status == 500


def test_auth_401_includes_www_authenticate_header(auth_config):
    async def ok(_request):
        return JSONResponse({"ok": True})

    app = Starlette(routes=[Route("/mcp", ok)])
    app.add_middleware(AuthMiddleware, authenticator=Authenticator(auth_config))

    response = TestClient(app).get("/mcp")

    assert response.status_code == 401
    assert response.headers["WWW-Authenticate"] == (
        'Bearer resource_metadata="https://app.sugra.ai/.well-known/oauth-protected-resource"'
    )


@pytest.mark.parametrize(
    "method",
    [
        "initialize",
        "notifications/initialized",
        "tools/list",
        "resources/list",
        "prompts/list",
        "ping",
    ],
)
def test_auth_middleware_allows_public_mcp_discovery_methods_without_bearer(
    auth_config,
    method,
):
    async def echo_method(request):
        payload = await request.json()
        return JSONResponse({"method": payload["method"]})

    app = Starlette(routes=[Route("/mcp", echo_method, methods=["POST"])])
    app.add_middleware(AuthMiddleware, authenticator=Authenticator(auth_config))

    response = TestClient(app).post(
        "/mcp",
        json={"jsonrpc": "2.0", "id": 1, "method": method, "params": {}},
    )

    assert response.status_code == 200
    assert response.json() == {"method": method}


@pytest.mark.parametrize(
    "method",
    [
        "initialize",
        "notifications/initialized",
        "tools/list",
        "resources/list",
        "prompts/list",
        "ping",
    ],
)
@pytest.mark.parametrize(
    "user_agent",
    ["grok-connectors-manager/0.1.0", "Grok-Connectors-Manager/2.0", " grok-connectors-manager/0.1.0"],
)
def test_auth_middleware_challenges_the_grok_web_connector_on_discovery(
    auth_config,
    method,
    user_agent,
):
    """The Grok web connector reads a 200 on initialize as "no sign-in" and never signs in."""

    async def ok(_request):
        return JSONResponse({"ok": True})

    app = Starlette(routes=[Route("/mcp", ok, methods=["POST"])])
    app.add_middleware(AuthMiddleware, authenticator=Authenticator(auth_config))

    response = TestClient(app).post(
        "/mcp",
        json={"jsonrpc": "2.0", "id": 1, "method": method, "params": {}},
        headers={"user-agent": user_agent},
    )

    assert response.status_code == 401
    assert response.json() == {"error": "missing_bearer_token"}
    assert response.headers["WWW-Authenticate"] == (
        'Bearer resource_metadata="https://app.sugra.ai/.well-known/oauth-protected-resource"'
    )


@pytest.mark.parametrize(
    "user_agent",
    [
        "grok-cli/1.0.51",
        "openai-mcp/1.0.0",
        "Claude-User",
        "python-httpx/0.28.1",
        "mcpbeat/0.1 (+https://mcpbeat.com/bot/; liveness check)",
        "my-grok-connectors-manager/1.0",
    ],
)
def test_auth_middleware_keeps_anonymous_initialize_for_other_clients(auth_config, user_agent):
    async def ok(_request):
        return JSONResponse({"ok": True})

    app = Starlette(routes=[Route("/mcp", ok, methods=["POST"])])
    app.add_middleware(AuthMiddleware, authenticator=Authenticator(auth_config))

    response = TestClient(app).post(
        "/mcp",
        json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
        headers={"user-agent": user_agent},
    )

    assert response.status_code == 200


def _initialize(client_name, request_id=1):
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "method": "initialize",
        "params": {"protocolVersion": "2025-03-26", "capabilities": {}, "clientInfo": {"name": client_name, "version": "0.1"}},
    }


@pytest.mark.parametrize(
    "payload",
    [_initialize("grok-validator"), [_initialize("grok-validator")], [{"jsonrpc": "2.0", "method": "ping"}, _initialize("grok-validator", 2)]],
)
def test_auth_middleware_challenges_the_grok_validator_initialize(auth_config, payload):
    """The Grok web connector's validator sends no User-Agent; its anonymous 200 leaves the sign-in window blank."""

    async def ok(_request):
        return JSONResponse({"ok": True})

    app = Starlette(routes=[Route("/mcp", ok, methods=["POST"])])
    app.add_middleware(AuthMiddleware, authenticator=Authenticator(auth_config))

    response = TestClient(app).post("/mcp", json=payload, headers={"user-agent": ""})

    assert response.status_code == 401
    assert response.json() == {"error": "missing_bearer_token"}
    assert response.headers["WWW-Authenticate"] == (
        'Bearer resource_metadata="https://app.sugra.ai/.well-known/oauth-protected-resource"'
    )


@pytest.mark.parametrize(
    "payload, user_agent",
    [
        (_initialize("grok-validator"), ""),
        ({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}, "grok-connectors-manager/0.1.0"),
        ({"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "fetch_data", "arguments": {}}},
         "grok-connectors-manager/0.1.0"),
    ],
)
def test_auth_middleware_serves_the_grok_web_connector_once_signed_in(auth_config, payload, user_agent):
    """The sign-in-first rule only withholds the anonymous answer: with a valid bearer the request goes through."""
    seen = {}

    async def ok(request):
        seen["body"] = await request.json()
        return JSONResponse({"ok": True})

    auth = Authenticator(auth_config)
    auth.resolve = AsyncMock(return_value=ResolvedAuth(api_key="sugra_k", user_id=1, method="oauth", platform="xai"))
    app = Starlette(routes=[Route("/mcp", ok, methods=["POST"])])
    app.add_middleware(AuthMiddleware, authenticator=auth)

    response = TestClient(app).post(
        "/mcp",
        json=payload,
        headers={
            "user-agent": user_agent,
            "accept": "application/json, text/event-stream",
            "authorization": "Bearer signed-in-token",
        },
    )

    assert response.status_code == 200
    assert response.json() == {"ok": True}
    assert seen["body"] == payload
    auth.resolve.assert_awaited_once_with("signed-in-token", deadline=ANY)


@pytest.mark.parametrize("client_name", ["smithery-probe", "mcpindex-trust", "rmcp", "Grok-Validator", "grok-validator-2", 7])
def test_auth_middleware_keeps_anonymous_initialize_for_other_client_names(auth_config, client_name):
    async def ok(_request):
        return JSONResponse({"ok": True})

    app = Starlette(routes=[Route("/mcp", ok, methods=["POST"])])
    app.add_middleware(AuthMiddleware, authenticator=Authenticator(auth_config))

    response = TestClient(app).post("/mcp", json=_initialize(client_name), headers={"user-agent": ""})

    assert response.status_code == 200


def test_auth_middleware_rejects_unauthenticated_tool_calls(auth_config, monkeypatch):
    """No tool is callable without a token: every registered name, and an unknown one."""
    monkeypatch.setenv("SUGRA_API_KEY", "dummy")
    import asyncio

    from sugra_api_mcp import tools  # noqa: F401
    from sugra_api_mcp.server import mcp

    names = [tool.name for tool in asyncio.run(mcp.list_tools())]
    assert names

    async def ok(_request):
        return JSONResponse({"ok": True})

    app = Starlette(routes=[Route("/mcp", ok, methods=["POST"])])
    app.add_middleware(AuthMiddleware, authenticator=Authenticator(auth_config))
    client = TestClient(app)

    for name in [*names, "not_a_registered_tool"]:
        response = client.post(
            "/mcp",
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": name, "arguments": {}},
            },
        )

        assert response.status_code == 401, name
        assert response.headers["WWW-Authenticate"] == (
            'Bearer resource_metadata="https://app.sugra.ai/.well-known/oauth-protected-resource"'
        )


def test_auth_middleware_rejects_mixed_unauthenticated_batches(auth_config):
    async def ok(_request):
        return JSONResponse({"ok": True})

    app = Starlette(routes=[Route("/mcp", ok, methods=["POST"])])
    app.add_middleware(AuthMiddleware, authenticator=Authenticator(auth_config))

    response = TestClient(app).post(
        "/mcp",
        json=[
            {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {"name": "list_toolsets", "arguments": {}},
            },
        ],
    )

    assert response.status_code == 401


def test_auth_middleware_rejects_oversized_public_discovery_body(auth_config):
    async def ok(_request):
        return JSONResponse({"ok": True})

    app = Starlette(routes=[Route("/mcp", ok, methods=["POST"])])
    app.add_middleware(AuthMiddleware, authenticator=Authenticator(auth_config))

    response = TestClient(app).post(
        "/mcp",
        content=b" " * (MAX_PUBLIC_MCP_CONTENT_LENGTH + 1),
        headers={"content-type": "application/json"},
    )

    assert response.status_code == 401


def test_auth_middleware_rejects_oversized_public_discovery_batch(auth_config):
    async def ok(_request):
        return JSONResponse({"ok": True})

    app = Starlette(routes=[Route("/mcp", ok, methods=["POST"])])
    app.add_middleware(AuthMiddleware, authenticator=Authenticator(auth_config))

    response = TestClient(app).post(
        "/mcp",
        json=[
            {"jsonrpc": "2.0", "id": item_id, "method": "tools/list", "params": {}}
            for item_id in range(MAX_PUBLIC_MCP_BATCH_ITEMS + 1)
        ],
    )

    assert response.status_code == 401
