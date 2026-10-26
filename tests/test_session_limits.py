"""The stateless Streamable HTTP transport, and what a client meets on it.

The server passes the transport mode and the body limit to the SDK itself, so
a new SDK release cannot move either unnoticed. The behaviour tests run the
real app: a request is served without a session id and with a stale one, no
session id is sent back, and GET and DELETE on the endpoint get 405.
"""

from __future__ import annotations

import ast
import contextlib
import inspect
import json
from collections.abc import AsyncIterator

import httpx

import sugra_api_mcp.tools  # noqa: F401  (registers the tools on server.mcp)
from sugra_api_mcp import server
from sugra_api_mcp.web import PostOnlyMiddleware
from tests.test_request_credentials import HEADERS, INITIALIZE

_SETTINGS = {"stateless_http": True, "max_request_body_size": 4 * 1024 * 1024}
_PING = {"jsonrpc": "2.0", "id": 9, "method": "ping"}
_LIST = {"jsonrpc": "2.0", "id": 2, "method": "tools/list"}


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


def test_the_mode_and_the_body_limit_are_passed_by_name() -> None:
    keywords = _server_keywords()
    assert keywords["stateless_http"] == "STATELESS_HTTP"
    assert keywords["max_request_body_size"] == "MAX_REQUEST_BODY_BYTES"
    assert "session_idle_timeout" not in keywords
    assert "max_sessions" not in keywords
    assert tuple(_SETTINGS.values()) == (server.STATELESS_HTTP, server.MAX_REQUEST_BODY_BYTES)


def test_the_session_manager_runs_stateless(monkeypatch) -> None:
    assert {name: getattr(server.mcp.settings, name) for name in _SETTINGS} == _SETTINGS
    monkeypatch.setattr(server.mcp, "_session_manager", None)
    server.mcp.streamable_http_app()
    manager = server.mcp.session_manager
    assert manager.stateless is True
    assert manager.max_request_body_size == _SETTINGS["max_request_body_size"]


def _message(response: httpx.Response) -> dict:
    """The one JSON-RPC message of a POST answer, sent as a single SSE event."""
    (data,) = [line[len("data: ") :] for line in response.text.splitlines() if line.startswith("data: ")]
    return json.loads(data)


@contextlib.asynccontextmanager
async def _served(monkeypatch, **settings: object) -> AsyncIterator[httpx.AsyncClient]:
    for name, value in settings.items():
        monkeypatch.setattr(server.mcp.settings, name, value)
    monkeypatch.setattr(server.mcp, "_session_manager", None)
    app = PostOnlyMiddleware(server.mcp.streamable_http_app(), path=server.mcp.settings.streamable_http_path)
    async with server.mcp.session_manager.run():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1:8002") as client:
            yield client


async def test_initialize_is_answered_and_no_session_id_is_sent(monkeypatch) -> None:
    async with _served(monkeypatch) as client:
        opened = await client.post("/mcp", json=INITIALIZE, headers=HEADERS)
    assert opened.status_code == 200
    assert "mcp-session-id" not in opened.headers
    assert _message(opened)["result"]["serverInfo"]["name"] == "sugra-api"


async def test_a_request_without_a_session_id_is_served(monkeypatch) -> None:
    # What ChatGPT sends: a call with no initialize before it and no Mcp-Session-Id.
    async with _served(monkeypatch) as client:
        listed = await client.post("/mcp", json=_LIST, headers=HEADERS)
        pinged = await client.post("/mcp", json=_PING, headers=HEADERS)
    assert listed.status_code == 200
    assert _message(listed)["result"]["tools"]
    assert pinged.status_code == 200
    assert "mcp-session-id" not in listed.headers


async def test_a_stale_session_id_is_ignored(monkeypatch) -> None:
    # A client still holding an id from before the switch, or from a restarted server.
    stale = {**HEADERS, "mcp-session-id": "0" * 32}
    async with _served(monkeypatch) as client:
        listed = await client.post("/mcp", json=_LIST, headers=stale)
        initialized = await client.post(
            "/mcp", json={"jsonrpc": "2.0", "method": "notifications/initialized"}, headers=stale
        )
    assert listed.status_code == 200
    assert _message(listed)["result"]["tools"]
    assert initialized.status_code == 202


async def test_get_and_delete_on_the_endpoint_get_405_with_allow_post(monkeypatch) -> None:
    async with _served(monkeypatch) as client:
        answers = [
            await client.get("/mcp", headers={**HEADERS, "accept": "text/event-stream"}),
            await client.delete("/mcp", headers={**HEADERS, "mcp-session-id": "0" * 32}),
        ]
    for answer in answers:
        assert answer.status_code == 405
        assert answer.headers["allow"] == "POST"
        assert answer.headers["content-type"] == "application/json"
        assert answer.json()["error"]["message"] == "Method Not Allowed"
        assert "mcp-session-id" not in answer.headers


async def test_a_body_over_the_limit_gets_413_and_the_next_request_is_served(monkeypatch) -> None:
    async with _served(monkeypatch, max_request_body_size=1024) as client:
        too_large = await client.post(
            "/mcp",
            json={"jsonrpc": "2.0", "id": 3, "method": "ping", "params": {"pad": "x" * 2048}},
            headers=HEADERS,
        )
        after = await client.post("/mcp", json=_PING, headers=HEADERS)
    assert too_large.status_code == 413
    assert after.status_code == 200
