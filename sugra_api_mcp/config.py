"""Configuration loading from environment variables."""

from __future__ import annotations

import math
import os
from dataclasses import dataclass

# Remediation copy for the call-time missing_api_key error. Shared by the
# keyless stand-in client (server.py) and the CLI doctor warning (__main__.py).
MISSING_API_KEY_HINT = (
    "Set SUGRA_API_KEY. Get one free at https://app.sugra.ai/settings/billing"
)

DEFAULT_ALLOWED_ORIGINS: tuple[str, ...] = (
    "https://chatgpt.com",
    "https://chat.openai.com",
    "https://platform.openai.com",
    "https://claude.ai",
    "https://claude.com",
    "https://cursor.sh",
    "https://app.cursor.sh",
)

# MCP-24.1: the opt-in switch for the MCP Apps price-chart widget, and the
# only values that turn it on (compared after strip + lower).
UI_WIDGETS_ENV = "SUGRA_MCP_UI_WIDGETS"
_TRUTHY_ENV_VALUES = frozenset({"1", "true", "yes", "on"})


@dataclass(frozen=True)
class Config:
    api_base: str
    api_key: str
    timeout: float
    tool_deadline: float = 40.0


@dataclass(frozen=True)
class AuthConfig:
    """Settings for OAuth / JWT validation on the HTTP transport."""

    app_url: str
    jwks_url: str
    internal_token: str | None


def load_config(*, require_api_key: bool = True) -> Config:
    """Load main client config.

    An empty SUGRA_API_KEY is always allowed here: startup must never fail on
    a missing key. MCP clients and directory evaluators launch the server
    without env configured and expect initialize plus tools/list introspection
    to work before the user supplies credentials. The key requirement is
    enforced at call time instead - ``server.get_client`` hands out a stand-in
    client whose network methods return the structured ``missing_api_key``
    error when no key is available.

    ``require_api_key`` is kept for signature compatibility and no longer
    triggers a raise. On the HTTP transport the auth middleware supplies a
    per-request key resolved from the Bearer token, exactly as before.
    """
    del require_api_key  # retained for signature compatibility only
    api_key = os.environ.get("SUGRA_API_KEY", "").strip()
    return Config(
        api_base=os.environ.get("SUGRA_API_BASE", "https://sugra.ai").rstrip("/"),
        api_key=api_key,
        timeout=float(os.environ.get("SUGRA_TIMEOUT", "30")),
        # MCP-10 (audit P1-4): end-to-end budget for ONE tool call, wrapped
        # around dispatch in SugraFastMCP.call_tool. Must sit BELOW common
        # client read timeouts (the audit harness cut at ~45s while the
        # gateway kept working to its 60s outbound budget, so the typed
        # timeout envelope never reached the agent).
        tool_deadline=_positive_seconds("SUGRA_TOOL_DEADLINE", "40"),
    )


# The lowest tool timeout an MCP client is documented to use: the canonical
# timeout chain records clients cutting at 60-180s. The server must be able to
# answer with its typed envelope before the earliest of those, so the whole
# server-side path - auth plus dispatch - has to fit underneath this.
CLIENT_TIMEOUT_FLOOR_SECONDS = 60.0


def validate_startup_budgets() -> None:
    """Refuse a configuration whose server-side path can outlive the client.

    MCP-17 (codex F4/F5). The tool budget bounds DISPATCH, and auth is bounded
    separately by AuthMiddleware, so the server-side worst case is the SUM of
    the two, reached only on a cold auth. That sum is what has to stay under
    the client's own cut, or the typed timeout envelope never arrives and the
    caller sees a dead connection instead - the exact failure the budget was
    introduced to prevent.

    Called from both transports at startup. Previously nothing called
    load_config on the startup path at all, so a nonpositive budget started
    happily and surfaced as an unstructured HTTP 500 from inside the auth
    middleware on the first authenticated request.
    """
    from .auth import AUTH_BUDGET_SECONDS

    total = load_config(require_api_key=False).tool_deadline
    worst_case = total + AUTH_BUDGET_SECONDS
    if worst_case > CLIENT_TIMEOUT_FLOOR_SECONDS:
        raise ValueError(
            f"SUGRA_TOOL_DEADLINE={total:g}s plus the {AUTH_BUDGET_SECONDS:g}s "
            f"auth budget is {worst_case:g}s, past the "
            f"{CLIENT_TIMEOUT_FLOOR_SECONDS:g}s floor of documented client "
            "timeouts. A client would cut the connection before the timeout "
            "envelope arrives."
        )


def _positive_seconds(var: str, default: str) -> float:
    """Parse a seconds budget, refusing values that cannot bound anything.

    MCP-17 (codex F2): the budget went straight to asyncio.timeout. Zero
    cancelled every call the instant it started and a negative value did the
    same, both silently - the operator saw tools that "always time out" with
    no hint that the configuration was the cause.
    """
    raw = os.environ.get(var, default).strip() or default
    try:
        value = float(raw)
    except ValueError as exc:
        raise ValueError(f"{var} must be a number of seconds, got {raw!r}") from exc
    if not math.isfinite(value) or value <= 0:
        raise ValueError(
            f"{var} must be a finite positive number of seconds, got {raw!r}")
    return value


def load_auth_config() -> AuthConfig:
    """Load OAuth / internal-lookup settings for the HTTP transport."""
    app_url = os.environ.get("SUGRA_APP_URL", "https://app.sugra.ai").rstrip("/")
    jwks_url = os.environ.get("SUGRA_JWKS_URL", f"{app_url}/oauth/jwks.json")
    internal_token = os.environ.get("INTERNAL_API_TOKEN", "").strip() or None
    return AuthConfig(app_url=app_url, jwks_url=jwks_url, internal_token=internal_token)


def load_allowed_origins() -> list[str]:
    """Load CORS allowed origins for the HTTP transport.

    Browser-based MCP clients (ChatGPT Connectors UI) send a CORS preflight
    before the actual MCP request. Without an exact-match origin in the
    response, the browser blocks the call and the connector add flow fails
    silently. Server-to-server clients (claude.ai backend, Codex CLI, stdio
    Claude Desktop) ignore CORS entirely, which is why this only matters
    for the hosted HTTP endpoint.

    `SUGRA_MCP_ALLOWED_ORIGINS` (comma-separated) overrides the default. A
    value of `*` allows any origin; only safe because hosted access is gated
    by Bearer token rather than browser cookies.
    """
    raw = os.environ.get("SUGRA_MCP_ALLOWED_ORIGINS", "").strip()
    if not raw:
        return list(DEFAULT_ALLOWED_ORIGINS)
    if raw == "*":
        return ["*"]
    return [origin.strip() for origin in raw.split(",") if origin.strip()]


def ui_widgets_enabled() -> bool:
    """Whether the MCP Apps price-chart widget is served at all.

    MCP-24.1. On only when SUGRA_MCP_UI_WIDGETS is 1, true, yes or on, in any
    case and with surrounding whitespace ignored. Unset, empty and every other
    value mean off. Off is the default because the widget is not ready for an
    app-directory review, and a directory scan of the server must find no UI:
    with the flag off the ui:// template is not registered and no tool
    declares it.

    Read once per process, when tools/widgets.py registers (see
    register_ui_widgets there), so a change takes a restart.
    """
    return os.environ.get(UI_WIDGETS_ENV, "").strip().lower() in _TRUTHY_ENV_VALUES
