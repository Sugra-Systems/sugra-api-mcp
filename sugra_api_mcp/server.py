"""FastMCP server instance, tool annotation helper, and shared client accessor."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import threading
import time
from contextvars import ContextVar
from copy import deepcopy
from dataclasses import dataclass
from typing import Any

from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import CallToolResult, Icon, TextContent, ToolAnnotations
from mcp.types import Tool as MCPTool

from . import __version__, observability
from .client import SugraClient
from .config import MISSING_API_KEY_HINT, Config, load_allowed_origins, load_config
from .errors import is_error_payload

api_key_ctx: ContextVar[str | None] = ContextVar("sugra_api_key", default=None)

OAUTH_SCOPES = ["sugra:read", "offline_access"]

OAUTH_SECURITY_SCHEMES: list[dict[str, Any]] = [
    {"type": "oauth2", "scopes": OAUTH_SCOPES},
]

WEBSITE_URL = "https://sugra.ai"

# Brand icons for the initialize response, served from our own host so MCP
# clients can fetch them without authentication.
SERVER_ICONS: list[Icon] = [
    Icon(
        src="https://app.sugra.ai/images/brand/sugra-app-icon-192.png",
        mimeType="image/png",
        sizes=["192x192"],
    ),
    Icon(
        src="https://app.sugra.ai/images/brand/sugra-app-icon-512.png",
        mimeType="image/png",
        sizes=["512x512"],
    ),
]

READ_ONLY_TOOL = ToolAnnotations(
    readOnlyHint=True,
    destructiveHint=False,
    idempotentHint=True,
    openWorldHint=True,
)


# MCP Apps (SEP-1865, extension io.modelcontextprotocol/ui): the one tool
# that may render an interactive widget. Its template declaration is attached
# in SugraFastMCP.list_tools via _meta.ui.resourceUri (spec section "Resource
# Discovery"; the flat "ui/resourceUri" key is deprecated). The
# widget is opt-in, so nothing is attached unless tools/widgets.py registered
# the template on the server and linked it with link_ui_template.
UI_TEMPLATE_TOOL = "call_endpoint"


def _oauth_security_schemes() -> list[dict[str, Any]]:
    return deepcopy(OAUTH_SECURITY_SCHEMES)


def _with_oauth_security(tool: MCPTool) -> MCPTool:
    payload = tool.model_dump(by_alias=True, exclude_none=True)
    payload["securitySchemes"] = _oauth_security_schemes()
    meta = dict(payload.get("_meta") or {})
    meta["securitySchemes"] = _oauth_security_schemes()
    payload["_meta"] = meta
    return MCPTool.model_validate(payload)


def _with_ui_template(tool: MCPTool, resource_uri: str | None) -> MCPTool:
    """Attach the MCP Apps template declaration (SEP-1865 "Resource Discovery").

    Only UI_TEMPLATE_TOOL renders a widget, and only when the server has a
    linked template (resource_uri is not None): _meta.ui.resourceUri then
    points at the ui:// template resource registered on that same server.
    """
    if resource_uri is None or tool.name != UI_TEMPLATE_TOOL:
        return tool
    payload = tool.model_dump(by_alias=True, exclude_none=True)
    meta = dict(payload.get("_meta") or {})
    meta["ui"] = {"resourceUri": resource_uri}
    payload["_meta"] = meta
    return MCPTool.model_validate(payload)


class SugraFastMCP(FastMCP):
    """FastMCP with OAuth tool metadata and the opt-in SEP-1865 UI template meta.

    Also pins serverInfo.version to the package version: FastMCP never
    forwards a version to the lowlevel server, whose initialize response then
    falls back to the MCP SDK version instead of ours.
    """

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._mcp_server.version = __version__
        # The ui:// template UI_TEMPLATE_TOOL declares, or None.
        # None until link_ui_template runs, so by default no tool carries
        # _meta.ui.
        self._ui_template_uri: str | None = None

    def link_ui_template(self, resource_uri: str) -> None:
        """Declare resource_uri as the MCP Apps template of UI_TEMPLATE_TOOL.

        The one caller is tools/widgets.py register_ui_widgets,
        right after it registered that resource here. list_tools never reads
        the environment; it attaches only what was linked, so the tool
        declaration and resources/list cannot disagree. A URI that is not a
        resource of this server is refused, so a tool can never point at a
        template the server does not serve.
        """
        listed = {str(resource.uri) for resource in self._resource_manager.list_resources()}
        if resource_uri not in listed:
            raise ValueError(f"cannot link unregistered UI template: {resource_uri}")
        self._ui_template_uri = resource_uri

    async def list_tools(self) -> list[MCPTool]:
        template_uri = self._ui_template_uri
        return [
            _with_ui_template(_with_oauth_security(tool), template_uri)
            for tool in await super().list_tools()
        ]

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> Any:
        """Refuse a call past the in-flight caps with server_busy.

        Admission and release go through the module-level counters under their
        lock (see MAX_IN_FLIGHT_TOOL_CALLS), so every server instance in the
        process shares them, and one caller holds at most its own share.
        """
        caller = current_caller()
        refusal = _admit_tool_call(caller)
        if refusal is not None:
            # The refused call never reaches its tool, so the tool's span never
            # starts; record one here, for registered names only.
            if self._tool_manager.get_tool(name) is not None:
                observability.record_refused_call(name, refusal["error"], refusal.get("scope"))
            return CallToolResult(
                isError=True,
                content=[TextContent(type="text", text=json.dumps(refusal))],
                structuredContent=refusal,
            )
        try:
            return await self._call_tool_in_budget(name, arguments)
        finally:
            _release_tool_call(caller)

    async def _call_tool_in_budget(self, name: str, arguments: dict[str, Any]) -> Any:
        """Report a failed tool call as a protocol-level error.

        Tools return their failures as structured payloads instead of raising,
        which the SDK cannot distinguish from data: anything returned becomes
        `isError=false`, and only a raised exception sets the flag - at the cost
        of discarding the payload and, for an exception with no message, saying
        nothing at all. Agents were left to notice the failure by inspecting the
        body, and clients that branch on the protocol flag never saw one.

        Returning a CallToolResult keeps both halves: the SDK passes it through
        untouched, so the flag is set AND the explanation survives.

        The text block is rebuilt from the same payload so it cannot be empty,
        which is the whole reason failures are returned instead of raised.
        Nothing is lost: every tool declares a dict result, so the block being
        replaced is the JSON rendering of that same dict.
        """
        # ONE end-to-end budget for the whole call.
        # Without it, a wedged upstream held the session past every client's
        # read timeout and the typed envelope never reached the agent; the
        # asyncio scope also CANCELS the outbound request instead of letting
        # it complete server-side after the caller has given up.
        # The budget is the tool's own, and auth is bounded separately.
        #
        # This used to subtract the auth leg, read from a ContextVar stamped by
        # AuthMiddleware. That subtraction was unsound on the streamable-HTTP
        # transport and took the hosted gateway down for three weeks. The SDK
        # starts the per-session server loop with task_group.start() from
        # INSIDE the request that creates the session, so the loop inherits
        # that request's contextvars; every later dispatch on the session read
        # the stamp of the request that OPENED it. Past one budget of session
        # age, every tool call was refused before dispatch, for good.
        #
        # The transport offers no way to route a value from the POST that
        # carries a tools/call to the dispatch that serves it: what is visible
        # here is always the session-creating request's stamp and never this
        # call's. An unattributable clock cannot shorten a budget, so it no
        # longer does. Auth keeps its own bound - AuthMiddleware runs
        # resolve() under min(AUTH_BUDGET_SECONDS, total) - which makes the
        # end-to-end worst case total + AUTH_BUDGET_SECONDS, and only on a
        # cold auth: a warm session resolves against the 60s access cache and
        # the 300s key cache. That bound is stated rather than guessed at.
        total = load_config(require_api_key=False).tool_deadline
        started = time.monotonic()
        deadline = total
        # Publish the timeout that bounds this dispatch, with the
        # task's cancellation count at entry, so the tool's span can ask
        # whether the budget owns a cancellation and name it deadline_exceeded
        # instead of a bare cancelled (the timeout's own state, never a clock
        # comparison - see observability.DispatchBudget for the ways a clock
        # or expired() alone misfile). Set and reset around ONE dispatch on
        # this task: it never inherits across calls the way the ambient stamp
        # did. Reached through the module attribute, not a from-import: the
        # wrapper reads the SAME attribute by name at call time, so the two
        # cannot bind to different objects (a module reload in the test suite
        # did exactly that).
        dispatch = asyncio.timeout(deadline)
        task = asyncio.current_task()
        budget_token = observability.dispatch_budget.set(
            observability.DispatchBudget(dispatch, task.cancelling() if task is not None else 0)
        )
        try:
            async with dispatch:
                result = await super().call_tool(name, arguments)
        except TimeoutError:
            payload = {
                "error": "deadline_exceeded",
                "message": (
                    f"Tool call exceeded its {deadline:.0f}s dispatch budget "
                    "and was cancelled server-side."
                ),
                "tool": name,
                "deadline_s": deadline,
                "elapsed_ms": int((time.monotonic() - started) * 1000),
                "retry_hint": (
                    "Retry once; if it repeats, narrow the request "
                    "(fewer points, tighter filters)."
                ),
            }
            return CallToolResult(
                isError=True,
                content=[TextContent(type="text", text=json.dumps(payload))],
                structuredContent=payload,
            )
        finally:
            observability.dispatch_budget.reset(budget_token)

        # Tools declaring a dict return arrive as (content, structured); the
        # bare forms are accepted so this cannot depend on that detail.
        if isinstance(result, tuple) and len(result) == 2:
            _content, structured = result
        elif isinstance(result, dict):
            structured = result
        else:
            return result

        if not is_error_payload(structured):
            return result

        return CallToolResult(
            isError=True,
            content=[TextContent(type="text", text=json.dumps(structured, indent=2, default=str))],
            structuredContent=structured,
        )


# At most MAX_IN_FLIGHT_TOOL_CALLS tool calls run at once in this
# process, across every server instance, and at most MAX_IN_FLIGHT_PER_CALLER of
# them for any one caller (current_caller), so no one API key can take every slot. Thirty
# days of hosted telemetry (21,840 calls) peaked at 6 concurrent calls, so both
# caps sit above real traffic and only stop a flood from piling up unbounded
# work. The lock keeps admission and release atomic even when calls arrive from
# more than one event loop thread.
MAX_IN_FLIGHT_TOOL_CALLS = 32
MAX_IN_FLIGHT_PER_CALLER = 16
_in_flight_lock = threading.Lock()
_in_flight_tool_calls = 0
_in_flight_by_caller: dict[str, int] = {}


def current_caller() -> str:
    """A stable name for the principal behind the tool call being dispatched.

    On the HTTP transport it is "http:" and a short SHA-256 digest of the API key
    the carrying request resolved to, so the name never contains the key. A
    `sugra_` bearer is its own key; an OAuth token resolves to the account's
    primary API key (auth.py), so every OAuth session of one account is one
    caller and a token refresh keeps the name. A request that carried no
    credential is "http:anonymous". Outside HTTP (stdio and in-process callers)
    every call is "local".
    """
    is_http_request, request_key = _dispatching_http_request()
    if request_key:
        return "http:" + hashlib.sha256(request_key.encode("utf-8")).hexdigest()[:16]
    if is_http_request or http_transport_ctx.get():
        return "http:anonymous"
    return "local"


def in_flight_tool_calls(caller: str | None = None) -> int:
    """Tool calls admitted and not yet finished; given a caller, only that caller's."""
    with _in_flight_lock:
        if caller is None:
            return _in_flight_tool_calls
        return _in_flight_by_caller.get(caller, 0)


