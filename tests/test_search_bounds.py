"""A catalog search can no longer hold the event loop.

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
import time
from pathlib import Path
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


def _until_sync(predicate, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
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
    """Terms are runs of two or more ASCII letters or digits, repeats included; a
    one-character token is not a term."""
    at_bound = " ".join(["ab"] * search.MAX_QUERY_TERMS + ["x"] * 50)
    assert search.query_limit_error(at_bound) is None
    over_bound = " ".join(["ab"] * (search.MAX_QUERY_TERMS + 1))
    payload = search.query_limit_error(over_bound)
    assert payload is not None and payload["terms"] == search.MAX_QUERY_TERMS + 1
    assert "ASCII letters or digits" in payload["hint"]


def test_the_term_bound_counts_ascii_runs_only() -> None:
    """Non-ASCII text yields no terms, so only the character bound can refuse it."""
    cyrillic = " ".join(["москва"] * (search.MAX_QUERY_TERMS + 1))
    assert len(cyrillic) <= search.MAX_QUERY_CHARS
    assert search._tokens(cyrillic) == []
    assert search.query_limit_error(cyrillic) is None
    assert search._tokens("café ticker") == ["caf", "ticker"]


def test_the_readme_describes_the_ascii_term_rule() -> None:
    text = (Path(__file__).resolve().parents[1] / "README.md").read_text(encoding="utf-8")
    assert "two or more ASCII letters or digits" in text




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


# ---- waiting for a slot: woken by the release, oldest first, bounded ---------


def _assert_no_search_left() -> None:
    assert gateway.search_pending() == 0
    assert gateway.search_waiting() == 0
    assert gateway._search_pending_by_caller == {}
    assert gateway._search_waiting_by_caller == {}


async def test_six_searches_from_one_caller_behind_a_slow_first_search_are_all_served(monkeypatch) -> None:
    """One client sends six searches at once and the first one is slow, as the
    first search after a quiet spell is on the hosted server. With the default
    bounds four are admitted and two wait, and all six are served."""
    blocking = _BlockingSearch()
    monkeypatch.setattr(gateway, "search_catalog", blocking)
    monkeypatch.setattr(gateway, "current_caller", _CALLER.get)
    calls = [_as_caller("caller-a", mcp.call_tool("search_endpoints", {"query": "US CPI"})) for _ in range(6)]
    assert await asyncio.to_thread(blocking.started.wait, 5)
    await asyncio.sleep(3.0)
    blocking.release.set()
    payloads = [_structured(result) for result in await asyncio.gather(*calls)]
    assert [payload.get("error") for payload in payloads] == [None] * 6
    assert blocking.calls == 6
    _assert_no_search_left()


async def test_the_slot_a_finished_search_frees_goes_to_the_waiting_search_in_the_release(monkeypatch) -> None:
    """The release that runs on the worker thread when a search ends hands its
    slot to the waiting search before it returns, so the waiting search never
    looks at the bound again. Checked by state, not by timing: the worker is
    still inside that release when the state is read, so the waiting search
    cannot have run and given the slot back yet."""
    blocking = _BlockingSearch()
    monkeypatch.setattr(gateway, "search_catalog", blocking)
    monkeypatch.setattr(gateway, "SEARCH_MAX_PENDING", 1)
    monkeypatch.setattr(gateway, "SEARCH_WAIT_SECONDS", 60.0)
    real_release = gateway._release_search_slot
    seen: list[tuple[int, int, bool]] = []

    def _release(caller: str) -> None:
        real_release(caller)
        seen.append((gateway.search_pending(), gateway.search_waiting(), threading.current_thread() is threading.main_thread()))

    monkeypatch.setattr(gateway, "_release_search_slot", _release)
    holder = asyncio.create_task(gateway._search_off_loop(None, "hold"))
    assert await asyncio.to_thread(blocking.started.wait, 5)
    waiter = asyncio.create_task(gateway._search_off_loop(None, "wait"))
    assert await _until(lambda: gateway.search_waiting() == 1)
    blocking.release.set()
    assert await asyncio.wait_for(asyncio.gather(holder, waiter), 5) == [[], []]
    assert seen[0] == (1, 0, False), seen
    _assert_no_search_left()


async def test_a_release_from_another_thread_wakes_the_waiting_search(monkeypatch) -> None:
    """A slot given back on a thread that is neither the event loop nor the
    search worker is handed over in the release and wakes the search waiting on
    the loop. The loop has no timer due for 5 s, so only that wake can rouse it."""
    monkeypatch.setattr(gateway, "SEARCH_MAX_PENDING", 1)
    monkeypatch.setattr(gateway, "SEARCH_WAIT_SECONDS", 60.0)
    monkeypatch.setattr(gateway, "current_caller", lambda: "caller-a")
    checked = threading.Event()
    seen: list[tuple[int, int]] = []

    def _search(*args: Any, **kwargs: Any) -> list[dict[str, Any]]:
        checked.wait(5)
        return []

    def _release() -> None:
        # Lets the loop go idle first; nothing below depends on how long this takes.
        time.sleep(0.05)
        gateway._release_search_slot("caller-a")
        seen.append((gateway.search_pending("caller-a"), gateway.search_waiting()))
        checked.set()

    monkeypatch.setattr(gateway, "search_catalog", _search)
    assert gateway._claim_search_slot("caller-a") is None
    waiter = asyncio.create_task(gateway._search_off_loop(None, "US CPI"))
    assert await _until(lambda: gateway.search_waiting() == 1)
    thread = threading.Thread(target=_release)
    thread.start()
    try:
        assert await asyncio.wait_for(waiter, 5) == []
    finally:
        checked.set()
        thread.join(5)
    assert seen == [(1, 0)]
    _assert_no_search_left()


async def test_a_release_never_hands_a_slot_to_a_search_whose_wait_has_ended(monkeypatch) -> None:
    """The wait of a search can end while its task has not run again yet. A
    release in that gap passes it over, so the slot stays free and the search
    answers server_busy instead of running after its wait. The deadline is put
    in the past by hand to make the gap without depending on timing."""
    monkeypatch.setattr(gateway, "SEARCH_MAX_PENDING", 1)
    monkeypatch.setattr(gateway, "SEARCH_WAIT_SECONDS", 60.0)
    ran: list[str] = []
    assert gateway._claim_search_slot("caller-a") is None
    started = time.monotonic()
    waiter = gateway._admit_search("caller-a", started, lambda: ran.append("waiter"))
    assert isinstance(waiter, gateway._SearchWaiter)
    waiter.deadline = time.monotonic() - 0.001
    gateway._release_search_slot("caller-a")
    assert gateway.search_pending() == 0, "the release handed its slot to a search whose wait had ended"
    assert gateway.search_waiting() == 0, "a search whose wait had ended kept its place in line"
    refused = await asyncio.wait_for(gateway._wait_for_slot(waiter, started), 5)
    assert (refused["error"], refused["scope"], refused["limit"]) == ("server_busy", "search", 1)
    assert ran == []
    _assert_no_search_left()


def _park_on_own_loop(loop: asyncio.AbstractEventLoop, call: Any) -> tuple[threading.Thread, list[asyncio.Task]]:
    """Start a thread running loop with one task that joins the line as caller-a
    and waits there. The task is kept, so the collector cannot close it first."""
    parked = threading.Event()
    kept: list[asyncio.Task] = []

    async def _wait() -> None:
        started = time.monotonic()
        waiter = gateway._admit_search("caller-a", started, call)
        assert isinstance(waiter, gateway._SearchWaiter)
        parked.set()
        await gateway._wait_for_slot(waiter, started)

    def _run() -> None:
        kept.append(loop.create_task(_wait()))
        loop.run_forever()
        loop.close()

    thread = threading.Thread(target=_run)
    thread.start()
    assert parked.wait(5)
    return thread, kept


def _close_parked_task(kept: list[asyncio.Task]) -> None:
    """Close the parked task's coroutine, as the collector would; it must give nothing back twice."""
    kept[0].get_coro().close()
    kept[0]._log_destroy_pending = False
    _assert_no_search_left()


