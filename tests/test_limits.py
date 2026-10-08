"""The request limits: settings, lanes, budget, shield and per-key counters, all counted in memory.

Time is a clock the test moves. Nothing opens a socket.
"""

from __future__ import annotations

import asyncio
import base64
import json
from typing import Any

import pytest

from sugra_api_mcp import gate, limits, observability, server
from sugra_api_mcp.limits import (
    Admission,
    AdmissionMiddleware,
    Counters,
    Held,
    Limits,
    Over,
    Settings,
    digest,
    load_settings,
)

SAMPLE_CALLER = "sugra_testkey_0123456789"
ADDRESS = "8.8.8.8"


class FakeClock:
    """A clock the test moves; the limits read the same one."""

    def __init__(self, now: float = 1_000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def build(*, per_minute: int | None = None, per_day: int | None = None) -> tuple[Limits, FakeClock]:
    clock = FakeClock()
    return Limits(Settings(per_minute, per_day), clock=clock, wall=clock), clock


def scope_of(
    method: str = "POST", bearer: str | None = None, peer: str = ADDRESS, path: str = "/mcp"
) -> dict[str, Any]:
    headers: list[tuple[bytes, bytes]] = [(b"host", b"app.test")]
    if bearer is not None:
        headers.append((b"authorization", f"Bearer {bearer}".encode()))
    return {
        "type": "http",
        "method": method,
        "path": path,
        "headers": headers,
        "client": (peer, 50_000),
        "state": {},
    }


def jwt_of(expiry: float | None) -> str:
    def part(value: dict[str, Any]) -> str:
        return base64.urlsafe_b64encode(json.dumps(value).encode()).decode().rstrip("=")

    claims: dict[str, Any] = {"sub": "1"} if expiry is None else {"sub": "1", "exp": expiry}
    return f"{part({'alg': 'RS256'})}.{part(claims)}.signature"


class Inner:
    """The app behind the admission: answers at once, or holds the request until released."""

    def __init__(self, *, hold: bool = False, status: int = 200, oauth: bool = False) -> None:
        self.hold = hold
        self.status = status
        self.oauth = oauth
        self.entered = 0
        self.proceed = asyncio.Event()
        self.seen_states: list[dict[str, Any]] = []

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        self.entered += 1
        self.seen_states.append(scope.get("state", {}))
        if self.hold:
            # Bounded, so a broken lane fails the test instead of hanging it.
            await asyncio.wait_for(self.proceed.wait(), 5.0)
        if self.oauth:
            scope["state"][server.REQUEST_PRINCIPAL_STATE] = server.RequestPrincipal(method="oauth")
        await send({"type": "http.response.start", "status": self.status, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})


async def call(app: Any, scope: dict[str, Any]) -> tuple[int, dict[str, str], bytes]:
    sent: list[dict[str, Any]] = []

    async def receive() -> dict[str, Any]:
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message: dict[str, Any]) -> None:
        sent.append(message)

    await app(scope, receive, send)
    start = sent[0]
    headers = {k.decode().lower(): v.decode() for k, v in start["headers"]}
    return start["status"], headers, b"".join(m.get("body", b"") for m in sent[1:])


async def settle() -> None:
    for _ in range(60):
        await asyncio.sleep(0)


async def hold_requests(app: Any, scopes: list[dict[str, Any]]) -> list[asyncio.Task[Any]]:
    tasks = [asyncio.create_task(call(app, scope)) for scope in scopes]
    await settle()
    return tasks


def peers(count: int) -> list[str]:
    """Distinct public addresses."""
    return [f"93.184.{n // 250}.{n % 250 + 1}" for n in range(count)]


def middleware(shared: Limits, inner: Any) -> AdmissionMiddleware:
    return AdmissionMiddleware(inner, limits=shared)


def key_admission(shared: Limits, api_key: str, *, recognised: bool) -> Admission:
    """The admission of one request carrying api_key, which the process has recognised or not."""
    if recognised:
        shared.recognised.remember(digest(api_key), 300)
    return Admission(shared, limits.KIND_API_KEY, digest(api_key), recognised)


async def a_call(admission: Admission, api_key: str, *, outcome: str = gate.SUCCEEDED, api_requests: int = 1) -> bool:
    """One tool call as server.py runs it: charged before, a step to run, settled after. True when it ran."""
    charge = admission.before_call(api_key)
    if charge.refusal is not None:
        return False
    await asyncio.sleep(0)
    admission.after_call(charge.held, outcome, api_requests)
    return True


def charged(admission: Admission, api_key: str | None) -> Held | None:
    """The handle of a call that before_call let run; the test fails if it was refused."""
    charge = admission.before_call(api_key)
    assert charge.refusal is None
    return charge.held


def minute_name(api_key: str) -> str:
    return f"m:{digest(api_key)}"


def day_name(api_key: str) -> str:
    return f"d:{digest(api_key)}"


# ---- Off, and the settings ----


@pytest.mark.parametrize("value", [None, "", "0", "off", "false", "no", "banana", "2"])
def test_every_value_but_a_truthy_one_is_off(monkeypatch, value: str | None) -> None:
    monkeypatch.delenv("SUGRA_MCP_LIMITS", raising=False)
    if value is not None:
        monkeypatch.setenv("SUGRA_MCP_LIMITS", value)
    monkeypatch.setenv("SUGRA_MCP_KEY_LIMIT_PER_MINUTE", "not a number")
    # Off reads no other setting, so even a bad one cannot stop it.
    assert limits.build_limits() is None


@pytest.mark.parametrize("value", ["1", "true", "YES", " on "])
def test_a_truthy_value_builds_the_limits(monkeypatch, value: str) -> None:
    monkeypatch.setenv("SUGRA_MCP_LIMITS", value)
    monkeypatch.delenv("SUGRA_MCP_KEY_LIMIT_PER_MINUTE", raising=False)
    monkeypatch.delenv("SUGRA_MCP_KEY_LIMIT_PER_DAY", raising=False)
    shared = limits.build_limits()
    assert isinstance(shared, Limits)
    assert not shared.has_key_limits


def test_settings_defaults_and_values() -> None:
    assert load_settings({}) == Settings(None, None)
    settings = load_settings({"SUGRA_MCP_KEY_LIMIT_PER_MINUTE": "7", "SUGRA_MCP_KEY_LIMIT_PER_DAY": "900"})
    assert (settings.key_limit_per_minute, settings.key_limit_per_day) == (7, 900)


@pytest.mark.parametrize(
    "extra",
    [
        {"SUGRA_MCP_KEY_LIMIT_PER_MINUTE": "0"},
        {"SUGRA_MCP_KEY_LIMIT_PER_MINUTE": "-3"},
        {"SUGRA_MCP_KEY_LIMIT_PER_DAY": "many"},
    ],
)
def test_a_bad_setting_stops_the_start(extra: dict[str, str]) -> None:
    with pytest.raises(ValueError, match="SUGRA_MCP_KEY_LIMIT_PER"):
        load_settings(extra)


# ---- The budget, the lanes ----


