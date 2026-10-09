"""The session teardown race filter: what it drops, what it lets through, and its count.

The first tests reproduce the race against the real SDK transport, in process
and without a network: a session ends while the SDK is still writing a message
into it. The rest build records with the same exception shapes (a real raise,
a real anyio task group, a real transport object in the frame) and hand them
to the filter directly.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import hashlib
import inspect
import json
import logging
import re
import sys
import threading
import time
from collections.abc import AsyncIterator, Callable, Iterator
from types import TracebackType
from typing import Any

import anyio
import httpx
import pytest
from mcp.server import streamable_http as sh
from mcp.server.fastmcp import FastMCP
from mcp.server.streamable_http import StreamableHTTPServerTransport
from mcp.server.transport_security import TransportSecuritySettings
from starlette.requests import Request

from sugra_api_mcp import observability, server, teardown_filter

SDK = teardown_filter.SDK_LOGGER
POST_MSG = "Error handling POST request"
SSE_MSG = "SSE response error"
GET_MSG = "Error in standalone SSE writer"
ALL_MSGS = (POST_MSG, SSE_MSG, GET_MSG)
CLIENT_HEADERS = {
    "Accept": "application/json, text/event-stream",
    "Content-Type": "application/json",
    "User-Agent": "ChatGPT-User/1.0",
    "Origin": "https://chatgpt.com",
}

ExcInfo = tuple[type[BaseException], BaseException, TracebackType]


@pytest.fixture(autouse=True)
def _no_filter_left_behind(monkeypatch) -> Iterator[None]:
    """No filter installed, and a fresh module counter, for every test."""
    teardown_filter.uninstall()
    monkeypatch.setattr(teardown_filter, "counter", teardown_filter.RaceCounter())
    monkeypatch.setattr(teardown_filter, "_probed", False)
    yield
    teardown_filter.uninstall()


class _Capture(logging.Handler):
    def __init__(self) -> None:
        super().__init__(level=logging.DEBUG)
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)


@pytest.fixture
def sdk_records() -> Iterator[list[logging.LogRecord]]:
    """Every record that gets past the SDK transport logger's filters."""
    sdk_logger = logging.getLogger(SDK)
    capture = _Capture()
    level = sdk_logger.level
    sdk_logger.setLevel(logging.ERROR)
    sdk_logger.addHandler(capture)
    yield capture.records
    sdk_logger.removeHandler(capture)
    sdk_logger.setLevel(level)


# ---- The race against the real SDK transport ----


class _HoldAccepted:
    """Holds the 202 answer once it is sent, as a slow socket write holds uvicorn's send."""

    def __init__(self, app: Any) -> None:
        self.app = app
        self.armed = False
        self.sent = asyncio.Event()
        self.release = asyncio.Event()

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        status = None

        async def held_send(message: Any) -> None:
            nonlocal status
            await send(message)
            if message["type"] == "http.response.start":
                status = message["status"]
            elif message["type"] == "http.response.body" and status == 202 and self.armed:
                self.armed = False
                self.sent.set()
                await self.release.wait()

        await self.app(scope, receive, held_send)


@contextlib.asynccontextmanager
async def _served(wrap: Callable[[Any], Any] = lambda app: app) -> AsyncIterator[tuple[httpx.AsyncClient, Any]]:
    mcp = FastMCP(
        "race", transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False)
    )
    app = wrap(mcp.streamable_http_app())
    async with mcp.session_manager.run():
        transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
        async with httpx.AsyncClient(transport=transport, base_url="http://app.sugra.ai") as client:
            yield client, app


async def _open_session(client: httpx.AsyncClient) -> dict[str, str]:
    init = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "clientInfo": {"name": "race", "version": "0"},
        },
    }
    opened = await client.post("/mcp", json=init, headers=CLIENT_HEADERS)
    assert opened.status_code == 200
    headers = {
        **CLIENT_HEADERS,
        "mcp-session-id": opened.headers["mcp-session-id"],
        "mcp-protocol-version": "2025-06-18",
    }
    ready = await client.post(
        "/mcp", json={"jsonrpc": "2.0", "method": "notifications/initialized"}, headers=headers
    )
    assert ready.status_code == 202
    return headers


async def _notification_meets_a_delete() -> tuple[int, int]:
    """A DELETE ends the session while the 202 answer to a notification is still being sent."""
    async with _served(_HoldAccepted) as (client, hold):
        headers = await _open_session(client)
        hold.armed = True
        note = {
            "jsonrpc": "2.0",
            "method": "notifications/progress",
            "params": {"progressToken": 1, "progress": 1},
        }
        post = asyncio.create_task(client.post("/mcp", json=note, headers=headers))
        await asyncio.wait_for(hold.sent.wait(), 10)
        deleted = await client.delete("/mcp", headers=headers)
        hold.release.set()
        answered = await asyncio.wait_for(post, 10)
        await asyncio.sleep(0.05)
    return deleted.status_code, answered.status_code


def _race_records(records: list[logging.LogRecord]) -> list[logging.LogRecord]:
    return [r for r in records if r.getMessage() in ALL_MSGS]


async def test_the_real_race_logs_an_error_without_the_filter(sdk_records) -> None:
    """The reproduction is the production record: bare ClosedResourceError from writer.send."""
    deleted, answered = await _notification_meets_a_delete()
    assert (deleted, answered) == (200, 202)
    [record] = _race_records(sdk_records)
    assert record.getMessage() == POST_MSG
    assert record.levelno == logging.ERROR
    assert record.exc_info is not None
    assert type(record.exc_info[1]) is anyio.ClosedResourceError


async def test_the_real_race_is_dropped_and_counted_by_client_class(sdk_records) -> None:
    counter = teardown_filter.counter
    teardown_filter.install()
    deleted, answered = await _notification_meets_a_delete()
    assert (deleted, answered) == (200, 202)
    assert _race_records(sdk_records) == []
    assert counter._counts == {("post", "closed", "app.sugra.ai", "chatgpt", "openai"): 1}


