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

import asyncio
import inspect
import json
from pathlib import Path
from typing import Any

import httpx
import pytest
from mcp.shared.memory import create_connected_server_and_client_session
from mcp.types import Implementation
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

import sugra_api_mcp.tools  # noqa: F401  (registers the tools on server.mcp)
from sugra_api_mcp import observability, server
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


async def test_middleware_marks_every_request_it_serves_as_http(monkeypatch) -> None:
    async def probe(request: Request) -> JSONResponse:
        state = request.scope.get("state") or {}
        principal = state.get(server.REQUEST_PRINCIPAL_STATE)
        return JSONResponse({
            "http": server.http_transport_ctx.get(),
            "key": state.get(server.REQUEST_API_KEY_STATE),
            "principal": None if principal is None else [principal.method, principal.user_id],
        })

    app = Starlette(routes=[Route("/mcp", probe, methods=["POST"])])
    authenticator = _authenticator()
    real_resolve = authenticator.resolve

    async def resolve(token: str) -> ResolvedAuth:
        token = token.strip()
        if token == "jwt-7":
            return ResolvedAuth(api_key=f"sugra_TENANT_{token[4:]}", user_id=7, access_token_id="jti-7", method="oauth")
        return await real_resolve(token)

    monkeypatch.setattr(authenticator, "resolve", resolve)
    app.add_middleware(AuthMiddleware, authenticator=authenticator)
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:8002") as client:
            public = await client.post("/mcp", json=INITIALIZE, headers=HEADERS)
            keyed = await client.post(
                "/mcp", json=INITIALIZE, headers={**HEADERS, "authorization": "Bearer sugra_marker_probe"}
            )
            oauth = await client.post("/mcp", json=INITIALIZE, headers={**HEADERS, "authorization": "Bearer jwt-7"})
    finally:
        await authenticator.aclose()
    assert public.json() == {"http": True, "key": None, "principal": None}
    assert keyed.json() == {"http": True, "key": "sugra_marker_probe", "principal": ["api_key", None]}
    assert oauth.json() == {"http": True, "key": "sugra_TENANT_7", "principal": ["oauth", 7]}
    assert server.http_transport_ctx.get() is False


class _CaptureSpan:
    def __init__(self, name: str) -> None:
        self.name = name
        self.attributes: dict[str, object] = {}
        self.ended = False

    def set_attribute(self, key: str, value: object) -> None:
        if not self.ended:
            self.attributes[key] = value

    def set_status(self, status: object) -> None:
        pass

    def end(self) -> None:
        self.ended = True


class _CaptureTracer:
    def __init__(self) -> None:
        self.spans: list[_CaptureSpan] = []

    def start_span(self, name: str) -> _CaptureSpan:
        span = _CaptureSpan(name)
        self.spans.append(span)
        return span


def _caller(span: _CaptureSpan) -> dict[str, object]:
    return {key: value for key, value in span.attributes.items() if key.startswith("mcp.caller.")}


