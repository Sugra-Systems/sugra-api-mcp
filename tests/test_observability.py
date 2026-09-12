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
import time
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


# ---- MCP-19: an HTTP failure is named by its STATUS, never by the API's text ----


# Attribute names a span may carry on each path. A privacy assertion that only
# scans STRING values for ONE sentinel let a mutation attach the whole error
# text under a new key as a list, and another attach the url under a key no
# sentinel was seeded in - both survived every test (codex r1). Pinning the
# name set kills both; the sentinels below then guard the values.
_BASE_ATTRS = frozenset({"mcp.tool.name", "mcp.success", "mcp.duration_ms"})
_FAILURE_ATTRS = _BASE_ATTRS | {"mcp.error.code"}

# The one scalar type each attribute may carry. Checked with `type(value) is`
# so a bool never passes as an int and a list never passes as a string: a
# mutation wrapping the tool name in a list on one status survived the name
# set and the sentinel scan alone (codex r2).
_ATTR_TYPES: dict[str, type] = {
    "mcp.tool.name": str,
    "mcp.success": bool,
    "mcp.duration_ms": int,
    "mcp.error.code": str,
    "mcp.operation_id": str,
    "mcp.exception.type": str,
    "mcp.agent.recipe_version": str,
    "mcp.agent.status": str,
    "mcp.agent.units": int,
    "mcp.agent.downstream_calls": int,
    "mcp.agent.stale": bool,
}

_API_TEXT = "Unknown ticker NOPE for user@example.com (token=abc123)"
_API_URL = "https://sugra.ai/api/v1/quotes/SECRETSYM-IN-URL/price?apikey=urlsecret"
_API_REQUEST_ID = "req-SECRETREQID"
_API_SENTINELS = ("user@example.com", "abc123", "SECRETSYM-IN-URL", "urlsecret", "SECRETREQID")


def _http_failure(status: object) -> dict:
    """The dict SugraClient._handle builds for a non-2xx answer: the API's own
    text sits at "error" for the caller, the status sits beside it, and every
    other field carries its own sentinel so a leak of ANY of them is caught."""
    return {
        "error": _API_TEXT,
        "status_code": status,
        "url": _API_URL,
        "elapsed_ms": 12,
        "request_id": _API_REQUEST_ID,
    }


def _leaves(value: object):
    """Every scalar inside a possibly nested attribute value."""
    if isinstance(value, dict):
        for item in value.values():
            yield from _leaves(item)
    elif isinstance(value, (list, tuple, set, frozenset)):
        for item in value:
            yield from _leaves(item)
    else:
        yield value


def _assert_span_is_clean(span: _FakeSpan, allowed: frozenset[str], *sentinels: str) -> None:
    """The span carries ONLY allowed attribute names, each value is exactly
    the scalar type that name may carry, and no sentinel appears in any value
    however it is nested or typed."""
    extra = set(span.attributes) - allowed
    assert not extra, f"unexpected span attributes: {sorted(extra)}"
    for key, value in span.attributes.items():
        assert type(value) is _ATTR_TYPES[key], f"{key} carries {type(value).__name__}: {value!r}"
        for leaf in _leaves(value):
            text = leaf if isinstance(leaf, str) else repr(leaf)
            for sentinel in sentinels:
                assert sentinel not in text, f"{sentinel!r} leaked into {key}={value!r}"


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        (400, "upstream_http_400"),
        (401, "upstream_http_401"),
        (403, "upstream_http_403"),
        (404, "upstream_http_404"),
        (422, "upstream_http_422"),
        (429, "upstream_http_429"),
        (500, "upstream_http_500"),
        (502, "upstream_http_502"),
        (503, "upstream_http_503"),
        (504, "upstream_http_504"),
        # Statuses the API does not return deliberately fall into their class.
        (307, "upstream_http_3xx"),
        (405, "upstream_http_4xx"),
        (410, "upstream_http_4xx"),
        (599, "upstream_http_5xx"),
    ],
)
def test_http_failure_is_named_by_status_not_by_text(monkeypatch, status: int, expected: str) -> None:
    """66% of failures were `unknown_error` because the client keeps the API's
    free-text `error` for the caller and the allowlist rightly refuses it. The
    status beside it is an int the client set from the response, so the span
    names the failure from a fixed table keyed on that status - a quota 429,
    a bad symbol 404 and an upstream outage 503 become three different codes
    while the text still never reaches the span."""
    tracer = _install_fake_tracer(monkeypatch)

    @observability.trace_mcp_tool("call_endpoint")
    async def fake_call() -> dict:
        return _http_failure(status)

    asyncio.run(fake_call())

    span = tracer.spans[0]
    assert span.attributes["mcp.success"] is False
    assert span.attributes["mcp.error.code"] == expected
    _assert_span_is_clean(span, _FAILURE_ATTRS, *_API_SENTINELS)


