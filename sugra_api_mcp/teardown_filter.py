"""Session teardown races of the SDK transport: dropped as errors, counted instead.

When a client session ends (a DELETE, the idle timeout, a shutdown) while the
SDK transport is still writing a message into it, the write meets a closed
stream and the SDK logs the anyio error with logger.exception on
mcp.server.streamable_http, at one of three sites:

    Error handling POST request      a message written after its 202 answer
    SSE response error               a request whose SSE answer has started
    Error in standalone SSE writer   the GET stream of server messages

The answer has already gone to the client and nothing is lost, but the Azure
Monitor exporter turns each such record into a server exception. This module
drops exactly those records and counts them instead. It is fail-open: a record
is dropped only when every condition below holds, and anything that cannot be
read, or any error in the filter itself, lets the record through unchanged.

    - the record is ERROR, from mcp.server.streamable_http;
    - its message is one of the three above, matched as text, with no args;
    - every leaf of its exception (the exception itself, or every exception
      inside an exception group) is anyio ClosedResourceError or
      BrokenResourceError, and no exception in the tree - a leaf, a group
      or the record's own exception - carries a cause or a context that
      is not itself in the tree;
    - when a transport is visible in the traceback frames, it reports
      is_terminated, and a BrokenResourceError leaf needs that transport.

A ClosedResourceError means the session's own stream was closed, which at
these sites only teardown does, so it is dropped even when no transport is
visible. A BrokenResourceError means the other end was closed, which can also
happen before the session is ended, so it is dropped only on a transport that
says it was ended. A visible transport that was not ended keeps every record.

Each drop is counted by site, leaf, and the host, User-Agent and Origin
classes of the request found in the same frames (observability's reducers;
unknown where no request is visible, as on the GET stream). Every
FLUSH_INTERVAL_SECONDS, and once more at shutdown, the counts since the
previous write are logged at INFO on sugra_mcp.session_race, only when there
are any:

    srace1 side=<side> dropped=<n> lines=<m> omitted=<o>
    <site> <leaf> <host> <ua> <origin> <count>

site is post, sse or get; leaf is closed, or broken when any leaf was a
BrokenResourceError. At most MAX_LINES lines are written, the largest counts
first; omitted is the sum of the counts left out. A line holds fixed classes
and a count, never a header value, an id or exception text.

Installed by the HTTP entry point only; the stdio server never loads it. Once
/mcp runs without sessions this whole class of record is gone, and so should
this module be.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import threading
from collections.abc import AsyncIterator, Callable, Iterator
from types import FrameType, TracebackType
from typing import Any

import anyio
from mcp.server.streamable_http import StreamableHTTPServerTransport
from starlette.requests import Request

from . import observability

SDK_LOGGER = "mcp.server.streamable_http"
COUNTER_LOGGER = "sugra_mcp.session_race"

# The SDK's own text at each site, and the site it names in the count.
RACE_MESSAGES: dict[str, str] = {
    "Error handling POST request": "post",
    "SSE response error": "sse",
    "Error in standalone SSE writer": "get",
}

FLUSH_INTERVAL_SECONDS = 60.0
# A count record stays far inside App Insights' 32,768 characters.
MAX_LINES = 400
# How much of an exception is read before the record is let through unread.
MAX_LEAVES = 32
MAX_GROUP_DEPTH = 4
MAX_FRAMES = 64

logger = logging.getLogger(COUNTER_LOGGER)
# Set here, not inherited: on the hosted server the root logger stays at
# WARNING, which would drop every count (gate.py sets its logger the same way).
logger.setLevel(logging.INFO)

_UNKNOWN = "unknown"

Key = tuple[str, str, str, str, str]


def _leaves(exc: BaseException) -> list[BaseException] | None:
    """Every leaf of an exception or exception group.

    None when there are too many to read, or when any exception in the tree,
    a group or the top exception included, carries a cause or a context
    outside the tree. A chain into the tree itself is no chain: anyio raises
    a task group's error while the body's exception is in flight, so the
    group's context is that same exception, already one of its leaves and
    read as one. Any other chain may lead to a real error.
    """
    leaves: list[BaseException] = []
    nodes: list[BaseException] = []

    def walk(e: BaseException, depth: int) -> bool:
        nodes.append(e)
        if isinstance(e, BaseExceptionGroup):
            if depth >= MAX_GROUP_DEPTH:
                return False
            return all(walk(inner, depth + 1) for inner in e.exceptions)
        leaves.append(e)
        return len(leaves) <= MAX_LEAVES

    if not walk(exc, 0) or not leaves:
        return None
    in_tree = {id(node) for node in nodes}
    for node in nodes:
        for chained in (node.__cause__, node.__context__):
            if chained is not None and id(chained) not in in_tree:
                return None
    return leaves


def _tracebacks(exc: BaseException) -> Iterator[TracebackType]:
    """The traceback of the exception, then of each exception inside it when it is a group."""
    pending: list[BaseException] = [exc]
    while pending:
        current = pending.pop(0)
        if current.__traceback__ is not None:
            yield current.__traceback__
        if isinstance(current, BaseExceptionGroup):
            pending.extend(current.exceptions)


def _frames(exc: BaseException) -> Iterator[FrameType]:
    seen = 0
    for tb in _tracebacks(exc):
        current: TracebackType | None = tb
        while current is not None:
            if seen >= MAX_FRAMES:
                return
            seen += 1
            yield current.tb_frame
            current = current.tb_next


def _transport_and_request(
    exc: BaseException,
) -> tuple[StreamableHTTPServerTransport | None, Request | None]:
    """The first SDK transport (a frame's self) and Starlette request in the traceback frames."""
    transport: StreamableHTTPServerTransport | None = None
    request: Request | None = None
    for frame in _frames(exc):
        local_vars = frame.f_locals
        if transport is None:
            candidate = local_vars.get("self")
            if isinstance(candidate, StreamableHTTPServerTransport):
                transport = candidate
        if request is None:
            found = local_vars.get("request")
            if isinstance(found, Request):
                request = found
        if transport is not None and request is not None:
            break
    return transport, request


def _label(value: object) -> str:
    """A class as a count key part: the class itself when it is text, else none.

    A key part that is not a string would break the sort and the join of the
    next write, so nothing else ever reaches the counter.
    """
    return value if type(value) is str and value else "none"


def _request_classes(request: Request | None) -> tuple[str, str, str]:
    """Host, User-Agent and Origin classes of the request, unknown when none is visible."""
    if request is None:
        return _UNKNOWN, _UNKNOWN, _UNKNOWN
    headers = request.headers
    host = _label(observability._host_class(headers.get("host")))
    ua = _label(observability._text_class(headers.get("user-agent"), observability._UA_PATTERNS))
    origin = _label(observability._origin_class(headers.get("origin")))
    return host, ua, origin


def race_key(record: logging.LogRecord) -> Key | None:
    """The count key of a teardown race record, or None for every record that must pass."""
    if record.name != SDK_LOGGER or record.levelno != logging.ERROR or record.args:
        return None
    if type(record.msg) is not str or record.msg not in RACE_MESSAGES:
        return None
    exc_info = record.exc_info
    if not exc_info or not isinstance(exc_info[1], BaseException):
        return None
    exc = exc_info[1]
    leaves = _leaves(exc)
    if leaves is None:
        return None
    for leaf in leaves:
        if not isinstance(leaf, (anyio.ClosedResourceError, anyio.BrokenResourceError)):
            return None
    transport, request = _transport_and_request(exc)
    terminated = transport.is_terminated is True if transport is not None else None
    if terminated is False:
        return None
    broken = any(isinstance(leaf, anyio.BrokenResourceError) for leaf in leaves)
    if broken and terminated is not True:
        return None
    host, ua, origin = _request_classes(request)
    return (RACE_MESSAGES[record.msg], "broken" if broken else "closed", host, ua, origin)


class RaceCounter:
    """Drops counted since the last write, by key."""

    def __init__(self) -> None:
        self._counts: dict[Key, int] = {}
        self._lock = threading.Lock()

    def add(self, key: Key) -> None:
        with self._lock:
            self._counts[key] = self._counts.get(key, 0) + 1

    def write(self) -> None:
        """Log the counts since the last write, if there are any, and start again from zero.

        The text is made before the counts are reset, so a write that fails
        keeps them for the next one.
        """
        with self._lock:
            if not self._counts:
                return
            text = _count_text(self._counts)
            self._counts = {}
        logger.info("%s", text)


def _count_text(counts: dict[Key, int]) -> str:
    ranked = sorted(counts.items(), key=lambda item: (-item[1], item[0]))
    shown = ranked[:MAX_LINES]
    omitted = sum(count for _, count in ranked[MAX_LINES:])
    header = (
        f"srace1 side={observability.process_side()} dropped={sum(counts.values())} "
        f"lines={len(shown)} omitted={omitted}"
    )
    lines = [" ".join((*key, str(count))) for key, count in shown]
    return "\n".join([header, *lines])


default_counter = RaceCounter()


class TeardownRaceFilter(logging.Filter):
    """Drops a teardown race record and counts it; lets every other record through."""

    def __init__(self, counter: RaceCounter) -> None:
        super().__init__()
        self.counter = counter

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            key = race_key(record)
            if key is None:
                return True
            self.counter.add(key)
        except Exception:
            return True
        return False


def installed_filter() -> TeardownRaceFilter | None:
    """The filter on the SDK transport logger, or None when it is not installed."""
    for candidate in logging.getLogger(SDK_LOGGER).filters:
        if isinstance(candidate, TeardownRaceFilter):
            return candidate
    return None


_install_lock = threading.Lock()


def install(counter: RaceCounter | None = None) -> TeardownRaceFilter:
    """Put the filter on the SDK transport logger once.

    A second call with the same counter (no counter means default_counter)
    returns the first filter. A second call with another counter raises
    RuntimeError: its drops would be counted where nothing writes them.
    """
    wanted = counter if counter is not None else default_counter
    with _install_lock:
        existing = installed_filter()
        if existing is not None:
            if existing.counter is not wanted:
                raise RuntimeError("the teardown race filter is installed with another counter")
            return existing
        race_filter = TeardownRaceFilter(wanted)
        logging.getLogger(SDK_LOGGER).addFilter(race_filter)
        return race_filter


def uninstall() -> None:
    """Take every filter of this module off the SDK transport logger."""
    with _install_lock:
        sdk_logger = logging.getLogger(SDK_LOGGER)
        for candidate in list(sdk_logger.filters):
            if isinstance(candidate, TeardownRaceFilter):
                sdk_logger.removeFilter(candidate)


def _write_logged(counter: RaceCounter) -> None:
    try:
        counter.write()
    except Exception as e:
        logger.warning("Session race count failed (%s).", type(e).__name__)


async def _write_every(counter: RaceCounter, interval: float) -> None:
    while True:
        await asyncio.sleep(interval)
        _write_logged(counter)


def wrap_lifespan(
    inner: Callable[[Any], contextlib.AbstractAsyncContextManager[Any]],
    counter: RaceCounter | None = None,
    *,
    interval: float = FLUSH_INTERVAL_SECONDS,
) -> Callable[[Any], contextlib.AbstractAsyncContextManager[Any]]:
    """The app lifespan inner, with the counts written every interval and once more on exit.

    The last write runs after inner has exited, so the races of the sessions
    ended at shutdown are in it; wrapped by gate.wrap_lifespan, it runs before
    the telemetry flush.
    """
    race_counter = counter if counter is not None else default_counter

    @contextlib.asynccontextmanager
    async def race_lifespan(app: Any) -> AsyncIterator[Any]:
        writer = asyncio.create_task(_write_every(race_counter, interval))
        try:
            async with inner(app) as state:
                yield state
        finally:
            writer.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await writer
            _write_logged(race_counter)

    return race_lifespan