async def test_the_real_race_on_an_sse_answer_is_dropped(monkeypatch, sdk_records) -> None:
    """The session is ended by a task that runs at the checkpoint inside the SDK's writer.send."""
    counter = teardown_filter.counter
    teardown_filter.install()
    create = sh.StreamableHTTPServerTransport._create_session_message

    def ending_the_session(self: Any, *args: Any) -> Any:
        message = create(self, *args)
        asyncio.get_running_loop().create_task(self.terminate())
        return message

    async with _served() as (client, _app):
        headers = await _open_session(client)
        monkeypatch.setattr(sh.StreamableHTTPServerTransport, "_create_session_message", ending_the_session)
        # httpx's ASGI transport asserts on an answer the server never completed.
        with contextlib.suppress(AssertionError):
            await asyncio.wait_for(
                client.post("/mcp", json={"jsonrpc": "2.0", "id": 7, "method": "ping"}, headers=headers),
                10,
            )
        await asyncio.sleep(0.05)
    assert _race_records(sdk_records) == []
    assert counter._counts == {("sse", "closed", "app.sugra.ai", "chatgpt", "openai"): 1}


# ---- Records with the production shapes ----


def _transport(terminated: bool) -> StreamableHTTPServerTransport:
    transport = StreamableHTTPServerTransport(mcp_session_id="s")
    transport._terminated = terminated
    return transport


def _request() -> Request:
    return Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/mcp",
            "headers": [
                (b"host", b"mcp.sugra.ai"),
                (b"user-agent", b"claude-user"),
                (b"origin", b"https://claude.ai"),
            ],
        }
    )


def _raised_in_handler(exc: BaseException, transport: object = None, request: object = None) -> ExcInfo:
    """exc_info as the SDK's POST handler holds it: self and request are locals of the frame."""

    def _handle_post_request(self: object, request: object) -> ExcInfo:
        try:
            raise exc
        except BaseException:
            info = sys.exc_info()
            assert info[0] is not None and info[1] is not None and info[2] is not None
            return info  # type: ignore[return-value]

    return _handle_post_request(transport, request)


def _raised_in_closure(exc: BaseException, transport: object) -> ExcInfo:
    """exc_info as the SDK's GET writer holds it: self is a free variable of a nested function."""
    self = transport

    def standalone_sse_writer() -> ExcInfo:
        try:
            assert self is transport
            raise exc
        except BaseException:
            info = sys.exc_info()
            assert info[0] is not None and info[1] is not None and info[2] is not None
            return info  # type: ignore[return-value]

    return standalone_sse_writer()


async def _group(*leaves: BaseException) -> BaseExceptionGroup[BaseException]:
    """The group a real anyio task group raises when its tasks fail with these."""

    async def fail(error: BaseException) -> None:
        raise error

    try:
        async with anyio.create_task_group() as tg:
            for leaf in leaves:
                tg.start_soon(fail, leaf)
    except BaseExceptionGroup as group:
        return group
    raise AssertionError("the task group did not fail")


def _record(msg: str, exc_info: ExcInfo | None, *, args: tuple[Any, ...] = (), level: int = logging.ERROR) -> logging.LogRecord:
    return logging.getLogger(SDK).makeRecord(SDK, level, "streamable_http.py", 1, msg, args, exc_info)


def _passes(record: logging.LogRecord) -> bool:
    """Whether a filter lets the record through; a drop is counted in the module's (fresh) counter."""
    return teardown_filter.TeardownRaceFilter().filter(record) is True


@pytest.mark.parametrize("msg", ALL_MSGS)
async def test_each_message_drops_a_closed_leaf_bare_or_in_a_group(msg) -> None:
    counter = teardown_filter.counter
    bare = _raised_in_handler(anyio.ClosedResourceError(), _transport(True), _request())
    grouped = _raised_in_handler(await _group(anyio.ClosedResourceError()), _transport(True), _request())
    assert not _passes(_record(msg, bare))
    assert not _passes(_record(msg, grouped))
    site = teardown_filter.RACE_MESSAGES[msg]
    assert counter._counts == {(site, "closed", "mcp.sugra.ai", "claude", "anthropic"): 2}


@pytest.mark.parametrize("msg", ALL_MSGS)
def test_a_closed_leaf_with_no_transport_visible_is_dropped_as_unknown(msg) -> None:
    counter = teardown_filter.counter
    assert not _passes(_record(msg, _raised_in_handler(anyio.ClosedResourceError())))
    site = teardown_filter.RACE_MESSAGES[msg]
    assert counter._counts == {(site, "closed", "unknown", "unknown", "unknown"): 1}


def test_the_get_writer_closure_shape_finds_its_transport() -> None:
    ended = _raised_in_closure(anyio.BrokenResourceError(), _transport(True))
    live = _raised_in_closure(anyio.ClosedResourceError(), _transport(False))
    assert not _passes(_record(GET_MSG, ended))
    assert _passes(_record(GET_MSG, live))


async def test_a_broken_leaf_is_dropped_only_on_an_ended_transport() -> None:
    assert not _passes(_record(POST_MSG, _raised_in_handler(anyio.BrokenResourceError(), _transport(True))))
    assert _passes(_record(POST_MSG, _raised_in_handler(anyio.BrokenResourceError())))
    assert _passes(_record(POST_MSG, _raised_in_handler(anyio.BrokenResourceError(), _transport(False))))
    mixed = await _group(anyio.ClosedResourceError(), anyio.BrokenResourceError())
    assert _passes(_record(SSE_MSG, _raised_in_handler(mixed)))


def test_a_transport_that_was_not_ended_keeps_a_closed_leaf() -> None:
    assert _passes(_record(POST_MSG, _raised_in_handler(anyio.ClosedResourceError(), _transport(False))))


