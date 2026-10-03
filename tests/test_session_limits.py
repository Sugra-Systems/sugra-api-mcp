"""The Streamable HTTP session limits, and what a client meets at each of them.

The server passes every limit to the SDK itself, so a new SDK release cannot
move one unnoticed. The behaviour tests run the real app with small values.
"""

from __future__ import annotations

import ast
import asyncio
import contextlib
import inspect
import time
from collections.abc import AsyncIterator

import httpx

import sugra_api_mcp.tools  # noqa: F401  (registers the tools on server.mcp)
from sugra_api_mcp import server
from tests.test_request_credentials import HEADERS, INITIALIZE

_LIMITS = {
    "session_idle_timeout": 7200,
    "max_sessions": 1000,
    "max_request_body_size": 4 * 1024 * 1024,
}


def _server_keywords() -> dict[str, str]:
    """The name each keyword of the module-level `mcp = SugraFastMCP(...)` passes."""
    for node in ast.parse(inspect.getsource(server)).body:
        if (
            isinstance(node, ast.Assign)
            and [ast.unparse(target) for target in node.targets] == ["mcp"]
            and isinstance(node.value, ast.Call)
            and ast.unparse(node.value.func) == "SugraFastMCP"
        ):
            return {keyword.arg: ast.unparse(keyword.value) for keyword in node.value.keywords if keyword.arg}
    raise AssertionError("server.py has no module-level mcp = SugraFastMCP(...)")


def test_every_limit_is_passed_by_name() -> None:
    keywords = _server_keywords()
    assert keywords["session_idle_timeout"] == "SESSION_IDLE_TIMEOUT_SECONDS"
    assert keywords["max_sessions"] == "MAX_SESSIONS"
    assert keywords["max_request_body_size"] == "MAX_REQUEST_BODY_BYTES"
    limits = (server.SESSION_IDLE_TIMEOUT_SECONDS, server.MAX_SESSIONS, server.MAX_REQUEST_BODY_BYTES)
    assert limits == tuple(_LIMITS.values())


def test_the_session_manager_runs_with_the_limits(monkeypatch) -> None:
    assert {name: getattr(server.mcp.settings, name) for name in _LIMITS} == _LIMITS
    monkeypatch.setattr(server.mcp, "_session_manager", None)
    server.mcp.streamable_http_app()
    manager = server.mcp.session_manager
    assert {name: getattr(manager, name) for name in _LIMITS} == _LIMITS


@contextlib.asynccontextmanager
async def _served(monkeypatch, **limits: float) -> AsyncIterator[httpx.AsyncClient]:
    for name, value in limits.items():
        monkeypatch.setattr(server.mcp.settings, name, value)
    monkeypatch.setattr(server.mcp, "_session_manager", None)
    app = server.mcp.streamable_http_app()
    async with server.mcp.session_manager.run():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1:8002") as client:
            yield client


async def _open(client: httpx.AsyncClient) -> str:
    opened = await client.post("/mcp", json=INITIALIZE, headers=HEADERS)
    assert opened.status_code == 200
    session_id = opened.headers["mcp-session-id"]
    initialized = await client.post(
        "/mcp",
        json={"jsonrpc": "2.0", "method": "notifications/initialized"},
        headers={**HEADERS, "mcp-session-id": session_id},
    )
    assert initialized.status_code == 202
    return session_id


async def _ping(client: httpx.AsyncClient, session_id: str) -> httpx.Response:
    return await client.post(
        "/mcp",
        json={"jsonrpc": "2.0", "id": 9, "method": "ping"},
        headers={**HEADERS, "mcp-session-id": session_id},
    )


async def test_an_idle_session_is_closed_and_its_id_then_gets_404(monkeypatch) -> None:
    async with _served(monkeypatch, session_idle_timeout=0.2) as client:
        session_id = await _open(client)
        open_sessions = server.mcp.session_manager._server_instances
        deadline = time.monotonic() + 5
        while session_id in open_sessions and time.monotonic() < deadline:
            await asyncio.sleep(0.05)
        assert session_id not in open_sessions
        expired = await _ping(client, session_id)
    assert expired.status_code == 404
    assert expired.json()["error"]["message"] == "Session not found"


async def test_a_session_past_the_limit_gets_503_and_open_ones_keep_working(monkeypatch) -> None:
    async with _served(monkeypatch, max_sessions=2) as client:
        first = await _open(client)
        second = await _open(client)
        refused = await client.post("/mcp", json=INITIALIZE, headers=HEADERS)
        pings = [(await _ping(client, session_id)).status_code for session_id in (first, second)]
        # A session the client ends makes room for a new one.
        ended = await client.delete("/mcp", headers={**HEADERS, "mcp-session-id": first})
        reopened = await client.post("/mcp", json=INITIALIZE, headers=HEADERS)
    assert refused.status_code == 503
    assert refused.json()["error"]["message"] == "Too many open sessions"
    assert "mcp-session-id" not in refused.headers
    assert pings == [200, 200]
    assert ended.status_code == 200
    assert reopened.status_code == 200


async def test_a_body_over_the_limit_gets_413_and_the_session_keeps_working(monkeypatch) -> None:
    async with _served(monkeypatch, max_request_body_size=1024) as client:
        session_id = await _open(client)
        too_large = await client.post(
            "/mcp",
            json={"jsonrpc": "2.0", "id": 3, "method": "ping", "params": {"pad": "x" * 2048}},
            headers={**HEADERS, "mcp-session-id": session_id},
        )
        after = await _ping(client, session_id)
    assert too_large.status_code == 413
    assert after.status_code == 200
