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
    mcp.success          - bool: False when the result is an error payload
                           (errors.is_error_payload - an "error" key with no
                           "data" beside it), the tool raised, or the call was
                           cancelled (deadline_exceeded for the dispatch
                           budget, cancelled for anything else)
    mcp.error.code       - the "error" value when it is in the allowlist;
                           otherwise the HTTP status the client recorded,
                           named through a fixed table (upstream_http_429,
                           upstream_http_5xx, ...); otherwise "unknown_error".
                           Free-text upstream messages never reach the span.
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

import asyncio
import contextlib
import functools
import logging
import os
import time
from collections.abc import Awaitable, Callable
from contextvars import ContextVar
from typing import Any, ParamSpec, TypeVar

from .errors import is_error_payload

logger = logging.getLogger("sugra_mcp.observability")

P = ParamSpec("P")
R = TypeVar("R")

_INITIALISED = False
_TRACER: Any | None = None
_VALID_OPERATION_IDS: frozenset[str] | None = None

# The asyncio.Timeout that bounds the running dispatch, published by server.py
# call_tool for the duration of ONE dispatch and reset after it. On a
# CancelledError the wrapper asks it whether it FIRED: attribution comes from
# the timeout's own state, never from a clock. A clock comparison misfiles in
# both directions (codex r1): an external cancel that lands after the deadline
# but before the timer ran reads as the budget, and a coarse loop clock (the
# loop runs a timer up to one clock resolution EARLY, 15.6 ms on Windows)
# fires the budget before loop.time() reaches the deadline, which reads as
# the caller. Set and read inside one dispatch on one task, so it cannot
# inherit across calls the way the MCP-17 stamp did.
dispatch_timeout: ContextVar[asyncio.Timeout | None] = ContextVar(
    "sugra_dispatch_timeout", default=None
)


def _cancellation_code() -> str:
    """deadline_exceeded when the published dispatch timeout fired, else cancelled."""
    timeout = dispatch_timeout.get()
    if timeout is not None and timeout.expired():
        return "deadline_exceeded"
    return "cancelled"

# HTTP failures from the Sugra API. SugraClient keeps the API's own text at
# result["error"] for the CALLER (a bad symbol says which, a quota refusal
# names the plan) and sets result["status_code"] from the response. That text
# can never pass the allowlist, so before MCP-19 every such failure was
# `unknown_error` - 66% of all failures over 90 days, with a caller's 429
# quota, a bad-symbol 404 and a 503 upstream outage indistinguishable. The
# span now names the failure from this fixed table keyed on the STATUS, an
# int the client set and never a caller value. Named entries are the statuses
# the API returns deliberately; anything else lands in its class bucket. The
# table is the only path from a status to a span, and every value in it is a
# constant.
_HTTP_STATUS_ERROR_CODES: dict[int, str] = {
    400: "upstream_http_400",  # a parameter value the router rejected
    401: "upstream_http_401",  # the caller's API key was refused
    403: "upstream_http_403",
    404: "upstream_http_404",  # unknown symbol / id / series
    422: "upstream_http_422",  # request shape rejected by validation
    429: "upstream_http_429",  # the caller's daily quota is exhausted
    500: "upstream_http_500",  # an API defect
    502: "upstream_http_502",  # the API's own upstream provider failed
    503: "upstream_http_503",  # the API's own upstream provider is unavailable
    504: "upstream_http_504",  # the API's own upstream provider timed out
}
_HTTP_CLASS_ERROR_CODES: dict[int, str] = {
    3: "upstream_http_3xx",
    4: "upstream_http_4xx",
    5: "upstream_http_5xx",
}


