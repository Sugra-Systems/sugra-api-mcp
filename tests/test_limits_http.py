"""The request limits on the real HTTP app: the entry point's wiring, tool calls, discovery and demand.

The app is the one the entry point serves (the SDK session manager behind the
authentication layer), driven in process. Time is a clock the test moves, and
the Sugra API a handler at the HTTP transport, so nothing leaves the process.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import logging
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
from starlette.middleware.cors import CORSMiddleware

import sugra_api_mcp.tools  # noqa: F401  (registers the tools on server.mcp)
from sugra_api_mcp import demand, gate, limits, observability, server
from sugra_api_mcp.auth import AuthError, AuthMiddleware, ResolvedAuth
from sugra_api_mcp.limits import AdmissionMiddleware, Limits, digest
from tests.test_limits import ADDRESS, build
from tests.test_request_credentials import (
    HEADERS,
    INITIALIZE,
    _answer_api_requests,
    _authenticator,
    _operation_without_params,
)

GOOD_KEY = "sugra_good_key_0123456789"


# ---- The entry point ----


def _entry_point(monkeypatch) -> dict[str, Any]:
    """Run the entry point's HTTP branch with uvicorn.run captured; return the app and the lifespan wiring."""
    import uvicorn

    from sugra_api_mcp import __main__ as entry

    captured: dict[str, Any] = {}
    real_wrap = gate.wrap_lifespan

    def run(app: Any, **kwargs: Any) -> None:
        captured.update(kwargs, app=app)

    def wrap(*args: Any, **kwargs: Any) -> Any:
        captured["wrap"] = kwargs
        return real_wrap(*args, **kwargs)

    monkeypatch.setattr(observability, "setup_observability", lambda: False)
    monkeypatch.setattr(uvicorn, "run", run)
    monkeypatch.setattr(gate, "wrap_lifespan", wrap)
    monkeypatch.setattr(server.mcp, "_session_manager", None)
    monkeypatch.delenv("SUGRA_AGENT_INTERNAL_TOKEN", raising=False)
    monkeypatch.setenv("SUGRA_APP_URL", "http://127.0.0.1:9")
    entry._run_server(argparse.Namespace(transport="streamable-http", host="127.0.0.1", port=0))
    return captured


@pytest.mark.parametrize("value", [None, "0", "off", "nonsense"])
def test_off_the_http_server_is_wired_as_it_was(monkeypatch, value: str | None) -> None:
    monkeypatch.delenv("SUGRA_MCP_LIMITS", raising=False)
    if value is not None:
        monkeypatch.setenv("SUGRA_MCP_LIMITS", value)
    captured = _entry_point(monkeypatch)
    layers = captured["app"].user_middleware
    assert [layer.cls for layer in layers] == [gate.GateMiddleware, CORSMiddleware, AuthMiddleware]
    assert layers[0].kwargs == {"max_body_bytes": server.mcp.settings.max_request_body_size}
    assert [fn.__name__ for fn in captured["wrap"]["on_exit"]] == ["close_clients", "aclose"]


def test_on_the_admission_stands_between_auth_and_cors_and_the_extra_counts_are_on(monkeypatch) -> None:
    monkeypatch.setenv("SUGRA_MCP_LIMITS", "1")
    monkeypatch.setenv("SUGRA_MCP_KEY_LIMIT_PER_MINUTE", "30")
    captured = _entry_point(monkeypatch)
    layers = captured["app"].user_middleware
    assert [layer.cls for layer in layers] == [
        gate.GateMiddleware,
        CORSMiddleware,
        AdmissionMiddleware,
        AuthMiddleware,
    ]
    assert layers[0].kwargs == {
        "max_body_bytes": server.mcp.settings.max_request_body_size,
        "extended_demand": True,
    }
    shared = layers[2].kwargs["limits"]
    assert isinstance(shared, Limits)
    assert shared.settings.key_limit_per_minute == 30
    # Nothing to close: the limits hold only memory.
    assert [fn.__name__ for fn in captured["wrap"]["on_exit"]] == ["close_clients", "aclose"]