@pytest.mark.parametrize("status", ["503", True, None, 3.0, 200, 99, 600, -1])
def test_status_that_is_not_an_http_failure_int_stays_unknown_error(monkeypatch, status: object) -> None:
    """Only an int in 300..599 can name a status. A string (a client that
    echoes text), a bool (a subclass of int, but True is 1 and outside the
    range), a float, a 2xx or an out-of-range number all keep the residual
    code - the table is the ONLY path from a value to a span attribute, and
    it is entered by exact type and exact range."""
    tracer = _install_fake_tracer(monkeypatch)

    @observability.trace_mcp_tool("call_endpoint")
    async def fake_call() -> dict:
        return _http_failure(status)

    asyncio.run(fake_call())

    span = tracer.spans[0]
    assert span.attributes["mcp.success"] is False
    assert span.attributes["mcp.error.code"] == "unknown_error"
    _assert_span_is_clean(span, _FAILURE_ATTRS, *_API_SENTINELS)


def test_allowlisted_code_wins_over_the_status_beside_it(monkeypatch) -> None:
    """tools/agent.py remaps the plane's 403 to `agent_plane_unavailable` and
    keeps status_code=403 in the dict. The named code is the more specific
    signal and must not be overridden by the generic status mapping."""
    tracer = _install_fake_tracer(monkeypatch)

    @observability.trace_mcp_tool("get_snapshot")
    async def fake_snapshot() -> dict:
        return {"error": "agent_plane_unavailable", "status_code": 403, "reason": "plane text"}

    asyncio.run(fake_snapshot())

    span = tracer.spans[0]
    assert span.attributes["mcp.error.code"] == "agent_plane_unavailable"
    _assert_span_is_clean(span, _FAILURE_ATTRS, "plane text")


def test_every_status_derived_code_is_allowlisted() -> None:
    """The status table and the allowlist must not drift: a code the mapping
    can produce that the allowlist does not know would be a value no test
    pinned and no dashboard was told about."""
    for status in range(300, 600):
        code = observability._http_status_error_code(status)
        assert code is not None, status
        assert code in observability._KNOWN_ERROR_CODES, code
    for status in (0, 99, 100, 200, 299, 600, 999):
        assert observability._http_status_error_code(status) is None, status


def test_partial_success_envelope_with_data_is_a_success(monkeypatch) -> None:
    """One definition of failure (errors.is_error_payload): an "error" note
    BESIDE "data" is a partial-degradation success - the tool protocol reports
    it as a success and shapes it, so the span must not count it as a failure
    (it counted as `unknown_error` before, inflating the very bucket MCP-19
    measures)."""
    tracer = _install_fake_tracer(monkeypatch)

    @observability.trace_mcp_tool("call_endpoint")
    async def fake_call() -> dict:
        return {"data": [{"v": 1}], "error": "partial: one component stale", "meta": {}}

    asyncio.run(fake_call())

    span = tracer.spans[0]
    assert span.attributes["mcp.success"] is True
    _assert_span_is_clean(span, _BASE_ATTRS, "one component stale")


@pytest.mark.parametrize("error_value", ["", None, {"code": "nested"}, 42])
def test_error_key_without_data_is_a_failure_whatever_its_type(monkeypatch, error_value: object) -> None:
    """The same definition from the other side: an "error" key with no "data"
    is a failure whatever the type of its value. An empty string is still a
    string, so that one already counted; None, a dict and a number read as
    SUCCESS under the old string test and are failures now."""
    tracer = _install_fake_tracer(monkeypatch)

    @observability.trace_mcp_tool("call_endpoint")
    async def fake_call() -> dict:
        return {"error": error_value}

    asyncio.run(fake_call())

    span = tracer.spans[0]
    assert span.attributes["mcp.success"] is False
    assert span.attributes["mcp.error.code"] == "unknown_error"


@pytest.mark.parametrize("code", ["invalid_anchor", "request_failed", "missing_api_key"])
def test_entity_and_keyless_codes_pass_the_allowlist(monkeypatch, code: str) -> None:
    """Codes the entity tools (invalid_anchor, the _clean_error fallback
    request_failed) and the keyless stdio client (missing_api_key) already
    return, but which the allowlist did not know."""
    tracer = _install_fake_tracer(monkeypatch)

    @observability.trace_mcp_tool("sugra_entity_lookup")
    async def fake_lookup() -> dict:
        return {"error": code, "detail": "free text stays out of spans"}

    asyncio.run(fake_lookup())

    span = tracer.spans[0]
    assert span.attributes["mcp.error.code"] == code
    _assert_span_is_clean(span, _FAILURE_ATTRS, "free text stays out of spans")


