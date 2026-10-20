"""An APP access check that fails on the APP's side: one retry, then a bounded stale pass.

While the APP restarts (an APP deploy) its access check answers a transport
error or a 5xx for a few seconds. The check is tried once more inside the auth
budget; if that fails too, a pass of the same token that expired less than
ACCESS_STALE_GRACE_SECONDS ago is served. A token never passed, a pass past the
grace, and any refusal the APP answers (4xx) still get what they got before.
"""

from __future__ import annotations

import asyncio
import logging
import time
from unittest.mock import AsyncMock

import httpx
import pytest
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.testclient import TestClient

import sugra_api_mcp.auth as auth_mod
from sugra_api_mcp.auth import (
    Authenticator,
    AuthError,
    AuthMiddleware,
    ResolvedAuth,
    _AccessPass,
    _JwtClaims,
    auth_deadline,
)
from sugra_api_mcp.config import AuthConfig

JTI = "grace-token-id"
CLAIMS = _JwtClaims(user_id=11, access_token_id=JTI)


class _ScriptedHttp:
    """Answers the access check from a script: an int status, an exception, or "hang"."""

    def __init__(self, *script) -> None:
        self.script = list(script)
        self.post_calls = 0

    async def post(self, url, headers=None, json=None):
        self.post_calls += 1
        step = self.script.pop(0)
        if step == "hang":
            await asyncio.sleep(60)
        if isinstance(step, Exception):
            raise step
        if step == 403:
            return httpx.Response(403, json={"error": "token_revoked"})
        if step == 204:
            return httpx.Response(204)
        return httpx.Response(step, json={"ok": True, "platform": "anthropic"})

    async def aclose(self):
        return None


@pytest.fixture(autouse=True)
def _short_pause(monkeypatch):
    monkeypatch.setattr(auth_mod, "ACCESS_RETRY_PAUSE_SECONDS", 0.0)


def _authenticator(*script) -> tuple[Authenticator, _ScriptedHttp]:
    auth = Authenticator(AuthConfig(
        app_url="https://app.example", jwks_url="https://app.example/jwks",
        internal_token="tkn"))
    http = _ScriptedHttp(*script)
    auth._http = http
    return auth, http


def _expire(auth: Authenticator, seconds_ago: float) -> None:
    """Move this token's stored pass to have expired `seconds_ago` seconds ago."""
    held = auth._access_cache[JTI]
    auth._access_cache[JTI] = _AccessPass(
        expires_at=time.time() - seconds_ago, platform=held.platform)


@pytest.mark.parametrize("failure", [
    httpx.ConnectError("refused"),
    503,
], ids=["transport", "http_503"])
async def test_recent_pass_is_served_while_the_app_check_fails(failure, caplog):
    auth, http = _authenticator(204, failure, failure)
    assert await auth._validate_mcp_access(CLAIMS) is None
    _expire(auth, 30)

    with caplog.at_level(logging.WARNING, logger="sugra_mcp.auth"):
        platform = await auth._validate_mcp_access(CLAIMS)

    assert platform is None
    assert http.post_calls == 3
    lines = [r.getMessage() for r in caplog.records if "mcp_access_stale_pass" in r.getMessage()]
    assert len(lines) == 1
    assert "user_id=11" in lines[0]
    assert JTI not in lines[0]
    expected = "reason=transport detail=ConnectError" if isinstance(failure, Exception) \
        else "reason=http_5xx detail=503"
    assert expected in lines[0]


async def test_stale_pass_keeps_the_platform_of_the_last_pass():
    auth, _ = _authenticator(200, 502, 502)
    assert await auth._validate_mcp_access(CLAIMS) == "anthropic"
    _expire(auth, 1)
    assert await auth._validate_mcp_access(CLAIMS) == "anthropic"


@pytest.mark.parametrize("failure", [httpx.ConnectError("refused"), 500])
async def test_a_token_never_passed_still_gets_502(failure):
    auth, http = _authenticator(failure, failure)
    with pytest.raises(AuthError) as exc:
        await auth._validate_mcp_access(CLAIMS)
    assert exc.value.status == 502
    assert http.post_calls == 2
    assert JTI not in auth._access_cache, "a failure must never be cached"


async def test_a_pass_past_the_grace_gets_502():
    auth, _ = _authenticator(204, 503, 503)
    await auth._validate_mcp_access(CLAIMS)
    _expire(auth, auth_mod.ACCESS_STALE_GRACE_SECONDS + 1)
    with pytest.raises(AuthError) as exc:
        await auth._validate_mcp_access(CLAIMS)
    assert exc.value.status == 502
    assert "HTTP 503" in str(exc.value)


async def test_a_served_stale_pass_is_never_extended():
    auth, _ = _authenticator(204, 503, 503)
    await auth._validate_mcp_access(CLAIMS)
    _expire(auth, 30)
    before = auth._access_cache[JTI]
    await auth._validate_mcp_access(CLAIMS)
    assert auth._access_cache[JTI] == before


