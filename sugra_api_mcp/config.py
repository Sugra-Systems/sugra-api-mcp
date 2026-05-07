"""Configuration loading from environment variables."""

from __future__ import annotations

import os
from dataclasses import dataclass


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

    When ``require_api_key`` is False (HTTP transport with OAuth), an empty
    SUGRA_API_KEY is acceptable - the auth middleware will supply a per-request
    key resolved from the Bearer token.
    """
    api_key = os.environ.get("SUGRA_API_KEY", "").strip()
    if require_api_key and not api_key:
        raise RuntimeError(
            "SUGRA_API_KEY environment variable is required. "
            "Get one free at https://app.sugra.ai/settings/billing"
        )
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
