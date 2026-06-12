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
import os
import sys
import types

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


# ---- Privacy-contract guards ----


def test_unknown_operation_id_kwarg_is_not_attached_to_span(monkeypatch) -> None:
    """A client supplying an arbitrary string as operation_id kwarg (PII,
    secret, raw query text) must NOT have that string labeled as
    mcp.operation_id. Only catalog-known operation_ids pass the allowlist.
    """
    tracer = _install_fake_tracer(monkeypatch)

    @observability.trace_mcp_tool("call_endpoint")
    async def fake_call(operation_id: str) -> dict:
        return {"data": {}}

    secret_like = "user_email_arman_at_outlook_dot_com_SECRET_XYZQ"
    asyncio.run(fake_call(operation_id=secret_like))

    # The string is not in the catalog -> must not appear in span attributes.
    assert "mcp.operation_id" not in tracer.spans[0].attributes
    for value in tracer.spans[0].attributes.values():
        if isinstance(value, str):
            assert secret_like not in value


def test_known_operation_id_kwarg_is_attached_to_span(monkeypatch) -> None:
    """Positive contract: a real catalog-known operation_id IS recorded as
    mcp.operation_id. Demonstrates the allowlist works both ways - real
    values pass, arbitrary strings are dropped.
    """
    tracer = _install_fake_tracer(monkeypatch)

    @observability.trace_mcp_tool("call_endpoint")
    async def fake_call(operation_id: str) -> dict:
        return {"data": {}}

    # quotes_symbol_price exists in the bundled catalog (stable since v0.4.0).
    asyncio.run(fake_call(operation_id="quotes_symbol_price"))

    assert tracer.spans[0].attributes.get("mcp.operation_id") == "quotes_symbol_price"


def test_unknown_error_string_is_mapped_to_unknown_error(monkeypatch) -> None:
    """Any string at result["error"] that is not in the known error-code
    allowlist must be mapped to the constant "unknown_error". Prevents
    free-text upstream error messages from reaching the span.
    """
    tracer = _install_fake_tracer(monkeypatch)

    @observability.trace_mcp_tool("call_endpoint")
    async def fake_call(operation_id: str = "test") -> dict:
        return {
            "error": "Upstream API returned: invalid JWT for user@example.com leaked secret_token=abc123",
        }

    asyncio.run(fake_call())

    span = tracer.spans[0]
    assert span.attributes["mcp.success"] is False
    assert span.attributes["mcp.error.code"] == "unknown_error"
    # Privacy invariant: the upstream free-text must not appear anywhere.
    for value in span.attributes.values():
        if isinstance(value, str):
            assert "secret_token" not in value
            assert "user@example.com" not in value


def test_known_error_code_is_passed_through(monkeypatch) -> None:
    """Positive contract: known catalog error codes (unknown_operation_id /
    missing_required_parameters / etc.) ARE preserved on the span - they
    are operational signal, not free-text.
    """
    tracer = _install_fake_tracer(monkeypatch)

    @observability.trace_mcp_tool("call_endpoint")
    async def fake_call() -> dict:
        return {"error": "missing_required_parameters", "missing": ["symbol"]}

    asyncio.run(fake_call())

    span = tracer.spans[0]
    assert span.attributes["mcp.error.code"] == "missing_required_parameters"


@pytest.mark.parametrize(
    "code",
    [
        "upstream_timeout",
        "upstream_connect_error",
        "upstream_transport_error",
        "tool_execution_failed",
        "agent_plane_unavailable",
    ],
)
def test_transport_error_codes_pass_the_allowlist(monkeypatch, code: str) -> None:
    """MCP-Imp-1: the structured transport-error codes returned by
    SugraClient (and the gateway safety net) must reach spans verbatim,
    not collapse into "unknown_error" - otherwise timeout vs connect vs
    disconnect are indistinguishable in App Insights.
    """
    tracer = _install_fake_tracer(monkeypatch)

    @observability.trace_mcp_tool("call_endpoint")
    async def fake_call() -> dict:
        return {"error": code, "reason": "free text stays out of spans", "elapsed_ms": 30000}

    asyncio.run(fake_call())

    span = tracer.spans[0]
    assert span.attributes["mcp.success"] is False
    assert span.attributes["mcp.error.code"] == code
    # Privacy: the free-text reason must not appear in any span attribute.
    for value in span.attributes.values():
        if isinstance(value, str):
            assert "free text stays out of spans" not in value


