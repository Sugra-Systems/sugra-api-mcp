"""FastMCP server instance, tool annotation helper, and shared client accessor."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from contextvars import ContextVar
from copy import deepcopy
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
# that renders an interactive widget. Its template declaration is attached in
# SugraFastMCP.list_tools via _meta.ui.resourceUri (spec section "Resource
# Discovery"; the flat "ui/resourceUri" key is deprecated).
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


def _with_ui_template(tool: MCPTool) -> MCPTool:
    """Attach the MCP Apps template declaration (SEP-1865 "Resource Discovery").

    Only UI_TEMPLATE_TOOL renders a widget: _meta.ui.resourceUri points at
    the predeclared ui:// template resource served by tools/widgets.py. The
    import is lazy because the tools package imports this module at load time.
    """
    if tool.name != UI_TEMPLATE_TOOL:
        return tool
    from .tools.widgets import PRICE_CHART_URI

    payload = tool.model_dump(by_alias=True, exclude_none=True)
    meta = dict(payload.get("_meta") or {})
    meta["ui"] = {"resourceUri": PRICE_CHART_URI}
    payload["_meta"] = meta
    return MCPTool.model_validate(payload)


class SugraFastMCP(FastMCP):
    """FastMCP with OAuth tool metadata and the SEP-1865 UI template meta.

    Also pins serverInfo.version to the package version: FastMCP never
    forwards a version to the lowlevel server, whose initialize response then
    falls back to the MCP SDK version instead of ours.
    """

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._mcp_server.version = __version__

    async def list_tools(self) -> list[MCPTool]:
        return [
            _with_ui_template(_with_oauth_security(tool))
            for tool in await super().list_tools()
        ]

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> Any:
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
        # MCP-10 (audit P1-4): ONE end-to-end budget for the whole call.
        # Without it, a wedged upstream held the session past every client's
        # read timeout and the typed envelope never reached the agent; the
        # asyncio scope also CANCELS the outbound request instead of letting
        # it complete server-side after the caller has given up.
        # codex r2: the budget is END-TO-END - auth already consumed part
        # of it. The middleware stamps the request start; what remains (with
        # a small floor so a slow-auth call still gets a real attempt) is the
        # tool budget. Stdio transport has no middleware stamp - full budget.
        # MCP-17: the budget is the tool's own, and auth is bounded separately.
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
        # MCP-19.1: publish the timeout that bounds this dispatch, with the
        # task's cancellation count at entry, so the tool's span can ask
        # whether the budget owns a cancellation and name it deadline_exceeded
        # instead of a bare cancelled (the timeout's own state, never a clock
        # comparison - see observability.DispatchBudget for the ways a clock
        # or expired() alone misfile). Set and reset around ONE dispatch on
        # this task: it never inherits across calls the way the MCP-17 stamp
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


def get_client() -> SugraClient | _KeylessClient:
    """Return the downstream HTTP client for the current request.

    HTTP transport: ``api_key_ctx`` is set per-request by ``AuthMiddleware`` after
    validating the Bearer token. We cache one client per distinct key to keep
    the httpx.AsyncClient alive across calls.

    stdio transport / no middleware: fall back to SUGRA_API_KEY from env. When
    that is empty too, return the keyless stand-in whose network methods answer
    with the structured ``missing_api_key`` error - the key requirement is
    enforced here at call time, never at process startup.
    """
    per_request_key = api_key_ctx.get()
    if per_request_key:
        client = _per_key_clients.get(per_request_key)
        if client is None:
            client = _build_client(per_request_key)
            _per_key_clients[per_request_key] = client
        return client

    global _shared_client
    if _shared_client is None:
        config = load_config()
        if not config.api_key:
            return _keyless_client
        _shared_client = SugraClient(config)
    return _shared_client
