"""MCP-26.1: a catalog search can no longer hold the event loop.

Before this change a three-word query took about 400 ms on the workstation, and
a 20,000-character query of distinct words held the only event loop for 133 s.
It was reachable with a made-up sugra_ Bearer, because catalog tools make no
upstream call. The search now tokenizes each endpoint once, refuses a query over
the bounds before any scoring, runs on a worker thread behind a bounded queue,
and every tool call counts against process-wide and per-caller in-flight caps.
"""

from __future__ import annotations

import asyncio
import contextvars
import hashlib
import json
import sys
import threading
from typing import Any

import pytest

from sugra_api_mcp import observability, server, tools  # noqa: F401  (registers the tools)
from sugra_api_mcp.catalog import aliases, search
from sugra_api_mcp.catalog.loader import load_catalog
from sugra_api_mcp.server import mcp
from sugra_api_mcp.tools import gateway

pytestmark = pytest.mark.anyio

QUOTE_CALL = {"operation_id": "quotes_symbol_price", "params": {"symbol": "AAPL"}}


@pytest.fixture
def anyio_backend():
    return "asyncio"


def _structured(result: Any) -> dict[str, Any]:
    if isinstance(result, tuple):
        return result[1]
    if getattr(result, "structuredContent", None) is not None:
        return result.structuredContent
    return json.loads(result.content[0].text)


def _vocabulary_query(words: int) -> str:
    vocab = sorted(
        {token for endpoint in load_catalog().endpoints for token in search._tokens(endpoint.summary) if len(token) >= 4}
    )
    return " ".join(vocab[i % len(vocab)] for i in range(words))


async def _until(predicate, timeout: float = 5.0) -> bool:
    for _ in range(int(timeout / 0.01)):
        if predicate():
            return True
        await asyncio.sleep(0.01)
    return predicate()


_CALLER = contextvars.ContextVar("test_caller", default="caller-a")


def _as_caller(name: str, coroutine: Any) -> asyncio.Task:
    """Run coroutine in a task whose current_caller() is name."""
    context = contextvars.copy_context()
    context.run(_CALLER.set, name)
    return asyncio.get_running_loop().create_task(coroutine, context=context)


# ---- bounds -----------------------------------------------------------------


