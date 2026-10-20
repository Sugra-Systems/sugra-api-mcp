"""An APP access check that fails on the APP's side: one retry, then a bounded stale pass.

While the APP restarts (an APP deploy) its access check answers a transport
error or a 5xx for a few seconds. The check is tried once more inside the auth
budget; if that fails too, a pass of the same token that expired less than
ACCESS_STALE_GRACE_SECONDS ago is served. A token never passed, a pass past the
grace, and any other APP answer (1xx, 3xx, 4xx, past 5xx) still get what they
got before, and such an answer drops the stored pass even when its body never
arrives or a pass that started earlier lands after it.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import jwt
import pytest
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route

import sugra_api_mcp.auth as auth_mod
from sugra_api_mcp.auth import (
    Authenticator,
    AuthError,
    AuthMiddleware,
    ResolvedAuth,
    _AccessPass,
    _JwtClaims,
)
from sugra_api_mcp.config import AuthConfig

JTI = "grace-token-id"
CLAIMS = _JwtClaims(user_id=11, access_token_id=JTI)


class _ScriptedHttp:
    """Answers the access check from a script.

    A step is an int status, an exception, "hang", or ("after", event, status):
    wait for the event, then answer the status.
    """

    def __init__(self, *script) -> None:
        self.script = list(script)
        self.post_calls = 0

    async def post(self, url, headers=None, json=None, extensions=None):
        self.post_calls += 1
        step = self.script.pop(0)
        await asyncio.sleep(0)  # a network call always yields to the loop
        if isinstance(step, tuple):
            _, event, step = step
            await event.wait()
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


def _config(app_url: str = "https://app.example") -> AuthConfig:
    return AuthConfig(app_url=app_url, jwks_url=f"{app_url}/jwks", internal_token="tkn")


def _authenticator(*script) -> tuple[Authenticator, _ScriptedHttp]:
    auth = Authenticator(_config())
    http = _ScriptedHttp(*script)
    auth._http = http
    return auth, http


def _expire(auth: Authenticator, seconds_ago: float) -> None:
    """Move this token's stored pass to have expired `seconds_ago` seconds ago."""
    held = auth._access_cache[JTI]
    auth._access_cache[JTI] = _AccessPass(
        expires_at=time.monotonic() - seconds_ago, platform=held.platform)


async def _started(http: _ScriptedHttp, calls: int) -> None:
    while http.post_calls < calls:
        await asyncio.sleep(0)


@pytest.mark.parametrize("failure", [
    httpx.ConnectError("refused"),
    503,
], ids=["transport", "http_503"])
async def test_recent_pass_is_served_while_the_app_check_fails(failure, caplog):
    auth, http = _authenticator(204, failure, failure)
    assert (await auth._validate_mcp_access(CLAIMS)).platform is None
    _expire(auth, 30)

    with caplog.at_level(logging.WARNING, logger="sugra_mcp.auth"):
        verdict = await auth._validate_mcp_access(CLAIMS)

    assert verdict.platform is None
    assert verdict.stale is True
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
    assert (await auth._validate_mcp_access(CLAIMS)).platform == "anthropic"
    _expire(auth, 1)
    assert (await auth._validate_mcp_access(CLAIMS)).platform == "anthropic"


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
    gone = time.monotonic() - auth_mod.ACCESS_STALE_GRACE_SECONDS - 60
    for i in range(auth_mod.ACCESS_CACHE_MAX_ENTRIES):
        auth._access_cache[f"old-{i}"] = _AccessPass(expires_at=gone)
    await auth._validate_mcp_access(_JwtClaims(user_id=12, access_token_id="fresh"))
    assert len(auth._access_cache) <= auth_mod.ACCESS_CACHE_MAX_ENTRIES
    assert JTI in auth._access_cache, "the prune dropped a pass still inside its grace"
    assert (await auth._validate_mcp_access(CLAIMS)).stale is True


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


@pytest.mark.parametrize("status", [100, 302, 600])
async def test_an_answer_outside_2xx_and_5xx_drops_the_pass_and_refuses(status):
    auth, http = _authenticator(204, status, 503, 503)
    await auth._validate_mcp_access(CLAIMS)
    _expire(auth, 5)

    with pytest.raises(AuthError) as refused:
        await auth._validate_mcp_access(CLAIMS)
    assert refused.value.status == 502
    assert f"HTTP {status}" in str(refused.value)
    assert http.post_calls == 2, "only a transport error or a 5xx is retried"
    assert JTI not in auth._access_cache

    with pytest.raises(AuthError) as later:
        await auth._validate_mcp_access(CLAIMS)
    assert later.value.status == 502, "a refused token was served stale"