async def test_a_group_with_any_other_leaf_passes() -> None:
    group = await _group(anyio.ClosedResourceError(), ValueError("boom"))
    assert _passes(_record(SSE_MSG, _raised_in_handler(group, _transport(True))))
    nested = BaseExceptionGroup("outer", [await _group(anyio.ClosedResourceError()), RuntimeError()])
    assert _passes(_record(SSE_MSG, _raised_in_handler(nested, _transport(True))))


def test_a_leaf_with_a_cause_or_a_context_passes() -> None:
    def caused() -> None:
        raise anyio.ClosedResourceError() from OSError()

    def in_context() -> None:
        try:
            raise KeyError("x")
        except KeyError:
            raise anyio.ClosedResourceError() from None

    def with_context() -> None:
        try:
            raise KeyError("x")
        except KeyError:
            raise anyio.ClosedResourceError()  # noqa: B904

    for raise_it in (caused, with_context):
        try:
            raise_it()
        except anyio.ClosedResourceError as e:
            exc = e
        assert _passes(_record(POST_MSG, _raised_in_handler(exc, _transport(True))))
    # "from None" still records the context; the filter reads it all the same.
    try:
        in_context()
    except anyio.ClosedResourceError as e:
        assert e.__context__ is not None
        assert _passes(_record(POST_MSG, _raised_in_handler(e, _transport(True))))


async def _group_raised_while_handling(error: BaseException) -> BaseExceptionGroup[BaseException]:
    """A real task group failing while error is being handled: error becomes the group's context."""
    try:
        raise error
    except BaseException:
        return await _group(anyio.ClosedResourceError())


def _group_raised_from(cause: BaseException) -> BaseExceptionGroup[BaseException]:
    try:
        raise BaseExceptionGroup("g", [anyio.ClosedResourceError()]) from cause
    except BaseExceptionGroup as group:
        return group


async def test_the_records_own_group_with_a_cause_or_a_context_passes() -> None:
    with_context = await _group_raised_while_handling(KeyError("real"))
    assert isinstance(with_context.__context__, KeyError) and with_context.__cause__ is None
    with_cause = _group_raised_from(OSError("real"))
    assert isinstance(with_cause.__cause__, OSError)
    for group in (with_context, with_cause):
        assert all(leaf.__cause__ is None and leaf.__context__ is None for leaf in group.exceptions)
        for msg in ALL_MSGS:
            assert _passes(_record(msg, _raised_in_handler(group, _transport(True), _request())))


async def test_a_group_inside_the_tree_with_a_cause_or_a_context_passes() -> None:
    for inner in (await _group_raised_while_handling(KeyError("real")), _group_raised_from(OSError("real"))):
        nested = BaseExceptionGroup("outer", [inner, anyio.ClosedResourceError()])
        assert nested.__cause__ is None and nested.__context__ is None
        assert _passes(_record(SSE_MSG, _raised_in_handler(nested, _transport(True))))


async def test_a_group_whose_context_is_its_own_leaf_is_dropped() -> None:
    """anyio's own shape: the body's error is both a leaf of the group and the group's context."""

    async def body(leaf: BaseException) -> BaseExceptionGroup[BaseException]:
        try:
            async with anyio.create_task_group() as tg:
                tg.start_soon(anyio.sleep, 1)
                raise leaf
        except BaseExceptionGroup as group:
            return group
        raise AssertionError("the task group did not fail")

    group = await body(anyio.ClosedResourceError())
    assert group.__context__ is group.exceptions[0]
    counter = teardown_filter.counter
    assert not _passes(_record(SSE_MSG, _raised_in_handler(group, _transport(True), _request())))
    assert counter._counts == {("sse", "closed", "mcp.sugra.ai", "claude", "anthropic"): 1}
    # The same shape whose leaf was raised while a real error was handled keeps the record.
    try:
        raise KeyError("real")
    except KeyError:
        chained = await body(anyio.ClosedResourceError())
    assert isinstance(chained.exceptions[0].__context__, KeyError)
    assert _passes(_record(SSE_MSG, _raised_in_handler(chained, _transport(True))))


def _raised_below(depth: int, exc: BaseException, transport: object = None) -> ExcInfo:
    """exc raised depth calls below its handler, in a frame whose self is transport.

    The traceback holds depth + 3 frames: the handler, depth + 1 calls of
    down, then bottom.
    """

    def bottom(self: object) -> None:
        raise exc

    def down(n: int) -> None:
        if n == 0:
            bottom(transport)
        else:
            down(n - 1)

    try:
        down(depth)
    except BaseException:
        info = sys.exc_info()
        assert info[0] is not None and info[1] is not None and info[2] is not None
        return info  # type: ignore[return-value]
    raise AssertionError("nothing was raised")


def test_a_live_transport_below_the_frame_cap_keeps_the_record() -> None:
    deep = teardown_filter.MAX_FRAMES + 5
    # Within the cap the frame's transport is found and decides.
    assert not _passes(_record(POST_MSG, _raised_below(10, anyio.ClosedResourceError(), _transport(True))))
    assert _passes(_record(POST_MSG, _raised_below(10, anyio.ClosedResourceError(), _transport(False))))
    # Below the cap it is never read: the record is kept, whatever the transport says.
    assert _passes(_record(POST_MSG, _raised_below(deep, anyio.ClosedResourceError(), _transport(False))))
    assert _passes(_record(POST_MSG, _raised_below(deep, anyio.ClosedResourceError(), _transport(True))))
    assert _passes(_record(POST_MSG, _raised_below(deep, anyio.ClosedResourceError())))