def test_a_slot_handed_to_a_search_whose_event_loop_closed_comes_back(monkeypatch) -> None:
    """The release hands its slot over and schedules the wake, then the waiting
    search's loop stops and closes before the wake runs. The release has already
    submitted that search to the worker, so the slot comes back when it ends,
    with no later release or admission."""
    monkeypatch.setattr(gateway, "SEARCH_MAX_PENDING", 1)
    monkeypatch.setattr(gateway, "SEARCH_WAIT_SECONDS", 60.0)
    loop = asyncio.new_event_loop()
    holding, go, finish = threading.Event(), threading.Event(), threading.Event()
    ran: list[str] = []

    def _search() -> list[Any]:
        ran.append(threading.current_thread().name)
        finish.wait(5)
        return []

    def _hold_then_stop() -> None:
        # The wake scheduled while this runs is left for the next loop pass,
        # and stop() ends the loop before that pass.
        holding.set()
        go.wait(5)
        loop.stop()

    assert gateway._claim_search_slot("caller-a") is None
    thread, kept = _park_on_own_loop(loop, _search)
    try:
        loop.call_soon_threadsafe(_hold_then_stop)
        assert holding.wait(5)
        gateway._release_search_slot("caller-a")
        assert (gateway.search_pending("caller-a"), gateway.search_waiting()) == (1, 0)
    finally:
        go.set()
        thread.join(5)
    try:
        assert loop.is_closed()
        assert not kept[0].done(), "the waiting search ran after all"
    finally:
        finish.set()
    assert _until_sync(lambda: gateway.search_pending() == 0), "the slot handed to the closed loop's search was lost"
    assert len(ran) == 1 and ran[0].startswith("catalog-search")
    _assert_no_search_left()
    _close_parked_task(kept)


