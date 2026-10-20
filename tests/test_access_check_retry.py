"""An APP access check that fails on the APP's side is retried inside the auth budget.

While the APP restarts (an APP deploy) its access check answers a transport
error or a 5xx for some seconds. The check is tried again after a pause that
grows to a cap, for as long as the auth budget holds the pause, a minimum
attempt and the reserve, the room rechecked after every pause. Retrying never
changes who gets in: any other APP answer (1xx, 3xx, 4xx, 600 and up) is a
refusal and final, a failure after the last attempt is the 502, and no pass is
ever served without a fresh APP check.
"""

from __future__ import annotations

import asyncio
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

JTI = "retry-token-id"
CLAIMS = _JwtClaims(user_id=11, access_token_id=JTI)


class _ScriptedHttp:
    """Answers the access check from a script; the last step repeats.

    A step is an int status, an exception, "hang", ("after", event, status):
    wait for the event, then answer the status, or ("until", seconds, failure):
    answer the failure until that long after the first call, then a pass.
    """

    def __init__(self, *script) -> None:
        self.script = list(script)
        self.post_calls = 0
        self.times: list[float] = []

    async def post(self, url, headers=None, json=None):
        self.post_calls += 1
        loop = asyncio.get_running_loop()
        self.times.append(loop.time())
        step = self.script.pop(0) if len(self.script) > 1 else self.script[0]
        await asyncio.sleep(0)  # a network call always yields to the loop
        if isinstance(step, tuple) and step[0] == "until":
            _, seconds, failure = step
            step = failure if loop.time() < self.times[0] + seconds else 200
        elif isinstance(step, tuple):
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


def _timing(monkeypatch, *, pause, max_pause, min_attempt, reserve, budget) -> None:
    monkeypatch.setattr(auth_mod, "ACCESS_RETRY_PAUSE_SECONDS", pause)
    monkeypatch.setattr(auth_mod, "ACCESS_RETRY_MAX_PAUSE_SECONDS", max_pause)
    monkeypatch.setattr(auth_mod, "ACCESS_RETRY_MIN_ATTEMPT_SECONDS", min_attempt)
    monkeypatch.setattr(auth_mod, "ACCESS_RETRY_RESERVE_SECONDS", reserve)
    monkeypatch.setattr(auth_mod, "AUTH_BUDGET_SECONDS", budget)


@pytest.fixture
def scaled(monkeypatch):
    """The production timing at a tenth: pause 0.1 s to 0.3 s, a 1.5 s budget."""
    _timing(monkeypatch, pause=0.1, max_pause=0.3, min_attempt=0.1, reserve=0.1, budget=1.5)


@pytest.fixture
def fast(monkeypatch):
    _timing(monkeypatch, pause=0.02, max_pause=0.02, min_attempt=0.05, reserve=0.05, budget=0.5)


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


def _bearer_resolver(auth: Authenticator) -> str:
    auth._validate_jwt = lambda bearer, has_kid=False: CLAIMS
    auth._lookup_api_key = AsyncMock(return_value="sugra_k")
    return jwt.encode({"sub": "11"}, "k" * 32, algorithm="HS256")


# The retry rides out a failure on the APP's side.

@pytest.mark.parametrize("failure", [
    httpx.ConnectError("refused"),
    503,
], ids=["transport", "http_503"])
async def test_a_failure_on_the_app_side_is_retried_until_it_passes(fast, failure):
    auth, http = _authenticator(failure, failure, 200)
    assert await auth._validate_mcp_access(CLAIMS) == "anthropic"
    assert http.post_calls == 3
    assert auth._access_cache[JTI].expires_at > time.monotonic()
    # The pass a retry earned is cached like any other.
    assert await auth._validate_mcp_access(CLAIMS) == "anthropic"
    assert http.post_calls == 3


@pytest.mark.parametrize("status", [500, 599])
async def test_every_5xx_is_retried(fast, status):
    auth, http = _authenticator(status, 200)
    assert await auth._validate_mcp_access(CLAIMS) == "anthropic"
    assert http.post_calls == 2


