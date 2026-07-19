"""Configuration loading from environment variables."""

from __future__ import annotations

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


@dataclass(frozen=True)
class Config:
    api_base: str
    api_key: str
    timeout: float


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
    )


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