def test_a_walk_that_reads_every_frame_up_to_the_cap_is_not_truncated() -> None:
    cap = teardown_filter.MAX_FRAMES
    counter = teardown_filter.counter
    exactly =_raised_below(cap - 3, anyio.ClosedResourceError())
    assert len(list(teardown_filter._frames(exactly[1]))) == cap
    assert not _passes(_record(POST_MSG, exactly))
    assert counter._counts == {("post", "closed", "unknown", "unknown", "unknown"): 1}
    one_more = _raised_below(cap - 2, anyio.ClosedResourceError())
    assert list(teardown_filter._frames(one_more[1]))[-1] is None
    assert _passes(_record(POST_MSG, one_more))


def _raised_through(exc: BaseException, *steps: object) -> ExcInfo:
    """exc raised below a handler through the given steps, outermost first.

    A step that is an int is that many frames with no self; any other step is
    one frame whose self it is.
    """

    def run(i: int) -> None:
        if i == len(steps):
            raise exc
        step = steps[i]
        if isinstance(step, int):
            filler(step, i)
        else:
            framed(step, i)

    def framed(self: object, i: int) -> None:
        run(i + 1)

    def filler(n: int, i: int) -> None:
        if n <= 1:
            run(i + 1)
        else:
            filler(n - 1, i)

    try:
        run(0)
    except BaseException:
        info = sys.exc_info()
        assert info[0] is not None and info[1] is not None and info[2] is not None
        return info  # type: ignore[return-value]
    raise AssertionError("nothing was raised")


def test_a_live_transport_after_an_ended_one_keeps_the_record() -> None:
    within = _raised_through(anyio.ClosedResourceError(), _transport(True), _transport(False))
    assert len(list(teardown_filter._frames(within[1]))) < teardown_filter.MAX_FRAMES
    assert _passes(_record(POST_MSG, within))
    past = _raised_through(
        anyio.ClosedResourceError(), _transport(True), teardown_filter.MAX_FRAMES + 10, _transport(False)
    )
    assert list(teardown_filter._frames(past[1]))[-1] is None
    assert _passes(_record(POST_MSG, past))


def test_a_truncated_walk_keeps_the_record_even_after_an_ended_transport() -> None:
    past = _raised_through(anyio.ClosedResourceError(), _transport(True), teardown_filter.MAX_FRAMES + 10)
    assert _passes(_record(POST_MSG, past))


def test_every_transport_ended_within_the_cap_drops_the_record() -> None:
    counter = teardown_filter.counter
    closed = _raised_through(anyio.ClosedResourceError(), _transport(True), 5, _transport(True))
    broken = _raised_through(anyio.BrokenResourceError(), _transport(True), _transport(True))
    assert not _passes(_record(POST_MSG, closed))
    assert not _passes(_record(GET_MSG, broken))
    assert counter._counts == {
        ("post", "closed", "unknown", "unknown", "unknown"): 1,
        ("get", "broken", "unknown", "unknown", "unknown"): 1,
    }


def _reachable_labels() -> tuple[set[str], set[str], set[str]]:
    """What the three reducers return for inputs that reach each of their classes."""
    # Every caller host (bare and with a port), every loopback host, other text, and no Host.
    hosts: list[object] = [*observability._CALLER_HOSTS, *(f"{h}:443" for h in observability._CALLER_HOSTS)]
    hosts += [*observability._LOOPBACK_HOSTS, "evil.example", "[::1]x", "", None]
    uas = [
        "sugra-playground/1", "sugra-api-mcp/0.6", "claude-user", "ChatGPT-User/1.0", "grok-agent",
        "cursor", "openbb", "python-httpx/0.27", "curl/8.0", "node-fetch/3", "Mozilla/5.0", "x", "", None,
    ]
    origins = [*observability._ORIGIN_CLASSES, "https://evil.example", "", None]
    host_out = {observability._host_class(v) for v in hosts}
    ua_out = {observability._text_class(v, observability._UA_PATTERNS) for v in uas}
    origin_out = {observability._origin_class(v) for v in origins}
    return (
        {v for v in host_out if v is not None},
        {v for v in ua_out if v is not None},
        {v for v in origin_out if v is not None},
    )


def test_the_label_whitelists_are_what_the_reducers_return() -> None:
    hosts, uas, origins = _reachable_labels()
    assert hosts == teardown_filter._HOST_LABELS
    assert uas == teardown_filter._UA_LABELS
    assert origins == teardown_filter._ORIGIN_LABELS
    # A Host header value is a label only where the reducer returns it as one.
    assert teardown_filter._label("evil.example", teardown_filter._HOST_LABELS) == "other"


def _every_key() -> list[tuple[str, ...]]:
    """Every key the filter can count, longest first."""
    parts = (
        sorted(teardown_filter.RACE_MESSAGES.values()),
        ["closed", "broken"],
        sorted({*teardown_filter._HOST_LABELS, "none", "unknown"}),
        sorted({*teardown_filter._UA_LABELS, "none", "unknown"}),
        sorted({*teardown_filter._ORIGIN_LABELS, "unknown"}),
    )
    keys: list[tuple[str, ...]] = [()]
    for part in parts:
        keys = [(*key, label) for key in keys for label in part]
    return sorted(keys, key=lambda key: (-sum(map(len, key)), key))


def _accounted(message: str) -> tuple[int, int, int, list[str]]:
    """dropped, omitted, the sum of the written counts, and the lines of a count record."""
    header, *lines = message.splitlines()
    match = re.fullmatch(r"srace1 side=(\S+) dropped=(\d+) lines=(\d+) omitted=(\d+) unmatched=(\d+)", header)
    assert match is not None, header
    assert int(match.group(3)) == len(lines)
    return int(match.group(2)), int(match.group(4)), sum(int(line.rsplit(" ", 1)[1]) for line in lines), lines


