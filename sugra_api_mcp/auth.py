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
import hashlib
import hmac
import logging
import os
import re
import secrets
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

from .client import shared_ssl_context
from .config import AuthConfig, caller_host
from .observability import _UA_PATTERNS, _host_class, _origin_class, _text_class
from .server import api_key_ctx
from .skills_index import PUBLIC_PATHS as SKILLS_PUBLIC_PATHS

logger = logging.getLogger("sugra_mcp.auth")

SIGNING_ALGORITHMS = ["RS256"]

REQUIRED_SCOPE = "sugra:read"

# The hosted MCP's second name. On it, the APP's metadata document names
# https://mcp.sugra.ai/mcp, so the challenge sends a client there: RFC 9728
# section 3.3 has a client check the document's resource against the URL it
# used. Tied to the hosted APP, whose tokens carry its own audience on both
# hosts; any other host or APP keeps the APP's root document.
MCP_HOST_APP_URL = "https://app.sugra.ai"
MCP_HOST = "mcp.sugra.ai"
MCP_HOST_METADATA_URL = "https://mcp.sugra.ai/.well-known/oauth-protected-resource/mcp"

API_KEY_CACHE_TTL_SECONDS = 300

INTERNAL_HTTP_TIMEOUT_SECONDS = 10.0
# How long a PASSING activity validation is trusted per token jti.
# Short on purpose - it coarsens the activity heartbeat, never the denial
# path (failures are not cached).
ACCESS_VALIDATION_TTL_SECONDS = 60.0
# Hard bounds and flood protection for the auth layer.
ACCESS_CACHE_MAX_ENTRIES = 4096
# Prune BELOW the cap: shrinking to exactly the cap re-runs the
# O(N log N) sweep on every subsequent miss.
ACCESS_CACHE_PRUNE_WATERMARK = ACCESS_CACHE_MAX_ENTRIES - 512
# When the APP access check fails on the APP's side (a transport error or a
# 5xx, as for the seconds an APP restart takes), it is tried again after a
# pause that doubles from the first value up to the cap (1, 2, 3, 3 ... s), for
# as long as the auth budget still holds the pause, the minimum attempt and the
# reserve; with the 15 s budget that rides out a restart of about 12 s. Every
# attempt is cut where the reserve begins. Retrying never changes who gets in:
# any other APP answer (1xx, 3xx, 4xx, 600 and up) is a refusal and final, also
# when its body then fails to arrive.
ACCESS_RETRY_PAUSE_SECONDS = 1.0
ACCESS_RETRY_MAX_PAUSE_SECONDS = 3.0
ACCESS_RETRY_MIN_ATTEMPT_SECONDS = 1.0
ACCESS_RETRY_RESERVE_SECONDS = 1.0
# Request extension under which the access check's response hook records the
# status as soon as the headers arrive.
ACTIVITY_STATUS_EXTENSION = "sugra_activity_status"
# Same set as APP McpConnectionService::ALLOWED_PLATFORMS. Anything else is
# dropped, never copied onto a span.
ACCESS_PLATFORMS: frozenset[str] = frozenset({
    "openai", "anthropic", "cursor", "google", "xai", "custom",
})
# The APP activity verdicts on the presented token or its connection
# (McpConnectionService::recordMcpActivity). Each one means the client must
# sign in again, so it answers 401 invalid_token. Any other APP 4xx is a fault
# between MCP and the APP (internal token, validation, route, throttle) and
# answers 502: a 401 there would send every client into a sign-in loop.
REAUTH_ACTIVITY_CODES = frozenset({
    "token_not_found",
    "token_user_mismatch",
    "token_revoked",
    "token_expired",
    "connection_not_found",
    "connection_disconnected",
})
JWKS_ADMISSION_SLOTS = 4
JWKS_ADMISSION_WAIT_SECONDS = 2.0
JWKS_FAILURE_COOLDOWN_SECONDS = 5.0
JWKS_FETCH_TIMEOUT_SECONDS = 5.0
# The tool deadline used to start AFTER the auth path, so cold auth
# (JWKS + two internal calls) ran on top of the 40s budget. Auth gets its own
# bounded slice, and the request start is stamped so the tool budget consumes
# only what REMAINS of the total.
AUTH_BUDGET_SECONDS = 15.0