def _http_status_error_code(status: Any) -> str | None:
    """The span code for an HTTP status, or None when the value cannot be one.

    Entered by type and by range: "503" is a string a client could echo, and
    a float is not a status - neither may reach the table; an int does,
    including an IntEnum such as http.HTTPStatus. bool is a subclass of int,
    but True and False are 1 and 0, so the range test below already excludes
    them (an explicit bool guard was dead code: no test could observe it).
    1xx/2xx carry no failure semantics, so a dict pairing one with an error
    key keeps the residual code.
    """
    if not isinstance(status, int):
        return None
    if status in _HTTP_STATUS_ERROR_CODES:
        return _HTTP_STATUS_ERROR_CODES[status]
    if 300 <= status < 600:
        return _HTTP_CLASS_ERROR_CODES[status // 100]
    return None


# Known MCP-tool error codes. Any string returned at result["error"] that is
# NOT in this set never reaches App Insights: the failure is named by its HTTP
# status when the client recorded one, else as "unknown_error", so free-text
# upstream messages (which can contain PII or query content) stay out.
_KNOWN_ERROR_CODES: frozenset[str] = frozenset({
    "unknown_operation_id",
    "missing_required_parameters",
    "missing_required_parameter_groups",
    "unsupported_method",
    "unresolved_path_parameters",
    "no_endpoint_found",
    # search_endpoints filter validation: an unknown toolset/source value is
    # reported as a typed error naming the valid values, never a silent empty
    # result list (an empty list cannot be told apart from "nothing matched").
    "unknown_toolset",
    "unknown_source",
    "stale_search_result",
    "response_too_large",
    "validation_failed",
    "auth_failed",
    # Transport-layer error contract (SugraClient catches httpx failures and
    # returns these as structured dicts; free text stays in the dict's
    # "reason" field which never reaches spans).
    "upstream_timeout",
    "upstream_connect_error",
    "upstream_transport_error",
    # Gateway safety net for unexpected exceptions inside call_endpoint.
    "tool_execution_failed",
    # MCP-10: the end-to-end per-call budget fired and the call was
    # cancelled server-side (audit P1-4). MCP-19.1: this is what the SPAN
    # says too - the budget cancels the task with a CancelledError, which
    # `except Exception` never saw, so the span used to end with no verdict.
    "deadline_exceeded",
    # MCP-19.1: a cancellation that is not the budget - the client went away
    # or the session shut down mid-call.
    "cancelled",
    # Agent Context Layer plane: infra-level credential rejected (hosted-only
    # tools, tools/agent.py remaps the plane 403 to this distinct code).
    "agent_plane_unavailable",
    # Sugra Entity tools (tools/entities.py): an unsupported anchor, and the
    # _clean_error fallback for a client dict that carries no error value.
    "invalid_anchor",
    "request_failed",
    # stdio without SUGRA_API_KEY (server.py _KeylessClient): every network
    # tool returns this instead of dialling out.
    "missing_api_key",
}) | frozenset(_HTTP_STATUS_ERROR_CODES.values()) | frozenset(_HTTP_CLASS_ERROR_CODES.values())


def _error_code_of(result: dict[str, Any]) -> str:
    """The allowlisted span code for a FAILED tool result.

    A code the tool named itself wins - it is the more specific signal (the
    plane's `agent_plane_unavailable` sits beside a status_code of 403).
    Otherwise the HTTP status the client recorded names the failure through
    the fixed table. Anything else is the residual `unknown_error`: free text
    with no status, or a shape no contract produces. Nothing taken from the
    dict itself is ever attached.
    """
    error_value = result.get("error")
    if isinstance(error_value, str) and error_value in _KNOWN_ERROR_CODES:
        return error_value
    return _http_status_error_code(result.get("status_code")) or "unknown_error"

# azure-monitor-opentelemetry enables all bundled instrumentations by default
# (fastapi, requests, urllib, urllib3, azure_sdk, django, flask, psycopg2).
# Those auto-spans carry URL + query-string attributes that bypass our privacy
# contract. Disable every bundled instrumentation explicitly so the only spans
# we emit are the ones we author here.
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
            # Disable every bundled auto-instrumentation so only our explicit
            # `@trace_mcp_tool` spans reach the workspace.
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


def _safe_status_ok(span: Any) -> None:
    """Set OTel span status to OK on success exit.

    Without an explicit OK, the Azure Monitor exporter leaves the top-level
    App Insights `success` column unset (KQL `avg(toint(success))` returns
    NaN). Setting OK on the success path populates the column so
    aggregations work without parsing `mcp.success` custom dimension.
    """
    with contextlib.suppress(Exception):
        from opentelemetry.trace import Status, StatusCode

        span.set_status(Status(StatusCode.OK))


def _safe_end(span: Any) -> None:
    with contextlib.suppress(Exception):
        span.end()


def trace_mcp_tool(
    tool_name: str,
    result_attrs: Callable[[Any], dict[str, Any]] | None = None,
) -> Callable[[Callable[P, Awaitable[R]]], Callable[P, Awaitable[R]]]:
    """Decorator that wraps an async MCP tool with an OpenTelemetry span.

    The span carries the dimensions documented at the module level. When
    `setup_observability()` was a no-op (env var unset or SDK missing)
    the decorator is a transparent pass-through with zero overhead.

    ``result_attrs`` is an optional extractor called with the tool RESULT on
    the success path; it returns extra span attributes (the extractor owns the
    privacy allowlist - it must derive attributes from response metadata only,
    never from request values). An extractor failure is swallowed: telemetry
    must never break the tool result.

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

                # operation_id is attached ONLY if the kwarg value matches a
                # catalog-known operation_id. Arbitrary client-supplied strings
                # (PII / secrets / free text) are dropped before reaching App
                # Insights.
                operation_id = kwargs.get("operation_id")
                if isinstance(operation_id, str) and operation_id in _get_valid_operation_ids():
                    _safe_attr(span, "mcp.operation_id", operation_id)

                try:
                    result = await func(*args, **kwargs)
                except asyncio.CancelledError:
                    # A BaseException: the dispatch budget (server.py) cancels
                    # the task, and the clause below never saw it, so the span
                    # ended through `finally` with only the tool name - the one
                    # failure that fires when the API is slowest was invisible
                    # to every failure query (MCP-19.1). Stamp the verdict and
                    # re-raise unchanged so the cancellation still propagates.
                    _safe_attr(span, "mcp.success", False)
                    _safe_attr(span, "mcp.error.code", _cancellation_code())
                    _safe_attr(span, "mcp.duration_ms", int((time.perf_counter() - start) * 1000))
                    _safe_status_error(span)
                    raise
                except Exception as e:
                    _safe_attr(span, "mcp.success", False)
                    _safe_attr(span, "mcp.error.code", "exception")
                    _safe_attr(span, "mcp.exception.type", type(e).__name__)
                    _safe_attr(span, "mcp.duration_ms", int((time.perf_counter() - start) * 1000))
                    _safe_status_error(span)
                    raise

                # Returned path. Failure is errors.is_error_payload - the ONE
                # definition the tool protocol (server.py) applies - so a
                # partial envelope pairing an error note with data counts as
                # the success the client received, and a bare error key counts
                # as a failure whatever the type of its value.
                success = not is_error_payload(result)
                error_code: str | None = None if success else _error_code_of(result)
                _safe_attr(span, "mcp.success", success)
                if error_code is not None:
                    _safe_attr(span, "mcp.error.code", error_code)
                if result_attrs is not None and success:
                    try:
                        for key, value in result_attrs(result).items():
                            _safe_attr(span, key, value)
                    except Exception:
                        # Extractor bugs must never break the tool result.
                        pass
                _safe_attr(span, "mcp.duration_ms", int((time.perf_counter() - start) * 1000))
                # Status drives App Insights top-level `success` column;
                # ERROR for tool-reported failures (catalog-mapped error
                # code), OK for clean success.
                if success:
                    _safe_status_ok(span)
                else:
                    _safe_status_error(span)
                return result
            finally:
                _safe_end(span)

        return wrapper

    return decorator