def test_an_absurd_side_and_huge_counts_stay_within_the_budget(caplog, monkeypatch) -> None:
    caplog.set_level(logging.INFO, logger=teardown_filter.COUNTER_LOGGER)
    monkeypatch.setattr(observability, "process_side", lambda: "x" * 100_000)
    keys = _every_key()
    assert len(keys) > teardown_filter.MAX_LINES
    counter = teardown_filter.RaceCounter()
    big = teardown_filter.COUNT_CAP // (2 * len(keys))
    counter._counts = {key: big + i for i, key in enumerate(keys)}  # type: ignore[misc]
    counter.write()
    [message] = _count_records(caplog)
    assert message.startswith("srace1 side=unknown ")
    assert len(message) <= teardown_filter.MAX_RECORD_CHARS
    dropped, omitted, written, lines = _accounted(message)
    assert dropped == sum(big + i for i in range(len(keys)))
    assert written + omitted == dropped
    assert 0 < len(lines) <= teardown_filter.MAX_LINES


def test_the_character_budget_stops_the_lines_and_counts_the_rest_as_omitted(caplog, monkeypatch) -> None:
    caplog.set_level(logging.INFO, logger=teardown_filter.COUNTER_LOGGER)
    monkeypatch.setattr(teardown_filter, "MAX_LINES", 10**6)
    keys = _every_key()
    counts = {key: 10**7 + i for i, key in enumerate(keys)}
    counter = teardown_filter.RaceCounter()
    counter._counts = dict(counts)  # type: ignore[arg-type]
    counter.write()
    [message] = _count_records(caplog)
    assert len(message) <= teardown_filter.MAX_RECORD_CHARS
    dropped, omitted, written, lines = _accounted(message)
    assert len(lines) < len(keys)
    assert dropped == sum(counts.values()) and written + omitted == dropped
    # It stopped at the budget: the next line, largest count first, would not fit.
    ranked = sorted(counts.items(), key=lambda item: (-item[1], item[0]))
    following = " ".join((*ranked[len(lines)][0], str(ranked[len(lines)][1])))
    assert len(message) + 1 + len(following) > teardown_filter.MAX_RECORD_CHARS


def _widest_header() -> int:
    """The longest header there can be: the longest side, every number at its widest."""
    cap = f"{teardown_filter.COUNT_CAP}+"
    side = "x" * teardown_filter.MAX_SIDE_CHARS
    return len(f"srace1 side={side} dropped={cap} lines={teardown_filter.MAX_LINES} omitted={cap} unmatched={cap}")


def test_the_record_stays_within_every_budget_size(monkeypatch) -> None:
    """From the widest header up, at no budget does the record pass it, whatever the counts."""
    monkeypatch.setattr(observability, "process_side", lambda: "x" * teardown_filter.MAX_SIDE_CHARS)
    keys = _every_key()[:40]
    exact = {key: 10**9 + i for i, key in enumerate(keys)}
    huge = {key: 10**30 + i for i, key in enumerate(keys)}
    floor = _widest_header()
    assert floor < teardown_filter.MAX_RECORD_CHARS
    for budget in range(floor, floor + 1_200):
        monkeypatch.setattr(teardown_filter, "MAX_RECORD_CHARS", budget)
        for counts, unmatched in ((exact, 5), (huge, 10**30), ({}, 10**30)):
            message = teardown_filter._count_text(counts, unmatched)
            assert len(message) <= budget, (budget, len(message))
        dropped, omitted, written, _ = _accounted(teardown_filter._count_text(exact))
        assert written + omitted == dropped == sum(exact.values())


def test_a_count_beyond_the_cap_is_written_as_the_cap_with_a_plus(caplog, monkeypatch) -> None:
    caplog.set_level(logging.INFO, logger=teardown_filter.COUNTER_LOGGER)
    monkeypatch.setattr(observability, "process_side", lambda: "x" * teardown_filter.MAX_SIDE_CHARS)
    cap = teardown_filter.COUNT_CAP
    first = ("post", "closed", "none", "other", "none")
    second = ("get", "closed", "unknown", "unknown", "unknown")
    counter = teardown_filter.RaceCounter()
    # More digits than int-to-text conversion allows by default: a count is never converted whole.
    counter._counts = {first: 10**5000, second: cap}  # type: ignore[misc]
    counter._unmatched = 10**30
    counter.write()
    [message] = _count_records(caplog)
    assert len(message) <= teardown_filter.MAX_RECORD_CHARS
    header, *lines = message.splitlines()
    assert header == (
        f"srace1 side={'x' * 128} dropped={cap}+ lines=2 omitted=0 unmatched={cap}+"
    )
    assert lines == [f"post closed none other none {cap}+", f"get closed unknown unknown unknown {cap}"]
    # A count at the cap is exact; one above it says so.
    assert teardown_filter._number(cap) == str(cap)
    assert teardown_filter._number(cap + 1) == f"{cap}+"


def test_an_omitted_count_beyond_the_cap_is_marked_in_the_header() -> None:
    keys = _every_key()[: teardown_filter.MAX_LINES + 1]
    counts = {key: 10**30 for key in keys}
    header = teardown_filter._count_text(counts).splitlines()[0]
    cap = teardown_filter.COUNT_CAP
    assert f"dropped={cap}+ " in header and f"omitted={cap}+ " in header


def test_a_class_outside_the_known_labels_is_counted_as_other(monkeypatch) -> None:
    leak = "Bearer sk-not-a-class"
    monkeypatch.setattr(observability, "_host_class", lambda value: leak)
    monkeypatch.setattr(observability, "_text_class", lambda value, patterns: leak)
    monkeypatch.setattr(observability, "_origin_class", lambda value: leak)
    counter = teardown_filter.counter
    assert not _passes(_record(POST_MSG, _raised_in_handler(anyio.ClosedResourceError(), _transport(True), _request())))
    assert counter._counts == {("post", "closed", "other", "other", "other"): 1}


def test_a_request_without_user_agent_or_origin_is_counted() -> None:
    bare = Request({"type": "http", "method": "POST", "path": "/mcp", "headers": [(b"host", b"app.sugra.ai")]})
    counter = teardown_filter.counter
    assert not _passes(_record(POST_MSG, _raised_in_handler(anyio.ClosedResourceError(), _transport(True), bare)))
    assert counter._counts == {("post", "closed", "app.sugra.ai", "other", "none"): 1}