def test_result_attrs_extractor_emits_extra_dimensions(monkeypatch) -> None:
    """MCP-2.3: the optional result_attrs extractor enriches SUCCESS spans
    with envelope-metadata dimensions (recipe_version / units / ...). The
    extractor owns the privacy allowlist; the wrapper just applies its output.
    """
    tracer = _install_fake_tracer(monkeypatch)

    @observability.trace_mcp_tool(
        "get_snapshot",
        result_attrs=lambda result: {"mcp.agent.units": result["billing"]["rate_limit_cost"]},
    )
    async def fake_snapshot() -> dict:
        return {"status": "full", "billing": {"rate_limit_cost": 2}}

    asyncio.run(fake_snapshot())

    span = tracer.spans[0]
    assert span.attributes["mcp.agent.units"] == 2
    assert span.attributes["mcp.success"] is True


def test_agent_extractor_emits_all_five_dimensions_no_request_values(monkeypatch) -> None:
    """The REAL agent extractor through the real decorator: all five
    mcp.agent.* dimensions land on the span, and no request/entity value
    (ticker, query text) appears in ANY span attribute (privacy invariant)."""
    from sugra_api_mcp.tools.agent import _agent_result_attrs

    tracer = _install_fake_tracer(monkeypatch)

    @observability.trace_mcp_tool("get_snapshot", result_attrs=_agent_result_attrs)
    async def fake_snapshot(recipe: str, entity: dict) -> dict:
        return {
            "schema_version": "1",
            "recipe_version": "company_snapshot@1",
            "status": "partial",
            "data": {"price": {"price": 123.4, "symbol": "SECRETTICKER"}},
            "freshness": {"class": "computed_mixed", "stale": True},
            "billing": {"rate_limit_cost": 2, "downstream_calls": 4, "remaining": 998},
        }

    asyncio.run(fake_snapshot("company_snapshot", {"namespace": "equity", "ids": {"symbol": "SECRETTICKER"}}))

    span = tracer.spans[0]
    assert span.attributes["mcp.agent.recipe_version"] == "company_snapshot@1"
    assert span.attributes["mcp.agent.status"] == "partial"
    assert span.attributes["mcp.agent.units"] == 2
    assert span.attributes["mcp.agent.downstream_calls"] == 4
    assert span.attributes["mcp.agent.stale"] is True
    # Privacy: request/entity values never reach span attributes.
    for value in span.attributes.values():
        if isinstance(value, str):
            assert "SECRETTICKER" not in value


def test_result_attrs_extractor_skipped_on_error_result(monkeypatch) -> None:
    """Error results carry no envelope metadata worth extracting - the
    extractor only runs on the success path."""
    tracer = _install_fake_tracer(monkeypatch)
    calls: list[object] = []

    def _extract(result: object) -> dict[str, object]:
        calls.append(result)
        return {"mcp.agent.units": 99}

    @observability.trace_mcp_tool("get_snapshot", result_attrs=_extract)
    async def fake_snapshot() -> dict:
        return {"error": "agent_plane_unavailable", "status_code": 403}

    asyncio.run(fake_snapshot())

    assert calls == []
    assert "mcp.agent.units" not in tracer.spans[0].attributes


def test_result_attrs_extractor_crash_is_swallowed(monkeypatch) -> None:
    """A buggy extractor must neither break the tool result nor the base
    dimensions."""
    tracer = _install_fake_tracer(monkeypatch)

    def _boom(result: object) -> dict[str, object]:
        raise RuntimeError("extractor crash")

    @observability.trace_mcp_tool("get_snapshot", result_attrs=_boom)
    async def fake_snapshot() -> dict:
        return {"status": "full"}

    assert asyncio.run(fake_snapshot()) == {"status": "full"}
    assert tracer.spans[0].attributes["mcp.success"] is True


