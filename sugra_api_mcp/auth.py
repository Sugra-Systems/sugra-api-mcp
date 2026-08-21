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
from concurrent.futures import ThreadPoolExecutor
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
# MCP-10: how long a PASSING activity validation is trusted per token jti.
# Short on purpose - it coarsens the activity heartbeat, never the denial
# path (failures are not cached).
ACCESS_VALIDATION_TTL_SECONDS = 60.0

MAX_PUBLIC_MCP_CONTENT_LENGTH = 64 * 1024

MAX_PUBLIC_MCP_BATCH_ITEMS = 16

PUBLIC_MCP_METHODS = frozenset(
    {
        "initialize",
        "notifications/initialized",
        "tools/list",
        "resources/list",
        "prompts/list",
        "ping",
    }
)

# Unauthenticated GET/HEAD surface of the hosted app: the human landing page on
# the host root and the liveness probe. STRICT exact-path allowlist (no slash
# normalization: /health/ and // variants deliberately stay behind auth and
# 401 rather than redirect). Handlers live in sugra_api_mcp.web; the two lists
# must stay in sync.
PUBLIC_GET_PATHS = frozenset({"/", "/health"})


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
        # MCP-10 (audit P1-5): per-user single-flight locks. The old single
        # Authenticator-wide lock was HELD ACROSS the internal HTTP call, so
        # every user's cold-cache lookup serialized behind every other's.
        self._user_locks: dict[int, asyncio.Lock] = {}
        # PyJWKClient does SYNCHRONOUS network I/O; on the event loop it
        # stalled every request in the process (including raw sugra_ keys)
        # whenever the key cache was cold. A DEDICATED executor keeps the
        # offload from competing with the default pool (fleet review rule).
        self._jwks_executor = ThreadPoolExecutor(
            max_workers=2, thread_name_prefix="jwks")
        # Short-TTL cache of a PASSING activity validation per token jti:
        # the check ran an internal HTTP round-trip on EVERY tool call.
        # Failures are never cached; the TTL only coarsens the activity
        # heartbeat, not the access decision it grants.
        self._access_cache: dict[str, float] = {}
        # One pooled client for all internal calls (a fresh client per call
        # paid TCP+TLS setup on every tool invocation).
        self._http = httpx.AsyncClient(timeout=INTERNAL_HTTP_TIMEOUT_SECONDS)

    async def aclose(self) -> None:
        self._jwks_executor.shutdown(wait=False, cancel_futures=True)
        await self._http.aclose()

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

        t0 = time.monotonic()
        loop = asyncio.get_running_loop()
        claims = await loop.run_in_executor(
            self._jwks_executor, self._validate_jwt, token)
        t1 = time.monotonic()
        await self._validate_mcp_access(claims)
        t2 = time.monotonic()
        api_key = await self._lookup_api_key(claims.user_id)
        t3 = time.monotonic()
        # MCP-10 stage attribution: the audit observed 45-120s holds that no
        # log could attribute to a stage. One structured line per JWT resolve
        # (logs, not span attributes - the observability privacy contract
        # allowlists span dimensions, and these are operational timings).
        logger.info(
            "auth_stages user_id=%d jwks_ms=%d activity_ms=%d key_ms=%d",
            claims.user_id,
            int((t1 - t0) * 1000),
            int((t2 - t1) * 1000),
            int((t3 - t2) * 1000),
        )
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

        # Per-user single-flight: concurrent requests for the SAME user share
        # one fetch; different users never wait on each other. setdefault is
        # atomic under the GIL, so the occasional extra Lock object is inert.
        async with self._user_locks.setdefault(user_id, asyncio.Lock()):
            cached = self._api_key_cache.get(user_id)
            if cached and cached.expires_at > now:
                return cached.api_key

            url = f"{self._config.app_url}/api/internal/user/{user_id}/primary-api-key"
            resp = await self._http.get(
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

        # jti-TTL cache: a PASS within the window skips the round-trip.
        now = time.time()
        expires = self._access_cache.get(claims.access_token_id)
        if expires is not None and expires > now:
            return

        url = f"{self._config.app_url}/api/internal/mcp/activity"
        try:
            resp = await self._http.post(
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
            self._access_cache[claims.access_token_id] = (
                now + ACCESS_VALIDATION_TTL_SECONDS)
            # The cache only ever holds passes; prune opportunistically so a
            # long-lived process does not accumulate dead jtis.
            if len(self._access_cache) > 4096:
                self._access_cache = {
                    jti: exp for jti, exp in self._access_cache.items()
                    if exp > now
                }
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

    async def _is_public_mcp_request(self, request: Request) -> bool:
        if request.method != "POST" or request.url.path.rstrip("/") != "/mcp":
            return False

        content_length = request.headers.get("content-length")
        if content_length is None:
            return False
        try:
            if int(content_length) > MAX_PUBLIC_MCP_CONTENT_LENGTH:
                return False
        except ValueError:
            return False

        try:
            payload = await request.json()
        except ValueError:
            return False

        if isinstance(payload, dict):
            method = payload.get("method")
            return isinstance(method, str) and method in PUBLIC_MCP_METHODS

        if isinstance(payload, list) and 0 < len(payload) <= MAX_PUBLIC_MCP_BATCH_ITEMS:
            for item in payload:
                if not isinstance(item, dict):
                    return False
                method = item.get("method")
                if not isinstance(method, str) or method not in PUBLIC_MCP_METHODS:
                    return False
            return True

        return False

    async def dispatch(self, request: Request, call_next) -> Response:  # type: ignore[override]
        if (
            request.method in ("GET", "HEAD")
            and request.url.path in PUBLIC_GET_PATHS
        ):
            return await call_next(request)

        header = request.headers.get("authorization", "")
        if not header:
            if await self._is_public_mcp_request(request):
                return await call_next(request)
            return JSONResponse(
                {"error": "missing_bearer_token"},
                status_code=401,
                headers=self._auth_headers(),
            )

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