def test_a_search_whose_event_loop_closed_while_it_waited_leaves_the_line(monkeypatch) -> None:
    """A search still waiting in line when its event loop closes can never
    answer. The next walk of the line takes it out, so it holds no place a new
    search needs and no release hands it a slot."""
    monkeypatch.setattr(gateway, "SEARCH_MAX_PENDING", 1)
    monkeypatch.setattr(gateway, "SEARCH_MAX_WAITING", 1)
    monkeypatch.setattr(gateway, "SEARCH_WAIT_SECONDS", 60.0)
    loop = asyncio.new_event_loop()
    ran: list[str] = []

    async def _admit_another() -> Any:
        return gateway._admit_search("caller-b", time.monotonic(), lambda: ran.append("caller-b"))

    assert gateway._claim_search_slot("caller-a") is None
    thread, kept = _park_on_own_loop(loop, lambda: ran.append("caller-a"))
    loop.call_soon_threadsafe(loop.stop)
    thread.join(5)
    assert loop.is_closed()
    assert gateway.search_waiting("caller-a") == 1
    newcomer = asyncio.run(_admit_another())
    assert isinstance(newcomer, gateway._SearchWaiter), "a search whose loop had closed still held the only place in line"
    assert (gateway.search_waiting("caller-a"), gateway.search_waiting("caller-b")) == (0, 1)
    gateway._leave_waiting(newcomer)
    gateway._release_search_slot("caller-a")
    assert ran == []
    _assert_no_search_left()
    _close_parked_task(kept)