def test_telemetry_failure_does_not_mask_tool_result(monkeypatch) -> None:
    """If set_attribute crashes (e.g. exporter has rolled out a breaking
    change), the wrapper must still return the tool's real result rather
    than masking the call with a telemetry error.
    """
    class _BrokenSpan:
        def __init__(self, name: str):
            self.ended = False

        def set_attribute(self, key: str, value: object) -> None:
            raise RuntimeError("simulated exporter failure")

        def set_status(self, status) -> None:
            raise RuntimeError("simulated exporter failure")

        def end(self) -> None:
            self.ended = True

    class _BrokenTracer:
        def __init__(self):
            self.last_span: _BrokenSpan | None = None

        def start_span(self, name: str) -> _BrokenSpan:
            self.last_span = _BrokenSpan(name)
            return self.last_span

    tracer = _BrokenTracer()
    monkeypatch.setattr(observability, "_TRACER", tracer)

    @observability.trace_mcp_tool("search_endpoints")
    async def fake_tool(query: str) -> dict:
        return {"results": [{"operation_id": "x"}]}

    # Real tool result must come through even when every telemetry call
    # throws on us.
    result = asyncio.run(fake_tool("AAPL"))
    assert result == {"results": [{"operation_id": "x"}]}
    # Span end must still have been attempted (via finally clause).
    assert tracer.last_span is not None
    assert tracer.last_span.ended is True


def test_telemetry_failure_does_not_mask_tool_exception(monkeypatch) -> None:
    """When the tool itself raises AND telemetry calls also fail, the
    decorator must re-raise the TOOL's exception, not the telemetry one.
    """
    class _BrokenSpan:
        def __init__(self, name: str):
            self.ended = False

        def set_attribute(self, key: str, value: object) -> None:
            raise RuntimeError("exporter died")

        def set_status(self, status) -> None:
            raise RuntimeError("exporter died")

        def end(self) -> None:
            self.ended = True

    class _BrokenTracer:
        def __init__(self):
            self.last_span: _BrokenSpan | None = None

        def start_span(self, name: str) -> _BrokenSpan:
            self.last_span = _BrokenSpan(name)
            return self.last_span

    tracer = _BrokenTracer()
    monkeypatch.setattr(observability, "_TRACER", tracer)

    @observability.trace_mcp_tool("call_endpoint")
    async def fake_call() -> dict:
        raise ValueError("the tool's real error")

    with pytest.raises(ValueError, match="the tool's real error"):
        asyncio.run(fake_call())

    # span.end still called via finally.
    assert tracer.last_span is not None
    assert tracer.last_span.ended is True


def test_span_creation_failure_falls_back_to_direct_call(monkeypatch) -> None:
    """If start_span itself fails, run the tool without telemetry rather
    than mask the call.
    """
    class _BrokenTracer:
        def start_span(self, name: str):
            raise RuntimeError("tracer is broken")

    monkeypatch.setattr(observability, "_TRACER", _BrokenTracer())

    @observability.trace_mcp_tool("list_toolsets")
    async def fake_tool() -> dict:
        return {"ok": True}

    result = asyncio.run(fake_tool())
    assert result == {"ok": True}


def test_success_path_sets_status_ok(monkeypatch) -> None:
    """App Insights top-level `success` column is driven by OTel span
    status. Without explicit OK on success, KQL aggregations like
    avg(toint(success)) return NaN. The decorator must set OK on clean
    return.
    """
    tracer = _install_fake_tracer(monkeypatch)

    @observability.trace_mcp_tool("search_endpoints")
    async def fake_tool(query: str) -> dict:
        return {"results": [{"operation_id": "x"}]}

    asyncio.run(fake_tool("anything"))

    span = tracer.spans[0]
    assert span.status is not None, "status was never set on success path"
    # opentelemetry.trace.Status has a status_code attribute or is enum-like
    code = getattr(span.status, "status_code", span.status)
    assert "OK" in repr(code), f"expected OK status, got {span.status!r}"