def test_a_class_that_is_not_text_is_counted_as_none(caplog, monkeypatch) -> None:
    caplog.set_level(logging.INFO, logger=teardown_filter.COUNTER_LOGGER)
    monkeypatch.setattr(observability, "_host_class", lambda value: None)
    monkeypatch.setattr(observability, "_text_class", lambda value, patterns: None)
    monkeypatch.setattr(observability, "_origin_class", lambda value: None)
    counter = teardown_filter.counter
    assert not _passes(_record(POST_MSG, _raised_in_handler(anyio.ClosedResourceError(), _transport(True), _request())))
    assert not _passes(_record(GET_MSG, _raised_in_handler(anyio.ClosedResourceError())))
    assert counter._counts == {
        ("post", "closed", "none", "none", "none"): 1,
        ("get", "closed", "unknown", "unknown", "unknown"): 1,
    }
    counter.write()
    [message] = _count_records(caplog)
    assert message.splitlines()[1:] == ["get closed unknown unknown unknown 1", "post closed none none none 1"]


def test_args_another_message_another_level_or_no_exception_pass() -> None:
    def info() -> ExcInfo:
        return _raised_in_handler(anyio.ClosedResourceError(), _transport(True))

    assert _passes(_record(POST_MSG, info(), args=("x",)))
    assert _passes(_record("Error in SSE writer", info()))
    assert _passes(_record("Error handling POST request ", info()))
    assert _passes(_record(POST_MSG, info(), level=logging.WARNING))
    assert _passes(_record(POST_MSG, info(), level=logging.CRITICAL))
    assert _passes(_record(POST_MSG, None))
    other = logging.getLogger("mcp.server.streamable_http_manager").makeRecord(
        "mcp.server.streamable_http_manager", logging.ERROR, "x.py", 1, POST_MSG, (), info()
    )
    assert _passes(other)


def test_an_error_inside_the_filter_lets_the_record_through() -> None:
    class Unreadable(StreamableHTTPServerTransport):
        @property
        def is_terminated(self) -> bool:
            raise RuntimeError("unreadable")

    record = _record(POST_MSG, _raised_in_handler(anyio.ClosedResourceError(), Unreadable(mcp_session_id="s")))
    counter = teardown_filter.counter
    assert _passes(record)
    assert counter._counts == {}
    # A record the filter could not decide, shaped like a race, is counted as kept.
    assert counter._unmatched == 1


def test_a_near_miss_is_kept_and_counted_as_unmatched() -> None:
    counter = teardown_filter.counter

    def closed() -> ExcInfo:
        return _raised_in_handler(anyio.ClosedResourceError(), _transport(True))

    near_misses = [
        _record("A changed SDK text", closed()),
        _record(POST_MSG, closed(), args=("x",)),
        _record(POST_MSG, closed(), level=logging.WARNING),
        _record(POST_MSG, _raised_in_handler(anyio.ClosedResourceError(), _transport(False))),
        _record(POST_MSG, _raised_in_handler(anyio.BrokenResourceError())),
    ]
    for record in near_misses:
        assert _passes(record)
    assert counter._unmatched == len(near_misses)
    assert counter._counts == {}
    # A drop is not a near miss, and records unlike a race are not counted at all.
    assert not _passes(_record(POST_MSG, closed()))
    assert _passes(_record(POST_MSG, _raised_in_handler(ValueError("x"), _transport(True))))
    assert _passes(_record(POST_MSG, None))
    other = logging.getLogger("mcp.server.streamable_http_manager").makeRecord(
        "mcp.server.streamable_http_manager", logging.ERROR, "x.py", 1, POST_MSG, (), closed()
    )
    assert _passes(other)
    assert counter._unmatched == len(near_misses)
    assert sum(counter._counts.values()) == 1


def test_the_unmatched_count_is_written_with_no_drop_and_taken_off(caplog) -> None:
    caplog.set_level(logging.INFO, logger=teardown_filter.COUNTER_LOGGER)
    counter = teardown_filter.RaceCounter()
    counter.add_unmatched()
    counter.add_unmatched()
    counter.write()
    counter.write()
    [message] = _count_records(caplog)
    assert re.fullmatch(r"srace1 side=\S+ dropped=0 lines=0 omitted=0 unmatched=2", message)
    assert counter._unmatched == 0


# ---- The count ----


def _count_records(caplog) -> list[str]:
    return [r.getMessage() for r in caplog.records if r.name == teardown_filter.COUNTER_LOGGER]


def test_the_count_is_written_as_a_delta_and_only_when_there_is_one(caplog, monkeypatch) -> None:
    monkeypatch.setenv("CONTAINER_APP_REVISION", "sugra-mcp--py1")
    caplog.set_level(logging.INFO, logger=teardown_filter.COUNTER_LOGGER)
    counter = teardown_filter.RaceCounter()
    counter.add(("post", "closed", "app.sugra.ai", "chatgpt", "openai"))
    counter.add(("post", "closed", "app.sugra.ai", "chatgpt", "openai"))
    counter.add(("get", "broken", "unknown", "unknown", "unknown"))
    counter.write()
    counter.write()
    [message] = _count_records(caplog)
    side = observability.process_side()
    assert message.splitlines() == [
        f"srace1 side={side} dropped=3 lines=2 omitted=0 unmatched=0",
        "post closed app.sugra.ai chatgpt openai 2",
        "get broken unknown unknown unknown 1",
    ]
    [record] = [r for r in caplog.records if r.name == teardown_filter.COUNTER_LOGGER]
    assert record.levelno == logging.INFO