async def test_requests_without_recognised_credentials_hit_the_budget() -> None:
    shared, clock = build()
    app = middleware(shared, Inner())
    statuses = [(await call(app, scope_of()))[0] for _ in range(25)]
    assert statuses == [200] * 20 + [429] * 5
    status, headers, body = await call(app, scope_of())
    assert status == 429
    assert int(headers["retry-after"]) >= 1
    refusal = json.loads(body)
    assert refusal["error"] == "rate_limited"
    assert refusal["scope"] == "budget"
    assert refusal["retry_after"] == int(headers["retry-after"])
    clock.advance(1.0)
    assert (await call(app, scope_of()))[0] == 200


async def test_the_budget_never_counts_a_recognised_credential() -> None:
    shared, _ = build()
    shared.recognised.remember(digest(SAMPLE_CALLER), 300)
    app = middleware(shared, Inner())
    for _ in range(100):
        assert (await call(app, scope_of(bearer=SAMPLE_CALLER)))[0] == 200


async def test_the_other_lane_has_eight_seats_and_the_recognised_lane_stays_open() -> None:
    shared, _ = build()
    inner = Inner(hold=True)
    app = middleware(shared, inner)
    shared.recognised.remember(digest(SAMPLE_CALLER), 300)
    held = await hold_requests(app, [scope_of(peer=a) for a in peers(8)])
    assert inner.entered == 8
    status, headers, body = await call(app, scope_of())
    assert status == 503
    assert headers["retry-after"] == "1"
    assert json.loads(body)["error"] == "server_busy"
    assert json.loads(body)["scope"] == "lane_other"
    # A recognised credential is served while the other lane is full.
    served = await hold_requests(app, [scope_of(bearer=SAMPLE_CALLER)])
    assert inner.entered == 9
    inner.proceed.set()
    results = await asyncio.gather(*held, *served)
    assert all(r[0] == 200 for r in results)
    assert (shared.seats.other, shared.seats.recognised, shared.seats.unverified_keys) == (0, 0, 0)


async def test_the_recognised_lane_has_twenty_four_seats() -> None:
    shared, _ = build()
    inner = Inner(hold=True)
    app = middleware(shared, inner)
    shared.recognised.remember(digest(SAMPLE_CALLER), 300)
    held = await hold_requests(app, [scope_of(bearer=SAMPLE_CALLER) for _ in range(24)])
    assert inner.entered == 24
    status, _, body = await call(app, scope_of(bearer=SAMPLE_CALLER))
    assert (status, json.loads(body)["scope"]) == (503, "lane_recognised")
    inner.proceed.set()
    await asyncio.gather(*held)
    assert shared.seats.recognised == 0


async def test_at_most_four_seats_go_to_keys_the_api_has_not_accepted() -> None:
    shared, _ = build()
    inner = Inner(hold=True)
    app = middleware(shared, inner)
    held = await hold_requests(app, [scope_of(bearer=f"sugra_fake_{n}", peer=a) for n, a in enumerate(peers(4))])
    assert inner.entered == 4
    status, _, body = await call(app, scope_of(bearer="sugra_fake_extra"))
    assert (status, json.loads(body)["scope"]) == (503, "lane_other")
    # Requests with no key and JWTs still have the other four seats.
    more = await hold_requests(app, [scope_of(), scope_of(bearer=jwt_of(None)), scope_of(), scope_of()])
    assert inner.entered == 8
    inner.proceed.set()
    await asyncio.gather(*held, *more)
    assert (shared.seats.other, shared.seats.unverified_keys) == (0, 0)


async def test_only_posts_take_seats_and_options_and_other_paths_pass_untouched() -> None:
    shared, _ = build()
    inner = Inner()
    app = middleware(shared, inner)
    assert (await call(app, scope_of(method="OPTIONS")))[0] == 200
    assert (await call(app, scope_of(path="/health")))[0] == 200
    assert (await call(app, scope_of(path="/.well-known/oauth-protected-resource")))[0] == 200
    assert all("sugra_admission" not in state for state in inner.seen_states)
    assert shared.budget._tokens == limits.BUDGET_BURST
    for method in ("GET", "DELETE"):
        assert (await call(middleware(shared, Inner()), scope_of(method=method)))[0] == 200
    assert (shared.seats.other, shared.seats.recognised) == (0, 0)


