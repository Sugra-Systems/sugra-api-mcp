"""A key no HTTP header can carry gets a structured error, never a failed tool.

httpx encodes a header value as ASCII, so a Sugra API key holding any other
character (an accented or Cyrillic letter, or an invisible character a paste
left behind) raised UnicodeEncodeError while the client was built, before any
request, and the tool answered with the opaque tool_execution_failed. A key
holding a line break or a NUL, or a space at either end, got a client, and h11
refused the header when the request was sent, which the tool answered as a
transport failure to retry.
A client that reads either as a passing fault calls the tool again and fails
the same way. server.get_client now hands any key that is no valid header
value (RFC 9110 section 5.5) the unusable-key stand-in: nothing is sent, and
the tool answers with unusable_api_key and a hint that says not to retry.
These tests drive each path a key arrives by and record what reaches the API,
read off the requests themselves.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

import h11
import httpx
import pytest
from mcp.shared.memory import create_connected_server_and_client_session
from mcp.types import Implementation

import sugra_api_mcp.tools  # noqa: F401  (registers the tools on server.mcp)
from sugra_api_mcp import observability, server
from sugra_api_mcp.auth import AuthMiddleware, ResolvedAuth
from sugra_api_mcp.client import SugraClient
from sugra_api_mcp.config import UNUSABLE_API_KEY_HINT, Config
from sugra_api_mcp.tools import entities, gateway
from tests.test_keyless import _fixture_catalog
from tests.test_request_credentials import (
    HEADERS,
    INITIALIZE,
    _answer_api_requests,
    _authenticator,
    _CaptureTracer,
    _operation_without_params,
)

UNUSABLE = {"error": "unusable_api_key", "hint": UNUSABLE_API_KEY_HINT}

# A Cyrillic letter, an accented Latin one, and a zero-width space, which is
# not whitespace, so stripping the key leaves it in place.
NON_ASCII_KEYS = ["sugra_test_Ив", "sugra_test_José", "sugra_test_key​"]

# A line break and a NUL, which h11 refuses when the request is sent, and DEL,
# which h11 sends although RFC 9110 leaves it out of a field value, as it does
# every control character but the tab.
CONTROL_KEYS = ["sugra_test_a\r\nb", "sugra_test_a\x00b", "sugra_test_a\x7fb"]

UNUSABLE_KEYS = NON_ASCII_KEYS + CONTROL_KEYS

# A space or a tab at either end, which h11 refuses when the request is sent.
# The environment key is stripped, so only a key set for the call arrives so.
EDGE_KEYS = ["sugra_test_a ", "\tsugra_test_a"]

# An environment variable cannot hold a NUL.
ENV_KEYS = [key for key in UNUSABLE_KEYS if "\x00" not in key]

CHATGPT = Implementation(name="openai-mcp", version="1.0.0")


@pytest.fixture
async def sent(monkeypatch) -> AsyncIterator[list[list[str]]]:
    """The x-api-key values of every Sugra API request, answered without a network.

    No key in env and no pool open when the test starts, and none left open after it.
    """
    keys: list[list[str]] = []

    async def answer(request: httpx.Request) -> httpx.Response:
        keys.append(request.headers.get_list("x-api-key"))
        return httpx.Response(200, json={"data": [{"symbol": "AAPL", "price": 200}], "meta": {}})

    _answer_api_requests(monkeypatch, answer)
    monkeypatch.delenv("SUGRA_API_KEY", raising=False)
    await server.close_clients()
    yield keys
    await server.close_clients()


def _payload(result: dict[str, Any]) -> Any:
    if result.get("structuredContent") is not None:
        return result["structuredContent"]
    return json.loads(result["content"][0]["text"])


# ---------------------------------------------------------------------------
# The client a call gets.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("key", NON_ASCII_KEYS)
def test_the_real_client_cannot_be_built_with_such_a_key(key: str) -> None:
    """Why the stand-in exists: httpx encodes a header value as ASCII."""
    with pytest.raises(UnicodeEncodeError):
        SugraClient(Config(api_base="https://api.test", api_key=key, timeout=5.0))


@pytest.mark.parametrize("key", [*CONTROL_KEYS[:2], *EDGE_KEYS])
def test_the_transport_refuses_such_an_ascii_key_only_when_sending(key: str) -> None:
    """Why ASCII is not enough: httpx takes such a header, and h11 refuses it on the way out."""
    headers = httpx.Headers({"x-api-key": key})
    with pytest.raises(h11.LocalProtocolError):
        h11.Request(method="GET", target="/", headers=[(b"host", b"api.test"), *headers.raw])


@pytest.mark.parametrize("key", ENV_KEYS)
async def test_an_env_key_no_header_can_carry_gets_the_stand_in(sent, monkeypatch, key: str) -> None:
    monkeypatch.setenv("SUGRA_API_KEY", key)

    client = server.get_client()

    assert isinstance(client, server._UnusableKeyClient)
    assert await client.get("/api/v1/ping") == UNUSABLE
    assert await client.post("/api/v1/ping", json={}) == UNUSABLE
    assert await client.request("GET", "/api/v1/ping") == UNUSABLE
    assert sent == []


@pytest.mark.parametrize("key", [*UNUSABLE_KEYS, *EDGE_KEYS])
async def test_a_context_key_no_header_can_carry_gets_the_stand_in(sent, key: str) -> None:
    previous_key = server.api_key_ctx.set(key)
    try:
        client = server.get_client()
    finally:
        server.api_key_ctx.reset(previous_key)

    assert isinstance(client, server._UnusableKeyClient)
    assert await client.get("/api/v1/ping") == UNUSABLE
    assert sent == []


async def test_an_ascii_key_still_gets_a_real_client(sent, monkeypatch) -> None:
    monkeypatch.setenv("SUGRA_API_KEY", "sugra_test_ascii")

    client = server.get_client()

    assert isinstance(client, SugraClient)
    await client.get("/api/v1/ping")
    assert sent == [["sugra_test_ascii"]]


@pytest.mark.parametrize("key", ["sugra_test_a b", "sugra_test_a\tb"])
async def test_a_space_or_a_tab_between_characters_is_sent_as_it_is(sent, monkeypatch, key: str) -> None:
    """A field value may hold them (RFC 9110 section 5.5), so the API judges such a key."""
    monkeypatch.setenv("SUGRA_API_KEY", key)

    client = server.get_client()

    assert isinstance(client, SugraClient)
    await client.get("/api/v1/ping")
    assert sent == [[key]]


async def test_the_stand_in_is_not_kept_once_the_key_can_be_sent(sent, monkeypatch) -> None:
    """A rotated key is sent on the very next call."""
    monkeypatch.setenv("SUGRA_API_KEY", UNUSABLE_KEYS[0])
    assert isinstance(server.get_client(), server._UnusableKeyClient)

    monkeypatch.setenv("SUGRA_API_KEY", "sugra_test_rotated")
    client = server.get_client()

    assert isinstance(client, SugraClient)
    await client.get("/api/v1/ping")
    assert sent == [["sugra_test_rotated"]]


# ---------------------------------------------------------------------------
# What the tools answer.
# ---------------------------------------------------------------------------


async def test_call_endpoint_and_fetch_data_return_the_structured_error(sent, monkeypatch) -> None:
    monkeypatch.setenv("SUGRA_API_KEY", UNUSABLE_KEYS[0])
    monkeypatch.setattr(gateway, "load_catalog", _fixture_catalog)

    called = await gateway.call_endpoint("quotes_symbol_price", params={"symbol": "AAPL"})
    fetched = await gateway.fetch_data("quote price for a symbol", params={"symbol": "AAPL"})

    assert called == UNUSABLE
    assert fetched == UNUSABLE
    assert sent == []


async def test_entity_tools_return_the_structured_error(sent, monkeypatch) -> None:
    monkeypatch.setenv("SUGRA_API_KEY", UNUSABLE_KEYS[1])

    screened = await entities.sugra_entity_screen("ACME HOLDINGS")
    looked_up = await entities.sugra_entity_lookup("lei", "529900T8BM49AURSDO55")

    for result in (screened, looked_up):
        assert result["error"] == "unusable_api_key"
        assert result["hint"] == UNUSABLE_API_KEY_HINT
    assert sent == []


@pytest.mark.parametrize("key", [NON_ASCII_KEYS[2], CONTROL_KEYS[0], CONTROL_KEYS[2]])
@pytest.mark.parametrize(
    ("tool", "arguments"),
    [("get_stock_quotes", {"symbols": "AAPL"}), ("get_price_history", {"symbol": "AAPL"})],
)
async def test_typed_tools_return_the_structured_error_and_send_nothing(
    sent, monkeypatch, tool: str, arguments: dict[str, Any], key: str
) -> None:
    monkeypatch.setenv("SUGRA_API_KEY", key)
    tracer = _CaptureTracer()
    monkeypatch.setattr(observability, "_TRACER", tracer)

    async with create_connected_server_and_client_session(server.mcp, client_info=CHATGPT) as session:
        result = await session.call_tool(tool, arguments)

    assert result.isError is True
    assert _payload(result.model_dump(by_alias=True)) == UNUSABLE
    assert sent == []
    [span] = tracer.spans
    assert span.attributes["mcp.error.code"] == "unusable_api_key"
    assert span.attributes["mcp.api.requests"] == 0


async def test_http_calls_with_such_a_key_send_nothing_and_say_why(sent, monkeypatch) -> None:
    """Through the real app: the keys a sign-in resolves to, taken as the account
    holds them, and a raw key in the bearer header, which reaches the server as
    Latin-1 text."""
    monkeypatch.setattr(server.mcp, "_session_manager", None)
    app = server.mcp.streamable_http_app()
    authenticator = _authenticator()
    real_resolve = authenticator.resolve
    resolved = {"jwt-legacy": UNUSABLE_KEYS[0], "jwt-broken": CONTROL_KEYS[0], "jwt-current": "sugra_test_current"}

    async def resolve(token: str, deadline: float | None = None) -> ResolvedAuth:
        token = token.strip()
        if token in resolved:
            return ResolvedAuth(
                api_key=resolved[token], user_id=7, access_token_id=token, method="oauth", platform="openai"
            )
        return await real_resolve(token)

    monkeypatch.setattr(authenticator, "resolve", resolve)
    app.add_middleware(AuthMiddleware, authenticator=authenticator)
    tracer = _CaptureTracer()
    monkeypatch.setattr(observability, "_TRACER", tracer)
    arguments = {"operation_id": _operation_without_params()}

    async def call(client: httpx.AsyncClient, session_id: str, authorization: bytes) -> dict[str, Any]:
        sent.clear()
        response = await client.post(
            "/mcp",
            json={
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {"name": "call_endpoint", "arguments": arguments},
            },
            headers=[*HEADERS.items(), ("mcp-session-id", session_id), ("authorization", authorization)],
        )
        assert response.status_code == 200
        message = next(json.loads(line[6:]) for line in response.text.splitlines() if line.startswith("data: "))
        return message["result"]

    try:
        async with server.mcp.session_manager.run():
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1:8002") as client:
                opened = await client.post(
                    "/mcp", json=INITIALIZE, headers={**HEADERS, "authorization": "Bearer jwt-current"}
                )
                assert opened.status_code == 200
                session_id = opened.headers["mcp-session-id"]
                await client.post(
                    "/mcp",
                    json={"jsonrpc": "2.0", "method": "notifications/initialized"},
                    headers={**HEADERS, "mcp-session-id": session_id, "authorization": "Bearer jwt-current"},
                )

                legacy = await call(client, session_id, b"Bearer jwt-legacy")
                assert legacy["isError"] is True
                assert _payload(legacy) == UNUSABLE
                assert sent == []

                broken = await call(client, session_id, b"Bearer jwt-broken")
                assert broken["isError"] is True
                assert _payload(broken) == UNUSABLE
                assert sent == []

                current = await call(client, session_id, b"Bearer jwt-current")
                assert current.get("isError") is not True
                assert sent == [["sugra_test_current"]]

                raw = await call(client, session_id, "Bearer sugra_test_José".encode("latin-1"))
                assert raw["isError"] is True
                assert _payload(raw) == UNUSABLE
                assert sent == []
    finally:
        await authenticator.aclose()

    spans = [span for span in tracer.spans if span.name == "mcp.tool.call_endpoint"]
    for span in spans[:2]:
        assert span.attributes["mcp.error.code"] == "unusable_api_key"
        assert span.attributes["mcp.api.requests"] == 0
    assert spans[2].attributes["mcp.success"] is True
    assert spans[2].attributes["mcp.api.requests"] == 1
