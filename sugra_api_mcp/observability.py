"""Optional Azure Application Insights instrumentation for hosted MCP.

Activates when ``APPLICATIONINSIGHTS_CONNECTION_STRING`` is set in the
environment. When unset (stdio mode, local dev, self-hosted without
Azure) the module is a graceful no-op: ``setup_observability()`` returns
False and ``@trace_mcp_tool`` becomes a transparent pass-through.

Custom dimensions captured per MCP tool invocation:
    mcp.tool.name        - the traced tool's registered name: the six gateway
                           tools (tools/gateway.py), sugra_entity_screen and
                           sugra_entity_lookup, and on the hosted server
                           resolve_entity, get_snapshot and get_timeseries.
                           A call naming no registered tool leaves a span
                           named mcp.tool.unknown with no mcp.tool.name at all
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
                           A tool that raised is "exception"; a cancelled call
                           is "deadline_exceeded" or "cancelled". A call that
                           never reached its tool is "unknown_tool" or
                           "invalid_arguments".
    mcp.busy.scope       - server_busy failures only: the bound
                           that refused the call, one of tool_calls /
                           caller_tool_calls / search / caller_search; any
                           other value is dropped
    mcp.caller.*         - how the call arrived, read from the
                           request that carried it and its session: transport,
                           auth (api_key / oauth / none / local), host, ua_class
                           and origin (HTTP only), client and client_version
                           (the clientInfo name as a class, and its version, as
                           the session's most recent initialize asserted them:
                           any re-initialize of the session replaces both).
                           Also session (16 hex of the
                           SHA-256 of Mcp-Session-Id, never the id) and net
                           (IPv4 /24, IPv6 /48, loopback or private; only when
                           the ASGI peer equals X-Real-IP, never a raw address).
                           Each value is a fixed class, a digest, a prefix or a
                           plain dotted version, never header, clientInfo or
                           address text. A call no HTTP request carried is
                           transport and auth local. For OAuth only, platform
                           carries the APP-verified connector class
                           (openai, anthropic, cursor, google, xai, custom).
                           Missing or unknown is omitted, never guessed
    enduser.pseudo.id    - the Azure user_Id column: the same
                           name admission uses (http: plus 16 hex of SHA-256
                           of the resolved API key, http:anonymous, or local).
                           Never the key
    enduser.id           - the Azure user_AuthenticatedId column:
                           the numeric OAuth user id as a decimal string, OAuth
                           callers only. Absent for a sugra_ key
    mcp.duration_ms      - integer ms wall-clock from before-call to
                           after-return
    mcp.api.requests     - integer count of the Sugra API requests the call
                           made, nested tool calls included
    mcp.side             - the deployment that served the call: the Container
                           Apps revision (CONTAINER_APP_REVISION), or "vm"
                           where the platform sets none
    mcp.request.id       - the X-Request-Id of the HTTP request that carried
                           the call, only when it is exactly 32 lowercase hex
                           (the id nginx assigns); anything else is dropped
    mcp.exception.type   - exception class name only (NEVER the message)
    mcp.agent.*          - agent tools only, from the response envelope
                           metadata (tools/agent.py _agent_result_attrs)

Which calls leave a span:
- A call that reached its tool, OAuth, unauthenticated or local, whatever
  the outcome.
- A call authenticated by a sugra_ API key only once it reached its first
  Sugra API request; the span then opens at the call's exit, backdated to
  its entry. Its catalog lookups and its failures before any API request
  leave no span: the gate summary (gate.py) counts them instead.
- A call that never reached its tool (unknown_tool, invalid_arguments) on
  the same terms: every caller except a sugra_ API key.
- A call refused at admission (server_busy): record_refused_call, for
  registered names only, whatever the caller.
Sampling is off: every span is exported.

Privacy contract (enforced by tests):
- Raw query strings, params dicts, body payloads, response payloads are
  NEVER attached to spans.
- Exception messages are NEVER attached (only the class name).
- operation_id and error_code are validated against catalog/whitelist
  allowlists before attachment.
- API keys, tokens, JWT claims other than the numeric OAuth sub, raw
  addresses, raw Host / User-Agent / Origin / clientInfo / session id
  NEVER reach a span. Identity is a digest, a decimal user id, a session
  digest or a coarse network prefix.
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
import hashlib
import ipaddress
import logging
import os
import re
import sys
import threading
import time
from collections.abc import Awaitable, Callable
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any, ParamSpec, TypeVar

from . import __version__
from .errors import is_error_payload

logger = logging.getLogger("sugra_mcp.observability")

P = ParamSpec("P")
R = TypeVar("R")

_INITIALISED = False
_TRACER: Any | None = None
_VALID_OPERATION_IDS: frozenset[str] | None = None

@dataclass(frozen=True)
class DispatchBudget:
    """What server.py call_tool publishes for the duration of ONE dispatch: the
    asyncio.Timeout that bounds it and the task's cancellation count at entry.

    On a CancelledError the wrapper asks this whether the budget OWNS the
    cancellation. Attribution comes from the timeout's own state, never from
    a clock: a clock comparison misfiles in both directions - an
    external cancel that lands after the deadline but before the timer ran
    reads as the budget, and a coarse loop clock (the loop runs a timer up to
    one clock resolution EARLY, 15.6 ms on Windows) fires the budget before
    loop.time() reaches the deadline, which reads as the caller. Set and read
    inside one dispatch on one task, so it cannot inherit across calls the
    way the ambient stamp did.
    """

    timeout: asyncio.Timeout
    cancelling_at_entry: int

    def owns_the_cancellation(self) -> bool:
        """True when the budget fired AND no competing cancellation arrived.

        asyncio.Timeout decides the same way at exit: after one uncancel()
        the task's count must be back at its entry value for the timeout to
        raise TimeoutError. One more outstanding request means an external
        cancel landed in the same loop turn; the CancelledError then reaches
        the caller instead of the envelope, so the span says cancelled too
        expired() alone proves the timer fired, not that it owns
        what is propagating.
        """
        if not self.timeout.expired():
            return False
        task = asyncio.current_task()
        return task is not None and task.cancelling() <= self.cancelling_at_entry + 1


dispatch_budget: ContextVar[DispatchBudget | None] = ContextVar("sugra_dispatch_budget", default=None)


def _cancellation_code() -> str:
    """deadline_exceeded when the published budget owns the cancellation, else cancelled."""
    budget = dispatch_budget.get()
    if budget is not None and budget.owns_the_cancellation():
        return "deadline_exceeded"
    return "cancelled"


@dataclass
class ToolDispatch:
    """What server.py call_tool publishes for ONE tools/call, for the wrapper to fill in.

    entered turns True once the call reached a traced tool, so a ToolError the
    SDK raises while it is still False means the call never started: a name
    no tool is registered under, or arguments that failed validation.
    api_requests counts the Sugra API requests the whole call made, nested
    tool calls included, for the gate summary (gate.py).
    """

    entered: bool = False
    api_requests: int = 0


tool_dispatch: ContextVar[ToolDispatch | None] = ContextVar("sugra_tool_dispatch", default=None)


class _CallFrame:
    """One traced tool invocation's count of Sugra API requests.

    note_api_request adds to every frame up the parent chain, so a tool that
    calls another traced tool (fetch_data delegates to call_endpoint) counts
    the nested call's requests as its own as well.
    """

    __slots__ = ("api_requests", "parent")

    def __init__(self, parent: _CallFrame | None) -> None:
        self.api_requests = 0
        self.parent = parent


_call_frame: ContextVar[_CallFrame | None] = ContextVar("sugra_call_frame", default=None)


def note_api_request() -> None:
    """Count one Sugra API request for the tool call being dispatched.

    server.get_client calls this each time it hands out a client that sends
    one (never the keyless stand-in, which answers without a request). The
    count is the span's mcp.api.requests, decides whether a sugra_ key call
    leaves a span at all, and is the per-call count of the gate summary.
    """
    dispatch = tool_dispatch.get()
    if dispatch is not None:
        dispatch.api_requests += 1
    frame = _call_frame.get()
    while frame is not None:
        frame.api_requests += 1
        frame = frame.parent


# nginx's $request_id: 32 lowercase hex. Anything else a request carries as
# X-Request-Id is dropped, never trimmed or copied through.
_REQUEST_ID_RE = re.compile(r"[0-9a-f]{32}")


def request_id_of(value: object) -> str | None:
    """The request id when the value is exactly 32 lowercase hex, else None."""
    if type(value) is not str:
        return None
    return value if _REQUEST_ID_RE.fullmatch(value) else None


# A Container Apps revision name: lowercase letters, digits and hyphens.
_SIDE_RE = re.compile(r"[a-z0-9][a-z0-9-]{0,127}")


def process_side() -> str:
    """The deployment this process belongs to: its Container Apps revision, or "vm".

    Azure Container Apps sets CONTAINER_APP_REVISION in every replica; the VM
    sets nothing, so an unset or empty value is "vm". A value that is not a
    plain revision name is "unknown", never copied through.
    """
    value = os.environ.get("CONTAINER_APP_REVISION", "").strip()
    if not value:
        return "vm"
    return value if _SIDE_RE.fullmatch(value) else "unknown"


# HTTP failures from the Sugra API. SugraClient keeps the API's own text at
# result["error"] for the CALLER (a bad symbol says which, a quota refusal
# names the plan) and sets result["status_code"] from the response. That text
# can never pass the allowlist, so every such failure used to be
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
    # call_endpoint refuses a params key the operation does not declare,
    # because the API would drop it silently and serve unfiltered data.
    "unknown_parameters",
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
    # The end-to-end per-call budget fired and the call was
    # cancelled server-side. This is what the SPAN
    # says too - the budget cancels the task with a CancelledError, which
    # `except Exception` never saw, so the span used to end with no verdict.
    "deadline_exceeded",
    # A cancellation that is not the budget - the client went away
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
    # A search query over the search bounds, refused before any
    # scoring (catalog/search.py query_limit_error).
    "query_too_long",
    # A tool call refused at the in-flight cap (server.py), or a
    # search refused because the search queue is full (tools/gateway.py).
    "server_busy",
    # A tool call that never reached its tool (server.py call_tool): a name
    # no tool is registered under, or arguments that failed validation.
    "unknown_tool",
    "invalid_arguments",
}) | frozenset(_HTTP_STATUS_ERROR_CODES.values()) | frozenset(_HTTP_CLASS_ERROR_CODES.values())
# Identity map so a str subclass that equals an allowlisted code attaches the
# interned constant, never the caller's object.
_KNOWN_ERROR_CODE_OF: dict[str, str] = {code: code for code in _KNOWN_ERROR_CODES}


def _error_code_of(result: dict[str, Any]) -> str:
    """The allowlisted span code for a FAILED tool result.

    A code the tool named itself wins - it is the more specific signal (the
    plane's `agent_plane_unavailable` sits beside a status_code of 403).
    The attached value is the interned constant from `_KNOWN_ERROR_CODES`, not
    the object in the result, so a str subclass cannot reach the span.
    Otherwise the HTTP status the client recorded names the failure through
    the fixed table. Anything else is the residual `unknown_error`: free text
    with no status, or a shape no contract produces. Nothing taken from the
    dict itself is ever attached.
    """
    error_value = result.get("error")
    if isinstance(error_value, str):
        known = _KNOWN_ERROR_CODE_OF.get(error_value)
        if known is not None:
            return known
    return _http_status_error_code(result.get("status_code")) or "unknown_error"


# The bound that refused a server_busy call, exactly as
# errors.server_busy_error names it. Two of the four are one caller's share
# (the name current_caller returns: http:<digest> of one key, http:anonymous
# for every unauthenticated HTTP request together, or local for everything
# off HTTP), so without the scope a span cannot tell one such caller held to
# its share from the process at its limit. Other callers are served only
# while the process-wide bound has room. Any other value is dropped, never
# mapped to a placeholder.
_BUSY_SCOPES: frozenset[str] = frozenset({"tool_calls", "caller_tool_calls", "search", "caller_search"})


def _busy_scope_of(error_code: str | None, scope: object) -> str | None:
    """The `mcp.busy.scope` value for a failure, or None when it has none.

    Only a `server_busy` failure has one, and only an exact `str` from the fixed
    set is kept. Both types are checked before any comparison or membership test,
    so a value that defines its own comparison or hash never raises into the tool
    result, and a str subclass never reaches the span.
    """
    if type(error_code) is not str or error_code != "server_busy" or type(scope) is not str:
        return None
    return scope if scope in _BUSY_SCOPES else None


def _payload_scope(result: Any) -> object:
    """The "scope" a returned payload carries, or None when reading it raises.

    The payload is the tool's own result, and a mapping whose lookup raises must
    still reach the caller unchanged: telemetry never breaks the tool result.
    """
    try:
        return result.get("scope")
    except Exception:
        return None


@dataclass(frozen=True)
class CallerFacts:
    """Raw facts about a tool call's caller, as server.current_caller_facts reads them.

    transport and auth are set by our own code. caller is the
    admission name from current_caller (already a digest). user_id is the
    numeric OAuth sub, or None. Every other field is whatever the client sent
    (header, clientInfo text, ASGI peer, or None), and none of it reaches a
    span as such: _caller_attrs reduces each to a fixed class, digest or prefix.
    request_id, the X-Request-Id header, is attached only when request_id_of
    accepts it as the exact 32 lowercase hex that nginx assigns.
    """

    transport: str
    auth: str
    host: object = None
    user_agent: object = None
    origin: object = None
    client_name: object = None
    client_version: object = None
    caller: object = None
    user_id: object = None
    session_id: object = None
    client_addr: object = None
    x_real_ip: object = None
    platform: object = None
    request_id: object = None


_CALLER_TRANSPORTS: frozenset[str] = frozenset({"streamable_http", "local"})
_CALLER_AUTH_METHODS: frozenset[str] = frozenset({"api_key", "oauth", "none", "local"})
_CALLER_PLATFORMS: frozenset[str] = frozenset({
    "openai", "anthropic", "cursor", "google", "xai", "custom",
})
_CALLER_HOSTS: frozenset[str] = frozenset({"app.sugra.ai", "mcp.sugra.ai"})
_LOOPBACK_HOSTS: frozenset[str] = frozenset({"localhost", "127.0.0.1", "[::1]"})
_CALLER_TEXT_MAX = 500

# The API's inbound-client classes (usage_clients.py), same patterns in
# the same order, so an MCP span and the API usage mix name a client alike.
# Order is load-bearing: our own consoles first, named agents before Mozilla.
_UA_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("playground", re.compile(r"sugra-playground", re.I)),
    ("mcp", re.compile(r"sugra-api-mcp", re.I)),
    ("claude", re.compile(r"claude-user|claude-web|anthropic|claude\.ai", re.I)),
    ("chatgpt", re.compile(r"chatgpt-user|chatgpt", re.I)),
    ("grok", re.compile(r"grok-agent|\bxai\b", re.I)),
    ("cursor", re.compile(r"\bcursor\b", re.I)),
    ("openbb", re.compile(r"openbb", re.I)),
    ("python", re.compile(r"python-requests|python-httpx|aiohttp|httpx/", re.I)),
    ("curl", re.compile(r"\bcurl/|\bwget/|httpie/", re.I)),
    ("node", re.compile(r"\baxios/|node-fetch|\bundici\b|node/|(?:^|[\s;(])node(?:$|[\s;)])", re.I)),
    ("browser", re.compile(r"mozilla/|chrome/|safari/|firefox/|\bedg/", re.I)),
)

# The same classes for the initialize clientInfo name, which names a product
# rather than an HTTP library (for example claude-ai or openai-mcp).
_CLIENT_NAME_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("playground", re.compile(r"sugra-playground", re.I)),
    ("mcp", re.compile(r"sugra-api-mcp", re.I)),
    ("claude", re.compile(r"claude|anthropic", re.I)),
    ("chatgpt", re.compile(r"chatgpt|openai", re.I)),
    ("grok", re.compile(r"grok|\bxai\b", re.I)),
    ("cursor", re.compile(r"cursor", re.I)),
    ("openbb", re.compile(r"openbb", re.I)),
)

# The connector origins config.DEFAULT_ALLOWED_ORIGINS admits, by vendor.
_ORIGIN_CLASSES: dict[str, str] = {
    "https://chatgpt.com": "openai",
    "https://chat.openai.com": "openai",
    "https://platform.openai.com": "openai",
    "https://claude.ai": "anthropic",
    "https://claude.com": "anthropic",
    "https://cursor.sh": "cursor",
    "https://app.cursor.sh": "cursor",
}

_VERSION_MAX = 23
_VERSION_RE = re.compile(r"[0-9]{1,5}(?:\.[0-9]{1,5}){0,3}")
_PSEUDO_ID_RE = re.compile(r"(?:http:[0-9a-f]{16}|http:anonymous|local)")
_AUTH_USER_RE = re.compile(r"[1-9][0-9]{0,9}")
_SESSION_ID_MAX = 256
_ADDR_MAX = 64


def _text_class(value: object, patterns: tuple[tuple[str, re.Pattern[str]], ...]) -> str:
    """The first class whose pattern occurs in the (truncated) text, else "other"."""
    if type(value) is not str or not value:
        return "other"
    text = value[:_CALLER_TEXT_MAX]
    for label, pattern in patterns:
        if pattern.search(text):
            return label
    return "other"


def _host_class(value: object) -> str | None:
    """app.sugra.ai, mcp.sugra.ai, loopback or other; None when there is no Host."""
    if type(value) is not str:
        return None
    host = value[:_CALLER_TEXT_MAX].strip().lower()
    if not host:
        return None
    if host.startswith("["):
        # An IPv6 literal: the bracketed address alone, or with a numeric port.
        end = host.find("]")
        port = host[end + 1:] if end != -1 else ""
        if end == -1 or (port and not (port[0] == ":" and port[1:].isascii() and port[1:].isdigit())):
            return "other"
        host = host[: end + 1]
    elif host.count(":") == 1:
        name, _, port = host.partition(":")
        if port.isascii() and port.isdigit():
            host = name
    if host in _CALLER_HOSTS:
        return host
    return "loopback" if host in _LOOPBACK_HOSTS else "other"


def _origin_class(value: object) -> str:
    """openai, anthropic or cursor for a known connector origin, none without one, else other."""
    if value is None:
        return "none"
    if type(value) is not str:
        return "other"
    origin = value[:_CALLER_TEXT_MAX].strip().lower()
    return _ORIGIN_CLASSES.get(origin, "other") if origin else "none"


def _version_of(value: object) -> str | None:
    """A plain dotted version, else None.

    One to four parts of one to five ASCII digits each, at most 23 characters in
    all. The bounds are deliberate: a client version is short, and anything
    longer or looser is dropped rather than trimmed.
    """
    if type(value) is not str or len(value) > _VERSION_MAX:
        return None
    return value if _VERSION_RE.fullmatch(value) else None


def _pseudo_id_of(value: object) -> str | None:
    """Admission name for user_Id: http:<16 hex>, http:anonymous, or local."""
    if type(value) is not str:
        return None
    return value if _PSEUDO_ID_RE.fullmatch(value) else None


def _authenticated_id_of(auth: object, user_id: object) -> str | None:
    """OAuth user id as a decimal string, only when auth is oauth."""
    if auth != "oauth" or type(user_id) is not int:
        return None
    text = str(user_id)
    return text if _AUTH_USER_RE.fullmatch(text) else None


def _session_digest(value: object) -> str | None:
    """16 lowercase hex of SHA-256 of the session id, never the id itself."""
    if type(value) is not str or not value or len(value) > _SESSION_ID_MAX:
        return None
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:16]


def _ip_of(value: object) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    """An IP address, with IPv4-mapped IPv6 unwrapped to IPv4."""
    if type(value) is not str or not value or len(value) > _ADDR_MAX:
        return None
    try:
        addr = ipaddress.ip_address(value)
    except ValueError:
        return None
    mapped = getattr(addr, "ipv4_mapped", None)
    return mapped if mapped is not None else addr


def _network_of(client_addr: object, x_real_ip: object) -> str | None:
    """Coarse origin: loopback, private, IPv4 /24 or IPv6 /48.

    A public or private prefix is attached only when the ASGI peer equals
    X-Real-IP, so a client-forgeable X-Forwarded-For cannot name the span
    when the process is behind a proxy that overwrites X-Real-IP (hosted
    nginx). Loopback is allowed without X-Real-IP: that is a local ASGI
    client. IPv4-mapped IPv6 (`::ffff:a.b.c.d`) is treated as the IPv4
    address.
    """
    addr = _ip_of(client_addr)
    if addr is None:
        return None
    if addr.is_loopback:
        if x_real_ip is None:
            return "loopback"
        real = _ip_of(x_real_ip)
        return "loopback" if real is not None and real.is_loopback else None
    real = _ip_of(x_real_ip)
    if real is None or addr != real:
        return None
    if addr.is_private or addr.is_link_local:
        return "private"
    if addr.version == 4:
        return str(ipaddress.ip_network(f"{addr}/24", strict=False))
    return str(ipaddress.ip_network(f"{addr}/48", strict=False))


def _caller_attrs(facts: object) -> dict[str, str]:
    """The caller attributes for a call: each a fixed class, digest or prefix.

    Reads the facts by attribute name, so a missing or odd field drops only its
    own attribute, and nothing a client sent is ever copied through.
    """
    attrs: dict[str, str] = {}
    transport = getattr(facts, "transport", None)
    if type(transport) is str and transport in _CALLER_TRANSPORTS:
        attrs["mcp.caller.transport"] = transport
    auth = getattr(facts, "auth", None)
    if type(auth) is str and auth in _CALLER_AUTH_METHODS:
        attrs["mcp.caller.auth"] = auth
    if attrs.get("mcp.caller.transport") == "streamable_http":
        host = _host_class(getattr(facts, "host", None))
        if host is not None:
            attrs["mcp.caller.host"] = host
        attrs["mcp.caller.ua_class"] = _text_class(getattr(facts, "user_agent", None), _UA_PATTERNS)
        attrs["mcp.caller.origin"] = _origin_class(getattr(facts, "origin", None))
        session = _session_digest(getattr(facts, "session_id", None))
        if session is not None:
            attrs["mcp.caller.session"] = session
        net = _network_of(getattr(facts, "client_addr", None), getattr(facts, "x_real_ip", None))
        if net is not None:
            attrs["mcp.caller.net"] = net
    if getattr(facts, "client_name", None) is not None:
        attrs["mcp.caller.client"] = _text_class(getattr(facts, "client_name", None), _CLIENT_NAME_PATTERNS)
    version = _version_of(getattr(facts, "client_version", None))
    if version is not None:
        attrs["mcp.caller.client_version"] = version
    pseudo = _pseudo_id_of(getattr(facts, "caller", None))
    if pseudo is not None:
        attrs["enduser.pseudo.id"] = pseudo
    authenticated = _authenticated_id_of(attrs.get("mcp.caller.auth"), getattr(facts, "user_id", None))
    if authenticated is not None:
        attrs["enduser.id"] = authenticated
    if attrs.get("mcp.caller.auth") == "oauth":
        platform = getattr(facts, "platform", None)
        if type(platform) is str and platform in _CALLER_PLATFORMS:
            attrs["mcp.caller.platform"] = platform
    return attrs


def _dispatch_facts() -> object:
    """The CallerFacts of the tool call being dispatched, or None when there are none.

    server.current_caller_facts is looked up at call time, never imported, so a
    test that reloads either module cannot leave a stale binding, and any failure
    to read the facts reads as no facts, never as a failed tool call.
    """
    try:
        provider = getattr(sys.modules.get("sugra_api_mcp.server"), "current_caller_facts", None)
        return provider() if callable(provider) else None
    except Exception:
        return None


def _caller_attrs_of(facts: object) -> dict[str, str]:
    """_caller_attrs for facts that may be None or unreadable: {} instead of raising."""
    if facts is None:
        return {}
    try:
        return _caller_attrs(facts)
    except Exception:
        return {}


def _dispatch_caller_attrs() -> dict[str, str]:
    """The caller attributes of the tool call being dispatched, or {} when there are none.

    Any failure to read the facts drops only the attributes, never the tool
    result (_dispatch_facts, _caller_attrs_of).
    """
    return _caller_attrs_of(_dispatch_facts())


def _facts_field(facts: object, name: str) -> object:
    """One field of the facts, or None when it is missing or reading it raises."""
    try:
        return getattr(facts, name, None)
    except Exception:
        return None


def _is_key_call(facts: object) -> bool:
    """True when the request that carried the call authenticated with a sugra_ API key."""
    auth = _facts_field(facts, "auth")
    return type(auth) is str and auth == "api_key"


def _placement_attrs(facts: object) -> dict[str, str]:
    """mcp.side always, and mcp.request.id when the carrying request had a valid one."""
    attrs = {"mcp.side": process_side()}
    request_id = request_id_of(_facts_field(facts, "request_id"))
    if request_id is not None:
        attrs["mcp.request.id"] = request_id
    return attrs


def record_refused_call(tool_name: str, error_code: str, scope: object = None) -> None:
    """Leave a failure span for a registered tool call refused before dispatch.

    A call refused at the in-flight cap never reaches its tool, so the
    tool's own span never starts and the refusal would be invisible. The caller
    passes only a name it found registered, the code must be allowlisted, and
    nothing from the call's arguments is attached. The refusal's
    scope rides along as `mcp.busy.scope` when the code is server_busy and the
    scope is one of the fixed names. So do the caller attributes of
    the request that carried the refused call.
    """
    if _TRACER is None or error_code not in _KNOWN_ERROR_CODES:
        return
    try:
        span = _TRACER.start_span(name=f"mcp.tool.{tool_name}")
    except Exception:
        return
    try:
        _safe_attr(span, "mcp.tool.name", tool_name)
        for key, value in _dispatch_caller_attrs().items():
            _safe_attr(span, key, value)
        _safe_attr(span, "mcp.success", False)
        _safe_attr(span, "mcp.error.code", error_code)
        busy_scope = _busy_scope_of(error_code, scope)
        if busy_scope is not None:
            _safe_attr(span, "mcp.busy.scope", busy_scope)
        _safe_attr(span, "mcp.duration_ms", 0)
        _safe_status_error(span)
    finally:
        _safe_end(span)


def record_unstarted_call(tool_name: str | None, error_code: str, duration_ms: int) -> None:
    """Leave a failure span for an admitted tool call that never reached its tool.

    server.py call_tool calls this when the SDK refused the call before the
    tool ran: tool_name is the registered name whose arguments failed
    validation, or None for a name no tool is registered under. That span is
    always named mcp.tool.unknown and carries no mcp.tool.name, so a name the
    client chose never reaches the workspace. A call authenticated by a sugra_
    API key leaves no span: it never reached the API, and the gate summary
    counts it instead.
    """
    if _TRACER is None or error_code not in _KNOWN_ERROR_CODES:
        return
    facts = _dispatch_facts()
    if _is_key_call(facts):
        return
    try:
        span = _TRACER.start_span(
            name="mcp.tool.unknown" if tool_name is None else f"mcp.tool.{tool_name}"
        )
    except Exception:
        return
    try:
        if tool_name is not None:
            _safe_attr(span, "mcp.tool.name", tool_name)
        for key, value in _caller_attrs_of(facts).items():
            _safe_attr(span, key, value)
        for key, value in _placement_attrs(facts).items():
            _safe_attr(span, key, value)
        _safe_attr(span, "mcp.success", False)
        _safe_attr(span, "mcp.error.code", error_code)
        _safe_attr(span, "mcp.api.requests", 0)
        _safe_attr(span, "mcp.duration_ms", duration_ms)
        _safe_status_error(span)
    finally:
        _safe_end(span)


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

# Environment variables through which the distro would export no spans
# (OTEL_TRACES_EXPORTER=none) or only a sample of them (OTEL_TRACES_SAMPLER,
# OTEL_TRACES_SAMPLER_ARG). An environment sampler outranks the sampling_ratio
# argument, so setup_observability removes all three before configuring.
_TRACE_OVERRIDE_VARS: tuple[str, ...] = (
    "OTEL_TRACES_EXPORTER",
    "OTEL_TRACES_SAMPLER",
    "OTEL_TRACES_SAMPLER_ARG",
)


def _drop_trace_overrides() -> None:
    """Remove the trace override variables; the warning names the variable, never its value."""
    for name in _TRACE_OVERRIDE_VARS:
        if name in os.environ:
            del os.environ[name]
            logger.warning("%s is set; removed so that every tool span is exported.", name)


def _add_service_version() -> None:
    """Add service.version=<package version> to OTEL_RESOURCE_ATTRIBUTES.

    The SDK reads the variable through its resource detector, and the exporter
    sends service.version as the application version (application_Version in
    App Insights). An operator's own service.version there wins, and the other
    attributes the operator set stay; only empty items are dropped.
    """
    raw = os.environ.get("OTEL_RESOURCE_ATTRIBUTES", "")
    items = [item for item in raw.split(",") if item.strip()]
    for item in items:
        key, sep, _value = item.partition("=")
        if sep and key.strip() == "service.version":
            return
    os.environ["OTEL_RESOURCE_ATTRIBUTES"] = ",".join([*items, f"service.version={__version__}"])


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
        # The package version reaches the resource the same way.
        _add_service_version()
        _drop_trace_overrides()

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
            # No sampling. Without a sampler setting the distro keeps at most
            # five traces a second; every tool span is exported instead, so
            # each call can be found by its request id.
            sampling_ratio=1.0,
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


def _flush_targets(provider: object) -> tuple[object, ...]:
    """The processors behind a logger or tracer provider, in order; else the provider itself."""
    # opentelemetry-sdk before 1.42 stops a provider's flush at the first processor returning a
    # falsy value, and Azure Monitor's live metrics and performance counter processors return None.
    multi = getattr(provider, "_multi_log_record_processor", None) or getattr(
        provider, "_active_span_processor", None
    )
    processors = getattr(multi, "_log_record_processors", None) or getattr(
        multi, "_span_processors", None
    )
    return tuple(processors) if processors else (provider,)


def flush_telemetry(timeout_s: float) -> bool:
    """Hand buffered logs, spans and metrics to their exporters, within timeout_s in all.

    The OpenTelemetry providers flush their batches from an atexit hook, and a
    process uvicorn stops on SIGTERM never runs it: uvicorn raises the signal
    again once it has shut down. gate.wrap_lifespan calls this on the way out
    instead. Logs go first, since the last gate summary is one. The work runs
    on a daemon thread, so an exporter that hangs costs at most timeout_s and
    never holds the process. True when every flush finished in time; a
    no-op without the OpenTelemetry SDK.
    """
    deadline = time.monotonic() + timeout_s
    done = threading.Event()

    def flush() -> None:
        try:
            from opentelemetry import metrics, trace
            from opentelemetry._logs import get_logger_provider

            providers = [
                get_logger_provider(),
                trace.get_tracer_provider(),
                metrics.get_meter_provider(),
            ]
            for provider in providers:
                for target in _flush_targets(provider):
                    force_flush = getattr(target, "force_flush", None)
                    remaining_ms = int((deadline - time.monotonic()) * 1000)
                    if callable(force_flush) and remaining_ms > 0:
                        with contextlib.suppress(Exception):
                            force_flush(timeout_millis=remaining_ms)
        except Exception:
            pass
        finally:
            done.set()

    threading.Thread(target=flush, name="sugra-telemetry-flush", daemon=True).start()
    return done.wait(timeout_s)


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


def _entry_attrs(tool_name: str, facts: object, operation_id: object) -> dict[str, Any]:
    """The attributes a tool span carries from the call's entry, in the order they are set."""
    attrs: dict[str, Any] = {"mcp.tool.name": tool_name}
    attrs.update(_caller_attrs_of(facts))
    # operation_id is attached ONLY if the kwarg value matches a
    # catalog-known operation_id. Arbitrary client-supplied strings
    # (PII / secrets / free text) are dropped before reaching App
    # Insights.
    if isinstance(operation_id, str) and operation_id in _get_valid_operation_ids():
        attrs["mcp.operation_id"] = operation_id
    attrs.update(_placement_attrs(facts))
    return attrs


def _late_span(tool_name: str, entry_attrs: dict[str, Any], frame: _CallFrame, start_ns: int) -> Any:
    """The span of a sugra_ key call that reached the API, opened at the call's exit.

    Backdated to the call's entry, so its timestamp and duration read like an
    eager span's. None when the call made no API request, or when span creation
    fails: telemetry never breaks the tool result.
    """
    if frame.api_requests <= 0 or _TRACER is None:
        return None
    try:
        span = _TRACER.start_span(name=f"mcp.tool.{tool_name}", start_time=start_ns)
    except Exception:
        return None
    for key, value in entry_attrs.items():
        _safe_attr(span, key, value)
    return span


def _stamp_failure(
    span: Any, error_code: str, exception_type: str | None, frame: _CallFrame, start: float
) -> None:
    """Close the attributes of a call that raised or was cancelled."""
    _safe_attr(span, "mcp.success", False)
    _safe_attr(span, "mcp.error.code", error_code)
    if exception_type is not None:
        _safe_attr(span, "mcp.exception.type", exception_type)
    _safe_attr(span, "mcp.api.requests", frame.api_requests)
    _safe_attr(span, "mcp.duration_ms", int((time.perf_counter() - start) * 1000))
    _safe_status_error(span)


def _stamp_returned(
    span: Any,
    result: Any,
    result_attrs: Callable[[Any], dict[str, Any]] | None,
    frame: _CallFrame,
    start: float,
) -> None:
    """Close the attributes of a call that returned."""
    # Failure is errors.is_error_payload - the ONE
    # definition the tool protocol (server.py) applies - so a
    # partial envelope pairing an error note with data counts as
    # the success the client received, and a bare error key counts
    # as a failure whatever the type of its value.
    success = not is_error_payload(result)
    error_code: str | None = None if success else _error_code_of(result)
    _safe_attr(span, "mcp.success", success)
    if error_code is not None:
        _safe_attr(span, "mcp.error.code", error_code)
        busy_scope = _busy_scope_of(error_code, _payload_scope(result))
        if busy_scope is not None:
            _safe_attr(span, "mcp.busy.scope", busy_scope)
    if result_attrs is not None and success:
        try:
            for key, value in result_attrs(result).items():
                _safe_attr(span, key, value)
        except Exception:
            # Extractor bugs must never break the tool result.
            pass
    _safe_attr(span, "mcp.api.requests", frame.api_requests)
    _safe_attr(span, "mcp.duration_ms", int((time.perf_counter() - start) * 1000))
    # Status drives App Insights top-level `success` column;
    # ERROR for tool-reported failures (catalog-mapped error
    # code), OK for clean success.
    if success:
        _safe_status_ok(span)
    else:
        _safe_status_error(span)


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

    A call authenticated by a sugra_ API key leaves a span only once it made a
    Sugra API request (see "Which calls leave a span" at the module level):
    its span opens at the call's exit, backdated to the entry, and carries
    the same attributes an eager span would.

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
            dispatch = tool_dispatch.get()
            if dispatch is not None:
                # The call reached its tool: whatever follows is the tool's own
                # outcome, never a refused name or refused arguments.
                dispatch.entered = True
            if _TRACER is None:
                return await func(*args, **kwargs)

            start = time.perf_counter()
            start_ns = time.time_ns()
            # How the call arrived, read before the tool runs so
            # every exit (success, failure, exception, cancellation) carries it.
            facts = _dispatch_facts()
            entry_attrs = _entry_attrs(tool_name, facts, kwargs.get("operation_id"))
            frame = _CallFrame(_call_frame.get())
            previous_frame = _call_frame.set(frame)
            span: Any = None
            try:
                if not _is_key_call(facts):
                    try:
                        span = _TRACER.start_span(name=f"mcp.tool.{tool_name}")
                    except Exception:
                        # Span creation itself failed - run the tool without
                        # telemetry rather than masking the call.
                        return await func(*args, **kwargs)
                    for key, value in entry_attrs.items():
                        _safe_attr(span, key, value)

                try:
                    result = await func(*args, **kwargs)
                except asyncio.CancelledError:
                    # A BaseException: the dispatch budget (server.py) cancels
                    # the task, and the clause below never saw it, so the span
                    # ended through `finally` with only the tool name - the one
                    # failure that fires when the API is slowest was invisible
                    # to every failure query. Stamp the verdict and
                    # re-raise unchanged so the cancellation still propagates.
                    if span is None:
                        span = _late_span(tool_name, entry_attrs, frame, start_ns)
                    if span is not None:
                        _stamp_failure(span, _cancellation_code(), None, frame, start)
                    raise
                except Exception as e:
                    if span is None:
                        span = _late_span(tool_name, entry_attrs, frame, start_ns)
                    if span is not None:
                        _stamp_failure(span, "exception", type(e).__name__, frame, start)
                    raise

                if span is None:
                    span = _late_span(tool_name, entry_attrs, frame, start_ns)
                if span is not None:
                    _stamp_returned(span, result, result_attrs, frame, start)
                return result
            finally:
                _call_frame.reset(previous_frame)
                if span is not None:
                    _safe_end(span)

        return wrapper

    return decorator