async def test_a_seat_comes_back_after_an_error_and_after_a_cancellation() -> None:
    shared, _ = build()

    async def broken(scope: Any, receive: Any, send: Any) -> None:
        raise RuntimeError("inner failed")

    with pytest.raises(RuntimeError):
        await call(middleware(shared, broken), scope_of())
    assert shared.seats.other == 0
    inner = Inner(hold=True)
    task = asyncio.create_task(call(middleware(shared, inner), scope_of(bearer=SAMPLE_CALLER)))
    await settle()
    assert (shared.seats.other, shared.seats.unverified_keys) == (1, 1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert (shared.seats.other, shared.seats.unverified_keys) == (0, 0)


async def test_a_refusal_for_no_seat_holds_no_seat() -> None:
    shared, _ = build()
    inner = Inner(hold=True)
    app = middleware(shared, inner)
    held = await hold_requests(app, [scope_of(peer=a) for a in peers(8)])
    for _ in range(5):
        assert (await call(app, scope_of()))[0] == 503
    inner.proceed.set()
    await asyncio.gather(*held)
    assert shared.seats.other == 0


async def test_a_failure_of_the_limits_lets_the_request_run(monkeypatch, caplog) -> None:
    shared, _ = build()

    def broken(scope: Any) -> Any:
        raise RuntimeError("boom")

    monkeypatch.setattr(shared, "admit", broken)
    inner = Inner()
    assert (await call(middleware(shared, inner), scope_of(bearer=SAMPLE_CALLER)))[0] == 200
    assert inner.entered == 1


# ---- Recognised credentials ----


async def test_a_jwt_the_auth_layer_verified_is_recognised_until_it_expires() -> None:
    shared, clock = build()
    token = jwt_of(clock() + 90)
    app = middleware(shared, Inner(oauth=True))
    assert (await call(app, scope_of(bearer=token)))[0] == 200
    assert shared.recognised.is_recognised(digest(token))
    clock.advance(89.0)
    assert shared.recognised.is_recognised(digest(token))
    clock.advance(2.0)
    assert not shared.recognised.is_recognised(digest(token))


async def test_a_jwt_is_recognised_for_ten_minutes_at_most() -> None:
    shared, clock = build()
    far = jwt_of(clock() + 86_400)
    garbled = "not.a-jwt.at-all"
    app = middleware(shared, Inner(oauth=True))
    await call(app, scope_of(bearer=far))
    await call(app, scope_of(bearer=garbled))
    clock.advance(599.0)
    assert shared.recognised.is_recognised(digest(far))
    assert shared.recognised.is_recognised(digest(garbled))
    clock.advance(2.0)
    assert not shared.recognised.is_recognised(digest(far))
    assert not shared.recognised.is_recognised(digest(garbled))


async def test_a_jwt_the_auth_layer_did_not_verify_is_never_recognised() -> None:
    shared, _ = build()
    app = middleware(shared, Inner(status=401))
    for _ in range(10):
        assert (await call(app, scope_of(bearer=jwt_of(None))))[0] == 401
    assert len(shared.recognised) == 0


async def test_a_recognised_jwt_the_auth_layer_then_refuses_is_struck_for_its_term() -> None:
    shared, clock = build()
    token = jwt_of(clock() + 500)
    await call(middleware(shared, Inner(oauth=True)), scope_of(bearer=token))
    assert shared.recognised.is_recognised(digest(token))
    await call(middleware(shared, Inner(status=403)), scope_of(bearer=token))
    assert not shared.recognised.is_recognised(digest(token))
    # Verified again within the earlier term: it does not come back.
    await call(middleware(shared, Inner(oauth=True)), scope_of(bearer=token))
    assert not shared.recognised.is_recognised(digest(token))
    clock.advance(501.0)
    assert not shared.recognised.is_recognised(digest(token))


async def test_a_jwt_refused_by_a_request_that_began_before_it_was_recognised_is_struck() -> None:
    shared, clock = build()
    token = jwt_of(clock() + 500)
    verified = Inner(hold=True, oauth=True)
    refused = Inner(hold=True, status=403)
    first = asyncio.create_task(call(middleware(shared, verified), scope_of(bearer=token)))
    second = asyncio.create_task(call(middleware(shared, refused), scope_of(bearer=token)))
    await settle()
    assert verified.entered == 1 and refused.entered == 1
    assert not shared.recognised.is_recognised(digest(token))
    # One request verifies and records the token, then the other, which began
    # before it was recognised, is refused.
    verified.proceed.set()
    assert (await first)[0] == 200
    assert shared.recognised.is_recognised(digest(token))
    refused.proceed.set()
    assert (await second)[0] == 403
    assert not shared.recognised.is_recognised(digest(token))
    # Verified again within the earlier term: it does not come back.
    await call(middleware(shared, Inner(oauth=True)), scope_of(bearer=token))
    assert not shared.recognised.is_recognised(digest(token))


def test_the_recognised_memory_is_bounded_and_holds_digests_only() -> None:
    clock = FakeClock()
    memory = limits.RecognisedMemory(clock)
    for n in range(limits.RECOGNISED_MAX_ENTRIES):
        assert memory.remember(digest(f"k{n}"), 300)
    assert not memory.remember(digest("one more"), 300)
    assert len(memory) == limits.RECOGNISED_MAX_ENTRIES
    clock.advance(301.0)
    assert memory.remember(digest("after the sweep"), 300)
    assert len(memory) == 1


def test_a_key_the_api_accepted_is_recognised_for_five_minutes() -> None:
    shared, clock = build()
    admission = Admission(shared, limits.KIND_API_KEY, digest(SAMPLE_CALLER), False)
    admission.after_call(None, gate.FAILED, 1)
    admission.after_call(None, gate.SUCCEEDED, 0)
    assert not shared.recognised.is_recognised(digest(SAMPLE_CALLER))
    admission.after_call(None, gate.SUCCEEDED, 1)
    assert shared.recognised.is_recognised(digest(SAMPLE_CALLER))
    clock.advance(299.0)
    assert shared.recognised.is_recognised(digest(SAMPLE_CALLER))
    clock.advance(2.0)
    assert not shared.recognised.is_recognised(digest(SAMPLE_CALLER))


def test_a_confirmed_key_the_api_refuses_is_dropped_and_stays_out() -> None:
    shared, clock = build()
    admission = Admission(shared, limits.KIND_API_KEY, digest(SAMPLE_CALLER), False)
    admission.after_call(None, gate.SUCCEEDED, 1)
    admission.after_call(None, gate.AUTH_REFUSED, 1)
    assert not shared.recognised.is_recognised(digest(SAMPLE_CALLER))
    admission.after_call(None, gate.SUCCEEDED, 1)
    assert not shared.recognised.is_recognised(digest(SAMPLE_CALLER))
    clock.advance(301.0)
    admission.after_call(None, gate.SUCCEEDED, 1)
    assert shared.recognised.is_recognised(digest(SAMPLE_CALLER))


# ---- The failed-check shield ----


async def refuse_checks(shared: Limits, clock: FakeClock, count: int, peer: str = ADDRESS) -> None:
    """Send count new made-up keys from peer, each one refused by the Sugra API."""
    app = middleware(shared, Inner())
    for n in range(count):
        scope = scope_of(bearer=f"sugra_bad_{peer}_{n}", peer=peer)
        assert (await call(app, scope))[0] == 200
        shared_admission(scope).after_call(None, gate.AUTH_REFUSED, 1)
        clock.advance(0.2)


def shared_admission(scope: dict[str, Any]) -> Any:
    return scope.get("state", {}).get(limits.ADMISSION_STATE)


def record_refusals(shared: Limits, clock: FakeClock, count: int, apart: float, address: str = ADDRESS) -> None:
    for _ in range(count):
        shared.shield.record(digest(address))
        clock.advance(apart)


def test_the_shield_holds_at_exactly_the_thirtieth_refusal_in_a_minute_and_not_at_twenty_nine() -> None:
    shared, clock = build()
    address = digest(ADDRESS)
    record_refusals(shared, clock, limits.SHIELD_FAILURES - 1, apart=1.0)
    assert shared.address_held(address) == 0
    shared.shield.record(address)
    assert 590 <= shared.address_held(address) <= 600
    clock.advance(601.0)
    assert shared.address_held(address) == 0


def test_the_window_slides_so_thirty_refusals_over_more_than_a_minute_hold_nothing() -> None:
    shared, clock = build()
    address = digest(ADDRESS)
    # 30 refusals 2.1 seconds apart span 60.9 seconds: never 30 inside one minute.
    record_refusals(shared, clock, limits.SHIELD_FAILURES, apart=2.1)
    assert shared.address_held(address) == 0
    # 2.0 seconds apart span 58 seconds, and 30 of them fall in one minute.
    shared, clock = build()
    record_refusals(shared, clock, limits.SHIELD_FAILURES, apart=2.0)
    assert shared.address_held(address) > 0


async def test_thirty_refusals_in_a_minute_from_one_address_hold_that_address_for_ten_minutes() -> None:
    shared, clock = build()
    await refuse_checks(shared, clock, 29)
    assert shared.address_held(digest(ADDRESS)) == 0
    await refuse_checks(shared, clock, 1)
    assert 590 <= shared.address_held(digest(ADDRESS)) <= 600
    app = middleware(shared, Inner())
    status, headers, body = await call(app, scope_of(bearer="sugra_new_key"))
    assert status == 429
    assert 590 <= int(headers["retry-after"]) <= 600
    assert json.loads(body)["scope"] == "failed_checks"
    assert shared.seats.other == 0
    clock.advance(601.0)
    assert (await call(app, scope_of(bearer="sugra_new_key")))[0] == 200


async def test_a_held_address_still_serves_everything_but_a_new_key() -> None:
    shared, clock = build()
    await refuse_checks(shared, clock, 30)
    shared.recognised.remember(digest(SAMPLE_CALLER), 300)
    app = middleware(shared, Inner(oauth=True))
    assert (await call(app, scope_of(bearer=SAMPLE_CALLER)))[0] == 200
    assert (await call(app, scope_of(bearer=jwt_of(None))))[0] == 200
    assert (await call(app, scope_of()))[0] == 200
    assert (await call(app, scope_of(bearer="sugra_other_key")))[0] == 429
    # Another address is not held.
    assert (await call(app, scope_of(bearer="sugra_other_key", peer="1.1.1.1")))[0] == 200


async def test_refusals_spread_over_many_addresses_hold_none() -> None:
    shared, clock = build()
    for address in peers(40):
        await refuse_checks(shared, clock, 5, peer=address)
        assert shared.address_held(digest(address)) == 0


async def test_a_private_loopback_or_proxy_address_is_never_held() -> None:
    shared, _ = build()
    app = middleware(shared, Inner())
    for peer in ("10.0.0.7", "127.0.0.1", "192.168.1.9", "172.16.4.4", "::1", "fd00::1"):
        scope = scope_of(bearer="sugra_any_key", peer=peer)
        assert (await call(app, scope))[0] == 200
        assert shared_admission(scope).address is None


async def test_the_proxy_header_names_the_address_only_when_proxy_headers_are_trusted(monkeypatch) -> None:
    shared, _ = build()
    app = middleware(shared, Inner())

    def with_real_ip() -> dict[str, Any]:
        scope = scope_of(bearer="sugra_any_key", peer="10.0.0.7")
        scope["headers"].append((b"x-real-ip", b"9.9.9.9"))
        return scope

    monkeypatch.delenv("SUGRA_MCP_TRUST_PROXY_HEADERS", raising=False)
    off = with_real_ip()
    await call(app, off)
    assert shared_admission(off).address is None
    monkeypatch.setenv("SUGRA_MCP_TRUST_PROXY_HEADERS", "1")
    on = with_real_ip()
    await call(app, on)
    assert shared_admission(on).address == digest("9.9.9.9")
    # Trusted, but the header is malformed: the connection peer is used.
    forged = scope_of(bearer="sugra_any_key", peer="8.8.4.4")
    forged["headers"].append((b"x-real-ip", b"not an address"))
    await call(app, forged)
    assert shared_admission(forged).address == digest("8.8.4.4")


def test_a_full_shield_table_holds_an_address_it_cannot_count_and_keeps_the_ones_it_holds(monkeypatch) -> None:
    monkeypatch.setattr(limits, "SHIELD_ENTRIES", 3)
    shared, clock = build()
    held = [digest(f"9.9.9.{n}") for n in range(3)]
    for address in held:
        shared.shield.record(address)
        assert shared.address_held(address) == 0
    # Three addresses with a live refusal fill the table: a fourth is held, because its next refusal would go uncounted.
    newcomer = digest("9.9.9.99")
    assert 1 <= shared.address_held(newcomer) <= limits.SHIELD_WINDOW_SECONDS
    shared.shield.record(newcomer)
    assert len(shared.shield) == 3
    # Those it holds are counted as ever and barred at the limit.
    for _ in range(limits.SHIELD_FAILURES - 1):
        shared.shield.record(held[0])
    assert 590 <= shared.address_held(held[0]) <= 600
    assert shared.address_held(held[1]) == 0
    assert len(shared.shield) == 3
    # The windows end, and the fourth address is not held and is counted again.
    clock.advance(limits.SHIELD_BAR_SECONDS + 1.0)
    assert shared.address_held(newcomer) == 0
    shared.shield.record(newcomer)
    assert len(shared.shield) == 1


def test_a_full_table_is_swept_at_most_once_a_second(monkeypatch) -> None:
    monkeypatch.setattr(limits, "SHIELD_ENTRIES", 2)
    shared, _ = build()
    for n in range(2):
        shared.shield.record(digest(f"9.9.9.{n}"))
    sweeps = 0
    table = shared.shield
    real = table._end

    def counting(row: Any) -> float:
        nonlocal sweeps
        sweeps += 1
        return real(row)

    monkeypatch.setattr(table, "_end", counting, raising=False)
    for _ in range(50):
        assert shared.address_held(digest("9.9.9.99")) > 0
    # Two rows, one pass: fifty refusals of a full table cost one sweep.
    assert sweeps == 2


# ---- The key-check limit ----


def test_twenty_key_checks_a_second_over_that_the_call_is_busy() -> None:
    shared, clock = build()
    assert [shared.key_check_over() for _ in range(22)] == [None] * 20 + [limits.KEY_CHECKS_PER_SECOND] * 2
    clock.advance(1.5)
    assert shared.key_check_over() is None


def test_an_unconfirmed_key_over_the_check_limit_gets_server_busy_and_a_confirmed_one_does_not() -> None:
    shared, _ = build()
    for _ in range(limits.KEY_CHECKS_PER_SECOND):
        assert shared.key_check_over() is None
    newcomer = key_admission(shared, "sugra_new_key", recognised=False)
    charge = newcomer.before_call("sugra_new_key")
    refusal = charge.refusal
    assert refusal is not None
    assert charge.held is None
    assert (refusal["error"], refusal["scope"], refusal["retry_after"]) == (
        "server_busy",
        "key_checks",
        1,
    )
    assert refusal["limit"] == limits.KEY_CHECKS_PER_SECOND
    known = key_admission(shared, SAMPLE_CALLER, recognised=True)
    assert known.before_call(SAMPLE_CALLER).refusal is None


def test_a_failure_inside_before_call_lets_the_call_run(monkeypatch) -> None:
    shared, _ = build(per_minute=1)

    def broken(_: str) -> Any:
        raise RuntimeError("boom")

    monkeypatch.setattr(shared, "reserve_key", broken)
    assert key_admission(shared, SAMPLE_CALLER, recognised=True).before_call(SAMPLE_CALLER).refusal is None


# ---- The per-key counters: reserved before the call ----


def test_per_key_limits_are_unset_by_default_and_nothing_is_counted() -> None:
    shared, _ = build()
    admission = key_admission(shared, SAMPLE_CALLER, recognised=True)
    for _ in range(50):
        assert admission.before_call(SAMPLE_CALLER).refusal is None
    assert len(shared.keys) == 0


def test_a_key_over_its_minute_limit_gets_rate_limited_with_the_seconds_to_wait() -> None:
    shared, clock = build(per_minute=3)
    admission = key_admission(shared, SAMPLE_CALLER, recognised=True)
    assert [admission.before_call(SAMPLE_CALLER).refusal for _ in range(3)] == [None] * 3
    clock.advance(10.0)
    over = admission.before_call(SAMPLE_CALLER).refusal
    assert over is not None
    assert (over["error"], over["scope"], over["limit"], over["retry_after"]) == ("rate_limited", "key_minute", 3, 50)
    clock.advance(50.0)
    assert admission.before_call(SAMPLE_CALLER).refusal is None


def test_a_key_over_its_day_limit_and_the_two_limits_are_independent() -> None:
    shared, clock = build(per_minute=5, per_day=7)
    admission = key_admission(shared, SAMPLE_CALLER, recognised=True)
    first_minute = [admission.before_call(SAMPLE_CALLER).refusal for _ in range(8)]
    assert first_minute[:5] == [None] * 5
    assert {(r["error"], r["scope"], r["limit"]) for r in first_minute[5:]} == {("rate_limited", "key_minute", 5)}
    # A new minute: two more calls fit in the day, and then the day is full.
    clock.advance(61.0)
    second_minute = [admission.before_call(SAMPLE_CALLER).refusal for _ in range(4)]
    assert second_minute[:2] == [None] * 2
    assert {(r["error"], r["scope"], r["limit"]) for r in second_minute[2:]} == {("rate_limited", "key_day", 7)}
    assert all(1 <= r["retry_after"] <= limits.DAY_SECONDS for r in second_minute[2:])
    clock.advance(61.0)
    assert admission.before_call(SAMPLE_CALLER).refusal is not None


def test_the_charges_of_one_key_never_count_against_another() -> None:
    shared, _ = build(per_minute=1)
    first = key_admission(shared, "sugra_first_key", recognised=True)
    second = key_admission(shared, "sugra_second_key", recognised=True)
    assert first.before_call("sugra_first_key").refusal is None
    assert first.before_call("sugra_first_key").refusal is not None
    assert second.before_call("sugra_second_key").refusal is None


def test_a_refused_call_charges_no_window() -> None:
    shared, _ = build(per_minute=3, per_day=1)
    admission = key_admission(shared, SAMPLE_CALLER, recognised=True)
    assert admission.before_call(SAMPLE_CALLER).refusal is None
    for _ in range(5):
        over = admission.before_call(SAMPLE_CALLER).refusal
        assert over is not None and over["scope"] == "key_day"
    # The day limit refused those calls, so the minute window holds the one call and no more.
    assert shared.keys.charges(minute_name(SAMPLE_CALLER)) == 1
    assert shared.keys.charges(day_name(SAMPLE_CALLER)) == 1


@pytest.mark.parametrize("recognised", [True, False], ids=["recognised key", "key not yet recognised"])
@pytest.mark.parametrize("window", ["minute", "day"])
async def test_concurrent_calls_on_one_key_with_a_limit_of_one_let_exactly_one_through(
    window: str, recognised: bool
) -> None:
    shared, _ = build(per_minute=1 if window == "minute" else None, per_day=1 if window == "day" else None)
    admissions = [key_admission(shared, SAMPLE_CALLER, recognised=recognised) for _ in range(12)]
    ran = await asyncio.gather(*[a_call(a, SAMPLE_CALLER) for a in admissions])
    assert sum(ran) == 1
    # And the next one is refused as well, however it comes.
    late = key_admission(shared, SAMPLE_CALLER, recognised=True)
    assert late.before_call(SAMPLE_CALLER).refusal is not None


@pytest.mark.parametrize("recognised", [True, False], ids=["recognised key", "key not yet recognised"])
async def test_concurrent_calls_let_through_exactly_the_limit_of_the_tighter_window(recognised: bool) -> None:
    shared, _ = build(per_minute=5, per_day=3)
    admissions = [key_admission(shared, SAMPLE_CALLER, recognised=recognised) for _ in range(12)]
    ran = await asyncio.gather(*[a_call(a, SAMPLE_CALLER) for a in admissions])
    assert sum(ran) == 3
    assert shared.keys.charges(minute_name(SAMPLE_CALLER)) == 3
    assert shared.keys.charges(day_name(SAMPLE_CALLER)) == 3


async def test_calls_in_flight_hold_their_places_until_they_end() -> None:
    shared, _ = build(per_minute=2)
    first = key_admission(shared, SAMPLE_CALLER, recognised=False)
    second = key_admission(shared, SAMPLE_CALLER, recognised=False)
    third = key_admission(shared, SAMPLE_CALLER, recognised=False)
    assert first.before_call(SAMPLE_CALLER).refusal is None
    assert second.before_call(SAMPLE_CALLER).refusal is None
    # Both are still running: the place of each is already taken.
    assert third.before_call(SAMPLE_CALLER).refusal is not None


def test_last_row_exhaustion_a_key_the_table_cannot_hold_is_refused_before_its_call(monkeypatch) -> None:
    # Room for three rows; one key with a minute and a day limit takes two of them.
    monkeypatch.setattr(limits, "KEY_ENTRIES", 3)
    shared, _ = build(per_minute=5, per_day=5)
    known = key_admission(shared, "sugra_known", recognised=True)
    assert known.before_call("sugra_known").refusal is None
    assert len(shared.keys) == 2
    for recognised in (True, False):
        name = f"sugra_stranger_{recognised}"
        stranger = key_admission(shared, name, recognised=recognised)
        for _ in range(3):
            over = stranger.before_call(name).refusal
            assert over is not None
            assert (over["error"], over["scope"]) == ("rate_limited", "key_minute")
            assert 1 <= over["retry_after"] <= limits.MINUTE_SECONDS
        # Not counted at all: neither window of the stranger took the one free row.
        assert shared.keys.charges(minute_name(name)) == 0
        assert shared.keys.charges(day_name(name)) == 0
    assert len(shared.keys) == 2
    # The key it holds is still counted, to its own limit.
    assert [known.before_call("sugra_known").refusal for _ in range(4)] == [None] * 4
    assert known.before_call("sugra_known").refusal is not None
    assert len(shared.keys) == 2


def test_a_full_key_table_frees_a_row_when_a_window_ends(monkeypatch) -> None:
    monkeypatch.setattr(limits, "KEY_ENTRIES", 2)
    shared, clock = build(per_minute=100)
    for n in range(2):
        assert key_admission(shared, f"sugra_known_{n}", recognised=True).before_call(f"sugra_known_{n}").refusal is None
    newcomer = key_admission(shared, "sugra_new", recognised=True)
    assert newcomer.before_call("sugra_new").refusal is not None
    clock.advance(61.0)
    assert newcomer.before_call("sugra_new").refusal is None
    assert len(shared.keys) == 1


def test_the_key_check_limit_and_the_shield_have_room_of_their_own_while_the_key_table_is_full(monkeypatch) -> None:
    monkeypatch.setattr(limits, "KEY_ENTRIES", 2)
    shared, _ = build(per_minute=100)
    for n in range(2):
        key_admission(shared, f"sugra_known_{n}", recognised=True).before_call(f"sugra_known_{n}")
    assert len(shared.keys) == 2
    assert shared.key_check_over() is None
    for _ in range(limits.SHIELD_FAILURES):
        shared.shield.record(digest("9.9.9.9"))
    assert shared.address_held(digest("9.9.9.9")) > 0


async def test_a_released_charge_frees_exactly_one_unit() -> None:
    shared, _ = build(per_minute=2, per_day=2)
    one = key_admission(shared, SAMPLE_CALLER, recognised=False)
    two = key_admission(shared, SAMPLE_CALLER, recognised=False)
    three = key_admission(shared, SAMPLE_CALLER, recognised=False)
    first = charged(one, SAMPLE_CALLER)
    assert two.before_call(SAMPLE_CALLER).refusal is None
    assert three.before_call(SAMPLE_CALLER).refusal is not None
    # The API did not accept the first call: its charge goes back, one unit of each window.
    one.after_call(first, gate.AUTH_REFUSED, 1)
    assert shared.keys.charges(minute_name(SAMPLE_CALLER)) == 1
    assert shared.keys.charges(day_name(SAMPLE_CALLER)) == 1
    assert three.before_call(SAMPLE_CALLER).refusal is None
    # Exactly one place was freed: the next call is refused again.
    four = key_admission(shared, SAMPLE_CALLER, recognised=False)
    assert four.before_call(SAMPLE_CALLER).refusal is not None


def test_a_charge_is_never_given_back_twice() -> None:
    shared, _ = build(per_minute=3)
    admission = key_admission(shared, SAMPLE_CALLER, recognised=False)
    other = key_admission(shared, SAMPLE_CALLER, recognised=False)
    assert other.before_call(SAMPLE_CALLER).refusal is None
    held = charged(admission, SAMPLE_CALLER)
    admission.after_call(held, gate.FAILED, 0)
    admission.after_call(held, gate.FAILED, 0)
    # The second settling of the same handle gave nothing back.
    assert shared.keys.charges(minute_name(SAMPLE_CALLER)) == 1


def test_a_charge_of_an_ended_window_frees_nothing_in_the_next_one() -> None:
    clock = FakeClock()
    table = Counters(10, clock)
    window = ("name", 60.0, 5)
    old_end = table.reserve([window])
    assert not isinstance(old_end, Over)
    clock.advance(61.0)
    assert not isinstance(table.reserve([window]), Over)
    table.release("name", old_end[0])
    assert table.charges("name") == 1


def test_a_counter_row_with_no_charge_left_holds_no_room() -> None:
    clock = FakeClock()
    table = Counters(1, clock)
    ends = table.reserve([("a", 60.0, 5)])
    assert not isinstance(ends, Over)
    assert isinstance(table.reserve([("b", 60.0, 5)]), Over)
    table.release("a", ends[0])
    assert len(table) == 0
    assert not isinstance(table.reserve([("b", 60.0, 5)]), Over)


def test_a_counter_table_never_evicts_a_live_row_and_says_when_one_ends() -> None:
    clock = FakeClock()
    table = Counters(2, clock)
    assert table.reserve([("a", 10.0, 5)]) == (clock() + 10.0,)
    assert table.reserve([("b", 5.0, 5)]) == (clock() + 5.0,)
    over = table.reserve([("c", 10.0, 5)])
    assert isinstance(over, Over) and over.retry_seconds >= 1
    # A name it holds is still counted, and a window is not moved by a later charge.
    assert table.reserve([("a", 10.0, 5)]) == (clock() + 10.0,)
    assert table.charges("a") == 2
    clock.advance(6.0)
    assert table.reserve([("c", 10.0, 5)]) == (clock() + 10.0,)
    assert table.charges("a") == 2
    assert len(table) == 2


def test_a_charge_that_needs_two_rows_takes_both_or_neither() -> None:
    clock = FakeClock()
    table = Counters(3, clock)
    assert not isinstance(table.reserve([("a", 60.0, 5), ("x", 86_400.0, 5)]), Over)
    over = table.reserve([("b", 60.0, 5), ("c", 86_400.0, 5)])
    assert isinstance(over, Over) and over.index == 0
    assert len(table) == 2
    assert table.charges("b") == 0
    assert table.charges("c") == 0
    # One row is still free, and a charge that needs only one takes it.
    assert not isinstance(table.reserve([("b", 60.0, 5)]), Over)
    assert len(table) == 3


# ---- Which calls stay counted ----


def test_a_recognised_key_stays_counted_whatever_the_outcome() -> None:
    shared, _ = build(per_minute=10)
    for outcome, api_requests in ((gate.FAILED, 0), (gate.SUCCEEDED, 0), (gate.SUCCEEDED, 1), (gate.AUTH_REFUSED, 1)):
        admission = key_admission(shared, SAMPLE_CALLER, recognised=True)
        admission.after_call(charged(admission, SAMPLE_CALLER), outcome, api_requests)
    assert shared.keys.charges(minute_name(SAMPLE_CALLER)) == 4


@pytest.mark.parametrize(
    ("outcome", "api_requests"),
    [(gate.FAILED, 0), (gate.AUTH_REFUSED, 1), (gate.SUCCEEDED, 0)],
)
def test_a_key_not_yet_recognised_is_given_back_only_when_the_api_did_not_take_it(
    outcome: str, api_requests: int
) -> None:
    # Given back: no request was sent, or the API answered 401 or 403 for the key.
    shared, _ = build(per_minute=10, per_day=10)
    admission = key_admission(shared, SAMPLE_CALLER, recognised=False)
    held = charged(admission, SAMPLE_CALLER)
    assert shared.keys.charges(minute_name(SAMPLE_CALLER)) == 1
    admission.after_call(held, outcome, api_requests)
    assert shared.keys.charges(minute_name(SAMPLE_CALLER)) == 0
    assert shared.keys.charges(day_name(SAMPLE_CALLER)) == 0
    assert len(shared.keys) == 0
    assert not shared.recognised.is_recognised(digest(SAMPLE_CALLER))


def test_a_key_not_yet_recognised_stays_counted_when_a_sent_request_has_no_answer_seen() -> None:
    # A request went out and no 401 or 403 came back: the API may have accepted the key.
    shared, _ = build(per_minute=10, per_day=10)
    admission = key_admission(shared, SAMPLE_CALLER, recognised=False)
    admission.after_call(charged(admission, SAMPLE_CALLER), gate.FAILED, 1)
    assert shared.keys.charges(minute_name(SAMPLE_CALLER)) == 1
    assert shared.keys.charges(day_name(SAMPLE_CALLER)) == 1


def test_a_key_not_yet_recognised_stays_counted_once_the_api_accepted_its_call() -> None:
    shared, _ = build(per_minute=10)
    admission = key_admission(shared, SAMPLE_CALLER, recognised=False)
    admission.after_call(charged(admission, SAMPLE_CALLER), gate.SUCCEEDED, 1)
    assert shared.keys.charges(minute_name(SAMPLE_CALLER)) == 1
    assert shared.recognised.is_recognised(digest(SAMPLE_CALLER))


def test_an_oauth_caller_is_counted_by_the_key_it_resolved_to() -> None:
    shared, _ = build(per_minute=2)
    first = Admission(shared, limits.KIND_TOKEN, digest("jwt-1"), False)
    other = Admission(shared, limits.KIND_TOKEN, digest("jwt-2"), False)
    held = charged(first, "sugra_TENANT_7")
    # A second token of the same tenant shares the tenant's key; a failed call stays counted.
    assert other.before_call("sugra_TENANT_7").refusal is None
    first.after_call(held, gate.FAILED, 0)
    assert first.before_call("sugra_TENANT_7").refusal is not None


def test_a_call_with_no_credential_of_its_own_is_counted_by_the_key_it_runs_with() -> None:
    shared, _ = build(per_minute=1)
    admission = Admission(shared, limits.KIND_NONE, None, False)
    assert admission.before_call("sugra_server_key").refusal is None
    assert admission.before_call("sugra_server_key").refusal is not None
    # No key at all means nothing to count.
    assert admission.before_call(None).refusal is None


def test_made_up_keys_leave_nothing_behind() -> None:
    shared, clock = build(per_minute=5, per_day=5)
    for n in range(500):
        admission = key_admission(shared, f"sugra_made_up_{n}", recognised=False)
        admission.after_call(charged(admission, f"sugra_made_up_{n}"), gate.AUTH_REFUSED, 1)
        # Ten a second: inside the key-check limit.
        clock.advance(0.1)
    assert len(shared.keys) == 0
    assert len(shared.recognised) == 0


def test_no_rejected_key_is_cached_and_no_summary_is_kept() -> None:
    shared, clock = build(per_minute=5)
    refused_key = "sugra_refused_key"
    for _ in range(3):
        admission = key_admission(shared, refused_key, recognised=False)
        admission.after_call(charged(admission, refused_key), gate.AUTH_REFUSED, 1)
        clock.advance(0.2)
    assert len(shared.recognised) == 0
    assert len(shared.keys) == 0
    assert set(vars(shared)) == {"settings", "_wall", "budget", "seats", "recognised", "shield", "checks", "keys"}


def test_the_tables_hold_digests_never_a_key_or_an_address() -> None:
    shared, _ = build(per_minute=5, per_day=50)
    key_admission(shared, SAMPLE_CALLER, recognised=True).before_call(SAMPLE_CALLER)
    shared.shield.record(digest(ADDRESS))
    for table in (shared.keys, shared.shield, shared.recognised):
        assert SAMPLE_CALLER not in repr(table._rows)
        assert ADDRESS not in repr(table._rows)


def route_calls(monkeypatch, admission: Admission, tool: Any, api_key: str = SAMPLE_CALLER) -> None:
    """Make server.mcp.call_tool run `tool` as the body of the call, on an HTTP request of admission carrying api_key."""
    monkeypatch.setattr(limits, "current_admission", lambda: admission)
    monkeypatch.setattr(server, "_dispatching_http_request", lambda: (True, api_key))
    monkeypatch.setattr(server.mcp, "_admitted_call_tool", tool)


async def test_a_call_that_raises_still_gives_its_charge_back(monkeypatch) -> None:
    shared, _ = build(per_minute=1)
    admission = key_admission(shared, SAMPLE_CALLER, recognised=False)

    async def raising(name: str, arguments: dict[str, Any], dispatch: Any, refusal: Any) -> Any:
        assert shared.keys.charges(minute_name(SAMPLE_CALLER)) == 1
        raise RuntimeError("the call failed")

    route_calls(monkeypatch, admission, raising)
    with pytest.raises(RuntimeError):
        await server.mcp.call_tool("call_endpoint", {})
    assert shared.keys.charges(minute_name(SAMPLE_CALLER)) == 0
    assert len(shared.keys) == 0


async def test_a_call_that_raises_after_the_api_took_its_key_stays_counted(monkeypatch) -> None:
    shared, _ = build(per_minute=1)
    admission = key_admission(shared, SAMPLE_CALLER, recognised=False)

    async def raising(name: str, arguments: dict[str, Any], dispatch: Any, refusal: Any) -> Any:
        dispatch.api_requests = 1  # the API request went out and was answered
        raise RuntimeError("the call failed locally")

    route_calls(monkeypatch, admission, raising)
    with pytest.raises(RuntimeError):
        await server.mcp.call_tool("call_endpoint", {})
    assert shared.keys.charges(minute_name(SAMPLE_CALLER)) == 1
    assert shared.keys.charges(day_name(SAMPLE_CALLER)) == 0  # no day limit set: no day row
    # The same key, repeated, meets its limit.
    assert admission.before_call(SAMPLE_CALLER).refusal is not None


async def test_a_call_cancelled_after_the_api_took_its_key_stays_counted(monkeypatch) -> None:
    shared, _ = build(per_minute=1)
    admission = key_admission(shared, SAMPLE_CALLER, recognised=False)
    answered = asyncio.Event()

    async def after_the_answer(name: str, arguments: dict[str, Any], dispatch: Any, refusal: Any) -> Any:
        dispatch.api_requests = 1
        answered.set()
        await asyncio.sleep(30)  # the result is still being shaped when the call is cancelled

    route_calls(monkeypatch, admission, after_the_answer)
    task = asyncio.create_task(server.mcp.call_tool("call_endpoint", {}))
    await answered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert shared.keys.charges(minute_name(SAMPLE_CALLER)) == 1
    assert admission.before_call(SAMPLE_CALLER).refusal is not None


async def test_a_call_cancelled_while_its_api_request_is_in_flight_stays_counted(monkeypatch) -> None:
    shared, _ = build(per_minute=1)
    admission = key_admission(shared, SAMPLE_CALLER, recognised=False)
    in_flight = asyncio.Event()

    async def sending(name: str, arguments: dict[str, Any], dispatch: Any, refusal: Any) -> Any:
        # The client counts the request as it hands the client out, before it sends.
        observability.note_api_request()
        in_flight.set()
        await asyncio.sleep(30)

    route_calls(monkeypatch, admission, sending)
    task = asyncio.create_task(server.mcp.call_tool("call_endpoint", {}))
    await in_flight.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert shared.keys.charges(minute_name(SAMPLE_CALLER)) == 1


async def test_a_cancelled_call_still_gives_its_charge_back(monkeypatch) -> None:
    shared, _ = build(per_minute=1)
    admission = key_admission(shared, SAMPLE_CALLER, recognised=False)
    running = asyncio.Event()

    async def waiting(name: str, arguments: dict[str, Any], dispatch: Any, refusal: Any) -> Any:
        running.set()
        await asyncio.sleep(30)

    route_calls(monkeypatch, admission, waiting)
    task = asyncio.create_task(server.mcp.call_tool("call_endpoint", {}))
    await running.wait()
    assert shared.keys.charges(minute_name(SAMPLE_CALLER)) == 1
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert shared.keys.charges(minute_name(SAMPLE_CALLER)) == 0


# ---- Each call settles its own charge ----


def test_a_refused_second_call_on_one_request_does_not_give_back_the_first_ones_charge() -> None:
    shared, _ = build(per_minute=1)
    admission = key_admission(shared, SAMPLE_CALLER, recognised=False)
    first = charged(admission, SAMPLE_CALLER)
    second = admission.before_call(SAMPLE_CALLER)
    assert second.refusal is not None and second.held is None
    # The refused call ends, with no request sent: it has nothing of its own to settle.
    admission.after_call(second.held, gate.FAILED, 0)
    assert shared.keys.charges(minute_name(SAMPLE_CALLER)) == 1
    # The first call is still in flight and still holds its place.
    assert admission.before_call(SAMPLE_CALLER).refusal is not None
    admission.after_call(first, gate.FAILED, 0)
    assert shared.keys.charges(minute_name(SAMPLE_CALLER)) == 0


def test_calls_of_one_request_finishing_in_reverse_order_settle_their_own_windows() -> None:
    shared, clock = build(per_minute=5, per_day=5)
    admission = key_admission(shared, SAMPLE_CALLER, recognised=False)
    first = charged(admission, SAMPLE_CALLER)
    clock.advance(70.0)  # the minute window of the first call has ended, the day window has not
    second = charged(admission, SAMPLE_CALLER)
    assert first is not None and second is not None and first.windows != second.windows
    assert shared.keys.charges(minute_name(SAMPLE_CALLER)) == 1
    assert shared.keys.charges(day_name(SAMPLE_CALLER)) == 2
    # The second call ends first, with no request sent: its own minute and day units go back.
    admission.after_call(second, gate.FAILED, 0)
    assert shared.keys.charges(minute_name(SAMPLE_CALLER)) == 0
    assert shared.keys.charges(day_name(SAMPLE_CALLER)) == 1
    # The first call ends later and frees its own day unit; its ended minute window frees nothing.
    admission.after_call(first, gate.FAILED, 0)
    assert shared.keys.charges(minute_name(SAMPLE_CALLER)) == 0
    assert shared.keys.charges(day_name(SAMPLE_CALLER)) == 0


def test_a_call_that_ends_with_a_request_sent_keeps_its_own_charge_whichever_call_ends_first() -> None:
    shared, _ = build(per_minute=5)
    admission = key_admission(shared, SAMPLE_CALLER, recognised=False)
    first = charged(admission, SAMPLE_CALLER)
    second = charged(admission, SAMPLE_CALLER)
    admission.after_call(second, gate.FAILED, 0)  # no request sent: released
    admission.after_call(first, gate.FAILED, 1)  # a request went out, no answer seen: kept
    assert shared.keys.charges(minute_name(SAMPLE_CALLER)) == 1


async def test_cancelling_one_of_two_concurrent_calls_settles_only_that_calls_charge(monkeypatch) -> None:
    shared, clock = build(per_minute=5)
    admission = key_admission(shared, SAMPLE_CALLER, recognised=False)
    started = [asyncio.Event(), asyncio.Event()]
    begun = 0

    async def waiting(name: str, arguments: dict[str, Any], dispatch: Any, refusal: Any) -> Any:
        nonlocal begun
        started[begun].set()
        begun += 1
        await asyncio.sleep(30)

    route_calls(monkeypatch, admission, waiting)
    first = asyncio.create_task(server.mcp.call_tool("call_endpoint", {}))
    await started[0].wait()
    clock.advance(70.0)  # the second call is charged in a later minute
    second = asyncio.create_task(server.mcp.call_tool("call_endpoint", {}))
    await started[1].wait()
    assert shared.keys.charges(minute_name(SAMPLE_CALLER)) == 1
    second.cancel()
    with pytest.raises(asyncio.CancelledError):
        await second
    # The cancelled call gave back its own charge; the first is still in flight.
    assert shared.keys.charges(minute_name(SAMPLE_CALLER)) == 0
    assert not first.done()
    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first
    assert len(shared.keys) == 0


async def test_a_call_refused_by_the_limits_is_never_run_and_never_settles_another_call(monkeypatch) -> None:
    shared, _ = build(per_minute=1)
    admission = key_admission(shared, SAMPLE_CALLER, recognised=False)
    running = asyncio.Event()
    ran = 0

    async def tool(name: str, arguments: dict[str, Any], dispatch: Any, refusal: Any) -> Any:
        nonlocal ran
        if refusal is not None:
            return refusal
        ran += 1
        running.set()
        await asyncio.sleep(30)

    route_calls(monkeypatch, admission, tool)
    first = asyncio.create_task(server.mcp.call_tool("call_endpoint", {}))
    await running.wait()
    # A second call of the same request meets the limit and ends at once.
    refused = await server.mcp.call_tool("call_endpoint", {})
    assert (refused["error"], refused["scope"]) == ("rate_limited", "key_minute")
    assert ran == 1
    assert shared.keys.charges(minute_name(SAMPLE_CALLER)) == 1
    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first
    assert shared.keys.charges(minute_name(SAMPLE_CALLER)) == 0


# ---- A surge of requests with no valid credential ----


async def test_a_surge_of_made_up_keys_is_held_at_the_address_after_thirty_refusals() -> None:
    shared, clock = build(per_minute=5)
    api_calls = 0

    async def tool_app(scope: Any, receive: Any, send: Any) -> None:
        nonlocal api_calls
        admission = scope["state"][limits.ADMISSION_STATE]
        charge = admission.before_call("sugra_surge")
        if charge.refusal is None:
            api_calls += 1
            admission.after_call(charge.held, gate.AUTH_REFUSED, 1)
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"x"})

    app = middleware(shared, tool_app)
    statuses = []
    for n in range(400):
        statuses.append((await call(app, scope_of(bearer=f"sugra_surge_{n}", peer=ADDRESS)))[0])
        # Ten a second: inside the budget and the key-check limit, so the address hold is what stops it.
        clock.advance(0.1)
    # The address is held after 30 refusals: the other 370 never reached the API.
    assert api_calls == 30
    assert statuses.count(200) == 30
    assert statuses.count(429) == 370
    assert len(shared.keys) == 0


