"""Tool calls on the HTTP transport use the credential of the request that carries them.

The Streamable HTTP session task keeps the ContextVars of the request that
opened the session. Before this rule, a session opened without a token ran every
tool on the SUGRA_API_KEY fallback, and a session opened by one principal kept
that principal's key for any later caller, including after the principal's
OAuth token was revoked. These tests drive the real app (AuthMiddleware in
front of the SDK session manager) in process and record which API key each tool
call would send upstream.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

import sugra_api_mcp.tools  # noqa: F401  (registers the tools on server.mcp)
from sugra_api_mcp import server
from sugra_api_mcp.auth import Authenticator, AuthError, AuthMiddleware, ResolvedAuth
from sugra_api_mcp.catalog.loader import load_catalog
from sugra_api_mcp.client import SugraClient
from sugra_api_mcp.config import AuthConfig
from sugra_api_mcp.tools import gateway

HEADERS = {"accept": "application/json, text/event-stream", "content-type": "application/json"}
INITIALIZE = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "tests", "version": "0"}},
}


def _operation_without_params() -> str:
    return next(
        endpoint.operation_id
        for endpoint in load_catalog().endpoints
        if endpoint.method == "GET"
        and not endpoint.required_groups
        and not any(p.required or p.location == "path" for p in endpoint.parameters)
    )


@pytest.fixture
def upstream(monkeypatch) -> list[str]:
    """Record the API key of every upstream call instead of sending it."""
    sent: list[str] = []

    async def fake_get(self: SugraClient, *args: Any, **kwargs: Any) -> dict[str, Any]:
        sent.append(self._config.api_key)
        return {"data": [{"ok": 1}], "meta": {}}

    async def fake_request(self: SugraClient, *args: Any, **kwargs: Any) -> dict[str, Any]:
        sent.append(self._config.api_key)
        return {"data": [{"ok": 1}], "meta": {}}

    monkeypatch.setattr(SugraClient, "get", fake_get)
    monkeypatch.setattr(SugraClient, "request", fake_request)
    monkeypatch.setattr(server, "_shared_client", None)
    monkeypatch.setattr(server, "_http_transport", False)
    monkeypatch.delenv("SUGRA_API_KEY", raising=False)
    return sent


async def _close_clients(keys: list[str]) -> None:
    for key in keys:
        client = server._per_key_clients.pop(key, None)
        if client is not None:
            await client.aclose()
    if isinstance(server._shared_client, SugraClient):
        await server._shared_client.aclose()


async def test_http_tool_calls_use_the_credential_of_the_request_that_carries_them(upstream, monkeypatch) -> None:
    monkeypatch.setattr(server.mcp, "_session_manager", None)
    app = server.mcp.streamable_http_app()
    authenticator = Authenticator(
        AuthConfig(app_url="http://127.0.0.1:9", jwks_url="http://127.0.0.1:9/jwks", internal_token="x")
    )
    revoked: set[str] = set()
    real_resolve = authenticator.resolve

    async def resolve(token: str) -> ResolvedAuth:
        token = token.strip()
        if token.startswith("jwt-"):
            if token in revoked:
                raise AuthError("token_revoked", status=403)
            return ResolvedAuth(api_key=f"sugra_TENANT_{token[4:]}", user_id=1, access_token_id=token)
        return await real_resolve(token)

    monkeypatch.setattr(authenticator, "resolve", resolve)
    app.add_middleware(AuthMiddleware, authenticator=authenticator)
    operation_id = _operation_without_params()

    async def open_session(client: httpx.AsyncClient, bearer: str | None) -> str:
        headers = dict(HEADERS)
        if bearer:
            headers["authorization"] = f"Bearer {bearer}"
        response = await client.post("/mcp", json=INITIALIZE, headers=headers)
        assert response.status_code == 200
        session_id = response.headers["mcp-session-id"]
        await client.post(
            "/mcp",
            json={"jsonrpc": "2.0", "method": "notifications/initialized"},
            headers={**headers, "mcp-session-id": session_id},
        )
        return session_id

    async def call(client: httpx.AsyncClient, session_id: str, bearer: str) -> tuple[int, list[str]]:
        upstream.clear()
        response = await client.post(
            "/mcp",
            json={
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {"name": "call_endpoint", "arguments": {"operation_id": operation_id}},
            },
            headers={**HEADERS, "mcp-session-id": session_id, "authorization": f"Bearer {bearer}"},
        )
        if response.status_code == 200:
            body = response.text
            message = next((json.loads(line[6:]) for line in body.splitlines() if line.startswith("data: ")), None)
            assert message is not None and "result" in message
        return response.status_code, list(upstream)

    try:
        async with server.mcp.session_manager.run():
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1:8002") as client:
                monkeypatch.setenv("SUGRA_API_KEY", "sugra_SERVER_FALLBACK")

                anonymous = await open_session(client, None)
                # A made-up raw key reaches the API as itself, which rejects it;
                # the server fallback key never answers for an anonymous session.
                assert await call(client, anonymous, "sugra_made_up") == (200, ["sugra_made_up"])
                # A client that opened the session before authorizing uses its own key.
                assert await call(client, anonymous, "jwt-B") == (200, ["sugra_TENANT_B"])

                opened_by_a = await open_session(client, "jwt-A")
                # Another principal on the same session id uses its own key, not A's.
                assert await call(client, opened_by_a, "jwt-B") == (200, ["sugra_TENANT_B"])

                revoked.add("jwt-A")
                assert await call(client, opened_by_a, "jwt-A") == (403, [])
                # After revocation a made-up raw key on A's session does not inherit A's key.
                assert await call(client, opened_by_a, "sugra_made_up") == (200, ["sugra_made_up"])
    finally:
        await _close_clients(["sugra_made_up", "sugra_TENANT_A", "sugra_TENANT_B"])
        await authenticator.aclose()


def test_http_transport_never_falls_back_to_the_env_key(upstream, monkeypatch) -> None:
    monkeypatch.setenv("SUGRA_API_KEY", "sugra_SERVER_FALLBACK")
    server.enable_http_transport()
    assert isinstance(server.get_client(), server._KeylessClient)
    assert server._shared_client is None


async def test_stdio_still_uses_the_env_key(upstream, monkeypatch) -> None:
    monkeypatch.setenv("SUGRA_API_KEY", "sugra_STDIO_KEY")
    try:
        await gateway.call_endpoint(operation_id=_operation_without_params())
        assert upstream == ["sugra_STDIO_KEY"]
    finally:
        await _close_clients([])