async def test_the_pause_doubles_up_to_its_cap(scaled):
    auth, http = _authenticator(503, 503, 503, 503, 200)
    assert await auth._validate_mcp_access(CLAIMS) == "anthropic"
    gaps = [b - a for a, b in zip(http.times, http.times[1:], strict=False)]
    assert len(gaps) == 4
    assert 0.09 <= gaps[0] < 0.18
    assert 0.19 <= gaps[1] < 0.28
    assert 0.29 <= gaps[2] < 0.38, "the pause grew past its cap"
    assert 0.29 <= gaps[3] < 0.38


async def test_the_retries_ride_out_a_five_second_outage():
    # Production timing: attempts at 0, 1, 3 and 6 s inside the 15 s budget.
    auth, http = _authenticator(("until", 5.0, httpx.ConnectError("refused")))
    loop = asyncio.get_running_loop()
    started = loop.time()
    assert await auth._validate_mcp_access(CLAIMS) == "anthropic"
    assert http.post_calls == 4
    assert loop.time() - started < auth_mod.AUTH_BUDGET_SECONDS - 1


async def test_the_retries_ride_out_a_restart_of_most_of_the_budget(scaled):
    # At a tenth of the timing: a 1.0 s outage in a 1.5 s budget is a 10 s
    # restart in the 15 s one.
    auth, http = _authenticator(("until", 1.0, 503))
    assert await auth._validate_mcp_access(CLAIMS) == "anthropic"
    assert http.post_calls >= 4


async def test_an_outage_past_the_budget_gets_502_before_the_reserve(scaled):
    auth, http = _authenticator(204, 503)
    await auth._validate_mcp_access(CLAIMS)
    _expire(auth, 1)
    loop = asyncio.get_running_loop()
    deadline = loop.time() + 1.5
    async with asyncio.timeout_at(deadline):
        with pytest.raises(AuthError) as exc:
            await auth._validate_mcp_access(CLAIMS, deadline)
    assert exc.value.status == 502
    assert "HTTP 503" in str(exc.value)
    assert loop.time() <= deadline - 0.05
    assert http.post_calls >= 4


async def test_an_expired_pass_is_never_served_during_an_outage(fast):
    auth, http = _authenticator(204, httpx.ConnectError("refused"))
    await auth._validate_mcp_access(CLAIMS)
    _expire(auth, 1)
    with pytest.raises(AuthError) as exc:
        await auth._validate_mcp_access(CLAIMS)
    assert exc.value.status == 502
    assert http.post_calls > 2


# The budget is rechecked after each pause, which can run long (codex S2).

@pytest.mark.parametrize("handed", [False, True], ids=["direct_resolve", "handed_deadline"])
async def test_a_pause_that_runs_long_leaves_no_attempt_past_the_budget(monkeypatch, handed):
    _timing(monkeypatch, pause=0.5, max_pause=0.5, min_attempt=0.5, reserve=0.3, budget=2.0)
    real_sleep = asyncio.sleep

    async def late_sleep(seconds, *args, **kwargs):
        # The retry pause wakes a whole second late, as on a starved loop.
        return await real_sleep(seconds + 1.0 if seconds == 0.5 else seconds, *args, **kwargs)

    monkeypatch.setattr(asyncio, "sleep", late_sleep)
    auth, http = _authenticator(503, 200)
    bearer = _bearer_resolver(auth)
    loop = asyncio.get_running_loop()
    started = loop.time()

    with pytest.raises(AuthError) as exc:
        if handed:
            await auth.resolve(bearer, deadline=started + 2.0)
        else:
            await auth.resolve(bearer)  # no outer timeout at all
    assert exc.value.status == 502
    assert http.post_calls == 1, "an attempt ran after the pause left no room for it"
    assert loop.time() - started < 2.0


