"""FastMCP server instance, tool annotation helper, and shared client accessor."""

from __future__ import annotations

import os
from contextvars import ContextVar
from copy import deepcopy
from typing import Any

from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import Tool as MCPTool
from mcp.types import ToolAnnotations

from .client import SugraClient
from .config import Config, load_config

api_key_ctx: ContextVar[str | None] = ContextVar("sugra_api_key", default=None)

OAUTH_SCOPES = ["sugra:read", "offline_access"]

OAUTH_SECURITY_SCHEMES: list[dict[str, Any]] = [
    {"type": "oauth2", "scopes": OAUTH_SCOPES},
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
    """FastMCP with OAuth tool metadata required by ChatGPT Apps."""

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
    """
    raw = os.environ.get("SUGRA_MCP_ALLOWED_HOSTS", "").strip()
    if not raw:
        return None
    hosts = [h.strip() for h in raw.split(",") if h.strip()]
    return TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=[*hosts, "127.0.0.1:*", "localhost:*", "[::1]:*"],
        allowed_origins=[
            *[f"https://{h}" for h in hosts],
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
