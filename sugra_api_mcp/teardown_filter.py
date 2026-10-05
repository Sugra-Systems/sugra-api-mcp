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
drops the records that meet every condition below and counts them instead;
every other record is kept unchanged. It is fail-open: a record whose
exception chains to anything outside its own tree, a walk that hits a cap
(MAX_LEAVES, MAX_GROUP_DEPTH or MAX_FRAMES), anything that cannot be read,
and any error in the filter itself all keep the record.

    - the record is ERROR, from mcp.server.streamable_http;
    - its message is one of the three above, matched as text, with no args;
    - every leaf of its exception (the exception itself, or every exception
      inside an exception group) is anyio ClosedResourceError or
      BrokenResourceError, and no exception in the tree - a leaf, a group
      or the record's own exception - carries a cause or a context that
      is not itself in the tree;
    - every traceback frame was read (at most MAX_FRAMES), every transport
      visible in them reports is_terminated, and a BrokenResourceError leaf
      needs at least one such transport.

A ClosedResourceError means the session's own stream was closed, which at
these sites only teardown does, so it is dropped even when no transport is
visible. A BrokenResourceError means the other end was closed, which can also
happen before the session is ended, so it is dropped only on a transport that
says it was ended. Any visible transport that was not ended keeps the record,
whatever the other transports say.

Each drop is counted by site, leaf, and the host, User-Agent and Origin
classes of the request found in the same frames (observability's reducers;
unknown where no request is visible, as on the GET stream). Every
FLUSH_INTERVAL_SECONDS, and once more at shutdown, the counts since the
previous write are logged at INFO on sugra_mcp.session_race, only when there
are any:

    srace1 side=<side> dropped=<n> lines=<m> omitted=<o>
    <site> <leaf> <host> <ua> <origin> <count>

site is post, sse or get; leaf is closed, or broken when any leaf was a
BrokenResourceError. Lines are written largest count first, at most MAX_LINES
of them and only while the record stays within MAX_RECORD_CHARS; omitted is
the sum of the counts of every line left out, so dropped always equals the
written counts plus omitted. side is process_side(), or unknown when it is
longer than MAX_SIDE_CHARS. A line holds fixed classes and a count, never a
header value, an id or exception text: a class outside the reducers' known
labels is written as other.

There is one counter, the module's counter: the filter counts into it and the
lifespan wrapper writes it. Neither takes another.

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
# A count record holds at most MAX_LINES lines and at most MAX_RECORD_CHARS
# characters, header included: App Insights keeps 32,768 characters of a
# message, and the rest is a reserve. _count_text stops adding lines at
# whichever bound comes first and counts every line left out in omitted.
MAX_LINES = 400
MAX_RECORD_CHARS = 32_768 - 768
# A longer side is written as unknown (process_side allows at most 128).
MAX_SIDE_CHARS = 128
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


def _frames(exc: BaseException) -> Iterator[FrameType | None]:
    """The traceback frames, at most MAX_FRAMES of them, then None once if any were left unread."""
    seen = 0
    for tb in _tracebacks(exc):
        current: TracebackType | None = tb
        while current is not None:
            if seen >= MAX_FRAMES:
                yield None
                return
            seen += 1
            yield current.tb_frame
            current = current.tb_next


def _transports_and_request(
    exc: BaseException,
) -> tuple[list[StreamableHTTPServerTransport], Request | None, bool]:
    """Every SDK transport (a frame's self) and the first Starlette request in the traceback frames.

    Every frame is read, up to MAX_FRAMES, so a transport in a later frame is
    never missed. The third value is True when frames were left unread: the
    transports found are then not known to be all of them.
    """
    transports: list[StreamableHTTPServerTransport] = []
    request: Request | None = None
    for frame in _frames(exc):
        if frame is None:
            return transports, request, True
        local_vars = frame.f_locals
        candidate = local_vars.get("self")
        if isinstance(candidate, StreamableHTTPServerTransport) and all(
            candidate is not seen for seen in transports
        ):
            transports.append(candidate)
        if request is None:
            found = local_vars.get("request")
            if isinstance(found, Request):
                request = found
    return transports, request, False


# Every label the reducers can return, each from the table the reducer itself
# reads (_host_class returns a _CALLER_HOSTS entry verbatim, loopback or other);
# any other value is written as other. A test calls the reducers over those
# tables and pins each set to what they return.
_HOST_LABELS = frozenset({*observability._CALLER_HOSTS, "loopback", "other"})
_UA_LABELS = frozenset({*(label for label, _ in observability._UA_PATTERNS), "other"})
_ORIGIN_LABELS = frozenset({*observability._ORIGIN_CLASSES.values(), "none", "other"})


def _label(value: object, known: frozenset[str]) -> str:
    """A class as a count key part: none without one, the class when it is a known label, else other.

    A key part that is not a string would break the sort and the join of the
    next write, and a string that is not a known label could be header text,
    so neither ever reaches the counter.
    """
    if value is None or value == "":
        return "none"
    return value if type(value) is str and value in known else "other"