def test_on_with_a_bad_limit_the_start_fails_where_an_operator_sees_it(monkeypatch) -> None:
    monkeypatch.setenv("SUGRA_MCP_LIMITS", "1")
    monkeypatch.setenv("SUGRA_MCP_KEY_LIMIT_PER_DAY", "0")
    with pytest.raises(ValueError, match="SUGRA_MCP_KEY_LIMIT_PER_DAY"):
        _entry_point(monkeypatch)


# ---- The real app ----


class _Collect(logging.Handler):
    def __init__(self) -> None:
        super().__init__(logging.DEBUG)
        self.messages: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.messages.append(record.getMessage())


@pytest.fixture
def written():
    """Every message the demand logger writes during the test."""
    handler = _Collect()
    demand.logger.addHandler(handler)
    try:
        yield handler.messages
    finally:
        demand.logger.removeHandler(handler)


@pytest.fixture
async def upstream(monkeypatch):
    """The Sugra API: refuses a key with 'bad' in it, answers any other. Records each key it was sent."""
    sent: list[str] = []

    async def answer(request: httpx.Request) -> httpx.Response:
        key = request.headers.get("x-api-key", "")
        sent.append(key)
        if "bad" in key:
            return httpx.Response(401, json={"detail": "Invalid API key"})
        return httpx.Response(200, json={"data": [{"ok": 1}], "meta": {}})

    _answer_api_requests(monkeypatch, answer)
    monkeypatch.delenv("SUGRA_API_KEY", raising=False)
    await server.close_clients()
    yield sent
    await server.close_clients()


@contextlib.asynccontextmanager
async def stack(
    monkeypatch,
    shared: Limits | None,
    counter: demand.DemandCounter | None = None,
    *,
    peer: str = ADDRESS,
    revoked: set[str] | None = None,
) -> AsyncIterator[httpx.AsyncClient]:
    """The app as the entry point builds it, with the admission only when shared is given."""
    monkeypatch.setattr(server.mcp, "_session_manager", None)
    monkeypatch.setattr(server.mcp.settings, "transport_security", None)
    app = server.mcp.streamable_http_app()
    authenticator = _authenticator()
    real_resolve = authenticator.resolve

    async def resolve(token: str) -> ResolvedAuth:
        token = token.strip()
        if token.startswith("jwt-"):
            if token in (revoked or set()):
                raise AuthError("token_revoked", status=403)
            return ResolvedAuth(
                api_key=f"sugra_TENANT_{token[4:]}", user_id=7, access_token_id=token, method="oauth"
            )
        return await real_resolve(token)

    monkeypatch.setattr(authenticator, "resolve", resolve)
    app.add_middleware(AuthMiddleware, authenticator=authenticator)
    if shared is not None:
        app.add_middleware(AdmissionMiddleware, limits=shared)
    app.add_middleware(
        gate.GateMiddleware,
        max_body_bytes=server.mcp.settings.max_request_body_size,
        summary=gate.GateSummary(),
        demand_counter=counter if counter is not None else demand.DemandCounter(),
        extended_demand=shared is not None,
    )
    try:
        async with server.mcp.session_manager.run():
            transport = httpx.ASGITransport(app=app, client=(peer, 50_000))
            async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1:8002") as client:
                yield client
    finally:
        await authenticator.aclose()


async def open_session(client: httpx.AsyncClient) -> str:
    response = await client.post("/mcp", json=INITIALIZE, headers=HEADERS)
    assert response.status_code == 200
    session = response.headers["mcp-session-id"]
    await client.post(
        "/mcp",
        json={"jsonrpc": "2.0", "method": "notifications/initialized"},
        headers={**HEADERS, "mcp-session-id": session},
    )
    return session


