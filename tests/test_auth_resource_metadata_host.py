"""The 401 challenge on mcp.sugra.ai points at that host's own metadata document.

RFC 9728 section 3.3 has a client check the document's resource against the
URL it used, and the APP's mcp.sugra.ai document names https://mcp.sugra.ai/mcp.
Every other host, and every other APP, keeps the APP's root document byte for byte.
"""

from __future__ import annotations

import pytest
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from sugra_api_mcp import config
from sugra_api_mcp.auth import Authenticator, AuthError, AuthMiddleware
from sugra_api_mcp.config import AuthConfig

APP_DOCUMENT = "https://app.sugra.ai/.well-known/oauth-protected-resource"
MCP_DOCUMENT = "https://mcp.sugra.ai/.well-known/oauth-protected-resource/mcp"
CONTAINER_HOST = "ca-sugra-mcp---py.agreeablegrass-542a2c6a.eastus.azurecontainerapps.io"
ENV = config.TRUST_PROXY_HEADERS_ENV


def _authenticator(app_url: str = "https://app.sugra.ai") -> Authenticator:
    return Authenticator(AuthConfig(
        app_url=app_url,
        jwks_url=f"{app_url}/oauth/jwks.json",
        internal_token="internal",
    ))


def _client(authenticator: Authenticator) -> TestClient:
    async def ok(_request):
        return JSONResponse({"ok": True})

    app = Starlette(routes=[Route("/mcp", ok, methods=["GET", "POST"])])
    app.add_middleware(AuthMiddleware, authenticator=authenticator)
    return TestClient(app)


def _challenge_document(response) -> str:
    challenge = response.headers["WWW-Authenticate"]
    prefix = 'Bearer resource_metadata="'
    assert challenge.startswith(prefix)
    return challenge[len(prefix):].split('"', 1)[0]


@pytest.fixture
def trust_off(monkeypatch):
    monkeypatch.delenv(ENV, raising=False)


@pytest.fixture
def trust_on(monkeypatch):
    monkeypatch.setenv(ENV, "1")


@pytest.mark.parametrize(
    ("host", "app_url", "expected"),
    [
        ("mcp.sugra.ai", "https://app.sugra.ai", MCP_DOCUMENT),
        ("app.sugra.ai", "https://app.sugra.ai", APP_DOCUMENT),
        (None, "https://app.sugra.ai", APP_DOCUMENT),
        ("", "https://app.sugra.ai", APP_DOCUMENT),
        ("MCP.SUGRA.AI", "https://app.sugra.ai", APP_DOCUMENT),
        ("mcp.sugra.ai:443", "https://app.sugra.ai", APP_DOCUMENT),
        ("mcp.sugra.ai.", "https://app.sugra.ai", APP_DOCUMENT),
        ("evil.mcp.sugra.ai", "https://app.sugra.ai", APP_DOCUMENT),
        (CONTAINER_HOST, "https://app.sugra.ai", APP_DOCUMENT),
        ("mcp.sugra.ai", "https://staging.example", "https://staging.example/.well-known/oauth-protected-resource"),
    ],
)
def test_the_document_is_picked_from_two_fixed_urls(host, app_url, expected):
    assert _authenticator(app_url).protected_resource_metadata_url_for(host) == expected


def test_the_root_document_property_is_unchanged():
    assert _authenticator().protected_resource_metadata_url == APP_DOCUMENT


def test_vm_shape_mcp_host_gets_its_own_document(trust_off):
    """The VM: nginx passes Host and clears X-Forwarded-Host; trust stays off."""
    response = _client(_authenticator()).get("/mcp", headers={"host": "mcp.sugra.ai"})

    assert response.status_code == 401
    assert response.headers["WWW-Authenticate"] == f'Bearer resource_metadata="{MCP_DOCUMENT}"'