def test_the_count_keeps_the_largest_lines_and_sums_the_rest(caplog, monkeypatch) -> None:
    caplog.set_level(logging.INFO, logger=teardown_filter.COUNTER_LOGGER)
    monkeypatch.setattr(teardown_filter, "MAX_LINES", 1)
    counter = teardown_filter.RaceCounter()
    for _ in range(3):
        counter.add(("post", "closed", "none", "other", "none"))
    counter.add(("sse", "closed", "none", "other", "none"))
    counter.write()
    [message] = _count_records(caplog)
    assert message.splitlines()[0].endswith("dropped=4 lines=1 omitted=1 unmatched=0")
    assert message.splitlines()[1:] == ["post closed none other none 3"]


def test_a_write_that_fails_keeps_the_counts_for_the_next_one(caplog, monkeypatch) -> None:
    caplog.set_level(logging.INFO, logger=teardown_filter.COUNTER_LOGGER)
    counter = teardown_filter.RaceCounter()
    key = ("post", "closed", "none", "other", "none")
    counter.add(key)
    counter.add(key)

    def unreadable() -> str:
        raise RuntimeError("side")

    monkeypatch.setattr(observability, "process_side", unreadable)
    with pytest.raises(RuntimeError):
        counter.write()
    assert counter._counts == {key: 2}
    assert _count_records(caplog) == []
    monkeypatch.undo()
    counter.write()
    [message] = _count_records(caplog)
    assert message.splitlines()[1:] == ["post closed none other none 2"]
    assert counter._counts == {}


class _OnEmit(logging.Handler):
    """A handler on the count logger that runs action for each record it is given."""

    def __init__(self, action: Callable[[], None]) -> None:
        super().__init__(level=logging.DEBUG)
        self.action = action

    def emit(self, record: logging.LogRecord) -> None:
        self.action()


@contextlib.contextmanager
def _count_handler(action: Callable[[], None]) -> Iterator[None]:
    count_logger = logging.getLogger(teardown_filter.COUNTER_LOGGER)
    handler = _OnEmit(action)
    count_logger.addHandler(handler)
    try:
        yield
    finally:
        count_logger.removeHandler(handler)


def test_a_handler_that_raises_keeps_the_counts_for_the_next_write(caplog) -> None:
    caplog.set_level(logging.INFO, logger=teardown_filter.COUNTER_LOGGER)
    counter = teardown_filter.RaceCounter()
    key = ("post", "closed", "none", "other", "none")
    counter.add(key)
    counter.add(key)

    def fail() -> None:
        raise RuntimeError("handler")

    with _count_handler(fail), pytest.raises(RuntimeError):
        counter.write()
    assert counter._counts == {key: 2}
    caplog.clear()
    counter.write()
    [message] = _count_records(caplog)
    assert message.splitlines()[1:] == ["post closed none other none 2"]
    assert counter._counts == {}


def test_drops_counted_while_a_write_is_logged_stay_for_the_next(caplog) -> None:
    caplog.set_level(logging.INFO, logger=teardown_filter.COUNTER_LOGGER)
    counter = teardown_filter.RaceCounter()
    written = ("post", "closed", "none", "other", "none")
    other = ("get", "closed", "unknown", "unknown", "unknown")
    counter.add(written)

    def count_meanwhile() -> None:
        counter.add(written)
        counter.add(other)

    with _count_handler(count_meanwhile):
        counter.write()
    assert counter._counts == {written: 1, other: 1}
    [message] = _count_records(caplog)
    assert message.splitlines()[1:] == ["post closed none other none 1"]


async def test_the_installed_filter_and_the_lifespan_share_the_one_counter(caplog) -> None:
    caplog.set_level(logging.INFO, logger=teardown_filter.COUNTER_LOGGER)
    race_filter = teardown_filter.install()
    assert not hasattr(race_filter, "counter")
    assert "counter" not in inspect.signature(teardown_filter.install).parameters
    assert "counter" not in inspect.signature(teardown_filter.wrap_lifespan).parameters

    @contextlib.asynccontextmanager
    async def inner(app: object) -> AsyncIterator[None]:
        yield

    lifespan = teardown_filter.wrap_lifespan(inner, interval=3600)
    async with lifespan(object()):
        record = _record(POST_MSG, _raised_in_handler(anyio.ClosedResourceError(), _transport(True), _request()))
        assert race_filter.filter(record) is False
        assert teardown_filter.counter._counts == {("post", "closed", "mcp.sugra.ai", "claude", "anthropic"): 1}
    [message] = _count_records(caplog)
    assert message.splitlines()[1:] == ["post closed mcp.sugra.ai claude anthropic 1"]
    assert teardown_filter.counter._counts == {}


async def test_the_lifespan_writes_every_interval_and_once_more_on_exit(monkeypatch) -> None:
    counter = teardown_filter.counter
    writes: list[int] = []
    write = counter.write

    def counted_write() -> None:
        writes.append(len(counter._counts))
        write()

    monkeypatch.setattr(counter, "write", counted_write)

    @contextlib.asynccontextmanager
    async def inner(app: object) -> AsyncIterator[str]:
        yield "state"
        counter.add(("post", "closed", "none", "other", "none"))

    lifespan = teardown_filter.wrap_lifespan(inner, interval=0.02)
    async with lifespan(object()) as state:
        assert state == "state"
        deadline = time.monotonic() + 5
        while len(writes) < 2 and time.monotonic() < deadline:
            await asyncio.sleep(0.01)
        periodic = len(writes)
    assert periodic >= 2
    # The last write comes after the app exited and holds what it counted.
    assert writes[-1] == 1
    assert len(writes) == periodic + 1


# ---- Where it is installed ----