async def tool_call(client: httpx.AsyncClient, session: str, bearer: str) -> tuple[int, dict[str, Any]]:
    """One call_endpoint call under bearer: the HTTP status and the tool's structured result (or the JSON body)."""
    response = await client.post(
        "/mcp",
        json={
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {"name": "call_endpoint", "arguments": {"operation_id": _operation_without_params()}},
        },
        headers={**HEADERS, "mcp-session-id": session, "authorization": f"Bearer {bearer}"},
    )
    if response.status_code != 200:
        return response.status_code, {"body": response.json(), "retry_after": response.headers.get("retry-after")}
    message = next(json.loads(line[6:]) for line in response.text.splitlines() if line.startswith("data: "))
    result = message["result"]
    return 200, {"is_error": result.get("isError", False), **(result.get("structuredContent") or {})}


async def test_a_confirmed_key_is_served_with_no_extra_check(monkeypatch, upstream) -> None:
    shared, _clock = build()
    async with stack(monkeypatch, shared) as client:
        session = await open_session(client)
        status, first = await tool_call(client, session, GOOD_KEY)
        assert (status, first.get("is_error")) == (200, False)
        # The first call was a check of a new key: one charge against the check limit.
        assert shared.checks.charges("checks") == 1
        assert shared.recognised.is_recognised(digest(GOOD_KEY))
        for _ in range(5):
            assert (await tool_call(client, session, GOOD_KEY))[0] == 200
        assert shared.checks.charges("checks") == 1
        assert upstream == [GOOD_KEY] * 6


async def test_thirty_refused_new_keys_from_one_address_hold_it_and_nothing_else(monkeypatch, upstream) -> None:
    shared, clock = build()
    async with stack(monkeypatch, shared) as client:
        session = await open_session(client)
        assert (await tool_call(client, session, GOOD_KEY))[0] == 200
        for n in range(30):
            status, result = await tool_call(client, session, f"sugra_bad_{n}")
            # The API's refusal reaches the caller as it always did.
            assert (status, result["is_error"], result["status_code"]) == (200, True, 401)
            clock.advance(0.2)
        status, held = await tool_call(client, session, "sugra_bad_next")
        assert status == 429
        assert held["body"]["error"] == "rate_limited"
        assert held["body"]["scope"] == "failed_checks"
        assert 590 <= int(held["retry_after"]) <= 600
        # Nothing reached the API for it, and everything else is still served.
        assert upstream.count("sugra_bad_next") == 0
        assert len(upstream) == 31
        assert (await tool_call(client, session, GOOD_KEY))[0] == 200
        assert (await open_session(client)) is not None


async def test_the_refused_keys_are_kept_nowhere_and_each_try_is_checked_again(monkeypatch, upstream) -> None:
    shared, clock = build(per_minute=5)
    async with stack(monkeypatch, shared) as client:
        session = await open_session(client)
        for _ in range(3):
            status, result = await tool_call(client, session, "sugra_bad_same")
            assert (status, result["status_code"]) == (200, 401)
            clock.advance(0.5)
    assert upstream == ["sugra_bad_same"] * 3
    # Nothing is kept for a refused key: not its recognition, not its charges.
    assert len(shared.recognised) == 0
    assert len(shared.keys) == 0


async def test_per_key_limits_unset_by_default_count_nothing(monkeypatch, upstream) -> None:
    shared, _ = build()
    async with stack(monkeypatch, shared) as client:
        session = await open_session(client)
        await tool_call(client, session, GOOD_KEY)
        for _ in range(20):
            assert (await tool_call(client, session, GOOD_KEY))[1].get("is_error") is False
        assert len(shared.keys) == 0


async def test_a_key_over_a_limit_the_operator_set_gets_rate_limited_without_an_api_call(
    monkeypatch, upstream
) -> None:
    shared, clock = build(per_minute=2)
    async with stack(monkeypatch, shared) as client:
        session = await open_session(client)
        # The first call checks the key and is counted like the next one.
        results = [await tool_call(client, session, GOOD_KEY) for _ in range(2)]
        assert [r[1]["is_error"] for r in results] == [False, False]
        status, over = await tool_call(client, session, GOOD_KEY)
        assert (status, over["is_error"], over["error"], over["scope"], over["limit"]) == (
            200,
            True,
            "rate_limited",
            "key_minute",
            2,
        )
        assert 1 <= over["retry_after"] <= 60
        assert len(upstream) == 2
        clock.advance(61.0)
        assert (await tool_call(client, session, GOOD_KEY))[1]["is_error"] is False
        assert len(upstream) == 3


