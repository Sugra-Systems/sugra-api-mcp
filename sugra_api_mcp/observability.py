"""Optional Azure Application Insights instrumentation for hosted MCP.

Activates when ``APPLICATIONINSIGHTS_CONNECTION_STRING`` is set in the
environment. When unset (stdio mode, local dev, self-hosted without
Azure) the module is a graceful no-op: ``setup_observability()`` returns
False and ``@trace_mcp_tool`` becomes a transparent pass-through.

Custom dimensions captured per MCP tool invocation:
    mcp.tool.name        - one of search_endpoints / describe_endpoint /
                           call_endpoint / fetch_data / list_toolsets /
                           list_sources
    mcp.operation_id     - the operation_id kwarg, ONLY if it matches a
                           catalog-known operation_id (allowlist). Arbitrary
                           client-supplied strings (PII, secrets, free text)
                           are dropped.
    mcp.success          - bool, derived from whether the tool returned
                           an "error" key or raised
    mcp.error.code       - the "error" key value if present AND if it
                           matches the known error-code allowlist. Free-text
                           upstream error messages are mapped to
                           "unknown_error" so they cannot reach the span.
    mcp.duration_ms      - integer ms wall-clock from before-call to
                           after-return
    mcp.exception.type   - exception class name only (NEVER the message)

Privacy contract (enforced by tests):
- Raw query strings, params dicts, body payloads, response payloads are
  NEVER attached to spans.
- Exception messages are NEVER attached (only the class name).
- operation_id and error_code are validated against catalog/whitelist
  allowlists before attachment.
- All bundled OpenTelemetry instrumentations (fastapi, requests, urllib,
  urllib3, azure_sdk, etc.) are explicitly DISABLED to prevent them from
  emitting auto-spans with URL/query attributes outside this allowlist.
- All span operations are wrapped in try/except; a telemetry failure
  cannot mask a tool result or leave a span un-ended.
"""

from __future__ import annotations

import contextlib
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
_VALID_OPERATION_IDS: frozenset[str] | None = None

# Known MCP-tool error codes. Any string returned at result["error"] that is
# NOT in this set is mapped to "unknown_error" so free-text upstream messages
# (which can contain PII or query content) cannot reach App Insights.
_KNOWN_ERROR_CODES: frozenset[str] = frozenset({
    "unknown_operation_id",
    "missing_required_parameters",
    "unsupported_method",
    "unresolved_path_parameters",
    "no_endpoint_found",
    "stale_search_result",
    "response_too_large",
    "validation_failed",
    "auth_failed",
})

# Codex Round 1 Critical #1: azure-monitor-opentelemetry enables all bundled
# instrumentations by default (fastapi, requests, urllib, urllib3, azure_sdk,
# django, flask, psycopg2). Those auto-spans carry URL + query-string
# attributes that bypass our privacy contract. Disable every bundled
# instrumentation explicitly so the only spans we emit are the ones we
# author here.
_DISABLE_ALL_INSTRUMENTATION = {
    "azure_sdk": {"enabled": False},
    "django": {"enabled": False},
    "fastapi": {"enabled": False},
    "flask": {"enabled": False},
    "psycopg2": {"enabled": False},
    "requests": {"enabled": False},
    "urllib": {"enabled": False},
    "urllib3": {"enabled": False},
}