def _admit_tool_call(caller: str) -> dict[str, Any] | None:
    """Count one call in for caller: None when admitted, else the server_busy payload."""
    global _in_flight_tool_calls
    from .errors import server_busy_error

    with _in_flight_lock:
        if _in_flight_tool_calls >= MAX_IN_FLIGHT_TOOL_CALLS:
            return server_busy_error("tool_calls", MAX_IN_FLIGHT_TOOL_CALLS)
        held = _in_flight_by_caller.get(caller, 0)
        if held >= MAX_IN_FLIGHT_PER_CALLER:
            return server_busy_error("caller_tool_calls", MAX_IN_FLIGHT_PER_CALLER)
        _in_flight_tool_calls += 1
        _in_flight_by_caller[caller] = held + 1
        return None


def _release_tool_call(caller: str) -> None:
    global _in_flight_tool_calls
    with _in_flight_lock:
        _in_flight_tool_calls -= 1
        held = _in_flight_by_caller.get(caller, 0) - 1
        if held > 0:
            _in_flight_by_caller[caller] = held
        else:
            _in_flight_by_caller.pop(caller, None)


def read_only(title: str) -> ToolAnnotations:
    """Tool annotations for a read-only Sugra API wrapper with a human-readable title.

    Gateway tools are safe to retry, do not mutate state, and pull from the
    open world through the Sugra API. The optional `title` surfaces in MCP
    client UIs as the display name distinct from the snake_case function name.
    """
    return ToolAnnotations(
        readOnlyHint=True,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=True,
        title=title,
    )