@pytest.mark.parametrize("failure", [httpx.ReadError("reset"), 502])
async def test_one_retry_and_a_success_on_it_serves_normally(failure):
    auth, http = _authenticator(failure, 200)
    verdict = await auth._validate_mcp_access(CLAIMS)
    assert verdict.platform == "anthropic"
    assert verdict.stale is False
    assert http.post_calls == 2
    assert auth._access_cache[JTI].expires_at > time.monotonic()
    # The pass the retry earned is cached like any other.
    assert (await auth._validate_mcp_access(CLAIMS)).platform == "anthropic"
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
    started = loop.time()
    with pytest.raises(AuthError) as exc:
        await auth._validate_mcp_access(CLAIMS, started + needed - 0.2)
    elapsed = loop.time() - started
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
    # The middleware's own budget: a retry left uncut would end here as a
    # TimeoutError instead of the stale pass.
    async with asyncio.timeout_at(deadline):
        verdict = await auth._validate_mcp_access(CLAIMS, deadline)
    finished = loop.time()
    assert verdict.platform == "anthropic"
    assert verdict.stale is True
    assert http.post_calls == 3
    assert finished <= deadline - 0.2


async def test_a_hung_first_check_is_cut_and_the_grace_still_applies(monkeypatch):
    monkeypatch.setattr(auth_mod, "ACCESS_RETRY_MIN_ATTEMPT_SECONDS", 0.05)
    monkeypatch.setattr(auth_mod, "ACCESS_RETRY_RESERVE_SECONDS", 0.3)
    auth, http = _authenticator(200, "hang", "hang")
    await auth._validate_mcp_access(CLAIMS)
    _expire(auth, 10)

    loop = asyncio.get_running_loop()
    deadline = loop.time() + 0.6
    async with asyncio.timeout_at(deadline):
        verdict = await auth._validate_mcp_access(CLAIMS, deadline)
    assert verdict.stale is True
    assert verdict.platform == "anthropic"
    assert loop.time() <= deadline - 0.2
    assert http.post_calls == 2, "the first check was cut where the reserve begins"


async def test_a_first_check_runs_uncut_when_the_budget_is_short():
    auth, http = _authenticator(200)
    loop = asyncio.get_running_loop()
    # Less than the reserve is left: the check is not cut at once, it runs and
    # the caller's own budget bounds it.
    verdict = await auth._validate_mcp_access(CLAIMS, loop.time() + 0.2)
    assert verdict.platform == "anthropic"
    assert http.post_calls == 1


async def test_a_pass_started_before_a_revocation_is_never_stored():
    release = asyncio.Event()
    auth, http = _authenticator(("after", release, 200), 403, 503, 503)
    early = asyncio.create_task(auth._validate_mcp_access(CLAIMS))
    await _started(http, 1)

    with pytest.raises(AuthError) as revoked:
        await auth._validate_mcp_access(CLAIMS)
    assert revoked.value.status == 401

    release.set()
    # The APP's earlier answer admits the request that asked for it ...
    assert (await early).platform == "anthropic"
    # ... and is never stored over the refusal recorded meanwhile.
    assert JTI not in auth._access_cache
    with pytest.raises(AuthError) as later:
        await auth._validate_mcp_access(CLAIMS)
    assert later.value.status == 502, "the revoked token was served from a late pass"


async def test_a_check_started_after_a_revocation_stores_its_pass():
    auth, _ = _authenticator(403, 200)
    with pytest.raises(AuthError):
        await auth._validate_mcp_access(CLAIMS)
    assert (await auth._validate_mcp_access(CLAIMS)).platform == "anthropic"
    assert JTI in auth._access_cache


async def test_the_refusal_record_is_bounded_and_a_dropped_entry_still_blocks():
    release = asyncio.Event()
    auth, http = _authenticator(("after", release, 200), 403)
    early = asyncio.create_task(auth._validate_mcp_access(CLAIMS))
    await _started(http, 1)
    with pytest.raises(AuthError):
        await auth._validate_mcp_access(CLAIMS)

    for i in range(auth_mod.ACCESS_CACHE_MAX_ENTRIES):
        auth._evict_access_pass(f"other-{i}")
    assert len(auth._access_refused) <= auth_mod.ACCESS_CACHE_MAX_ENTRIES
    assert JTI not in auth._access_refused

    release.set()
    await early
    assert JTI not in auth._access_cache, "a dropped refusal let a late pass in"


async def test_a_wall_clock_step_back_never_lengthens_a_pass(monkeypatch):
    clock = {"mono": 1000.0, "wall": 1_900_000_000.0}
    monkeypatch.setattr(auth_mod, "time", SimpleNamespace(
        monotonic=lambda: clock["mono"], time=lambda: clock["wall"]))
    auth, http = _authenticator(200, 503, 503)
    await auth._validate_mcp_access(CLAIMS)

    # Past the pass and its grace, then the wall clock steps back an hour.
    passed = auth_mod.ACCESS_VALIDATION_TTL_SECONDS + auth_mod.ACCESS_STALE_GRACE_SECONDS + 40
    clock["mono"] += passed
    clock["wall"] += passed - 3600

    with pytest.raises(AuthError) as exc:
        await auth._validate_mcp_access(CLAIMS)
    assert exc.value.status == 502
    assert http.post_calls == 3


