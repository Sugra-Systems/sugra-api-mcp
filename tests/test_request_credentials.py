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
from pathlib import Path
from typing import Any

import httpx
import pytest
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

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


def _authenticator() -> Authenticator:
    return Authenticator(AuthConfig(app_url="http://127.0.0.1:9", jwks_url="http://127.0.0.1:9/jwks", internal_token="x"))


@pytest.fixture
def upstream(monkeypatch) -> list[str]:
    """Record the API key of every upstream call instead of sending it."""
    sent: list[str] = []

    async def fake_request(self: SugraClient, *args: Any, **kwargs: Any) -> dict[str, Any]:
        sent.append(self._config.api_key)
        return {"data": [{"ok": 1}], "meta": {}}

    monkeypatch.setattr(SugraClient, "request", fake_request)
    monkeypatch.setattr(server, "_shared_client", None)
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
    authenticator = _authenticator()
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
    endpoint_call = ("call_endpoint", {"operation_id": _operation_without_params()})
    entity_screen = ("sugra_entity_screen", {"name": "Acme Holdings"})

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

    async def call(
        client: httpx.AsyncClient, session_id: str, bearer: str, tool: tuple[str, dict[str, Any]] = endpoint_call
    ) -> tuple[int, list[str]]:
        upstream.clear()
        name, arguments = tool
        response = await client.post(
            "/mcp",
            json={"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": name, "arguments": arguments}},
            headers={**HEADERS, "mcp-session-id": session_id, "authorization": f"Bearer {bearer}"},
        )
        if response.status_code == 200:
            message = next(
                (json.loads(line[6:]) for line in response.text.splitlines() if line.startswith("data: ")), None
            )
            assert message is not None and "result" in message
        return response.status_code, sorted(set(upstream))

    try:
        async with server.mcp.session_manager.run():
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1:8002") as client:
                monkeypatch.setenv("SUGRA_API_KEY", "sugra_SERVER_FALLBACK")

                anonymous = await open_session(client, None)
                # A made-up raw key reaches the API as itself, which rejects it;
                # the server fallback key never answers for an anonymous session.
                assert await call(client, anonymous, "sugra_made_up") == (200, ["sugra_made_up"])
                assert await call(client, anonymous, "sugra_made_up", entity_screen) == (200, ["sugra_made_up"])
                # A client that opened the session before authorizing uses its own key.
                assert await call(client, anonymous, "jwt-B") == (200, ["sugra_TENANT_B"])
                assert await call(client, anonymous, "jwt-B", entity_screen) == (200, ["sugra_TENANT_B"])

                opened_by_a = await open_session(client, "jwt-A")
                # Another principal on the same session id uses its own key, not A's.
                assert await call(client, opened_by_a, "jwt-B") == (200, ["sugra_TENANT_B"])
                assert await call(client, opened_by_a, "jwt-B", entity_screen) == (200, ["sugra_TENANT_B"])

                revoked.add("jwt-A")
                assert await call(client, opened_by_a, "jwt-A") == (403, [])
                # After revocation a made-up raw key on A's session does not inherit A's key.
                assert await call(client, opened_by_a, "sugra_made_up") == (200, ["sugra_made_up"])
    finally:
        await _close_clients(["sugra_made_up", "sugra_TENANT_A", "sugra_TENANT_B"])
        await authenticator.aclose()


async def test_middleware_marks_every_request_it_serves_as_http() -> None:
    async def probe(request: Request) -> JSONResponse:
        state = request.scope.get("state") or {}
        return JSONResponse({"http": server.http_transport_ctx.get(), "key": state.get(server.REQUEST_API_KEY_STATE)})

    app = Starlette(routes=[Route("/mcp", probe, methods=["POST"])])
    authenticator = _authenticator()
    app.add_middleware(AuthMiddleware, authenticator=authenticator)
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:8002") as client:
            public = await client.post("/mcp", json=INITIALIZE, headers=HEADERS)
            keyed = await client.post(
                "/mcp", json=INITIALIZE, headers={**HEADERS, "authorization": "Bearer sugra_marker_probe"}
            )
    finally:
        await authenticator.aclose()
    assert public.json() == {"http": True, "key": None}
    assert keyed.json() == {"http": True, "key": "sugra_marker_probe"}
    assert server.http_transport_ctx.get() is False


def test_http_dispatch_without_an_attached_request_refuses_inherited_and_env_keys(upstream, monkeypatch) -> None:
    monkeypatch.setenv("SUGRA_API_KEY", "sugra_SERVER_FALLBACK")
    transport_token = server.http_transport_ctx.set(True)
    key_token = server.api_key_ctx.set("sugra_INHERITED_OPENER")
    try:
        client = server.get_client()
    finally:
        server.api_key_ctx.reset(key_token)
        server.http_transport_ctx.reset(transport_token)
    assert isinstance(client, server._KeylessClient)
    assert server._shared_client is None
    assert "sugra_INHERITED_OPENER" not in server._per_key_clients


async def test_in_process_callers_keep_api_key_ctx(upstream) -> None:
    token = server.api_key_ctx.set("sugra_IN_PROCESS")
    try:
        client = server.get_client()
    finally:
        server.api_key_ctx.reset(token)
    try:
        assert isinstance(client, SugraClient)
        assert client._config.api_key == "sugra_IN_PROCESS"
    finally:
        await _close_clients(["sugra_IN_PROCESS"])


async def test_stdio_still_uses_the_env_key(upstream, monkeypatch) -> None:
    monkeypatch.setenv("SUGRA_API_KEY", "sugra_STDIO_KEY")
    try:
        await gateway.call_endpoint(operation_id=_operation_without_params())
        assert upstream == ["sugra_STDIO_KEY"]
    finally:
        await _close_clients([])


def test_every_credential_consumer_goes_through_get_client() -> None:
    package = Path(server.__file__).parent
    readers = sorted(
        path.relative_to(package).as_posix()
        for path in package.rglob("*.py")
        if "api_key_ctx" in path.read_text(encoding="utf-8")
    )
    assert readers == ["auth.py", "server.py"]
    for path in package.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        if path.name == "server.py" or "get_client()" not in text:
            continue
        import_line = next((line for line in text.splitlines() if line.startswith("from ..server import")), "")
        assert "get_client" in import_line, f"{path.name} must call server.get_client"