def _build_transport_security() -> TransportSecuritySettings | None:
    """Build DNS rebinding protection settings from SUGRA_MCP_ALLOWED_HOSTS.

    When deployed behind a reverse proxy (e.g. nginx at app.sugra.ai), the Host
    header won't match the default localhost allowlist. Set the env var to a
    comma-separated list of public hostnames to allow.

    Browser-based MCP clients (ChatGPT Connectors UI) send an Origin header
    that the inner FastMCP middleware also validates against
    ``allowed_origins``. The outer Starlette CORS layer would otherwise let
    the preflight through only to have the actual request rejected with 403
    Invalid Origin from this inner layer, so the two allowlists must stay in
    sync.
    """
    raw = os.environ.get("SUGRA_MCP_ALLOWED_HOSTS", "").strip()
    if not raw:
        return None
    hosts = [h.strip() for h in raw.split(",") if h.strip()]

    cors_origins = load_allowed_origins()
    if cors_origins == ["*"]:
        # SUGRA_MCP_ALLOWED_ORIGINS=* asks both layers to allow any origin.
        # FastMCP's inner middleware does not understand "*" as a glob (only
        # exact match and ":*" port suffix), so we disable inner DNS rebinding
        # protection entirely. Host check is also lost; Bearer auth still
        # gates tool calls and the outer reverse proxy still constrains Host.
        # Intended for self-hosted or dev only.
        logging.getLogger("sugra_mcp.security").warning(
            "SUGRA_MCP_ALLOWED_ORIGINS=*: inner DNS rebinding protection "
            "disabled. Bearer auth still gates tool calls, but the hosted MCP "
            "endpoint becomes browser-reachable from any origin. Use only for "
            "self-hosted or dev environments."
        )
        return TransportSecuritySettings(enable_dns_rebinding_protection=False)

    return TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=[*hosts, "127.0.0.1:*", "localhost:*", "[::1]:*"],
        allowed_origins=[
            *[f"https://{h}" for h in hosts],
            *cors_origins,
            "http://127.0.0.1:*",
            "http://localhost:*",
            "http://[::1]:*",
        ],
    )


