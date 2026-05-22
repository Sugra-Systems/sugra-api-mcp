"""Optional Azure Application Insights instrumentation for hosted MCP.

Activates when ``APPLICATIONINSIGHTS_CONNECTION_STRING`` is set in the
environment. When unset (stdio mode, local dev, self-hosted without
Azure) the module is a graceful no-op: ``setup_observability()`` returns
False and ``@trace_mcp_tool`` becomes a transparent pass-through.

Custom dimensions captured per MCP tool invocation:
    mcp.tool.name        - one of search_endpoints / describe_endpoint /
                           call_endpoint / fetch_data / list_toolsets /
                           list_sources
    mcp.operation_id     - the operation_id arg (only call_endpoint and
                           describe_endpoint receive one)
    mcp.success          - bool, derived from whether the tool returned
                           an "error" key or raised
    mcp.error.code       - the "error" key value if present, never the
                           free-text message (avoids leaking PII)
    mcp.duration_ms      - integer ms wall-clock from before-call to
                           after-return

Privacy: raw query strings, params dicts, body payloads, and response
payloads are NEVER attached to spans. Tool name + operation_id (both
from the bundled catalog, not user content) + success flag + integer
error code are catalog-derived and safe.
"""

from __future__ import annotations

import functools
import logging
import os
import time
from collections.abc import Awaitable, Callable
from typing import Any, ParamSpec, TypeVar

logger = logging.getLogger("sugra_mcp.observability")

P = ParamSpec("P")
R = TypeVar("R")

_INITIALISED = False
_TRACER: Any | None = None


def setup_observability(connection_string: str | None = None) -> bool:
    """Configure Azure Monitor OpenTelemetry if a connection string is available.

    Returns True when instrumentation is active, False when skipped (env
    var not set or azure-monitor-opentelemetry not installed).
    """
    global _INITIALISED, _TRACER
    if _INITIALISED:
        return _TRACER is not None

    conn = connection_string or os.environ.get("APPLICATIONINSIGHTS_CONNECTION_STRING", "").strip()
    if not conn:
        _INITIALISED = True
        logger.debug(
            "APPLICATIONINSIGHTS_CONNECTION_STRING not set; MCP tool spans disabled."
        )
        return False

    try:
        from azure.monitor.opentelemetry import configure_azure_monitor
        from opentelemetry import trace
    except ImportError as e:
        # azure-monitor-opentelemetry not in the install (e.g. plain stdio
        # install with `pip install sugra-api-mcp`). Don't fail; just disable.
        logger.warning(
            "azure-monitor-opentelemetry not installed; MCP tool spans disabled. "
            "Install with `pip install 'sugra-api-mcp[http]'`. (%s)",
            e,
        )
        _INITIALISED = True
        return False

    try:
        configure_azure_monitor(
            connection_string=conn,
            # Service name shows up as `cloud_RoleName` in App Insights so
            # MCP traces are easy to filter from API traces when both share
            # a workspace.
            resource_attributes={"service.name": "sugra-mcp"},
            # Disable logger handler injection - we already log to journalctl
            # and don't want duplicate ingest. Spans / traces only.
            disable_logging=True,
            disable_metrics=False,
        )
        _TRACER = trace.get_tracer("sugra_mcp.tools")
        _INITIALISED = True
        logger.info("Azure Monitor instrumentation active for sugra-mcp")
        return True
    except Exception as e:
        # Never let observability setup take down the MCP server.
        logger.warning("Azure Monitor configuration failed; spans disabled: %s", e)
        _INITIALISED = True
        return False


def trace_mcp_tool(tool_name: str) -> Callable[[Callable[P, Awaitable[R]]], Callable[P, Awaitable[R]]]:
    """Decorator that wraps an async MCP tool with an OpenTelemetry span.

    The span carries the custom dimensions documented at the module level.
    When `setup_observability()` was a no-op (env var unset or SDK missing)
    the decorator is a transparent pass-through with zero overhead.

    Usage::

        @mcp.tool(annotations=read_only("Search endpoints"))
        @trace_mcp_tool("search_endpoints")
        async def search_endpoints(query: str, ...) -> dict[str, Any]:
            ...
    """

    def decorator(func: Callable[P, Awaitable[R]]) -> Callable[P, Awaitable[R]]:
        @functools.wraps(func)
        async def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
            if _TRACER is None:
                return await func(*args, **kwargs)

            start = time.perf_counter()
            span = _TRACER.start_span(name=f"mcp.tool.{tool_name}")
            span.set_attribute("mcp.tool.name", tool_name)

            # operation_id is the most-useful per-call dimension. Capture it
            # ONLY when passed as a kwarg - never from positional args. This
            # is the safe path because search_endpoints / fetch_data take a
            # query string as the first positional, and we MUST NOT label
            # raw user queries as operation_ids (privacy + correctness).
            # The MCP runtime passes tool arguments as kwargs per JSON-RPC
            # params unpacking, so we never lose real operation_ids here.
            operation_id = kwargs.get("operation_id")
            if isinstance(operation_id, str):
                span.set_attribute("mcp.operation_id", operation_id)

            try:
                result = await func(*args, **kwargs)
            except Exception as e:
                span.set_attribute("mcp.success", False)
                span.set_attribute("mcp.error.code", "exception")
                span.set_attribute("mcp.duration_ms", int((time.perf_counter() - start) * 1000))
                # Record the exception type but NOT the message - message may
                # contain user query content or PII from upstream APIs.
                span.set_attribute("mcp.exception.type", type(e).__name__)
                span.set_status(trace_status_error())
                span.end()
                raise

            # Success path: inspect the returned dict for "error" key without
            # surfacing the full payload. Keep dimensions catalog-only.
            success = True
            error_code: str | None = None
            if isinstance(result, dict):
                error_value = result.get("error")
                if isinstance(error_value, str):
                    success = False
                    error_code = error_value
            span.set_attribute("mcp.success", success)
            if error_code is not None:
                span.set_attribute("mcp.error.code", error_code)
            span.set_attribute("mcp.duration_ms", int((time.perf_counter() - start) * 1000))
            span.end()
            return result

        return wrapper

    return decorator


def trace_status_error():
    """Return an OpenTelemetry StatusCode.ERROR, importing lazily so the
    module imports without azure-monitor-opentelemetry installed.
    """
    from opentelemetry.trace import Status, StatusCode

    return Status(StatusCode.ERROR)