def _tools_list_digest() -> str:
    tools = asyncio.run(server.mcp.list_tools())
    core = [{"name": t.name, "description": t.description, "inputSchema": t.inputSchema} for t in tools]
    full = [t.model_dump(by_alias=True, mode="json") for t in tools]
    text = json.dumps([core, full], sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _run_http(monkeypatch) -> dict[str, Any]:
    import uvicorn

    from sugra_api_mcp import __main__ as entry
    from sugra_api_mcp import gate

    captured: dict[str, Any] = {}

    def run(app: Any, **kwargs: Any) -> None:
        captured.update(kwargs, app=app)

    monkeypatch.setattr(observability, "setup_observability", lambda: False)
    monkeypatch.setattr(uvicorn, "run", run)
    monkeypatch.setattr(server.mcp, "_session_manager", None)
    monkeypatch.delenv("SUGRA_AGENT_INTERNAL_TOKEN", raising=False)
    monkeypatch.setenv("SUGRA_APP_URL", "http://127.0.0.1:9")
    entry._run_server(argparse.Namespace(transport="streamable-http", host="127.0.0.1", port=0))
    assert captured["app"].user_middleware[0].cls is gate.GateMiddleware
    return captured


def test_the_http_server_installs_the_filter_and_leaves_tools_list_as_it_was(monkeypatch) -> None:
    from sugra_api_mcp import tools  # noqa: F401

    before = _tools_list_digest()
    captured = _run_http(monkeypatch)
    race_filter = teardown_filter.installed_filter()
    assert isinstance(race_filter, teardown_filter.TeardownRaceFilter)
    assert captured["app"].router.lifespan_context.__qualname__ == "wrap_lifespan.<locals>.lifespan"
    assert _tools_list_digest() == before
    # Installing twice leaves one filter.
    teardown_filter.install()
    sdk_filters = logging.getLogger(SDK).filters
    assert sum(isinstance(f, teardown_filter.TeardownRaceFilter) for f in sdk_filters) == 1


def test_the_three_messages_and_the_logger_are_the_installed_sdks_own() -> None:
    """A changed text in the SDK would let every race through again; this fails first."""
    source = inspect.getsource(sh)
    for msg in teardown_filter.RACE_MESSAGES:
        assert f'logger.exception("{msg}")' in source, msg
    assert sh.logger.name == teardown_filter.SDK_LOGGER


def test_install_is_idempotent_and_uninstall_removes_the_filter() -> None:
    first = teardown_filter.install()
    assert teardown_filter.install() is first
    assert teardown_filter.installed_filter() is first
    teardown_filter.uninstall()
    assert teardown_filter.installed_filter() is None
    assert teardown_filter.install() is not first


def _warnings(caplog) -> list[str]:
    return [
        r.getMessage()
        for r in caplog.records
        if r.name == teardown_filter.COUNTER_LOGGER and r.levelno == logging.WARNING
    ]


def test_install_is_silent_when_the_sdk_has_what_the_filter_reads(caplog) -> None:
    caplog.set_level(logging.INFO, logger=teardown_filter.COUNTER_LOGGER)
    teardown_filter.install()
    teardown_filter.install()
    assert _warnings(caplog) == []


def test_install_warns_once_naming_the_missing_transport_attribute(caplog, monkeypatch) -> None:
    caplog.set_level(logging.INFO, logger=teardown_filter.COUNTER_LOGGER)
    monkeypatch.delattr(StreamableHTTPServerTransport, "is_terminated")
    race_filter = teardown_filter.install()
    assert teardown_filter.install() is race_filter
    [warning] = _warnings(caplog)
    assert "StreamableHTTPServerTransport.is_terminated" in warning
    assert teardown_filter.installed_filter() is race_filter


def test_an_install_after_uninstall_does_not_warn_again(caplog, monkeypatch) -> None:
    caplog.set_level(logging.INFO, logger=teardown_filter.COUNTER_LOGGER)
    monkeypatch.delattr(StreamableHTTPServerTransport, "is_terminated")
    first = teardown_filter.install()
    teardown_filter.uninstall()
    second = teardown_filter.install()
    assert second is not first
    assert teardown_filter.installed_filter() is second
    [warning] = _warnings(caplog)
    assert "StreamableHTTPServerTransport.is_terminated" in warning


def test_install_warns_when_the_sdk_logger_has_another_name(caplog, monkeypatch) -> None:
    caplog.set_level(logging.INFO, logger=teardown_filter.COUNTER_LOGGER)
    monkeypatch.setattr(sh, "logger", logging.getLogger("mcp.server.moved"))
    teardown_filter.install()
    [warning] = _warnings(caplog)
    assert f"logger {SDK}" in warning


def test_a_probe_that_fails_never_blocks_the_install(monkeypatch) -> None:
    def broken() -> list[str]:
        raise RuntimeError("probe")

    monkeypatch.setattr(teardown_filter, "_missing_sdk_parts", broken)
    assert teardown_filter.install() is teardown_filter.installed_filter()


def test_installs_at_the_same_time_leave_one_filter(monkeypatch) -> None:
    look = teardown_filter.installed_filter

    def slow_look() -> teardown_filter.TeardownRaceFilter | None:
        found = look()
        time.sleep(0.02)
        return found

    monkeypatch.setattr(teardown_filter, "installed_filter", slow_look)
    start = threading.Barrier(8)
    results: list[teardown_filter.TeardownRaceFilter] = []

    def run() -> None:
        start.wait()
        results.append(teardown_filter.install())

    threads = [threading.Thread(target=run) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(10)
    sdk_filters = [f for f in logging.getLogger(SDK).filters if isinstance(f, teardown_filter.TeardownRaceFilter)]
    assert len(results) == 8
    assert len(sdk_filters) == 1
    assert all(result is sdk_filters[0] for result in results)


def test_the_stdio_server_never_installs_the_filter(monkeypatch) -> None:
    from sugra_api_mcp.__main__ import _run_server

    ran: list[str] = []
    monkeypatch.setattr(server.mcp, "run", lambda transport: ran.append(transport))
    _run_server(argparse.Namespace(transport="stdio"))
    assert ran == ["stdio"]
    assert teardown_filter.installed_filter() is None