mcp = SugraFastMCP(
    "sugra-api",
    instructions=(
        "Sugra API gateway - unified operation_id access across the bundled endpoint "
        "catalog. Use search_endpoints to find operations, describe_endpoint to inspect "
        "parameters, and call_endpoint to call by operation_id."
    ),
    website_url=WEBSITE_URL,
    icons=SERVER_ICONS,
    transport_security=_build_transport_security(),
)

_shared_client: SugraClient | None = None

_per_key_clients: dict[str, SugraClient] = {}

# Call-time key enforcement (keyless startup): the process must boot and
# answer initialize / tools/list / prompts / resources without SUGRA_API_KEY -
# directory evaluators and several MCP clients launch the server with no env
# configured and only let the user add credentials after introspection
# succeeds. The key requirement therefore lives here, on the first outbound
# API call, not in load_config at process start.
def missing_api_key_error() -> dict[str, Any]:
    """Structured tool error returned when no API key is available at call time.

    Mirrors the error contract used across tools/gateway.py: a dict with an
    ``error`` code plus an actionable hint, never a raised exception (a raise
    surfaces to MCP clients as an opaque "Error executing tool" message).
    """
    return {"error": "missing_api_key", "hint": MISSING_API_KEY_HINT}


class _KeylessClient:
    """Stand-in for SugraClient when no API key is available.

    Every network method returns the structured ``missing_api_key`` error
    instead of performing a request, so catalog-only tools keep working and
    network-backed tools degrade to an actionable error. Not cached as the
    shared client: if the environment gains a key later, the next
    ``get_client()`` call builds a real client.
    """

    async def get(
        self, path: str, params: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        return missing_api_key_error()

    async def post(
        self,
        path: str,
        json: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        return missing_api_key_error()

    async def request(
        self,
        method: str,
        path: str,
        params: dict[str, Any] | None = None,
        json: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        return missing_api_key_error()

    async def aclose(self) -> None:
        return None


_keyless_client = _KeylessClient()


def _build_client(api_key: str) -> SugraClient:
    return SugraClient(
        Config(
            api_base=os.environ.get("SUGRA_API_BASE", "https://sugra.ai").rstrip("/"),
            api_key=api_key,
            timeout=float(os.environ.get("SUGRA_TIMEOUT", "30")),
        )
    )


# Request-scoped credentials. On the Streamable HTTP transport the per-session
# server task is started inside the request that OPENED the session and keeps
# that request's ContextVars for its whole life, so a ContextVar set by
# AuthMiddleware on a later request never reaches tool dispatch. A session opened
# without a token then ran every tool on the SUGRA_API_KEY fallback, and a
# session opened by one principal kept that principal's key for any later
# caller. The SDK does hand each handler the Starlette Request that delivered
# its JSON-RPC message, so AuthMiddleware stores the resolved key on that
# request's scope state and get_client reads it from there.
REQUEST_API_KEY_STATE = "sugra_api_key"

# How the request that carried a tool call authenticated, stored by
# AuthMiddleware beside the key and read at dispatch the same way, for caller
# attribution on spans. It never holds the token, the token id or the key.
REQUEST_PRINCIPAL_STATE = "sugra_principal"


@dataclass(frozen=True)
class RequestPrincipal:
    method: str | None
    user_id: int | None = None
    platform: str | None = None

# Set by AuthMiddleware for every request it serves. A Streamable HTTP session
# task inherits it from the request that opened the session, so a dispatch that
# belongs to the HTTP transport is known as such even with no attached request,
# and never borrows an inherited key or the SUGRA_API_KEY fallback. stdio never
# sets it, so a process that also serves stdio keeps the env key there.
http_transport_ctx: ContextVar[bool] = ContextVar("sugra_http_transport", default=False)


def _dispatching_http_request() -> tuple[bool, str | None]:
    """Return (True, key or None) while a handler runs for an HTTP request.

    Returns (False, None) outside an MCP request context and on stdio, where the
    SDK carries no HTTP request.
    """
    from mcp.server.lowlevel.server import request_ctx

    try:
        context = request_ctx.get()
    except LookupError:
        return False, None
    scope = getattr(getattr(context, "request", None), "scope", None)
    if not isinstance(scope, dict):
        return False, None
    state = scope.get("state")
    key = state.get(REQUEST_API_KEY_STATE) if isinstance(state, dict) else None
    return True, key if isinstance(key, str) and key else None


def current_caller_facts() -> observability.CallerFacts | None:
    """What the tool call being dispatched says about its caller, or None outside a dispatch.

    Everything comes from the SDK request context set
    for this one message: the Starlette Request that carried it and the session
    it belongs to. Never from a ContextVar a middleware set, which names the
    request that opened the session. A message no HTTP request carried (stdio,
    an in-process client) is local. caller is current_caller(), already a digest.
    The header, peer and clientInfo values are RAW; observability reduces them
    to fixed classes, digests or a coarse prefix before anything reaches a span.
    """
    from mcp.server.lowlevel.server import request_ctx

    try:
        context = request_ctx.get()
    except LookupError:
        return None
    session = getattr(context, "session", None)
    client_info = getattr(getattr(session, "client_params", None), "clientInfo", None)
    client_name = getattr(client_info, "name", None)
    client_version = getattr(client_info, "version", None)
    # Through the module global so a test that patches current_caller, and the
    # admission count, share one name with the span's user_Id.
    caller = current_caller()
    request = getattr(context, "request", None)
    scope = getattr(request, "scope", None)
    if not isinstance(scope, dict):
        # No HTTP request carried this message: a stdio or in-process client. Not
        # the transport marker AuthMiddleware sets either, because a session task
        # inherits that from the request that opened the session.
        return observability.CallerFacts(
            transport="local",
            auth="local",
            caller=caller,
            client_name=client_name,
            client_version=client_version,
        )
    state = scope.get("state")
    principal = state.get(REQUEST_PRINCIPAL_STATE) if isinstance(state, dict) else None
    headers = getattr(request, "headers", None)
    peer = scope.get("client")
    client_addr = peer[0] if type(peer) in (tuple, list) and peer else None
    return observability.CallerFacts(
        transport="streamable_http",
        auth=principal.method if isinstance(principal, RequestPrincipal) else "none",
        host=headers.get("host") if headers is not None else None,
        user_agent=headers.get("user-agent") if headers is not None else None,
        origin=headers.get("origin") if headers is not None else None,
        client_name=client_name,
        client_version=client_version,
        caller=caller,
        user_id=principal.user_id if isinstance(principal, RequestPrincipal) else None,
        platform=principal.platform if isinstance(principal, RequestPrincipal) else None,
        session_id=headers.get("mcp-session-id") if headers is not None else None,
        client_addr=client_addr,
        x_real_ip=headers.get("x-real-ip") if headers is not None else None,
    )


def _client_for_key(api_key: str) -> SugraClient:
    client = _per_key_clients.get(api_key)
    if client is None:
        client = _build_client(api_key)
        _per_key_clients[api_key] = client
    return client


def get_client() -> SugraClient | _KeylessClient:
    """Return the downstream HTTP client for the current request.

    HTTP transport: the key comes only from the request that carries the tool
    call, stored on its scope state by ``AuthMiddleware``. Without one the call
    gets the keyless stand-in: never the session opener's key and never the
    SUGRA_API_KEY fallback. A dispatch that belongs to the HTTP transport
    (``http_transport_ctx``) but carries no request is refused the same way. We
    cache one client per distinct key to keep the httpx.AsyncClient alive
    across calls.

    stdio transport / in-process callers: ``api_key_ctx`` when set, else
    SUGRA_API_KEY from env. When that is empty too, return the keyless stand-in
    whose network methods answer with the structured ``missing_api_key`` error -
    the key requirement is enforced here at call time, never at process startup.
    """
    is_http_request, request_key = _dispatching_http_request()
    if is_http_request:
        return _client_for_key(request_key) if request_key else _keyless_client
    if http_transport_ctx.get():
        return _keyless_client

    per_request_key = api_key_ctx.get()
    if per_request_key:
        return _client_for_key(per_request_key)

    global _shared_client
    if _shared_client is None:
        config = load_config()
        if not config.api_key:
            return _keyless_client
        _shared_client = SugraClient(config)
    return _shared_client