async def test_a_hung_retry_after_a_long_pause_ends_inside_a_direct_resolves_budget(monkeypatch):
    _timing(monkeypatch, pause=0.5, max_pause=0.5, min_attempt=0.5, reserve=0.3, budget=2.0)
    real_sleep = asyncio.sleep

    async def late_sleep(seconds, *args, **kwargs):
        # Late, but still leaving room for one attempt before the reserve.
        return await real_sleep(seconds + 0.3 if seconds == 0.5 else seconds, *args, **kwargs)

    monkeypatch.setattr(asyncio, "sleep", late_sleep)
    auth, http = _authenticator(503, "hang")
    bearer = _bearer_resolver(auth)
    loop = asyncio.get_running_loop()
    started = loop.time()

    # No outer timeout: only the access check's own cut can end the hung retry.
    with pytest.raises(AuthError) as exc:
        await asyncio.wait_for(auth.resolve(bearer), 10)
    assert exc.value.status == 502
    assert http.post_calls == 2
    assert loop.time() - started < 2.0


# A refusal is final.

async def test_a_4xx_is_never_retried():
    auth, http = _authenticator(403, 200)
    with pytest.raises(AuthError) as exc:
        await auth._validate_mcp_access(CLAIMS)
    assert exc.value.status == 401
    assert http.post_calls == 1
    assert JTI not in auth._access_cache


@pytest.mark.parametrize("status", [100, 302, 404, 600])
async def test_an_answer_outside_2xx_and_5xx_is_a_refusal(status):
    auth, http = _authenticator(status, 200)
    with pytest.raises(AuthError) as exc:
        await auth._validate_mcp_access(CLAIMS)
    assert exc.value.status == 502
    assert f"HTTP {status}" in str(exc.value)
    assert http.post_calls == 1, "only a transport error or a 5xx is retried"
    assert JTI not in auth._access_cache


# The live pass cache.

async def test_a_wall_clock_step_never_moves_a_pass(monkeypatch):
    clock = {"mono": 1000.0, "wall": 1_900_000_000.0}
    monkeypatch.setattr(auth_mod, "time", SimpleNamespace(
        monotonic=lambda: clock["mono"], time=lambda: clock["wall"]))
    auth, http = _authenticator(200)
    await auth._validate_mcp_access(CLAIMS)

    # Inside the TTL, the wall clock jumps an hour ahead: still the cached pass.
    clock["mono"] += auth_mod.ACCESS_VALIDATION_TTL_SECONDS - 1
    clock["wall"] += 3600
    await auth._validate_mcp_access(CLAIMS)
    assert http.post_calls == 1

    # Past the TTL, the wall clock steps back an hour: a fresh check all the same.
    clock["mono"] += 2
    clock["wall"] -= 7200
    await auth._validate_mcp_access(CLAIMS)
    assert http.post_calls == 2


async def test_a_pass_started_before_a_revocation_is_never_stored():
    release = asyncio.Event()
    auth, http = _authenticator(("after", release, 200), 403, 200)
    early = asyncio.create_task(auth._validate_mcp_access(CLAIMS))
    await _started(http, 1)

    with pytest.raises(AuthError) as revoked:
        await auth._validate_mcp_access(CLAIMS)
    assert revoked.value.status == 401

    release.set()
    # The APP's earlier answer admits the request that asked for it ...
    assert await early == "anthropic"
    # ... and is never stored over the refusal recorded meanwhile.
    assert JTI not in auth._access_cache
    await auth._validate_mcp_access(CLAIMS)
    assert http.post_calls == 3, "the revoked token was served from a late pass"


async def test_a_check_started_after_a_revocation_stores_its_pass():
    auth, _ = _authenticator(403, 200)
    with pytest.raises(AuthError):
        await auth._validate_mcp_access(CLAIMS)
    assert await auth._validate_mcp_access(CLAIMS) == "anthropic"
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


# Every attempt is cut.

async def test_a_hung_first_check_is_cut_at_the_reserve(scaled):
    auth, http = _authenticator("hang", 200)
    loop = asyncio.get_running_loop()
    deadline = loop.time() + 1.5
    # The caller's own budget: a check left uncut would end here as a
    # TimeoutError instead of the 502.
    async with asyncio.timeout_at(deadline):
        with pytest.raises(AuthError) as exc:
            await auth._validate_mcp_access(CLAIMS, deadline)
    assert exc.value.status == 502
    assert http.post_calls == 1
    assert loop.time() <= deadline - 0.1 + 0.05