async def test_every_tool_span_names_how_its_own_request_arrived(upstream, monkeypatch) -> None:
    """MCP-26.1.3: on one session, each tools/call span carries the auth method,
    host, User-Agent class and Origin of the request that carried that call, and
    the client class the session's most recent initialize asserted. That one is
    client-reported: any request that re-initializes the session can change it."""
    # The hosted Host and Origin protection, so app.sugra.ai and mcp.sugra.ai are
    # admitted exactly as nginx forwards them in production.
    monkeypatch.setenv("SUGRA_MCP_ALLOWED_HOSTS", "app.sugra.ai,mcp.sugra.ai")
    monkeypatch.setattr(server.mcp.settings, "transport_security", server._build_transport_security())
    monkeypatch.setattr(server.mcp, "_session_manager", None)
    app = server.mcp.streamable_http_app()
    authenticator = _authenticator()
    real_resolve = authenticator.resolve

    async def resolve(token: str) -> ResolvedAuth:
        token = token.strip()
        if token.startswith("jwt-"):
            return ResolvedAuth(api_key=f"sugra_TENANT_{token[4:]}", user_id=42, access_token_id=token, method="oauth")
        return await real_resolve(token)

    monkeypatch.setattr(authenticator, "resolve", resolve)
    app.add_middleware(AuthMiddleware, authenticator=authenticator)
    tracer = _CaptureTracer()
    monkeypatch.setattr(observability, "_TRACER", tracer)
    arguments = {"operation_id": _operation_without_params()}
    initialize = {**INITIALIZE, "params": {**INITIALIZE["params"], "clientInfo": {"name": "claude-ai", "version": "1.2.3"}}}

    try:
        async with server.mcp.session_manager.run():
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1:8002") as client:
                opened = await client.post("/mcp", json=initialize, headers={**HEADERS, "host": "app.sugra.ai"})
                assert opened.status_code == 200
                session_id = opened.headers["mcp-session-id"]
                await client.post(
                    "/mcp",
                    json={"jsonrpc": "2.0", "method": "notifications/initialized"},
                    headers={**HEADERS, "mcp-session-id": session_id, "host": "app.sugra.ai"},
                )

                async def call(headers: dict[str, str]) -> None:
                    response = await client.post(
                        "/mcp",
                        json={
                            "jsonrpc": "2.0",
                            "id": 2,
                            "method": "tools/call",
                            "params": {"name": "call_endpoint", "arguments": arguments},
                        },
                        headers={**HEADERS, "mcp-session-id": session_id, **headers},
                    )
                    assert response.status_code == 200

                await call({"authorization": "Bearer jwt-B", "host": "mcp.sugra.ai", "user-agent": "python-httpx/0.27.0"})
                await call({
                    "authorization": "Bearer sugra_made_up",
                    "host": "app.sugra.ai",
                    "user-agent": "curl/8.5.0",
                    "origin": "https://chatgpt.com",
                })
                # Refused at admission: this span comes from record_refused_call.
                monkeypatch.setattr(server, "MAX_IN_FLIGHT_TOOL_CALLS", 0)
                await call({"authorization": "Bearer jwt-B", "host": "mcp.sugra.ai", "user-agent": "python-httpx/0.27.0"})
    finally:
        await _close_clients(["sugra_made_up", "sugra_TENANT_B"])
        await authenticator.aclose()

    calls = [span for span in tracer.spans if span.name == "mcp.tool.call_endpoint"]
    common = {"mcp.caller.transport": "streamable_http", "mcp.caller.client": "claude", "mcp.caller.client_version": "1.2.3"}
    by_oauth = {**common, "mcp.caller.auth": "oauth", "mcp.caller.host": "mcp.sugra.ai",
                "mcp.caller.ua_class": "python", "mcp.caller.origin": "none"}
    by_key = {**common, "mcp.caller.auth": "api_key", "mcp.caller.host": "app.sugra.ai",
              "mcp.caller.ua_class": "curl", "mcp.caller.origin": "openai"}
    assert [_caller(span) for span in calls] == [by_oauth, by_key, by_oauth]
    assert [span.attributes.get("mcp.error.code") for span in calls] == [None, None, "server_busy"]


class _Gate:
    """Inner ASGI layer: a tagged request waits, after AuthMiddleware wrote its
    state, until every tagged request of the round has got that far, so every auth
    write lands before any dispatch reads caller facts."""

    def __init__(self, app: Any, gates: dict[bytes, asyncio.Event]) -> None:
        self.app = app
        self.gates = gates

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if scope["type"] == "http":
            tag = dict(scope["headers"]).get(b"x-gate")
            if tag is not None and tag in self.gates:
                self.gates[tag].set()
                await asyncio.wait_for(asyncio.gather(*(event.wait() for event in self.gates.values())), 5)
        await self.app(scope, receive, send)