async def test_a_surge_from_many_addresses_hits_the_budget_and_the_key_check_limit() -> None:
    shared, _ = build()
    api_calls = 0

    async def tool_app(scope: Any, receive: Any, send: Any) -> None:
        nonlocal api_calls
        if scope["state"][limits.ADMISSION_STATE].before_call(None).refusal is None:
            api_calls += 1
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"x"})

    app = middleware(shared, tool_app)
    statuses = []
    for n, address in enumerate(peers(300)):
        statuses.append((await call(app, scope_of(bearer=f"sugra_surge_{n}", peer=address)))[0])
    # All in one instant: the burst of 20 gets in, and the rest wait at the budget.
    assert statuses.count(200) == 20
    assert statuses.count(429) == 280
    assert api_calls <= 20


async def test_a_surge_of_bad_jwts_and_of_discovery_is_bounded() -> None:
    shared, _ = build()
    inner = Inner(status=401)
    app = middleware(shared, inner)
    statuses = []
    for n, address in enumerate(peers(100)):
        token = jwt_of(None) if n % 2 else None
        statuses.append((await call(app, scope_of(bearer=token, peer=address)))[0])
    assert statuses.count(429) == 80
    assert inner.entered == 20
    assert len(shared.recognised) == 0


async def test_recognised_traffic_is_served_through_a_surge() -> None:
    shared, _ = build()
    shared.recognised.remember(digest(SAMPLE_CALLER), 300)
    held_inner = Inner(hold=True)
    app = middleware(shared, held_inner)
    surge = await hold_requests(
        app, [scope_of(bearer=f"sugra_surge_{n}", peer=a) for n, a in enumerate(peers(60))]
    )
    served = await hold_requests(app, [scope_of(bearer=SAMPLE_CALLER) for _ in range(24)])
    # Of the 60 made-up keys, 20 pass the budget and 4 take a seat; the 24 recognised calls all got in.
    assert held_inner.entered == 4 + 24
    assert shared.seats.recognised == 24
    held_inner.proceed.set()
    results = [r[0] for r in await asyncio.gather(*surge, *served)]
    assert results[-24:] == [200] * 24
    assert sorted(results[:-24]) == [200] * 4 + [429] * 40 + [503] * 16
