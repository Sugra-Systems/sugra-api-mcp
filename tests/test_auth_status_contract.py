"""A dead token answers 401 invalid_token; an APP fault answers 502, never a refusal."""

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

from sugra_api_mcp import auth as auth_module
from sugra_api_mcp.auth import Authenticator, AuthError, AuthMiddleware, _CachedKey
from sugra_api_mcp.config import AuthConfig

METADATA = "https://app.sugra.ai/.well-known/oauth-protected-resource"
BARE_CHALLENGE = f'Bearer resource_metadata="{METADATA}"'
INVALID_TOKEN_CHALLENGE = f'Bearer resource_metadata="{METADATA}", error="invalid_token"'

REAUTH_CODES = [
    ("token_not_found", 404),
    ("token_user_mismatch", 403),
    ("token_revoked", 403),
    ("token_expired", 403),
    ("connection_not_found", 404),
    ("connection_disconnected", 403),
]


@pytest.fixture(scope="module")
def keypair() -> tuple[rsa.RSAPrivateKey, str]:
    signer = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    public_pem = signer.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    ).decode()
    return signer, public_pem


def _token(
    signer: rsa.RSAPrivateKey,
    jti: str = "contract-jti",
    *,
    scopes: tuple[str, ...] = ("sugra:read",),
    lifetime: int = 3600,
) -> str:
    now = int(time.time())
    private_pem = signer.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    return jwt.encode(
        {
            "iss": "https://app.sugra.ai",
            "aud": "https://app.sugra.ai/mcp",
            "jti": jti,
            "sub": "42",
            "exp": now + lifetime,
            "iat": now - 7200,
            "scopes": list(scopes),
        },
        private_pem,
        algorithm="RS256",
    )


def _authenticator(public_pem: str, *, cached_key: bool) -> Authenticator:
    auth = Authenticator(AuthConfig(
        app_url="https://app.sugra.ai",
        jwks_url="https://app.sugra.ai/oauth/jwks.json",
        internal_token="internal",
    ))
    signing_key = MagicMock()
    signing_key.key = public_pem
    auth._jwks.get_signing_keys = MagicMock(return_value=[signing_key])
    if cached_key:
        auth._api_key_cache[42] = _CachedKey("sugra_cached_key", time.time() + 60)
    return auth


def _activity(response: httpx.Response):
    async def fake_post(self, url, headers=None, json=None):
        return response
    return patch("httpx.AsyncClient.post", new=fake_post)


def _lookup(response: httpx.Response):
    async def fake_get(self, url, headers=None):
        return response
    return patch("httpx.AsyncClient.get", new=fake_get)


@pytest.mark.parametrize(("code", "app_status"), REAUTH_CODES)
async def test_dead_token_verdicts_answer_401(keypair, code, app_status):
    signer, public_pem = keypair
    auth = _authenticator(public_pem, cached_key=True)

    with _activity(httpx.Response(app_status, json={"error": code})), \
            pytest.raises(AuthError) as exc:
        await auth.resolve(_token(signer))

    assert exc.value.status == 401
    assert code in str(exc.value)


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(401, json={"error": "unauthorized"}),
        httpx.Response(422, json={"error": "validation_failed", "details": {}}),
        httpx.Response(404, json={"message": "Not Found"}),
        httpx.Response(404, text="<html>Not Found</html>"),
        httpx.Response(429, json={"message": "Too Many Attempts."}),
        httpx.Response(403, json={"error": "some_future_code"}),
        httpx.Response(403, json={"error": ["token_revoked"]}),
        httpx.Response(403, json=["token_revoked"]),
    ],
    ids=["internal_token_rejected", "validation_failed", "bare_404", "html_404",
         "throttled", "unknown_code", "list_code", "list_body"],
)
async def test_other_activity_refusals_answer_502(keypair, response):
    signer, public_pem = keypair
    auth = _authenticator(public_pem, cached_key=True)

    with _activity(response), pytest.raises(AuthError) as exc:
        await auth.resolve(_token(signer))

    assert exc.value.status == 502
    assert f"HTTP {response.status_code}" in str(exc.value)


async def test_deleted_user_answers_401(keypair):
    signer, public_pem = keypair
    auth = _authenticator(public_pem, cached_key=False)

    with _activity(httpx.Response(204)), \
            _lookup(httpx.Response(404, json={"error": "user_not_found"})), \
            pytest.raises(AuthError) as exc:
        await auth.resolve(_token(signer))

    assert exc.value.status == 401
    assert "no API key" not in str(exc.value)