async def test_concurrent_calls_each_carry_their_own_request(monkeypatch) -> None:
    """MCP-26.1.3 (review): two sessions called at once, with every auth write landing
    before either dispatch and both upstream calls overlapping, still give each span
    the caller facts of its own request, so no shared last-writer state can stand in."""
    monkeypatch.setenv("SUGRA_MCP_ALLOWED_HOSTS", "app.sugra.ai,mcp.sugra.ai")
    monkeypatch.setattr(server.mcp.settings, "transport_security", server._build_transport_security())
    monkeypatch.setattr(server.mcp, "_session_manager", None)
    monkeypatch.setattr(server, "_shared_client", None)
    monkeypatch.delenv("SUGRA_API_KEY", raising=False)
    arrived: list[str] = []
    both_arrived = asyncio.Event()

    async def fake_request(self: SugraClient, *args: Any, **kwargs: Any) -> dict[str, Any]:
        arrived.append(self._config.api_key)
        if len(arrived) >= 2:
            both_arrived.set()
        await asyncio.wait_for(both_arrived.wait(), 5)
        return {"data": [{"ok": 1}], "meta": {}}

    monkeypatch.setattr(SugraClient, "request", fake_request)
    gates: dict[bytes, asyncio.Event] = {}
    app = server.mcp.streamable_http_app()
    app.add_middleware(_Gate, gates=gates)
    authenticator = _authenticator()
    real_resolve = authenticator.resolve

    async def resolve(token: str) -> ResolvedAuth:
        token = token.strip()
        if token.startswith("jwt-"):
            return ResolvedAuth(api_key=f"sugra_TENANT_{token[4:]}", user_id=42, access_token_id=token, method="oauth")
        return await real_resolve(token)

    monkeypatch.setattr(authenticator, "resolve", resolve)
    app.add_middleware(AuthMiddleware, authenticator=authenticator)
    tracer = _CaptureTracer()
    monkeypatch.setattr(observability, "_TRACER", tracer)
    arguments = {"operation_id": _operation_without_params()}

    async def open_session(client: httpx.AsyncClient, host: str, name: str, version: str) -> str:
        initialize = {**INITIALIZE, "params": {**INITIALIZE["params"], "clientInfo": {"name": name, "version": version}}}
        opened = await client.post("/mcp", json=initialize, headers={**HEADERS, "host": host})
        assert opened.status_code == 200
        session_id = opened.headers["mcp-session-id"]
        await client.post(
            "/mcp",
            json={"jsonrpc": "2.0", "method": "notifications/initialized"},
            headers={**HEADERS, "mcp-session-id": session_id, "host": host},
        )
        return session_id

    async def call(client: httpx.AsyncClient, session_id: str, headers: dict[str, str]) -> None:
        response = await client.post(
            "/mcp",
            json={
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {"name": "call_endpoint", "arguments": arguments},
            },
            headers={**HEADERS, "mcp-session-id": session_id, **headers},
        )
        assert response.status_code == 200

    by_key = {
        "authorization": "Bearer sugra_concurrent_a",
        "host": "mcp.sugra.ai",
        "user-agent": "python-requests/2.32.0",
        "origin": "https://chatgpt.com",
        "x-gate": "a",
    }
    by_oauth = {"authorization": "Bearer jwt-B", "host": "app.sugra.ai", "user-agent": "curl/8.5.0", "x-gate": "b"}
    try:
        async with server.mcp.session_manager.run():
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1:8002") as client:
                session_a = await open_session(client, "app.sugra.ai", "claude-ai", "1.0.0")
                session_b = await open_session(client, "mcp.sugra.ai", "openai-mcp", "2.0.0")
                gates.update({b"a": asyncio.Event(), b"b": asyncio.Event()})
                await asyncio.gather(call(client, session_a, by_key), call(client, session_b, by_oauth))
                gates.clear()
    finally:
        await _close_clients(["sugra_concurrent_a", "sugra_TENANT_B"])
        await authenticator.aclose()

    assert sorted(arrived) == ["sugra_TENANT_B", "sugra_concurrent_a"]
    spans = sorted(
        (_caller(span) for span in tracer.spans if span.name == "mcp.tool.call_endpoint"),
        key=lambda facts: str(facts.get("mcp.caller.client")),
    )
    common = {"mcp.caller.transport": "streamable_http"}
    assert spans == [
        {**common, "mcp.caller.auth": "oauth", "mcp.caller.host": "app.sugra.ai", "mcp.caller.ua_class": "curl",
         "mcp.caller.origin": "none", "mcp.caller.client": "chatgpt", "mcp.caller.client_version": "2.0.0"},
        {**common, "mcp.caller.auth": "api_key", "mcp.caller.host": "mcp.sugra.ai", "mcp.caller.ua_class": "python",
         "mcp.caller.origin": "openai", "mcp.caller.client": "claude", "mcp.caller.client_version": "1.0.0"},
    ]


async def test_an_unauthenticated_tool_call_is_refused_before_any_span(monkeypatch) -> None:
    """On the real middleware stack a tools/call without a credential is refused with
    401 before dispatch (tools/call is not a public method), so no tool span, and no
    caller attribute, can come from an anonymous call."""
    monkeypatch.setenv("SUGRA_MCP_ALLOWED_HOSTS", "app.sugra.ai,mcp.sugra.ai")
    monkeypatch.setattr(server.mcp.settings, "transport_security", server._build_transport_security())
    monkeypatch.setattr(server.mcp, "_session_manager", None)
    app = server.mcp.streamable_http_app()
    authenticator = _authenticator()
    app.add_middleware(AuthMiddleware, authenticator=authenticator)
    tracer = _CaptureTracer()
    monkeypatch.setattr(observability, "_TRACER", tracer)
    try:
        async with server.mcp.session_manager.run():
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1:8002") as client:
                opened = await client.post("/mcp", json=INITIALIZE, headers={**HEADERS, "host": "app.sugra.ai"})
                assert opened.status_code == 200
                session_id = opened.headers["mcp-session-id"]
                refused = await client.post(
                    "/mcp",
                    json={
                        "jsonrpc": "2.0",
                        "id": 2,
                        "method": "tools/call",
                        "params": {"name": "list_toolsets", "arguments": {}},
                    },
                    headers={**HEADERS, "mcp-session-id": session_id, "host": "app.sugra.ai"},
                )
    finally:
        await authenticator.aclose()
    assert refused.status_code == 401
    assert [span.name for span in tracer.spans if span.name.startswith("mcp.tool.")] == []


