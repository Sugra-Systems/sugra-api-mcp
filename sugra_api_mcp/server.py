"""FastMCP server instance, tool annotation helper, and shared client accessor."""

from __future__ import annotations

import logging
import os
from contextvars import ContextVar
from copy import deepcopy
from typing import Any

from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import Icon, ToolAnnotations
from mcp.types import Tool as MCPTool

from . import __version__
from .client import SugraClient
from .config import Config, load_allowed_origins, load_config

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


def _oauth_security_schemes() -> list[dict[str, Any]]:
    return deepcopy(OAUTH_SECURITY_SCHEMES)


def _with_oauth_security(tool: MCPTool) -> MCPTool:
    payload = tool.model_dump(by_alias=True, exclude_none=True)
    payload["securitySchemes"] = _oauth_security_schemes()
    meta = dict(payload.get("_meta") or {})
    meta["securitySchemes"] = _oauth_security_schemes()
    payload["_meta"] = meta
    return MCPTool.model_validate(payload)


class SugraFastMCP(FastMCP):
    """FastMCP with OAuth tool metadata required by ChatGPT Apps.

    Also pins serverInfo.version to the package version: FastMCP never
    forwards a version to the lowlevel server, whose initialize response then
    falls back to the MCP SDK version instead of ours.
    """

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._mcp_server.version = __version__

    async def list_tools(self) -> list[MCPTool]:
        return [_with_oauth_security(tool) for tool in await super().list_tools()]


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


def _build_client(api_key: str) -> SugraClient:
    return SugraClient(
        Config(
            api_base=os.environ.get("SUGRA_API_BASE", "https://sugra.ai").rstrip("/"),
            api_key=api_key,
            timeout=float(os.environ.get("SUGRA_TIMEOUT", "30")),
        )
    )


def get_client() -> SugraClient:
    """Return the downstream HTTP client for the current request.

    HTTP transport: ``api_key_ctx`` is set per-request by ``AuthMiddleware`` after
    validating the Bearer token. We cache one client per distinct key to keep
    the httpx.AsyncClient alive across calls.

    stdio transport / no middleware: fall back to SUGRA_API_KEY from env.
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
        _shared_client = SugraClient(load_config())
    return _shared_client