# Stamped by AuthMiddleware at request entry, for diagnostics only.
#
# Do NOT wire this back into the tool budget. call_tool used to
# subtract it, and on the streamable-HTTP transport that took the hosted
# gateway down for three weeks: the SDK starts the per-session server loop
# from inside the request that creates the session, so the loop inherits that
# request's contextvars and every later dispatch read the age of the session
# instead of its own auth leg. A ContextVar cannot carry a value from the POST
# that carries a tools/call to the dispatch that serves it - what is visible
# at dispatch is always the session-creating request's stamp. Auth is bounded
# on its own side instead, by the asyncio.timeout below.
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

# Clients that decide whether a server needs sign-in from the answer to an
# unauthenticated initialize: a 200 reads as "no sign-in", so they never fetch
# the protected resource metadata and every tools/call then fails with 401.
# Their discovery methods get the bare challenge instead. Matched on the
# lower-cased User-Agent prefix. The Grok web connector (grok.com) sends
# grok-connectors-manager/<version>; Grok Build (grok-cli) is not in it.
SIGN_IN_FIRST_USER_AGENTS = ("grok-connectors-manager/",)
# The same clients, matched on the initialize clientInfo.name where they send no
# User-Agent: the Grok web connector checks a server with a separate
# grok-validator initialize, and its anonymous 200 alone leaves the sign-in
# window blank. Exact names only, so directory crawlers keep their listing.
# Observed 2026-10-14: clientInfo {"name": "grok-validator", "version": "0.1"},
# protocolVersion 2025-03-26, no User-Agent and no Origin. Each challenge shows
# in the demand count as an initialize line with status 401 and client grok.
SIGN_IN_FIRST_CLIENT_NAMES = frozenset({"grok-validator"})

# Unauthenticated GET/HEAD surface of the hosted app: the human landing page on
# the host root, the liveness probe, and the skills index with the exact file
# paths it lists. STRICT exact-path allowlist (no slash normalization: /health/
# and // variants deliberately stay behind auth and 401 rather than redirect).
# Handlers live in sugra_api_mcp.web; the two lists must stay in sync.
PUBLIC_GET_PATHS = frozenset({"/", "/health"}) | SKILLS_PUBLIC_PATHS

# PyJWT reasons that are fixed text. A reason outside this set is left out of
# the AuthError message: some quote a value decoded from the presented token
# (an unknown kid, an unsupported crit entry, an undecodable header byte).
_FIXED_JWT_REASONS = frozenset({
    "Not enough segments",
    "Invalid header padding",
    "Invalid payload padding",
    "Invalid crypto padding",
    "Invalid header string: must be a json object",
    "Invalid payload string: must be a json object",
    "Key ID header parameter must be a string",
    "The JWKS endpoint did not return a JSON object",
    "The JWKS endpoint did not contain any signing keys",
})

# The auth failure line names the token by shape and fingerprint, never by
# its text. A placeholder is a template a client sent unexpanded instead of a
# key: ${NAME}, $NAME, {{NAME}} or <NAME>.
_PLACEHOLDER_RE = re.compile(
    r"\$\{[^{}]*\}|\$[A-Za-z_][A-Za-z0-9_]*|\{\{[^{}]*\}\}|<[^<>]*>")
_JWT_SHAPE_RE = re.compile(r"[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+")
# Drawn once per process: the fingerprint links repeats of one failing token
# within the process and cannot be recomputed from a guess outside it. A forked
# worker draws its own instead of keeping its parent's.
_fingerprint_key = secrets.token_bytes(32)


def _draw_fingerprint_key() -> None:
    """Replace the fingerprint key; runs in every child right after a fork."""
    global _fingerprint_key
    _fingerprint_key = secrets.token_bytes(32)


