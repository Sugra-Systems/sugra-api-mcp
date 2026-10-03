"""Which tool calls leave a span, and what the gate line of each says.

A call that authenticated with OAuth leaves a span once it is admitted, even
when it names no registered tool or its arguments fail validation. A call that
authenticated with a sugra_ API key is unverified until the Sugra API answers
it, so it leaves a span only once it sent its first API request; before that
only its gate line counts it. These tests drive the real app in process (the
gate in front of AuthMiddleware in front of the SDK session manager) against a
mock Sugra API, one test per entry kind and outcome.
"""

from __future__ import annotations

import contextlib
import re
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
import pytest
from mcp.server.fastmcp import FastMCP

import sugra_api_mcp.tools  # noqa: F401  (registers the tools on server.mcp)
from sugra_api_mcp import gate, observability, server
from sugra_api_mcp.auth import AuthMiddleware, ResolvedAuth
from sugra_api_mcp.client import SugraClient
from sugra_api_mcp.config import Config
from sugra_api_mcp.tools import gateway
from sugra_api_mcp.tools.agent import register_agent_tools
from tests.test_request_credentials import (
    HEADERS,
    INITIALIZE,
    _authenticator,
    _CaptureSpan,
    _CaptureTracer,
    _operation_without_params,
)

_REQUEST_ID = "5d41402abc4b2a76b9719d911017c592"
_BEARER = {"oauth": "jwt-telemetry", "api_key": "sugra_dummy"}
# The key each entry kind's API requests carry: an OAuth token resolves to
# its account's key, a sugra_ bearer is its own.
_SENT_KEY = {"oauth": "sugra_TENANT_telemetry", "api_key": "sugra_dummy"}
_OPERATION = _operation_without_params()

Answer = Callable[[httpx.Request], httpx.Response]
Call = Callable[[str, str, dict[str, Any]], Awaitable[None]]


def _status(code: int) -> Answer:
    def answer(request: httpx.Request) -> httpx.Response:
        if code == 200:
            return httpx.Response(200, json={"data": [{"ok": 1}], "meta": {}})
        return httpx.Response(code, json={"detail": "refused"})

    return answer


def _raise(error: type[httpx.TransportError]) -> Answer:
    def answer(request: httpx.Request) -> httpx.Response:
        raise error("no answer", request=request)

    return answer


@dataclass(frozen=True)
class _Outcome:
    tool: str
    arguments: dict[str, Any]
    flags: str
    api_requests: int
    span: str
    error_code: str | None = None
    # The mock API's answer, or None when no request may reach it.
    answer: Answer | None = None


_ENDPOINT = {"operation_id": _OPERATION}
_OUTCOMES = {
    "api-success": _Outcome("call_endpoint", _ENDPOINT, "T", 1, "mcp.tool.call_endpoint", answer=_status(200)),
    "search_endpoints": _Outcome("search_endpoints", {"query": "consumer prices"}, "T", 0, "mcp.tool.search_endpoints"),
    "describe_endpoint": _Outcome("describe_endpoint", _ENDPOINT, "T", 0, "mcp.tool.describe_endpoint"),
    "list_toolsets": _Outcome("list_toolsets", {}, "T", 0, "mcp.tool.list_toolsets"),
    "list_sources": _Outcome("list_sources", {}, "T", 0, "mcp.tool.list_sources"),
    "unknown-tool": _Outcome("no_such_tool", {}, "TF", 0, "mcp.tool.unknown", "unknown_tool"),
    "invalid-arguments": _Outcome("call_endpoint", {"params": {}}, "TF", 0, "mcp.tool.call_endpoint", "invalid_arguments"),
    "refused-by-its-tool": _Outcome(
        "call_endpoint", {"operation_id": "no_such_operation"}, "TF", 0, "mcp.tool.call_endpoint", "unknown_operation_id"
    ),
    "api-5xx": _Outcome("call_endpoint", _ENDPOINT, "TF", 1, "mcp.tool.call_endpoint", "upstream_http_503", _status(503)),
    "api-timeout": _Outcome(
        "call_endpoint", _ENDPOINT, "TF", 1, "mcp.tool.call_endpoint", "upstream_timeout", _raise(httpx.ReadTimeout)
    ),
    "api-connect-error": _Outcome(
        "call_endpoint", _ENDPOINT, "TF", 1, "mcp.tool.call_endpoint", "upstream_connect_error", _raise(httpx.ConnectError)
    ),
    "api-disconnect": _Outcome(
        "call_endpoint",
        _ENDPOINT,
        "TF",
        1,
        "mcp.tool.call_endpoint",
        "upstream_transport_error",
        _raise(httpx.RemoteProtocolError),
    ),
    "api-401": _Outcome("call_endpoint", _ENDPOINT, "TA", 1, "mcp.tool.call_endpoint", "upstream_http_401", _status(401)),
    "api-403": _Outcome("call_endpoint", _ENDPOINT, "TA", 1, "mcp.tool.call_endpoint", "upstream_http_403", _status(403)),
}


