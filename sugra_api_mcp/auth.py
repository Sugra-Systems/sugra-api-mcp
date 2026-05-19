"""Per-request authentication for the HTTP transport.

Accepts two Bearer token formats:

- Raw Sugra API key (``sugra_...``) - V1 back-compat, used as x-api-key downstream
- JWT issued by Passport at https://app.sugra.ai/oauth/authorize - validates the
  signature, audience, scope, and hosted access status, then looks up the user's
  primary API key via app.sugra.ai internal endpoints

On success the resolved x-api-key is stored in a ContextVar that
``sugra_api_mcp.server.get_client`` reads when building downstream requests.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass

import httpx
import jwt
from jwt import PyJWKClient
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.types import ASGIApp

from .config import AuthConfig
from .server import api_key_ctx

logger = logging.getLogger("sugra_mcp.auth")

SIGNING_ALGORITHMS = ["RS256"]

REQUIRED_SCOPE = "sugra:read"

API_KEY_CACHE_TTL_SECONDS = 300

INTERNAL_HTTP_TIMEOUT_SECONDS = 10.0


class AuthError(Exception):
    def __init__(self, message: str, *, status: int = 401) -> None:
        super().__init__(message)
        self.status = status


@dataclass(frozen=True)
class _CachedKey:
    api_key: str
    expires_at: float


@dataclass(frozen=True)
class ResolvedAuth:
    api_key: str
    user_id: int | None = None
    access_token_id: str | None = None


@dataclass(frozen=True)
class _JwtClaims:
    user_id: int
    access_token_id: str


class Authenticator:
    """Resolves a Bearer token to a downstream x-api-key."""

    def __init__(self, config: AuthConfig) -> None:
        self._config = config
        self._jwks = PyJWKClient(config.jwks_url, cache_keys=True, lifespan=3600)
        self._api_key_cache: dict[int, _CachedKey] = {}
        self._lock = asyncio.Lock()

    @property
    def protected_resource_metadata_url(self) -> str:
        return f"{self._config.app_url}/.well-known/oauth-protected-resource"

    @property
    def audience(self) -> str:
        return f"{self._config.app_url}/mcp"

    async def resolve(self, token: str) -> ResolvedAuth:
        token = token.strip()
        if not token:
            raise AuthError("Empty token")

        if token.startswith("sugra_"):
            return ResolvedAuth(api_key=token)

        claims = self._validate_jwt(token)
        await self._validate_mcp_access(claims)
        api_key = await self._lookup_api_key(claims.user_id)
        resolved = ResolvedAuth(
            api_key=api_key,
            user_id=claims.user_id,
            access_token_id=claims.access_token_id,
        )
        return resolved

    def _validate_jwt(self, token: str) -> _JwtClaims:
        try:
            signing_key = self._jwks.get_signing_key_from_jwt(token)
        except Exception:
            # Passport / league-oauth2-server issues JWTs without a `kid`
            # header, so kid-based lookup fails. Fall back to the sole key
            # published in our JWKS while we use a single signing key.
            try:
                keys = list(self._jwks.get_signing_keys())
            except Exception as e:
                raise AuthError(f"Unable to load signing keys: {e}") from e
            if len(keys) != 1:
                raise AuthError("No unique signing key available in JWKS") from None
            signing_key = keys[0]

        try:
            decoded = jwt.decode(
                token,
                signing_key.key,
                algorithms=SIGNING_ALGORITHMS,
                audience=self.audience,
            )
        except jwt.ExpiredSignatureError as e:
            raise AuthError("Token expired") from e
        except jwt.InvalidTokenError as e:
            raise AuthError(f"Invalid token: {e}") from e

        issuer = decoded.get("iss")
        if issuer is not None and issuer != self._config.app_url:
            raise AuthError("Invalid token issuer")

        sub = decoded.get("sub")
        if sub is None:
            raise AuthError("Token missing sub claim")
        self._validate_scopes(decoded)
        try:
            user_id = int(sub)
        except (TypeError, ValueError) as e:
            raise AuthError("Token sub is not an integer user id") from e
        access_token_id = decoded.get("jti")
        if not access_token_id:
            raise AuthError("Token missing jti claim")
        return _JwtClaims(
            user_id=user_id,
            access_token_id=str(access_token_id),
        )

    def _validate_scopes(self, decoded: dict) -> None:
        scopes_claim = decoded.get("scopes")
        if scopes_claim is None:
            scopes_claim = decoded.get("scope", "")

        if isinstance(scopes_claim, str):
            scopes = set(scopes_claim.split())
        elif isinstance(scopes_claim, list):
            scopes = {str(scope) for scope in scopes_claim}
        else:
            scopes = set()

        if REQUIRED_SCOPE not in scopes:
            raise AuthError(f"Token missing required scope: {REQUIRED_SCOPE}")

    async def _lookup_api_key(self, user_id: int) -> str:
        now = time.time()
        cached = self._api_key_cache.get(user_id)
        if cached and cached.expires_at > now:
            return cached.api_key

        if not self._config.internal_token:
            raise AuthError("INTERNAL_API_TOKEN not configured on MCP server", status=500)

        async with self._lock:
            cached = self._api_key_cache.get(user_id)
            if cached and cached.expires_at > now:
                return cached.api_key

            url = f"{self._config.app_url}/api/internal/user/{user_id}/primary-api-key"
            async with httpx.AsyncClient(timeout=INTERNAL_HTTP_TIMEOUT_SECONDS) as client:
                resp = await client.get(
                    url,
                    headers={"X-Internal-Token": self._config.internal_token},
                )

            if resp.status_code == 404:
                body = resp.json() if resp.headers.get("content-type", "").startswith("application/json") else {}
                raise AuthError(
                    body.get("message")
                    or "User has no API key. Create one at https://app.sugra.ai/settings",
                    status=403,
                )
            if resp.status_code >= 400:
                raise AuthError(f"Internal lookup failed: HTTP {resp.status_code}", status=502)

            data = resp.json()
            api_key = data["api_key"]
            self._api_key_cache[user_id] = _CachedKey(
                api_key=api_key,
                expires_at=now + API_KEY_CACHE_TTL_SECONDS,
            )
            return api_key

    async def _validate_mcp_access(self, claims: _JwtClaims) -> None:
        if not self._config.internal_token:
            raise AuthError("INTERNAL_API_TOKEN not configured on MCP server", status=500)

        url = f"{self._config.app_url}/api/internal/mcp/activity"
        try:
            async with httpx.AsyncClient(timeout=INTERNAL_HTTP_TIMEOUT_SECONDS) as client:
                resp = await client.post(
                    url,
                    headers={"X-Internal-Token": self._config.internal_token},
                    json={
                        "user_id": claims.user_id,
                        "access_token_id": claims.access_token_id,
                    },
                )
        except Exception as e:
            logger.info("mcp_access_validation_exception user_id=%d error=%s", claims.user_id, e)
            raise AuthError("Internal MCP access validation failed", status=502) from e

        if 200 <= resp.status_code < 300:
            return

        logger.info(
            "mcp_access_validation_failed user_id=%d status=%d",
            claims.user_id,
            resp.status_code,
        )

        if resp.status_code >= 500:
            raise AuthError(f"Internal MCP access validation failed: HTTP {resp.status_code}", status=502)

        try:
            body = resp.json()
        except ValueError:
            body = {}

        message = body.get("error") if isinstance(body, dict) else None
        raise AuthError(message or f"MCP access validation failed: HTTP {resp.status_code}", status=403)


class AuthMiddleware(BaseHTTPMiddleware):
    """Starlette middleware that resolves the Authorization header per request."""

    def __init__(self, app: ASGIApp, authenticator: Authenticator) -> None:
        super().__init__(app)
        self._auth = authenticator

    def _auth_headers(self) -> dict[str, str]:
        return {
            "WWW-Authenticate": (
                'Bearer resource_metadata="'
                f'{self._auth.protected_resource_metadata_url}"'
            )
        }

    async def dispatch(self, request: Request, call_next) -> Response:  # type: ignore[override]
        header = request.headers.get("authorization", "")
        if not header.lower().startswith("bearer "):
            return JSONResponse(
                {"error": "missing_bearer_token"},
                status_code=401,
                headers=self._auth_headers(),
            )

        token = header[7:].strip()
        try:
            resolved = await self._auth.resolve(token)
        except AuthError as e:
            token_prefix = token[:12] + "..." if len(token) > 12 else token
            logger.warning(
                "auth_failed status=%d token_prefix=%s msg=%s",
                e.status, token_prefix, e,
            )
            return JSONResponse(
                {"error": "auth_failed", "message": str(e)},
                status_code=e.status,
                headers=self._auth_headers() if e.status == 401 else None,
            )

        ctx_token = api_key_ctx.set(resolved.api_key)
        try:
            return await call_next(request)
        finally:
            api_key_ctx.reset(ctx_token)