if hasattr(os, "register_at_fork"):
    os.register_at_fork(after_in_child=_draw_fingerprint_key)


def _jwt_reason(prefix: str, error: Exception) -> str:
    """The AuthError message for a PyJWT failure: the reason only when it is fixed text."""
    reason = str(error)
    return f"{prefix}: {reason}" if reason in _FIXED_JWT_REASONS else prefix


def _signs_in_first(payload: dict) -> bool:
    """True for an initialize whose clientInfo.name is in SIGN_IN_FIRST_CLIENT_NAMES."""
    if payload.get("method") != "initialize":
        return False
    params = payload.get("params")
    client_info = params.get("clientInfo") if isinstance(params, dict) else None
    name = client_info.get("name") if isinstance(client_info, dict) else None
    return isinstance(name, str) and name in SIGN_IN_FIRST_CLIENT_NAMES


def _token_kind(token: str) -> str:
    """empty, api_key, placeholder, jwt or other, from the token's shape alone."""
    if not token:
        return "empty"
    if token.startswith("sugra_"):
        return "api_key"
    if _PLACEHOLDER_RE.fullmatch(token):
        return "placeholder"
    if _JWT_SHAPE_RE.fullmatch(token):
        return "jwt"
    return "other"


def _token_fingerprint(token: str) -> str:
    """First 8 hex of HMAC-SHA256 under the per-process key; "-" for an empty token."""
    if not token:
        return "-"
    digest = hmac.new(
        _fingerprint_key, token.encode("utf-8", "surrogatepass"), hashlib.sha256)
    return digest.hexdigest()[:8]


class AuthError(Exception):
    """An auth refusal. A 401 says error="invalid_token" unless reauth is False."""

    def __init__(
        self, message: str, *, status: int = 401, reauth: bool = True,
    ) -> None:
        super().__init__(message)
        self.status = status
        self.reauth = reauth


class _AppSideFailure(Exception):
    """The APP access check failed on the APP's side: a transport error or a 5xx.

    `reason` is "transport" (`detail` the exception class) or "http_5xx" (`detail`
    the status); `cause` is the transport exception, if any.
    """

    def __init__(
        self, reason: str, detail: str, cause: BaseException | None = None,
    ) -> None:
        super().__init__(reason)
        self.reason = reason
        self.detail = detail
        self.cause = cause


def _is_app_refusal(status: int) -> bool:
    """An APP answer that is neither a pass (2xx) nor a failure on its side (5xx)."""
    return not (200 <= status < 300 or 500 <= status < 600)


async def _note_activity_status(response: httpx.Response) -> None:
    """Response hook: record an access check's status before its body is read.

    httpx runs response hooks once the headers are in and before it reads the
    body, so a refusal whose body then fails to arrive is still a refusal.
    """
    seen = response.request.extensions.get(ACTIVITY_STATUS_EXTENSION)
    if isinstance(seen, dict):
        seen["status"] = response.status_code


@dataclass(frozen=True)
class _CachedKey:
    api_key: str
    expires_at: float


@dataclass(frozen=True)
class _AccessPass:
    """A passing activity check: expiry plus optional connector platform."""

    expires_at: float
    platform: str | None = None


def _platform_from_activity(resp: object) -> str | None:
    """Allowlisted connector platform from a 2xx activity body, else None.

    A missing, non-JSON or unknown value must not fail auth: an older APP
    still answers {"ok": true} or 204, and the call is already admitted.
    """
    try:
        body = resp.json()  # type: ignore[union-attr]
    except Exception:
        return None
    if not isinstance(body, dict):
        return None
    platform = body.get("platform")
    if type(platform) is not str:
        return None
    return platform if platform in ACCESS_PLATFORMS else None


@dataclass(frozen=True)
class ResolvedAuth:
    api_key: str
    user_id: int | None = None
    access_token_id: str | None = None
    # How the bearer authenticated, "api_key" (a sugra_ key taken as
    # itself) or "oauth" (a validated JWT resolved to the account's primary key).
    # No default label: a constructor that does not say leaves None, and spans
    # then carry no auth class rather than a wrong one.
    method: str | None = None
    # Server-verified connector platform from the APP activity
    # response. Allowlisted at the span. None when the APP omitted it.
    platform: str | None = None