def test_tool_reported_failure_sets_status_error(monkeypatch) -> None:
    """A tool returning {"error": "<code>"} is a tool-reported failure
    (NOT an exception). The decorator should map it to span status ERROR
    so App Insights success column reflects it.
    """
    tracer = _install_fake_tracer(monkeypatch)

    @observability.trace_mcp_tool("call_endpoint")
    async def fake_tool(operation_id: str) -> dict:
        return {"error": "unknown_operation_id"}

    asyncio.run(fake_tool(operation_id="nonexistent_op_id"))

    span = tracer.spans[0]
    assert span.status is not None, "status was never set on failure path"
    code = getattr(span.status, "status_code", span.status)
    assert "ERROR" in repr(code), f"expected ERROR status, got {span.status!r}"


def test_exception_path_sets_status_error(monkeypatch) -> None:
    """When the wrapped tool raises, span status must be ERROR."""
    tracer = _install_fake_tracer(monkeypatch)

    @observability.trace_mcp_tool("call_endpoint")
    async def fake_tool() -> dict:
        raise RuntimeError("boom")

    with pytest.raises(RuntimeError):
        asyncio.run(fake_tool())

    span = tracer.spans[0]
    assert span.status is not None
    code = getattr(span.status, "status_code", span.status)
    assert "ERROR" in repr(code)


def test_setup_sets_otel_service_name_default(monkeypatch) -> None:
    """The SDK's configure_azure_monitor takes **kwargs and silently drops
    unknown keys (verified empirically against azure-monitor-opentelemetry
    1.8.8: a `resource_attributes` dict left cloud_RoleName at
    `unknown_service`). The canonical OTel override is OTEL_SERVICE_NAME,
    which the SDK honours via the Resource auto-detector. setup must set
    this env var before calling configure_azure_monitor.
    """
    monkeypatch.setenv(
        "APPLICATIONINSIGHTS_CONNECTION_STRING",
        "InstrumentationKey=00000000-0000-0000-0000-000000000000",
    )
    monkeypatch.delenv("OTEL_SERVICE_NAME", raising=False)

    captured_env: dict[str, str | None] = {}

    def _fake_configure(**kwargs) -> None:
        captured_env["OTEL_SERVICE_NAME"] = os.environ.get("OTEL_SERVICE_NAME")

    fake_module = types.ModuleType("azure.monitor.opentelemetry")
    fake_module.configure_azure_monitor = _fake_configure
    monkeypatch.setitem(sys.modules, "azure.monitor.opentelemetry", fake_module)

    fake_trace_mod = types.ModuleType("opentelemetry")
    fake_trace_mod.trace = types.SimpleNamespace(
        get_tracer=lambda _name: object()
    )
    monkeypatch.setitem(sys.modules, "opentelemetry", fake_trace_mod)
    monkeypatch.setitem(sys.modules, "opentelemetry.trace", fake_trace_mod.trace)

    assert observability.setup_observability() is True
    assert captured_env["OTEL_SERVICE_NAME"] == "sugra-mcp"


def test_setup_preserves_operator_otel_service_name_override(monkeypatch) -> None:
    """setdefault respects any operator-supplied OTEL_SERVICE_NAME (e.g. for
    multi-tenant deployments where one runtime hosts two MCP profiles).
    """
    monkeypatch.setenv(
        "APPLICATIONINSIGHTS_CONNECTION_STRING",
        "InstrumentationKey=00000000-0000-0000-0000-000000000000",
    )
    monkeypatch.setenv("OTEL_SERVICE_NAME", "sugra-mcp-staging")

    captured: dict[str, str | None] = {}

    def _fake_configure(**kwargs) -> None:
        captured["OTEL_SERVICE_NAME"] = os.environ.get("OTEL_SERVICE_NAME")

    fake_module = types.ModuleType("azure.monitor.opentelemetry")
    fake_module.configure_azure_monitor = _fake_configure
    monkeypatch.setitem(sys.modules, "azure.monitor.opentelemetry", fake_module)

    fake_trace_mod = types.ModuleType("opentelemetry")
    fake_trace_mod.trace = types.SimpleNamespace(
        get_tracer=lambda _name: object()
    )
    monkeypatch.setitem(sys.modules, "opentelemetry", fake_trace_mod)
    monkeypatch.setitem(sys.modules, "opentelemetry.trace", fake_trace_mod.trace)

    assert observability.setup_observability() is True
    assert captured["OTEL_SERVICE_NAME"] == "sugra-mcp-staging"