def test_vm_shape_app_host_keeps_the_app_document_byte_for_byte(trust_off):
    response = _client(_authenticator()).get("/mcp", headers={"host": "app.sugra.ai"})

    assert response.status_code == 401
    assert response.headers["WWW-Authenticate"] == f'Bearer resource_metadata="{APP_DOCUMENT}"'


def test_container_shape_mcp_host_gets_its_own_document(trust_on):
    """The container: Host is the app's own name, the edge sends X-Forwarded-Host."""
    response = _client(_authenticator()).get(
        "/mcp", headers={"host": CONTAINER_HOST, "x-forwarded-host": "mcp.sugra.ai"},
    )

    assert response.status_code == 401
    assert _challenge_document(response) == MCP_DOCUMENT


@pytest.mark.parametrize("forwarded", ["app.sugra.ai", None])
def test_container_shape_other_hosts_keep_the_app_document(trust_on, forwarded):
    headers = {"host": CONTAINER_HOST}
    if forwarded is not None:
        headers["x-forwarded-host"] = forwarded
    response = _client(_authenticator()).get("/mcp", headers=headers)

    assert _challenge_document(response) == APP_DOCUMENT


def test_trust_off_a_client_forwarded_host_cannot_pick_the_document(trust_off):
    response = _client(_authenticator()).get(
        "/mcp", headers={"host": "app.sugra.ai", "x-forwarded-host": "mcp.sugra.ai"},
    )

    assert _challenge_document(response) == APP_DOCUMENT


@pytest.mark.parametrize("forwarded", ["evil.example", "MCP.SUGRA.AI", "mcp.sugra.ai:443", "mcp.sugra.ai, evil"])
def test_trust_on_any_other_forwarded_host_keeps_the_app_document(trust_on, forwarded):
    response = _client(_authenticator()).get(
        "/mcp", headers={"host": CONTAINER_HOST, "x-forwarded-host": forwarded},
    )

    assert _challenge_document(response) == APP_DOCUMENT


def test_another_app_never_points_at_mcp_sugra_ai(trust_off):
    response = _client(_authenticator("https://staging.example")).get("/mcp", headers={"host": "mcp.sugra.ai"})

    assert _challenge_document(response) == "https://staging.example/.well-known/oauth-protected-resource"


@pytest.mark.parametrize(
    ("authorization", "error", "expected_suffix"),
    [
        (None, None, ""),
        ("Basic abc", None, ""),
        ("Bearer dead", AuthError("token_revoked"), ', error="invalid_token"'),
        ("Bearer scope", AuthError("missing scope", reauth=False), ""),
    ],
)
@pytest.mark.parametrize(("host", "document"), [("mcp.sugra.ai", MCP_DOCUMENT), ("app.sugra.ai", APP_DOCUMENT)])
def test_every_401_challenge_follows_the_host(trust_off, monkeypatch, authorization, error, expected_suffix,
                                              host, document):
    authenticator = _authenticator()

    async def refuse(_token):
        raise error

    monkeypatch.setattr(authenticator, "resolve", refuse)
    headers = {"host": host}
    if authorization is not None:
        headers["authorization"] = authorization
    response = _client(authenticator).get("/mcp", headers=headers)

    assert response.status_code == 401
    assert response.headers["WWW-Authenticate"] == f'Bearer resource_metadata="{document}"{expected_suffix}'


@pytest.mark.parametrize(("host", "document"), [("mcp.sugra.ai", MCP_DOCUMENT), ("app.sugra.ai", APP_DOCUMENT)])
def test_the_auth_timeout_challenge_follows_the_host(trust_off, monkeypatch, host, document):
    authenticator = _authenticator()

    async def slow(_token):
        raise TimeoutError

    monkeypatch.setattr(authenticator, "resolve", slow)
    response = _client(authenticator).get("/mcp", headers={"host": host, "authorization": "Bearer slow"})

    assert response.status_code == 503
    assert response.headers["WWW-Authenticate"] == f'Bearer resource_metadata="{document}"'
