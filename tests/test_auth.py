"""Unit tests for sugra_api_mcp.auth."""

from __future__ import annotations

import time
from unittest.mock import MagicMock, patch

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
    Authenticator,
    AuthError,
    AuthMiddleware,
    _CachedKey,
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
    key = await auth.resolve("sugra_ao1_abc123xyz")
    assert key == "sugra_ao1_abc123xyz"


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
    auth._api_key_cache[42] = _CachedKey(
        api_key="sugra_cached_key",
        expires_at=time.time() + 60,
    )

    resolved = await auth.resolve(token)
    assert resolved == "sugra_cached_key"


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
    auth._api_key_cache[42] = _CachedKey(
        api_key="sugra_cached_key",
        expires_at=time.time() + 60,
    )

    resolved = await auth.resolve(token)
    assert resolved == "sugra_cached_key"


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

    with pytest.raises(AuthError, match="sugra:read"):
        await auth.resolve(token)


async def test_lookup_404_raises_with_user_message(auth_config, rsa_keypair):
    private_key, public_pem = rsa_keypair
    token = _make_jwt(
        private_key,
        {
            "iss": "https://app.sugra.ai",
            "aud": "https://app.sugra.ai/mcp",
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

    mock_response = MagicMock(spec=httpx.Response)
    mock_response.status_code = 404
    mock_response.headers = {"content-type": "application/json"}
    mock_response.json = MagicMock(return_value={"error": "no_api_key", "message": "User has no API key"})

    async def fake_get(self, url, headers=None):
        return mock_response

    with patch("httpx.AsyncClient.get", new=fake_get):
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

    with patch("httpx.AsyncClient.get", new=fake_get):
        k1 = await auth.resolve(token)
        k2 = await auth.resolve(token)
        assert k1 == "sugra_test_key"
        assert k2 == "sugra_test_key"
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