class _LoopClosingAtTheWake:
    """An event loop that is still open when the release looks at it and closed
    by the time the release sends the wake."""

    def __init__(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop

    def create_future(self) -> asyncio.Future[Any]:
        return self._loop.create_future()

    def is_closed(self) -> bool:
        return False

    def call_soon_threadsafe(self, *args: Any) -> Any:
        raise RuntimeError("Event loop is closed")


async def test_a_search_handed_a_slot_as_its_event_loop_closes_is_cancelled(monkeypatch) -> None:
    """When the loop closes between the hand-off and the wake, nobody is left to
    read the answer. The search the release submitted is cancelled: while it is
    still queued it never runs, and its slot comes back at once."""
    monkeypatch.setattr(gateway, "SEARCH_MAX_PENDING", 1)
    ran: list[str] = []
    busy, hold = threading.Event(), threading.Event()

    def _occupy() -> None:
        busy.set()
        hold.wait(5)

    occupying = gateway._search_executor.submit(_occupy)
    try:
        assert await asyncio.to_thread(busy.wait, 5)
        assert gateway._claim_search_slot("caller-a") is None
        loop = _LoopClosingAtTheWake(asyncio.get_running_loop())
        waiter = gateway._SearchWaiter("caller-a", lambda: ran.append("waiter"), loop, time.monotonic() + 60, "search")
        with gateway._search_lock:
            gateway._search_waiters.append(waiter)
            gateway._search_waiting_by_caller["caller-a"] = 1
        gateway._release_search_slot("caller-a")
        assert waiter.future is not None and waiter.future.cancelled()
        assert gateway.search_pending() == 0, "the slot handed to a search nobody can read was kept"
    finally:
        hold.set()
    await asyncio.wrap_future(occupying)
    assert ran == []
    _assert_no_search_left()


@pytest.mark.parametrize(
    ("bounds", "scope", "limit"),
    [
        ({"SEARCH_MAX_PENDING": 1, "SEARCH_MAX_WAITING": 1}, "search", 1),
        ({"SEARCH_MAX_PENDING_PER_CALLER": 1, "SEARCH_MAX_WAITING_PER_CALLER": 1}, "caller_search", 1),
    ],
    ids=["search", "caller_search"],
)
async def test_a_search_past_the_waiting_bound_is_refused_at_once(monkeypatch, bounds, scope, limit) -> None:
    blocking = _BlockingSearch()
    monkeypatch.setattr(gateway, "search_catalog", blocking)
    monkeypatch.setattr(gateway, "current_caller", _CALLER.get)
    monkeypatch.setattr(gateway, "SEARCH_WAIT_SECONDS", 5.0)
    for name, value in bounds.items():
        monkeypatch.setattr(gateway, name, value)
    first = _as_caller("caller-a", gateway._search_off_loop(None, "US CPI"))
    waiting = None
    try:
        assert await asyncio.to_thread(blocking.started.wait, 5)
        waiting = _as_caller("caller-a", gateway._search_off_loop(None, "US CPI"))
        await asyncio.sleep(0.03)
        assert not waiting.done(), "the second search did not wait for a slot"
        begun = time.monotonic()
        refused = await asyncio.wait_for(_as_caller("caller-a", gateway._search_off_loop(None, "US CPI")), 2)
        assert time.monotonic() - begun < 0.5, "the search past the waiting bound waited"
        assert (refused["error"], refused["scope"], refused["limit"], refused["elapsed_ms"]) == (
            "server_busy", scope, limit, 0)
        assert gateway.search_waiting() == 1
    finally:
        blocking.release.set()
        await first
    assert await waiting == []
    _assert_no_search_left()


async def test_a_waiting_search_cancelled_leaves_the_line(monkeypatch) -> None:
    blocking = _BlockingSearch()
    monkeypatch.setattr(gateway, "search_catalog", blocking)
    monkeypatch.setattr(gateway, "SEARCH_MAX_PENDING", 1)
    monkeypatch.setattr(gateway, "SEARCH_WAIT_SECONDS", 5.0)
    first = asyncio.create_task(gateway._search_off_loop(None, "US CPI"))
    assert await asyncio.to_thread(blocking.started.wait, 5)
    cancelled = asyncio.create_task(gateway._search_off_loop(None, "US CPI"))
    later = asyncio.create_task(gateway._search_off_loop(None, "US CPI"))
    assert await _until(lambda: gateway.search_waiting() == 2)
    cancelled.cancel()
    with pytest.raises(asyncio.CancelledError):
        await cancelled
    assert gateway.search_waiting() == 1
    assert gateway.search_pending() == 1
    blocking.release.set()
    assert await asyncio.wait_for(later, 2) == []
    await first
    assert blocking.calls == 2, "a search cancelled while waiting still ran"
    _assert_no_search_left()


async def test_a_waiting_search_cancelled_after_the_hand_off_gives_its_slot_back(monkeypatch) -> None:
    """The release hands its slot to the waiting search and submits its search
    before that search runs again. Cancelled in between, while its search is
    still queued behind other work, it is cancelled like a queued search: the
    search never runs and the slot comes back at once."""
    monkeypatch.setattr(gateway, "SEARCH_MAX_PENDING", 1)
    monkeypatch.setattr(gateway, "SEARCH_WAIT_SECONDS", 5.0)
    monkeypatch.setattr(gateway, "current_caller", lambda: "caller-a")
    ran: list[str] = []
    monkeypatch.setattr(gateway, "search_catalog", lambda catalog, query, **kwargs: ran.append(query) or [])
    busy, hold = threading.Event(), threading.Event()

    def _occupy() -> None:
        busy.set()
        hold.wait(5)

    # Keeps the worker busy, so the search the release submits stays queued.
    occupying = gateway._search_executor.submit(_occupy)
    try:
        assert await asyncio.to_thread(busy.wait, 5)
        assert gateway._claim_search_slot("caller-a") is None
        waiter = asyncio.create_task(gateway._search_off_loop(None, "cancelled"))
        assert await _until(lambda: gateway.search_waiting() == 1)
        gateway._release_search_slot("caller-a")
        assert (gateway.search_pending(), gateway.search_waiting()) == (1, 0), "the release did not hand its slot to the waiting search"
        waiter.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiter
        assert gateway.search_pending() == 0, "the search cancelled after the hand-off kept its slot"
    finally:
        hold.set()
    await asyncio.wrap_future(occupying)
    assert ran == [], "a search cancelled after the hand-off still ran"
    _assert_no_search_left()
    assert await asyncio.wait_for(gateway._search_off_loop(None, "served"), 2) == []
    assert ran == ["served"]
    _assert_no_search_left()


async def test_a_handed_search_cancelled_while_its_caller_awaits_the_answer_never_runs(monkeypatch) -> None:
    """Woken with a slot, the waiting call awaits the future of the search the
    release submitted. Cancelled there, while that search is still queued behind
    other work, the search is cancelled with it, as a queued search is: it never
    runs and its slot comes back at once."""
    monkeypatch.setattr(gateway, "SEARCH_MAX_PENDING", 1)
    monkeypatch.setattr(gateway, "SEARCH_WAIT_SECONDS", 5.0)
    monkeypatch.setattr(gateway, "current_caller", lambda: "caller-a")
    ran: list[str] = []
    monkeypatch.setattr(gateway, "search_catalog", lambda catalog, query, **kwargs: ran.append(query) or [])
    awaiting: list[Any] = []
    real_wrap_future = asyncio.wrap_future

    def _wrap_future(future: Any, **kwargs: Any) -> Any:
        awaiting.append(future)
        return real_wrap_future(future, **kwargs)

    monkeypatch.setattr(gateway.asyncio, "wrap_future", _wrap_future)
    busy, hold = threading.Event(), threading.Event()

    def _occupy() -> None:
        busy.set()
        hold.wait(5)

    occupying = gateway._search_executor.submit(_occupy)
    try:
        assert await asyncio.to_thread(busy.wait, 5)
        assert gateway._claim_search_slot("caller-a") is None
        waiter = asyncio.create_task(gateway._search_off_loop(None, "cancelled"))
        assert await _until(lambda: gateway.search_waiting() == 1)
        gateway._release_search_slot("caller-a")
        # The waiting call has woken and awaits the handed future once it wrapped it.
        assert await _until(lambda: len(awaiting) == 1)
        assert not awaiting[0].done()
        waiter.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiter
        assert await _until(lambda: gateway.search_pending() == 0, timeout=2.0), "the cancelled search kept its slot"
        assert awaiting[0].cancelled()
    finally:
        hold.set()
    await real_wrap_future(occupying)
    assert ran == [], "a search cancelled while its caller awaited the answer still ran"
    _assert_no_search_left()


async def test_waiting_searches_are_served_oldest_first_past_a_caller_at_its_bound(monkeypatch) -> None:
    """A freed slot goes to the oldest waiting search it can admit. A search
    whose caller is at its own bound stays in line without holding up the
    searches of other callers behind it."""
    order: list[str] = []
    monkeypatch.setattr(gateway, "search_catalog", lambda catalog, query, **kwargs: order.append(query) or [])
    monkeypatch.setattr(gateway, "current_caller", _CALLER.get)
    monkeypatch.setattr(gateway, "SEARCH_MAX_PENDING", 2)
    monkeypatch.setattr(gateway, "SEARCH_MAX_PENDING_PER_CALLER", 1)
    monkeypatch.setattr(gateway, "SEARCH_WAIT_SECONDS", 5.0)
    assert gateway._claim_search_slot("caller-a") is None
    assert gateway._claim_search_slot("caller-c") is None
    tasks = []
    for caller, query in (("caller-a", "a2"), ("caller-b", "b1"), ("caller-d", "d1")):
        tasks.append(_as_caller(caller, gateway._search_off_loop(None, query)))
        assert await _until(lambda: gateway.search_waiting() == len(tasks))
    gateway._release_search_slot("caller-c")
    assert await _until(lambda: order == ["b1", "d1"])
    assert gateway.search_waiting() == 1, "the search of the caller at its bound left the line"
    gateway._release_search_slot("caller-a")
    assert await asyncio.wait_for(asyncio.gather(*tasks), 2) == [[], [], []]
    assert order == ["b1", "d1", "a2"]
    _assert_no_search_left()


# ---- the in-flight caps on tool calls ----------------------------------------


class _GateClient:
    def __init__(self, entered: asyncio.Event, release: asyncio.Event) -> None:
        self.entered = entered
        self.release = release

    async def get(self, path: str, params: dict[str, Any] | None = None, **_kwargs: Any) -> Any:
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
    """Each bound, driven through a real tool call, leaves exactly one
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


def test_an_allowlisted_str_subclass_is_not_attached() -> None:
    """The span gets the interned allowlisted str, not the caller's object."""
    class _Code(str):
        pass

    raw = _Code("query_too_long")
    got = observability._error_code_of({"error": raw})
    assert got == "query_too_long"
    assert type(got) is str
    assert got is not raw
    assert got is observability._KNOWN_ERROR_CODE_OF["query_too_long"]