@pytest.mark.parametrize("tool", ["search_endpoints", "fetch_data"])
@pytest.mark.parametrize("shape", ["too_many_terms", "too_many_chars"])
async def test_an_oversized_query_is_refused_before_any_search(monkeypatch, tool, shape) -> None:
    def _must_not_search(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("an oversized query reached search_catalog")

    monkeypatch.setattr(gateway, "search_catalog", _must_not_search)
    if shape == "too_many_terms":
        query = " ".join(f"zq{i}" for i in range(search.MAX_QUERY_TERMS + 1))
        assert len(query) <= search.MAX_QUERY_CHARS
    else:
        query = "x" * (search.MAX_QUERY_CHARS + 1)
    result = await mcp.call_tool(tool, {"query": query})

    payload = _structured(result)
    assert result.isError is True
    assert payload["error"] == "query_too_long"
    assert payload["max_terms"] == search.MAX_QUERY_TERMS
    assert payload["max_chars"] == search.MAX_QUERY_CHARS
    assert payload["elapsed_ms"] == 0


def test_a_huge_query_is_refused_without_tokenizing_it(monkeypatch) -> None:
    real_tokens = search._tokens

    def _tokens(value: str) -> list[str]:
        assert len(value) <= search.MAX_QUERY_CHARS, "an oversized query was tokenized"
        return real_tokens(value)

    monkeypatch.setattr(search, "_tokens", _tokens)
    payload = search.query_limit_error("word " * 1_000_000)
    assert payload is not None and payload["error"] == "query_too_long"
    assert "terms" not in payload


def test_the_term_count_is_the_scoring_token_count() -> None:
    """Terms are runs of two or more letters or digits, repeats included; a
    one-character token is not a term."""
    at_bound = " ".join(["ab"] * search.MAX_QUERY_TERMS + ["x"] * 50)
    assert search.query_limit_error(at_bound) is None
    over_bound = " ".join(["ab"] * (search.MAX_QUERY_TERMS + 1))
    payload = search.query_limit_error(over_bound)
    assert payload is not None and payload["terms"] == search.MAX_QUERY_TERMS + 1


async def test_a_query_at_the_bounds_still_searches() -> None:
    query = _vocabulary_query(search.MAX_QUERY_TERMS)
    assert search.query_limit_error(query) is None
    result = await mcp.call_tool("search_endpoints", {"query": query, "limit": 3})
    payload = _structured(result)
    assert "error" not in payload
    assert payload["total_matched"] >= 1


def test_search_catalog_itself_refuses_an_oversized_query() -> None:
    with pytest.raises(ValueError, match="query_too_long"):
        search.search_catalog(load_catalog(), "x" * (search.MAX_QUERY_CHARS + 1))


def test_the_cli_search_refuses_an_oversized_query(monkeypatch, capsys) -> None:
    from sugra_api_mcp.__main__ import main

    monkeypatch.setattr(sys, "argv", ["sugra-api-mcp", "search", "x" * (search.MAX_QUERY_CHARS + 1)])
    with pytest.raises(SystemExit) as exit_info:
        main()
    assert exit_info.value.code == 2
    assert json.loads(capsys.readouterr().out)["error"] == "query_too_long"


# ---- the cached profile answers exactly what re-tokenizing answered ----------


def test_every_endpoint_profile_equals_its_tokenized_fields() -> None:
    for endpoint in load_catalog().endpoints:
        profile = search._profile(endpoint)
        tag_text = " ".join([*endpoint.tags, endpoint.toolset, endpoint.source_family])
        param_text = " ".join(f"{p.name} {p.description}" for p in endpoint.parameters)
        text = search._endpoint_text(endpoint)
        assert profile.operation_id == set(search._tokens(endpoint.operation_id)), endpoint.operation_id
        assert profile.tags == set(search._tokens(tag_text)), endpoint.operation_id
        assert profile.summary == set(search._tokens(endpoint.summary)), endpoint.operation_id
        assert profile.path == set(search._tokens(endpoint.path)), endpoint.operation_id
        assert profile.params == set(search._tokens(param_text)), endpoint.operation_id
        assert profile.description == set(search._tokens(endpoint.description)), endpoint.operation_id
        assert profile.text == set(search._tokens(text)), endpoint.operation_id
        assert profile.text_normalized == " ".join(search._tokens(text)), endpoint.operation_id


def test_alias_matching_from_profiles_equals_the_old_function_for_every_pair(monkeypatch) -> None:
    """The reference is the pre-change _alias_matches itself, called for every
    endpoint and every alias expansion. _tokens is memoized for the sweep, which
    changes no answer because it is a pure function."""
    real_tokens = search._tokens
    memo: dict[str, list[str]] = {}

    def _memoized(value: str) -> list[str]:
        found = memo.get(value)
        if found is None:
            found = real_tokens(value)
            memo[value] = found
        return found

    expansions = sorted({expansion for values in aliases.ALIASES.values() for expansion in values})
    endpoints = load_catalog().endpoints
    profiles = [search._profile(endpoint) for endpoint in endpoints]
    monkeypatch.setattr(search, "_tokens", _memoized)
    checked = 0
    for endpoint, profile in zip(endpoints, profiles, strict=True):
        text = search._endpoint_text(endpoint)
        for expansion in expansions:
            assert search._alias_matches_profile(profile, expansion) == search._alias_matches(text, expansion), (
                endpoint.operation_id,
                expansion,
            )
            checked += 1
    assert checked == len(endpoints) * len(expansions)


def test_the_profile_cache_is_keyed_by_the_endpoint_object() -> None:
    endpoint = load_catalog().endpoints[0]
    assert search._profile(endpoint) is search._profile(endpoint)
    copy = endpoint.model_copy()
    assert search._profile(copy) is not search._profile(endpoint)


def test_every_field_reaches_its_profile_when_no_other_field_repeats_it() -> None:
    """The bundled catalog cannot prove this on its own: every endpoint's
    source_family words also appear in its tags or toolset today, so a profile
    that dropped source_family would still equal the tokenized fields there."""
    from sugra_api_mcp.catalog.models import Endpoint, EndpointParameter

    endpoint = Endpoint(
        operation_id="zzop_probe",
        method="GET",
        path="/api/v1/zzpath/probe",
        summary="zzsummary words",
        description="zzdescription words",
        tags=["Zztag"],
        toolset="zztoolset",
        source_family="zzfamily",
        parameters=[EndpointParameter(name="zzparam", location="query", description="zzparamdesc")],
    )
    expected = {
        "zzop": "operation_id",
        "zztag": "tag_toolset",
        "zztoolset": "tag_toolset",
        "zzfamily": "tag_toolset",
        "zzsummary": "summary",
        "zzpath": "path",
        "zzparam": "params",
        "zzparamdesc": "params",
        "zzdescription": "description",
    }
    for term, field in expected.items():
        _score_value, why = search._score(
            endpoint, [term], {},
            boost_quotes_symbol=False, boost_markets_toolset=False, boost_symbol_input=False,
            boost_forex=False, boost_crypto=False, boost_us_macro=False,
            central_bank_prefixes=[], query_countries=set(),
        )
        assert f"{field}:{term}" in why, (term, why)


# ---- off the event loop, behind bounded queues -------------------------------


async def test_search_runs_off_the_event_loop(monkeypatch) -> None:
    """Deterministic: the search waits for an event that only the event loop
    sets, after it sees the search start. A search running ON the loop would
    hold the loop until its own wait timed out."""
    started, release = threading.Event(), threading.Event()
    released_in_time: list[bool] = []

    def _search_waiting_for_the_loop(*args: Any, **kwargs: Any) -> list[dict[str, Any]]:
        started.set()
        released_in_time.append(release.wait(5))
        return []

    monkeypatch.setattr(gateway, "search_catalog", _search_waiting_for_the_loop)
    call = asyncio.create_task(mcp.call_tool("search_endpoints", {"query": "US CPI"}))
    assert await asyncio.to_thread(started.wait, 5)
    release.set()
    result = await call
    assert released_in_time == [True]
    assert _structured(result)["results"] == []


class _BlockingSearch:
    """The first call blocks until released; later calls return at once."""

    def __init__(self) -> None:
        self.started = threading.Event()
        self.release = threading.Event()
        self.calls = 0
        self._lock = threading.Lock()

    def __call__(self, *args: Any, **kwargs: Any) -> list[dict[str, Any]]:
        with self._lock:
            self.calls += 1
            first = self.calls == 1
        if first:
            self.started.set()
            self.release.wait(5)
        return []


async def test_a_full_search_queue_answers_server_busy_after_the_wait(monkeypatch) -> None:
    blocking = _BlockingSearch()
    monkeypatch.setattr(gateway, "search_catalog", blocking)
    monkeypatch.setattr(gateway, "SEARCH_MAX_PENDING", 1)
    monkeypatch.setattr(gateway, "SEARCH_WAIT_SECONDS", 0.1)
    first = asyncio.create_task(mcp.call_tool("search_endpoints", {"query": "US CPI"}))
    try:
        assert await asyncio.to_thread(blocking.started.wait, 5)
        assert gateway.search_pending() == 1
        for tool in ("search_endpoints", "fetch_data"):
            refused = await mcp.call_tool(tool, {"query": "US CPI"})
            payload = _structured(refused)
            assert refused.isError is True, tool
            assert payload["error"] == "server_busy", tool
            assert payload["scope"] == "search", tool
            assert payload["limit"] == 1, tool
            assert payload["elapsed_ms"] >= 50, tool
    finally:
        blocking.release.set()
        await first
    assert await _until(lambda: gateway.search_pending() == 0)


async def test_one_caller_cannot_take_every_search_slot(monkeypatch) -> None:
    blocking = _BlockingSearch()
    monkeypatch.setattr(gateway, "search_catalog", blocking)
    monkeypatch.setattr(gateway, "current_caller", _CALLER.get)
    monkeypatch.setattr(gateway, "SEARCH_MAX_PENDING_PER_CALLER", 1)
    monkeypatch.setattr(gateway, "SEARCH_WAIT_SECONDS", 0.1)
    first = _as_caller("caller-a", mcp.call_tool("search_endpoints", {"query": "US CPI"}))
    other = None
    try:
        assert await asyncio.to_thread(blocking.started.wait, 5)
        refused = _structured(await _as_caller("caller-a", mcp.call_tool("search_endpoints", {"query": "US CPI"})))
        assert refused["error"] == "server_busy"
        assert refused["scope"] == "caller_search"
        assert refused["limit"] == 1
        other = _as_caller("caller-b", mcp.call_tool("search_endpoints", {"query": "US CPI"}))
        assert await _until(lambda: gateway.search_pending("caller-b") == 1), "another caller was not admitted"
    finally:
        blocking.release.set()
        await first
    assert _structured(await other)["results"] == []
    assert await _until(lambda: gateway.search_pending() == 0)
    assert gateway._search_pending_by_caller == {}


async def test_a_slot_freed_during_the_wait_serves_the_search(monkeypatch) -> None:
    blocking = _BlockingSearch()
    monkeypatch.setattr(gateway, "search_catalog", blocking)
    monkeypatch.setattr(gateway, "SEARCH_MAX_PENDING", 1)
    monkeypatch.setattr(gateway, "SEARCH_WAIT_SECONDS", 5.0)
    first = asyncio.create_task(mcp.call_tool("search_endpoints", {"query": "US CPI"}))
    assert await asyncio.to_thread(blocking.started.wait, 5)
    second = asyncio.create_task(mcp.call_tool("search_endpoints", {"query": "US CPI"}))
    await asyncio.sleep(0.1)
    assert not second.done(), "the second search did not wait for a slot"
    blocking.release.set()
    await first
    payload = _structured(await second)
    assert payload.get("error") is None
    assert payload["results"] == []
    assert blocking.calls == 2


async def test_a_search_cancelled_while_running_holds_its_slot_until_it_ends(monkeypatch) -> None:
    blocking = _BlockingSearch()
    monkeypatch.setattr(gateway, "search_catalog", blocking)
    task = asyncio.create_task(mcp.call_tool("search_endpoints", {"query": "US CPI"}))
    assert await asyncio.to_thread(blocking.started.wait, 5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert gateway.search_pending() == 1, "a cancelled caller released the slot of a search still running"
    blocking.release.set()
    assert await _until(lambda: gateway.search_pending() == 0)


async def test_a_search_cancelled_while_queued_never_runs_and_frees_its_slot(monkeypatch) -> None:
    blocking = _BlockingSearch()
    monkeypatch.setattr(gateway, "search_catalog", blocking)
    monkeypatch.setattr(gateway, "SEARCH_MAX_PENDING", 2)
    first = asyncio.create_task(mcp.call_tool("search_endpoints", {"query": "US CPI"}))
    assert await asyncio.to_thread(blocking.started.wait, 5)
    queued = asyncio.create_task(mcp.call_tool("search_endpoints", {"query": "US CPI"}))
    assert await _until(lambda: gateway.search_pending() == 2)
    queued.cancel()
    with pytest.raises(asyncio.CancelledError):
        await queued
    assert await _until(lambda: gateway.search_pending() == 1), "a dropped queued search kept its slot"
    blocking.release.set()
    await first
    assert await _until(lambda: gateway.search_pending() == 0)
    assert blocking.calls == 1, "a search cancelled while queued still ran"


async def test_a_search_that_raises_frees_its_slot(monkeypatch) -> None:
    def _broken_search(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("search exploded")

    monkeypatch.setattr(gateway, "search_catalog", _broken_search)
    with pytest.raises(Exception, match="search exploded"):
        await mcp.call_tool("search_endpoints", {"query": "US CPI"})
    failed = _structured(await mcp.call_tool("fetch_data", {"query": "US CPI"}))
    assert failed["error"] == "tool_execution_failed"
    assert await _until(lambda: gateway.search_pending() == 0)
    assert gateway._search_pending_by_caller == {}


# ---- the in-flight caps on tool calls ----------------------------------------


class _GateClient:
    def __init__(self, entered: asyncio.Event, release: asyncio.Event) -> None:
        self.entered = entered
        self.release = release

    async def get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        self.entered.set()
        await self.release.wait()
        return {"data": [{"ok": 1}]}

    async def request(self, method: str, path: str, **kwargs: Any) -> Any:
        self.entered.set()
        await self.release.wait()
        return {"data": [{"ok": 1}]}


async def test_calls_beyond_the_in_flight_cap_are_refused_across_server_instances(monkeypatch) -> None:
    entered, release = asyncio.Event(), asyncio.Event()
    monkeypatch.setattr(gateway, "get_client", lambda: _GateClient(entered, release))
    monkeypatch.setattr(server, "MAX_IN_FLIGHT_TOOL_CALLS", 1)
    other = server.SugraFastMCP("second-instance-probe")
    first = asyncio.create_task(mcp.call_tool("call_endpoint", QUOTE_CALL))
    try:
        await asyncio.wait_for(entered.wait(), 5)
        for instance in (mcp, other):
            refused = await instance.call_tool("list_toolsets", {})
            payload = _structured(refused)
            assert refused.isError is True
            assert payload["error"] == "server_busy"
            assert payload["scope"] == "tool_calls"
            assert payload["limit"] == 1
            assert payload["elapsed_ms"] == 0
    finally:
        release.set()
        await first
    assert server.in_flight_tool_calls() == 0
    assert "error" not in _structured(await mcp.call_tool("list_toolsets", {}))


async def test_one_caller_cannot_take_every_in_flight_slot(monkeypatch) -> None:
    entered, release = asyncio.Event(), asyncio.Event()
    monkeypatch.setattr(gateway, "get_client", lambda: _GateClient(entered, release))
    monkeypatch.setattr(server, "current_caller", _CALLER.get)
    monkeypatch.setattr(server, "MAX_IN_FLIGHT_PER_CALLER", 1)
    first = _as_caller("caller-a", mcp.call_tool("call_endpoint", QUOTE_CALL))
    try:
        await asyncio.wait_for(entered.wait(), 5)
        refused = await _as_caller("caller-a", mcp.call_tool("list_toolsets", {}))
        payload = _structured(refused)
        assert refused.isError is True
        assert payload["scope"] == "caller_tool_calls"
        assert payload["limit"] == 1
        served = await _as_caller("caller-b", mcp.call_tool("list_toolsets", {}))
        assert "error" not in _structured(served), "another caller was refused"
        assert server.in_flight_tool_calls("caller-a") == 1
    finally:
        release.set()
        await first
    assert server.in_flight_tool_calls() == 0
    assert server._in_flight_by_caller == {}


async def test_the_in_flight_count_returns_to_zero_after_every_outcome(monkeypatch) -> None:
    def _raising_catalog() -> Any:
        raise RuntimeError("catalog exploded")

    entered, never = asyncio.Event(), asyncio.Event()
    monkeypatch.setattr(gateway, "get_client", lambda: _GateClient(entered, never))

    monkeypatch.setenv("SUGRA_TOOL_DEADLINE", "0.2")
    timed_out = await mcp.call_tool("call_endpoint", QUOTE_CALL)
    assert _structured(timed_out)["error"] == "deadline_exceeded"
    assert server.in_flight_tool_calls() == 0

    monkeypatch.setenv("SUGRA_TOOL_DEADLINE", "40")
    entered.clear()
    cancelled = asyncio.create_task(mcp.call_tool("call_endpoint", QUOTE_CALL))
    await asyncio.wait_for(entered.wait(), 5)
    assert server.in_flight_tool_calls() == 1
    cancelled.cancel()
    with pytest.raises(asyncio.CancelledError):
        await cancelled
    assert server.in_flight_tool_calls() == 0

    monkeypatch.setattr(gateway, "load_catalog", _raising_catalog)
    with pytest.raises(Exception, match="catalog exploded"):
        await mcp.call_tool("list_toolsets", {})
    assert server.in_flight_tool_calls() == 0
    assert server._in_flight_by_caller == {}


def test_admission_and_release_stay_consistent_across_threads(monkeypatch) -> None:
    monkeypatch.setattr(server, "MAX_IN_FLIGHT_TOOL_CALLS", 10_000)
    monkeypatch.setattr(server, "MAX_IN_FLIGHT_PER_CALLER", 10_000)
    refusals: list[dict[str, Any]] = []

    def _worker(index: int) -> None:
        caller = f"thread-{index % 3}"
        for _ in range(5_000):
            refusal = server._admit_tool_call(caller)
            if refusal is not None:
                refusals.append(refusal)
                continue
            server._release_tool_call(caller)

    threads = [threading.Thread(target=_worker, args=(i,)) for i in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert refusals == []
    assert server.in_flight_tool_calls() == 0
    assert server._in_flight_by_caller == {}


def test_admission_and_release_touch_the_counts_only_under_the_lock(monkeypatch) -> None:
    """The thread test above cannot prove the lock on CPython, where the
    interpreter lock hides most lost updates. This one fails whenever a
    per-caller count is read or written outside the admission lock."""

    class _TrackedLock:
        def __init__(self) -> None:
            self.held = False
            self.acquired = 0

        def __enter__(self) -> _TrackedLock:
            self.held = True
            self.acquired += 1
            return self

        def __exit__(self, *exc: object) -> None:
            self.held = False

    lock = _TrackedLock()

    class _GuardedCounts(dict):
        def get(self, key: Any, default: Any = None) -> Any:
            assert lock.held, "a per-caller count was read outside the lock"
            return super().get(key, default)

        def __setitem__(self, key: Any, value: Any) -> None:
            assert lock.held, "a per-caller count was written outside the lock"
            super().__setitem__(key, value)

        def pop(self, key: Any, default: Any = None) -> Any:
            assert lock.held, "a per-caller count was removed outside the lock"
            return super().pop(key, default)

    monkeypatch.setattr(server, "_in_flight_lock", lock)
    monkeypatch.setattr(server, "_in_flight_by_caller", _GuardedCounts())
    assert server._admit_tool_call("caller-a") is None
    assert server.in_flight_tool_calls("caller-a") == 1
    server._release_tool_call("caller-a")
    assert server.in_flight_tool_calls() == 0
    assert lock.acquired == 4


def test_the_caller_name_follows_the_request_credential_and_never_contains_it(monkeypatch) -> None:
    credential = "sugra_zz_not_a_real_key_0123456789"
    monkeypatch.setattr(server, "_dispatching_http_request", lambda: (True, credential))
    named = server.current_caller()
    assert named == "http:" + hashlib.sha256(credential.encode("utf-8")).hexdigest()[:16]
    assert credential not in named
    monkeypatch.setattr(server, "_dispatching_http_request", lambda: (True, "sugra_zz_other_key_9876543210"))
    assert server.current_caller() != named
    monkeypatch.setattr(server, "_dispatching_http_request", lambda: (True, None))
    assert server.current_caller() == "http:anonymous"
    monkeypatch.setattr(server, "_dispatching_http_request", lambda: (False, None))
    assert server.current_caller() == "local"
    previous = server.http_transport_ctx.set(True)
    try:
        assert server.current_caller() == "http:anonymous"
    finally:
        server.http_transport_ctx.reset(previous)


class _CaptureSpan:
    def __init__(self, name: str) -> None:
        self.name = name
        self.attributes: dict[str, object] = {}
        self.ended = False

    def set_attribute(self, key: str, value: object) -> None:
        # The SDK ignores writes to an ended span, and so does this double: an
        # attribute attached after end() must fail here, not vanish in production.
        if not self.ended:
            self.attributes[key] = value

    def set_status(self, status: object) -> None:
        self.status = status

    def end(self) -> None:
        self.ended = True


class _CaptureTracer:
    def __init__(self) -> None:
        self.spans: list[_CaptureSpan] = []

    def start_span(self, name: str) -> _CaptureSpan:
        span = _CaptureSpan(name)
        self.spans.append(span)
        return span


async def test_a_refused_call_leaves_a_span_for_registered_tools_only(monkeypatch) -> None:
    tracer = _CaptureTracer()
    monkeypatch.setattr(observability, "_TRACER", tracer)
    monkeypatch.setattr(server, "MAX_IN_FLIGHT_TOOL_CALLS", 0)

    refused = await mcp.call_tool("list_toolsets", {})
    assert _structured(refused)["error"] == "server_busy"
    unknown = await mcp.call_tool("no_such_tool_name", {})
    assert _structured(unknown)["error"] == "server_busy"

    assert [span.name for span in tracer.spans] == ["mcp.tool.list_toolsets"]
    assert tracer.spans[0].attributes == {
        "mcp.tool.name": "list_toolsets",
        "mcp.success": False,
        "mcp.error.code": "server_busy",
        "mcp.busy.scope": "tool_calls",
        "mcp.duration_ms": 0,
    }
    assert tracer.spans[0].ended is True


@pytest.mark.parametrize(
    ("tool", "arguments", "module", "bound", "scope"),
    [
        ("list_toolsets", {}, server, "MAX_IN_FLIGHT_TOOL_CALLS", "tool_calls"),
        ("list_toolsets", {}, server, "MAX_IN_FLIGHT_PER_CALLER", "caller_tool_calls"),
        ("search_endpoints", {"query": "US CPI"}, gateway, "SEARCH_MAX_PENDING", "search"),
        ("search_endpoints", {"query": "US CPI"}, gateway, "SEARCH_MAX_PENDING_PER_CALLER", "caller_search"),
        ("fetch_data", {"query": "US CPI"}, gateway, "SEARCH_MAX_PENDING", "search"),
        ("fetch_data", {"query": "US CPI"}, gateway, "SEARCH_MAX_PENDING_PER_CALLER", "caller_search"),
    ],
    ids=["tool_calls", "caller_tool_calls", "search", "caller_search", "fetch_data-search", "fetch_data-caller_search"],
)
async def test_every_bound_names_itself_on_the_refusal_span(monkeypatch, tool, arguments, module, bound, scope) -> None:
    """MCP-26.1.1: each bound, driven through a real tool call, leaves exactly one
    span, and its mcp.busy.scope is the scope the refusal payload carries."""
    tracer = _CaptureTracer()
    monkeypatch.setattr(observability, "_TRACER", tracer)
    monkeypatch.setattr(module, bound, 0)
    monkeypatch.setattr(gateway, "SEARCH_WAIT_SECONDS", 0.05)

    payload = _structured(await mcp.call_tool(tool, arguments))

    assert (payload["error"], payload["scope"], payload["limit"]) == ("server_busy", scope, 0)
    assert [span.name for span in tracer.spans] == [f"mcp.tool.{tool}"]
    assert tracer.spans[0].attributes["mcp.error.code"] == "server_busy"
    assert tracer.spans[0].attributes["mcp.busy.scope"] == scope
    assert tracer.spans[0].ended is True
    assert server.in_flight_tool_calls() == 0
    assert gateway.search_pending() == 0


class _RaisingSpan(_CaptureSpan):
    """An exporter that fails on every write."""

    def set_attribute(self, key: str, value: object) -> None:
        raise RuntimeError("exporter died")

    def set_status(self, status: object) -> None:
        raise RuntimeError("exporter died")


class _RaisingTracer(_CaptureTracer):
    def start_span(self, name: str) -> _CaptureSpan:
        span = _RaisingSpan(name)
        self.spans.append(span)
        return span


@pytest.mark.parametrize(
    ("tool", "arguments", "module", "bound", "scope"),
    [
        ("list_toolsets", {}, server, "MAX_IN_FLIGHT_TOOL_CALLS", "tool_calls"),
        ("search_endpoints", {"query": "US CPI"}, gateway, "SEARCH_MAX_PENDING", "search"),
    ],
    ids=["admission", "search-queue"],
)
async def test_a_failing_exporter_never_hides_a_refusal(monkeypatch, tool, arguments, module, bound, scope) -> None:
    """A telemetry failure on the refusal span must still hand the caller the
    structured server_busy payload, on both refusal paths."""
    tracer = _RaisingTracer()
    monkeypatch.setattr(observability, "_TRACER", tracer)
    monkeypatch.setattr(module, bound, 0)
    monkeypatch.setattr(gateway, "SEARCH_WAIT_SECONDS", 0.05)

    payload = _structured(await mcp.call_tool(tool, arguments))

    assert (payload["error"], payload["scope"]) == ("server_busy", scope)
    assert [span.ended for span in tracer.spans] == [True]


def test_the_new_error_codes_reach_telemetry() -> None:
    assert observability._error_code_of({"error": "query_too_long"}) == "query_too_long"
    assert observability._error_code_of({"error": "server_busy"}) == "server_busy"