# ---- MCP-19 (codex r1 S2): a partial envelope now reaches the agent extractor ----


_AGENT_SUCCESS_ATTRS = _BASE_ATTRS | {
    "mcp.agent.recipe_version",
    "mcp.agent.status",
    "mcp.agent.units",
    "mcp.agent.downstream_calls",
    "mcp.agent.stale",
}


@pytest.mark.parametrize(
    "recipe_version",
    [
        "user@example.com token=abc123",  # not the shape at all
        "user_ssn_123456789@1",  # the shape, but not a known recipe (codex r2)
        "token_abc123@1",
    ],
)
def test_agent_extractor_drops_a_recipe_version_that_is_not_a_known_recipe(
    monkeypatch, recipe_version: str
) -> None:
    """A partial envelope (an error note beside data) is a success now, so
    the real agent extractor runs on it. recipe_version is attached only as
    `<recipe>@<n>` for a recipe in the known set; a value that is merely
    SHAPED like one stays off the span, and so does the error note, while the
    bounded dimensions land."""
    from sugra_api_mcp.tools.agent import _agent_result_attrs

    tracer = _install_fake_tracer(monkeypatch)

    @observability.trace_mcp_tool("get_snapshot", result_attrs=_agent_result_attrs)
    async def fake_snapshot() -> dict:
        return {
            "data": {"price": {"price": 1.0}},
            "error": "partial: quote component stale for SECRETTICKER",
            "recipe_version": recipe_version,
            "status": "partial",
            "freshness": {"class": "computed_mixed", "stale": True},
            "billing": {"rate_limit_cost": 2, "downstream_calls": 3, "remaining": 40},
        }

    asyncio.run(fake_snapshot())

    span = tracer.spans[0]
    assert span.attributes["mcp.success"] is True
    assert "mcp.agent.recipe_version" not in span.attributes
    assert span.attributes["mcp.agent.status"] == "partial"
    assert span.attributes["mcp.agent.units"] == 2
    assert span.attributes["mcp.agent.stale"] is True
    _assert_span_is_clean(
        span,
        _AGENT_SUCCESS_ATTRS,
        "user@example.com",
        "abc123",
        "123456789",
        "SECRETTICKER",
        "quote component",
    )


@pytest.mark.parametrize(
    "value",
    [
        # The six distinct values production spans carried over the 90 days to
        # 2026-09-11 - the first cut of the allowlist rejected the sixth, the
        # majority of the dimension's volume, and every fixture was a snapshot.
        "quote_snapshot@1",
        "company_snapshot@1",
        "earnings_snapshot@1",
        "macro_calendar@1",
        "debt_snapshot@1",
        "timeseries.price@1",
        # The rest of the plane's manifest, and a bumped version.
        "etf_snapshot@1",
        "macro_indicator_snapshot@1",
        "timeseries.macro_series@1",
        "timeseries.etf_flows@1",
        "timeseries.etf_monthly_flows@1",
        "company_snapshot@12",
        "timeseries.price@999",
    ],
)
def test_agent_extractor_keeps_a_known_recipe_at_a_numeric_version(value: str) -> None:
    from sugra_api_mcp.tools.agent import _agent_result_attrs

    assert _agent_result_attrs({"recipe_version": value})["mcp.agent.recipe_version"] == value


@pytest.mark.parametrize(
    "value",
    [
        "",
        "company_snapshot",
        "@1",
        "company_snapshot@",
        "Company_Snapshot@1",
        "company snapshot@1",
        "company_snapshot@1 extra",
        "company_snapshot@1\n",
        "company_snapshot@0",
        "company_snapshot@01",
        "company_snapshot@1000",
        "company_snapshot@1234567",
        "unknown_recipe@1",
        "user_ssn_123456789@1",
        "timeseries.price",
        "timeseries.unknown@1",
        "timeseries@1",
        "price@1",
        "x" * 70 + "@1",
        None,
        3,
    ],
)
def test_agent_extractor_rejects_a_recipe_version_outside_the_known_set(value: object) -> None:
    from sugra_api_mcp.tools.agent import _agent_result_attrs

    assert "mcp.agent.recipe_version" not in _agent_result_attrs({"recipe_version": value})