async def test_the_bound_keeps_a_pass_inside_its_grace():
    auth, _ = _authenticator(204, 204, 503, 503)
    await auth._validate_mcp_access(CLAIMS)
    _expire(auth, 30)
    gone = time.time() - auth_mod.ACCESS_STALE_GRACE_SECONDS - 60
    for i in range(auth_mod.ACCESS_CACHE_MAX_ENTRIES):
        auth._access_cache[f"old-{i}"] = _AccessPass(expires_at=gone)
    await auth._validate_mcp_access(_JwtClaims(user_id=12, access_token_id="fresh"))
    assert len(auth._access_cache) <= auth_mod.ACCESS_CACHE_MAX_ENTRIES
    assert JTI in auth._access_cache, "the prune dropped a pass still inside its grace"
    assert await auth._validate_mcp_access(CLAIMS) is None


async def test_a_revocation_drops_the_pass_and_a_later_app_failure_gets_502():
    auth, http = _authenticator(204, 403, 503, 503)
    await auth._validate_mcp_access(CLAIMS)
    _expire(auth, 5)

    with pytest.raises(AuthError) as revoked:
        await auth._validate_mcp_access(CLAIMS)
    assert revoked.value.status == 401
    assert JTI not in auth._access_cache
    assert http.post_calls == 2, "a 4xx answer is never retried"

    with pytest.raises(AuthError) as later:
        await auth._validate_mcp_access(CLAIMS)
    assert later.value.status == 502


@pytest.mark.parametrize("failure", [httpx.ReadError("reset"), 502])
async def test_one_retry_and_a_success_on_it_serves_normally(failure):
    auth, http = _authenticator(failure, 200)
    assert await auth._validate_mcp_access(CLAIMS) == "anthropic"
    assert http.post_calls == 2
    assert auth._access_cache[JTI].expires_at > time.time()
    # The pass the retry earned is cached like any other.
    assert await auth._validate_mcp_access(CLAIMS) == "anthropic"
    assert http.post_calls == 2


async def test_there_is_never_a_second_retry():
    auth, http = _authenticator(503, 503, 200)
    with pytest.raises(AuthError):
        await auth._validate_mcp_access(CLAIMS)
    assert http.post_calls == 2


async def test_no_retry_when_the_budget_cannot_hold_one(monkeypatch):
    monkeypatch.setattr(auth_mod, "ACCESS_RETRY_PAUSE_SECONDS", 1.0)
    auth, http = _authenticator(503, 200)
    loop = asyncio.get_running_loop()
    needed = (auth_mod.ACCESS_RETRY_PAUSE_SECONDS + auth_mod.ACCESS_RETRY_MIN_ATTEMPT_SECONDS
              + auth_mod.ACCESS_RETRY_RESERVE_SECONDS)
    token = auth_deadline.set(loop.time() + needed - 0.2)
    try:
        started = loop.time()
        with pytest.raises(AuthError) as exc:
            await auth._validate_mcp_access(CLAIMS)
        elapsed = loop.time() - started
    finally:
        auth_deadline.reset(token)
    assert exc.value.status == 502
    assert http.post_calls == 1
    assert elapsed < 0.5, "the pause ran although no retry fits the budget"


async def test_the_retry_is_cut_before_the_auth_budget_ends(monkeypatch):
    monkeypatch.setattr(auth_mod, "ACCESS_RETRY_MIN_ATTEMPT_SECONDS", 0.05)
    monkeypatch.setattr(auth_mod, "ACCESS_RETRY_RESERVE_SECONDS", 0.3)
    auth, http = _authenticator(200, 503, "hang")
    await auth._validate_mcp_access(CLAIMS)
    _expire(auth, 10)

    loop = asyncio.get_running_loop()
    deadline = loop.time() + 0.6
    token = auth_deadline.set(deadline)
    try:
        # The middleware's own budget: a retry left uncut would end here as a
        # TimeoutError instead of the stale pass.
        async with asyncio.timeout_at(deadline):
            platform = await auth._validate_mcp_access(CLAIMS)
        finished = loop.time()
    finally:
        auth_deadline.reset(token)
    assert platform == "anthropic"
    assert http.post_calls == 3
    assert finished <= deadline - 0.2


def test_the_middleware_hands_its_budget_to_the_access_check():
    seen = {}

    async def _resolve(token):
        seen["deadline"] = auth_deadline.get()
        seen["now"] = asyncio.get_running_loop().time()
        return ResolvedAuth(api_key="sugra_k", user_id=1, method="oauth")

    async def ok(_request):
        return JSONResponse({"ok": True})

    auth = Authenticator(AuthConfig(
        app_url="https://app.example", jwks_url="https://app.example/jwks",
        internal_token="tkn"))
    auth.resolve = AsyncMock(side_effect=_resolve)
    app = Starlette(routes=[Route("/mcp", ok, methods=["POST"])])
    app.add_middleware(AuthMiddleware, authenticator=auth)

    response = TestClient(app).post(
        "/mcp", json={}, headers={"authorization": "Bearer t"})

    assert response.status_code == 200
    assert seen["deadline"] is not None
    remaining = seen["deadline"] - seen["now"]
    assert 0 < remaining <= auth_mod.AUTH_BUDGET_SECONDS