class _Recorded:
    """What one test's mock Sugra API, gate summary and tracer saw."""

    def __init__(self, answer: Answer | None) -> None:
        self.answer = answer
        self.sent: list[str] = []
        self.lines: list[str] = []
        self.tracer = _CaptureTracer()
        self.summary = gate.GateSummary()
        # GateMiddleware hands each line to add: keep them, in order.
        self.summary.add = self.lines.append  # type: ignore[method-assign]

    def api(self, request: httpx.Request) -> httpx.Response:
        self.sent.append(request.headers["x-api-key"])
        if self.answer is None:
            return httpx.Response(500, json={"detail": "no request was expected"})
        return self.answer(request)

    def tool_spans(self) -> list[_CaptureSpan]:
        return [span for span in self.tracer.spans if span.name.startswith("mcp.tool.")]


@contextlib.asynccontextmanager
async def _served(monkeypatch, recorded: _Recorded) -> AsyncIterator[Call]:
    """An initialized session on the real app; yields call(entry, tool, arguments)."""
    built: list[SugraClient] = []

    def build_client(api_key: str) -> SugraClient:
        config = Config(api_base="https://api.test", api_key=api_key, timeout=5.0)
        client = SugraClient(config, transport=httpx.MockTransport(recorded.api))
        built.append(client)
        return client

    monkeypatch.delenv("CONTAINER_APP_REVISION", raising=False)
    monkeypatch.delenv("SUGRA_API_KEY", raising=False)
    await server.close_clients()
    monkeypatch.setattr(server, "_build_client", build_client)
    monkeypatch.setattr(observability, "_TRACER", recorded.tracer)
    monkeypatch.setattr(server.mcp, "_session_manager", None)
    app = server.mcp.streamable_http_app()
    authenticator = _authenticator()
    real_resolve = authenticator.resolve

    async def resolve(token: str) -> ResolvedAuth:
        token = token.strip()
        if token == _BEARER["oauth"]:
            return ResolvedAuth(
                api_key=_SENT_KEY["oauth"],
                user_id=42,
                access_token_id="jti-telemetry",
                method="oauth",
                platform="anthropic",
            )
        return await real_resolve(token)

    monkeypatch.setattr(authenticator, "resolve", resolve)
    app.add_middleware(AuthMiddleware, authenticator=authenticator)
    app.add_middleware(
        gate.GateMiddleware, max_body_bytes=server.mcp.settings.max_request_body_size, summary=recorded.summary
    )
    try:
        async with server.mcp.session_manager.run():
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1:8002") as client:
                opened = await client.post("/mcp", json=INITIALIZE, headers=HEADERS)
                session = {"mcp-session-id": opened.headers["mcp-session-id"]}
                await client.post(
                    "/mcp",
                    json={"jsonrpc": "2.0", "method": "notifications/initialized"},
                    headers={**HEADERS, **session},
                )

                async def call(entry: str, tool: str, arguments: dict[str, Any]) -> None:
                    response = await client.post(
                        "/mcp",
                        json={
                            "jsonrpc": "2.0",
                            "id": 2,
                            "method": "tools/call",
                            "params": {"name": tool, "arguments": arguments},
                        },
                        headers={
                            **HEADERS,
                            **session,
                            "authorization": f"Bearer {_BEARER[entry]}",
                            "x-request-id": _REQUEST_ID,
                        },
                    )
                    assert response.status_code == 200

                yield call
    finally:
        for client in built:
            await client.aclose()
        await authenticator.aclose()


def _assert_line(line: str, flags: str, api_requests: int) -> None:
    assert re.fullmatch(rf"{_REQUEST_ID} {flags} \d+ {api_requests}", line), line


def _assert_placed(span: _CaptureSpan, entry: str, api_requests: int) -> None:
    """The attributes every span of an admitted call carries, whatever its outcome."""
    attributes = span.attributes
    assert span.ended
    assert attributes["mcp.caller.auth"] == entry
    assert attributes["mcp.side"] == "vm"
    assert attributes["mcp.request.id"] == _REQUEST_ID
    assert attributes["mcp.api.requests"] == api_requests
    assert attributes.get("enduser.id") == ("42" if entry == "oauth" else None)
    for secret in (*_BEARER.values(), *_SENT_KEY.values()):
        assert secret not in repr(attributes)


