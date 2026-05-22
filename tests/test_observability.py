"""Tests for the optional Azure App Insights instrumentation layer.

Two invariants under test:

1. When ``APPLICATIONINSIGHTS_CONNECTION_STRING`` is unset (the default
   path for stdio users and local dev), the decorator is a transparent
   pass-through: identical return value, no span overhead, no SDK import.

2. When a tracer IS attached, the decorator records span dimensions per
   the privacy contract documented in observability.py:
     - mcp.tool.name
     - mcp.operation_id (when the wrapped tool received one)
     - mcp.success (true unless return dict has "error" key or exception)
     - mcp.error.code (the error key value, never the message)
     - mcp.duration_ms (integer milliseconds)

Privacy invariant covered too: the wrapped tool's query / params / body
arguments are NEVER set as span attributes, even when the tracer is
active.
"""

from __future__ import annotations

import asyncio
import importlib

import pytest

from sugra_api_mcp import observability


@pytest.fixture(autouse=True)
def reset_observability_module():
    """Each test starts with a fresh module-level _INITIALISED / _TRACER state
    so configuration in one test doesn't leak into the next.
    """
    importlib.reload(observability)
    yield
    importlib.reload(observability)


class _FakeSpan:
    """Capture-everything span: records every set_attribute, end, set_status
    so we can assert on the dimensions actually emitted.
    """

    def __init__(self, name: str):
        self.name = name
        self.attributes: dict[str, object] = {}
        self.ended = False
        self.status = None

    def set_attribute(self, key: str, value: object) -> None:
        self.attributes[key] = value

    def set_status(self, status) -> None:
        self.status = status

    def end(self) -> None:
        self.ended = True


class _FakeTracer:
    def __init__(self):
        self.spans: list[_FakeSpan] = []

    def start_span(self, name: str) -> _FakeSpan:
        span = _FakeSpan(name)
        self.spans.append(span)
        return span


def _install_fake_tracer(monkeypatch) -> _FakeTracer:
    tracer = _FakeTracer()
    monkeypatch.setattr(observability, "_TRACER", tracer)
    return tracer


def test_decorator_is_passthrough_when_tracer_not_configured() -> None:
    """No env var set, no tracer attached - decorator must be a no-op."""
    @observability.trace_mcp_tool("search_endpoints")
    async def fake_tool(query: str) -> dict:
        return {"results": [{"operation_id": "x"}], "query": query}

    result = asyncio.run(fake_tool("AAPL"))

    assert result == {"results": [{"operation_id": "x"}], "query": "AAPL"}


def test_decorator_emits_span_with_tool_name_and_success(monkeypatch) -> None:
    tracer = _install_fake_tracer(monkeypatch)

    @observability.trace_mcp_tool("search_endpoints")
    async def fake_tool(query: str) -> dict:
        return {"results": []}

    asyncio.run(fake_tool("AAPL"))

    assert len(tracer.spans) == 1
    span = tracer.spans[0]
    assert span.name == "mcp.tool.search_endpoints"
    assert span.attributes["mcp.tool.name"] == "search_endpoints"
    assert span.attributes["mcp.success"] is True
    assert isinstance(span.attributes["mcp.duration_ms"], int)
    assert span.attributes["mcp.duration_ms"] >= 0
    assert span.ended is True


def test_decorator_ignores_operation_id_passed_positionally(monkeypatch) -> None:
    """Privacy / correctness: capture operation_id ONLY from kwargs.

    search_endpoints and fetch_data take a query string as the first
    positional arg. Capturing positional[0] as operation_id would label
    raw user queries as operation_ids - both a privacy leak (raw query
    in App Insights) and a metrics-pollution issue (mcp.operation_id
    cardinality explodes). The MCP runtime always passes args as kwargs
    per JSON-RPC params unpacking, so this restriction loses nothing in
    production.
    """
    tracer = _install_fake_tracer(monkeypatch)

    @observability.trace_mcp_tool("describe_endpoint")
    async def fake_describe(operation_id: str) -> dict:
        return {"operation_id": operation_id}

    asyncio.run(fake_describe("quotes_symbol_price"))

    # Positional arg did not appear in the span.
    assert "mcp.operation_id" not in tracer.spans[0].attributes