async def test_calls_arriving_together_on_one_key_with_a_limit_of_one_reach_the_api_once(
    monkeypatch, upstream
) -> None:
    shared, _ = build(per_minute=1)
    async with stack(monkeypatch, shared) as client:
        sessions = [await open_session(client) for _ in range(4)]
        results = await asyncio.gather(*[tool_call(client, session, GOOD_KEY) for session in sessions])
        refused = [r for _, r in results if r.get("error") == "rate_limited"]
        served = [r for _, r in results if r.get("is_error") is False]
        assert (len(served), len(refused)) == (1, 3)
        assert upstream == [GOOD_KEY]


async def test_a_made_up_key_is_not_counted_against_a_limit_and_leaves_no_row(monkeypatch, upstream) -> None:
    shared, clock = build(per_minute=1, per_day=1)
    async with stack(monkeypatch, shared) as client:
        session = await open_session(client)
        for n in range(3):
            status, result = await tool_call(client, session, f"sugra_bad_{n}")
            # The API's refusal reaches the caller as it always did, not a rate limit.
            assert (status, result["is_error"], result["status_code"]) == (200, True, 401)
            clock.advance(0.2)
        assert len(shared.keys) == 0
        assert (await tool_call(client, session, GOOD_KEY))[1]["is_error"] is False


async def test_a_key_at_its_day_limit_gets_no_call_when_the_replica_forgets_it(monkeypatch, upstream) -> None:
    shared, clock = build(per_day=2)
    async with stack(monkeypatch, shared) as client:
        session = await open_session(client)
        assert [(await tool_call(client, session, GOOD_KEY))[1]["is_error"] for _ in range(2)] == [False, False]
        # Five minutes on, the replica no longer recognises the key: its lane is decided again,
        # its quota is not.
        clock.advance(limits.RECOGNISED_KEY_SECONDS + 1.0)
        assert not shared.recognised.is_recognised(digest(GOOD_KEY))
        status, over = await tool_call(client, session, GOOD_KEY)
        assert (status, over["is_error"], over["error"], over["scope"]) == (200, True, "rate_limited", "key_day")
        assert len(upstream) == 2


async def test_a_jwt_the_auth_layer_verified_is_recognised_and_a_revoked_one_is_struck(
    monkeypatch, upstream
) -> None:
    shared, _ = build()
    revoked: set[str] = set()
    async with stack(monkeypatch, shared, revoked=revoked) as client:
        session = await open_session(client)
        assert (await tool_call(client, session, "jwt-1"))[0] == 200
        assert shared.recognised.is_recognised(digest("jwt-1"))
        revoked.add("jwt-1")
        assert (await tool_call(client, session, "jwt-1"))[0] == 403
        assert not shared.recognised.is_recognised(digest("jwt-1"))


async def test_discovery_is_byte_identical_with_the_limits_on_and_off(monkeypatch) -> None:
    async def discover(shared: Limits | None) -> list[tuple[int, bytes, dict[str, str]]]:
        seen = []
        async with stack(monkeypatch, shared) as client:
            first = await client.post("/mcp", json=INITIALIZE, headers=HEADERS)
            session = first.headers["mcp-session-id"]
            headers = {**HEADERS, "mcp-session-id": session}
            await client.post(
                "/mcp", json={"jsonrpc": "2.0", "method": "notifications/initialized"}, headers=headers
            )
            responses = [first]
            for number, method in enumerate(("tools/list", "resources/list", "prompts/list"), start=2):
                responses.append(
                    await client.post(
                        "/mcp", json={"jsonrpc": "2.0", "id": number, "method": method, "params": {}}, headers=headers
                    )
                )
        for response in responses:
            kept = {k: v for k, v in response.headers.items() if k not in ("mcp-session-id", "date")}
            seen.append((response.status_code, response.content, kept))
        return seen

    off = await discover(None)
    shared, _ = build()
    on = await discover(shared)
    assert on == off
    assert all(status == 200 for status, _, _ in off)