# A local APP over a real socket, through the Authenticator's own client: the
# status arrives, then the body breaks or stalls.

def _raw(status: int, reason: str, body: bytes, length: int | None = None) -> bytes:
    size = len(body) if length is None else length
    return (
        f"HTTP/1.1 {status} {reason}\r\nContent-Type: application/json\r\n"
        f"Content-Length: {size}\r\nConnection: close\r\n\r\n"
    ).encode() + body


_PASS = _raw(204, "No Content", b"")
_BROKEN_403 = _raw(403, "Forbidden", b'{"error": "tok', length=200)
_BROKEN_503 = _raw(503, "Service Unavailable", b'{"err', length=200)
_FULL_503 = _raw(503, "Service Unavailable", b"{}")


class _LocalApp:
    def __init__(self, *answers) -> None:
        self.answers = list(answers)
        self.requests = 0
        self.release = asyncio.Event()
        self.server: asyncio.Server | None = None

    async def _handle(self, reader, writer) -> None:
        head = await reader.readuntil(b"\r\n\r\n")
        length = re.search(rb"content-length: (\d+)", head, re.IGNORECASE)
        await reader.readexactly(int(length.group(1)) if length else 0)
        self.requests += 1
        raw, then = self.answers.pop(0)
        writer.write(raw)
        await writer.drain()
        if then == "stall":
            await self.release.wait()
        writer.close()

    async def __aenter__(self) -> Authenticator:
        self.server = await asyncio.start_server(self._handle, "127.0.0.1", 0)
        port = self.server.sockets[0].getsockname()[1]
        self.auth = Authenticator(_config(f"http://127.0.0.1:{port}"))
        return self.auth

    async def __aexit__(self, *exc) -> None:
        self.release.set()
        await self.auth.aclose()
        self.server.close()
        await self.server.wait_closed()


async def test_a_refusal_whose_body_breaks_still_refuses_and_drops_the_pass():
    app = _LocalApp((_PASS, "close"), (_BROKEN_403, "close"), (_FULL_503, "close"),
                    (_FULL_503, "close"))
    async with app as auth:
        await auth._validate_mcp_access(CLAIMS)
        _expire(auth, 5)
        with pytest.raises(AuthError) as refused:
            await auth._validate_mcp_access(CLAIMS)
        assert refused.value.status == 502
        assert "HTTP 403" in str(refused.value)
        assert JTI not in auth._access_cache
        assert app.requests == 2, "a refusal was retried"
        with pytest.raises(AuthError) as later:
            await auth._validate_mcp_access(CLAIMS)
        assert later.value.status == 502, "a refused token was served stale"


async def test_a_refusal_whose_body_stalls_past_the_cut_still_refuses(monkeypatch):
    monkeypatch.setattr(auth_mod, "ACCESS_RETRY_MIN_ATTEMPT_SECONDS", 0.05)
    monkeypatch.setattr(auth_mod, "ACCESS_RETRY_RESERVE_SECONDS", 0.3)
    app = _LocalApp((_PASS, "close"), (_BROKEN_403, "stall"))
    async with app as auth:
        await auth._validate_mcp_access(CLAIMS)
        _expire(auth, 5)
        loop = asyncio.get_running_loop()
        with pytest.raises(AuthError) as refused:
            await auth._validate_mcp_access(CLAIMS, loop.time() + 0.8)
        assert refused.value.status == 502
        assert JTI not in auth._access_cache
        assert app.requests == 2


async def test_a_refusal_whose_body_outlasts_the_callers_budget_drops_the_pass():
    app = _LocalApp((_PASS, "close"), (_BROKEN_403, "stall"))
    async with app as auth:
        await auth._validate_mcp_access(CLAIMS)
        _expire(auth, 5)
        loop = asyncio.get_running_loop()
        deadline = loop.time() + 0.4
        with pytest.raises(TimeoutError):
            async with asyncio.timeout_at(deadline):
                await auth._validate_mcp_access(CLAIMS, deadline)
        assert JTI not in auth._access_cache


async def test_a_5xx_whose_body_breaks_is_still_an_app_failure():
    app = _LocalApp((_PASS, "close"), (_BROKEN_503, "close"), (_BROKEN_503, "close"))
    async with app as auth:
        await auth._validate_mcp_access(CLAIMS)
        _expire(auth, 5)
        verdict = await auth._validate_mcp_access(CLAIMS)
        assert verdict.stale is True
        assert app.requests == 3