def setup_observability(connection_string: str | None = None) -> bool:
    """Configure Azure Monitor OpenTelemetry if a connection string is available.

    Returns True when instrumentation is active, False when skipped (env
    var not set or azure-monitor-opentelemetry not installed). Never logs
    exception messages (only class names) so setup failures cannot leak
    text through any subsequently-attached log exporter.
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
        # Log the exception class only - never str(e), to keep any future
        # log-export path from carrying free text.
        logger.warning(
            "azure-monitor-opentelemetry not installed (%s); MCP tool spans disabled. "
            "Install with `pip install 'sugra-api-mcp[http]'`.",
            type(e).__name__,
        )
        _INITIALISED = True
        return False

    try:
        # cloud_RoleName for filtering in App Insights when API + MCP
        # share a workspace. The azure-monitor-opentelemetry SDK accepts
        # **kwargs and silently ignores unrecognised keys (e.g. a plain
        # `resource_attributes` dict), so we set the canonical OTel env
        # var BEFORE calling configure_azure_monitor — the SDK reads it
        # via Resource auto-detector. setdefault preserves any operator
        # override in /etc/systemd unit or .env.
        os.environ.setdefault("OTEL_SERVICE_NAME", "sugra-mcp")

        configure_azure_monitor(
            connection_string=conn,
            # No automatic log handler injection - we log to journalctl
            # only and do NOT want any exception messages exported as
            # traces (privacy contract).
            disable_logging=True,
            disable_metrics=False,
            # Codex CRIT-1: disable every bundled auto-instrumentation so
            # only our explicit `@trace_mcp_tool` spans reach the workspace.
            instrumentation_options=_DISABLE_ALL_INSTRUMENTATION,
        )
        _TRACER = trace.get_tracer("sugra_mcp.tools")
        _INITIALISED = True
        logger.info("Azure Monitor instrumentation active for sugra-mcp")
        return True
    except Exception as e:
        # Never let observability setup take down the MCP server. Log class
        # only, not the message.
        logger.warning(
            "Azure Monitor configuration failed (%s); spans disabled.",
            type(e).__name__,
        )
        _INITIALISED = True
        return False


def _get_valid_operation_ids() -> frozenset[str]:
    """Lazy-init frozen set of catalog operation_ids for kwarg validation.

    Used by the decorator to allowlist `operation_id` kwargs before
    setting them as span attributes - prevents clients from labeling
    spans with arbitrary strings (PII, secrets, free-text).
    """
    global _VALID_OPERATION_IDS
    if _VALID_OPERATION_IDS is None:
        try:
            from .catalog.loader import load_catalog

            _VALID_OPERATION_IDS = frozenset(e.operation_id for e in load_catalog().endpoints)
        except Exception:
            # If catalog load fails (e.g. broken install), bail to empty
            # set rather than crash. operation_id will simply not be
            # attached to spans until catalog loads next.
            _VALID_OPERATION_IDS = frozenset()
    return _VALID_OPERATION_IDS


def _safe_attr(span: Any, key: str, value: Any) -> None:
    """Set a span attribute, silencing any exporter / SDK failure.

    Telemetry must never mask a tool result or original exception.
    """
    with contextlib.suppress(Exception):
        span.set_attribute(key, value)


def _safe_status_error(span: Any) -> None:
    with contextlib.suppress(Exception):
        from opentelemetry.trace import Status, StatusCode

        span.set_status(Status(StatusCode.ERROR))


def _safe_end(span: Any) -> None:
    with contextlib.suppress(Exception):
        span.end()


def trace_mcp_tool(tool_name: str) -> Callable[[Callable[P, Awaitable[R]]], Callable[P, Awaitable[R]]]:
    """Decorator that wraps an async MCP tool with an OpenTelemetry span.

    The span carries the dimensions documented at the module level. When
    `setup_observability()` was a no-op (env var unset or SDK missing)
    the decorator is a transparent pass-through with zero overhead.

    Failure-safety guarantees:
    - If span creation fails, the tool still runs.
    - If any set_attribute / set_status / end call fails, the tool result
      is preserved and the original exception (if any) is re-raised
      unchanged.
    - span.end() is guaranteed via try/finally.

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
            span: Any = None
            try:
                span = _TRACER.start_span(name=f"mcp.tool.{tool_name}")
            except Exception:
                # Span creation itself failed - run the tool without
                # telemetry rather than masking the call.
                return await func(*args, **kwargs)

            try:
                _safe_attr(span, "mcp.tool.name", tool_name)

                # Codex CRIT-2: operation_id is attached ONLY if the kwarg
                # value matches a catalog-known operation_id. Arbitrary
                # client-supplied strings (PII / secrets / free text) are
                # dropped before reaching App Insights.
                operation_id = kwargs.get("operation_id")
                if isinstance(operation_id, str) and operation_id in _get_valid_operation_ids():
                    _safe_attr(span, "mcp.operation_id", operation_id)

                try:
                    result = await func(*args, **kwargs)
                except Exception as e:
                    _safe_attr(span, "mcp.success", False)
                    _safe_attr(span, "mcp.error.code", "exception")
                    _safe_attr(span, "mcp.exception.type", type(e).__name__)
                    _safe_attr(span, "mcp.duration_ms", int((time.perf_counter() - start) * 1000))
                    _safe_status_error(span)
                    raise

                # Success path: catalog-bounded error code allowlist.
                success = True
                error_code: str | None = None
                if isinstance(result, dict):
                    error_value = result.get("error")
                    if isinstance(error_value, str):
                        success = False
                        # Codex CRIT-3: map unknown error strings to a
                        # constant so free-text upstream messages cannot
                        # reach the span attribute.
                        error_code = (
                            error_value if error_value in _KNOWN_ERROR_CODES else "unknown_error"
                        )
                _safe_attr(span, "mcp.success", success)
                if error_code is not None:
                    _safe_attr(span, "mcp.error.code", error_code)
                _safe_attr(span, "mcp.duration_ms", int((time.perf_counter() - start) * 1000))
                return result
            finally:
                _safe_end(span)

        return wrapper

    return decorator
