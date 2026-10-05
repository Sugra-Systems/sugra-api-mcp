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
def _no_filter_left_behind() -> Iterator[None]:
    teardown_filter.uninstall()
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
    counter = teardown_filter.RaceCounter()
    teardown_filter.install(counter)
    deleted, answered = await _notification_meets_a_delete()
    assert (deleted, answered) == (200, 202)
    assert _race_records(sdk_records) == []
    assert counter._counts == {("post", "closed", "app.sugra.ai", "chatgpt", "openai"): 1}


async def test_the_real_race_on_an_sse_answer_is_dropped(monkeypatch, sdk_records) -> None:
    """The session is ended by a task that runs at the checkpoint inside the SDK's writer.send."""
    counter = teardown_filter.RaceCounter()
    teardown_filter.install(counter)
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


def _passes(record: logging.LogRecord, counter: teardown_filter.RaceCounter | None = None) -> bool:
    race_filter = teardown_filter.TeardownRaceFilter(counter or teardown_filter.RaceCounter())
    return race_filter.filter(record) is True


@pytest.mark.parametrize("msg", ALL_MSGS)
async def test_each_message_drops_a_closed_leaf_bare_or_in_a_group(msg) -> None:
    counter = teardown_filter.RaceCounter()
    bare = _raised_in_handler(anyio.ClosedResourceError(), _transport(True), _request())
    grouped = _raised_in_handler(await _group(anyio.ClosedResourceError()), _transport(True), _request())
    assert not _passes(_record(msg, bare), counter)
    assert not _passes(_record(msg, grouped), counter)
    site = teardown_filter.RACE_MESSAGES[msg]
    assert counter._counts == {(site, "closed", "mcp.sugra.ai", "claude", "anthropic"): 2}


@pytest.mark.parametrize("msg", ALL_MSGS)
def test_a_closed_leaf_with_no_transport_visible_is_dropped_as_unknown(msg) -> None:
    counter = teardown_filter.RaceCounter()
    assert not _passes(_record(msg, _raised_in_handler(anyio.ClosedResourceError())), counter)
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
    counter = teardown_filter.RaceCounter()
    assert not _passes(_record(SSE_MSG, _raised_in_handler(group, _transport(True), _request())), counter)
    assert counter._counts == {("sse", "closed", "mcp.sugra.ai", "claude", "anthropic"): 1}
    # The same shape whose leaf was raised while a real error was handled keeps the record.
    try:
        raise KeyError("real")
    except KeyError:
        chained = await body(anyio.ClosedResourceError())
    assert isinstance(chained.exceptions[0].__context__, KeyError)
    assert _passes(_record(SSE_MSG, _raised_in_handler(chained, _transport(True))))


def test_a_request_without_user_agent_or_origin_is_counted() -> None:
    bare = Request({"type": "http", "method": "POST", "path": "/mcp", "headers": [(b"host", b"app.sugra.ai")]})
    counter = teardown_filter.RaceCounter()
    assert not _passes(_record(POST_MSG, _raised_in_handler(anyio.ClosedResourceError(), _transport(True), bare)), counter)
    assert counter._counts == {("post", "closed", "app.sugra.ai", "other", "none"): 1}


def test_a_class_that_is_not_text_is_counted_as_none(caplog, monkeypatch) -> None:
    caplog.set_level(logging.INFO, logger=teardown_filter.COUNTER_LOGGER)
    monkeypatch.setattr(observability, "_host_class", lambda value: None)
    monkeypatch.setattr(observability, "_text_class", lambda value, patterns: None)
    monkeypatch.setattr(observability, "_origin_class", lambda value: None)
    counter = teardown_filter.RaceCounter()
    assert not _passes(_record(POST_MSG, _raised_in_handler(anyio.ClosedResourceError(), _transport(True), _request())), counter)
    assert not _passes(_record(GET_MSG, _raised_in_handler(anyio.ClosedResourceError())), counter)
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
    counter = teardown_filter.RaceCounter()
    assert _passes(record, counter)
    assert counter._counts == {}


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
        f"srace1 side={side} dropped=3 lines=2 omitted=0",
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
    assert message.splitlines()[0].endswith("dropped=4 lines=1 omitted=1")
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


async def test_the_lifespan_writes_every_interval_and_once_more_on_exit(monkeypatch) -> None:
    counter = teardown_filter.RaceCounter()
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

    lifespan = teardown_filter.wrap_lifespan(inner, counter, interval=0.02)
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
    assert race_filter is not None and race_filter.counter is teardown_filter.default_counter
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


def test_a_second_install_with_another_counter_raises() -> None:
    counter = teardown_filter.RaceCounter()
    first = teardown_filter.install(counter)
    assert teardown_filter.install(counter) is first
    with pytest.raises(RuntimeError):
        teardown_filter.install(teardown_filter.RaceCounter())
    with pytest.raises(RuntimeError):
        teardown_filter.install()
    assert teardown_filter.installed_filter() is first
    teardown_filter.uninstall()
    default = teardown_filter.install()
    assert teardown_filter.install() is default
    assert teardown_filter.install(teardown_filter.default_counter) is default


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