class _UserLockEntry:
    __slots__ = ("lock", "refs")

    def __init__(self) -> None:
        self.lock = asyncio.Lock()
        self.refs = 0


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
        # Per-user single-flight locks. The old single
        # Authenticator-wide lock was HELD ACROSS the internal HTTP call, so
        # every user's cold-cache lookup serialized behind every other's.
        self._user_locks: dict[int, _UserLockEntry] = {}
        # PyJWKClient does SYNCHRONOUS network I/O; on the event loop it
        # stalled every request in the process (including raw sugra_ keys)
        # whenever the key cache was cold. A DEDICATED executor keeps the
        # offload from competing with the default pool.
        self._jwks_executor = ThreadPoolExecutor(
            max_workers=2, thread_name_prefix="jwks")
        # Flood protection. Admission to the executor is
        # bounded (a queue of malformed/unique-kid bearers must not bury
        # legitimate JWTs), and a failing JWKS endpoint puts the whole JWT
        # path on a short cooldown instead of hammering the pool.
        self._jwks_gate = asyncio.Semaphore(JWKS_ADMISSION_SLOTS)
        # -inf: monotonic() can be near zero right after boot -
        # a 0.0 init would false-arm the cooldown.
        self._jwks_failed_at = float("-inf")
        # Short-TTL cache of a PASSING activity validation per token jti:
        # the check ran an internal HTTP round-trip on EVERY tool call.
        # Failures are never cached; the TTL only coarsens the activity
        # heartbeat, not the access decision it grants.
        self._access_cache: dict[str, _AccessPass] = {}
        # Refusals per jti, each as the eviction count when it was recorded: a
        # check that started before a recorded refusal never stores its pass.
        # Bounded like the pass cache; a dropped entry raises the floor, which
        # then counts for every jti.
        self._access_epoch = 0
        self._access_refused: dict[str, int] = {}
        self._access_refused_floor = 0
        # One pooled client for all internal calls (a fresh client per call
        # paid TCP+TLS setup on every tool invocation), on the process-wide
        # TLS context so it does not build a second one.
        self._http = httpx.AsyncClient(
            timeout=INTERNAL_HTTP_TIMEOUT_SECONDS,
            verify=shared_ssl_context(),
            event_hooks={"response": [_note_activity_status]},
        )

    async def aclose(self) -> None:
        self._jwks_executor.shutdown(wait=False, cancel_futures=True)
        await self._http.aclose()

    @property
    def protected_resource_metadata_url(self) -> str:
        return f"{self._config.app_url}/.well-known/oauth-protected-resource"

    def protected_resource_metadata_url_for(self, host: object) -> str:
        """The metadata document for a request to `host` (from config.caller_host).

        Exactly mcp.sugra.ai in front of the hosted APP gets that host's own
        document; everything else gets protected_resource_metadata_url.
        """
        if host == MCP_HOST and self._config.app_url == MCP_HOST_APP_URL:
            return MCP_HOST_METADATA_URL
        return self.protected_resource_metadata_url

    @property
    def audience(self) -> str:
        return f"{self._config.app_url}/mcp"

    async def resolve(self, token: str, deadline: float | None = None) -> ResolvedAuth:
        # `deadline`: the event-loop time the caller's auth budget ends, handed
        # down to the access check by argument (AuthMiddleware passes its own).
        token = token.strip()
        if not token:
            raise AuthError("Empty token")

        if token.startswith("sugra_"):
            return ResolvedAuth(api_key=token, method="api_key")

        # A malformed bearer must fail HERE, on the loop, at
        # parse cost - never occupy a JWKS executor slot.
        try:
            header = jwt.get_unverified_header(token)
        except jwt.InvalidTokenError as e:
            raise AuthError(_jwt_reason("Malformed token", e)) from e
        has_kid = bool(header.get("kid"))

        # A failing JWKS endpoint cooldowns the whole JWT path.
        if time.monotonic() - self._jwks_failed_at < JWKS_FAILURE_COOLDOWN_SECONDS:
            raise AuthError("Signing keys temporarily unavailable", status=503)

        # Stage attribution: 45-120s holds were observed that no log
        # could attribute to a stage. The line lands in a finally (
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
                platform = await self._validate_mcp_access(claims, deadline)
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
        except asyncio.CancelledError:
            outcome = "cancelled"
            raise
        except AuthError:
            raise
        except Exception:
            outcome = "error"
            raise
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
            method="oauth",
            platform=platform,
        )
        return resolved

    def _validate_jwt(self, token: str, has_kid: bool = False) -> _JwtClaims:
        # The fallback exists ONLY for the expected no-kid case
        # (Passport / league-oauth2-server). A token WITH a kid that fails
        # lookup is invalid - retrying the whole key list on it let malformed
        # traffic double its JWKS I/O.
        if has_kid:
            try:
                signing_key = self._jwks.get_signing_key_from_jwt(token)
            except jwt.exceptions.PyJWKClientConnectionError as e:
                # An unreachable JWKS on the STANDARD path must be a
                # 503 - it is what arms the failure cooldown.
                raise AuthError(
                    f"Unable to load signing keys: {e}", status=503) from e
            except Exception as e:
                raise AuthError(_jwt_reason("Unknown signing key", e)) from e
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
            # A valid token without the scope is not an invalid token: it keeps
            # the bare challenge (insufficient_scope is MCP-29 decision 9).
            raise AuthError(
                f"Token missing required scope: {REQUIRED_SCOPE}", reauth=False)

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
        # Entries are reference-counted and remove themselves
        # when the last user leaves - a locked()-based sweep raced the
        # release/wakeup window and could split the single-flight exactly
        # on degraded lookups.
        entry = self._user_locks.setdefault(user_id, _UserLockEntry())
        entry.refs += 1
        try:
            return await self._locked_lookup(user_id, entry, now)
        finally:
            entry.refs -= 1
            if entry.refs == 0 and self._user_locks.get(user_id) is entry:
                del self._user_locks[user_id]

    async def _locked_lookup(
        self, user_id: int, entry: _UserLockEntry, now: float
    ) -> str:
        async with entry.lock:
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
                body = _json_object(resp)
                code = body.get("error")
                if code == "no_api_key":
                    message = body.get("message")
                    raise AuthError(
                        message if isinstance(message, str) and message
                        else "User has no API key. Create one at https://app.sugra.ai/settings",
                        status=403,
                    )
                if code == "user_not_found":
                    # The token's account is gone: sign in again.
                    raise AuthError("Access token refused (user_not_found), sign in again")
            if resp.status_code >= 400:
                raise AuthError(f"Internal lookup failed: HTTP {resp.status_code}", status=502)

            data = resp.json()
            api_key = data["api_key"]
            self._api_key_cache[user_id] = _CachedKey(
                api_key=api_key,
                expires_at=now + API_KEY_CACHE_TTL_SECONDS,
            )
            return api_key

    async def _validate_mcp_access(
        self, claims: _JwtClaims, deadline: float | None = None,
    ) -> str | None:
        if not self._config.internal_token:
            raise AuthError("INTERNAL_API_TOKEN not configured on MCP server", status=500)

        # jti-TTL cache: a PASS within the window skips the round-trip.
        now = time.monotonic()
        cached = self._access_cache.get(claims.access_token_id)
        if cached is not None and cached.expires_at > now:
            return cached.platform

        # A failure on the APP's side is retried with a growing pause while the
        # auth budget holds the pause, a minimum attempt and the reserve, the
        # room rechecked after each pause, which can run long. `deadline` is the
        # event-loop time the caller's auth budget ends; without one the check
        # counts AUTH_BUDGET_SECONDS from here. Every attempt is cut where the
        # reserve begins; when a minimum attempt no longer fits before it, the
        # one attempt is cut at the deadline itself, never left uncut.
        loop = asyncio.get_running_loop()
        if deadline is None:
            deadline = loop.time() + AUTH_BUDGET_SECONDS
        reserve_at = deadline - ACCESS_RETRY_RESERVE_SECONDS
        cut = reserve_at
        if reserve_at - loop.time() < ACCESS_RETRY_MIN_ATTEMPT_SECONDS:
            cut = deadline
        pause = ACCESS_RETRY_PAUSE_SECONDS
        while True:
            try:
                return await self._attempt_activity(claims, cut)
            except _AppSideFailure as failure:
                last = failure
            if reserve_at - loop.time() < pause + ACCESS_RETRY_MIN_ATTEMPT_SECONDS:
                break
            await asyncio.sleep(pause)
            if reserve_at - loop.time() < ACCESS_RETRY_MIN_ATTEMPT_SECONDS:
                break
            pause = min(pause * 2, ACCESS_RETRY_MAX_PAUSE_SECONDS)
        if last.reason == "transport":
            raise AuthError(
                "Internal MCP access validation failed", status=502) from last.cause
        raise AuthError(
            f"Internal MCP access validation failed: HTTP {last.detail}", status=502)

    async def _attempt_activity(self, claims: _JwtClaims, cut: float) -> str | None:
        """One access check cut at `cut`; the cut is a failure on the APP's side
        unless a refusal's headers already came in."""
        seen: dict[str, int] = {}
        try:
            async with asyncio.timeout_at(cut):
                return await self._check_activity(claims, time.monotonic(), seen)
        except TimeoutError as e:
            logger.info(
                "mcp_access_validation_exception user_id=%d error=%s",
                claims.user_id, "check cut before the auth budget ends")
            raise self._broken_check(claims, seen, e) from e
        except asyncio.CancelledError:
            # Cancelled after a refusal's headers came in: the refusal still
            # drops the stored pass.
            if _is_app_refusal(seen.get("status", 200)):
                self._evict_access_pass(claims.access_token_id)
            raise

    async def _check_activity(
        self, claims: _JwtClaims, now: float, seen: dict[str, int],
    ) -> str | None:
        """One APP access check; a failure on the APP's side raises _AppSideFailure.

        `seen` gets the status as soon as the headers arrive (the response hook).
        """
        epoch = self._access_epoch
        url = f"{self._config.app_url}/api/internal/mcp/activity"
        try:
            resp = await self._http.post(
                url,
                headers={"X-Internal-Token": self._config.internal_token},
                json={
                    "user_id": claims.user_id,
                    "access_token_id": claims.access_token_id,
                },
                extensions={ACTIVITY_STATUS_EXTENSION: seen},
            )
        except Exception as e:
            logger.info("mcp_access_validation_exception user_id=%d error=%s", claims.user_id, e)
            raise self._broken_check(claims, seen, e) from e

        if 200 <= resp.status_code < 300:
            platform = _platform_from_activity(resp)
            if self._refused_since(claims.access_token_id, epoch):
                # A refusal of this token was recorded while this check was in
                # flight: this earlier pass admits this request only and is
                # never stored, so it cannot outlive the refusal.
                return platform
            self._access_cache[claims.access_token_id] = _AccessPass(
                expires_at=now + ACCESS_VALIDATION_TTL_SECONDS,
                platform=platform,
            )
            # The cache only ever holds passes. HARD bound: pruning
            # expired entries alone cannot shrink a cache full of LIVE jtis,
            # and re-running an O(N) comprehension per auth is itself the
            # DoS. Keep the newest entries by expiry when over the cap.
            if len(self._access_cache) > ACCESS_CACHE_MAX_ENTRIES:
                live = {jti: item for jti, item in self._access_cache.items()
                        if item.expires_at > now}
                if len(live) > ACCESS_CACHE_PRUNE_WATERMARK:
                    live = dict(sorted(
                        live.items(), key=lambda kv: kv[1].expires_at,
                    )[-ACCESS_CACHE_PRUNE_WATERMARK:])
                self._access_cache = live
            return platform

        logger.info(
            "mcp_access_validation_failed user_id=%d status=%d",
            claims.user_id,
            resp.status_code,
        )

        if 500 <= resp.status_code < 600:
            raise _AppSideFailure("http_5xx", str(resp.status_code))

        # The APP answered, neither with a pass nor with a failure on its side
        # (a 1xx, 3xx, 4xx or 600 and up): a refusal, never retried. A pass of
        # this token from a check still in flight must not be stored after it.
        self._evict_access_pass(claims.access_token_id)
        code = _json_object(resp).get("error")
        if isinstance(code, str) and code in REAUTH_ACTIVITY_CODES:
            raise AuthError(f"Access token refused ({code}), sign in again")
        raise AuthError(
            f"Internal MCP access validation failed: HTTP {resp.status_code}", status=502)

    def _broken_check(
        self, claims: _JwtClaims, seen: dict[str, int], cause: BaseException,
    ) -> Exception:
        """What a check that broke before its response was complete raises.

        A refusal whose headers already came in is still a refusal: it drops the
        stored pass and is never retried. A 5xx whose body broke stays a 5xx;
        anything else is a transport failure on the APP's side.
        """
        status = seen.get("status")
        if status is not None and _is_app_refusal(status):
            self._evict_access_pass(claims.access_token_id)
            return AuthError(
                f"Internal MCP access validation failed: HTTP {status}", status=502)
        if status is not None and 500 <= status < 600:
            return _AppSideFailure("http_5xx", str(status), cause)
        return _AppSideFailure("transport", cause.__class__.__name__, cause)

    def _evict_access_pass(self, jti: str) -> None:
        """Drop this token's stored pass and record the refusal for checks in flight."""
        self._access_cache.pop(jti, None)
        self._access_epoch += 1
        self._access_refused.pop(jti, None)
        self._access_refused[jti] = self._access_epoch
        if len(self._access_refused) > ACCESS_CACHE_MAX_ENTRIES:
            # Insertion order is epoch order: drop the oldest, and keep what
            # they said in the floor, which counts for every jti.
            excess = len(self._access_refused) - ACCESS_CACHE_PRUNE_WATERMARK
            for old in list(self._access_refused)[:excess]:
                self._access_refused_floor = self._access_refused.pop(old)

    def _refused_since(self, jti: str, epoch: int) -> bool:
        """Whether a refusal of this token was recorded after eviction count `epoch`."""
        return max(self._access_refused_floor, self._access_refused.get(jti, 0)) > epoch


