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
from concurrent.futures import ThreadPoolExecutor
from contextvars import ContextVar
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
# MCP-10 r2: hard bounds and flood protection for the auth layer.
ACCESS_CACHE_MAX_ENTRIES = 4096
# Prune BELOW the cap (agy r2): shrinking to exactly the cap re-runs the
# O(N log N) sweep on every subsequent miss.
ACCESS_CACHE_PRUNE_WATERMARK = ACCESS_CACHE_MAX_ENTRIES - 512
USER_LOCKS_PRUNE_THRESHOLD = 2048
JWKS_ADMISSION_SLOTS = 4
JWKS_ADMISSION_WAIT_SECONDS = 2.0
JWKS_FAILURE_COOLDOWN_SECONDS = 5.0
JWKS_FETCH_TIMEOUT_SECONDS = 5.0
# codex r2: the tool deadline started AFTER the auth path, so cold auth
# (JWKS + two internal calls) ran on top of the 40s budget. Auth gets its own
# bounded slice, and the request start is stamped so the tool budget consumes
# only what REMAINS of the total.
AUTH_BUDGET_SECONDS = 15.0

# Stamped by AuthMiddleware at request entry; SugraFastMCP.call_tool reads it
# to compute the remaining budget. ContextVar so concurrent requests never
# see each other's clock.
request_started_at: ContextVar[float | None] = ContextVar(
    "sugra_request_started_at", default=None)

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
        self._jwks = PyJWKClient(
            config.jwks_url, cache_keys=True, lifespan=3600,
            timeout=JWKS_FETCH_TIMEOUT_SECONDS)
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
        # r2 (codex+agy): flood protection. Admission to the executor is
        # bounded (a queue of malformed/unique-kid bearers must not bury
        # legitimate JWTs), and a failing JWKS endpoint puts the whole JWT
        # path on a short cooldown instead of hammering the pool.
        self._jwks_gate = asyncio.Semaphore(JWKS_ADMISSION_SLOTS)
        self._jwks_failed_at = 0.0
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

        # r2 (codex): a malformed bearer must fail HERE, on the loop, at
        # parse cost - never occupy a JWKS executor slot.
        try:
            header = jwt.get_unverified_header(token)
        except jwt.InvalidTokenError as e:
            raise AuthError(f"Malformed token: {e}") from e
        has_kid = bool(header.get("kid"))

        # r2 (agy): a failing JWKS endpoint cooldowns the whole JWT path.
        if time.monotonic() - self._jwks_failed_at < JWKS_FAILURE_COOLDOWN_SECONDS:
            raise AuthError("Signing keys temporarily unavailable", status=503)

        # MCP-10 stage attribution: the audit observed 45-120s holds no log
        # could attribute to a stage. The line lands in a finally (codex r2:
        # the degraded paths this exists for exited before the old log).
        stages = {"jwks_ms": -1, "activity_ms": -1, "key_ms": -1}
        outcome = "ok"
        user_for_log = -1
        try:
            t0 = time.monotonic()
            try:
                await asyncio.wait_for(
                    self._jwks_gate.acquire(),
                    timeout=JWKS_ADMISSION_WAIT_SECONDS)
            except TimeoutError:
                outcome = "jwks_busy"
                raise AuthError(
                    "Authentication is briefly overloaded, retry",
                    status=503) from None
            try:
                loop = asyncio.get_running_loop()
                claims = await loop.run_in_executor(
                    self._jwks_executor, self._validate_jwt, token, has_kid)
            except AuthError as e:
                if e.status == 503:
                    self._jwks_failed_at = time.monotonic()
                outcome = "jwt_invalid"
                raise
            finally:
                self._jwks_gate.release()
                stages["jwks_ms"] = int((time.monotonic() - t0) * 1000)
            user_for_log = claims.user_id
            t1 = time.monotonic()
            try:
                await self._validate_mcp_access(claims)
            except AuthError:
                outcome = "activity_denied"
                raise
            finally:
                stages["activity_ms"] = int((time.monotonic() - t1) * 1000)
            t2 = time.monotonic()
            try:
                api_key = await self._lookup_api_key(claims.user_id)
            except AuthError:
                outcome = "key_lookup_failed"
                raise
            finally:
                stages["key_ms"] = int((time.monotonic() - t2) * 1000)
        finally:
            logger.info(
                "auth_stages outcome=%s user_id=%d jwks_ms=%d "
                "activity_ms=%d key_ms=%d",
                outcome, user_for_log, stages["jwks_ms"],
                stages["activity_ms"], stages["key_ms"],
            )
        resolved = ResolvedAuth(
            api_key=api_key,
            user_id=claims.user_id,
            access_token_id=claims.access_token_id,
        )
        return resolved

    def _validate_jwt(self, token: str, has_kid: bool = False) -> _JwtClaims:
        # r2 (codex): the fallback exists ONLY for the expected no-kid case
        # (Passport / league-oauth2-server). A token WITH a kid that fails
        # lookup is invalid - retrying the whole key list on it let malformed
        # traffic double its JWKS I/O.
        if has_kid:
            try:
                signing_key = self._jwks.get_signing_key_from_jwt(token)
            except jwt.exceptions.PyJWKClientConnectionError as e:
                # agy r2: an unreachable JWKS on the STANDARD path must be a
                # 503 - it is what arms the failure cooldown.
                raise AuthError(
                    f"Unable to load signing keys: {e}", status=503) from e
            except Exception as e:
                raise AuthError(f"Unknown signing key: {e}") from e
        else:
            try:
                keys = list(self._jwks.get_signing_keys())
            except Exception as e:
                raise AuthError(
                    f"Unable to load signing keys: {e}", status=503) from e
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
        # agy r2: the lock table is pruned once it grows past the
        # threshold - an idle lock nobody holds is safe to drop (a racing
        # setdefault simply creates a fresh one).
        if len(self._user_locks) > USER_LOCKS_PRUNE_THRESHOLD:
            self._user_locks = {
                uid: lock for uid, lock in self._user_locks.items()
                if lock.locked()
            }
        async with self._user_locks.setdefault(user_id, asyncio.Lock()):
            cached = self._api_key_cache.get(user_id)
            if cached and cached.expires_at > now:
                return cached.api_key

            url = f"{self._config.app_url}/api/internal/user/{user_id}/primary-api-key"
            try:
                resp = await self._http.get(
                    url,
                    headers={"X-Internal-Token": self._config.internal_token},
                )
            except Exception as e:  # httpx transport class - mirror activity
                raise AuthError(
                    f"Internal lookup failed: {e.__class__.__name__}",
                    status=502) from e

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
            # The cache only ever holds passes. HARD bound (agy r2): pruning
            # expired entries alone cannot shrink a cache full of LIVE jtis,
            # and re-running an O(N) comprehension per auth is itself the
            # DoS. Keep the newest entries by expiry when over the cap.
            if len(self._access_cache) > ACCESS_CACHE_MAX_ENTRIES:
                live = {jti: exp for jti, exp in self._access_cache.items()
                        if exp > now}
                if len(live) > ACCESS_CACHE_PRUNE_WATERMARK:
                    live = dict(sorted(
                        live.items(), key=lambda kv: kv[1],
                    )[-ACCESS_CACHE_PRUNE_WATERMARK:])
                self._access_cache = live
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
        request_started_at.set(time.monotonic())
        try:
            async with asyncio.timeout(AUTH_BUDGET_SECONDS):
                resolved = await self._auth.resolve(token)
        except TimeoutError:
            return JSONResponse(
                {"error": "auth_timeout",
                 "message": (
                     f"Authentication exceeded its {AUTH_BUDGET_SECONDS:.0f}s "
                     "budget and was cancelled."
                 )},
                status_code=503,
                headers=self._auth_headers(),
            )
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