def test_known_recipes_match_what_the_tools_document() -> None:
    """The set the extractor allowlists and the sets the tools DOCUMENT and
    ACCEPT are the same manifest: the snapshot recipes are the ones the
    get_snapshot docstring lists, and the timeseries family is exactly the
    metrics get_timeseries takes. A recipe added to one without the other is
    either invisible in telemetry or promised but never attributed."""
    import re
    from typing import get_args

    from sugra_api_mcp.tools.agent import _KNOWN_RECIPES, MetricName, get_snapshot

    documented = re.search(r"recipe \(([^)]*)\)", get_snapshot.__doc__ or "", flags=re.S)
    assert documented is not None, "get_snapshot docstring no longer lists the recipes"
    snapshot_names = {name.strip() for name in documented.group(1).replace("\n", " ").split(",")}
    series_names = {f"timeseries.{metric}" for metric in get_args(MetricName)}
    assert len(series_names) == 4
    assert snapshot_names | series_names == set(_KNOWN_RECIPES)


def test_get_timeseries_envelope_keeps_its_recipe_version_on_the_span(monkeypatch) -> None:
    """The plane's real series envelope shape through the real extractor: the
    dimension must land, or every get_timeseries success span silently loses
    it after the deploy - the regression a verification pass caught in the
    first cut of the allowlist."""
    from sugra_api_mcp.tools.agent import _agent_result_attrs

    tracer = _install_fake_tracer(monkeypatch)

    @observability.trace_mcp_tool("get_timeseries", result_attrs=_agent_result_attrs)
    async def fake_timeseries() -> dict:
        return {
            "schema_version": "1",
            "recipe_version": "timeseries.price@1",
            "status": "full",
            "data": {"points": [{"t": "2026-09-10", "v": 1.0}], "downsampled": False},
            "freshness": {"class": "computed", "stale": False},
            "billing": {"rate_limit_cost": 1, "downstream_calls": 1, "remaining": 40},
        }

    asyncio.run(fake_timeseries())

    span = tracer.spans[0]
    assert span.attributes["mcp.success"] is True
    assert span.attributes["mcp.agent.recipe_version"] == "timeseries.price@1"
    assert span.attributes["mcp.agent.status"] == "full"
    _assert_span_is_clean(span, _AGENT_SUCCESS_ATTRS)


# ---- MCP-19.1: a cancelled call leaves a verdict on its span ----


async def _dispatch(coro_factory, timeout_s: float | None, sink: list | None = None):
    """Mirror SugraFastMCP.call_tool: the dispatch timeout and the tool run in
    ONE task, and the budget (timeout plus the cancellation count at entry)
    is published for the span while it runs, or nothing is published when
    timeout_s is None. `sink` receives the budget so a test can reach the
    timeout's own deadline."""
    if timeout_s is None:
        return await coro_factory()
    timeout = asyncio.timeout(timeout_s)
    task = asyncio.current_task()
    budget = observability.DispatchBudget(timeout, task.cancelling() if task is not None else 0)
    if sink is not None:
        sink.append(budget)
    token = observability.dispatch_budget.set(budget)
    try:
        async with timeout:
            return await coro_factory()
    finally:
        observability.dispatch_budget.reset(token)


def _assert_cancelled_verdict(span: _FakeSpan, code: str, *extra_attrs: str) -> None:
    assert span.attributes["mcp.success"] is False
    assert span.attributes["mcp.error.code"] == code
    assert isinstance(span.attributes["mcp.duration_ms"], int)
    assert "ERROR" in repr(getattr(span.status, "status_code", span.status))
    assert span.ended is True
    _assert_span_is_clean(span, _FAILURE_ATTRS | set(extra_attrs))


def test_a_call_cancelled_by_the_budget_records_deadline_exceeded(monkeypatch) -> None:
    """server.py bounds dispatch with asyncio.timeout, which CANCELS the task.
    CancelledError is a BaseException, so the wrapper's `except Exception`
    never saw it and the span ended with no mcp.success at all - 66 such spans
    in 90 days, and the allowlisted deadline_exceeded never once on a span.
    call_tool publishes the timeout itself; a cancellation the wrapper sees
    while that timeout reports expired IS the budget."""
    tracer = _install_fake_tracer(monkeypatch)

    @observability.trace_mcp_tool("call_endpoint")
    async def slow_call(operation_id: str) -> dict:
        await asyncio.sleep(5)
        return {"data": []}

    with pytest.raises(TimeoutError):
        asyncio.run(_dispatch(lambda: slow_call(operation_id="quotes_symbol_price"), 0.05))

    span = tracer.spans[0]
    _assert_cancelled_verdict(span, "deadline_exceeded", "mcp.operation_id")
    assert span.attributes["mcp.operation_id"] == "quotes_symbol_price"


