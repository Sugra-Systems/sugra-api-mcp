"""No auto-instrumented HTTP client span leaves the process.

azure-monitor-opentelemetry switches on every instrumentation it bundles
unless the caller names it, and from distro 1.8.10 that includes ``httpx`` and
``httpx2``: a client span per outgoing call, named after the request and
carrying the full URL. Every call this server makes goes through httpx, so the
list in ``observability`` has to name every library the installed distro can
switch on, and the spans the server writes itself have to stay.

The tests that need the distro (or its httpx instrumentor) skip where it is not
installed; CI installs the ``http`` extra unpinned, so a library a newer distro
adds fails the coverage test there instead of reaching the exporter.
"""

from __future__ import annotations

import ast
import asyncio
import importlib
import importlib.metadata
import sys
import types
from typing import Any

import httpx
import pytest

from sugra_api_mcp import observability
from sugra_api_mcp.client import SugraClient
from sugra_api_mcp.config import Config

_API_BASE = "https://api.test"
_PATH = "/api/v1/entities/lookup"
_SENTINEL_QUERY = {"lei": "SENTINEL0LEI0000000042"}

_DISTRO = "azure-monitor-opentelemetry"
_CONSTANTS = "azure/monitor/opentelemetry/_constants.py"


@pytest.fixture(autouse=True)
def reset_observability_module():
    importlib.reload(observability)
    yield
    importlib.reload(observability)


def _import_or_skip(name: str) -> Any:
    """Import ``name`` or skip the test, whatever way the import fails to work."""
    try:
        return importlib.import_module(name)
    except ImportError as exc:
        pytest.skip(f"cannot import {name}: {type(exc).__name__}")


def _distro_supported_libraries() -> set[str]:
    """Library names the installed distro can instrument, read from its source.

    Read as text rather than imported: the constants live in a package whose
    ``__init__`` pulls in the SDK, and a coverage check must not depend on that
    import succeeding.
    """
    try:
        dist = importlib.metadata.distribution(_DISTRO)
    except importlib.metadata.PackageNotFoundError:
        pytest.skip(f"{_DISTRO} is not installed")
    tree = ast.parse(dist.locate_file(_CONSTANTS).read_text(encoding="utf-8"))
    namespace: dict[str, Any] = {}
    for node in tree.body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            target = node.targets[0]
            if isinstance(target, ast.Name):
                try:
                    code = compile(ast.Expression(node.value), _CONSTANTS, "eval")
                    namespace[target.id] = eval(code, {}, namespace)
                except Exception:
                    continue
    libraries = namespace.get("_ALL_SUPPORTED_INSTRUMENTED_LIBRARIES")
    assert libraries, (
        "the distro no longer defines _ALL_SUPPORTED_INSTRUMENTED_LIBRARIES; "
        "find where it lists the libraries it can instrument and update this helper"
    )
    return set(libraries)


def test_httpx_and_httpx2_are_named_and_disabled() -> None:
    options = observability._DISABLE_ALL_INSTRUMENTATION
    assert options["httpx"] == {"enabled": False}
    assert options["httpx2"] == {"enabled": False}


def test_every_bundled_instrumentation_is_disabled() -> None:
    """A library the distro can switch on and this list does not name is exported."""
    options = observability._DISABLE_ALL_INSTRUMENTATION
    supported = _distro_supported_libraries()
    assert supported <= set(options), (
        f"distro can instrument {sorted(supported - set(options))} "
        "and observability._DISABLE_ALL_INSTRUMENTATION does not disable it"
    )
    for name, value in options.items():
        assert value == {"enabled": False}, name


def test_setup_hands_the_disable_list_to_the_distro(monkeypatch) -> None:
    monkeypatch.setenv(
        "APPLICATIONINSIGHTS_CONNECTION_STRING",
        "InstrumentationKey=00000000-0000-0000-0000-000000000000",
    )
    # setup_observability writes these into os.environ; record them first so
    # monkeypatch puts them back as the process had them.
    for name in ("OTEL_SERVICE_NAME", "OTEL_RESOURCE_ATTRIBUTES"):
        monkeypatch.setenv(name, "")
        monkeypatch.delenv(name)
    captured: dict[str, Any] = {}

    def _fake_configure(**kwargs: Any) -> None:
        captured.update(kwargs)

    fake_module = types.ModuleType("azure.monitor.opentelemetry")
    fake_module.configure_azure_monitor = _fake_configure
    monkeypatch.setitem(sys.modules, "azure.monitor.opentelemetry", fake_module)

    assert observability.setup_observability() is True
    assert captured["instrumentation_options"] is observability._DISABLE_ALL_INSTRUMENTATION
    assert captured["instrumentation_options"]["httpx"] == {"enabled": False}
    assert captured["instrumentation_options"]["httpx2"] == {"enabled": False}