async def test_a_hung_retry_is_cut_before_the_budget_ends(scaled):
    auth, http = _authenticator(503, "hang")
    loop = asyncio.get_running_loop()
    deadline = loop.time() + 1.5
    async with asyncio.timeout_at(deadline):
        with pytest.raises(AuthError) as exc:
            await auth._validate_mcp_access(CLAIMS, deadline)
    assert exc.value.status == 502
    assert http.post_calls == 2
    assert loop.time() <= deadline - 0.1 + 0.05


async def test_a_short_budget_still_cuts_its_one_check_at_the_deadline():
    auth, http = _authenticator("hang")
    loop = asyncio.get_running_loop()
    deadline = loop.time() + 0.3  # less than the reserve and a minimum attempt
    with pytest.raises(AuthError) as exc:
        await asyncio.wait_for(auth._validate_mcp_access(CLAIMS, deadline), 3)
    assert exc.value.status == 502
    assert http.post_calls == 1
    assert loop.time() < deadline + 0.2


async def test_a_short_budget_still_runs_its_one_check():
    auth, http = _authenticator(200)
    loop = asyncio.get_running_loop()
    assert await auth._validate_mcp_access(CLAIMS, loop.time() + 0.3) == "anthropic"
    assert http.post_calls == 1


# A refusal whose body breaks, over a real socket: retried, then refused.

def _raw(status: int, reason: str, body: bytes, length: int | None = None) -> bytes:
    size = len(body) if length is None else length
    return (
        f"HTTP/1.1 {status} {reason}\r\nContent-Type: application/json\r\n"
        f"Content-Length: {size}\r\nConnection: close\r\n\r\n"
    ).encode() + body


class _LocalApp:
    def __init__(self, *answers: bytes) -> None:
        self.answers = list(answers)
        self.requests = 0
        self.server: asyncio.Server | None = None

    async def _handle(self, reader, writer) -> None:
        head = await reader.readuntil(b"\r\n\r\n")
        length = re.search(rb"content-length: (\d+)", head, re.IGNORECASE)
        await reader.readexactly(int(length.group(1)) if length else 0)
        self.requests += 1
        writer.write(self.answers.pop(0))
        await writer.drain()
        writer.close()

    async def __aenter__(self) -> Authenticator:
        self.server = await asyncio.start_server(self._handle, "127.0.0.1", 0)
        port = self.server.sockets[0].getsockname()[1]
        self.auth = Authenticator(_config(f"http://127.0.0.1:{port}"))
        return self.auth

    async def __aexit__(self, *exc) -> None:
        await self.auth.aclose()
        self.server.close()
        await self.server.wait_closed()


async def test_a_refusal_whose_body_breaks_is_retried_and_never_admits(fast):
    app = _LocalApp(
        _raw(403, "Forbidden", b'{"error": "tok', length=200),
        _raw(403, "Forbidden", b'{"error": "token_revoked"}'),
    )
    async with app as auth:
        with pytest.raises(AuthError) as exc:
            await auth._validate_mcp_access(CLAIMS)
        assert exc.value.status == 401
        assert app.requests == 2
        assert JTI not in auth._access_cache


# The deadline goes down by argument.

async def test_resolve_hands_its_deadline_to_the_access_check():
    auth, http = _authenticator(503, 200)
    bearer = _bearer_resolver(auth)
    loop = asyncio.get_running_loop()

    # A budget with no room for a retry: the check must not count its own.
    with pytest.raises(AuthError) as exc:
        await auth.resolve(bearer, deadline=loop.time() + 1.5)
    assert exc.value.status == 502
    assert http.post_calls == 1


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
    _timing(monkeypatch, pause=0.05, max_pause=0.05, min_attempt=0.05, reserve=0.05, budget=0.3)
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

    assert await children[0] == "anthropic"
    assert worker_http.post_calls == 2, "the task skipped its retry on a budget it inherited"