@pytest.mark.parametrize("outcome", list(_OUTCOMES))
@pytest.mark.parametrize("entry", ["oauth", "api_key"])
async def test_a_call_leaves_its_line_and_its_span(monkeypatch, entry: str, outcome: str) -> None:
    case = _OUTCOMES[outcome]
    recorded = _Recorded(case.answer)
    async with _served(monkeypatch, recorded) as call:
        await call(entry, case.tool, case.arguments)

    [line] = recorded.lines
    _assert_line(line, case.flags, case.api_requests)
    assert recorded.sent == [_SENT_KEY[entry]] * case.api_requests

    spans = recorded.tool_spans()
    if entry == "api_key" and case.api_requests == 0:
        # Unverified and never sent to the API: the gate line is its only record.
        assert spans == []
        return
    [span] = spans
    assert span.name == case.span
    _assert_placed(span, entry, case.api_requests)
    assert span.attributes.get("mcp.success") is (case.error_code is None)
    assert span.attributes.get("mcp.error.code") == case.error_code
    if case.span == "mcp.tool.unknown":
        assert "mcp.tool.name" not in span.attributes
        assert case.tool not in repr(span.attributes)
    else:
        assert span.attributes["mcp.tool.name"] == case.tool
    # An OAuth span opens with the call; a key call's opens at its exit,
    # dated from the call's entry.
    if entry == "oauth":
        assert span.start_time is None
    else:
        assert isinstance(span.start_time, int)


@pytest.mark.parametrize("entry", ["oauth", "api_key"])
async def test_a_call_refused_at_admission_keeps_its_span(monkeypatch, entry: str) -> None:
    recorded = _Recorded(None)
    async with _served(monkeypatch, recorded) as call:
        monkeypatch.setattr(server, "MAX_IN_FLIGHT_TOOL_CALLS", 0)
        await call(entry, "call_endpoint", _ENDPOINT)

    [line] = recorded.lines
    _assert_line(line, "TF", 0)
    assert recorded.sent == []
    # A refusal at the in-flight cap leaves its span for every caller
    # (observability.record_refused_call).
    [span] = recorded.tool_spans()
    assert span.name == "mcp.tool.call_endpoint"
    assert span.attributes["mcp.error.code"] == "server_busy"
    assert span.attributes["mcp.caller.auth"] == entry


@pytest.mark.parametrize("entry", ["oauth", "api_key"])
async def test_a_tool_that_failed_inside_is_not_counted_as_refused(monkeypatch, entry: str) -> None:
    def broken_catalog() -> Any:
        raise RuntimeError("catalog unavailable")

    recorded = _Recorded(None)
    async with _served(monkeypatch, recorded) as call:
        monkeypatch.setattr(gateway, "load_catalog", broken_catalog)
        await call(entry, "list_toolsets", {})

    [line] = recorded.lines
    _assert_line(line, "TF", 0)
    spans = recorded.tool_spans()
    if entry == "api_key":
        assert spans == []
        return
    # The tool's own span, never a second one for an unstarted call.
    [span] = spans
    assert span.name == "mcp.tool.list_toolsets"
    assert span.attributes["mcp.error.code"] == "exception"
    assert span.attributes["mcp.exception.type"] == "RuntimeError"
    _assert_placed(span, entry, 0)


async def test_a_key_the_api_refused_is_sent_again_on_the_next_call(monkeypatch) -> None:
    recorded = _Recorded(_status(401))
    async with _served(monkeypatch, recorded) as call:
        await call("api_key", "call_endpoint", _ENDPOINT)
        await call("api_key", "call_endpoint", _ENDPOINT)

    assert recorded.sent == [_SENT_KEY["api_key"]] * 2
    assert len(recorded.lines) == 2
    for line in recorded.lines:
        _assert_line(line, "TA", 1)
    spans = recorded.tool_spans()
    assert [span.attributes["mcp.error.code"] for span in spans] == ["upstream_http_401"] * 2


def _traced(tool: Any) -> bool:
    code = tool.fn.__code__
    return (
        code.co_qualname == "trace_mcp_tool.<locals>.decorator.<locals>.wrapper"
        and Path(code.co_filename).name == "observability.py"
    )


def test_every_tool_runs_inside_its_span_wrapper(monkeypatch) -> None:
    """call_tool tells a refused call from a tool's own failure by the flag the
    span wrapper sets on entry, so every tool, hosted ones included, must run
    inside it."""
    monkeypatch.setenv("SUGRA_AGENT_INTERNAL_TOKEN", "x")
    hosted = FastMCP("telemetry-probe")
    assert register_agent_tools(hosted) is True
    tools = [*server.mcp._tool_manager.list_tools(), *hosted._tool_manager.list_tools()]
    assert {tool.name for tool in tools} >= {
        "search_endpoints",
        "describe_endpoint",
        "call_endpoint",
        "fetch_data",
        "list_toolsets",
        "list_sources",
        "sugra_entity_screen",
        "sugra_entity_lookup",
        "resolve_entity",
        "get_snapshot",
        "get_timeseries",
    }
    assert [tool.name for tool in tools if not _traced(tool)] == []