@pytest.mark.parametrize(
    "env_value",
    [None, "", "httpx", "httpx2", "requests,urllib3", " httpx , httpx2 ", "none", "all"],
)
def test_environment_cannot_switch_an_instrumentation_back_on(monkeypatch, env_value) -> None:
    """The distro merges our explicit options over what the environment says.

    OTEL_PYTHON_DISABLED_INSTRUMENTATIONS only ever switches more off, and the
    distro has no variable that switches one on, so whatever an operator sets
    the merged result for every library stays disabled.
    """
    configurations = _import_or_skip("azure.monitor.opentelemetry._utils.configurations")
    if env_value is None:
        monkeypatch.delenv("OTEL_PYTHON_DISABLED_INSTRUMENTATIONS", raising=False)
    else:
        monkeypatch.setenv("OTEL_PYTHON_DISABLED_INSTRUMENTATIONS", env_value)

    merged: dict[str, Any] = {
        "instrumentation_options": {
            name: dict(value) for name, value in observability._DISABLE_ALL_INSTRUMENTATION.items()
        }
    }
    configurations._default_instrumentation_options(merged)

    effective = merged["instrumentation_options"]
    assert effective, "the distro returned no instrumentation options"
    for name, value in effective.items():
        assert value["enabled"] is False, name
        assert not configurations._is_instrumentation_enabled(merged, name), name


class _OfflinePool:
    """Stands in for the connection pool: answers without opening a socket."""

    async def handle_async_request(self, request: Any) -> Any:
        import httpcore

        return httpcore.Response(
            200, headers=[(b"content-type", b"application/json")], content=b'{"data": []}'
        )

    async def aclose(self) -> None:
        return None


def _offline_transport() -> httpx.AsyncHTTPTransport:
    """The transport class a real call uses, with no network behind it.

    httpx's instrumentation hooks ``AsyncHTTPTransport.handle_async_request``,
    which a ``MockTransport`` never reaches; only this keeps the call on the
    instrumented path.
    """
    transport = httpx.AsyncHTTPTransport()
    transport._pool = _OfflinePool()  # type: ignore[assignment]
    return transport


def _run_one_client_call_and_one_tool_span(monkeypatch, options: dict[str, Any]):
    """Start the distro's instrumentation with ``options`` and make one call.

    Returns the finished spans: one outgoing API call through ``SugraClient``
    (the path every tool takes) and one span written by the server itself.
    """
    trace = _import_or_skip("opentelemetry.trace")
    sdk_trace = _import_or_skip("opentelemetry.sdk.trace")
    export = _import_or_skip("opentelemetry.sdk.trace.export")
    memory = _import_or_skip("opentelemetry.sdk.trace.export.in_memory_span_exporter")
    httpx_instrumentation = _import_or_skip("opentelemetry.instrumentation.httpx")
    distro_configure = _import_or_skip("azure.monitor.opentelemetry._configure")
    configurations = _import_or_skip("azure.monitor.opentelemetry._utils.configurations")

    exporter = memory.InMemorySpanExporter()
    provider = sdk_trace.TracerProvider()
    provider.add_span_processor(export.SimpleSpanProcessor(exporter))
    # The instrumentor asks the global provider; point that at ours for this test
    # only (the global can be set once per process, patching the slot cannot).
    monkeypatch.setattr(trace, "_TRACER_PROVIDER", provider)

    configuration: dict[str, Any] = {"instrumentation_options": options}
    configurations._default_instrumentation_options(configuration)
    instrumentor = httpx_instrumentation.HTTPXClientInstrumentor()
    try:
        distro_configure._setup_instrumentations(configuration)

        async def scenario() -> None:
            config = Config(api_base=_API_BASE, api_key="test-key", timeout=5)
            client = SugraClient(config, transport=_offline_transport())
            try:
                result = await client.get(_PATH, params=_SENTINEL_QUERY)
            finally:
                await client.aclose()
            assert "error" not in result

            monkeypatch.setattr(observability, "_TRACER", provider.get_tracer("sugra_mcp.tools"))

            @observability.trace_mcp_tool("search_endpoints")
            async def tool() -> dict[str, Any]:
                return {"results": []}

            await tool()

        asyncio.run(scenario())
    finally:
        instrumentor.uninstrument()
    return exporter.get_finished_spans()


def _only_httpx_enabled() -> dict[str, Any]:
    options = {
        name: dict(value) for name, value in observability._DISABLE_ALL_INSTRUMENTATION.items()
    }
    options["httpx"] = {"enabled": True}
    return options


def test_a_client_call_exports_no_span_and_the_servers_own_span_stays(monkeypatch) -> None:
    spans = _run_one_client_call_and_one_tool_span(
        monkeypatch,
        {
            name: dict(value)
            for name, value in observability._DISABLE_ALL_INSTRUMENTATION.items()
        },
    )

    assert [s.name for s in spans] == ["mcp.tool.search_endpoints"]
    for span in spans:
        for key, value in (span.attributes or {}).items():
            assert _API_BASE not in str(value), key
            assert _PATH not in str(value), key
            assert "SENTINEL0LEI" not in str(value), key


def test_the_check_would_notice_a_client_span(monkeypatch) -> None:
    """Positive control: with httpx left on, the same call does export a span.

    Without this the test above would pass just as well if the call never
    produced a client span in the first place.
    """
    spans = _run_one_client_call_and_one_tool_span(monkeypatch, _only_httpx_enabled())

    client_spans = [s for s in spans if s.name != "mcp.tool.search_endpoints"]
    assert client_spans, "httpx instrumentation produced no span; the check above proves nothing"
    assert any(_PATH in str(v) for s in client_spans for v in (s.attributes or {}).values())