def _json_object(resp: httpx.Response) -> dict:
    """The response body when it is a JSON object, else an empty dict."""
    try:
        body = resp.json()
    except ValueError:
        return {}
    return body if isinstance(body, dict) else {}


class AuthMiddleware(BaseHTTPMiddleware):
    """Starlette middleware that resolves the Authorization header per request."""

    def __init__(self, app: ASGIApp, authenticator: Authenticator) -> None:
        super().__init__(app)
        self._auth = authenticator

    def _auth_headers(self, request: Request, *, invalid_token: bool = False) -> dict[str, str]:
        # A presented token that failed gets error="invalid_token", which is
        # what makes a client run its sign-in again; a request with no token,
        # a token without the scope and the auth timeout get the bare
        # challenge (RFC 6750 3.1).
        host = caller_host(request.headers.get("host"), request.headers.get("x-forwarded-host"))
        challenge = (
            'Bearer resource_metadata="'
            f'{self._auth.protected_resource_metadata_url_for(host)}"'
        )
        if invalid_token:
            challenge += ', error="invalid_token"'
        return {"WWW-Authenticate": challenge}

    async def _is_public_mcp_request(self, request: Request) -> bool:
        if request.method != "POST" or request.url.path.rstrip("/") != "/mcp":
            return False

        user_agent = request.headers.get("user-agent", "").strip().lower()
        if user_agent.startswith(SIGN_IN_FIRST_USER_AGENTS):
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
            if _signs_in_first(payload):
                return False
            return isinstance(method, str) and method in PUBLIC_MCP_METHODS

        if isinstance(payload, list) and 0 < len(payload) <= MAX_PUBLIC_MCP_BATCH_ITEMS:
            for item in payload:
                if not isinstance(item, dict) or _signs_in_first(item):
                    return False
                method = item.get("method")
                if not isinstance(method, str) or method not in PUBLIC_MCP_METHODS:
                    return False
            return True

        return False

    async def dispatch(self, request: Request, call_next) -> Response:  # type: ignore[override]
        # Mark everything this middleware serves as the HTTP transport. A
        # Streamable HTTP session task inherits the marker from the request that
        # opened it, so server.get_client refuses the env fallback key there even
        # when a dispatch carries no HTTP request.
        from .server import http_transport_ctx

        previous_transport = http_transport_ctx.set(True)
        try:
            return await self._dispatch(request, call_next)
        finally:
            http_transport_ctx.reset(previous_transport)

    async def _dispatch(self, request: Request, call_next) -> Response:
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
                headers=self._auth_headers(request),
            )

        if not header.lower().startswith("bearer "):
            return JSONResponse(
                {"error": "missing_bearer_token"},
                status_code=401,
                headers=self._auth_headers(request),
            )

        token = header[7:].strip()
        request_started_at.set(time.monotonic())
        try:
            # The auth slice also respects a SMALL total budget.
            from .config import load_config as _load_config

            total_budget = _load_config(require_api_key=False).tool_deadline
            auth_budget = min(AUTH_BUDGET_SECONDS, total_budget)
            async with asyncio.timeout(auth_budget) as budget:
                # The budget's end goes down by argument, never by ambient
                # state that a task started here could inherit.
                resolved = await self._auth.resolve(token, deadline=budget.when())
            # The per-session MCP task never sees this request's ContextVars, but the
            # SDK hands this request's scope to the handler, where server.get_client
            # reads the key. The ContextVar below stays for in-process callers.
            from .server import REQUEST_API_KEY_STATE, REQUEST_PRINCIPAL_STATE, RequestPrincipal

            state = request.scope.setdefault("state", {})
            state[REQUEST_API_KEY_STATE] = resolved.api_key
            # How this request authenticated, read at dispatch from the
            # same scope for caller attribution. Never the token or the token id.
            state[REQUEST_PRINCIPAL_STATE] = RequestPrincipal(
                method=resolved.method,
                user_id=resolved.user_id,
                platform=resolved.platform,
            )
        except TimeoutError:
            return JSONResponse(
                {"error": "auth_timeout",
                 "message": (
                     f"Authentication exceeded its {auth_budget:.0f}s "
                     "budget and was cancelled."
                 )},
                status_code=503,
                headers=self._auth_headers(request),
            )
        except AuthError as e:
            # Shape class and fingerprint of the token, and the caller's fixed
            # classes the spans carry too, never raw header text.
            logger.warning(
                "auth_failed status=%d kind=%s fp=%s host=%s ua=%s origin=%s msg=%s",
                e.status,
                _token_kind(token),
                _token_fingerprint(token),
                _host_class(caller_host(request.headers.get("host"), request.headers.get("x-forwarded-host"))) or "-",
                _text_class(request.headers.get("user-agent"), _UA_PATTERNS),
                _origin_class(request.headers.get("origin")),
                e,
            )
            if e.status == 401 and e.reauth:
                return JSONResponse(
                    {"error": "auth_failed", "message": str(e)},
                    status_code=401,
                    headers=self._auth_headers(request, invalid_token=True),
                )
            return JSONResponse(
                {"error": "auth_failed", "message": str(e)},
                status_code=e.status,
                headers=self._auth_headers(request) if e.status == 401 else None,
            )

        ctx_token = api_key_ctx.set(resolved.api_key)
        try:
            return await call_next(request)
        finally:
            api_key_ctx.reset(ctx_token)