async def test_a_stale_admission_is_its_own_auth_stage_outcome(caplog):
    auth, _ = _authenticator(204, 503, 503)
    auth._validate_jwt = lambda bearer, has_kid=False: CLAIMS
    auth._lookup_api_key = AsyncMock(return_value="sugra_k")
    bearer = jwt.encode({"sub": "11"}, "k" * 32, algorithm="HS256")

    with caplog.at_level(logging.INFO, logger="sugra_mcp.auth"):
        await auth.resolve(bearer)
        _expire(auth, 30)
        await auth.resolve(bearer)

    outcomes = [r.getMessage().split()[1] for r in caplog.records
                if r.getMessage().startswith("auth_stages ")]
    assert outcomes == ["outcome=ok", "outcome=ok_stale"]


async def test_resolve_hands_its_deadline_to_the_access_check():
    auth, http = _authenticator(503, 200)
    auth._validate_jwt = lambda bearer, has_kid=False: CLAIMS
    auth._lookup_api_key = AsyncMock(return_value="sugra_k")
    bearer = jwt.encode({"sub": "11"}, "k" * 32, algorithm="HS256")
    loop = asyncio.get_running_loop()

    # A budget with no room for the retry: the check must not count its own.
    with pytest.raises(AuthError) as exc:
        await auth.resolve(bearer, deadline=loop.time() + 1.5)
    assert exc.value.status == 502
    assert http.post_calls == 1


# The middleware hands its budget down by argument; nothing ambient is left
# for a task started during resolve.

def _middleware_app(resolve) -> Starlette:
    auth = Authenticator(_config())
    auth.resolve = resolve

    async def ok(_request):
        return JSONResponse({"ok": True})

    app = Starlette(routes=[Route("/mcp", ok, methods=["POST"])])
    app.add_middleware(AuthMiddleware, authenticator=auth)
    return app


async def _post(app: Starlette) -> httpx.Response:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.post("/mcp", json={}, headers={"authorization": "Bearer t"})


async def test_the_middleware_hands_its_budget_to_resolve():
    seen = {}

    async def resolve(bearer, deadline=None):
        seen["deadline"] = deadline
        seen["now"] = asyncio.get_running_loop().time()
        return ResolvedAuth(api_key="sugra_k", user_id=1, method="oauth")

    response = await _post(_middleware_app(resolve))

    assert response.status_code == 200
    remaining = seen["deadline"] - seen["now"]
    assert 0 < remaining <= auth_mod.AUTH_BUDGET_SECONDS


async def test_the_middleware_hands_its_budget_when_resolve_refuses():
    seen = {}

    async def resolve(bearer, deadline=None):
        seen["deadline"] = deadline
        raise AuthError("Access token refused (token_revoked), sign in again")

    response = await _post(_middleware_app(resolve))

    assert response.status_code == 401
    assert seen["deadline"] is not None


async def test_the_middleware_budget_cancels_resolve_at_the_deadline_it_handed(monkeypatch):
    monkeypatch.setattr(auth_mod, "AUTH_BUDGET_SECONDS", 0.2)
    seen = {}

    async def resolve(bearer, deadline=None):
        seen["deadline"] = deadline
        try:
            await asyncio.sleep(5)
        except asyncio.CancelledError:
            seen["cancelled_at"] = asyncio.get_running_loop().time()
            raise

    response = await _post(_middleware_app(resolve))

    assert response.status_code == 503
    assert response.json()["error"] == "auth_timeout"
    assert abs(seen["cancelled_at"] - seen["deadline"]) < 0.1


async def test_a_task_started_during_resolve_keeps_its_own_budget(monkeypatch):
    monkeypatch.setattr(auth_mod, "AUTH_BUDGET_SECONDS", 0.3)
    monkeypatch.setattr(auth_mod, "ACCESS_RETRY_MIN_ATTEMPT_SECONDS", 0.05)
    monkeypatch.setattr(auth_mod, "ACCESS_RETRY_RESERVE_SECONDS", 0.05)
    worker, worker_http = _authenticator(503, 200)
    go = asyncio.Event()
    children = []

    async def child():
        await go.wait()
        return await worker._validate_mcp_access(CLAIMS)

    async def resolve(bearer, deadline=None):
        children.append(asyncio.create_task(child()))
        return ResolvedAuth(api_key="sugra_k", user_id=1, method="oauth")

    response = await _post(_middleware_app(resolve))
    assert response.status_code == 200
    await asyncio.sleep(0.35)  # the request's own budget is over
    go.set()

    verdict = await children[0]
    assert verdict.platform == "anthropic"
    assert worker_http.post_calls == 2, "the task skipped its retry on a budget it inherited"