# ---- The extra demand counts ----


def _extra_records(written: list[str]) -> dict[tuple[str, str], int]:
    counts: dict[tuple[str, str], int] = {}
    for message in written:
        header, *lines = message.split("\n")
        if not header.startswith("sdemandx1 "):
            continue
        assert header.endswith(f"lines={len(lines)}")
        for line in lines:
            kind, value, count = line.split(" ")
            counts[(kind, value)] = counts.get((kind, value), 0) + int(count)
    return counts


async def _declare(client: httpx.AsyncClient) -> None:
    capabilities = {"extensions": {"io.modelcontextprotocol/ui": {}, "vendor.example/private-name": {}}}
    initialize = {**INITIALIZE, "params": {**INITIALIZE["params"], "capabilities": capabilities}}

    def headers(number: int) -> dict[str, str]:
        # A request id as nginx writes it: the gate tracks the requests that carry one.
        return {**HEADERS, "x-request-id": f"{number:02x}" + "cd" * 15}

    assert (await client.post("/mcp", json=initialize, headers=headers(1))).status_code in (200, 400)
    await client.post("/mcp", json={"jsonrpc": "2.0", "id": 5, "method": "server/discover"}, headers=headers(2))
    for number, method in enumerate(("events/list", "events/poll"), start=3):
        await client.post("/mcp", json={"jsonrpc": "2.0", "id": number, "method": method}, headers=headers(number))
    await client.post("/mcp", json=INITIALIZE, headers=headers(9))


async def test_on_the_extra_counts_are_written_beside_the_demand_record(monkeypatch, written) -> None:
    shared, _ = build()
    counter = demand.DemandCounter()
    async with stack(monkeypatch, shared, counter) as client:
        await _declare(client)
    counter.write()
    assert _extra_records(written) == {
        ("extension", "other"): 1,
        ("extension", "ui"): 1,
        ("discover", "attempt"): 1,
        ("events", "request"): 2,
    }
    # No extension name is ever written, and the usual record is still there.
    assert "private-name" not in "\n".join(written)
    assert any(message.startswith("sdemand1 ") for message in written)


async def test_off_nothing_extra_is_counted_and_the_demand_record_is_the_same(monkeypatch, written) -> None:
    counter = demand.DemandCounter()
    async with stack(monkeypatch, None, counter) as client:
        await _declare(client)
    counter.write()
    assert _extra_records(written) == {}
    off_record = [m for m in written if m.startswith("sdemand1 ")]
    assert len(off_record) == 1

    written.clear()
    shared, _ = build()
    counter_on = demand.DemandCounter()
    async with stack(monkeypatch, shared, counter_on) as client:
        await _declare(client)
    counter_on.write()
    on_record = [m for m in written if m.startswith("sdemand1 ")]
    # The record differs only in the boot-time side field.
    assert [m.split(" ", 2)[2] for m in on_record] == [m.split(" ", 2)[2] for m in off_record]


def test_the_extra_facts_are_read_from_a_batch_and_bounded() -> None:
    batch = [{"method": "server/discover"}, {"method": "events/x"}, "junk", {"method": 5}]
    assert demand.extension_facts(batch) == [("discover", "attempt"), ("events", "request")]
    assert demand.extension_facts({"method": "initialize", "params": {"capabilities": {"extensions": []}}}) == []
    many = {f"name-{n}": {} for n in range(500)}
    facts = demand.extension_facts(
        {"method": "initialize", "params": {"capabilities": {"extensions": many}}}
    )
    assert facts == [("extension", "other")]
    assert demand.extension_facts([{"method": "server/discover"}] * 500) == [("discover", "attempt")] * 16
    assert demand.extension_facts(None) == []