def test_the_budget_is_recognised_even_when_the_loop_clock_is_coarse(monkeypatch) -> None:
    """The event loop runs a timer up to one clock resolution EARLY (15.6 ms
    on Windows), so the budget can fire while loop.time() still reads before
    the deadline. Attribution therefore asks the timeout whether it fired and
    never compares clocks - a comparison filed this budget cancel as
    `cancelled` (codex r1)."""
    tracer = _install_fake_tracer(monkeypatch)

    @observability.trace_mcp_tool("get_timeseries")
    async def slow_series() -> dict:
        await asyncio.sleep(5)
        return {"status": "full"}

    async def run() -> None:
        asyncio.get_running_loop()._clock_resolution = 0.05  # the timer fires at once
        await _dispatch(slow_series, 0.03)

    with pytest.raises(TimeoutError):
        asyncio.run(run())

    _assert_cancelled_verdict(tracer.spans[0], "deadline_exceeded")


@pytest.mark.parametrize("timeout_s", [None, 10.0])
def test_a_cancellation_that_is_not_the_budget_records_cancelled(monkeypatch, timeout_s) -> None:
    """A cancellation that arrives with no timeout published, or under a
    timeout that has not fired, is a different fact - the client went away,
    the session shut down - and is filed as `cancelled` with the same
    duration and ERROR status. The exception is re-raised unchanged so
    cancellation semantics stay intact."""
    tracer = _install_fake_tracer(monkeypatch)

    @observability.trace_mcp_tool("get_snapshot")
    async def slow_snapshot() -> dict:
        await asyncio.sleep(5)
        return {"status": "full"}

    async def run() -> None:
        dispatch = asyncio.create_task(_dispatch(slow_snapshot, timeout_s))
        await asyncio.sleep(0.01)
        dispatch.cancel()
        await dispatch

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(run())

    _assert_cancelled_verdict(tracer.spans[0], "cancelled")


def test_an_external_cancel_after_the_deadline_passed_is_still_cancelled(monkeypatch) -> None:
    """The loop is blocked past the published deadline, so the budget's timer
    is due but has NOT run; then an external cancel lands first. The clock
    says the deadline is past, the timeout says it never fired - and the
    timeout is right. A clock comparison filed this on the budget (codex r1)."""
    tracer = _install_fake_tracer(monkeypatch)

    @observability.trace_mcp_tool("resolve_entity")
    async def slow_resolve() -> dict:
        await asyncio.sleep(5)
        return {"status": "resolved"}

    async def run() -> None:
        dispatch = asyncio.create_task(_dispatch(slow_resolve, 0.03))
        await asyncio.sleep(0.01)
        time.sleep(0.06)  # blocks the loop: the deadline passes, the timer cannot run
        dispatch.cancel()
        await dispatch

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(run())

    _assert_cancelled_verdict(tracer.spans[0], "cancelled")


def test_a_competing_external_cancel_in_the_same_loop_turn_is_cancelled(monkeypatch) -> None:
    """Both the budget's timer and an external cancel run before the tool
    resumes: the timeout reports expired, but the task carries TWO
    cancellation requests, and asyncio.Timeout then hands the CancelledError
    to the caller instead of raising TimeoutError. The span follows the same
    ownership rule the timeout applies and says cancelled - expired() alone
    proves the timer fired, not that it owns what propagates (codex r2)."""
    tracer = _install_fake_tracer(monkeypatch)

    @observability.trace_mcp_tool("get_snapshot")
    async def slow_snapshot() -> dict:
        await asyncio.sleep(5)
        return {"status": "full"}

    async def run() -> None:
        sink: list = []
        dispatch = asyncio.create_task(_dispatch(slow_snapshot, 0.03, sink))
        await asyncio.sleep(0.01)
        loop = asyncio.get_running_loop()
        loop.call_at(sink[0].timeout.when() - 0.005, dispatch.cancel)
        time.sleep(0.06)  # both callbacks are due and run before the tool resumes
        await dispatch

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(run())

    _assert_cancelled_verdict(tracer.spans[0], "cancelled")


@pytest.mark.parametrize("code", ["deadline_exceeded", "cancelled"])
def test_cancellation_codes_are_allowlisted(code: str) -> None:
    assert code in observability._KNOWN_ERROR_CODES