def _request_classes(request: Request | None) -> tuple[str, str, str]:
    """Host, User-Agent and Origin classes of the request, unknown when none is visible."""
    if request is None:
        return _UNKNOWN, _UNKNOWN, _UNKNOWN
    headers = request.headers
    host = _label(observability._host_class(headers.get("host")), _HOST_LABELS)
    ua = _label(
        observability._text_class(headers.get("user-agent"), observability._UA_PATTERNS), _UA_LABELS
    )
    origin = _label(observability._origin_class(headers.get("origin")), _ORIGIN_LABELS)
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
    transports, request, truncated = _transports_and_request(exc)
    if truncated:
        return None
    # Any visible transport that was not ended vetoes the drop.
    if not all(transport.is_terminated is True for transport in transports):
        return None
    broken = any(isinstance(leaf, anyio.BrokenResourceError) for leaf in leaves)
    if broken and not transports:
        return None
    host, ua, origin = _request_classes(request)
    return (RACE_MESSAGES[record.msg], "broken" if broken else "closed", host, ua, origin)


class RaceCounter:
    """Drops counted since the last write, by key."""

    def __init__(self) -> None:
        self._counts: dict[Key, int] = {}
        self._lock = threading.Lock()
        # One write at a time, so two writers never log the same counts.
        self._write_lock = threading.Lock()

    def add(self, key: Key) -> None:
        with self._lock:
            self._counts[key] = self._counts.get(key, 0) + 1

    def write(self) -> None:
        """Log the counts since the last write, if there are any, and take them off.

        The counts written are taken off once logger.info has returned. An
        exception that reaches write - from building the text, a filter, or a
        handler that raises - keeps them for the next one. A handler that
        catches its own error (logging.Handler.handleError, as the exporter's
        emit does) returns normally, so those counts are taken off and lost
        with that record. The counter lock is not held while the record is
        logged: drops counted meanwhile, even by a handler, stay for the next
        write.
        """
        with self._write_lock:
            with self._lock:
                if not self._counts:
                    return
                written = dict(self._counts)
            text = _count_text(written)
            logger.info("%s", text)
            with self._lock:
                for key, count in written.items():
                    left = self._counts.get(key, 0) - count
                    if left > 0:
                        self._counts[key] = left
                    else:
                        self._counts.pop(key, None)


def _side() -> str:
    side = observability.process_side()
    return side if type(side) is str and 0 < len(side) <= MAX_SIDE_CHARS else _UNKNOWN


def _count_text(counts: dict[Key, int]) -> str:
    """The count record: the header, then lines largest first within MAX_LINES and MAX_RECORD_CHARS.

    Every line left out, by either bound, is counted in omitted, so the
    written counts plus omitted always equal dropped.
    """
    ranked = sorted(counts.items(), key=lambda item: (-item[1], item[0]))
    dropped = sum(counts.values())
    prefix = f"srace1 side={_side()} dropped={dropped} "

    def header(lines: int, omitted: int) -> str:
        return f"{prefix}lines={lines} omitted={omitted}"

    # The header's length with every count omitted bounds it from above: lines
    # and omitted only shrink from there as lines are added.
    used = len(header(MAX_LINES, dropped))
    lines: list[str] = []
    written = 0
    for key, count in ranked:
        if len(lines) >= MAX_LINES:
            break
        line = " ".join((*key, str(count)))
        if used + 1 + len(line) > MAX_RECORD_CHARS:
            break
        lines.append(line)
        used += 1 + len(line)
        written += count
    return "\n".join([header(len(lines), dropped - written), *lines])


# The one counter: the filter counts into it, the lifespan wrapper writes it.
counter = RaceCounter()


class TeardownRaceFilter(logging.Filter):
    """Drops a teardown race record and counts it in the module's counter; lets every other record through."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            key = race_key(record)
            if key is None:
                return True
            counter.add(key)
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


def install() -> TeardownRaceFilter:
    """Put the filter on the SDK transport logger once; a second call returns the first filter."""
    with _install_lock:
        existing = installed_filter()
        if existing is not None:
            return existing
        race_filter = TeardownRaceFilter()
        logging.getLogger(SDK_LOGGER).addFilter(race_filter)
        return race_filter


def uninstall() -> None:
    """Take every filter of this module off the SDK transport logger."""
    with _install_lock:
        sdk_logger = logging.getLogger(SDK_LOGGER)
        for candidate in list(sdk_logger.filters):
            if isinstance(candidate, TeardownRaceFilter):
                sdk_logger.removeFilter(candidate)


def _write_logged() -> None:
    """Write the module's counter; a failure is a warning, never a raise."""
    try:
        counter.write()
    except Exception as e:
        logger.warning("Session race count failed (%s).", type(e).__name__)


async def _write_every(interval: float) -> None:
    while True:
        await asyncio.sleep(interval)
        _write_logged()


def wrap_lifespan(
    inner: Callable[[Any], contextlib.AbstractAsyncContextManager[Any]],
    *,
    interval: float = FLUSH_INTERVAL_SECONDS,
) -> Callable[[Any], contextlib.AbstractAsyncContextManager[Any]]:
    """The app lifespan inner, with the module's counter written every interval and once more on exit.

    It writes the one counter the filter counts into. The last write runs
    after inner has exited, so the races of the sessions ended at shutdown are
    in it; wrapped by gate.wrap_lifespan, it runs before the telemetry flush.
    """

    @contextlib.asynccontextmanager
    async def race_lifespan(app: Any) -> AsyncIterator[Any]:
        writer = asyncio.create_task(_write_every(interval))
        try:
            async with inner(app) as state:
                yield state
        finally:
            writer.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await writer
            _write_logged()

    return race_lifespan