async def test_an_http_call_with_no_principal_is_auth_none(monkeypatch) -> None:
    """codex r2: where the streamable HTTP app runs without AuthMiddleware (an embedding
    that mounts it bare), a tool call has a request but no principal, so its span says
    auth none, with that request's own host, never api_key."""
    monkeypatch.setattr(server.mcp, "_session_manager", None)
    app = server.mcp.streamable_http_app()
    tracer = _CaptureTracer()
    monkeypatch.setattr(observability, "_TRACER", tracer)
    async with server.mcp.session_manager.run():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1:8002") as client:
            opened = await client.post("/mcp", json=INITIALIZE, headers=HEADERS)
            assert opened.status_code == 200
            session_id = opened.headers["mcp-session-id"]
            await client.post(
                "/mcp",
                json={"jsonrpc": "2.0", "method": "notifications/initialized"},
                headers={**HEADERS, "mcp-session-id": session_id},
            )
            response = await client.post(
                "/mcp",
                json={
                    "jsonrpc": "2.0",
                    "id": 2,
                    "method": "tools/call",
                    "params": {"name": "list_toolsets", "arguments": {}},
                },
                headers={**HEADERS, "mcp-session-id": session_id, "user-agent": "curl/8.5.0"},
            )
            assert response.status_code == 200
    spans = [span for span in tracer.spans if span.name == "mcp.tool.list_toolsets"]
    assert [_caller(span) for span in spans] == [{
        "mcp.caller.transport": "streamable_http",
        "mcp.caller.auth": "none",
        "mcp.caller.host": "loopback",
        "mcp.caller.ua_class": "curl",
        "mcp.caller.origin": "none",
        "mcp.caller.client": "other",
        "mcp.caller.client_version": "0",
    }]


async def test_a_call_no_http_request_carried_is_local_whatever_it_inherited(monkeypatch) -> None:
    """MCP-26.1.3 (codex r1): a stdio or in-process client carries no HTTP request,
    so its span says transport and auth local even when the task inherited the
    HTTP transport marker, and carries no host, User-Agent or origin."""
    tracer = _CaptureTracer()
    monkeypatch.setattr(observability, "_TRACER", tracer)
    previous = server.http_transport_ctx.set(True)
    try:
        async with create_connected_server_and_client_session(
            server.mcp, client_info=Implementation(name="claude-code", version="2.0.1")
        ) as session:
            await session.call_tool("list_toolsets", {})
    finally:
        server.http_transport_ctx.reset(previous)
    spans = [span for span in tracer.spans if span.name == "mcp.tool.list_toolsets"]
    assert [_caller(span) for span in spans] == [{
        "mcp.caller.transport": "local",
        "mcp.caller.auth": "local",
        "mcp.caller.client": "claude",
        "mcp.caller.client_version": "2.0.1",
    }]


def test_caller_attribution_reads_only_the_request_scope() -> None:
    package = Path(server.__file__).parent

    def files_naming(text: str) -> list[str]:
        return sorted(
            path.relative_to(package).as_posix()
            for path in package.rglob("*.py")
            if text in path.read_text(encoding="utf-8")
        )

    assert files_naming("REQUEST_PRINCIPAL_STATE") == ["auth.py", "server.py"]
    assert files_naming("mcp.caller.") == ["observability.py"]
    observability_text = (package / "observability.py").read_text(encoding="utf-8")
    assert "api_key_ctx" not in observability_text and "request_started_at" not in observability_text
    facts_source = inspect.getsource(server.current_caller_facts)
    assert "api_key_ctx" not in facts_source and "request_started_at" not in facts_source
    assert "http_transport_ctx" not in facts_source


def test_http_dispatch_without_an_attached_request_refuses_inherited_and_env_keys(upstream, monkeypatch) -> None:
    monkeypatch.setenv("SUGRA_API_KEY", "sugra_SERVER_FALLBACK")
    previous_transport = server.http_transport_ctx.set(True)
    previous_key = server.api_key_ctx.set("sugra_INHERITED_OPENER")
    try:
        client = server.get_client()
    finally:
        server.api_key_ctx.reset(previous_key)
        server.http_transport_ctx.reset(previous_transport)
    assert isinstance(client, server._KeylessClient)
    assert server._shared_client is None
    assert "sugra_INHERITED_OPENER" not in server._per_key_clients


async def test_in_process_callers_keep_api_key_ctx(upstream) -> None:
    previous = server.api_key_ctx.set("sugra_IN_PROCESS")
    try:
        client = server.get_client()
    finally:
        server.api_key_ctx.reset(previous)
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