def test_decorator_captures_operation_id_from_kwarg(monkeypatch) -> None:
    tracer = _install_fake_tracer(monkeypatch)

    @observability.trace_mcp_tool("call_endpoint")
    async def fake_call(operation_id: str, params: dict | None = None) -> dict:
        return {"data": {}}

    asyncio.run(fake_call(operation_id="fred_series_series_id", params={"series_id": "CPIAUCSL"}))

    assert tracer.spans[0].attributes["mcp.operation_id"] == "fred_series_series_id"


def test_decorator_marks_success_false_when_dict_has_error_key(monkeypatch) -> None:
    """The catalog-level error path: tool returns {"error": "code"} and the
    decorator must extract the code into mcp.error.code without surfacing
    any free-text body.
    """
    tracer = _install_fake_tracer(monkeypatch)

    @observability.trace_mcp_tool("call_endpoint")
    async def fake_call(operation_id: str) -> dict:
        return {"error": "unknown_operation_id", "operation_id": operation_id}

    asyncio.run(fake_call("bogus_op"))

    span = tracer.spans[0]
    assert span.attributes["mcp.success"] is False
    assert span.attributes["mcp.error.code"] == "unknown_operation_id"


def test_decorator_records_exception_without_message(monkeypatch) -> None:
    """When the tool itself raises, the decorator must mark the span ERROR,
    record the exception class name, and re-raise. The exception MESSAGE
    must never be attached - it may contain query content / PII.
    """
    tracer = _install_fake_tracer(monkeypatch)

    @observability.trace_mcp_tool("call_endpoint")
    async def fake_call() -> dict:
        raise ValueError("user query: super secret content")

    with pytest.raises(ValueError, match="super secret content"):
        asyncio.run(fake_call())

    span = tracer.spans[0]
    assert span.attributes["mcp.success"] is False
    assert span.attributes["mcp.error.code"] == "exception"
    assert span.attributes["mcp.exception.type"] == "ValueError"
    # Privacy invariant: the message must not appear in any attribute value.
    for key, value in span.attributes.items():
        if isinstance(value, str):
            assert "super secret content" not in value, (
                f"exception message leaked into attribute {key}={value!r}"
            )
    assert span.ended is True


def test_decorator_does_not_attach_args_as_attributes(monkeypatch) -> None:
    """Privacy invariant: query string, params dict, body must NEVER appear
    in span attributes. Only catalog-derived metadata is allowed.
    """
    tracer = _install_fake_tracer(monkeypatch)

    @observability.trace_mcp_tool("search_endpoints")
    async def fake_tool(query: str, toolset: str | None = None) -> dict:
        return {"results": []}

    sensitive_query = "extremely-private-user-search-XYZQR"
    asyncio.run(fake_tool(sensitive_query, toolset="markets"))

    span = tracer.spans[0]
    for value in span.attributes.values():
        if isinstance(value, str):
            assert sensitive_query not in value, (
                f"query content leaked: {value!r}"
            )


def test_setup_observability_returns_false_when_env_unset(monkeypatch) -> None:
    monkeypatch.delenv("APPLICATIONINSIGHTS_CONNECTION_STRING", raising=False)
    assert observability.setup_observability() is False


def test_setup_observability_is_idempotent(monkeypatch) -> None:
    """Calling setup_observability twice must not re-import SDK or replace
    a working tracer. _INITIALISED guards against double-init.
    """
    monkeypatch.delenv("APPLICATIONINSIGHTS_CONNECTION_STRING", raising=False)
    first = observability.setup_observability()
    second = observability.setup_observability()
    assert first == second