async def test_user_without_key_still_answers_403(keypair):
    signer, public_pem = keypair
    auth = _authenticator(public_pem, cached_key=False)
    body = {"error": "no_api_key", "message": "User has no API key"}

    with _activity(httpx.Response(204)), _lookup(httpx.Response(404, json=body)), \
            pytest.raises(AuthError) as exc:
        await auth.resolve(_token(signer))

    assert exc.value.status == 403
    assert str(exc.value) == "User has no API key"


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(404, json={"message": "Not Found"}),
        httpx.Response(404, json={"error": "some_future_code"}),
        httpx.Response(404, text="<html>Not Found</html>"),
        httpx.Response(404, text="{not json", headers={"content-type": "application/json"}),
    ],
    ids=["no_code", "unknown_code", "html", "broken_json"],
)
async def test_other_key_lookup_404s_answer_502(keypair, response):
    signer, public_pem = keypair
    auth = _authenticator(public_pem, cached_key=False)

    with _activity(httpx.Response(204)), _lookup(response), \
            pytest.raises(AuthError) as exc:
        await auth.resolve(_token(signer))

    assert exc.value.status == 502
    assert "HTTP 404" in str(exc.value)


def _client(auth: Authenticator) -> TestClient:
    async def ok(_request):
        return JSONResponse({"ok": True})

    app = Starlette(routes=[Route("/mcp", ok, methods=["GET", "POST"])])
    app.add_middleware(AuthMiddleware, authenticator=auth)
    return TestClient(app)


def test_revoked_token_gets_the_invalid_token_challenge(keypair):
    signer, public_pem = keypair
    auth = _authenticator(public_pem, cached_key=True)

    with _activity(httpx.Response(403, json={"error": "token_revoked"})):
        response = _client(auth).get(
            "/mcp", headers={"authorization": f"Bearer {_token(signer)}"})

    assert response.status_code == 401
    assert response.headers["WWW-Authenticate"] == INVALID_TOKEN_CHALLENGE
    assert response.json()["error"] == "auth_failed"


def test_expired_token_gets_the_invalid_token_challenge(keypair):
    signer, public_pem = keypair
    auth = _authenticator(public_pem, cached_key=True)

    response = _client(auth).get(
        "/mcp", headers={"authorization": f"Bearer {_token(signer, lifetime=-60)}"})

    assert response.status_code == 401
    assert response.json()["message"] == "Token expired"
    assert response.headers["WWW-Authenticate"] == INVALID_TOKEN_CHALLENGE


def test_token_without_the_scope_keeps_the_bare_challenge(keypair):
    signer, public_pem = keypair
    auth = _authenticator(public_pem, cached_key=True)
    bearer = _token(signer, scopes=("other:read",))

    response = _client(auth).get("/mcp", headers={"authorization": f"Bearer {bearer}"})

    assert response.status_code == 401
    assert "required scope" in response.json()["message"]
    assert response.headers["WWW-Authenticate"] == BARE_CHALLENGE


def test_missing_bearer_keeps_the_bare_challenge(keypair):
    _, public_pem = keypair
    response = _client(_authenticator(public_pem, cached_key=True)).get("/mcp")

    assert response.status_code == 401
    assert response.headers["WWW-Authenticate"] == BARE_CHALLENGE


@pytest.mark.parametrize("status", [403, 500, 502, 503])
def test_non_401_auth_errors_carry_no_challenge(keypair, status):
    _, public_pem = keypair
    auth = _authenticator(public_pem, cached_key=True)

    async def refuse(_token):
        raise AuthError("refused", status=status)

    auth.resolve = refuse
    response = _client(auth).get("/mcp", headers={"authorization": "Bearer x"})

    assert response.status_code == status
    assert "WWW-Authenticate" not in response.headers


def test_auth_timeout_keeps_the_bare_challenge(keypair, monkeypatch):
    _, public_pem = keypair
    auth = _authenticator(public_pem, cached_key=True)
    monkeypatch.setattr(auth_module, "AUTH_BUDGET_SECONDS", 0.01)

    async def hang(_token):
        import asyncio
        await asyncio.sleep(1)

    auth.resolve = hang
    response = _client(auth).get("/mcp", headers={"authorization": "Bearer x"})

    assert response.status_code == 503
    assert response.headers["WWW-Authenticate"] == BARE_CHALLENGE
