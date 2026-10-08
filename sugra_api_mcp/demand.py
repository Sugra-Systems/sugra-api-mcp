"""Demand count: requests at the gate by method bucket, caller class and status.

GateMiddleware counts every request that reaches the gate as a POST to /mcp
whose first X-Request-Id is a valid request id (nginx gives one to every
request it proxies; the gate tracks no other request), exactly once: by its
method bucket, in failed, or in lost. A POST to /mcp is one whose path equals
/mcp once trailing slashes are removed, the same test auth.py uses to decide
what /mcp is, so /mcp/ is one and /mcpx, /mcp/x and /api/mcp are not. A POST
to any other path, or without a valid request id, writes nothing at all: no
line and no zero. Every SUMMARY_INTERVAL_SECONDS of the gate, and at shutdown, the counts
since the previous write are logged at INFO on sugra_mcp.demand, when there
are any or lost grew since the last record (such a record can read
requests=0 lines=0 with only lost grown):

    sdemand1 side=<side> requests=<n> lines=<m> omitted=<o> failed=<f> lost=<l>
    <method> <status> <host> <ua> <origin> <client> <count>

method is initialize or tools/list; other for any other single JSON-RPC
body (tools/call, ping, notifications, a body without a method); batch for a
JSON array, counted once whatever it holds; or unread for a body the gate
did not decode, so which method it carried is not known. That is a body
answered before it was read, as the auth layer answers a bad bearer token
(401), a body over the server's limit, whatever its size (the server's 413),
and a body read whole that is not JSON; the status tells them apart.

status is the HTTP status of the answer start that was sent to the client,
or none when none was: no answer started, or its send failed.

host is app.sugra.ai or mcp.sugra.ai when the Host header names one of those
two (observability._CALLER_HOSTS, a fixed set; a subdomain is not one of
them), else the fixed class loopback, other or none. ua and origin are
observability's caller classes of the User-Agent and Origin headers
(_text_class over _UA_PATTERNS, _origin_class), the same classes the tool
spans carry; a User-Agent no pattern names is other.

client is the class of the initialize clientInfo name (_CLIENT_NAME_PATTERNS),
none when an initialize names no client, and - for every other method: only
initialize carries clientInfo.

failed counts the requests whose demand step raised: such a request is in
requests and in no line, and the answer it was sent is never touched by it.

lost is the total, since the process started, of the requests counted into
no line. It has two sources. The first is the requests of a write the logger
raised on: such a write's interval is dropped, never retried, so no request
is ever written twice; a handler may still have taken the record before
another raised. The second is a request still running after the gate's exit
drain: the exit sequence closes the counter (close) right before its final
write, which comes right before the telemetry flush, and a request counted
after close goes to lost. A lost request is in no later requests total, so
what a process counted is the sum of requests over the records it wrote plus
the lost of its last record, less a loss taken after that record.

A loss taken after the final exit write is NOT reported by the exiting
process: it writes no further record. It shows only if the same counter
serves again (a lifespan started again calls open), in the lost of its next
record.

Each line is a count since the previous write, per process, starting from zero
when the process starts. At most MAX_LINES lines are written, the largest
counts first, each field cut to FIELD_MAX characters and the side to
SIDE_MAX, and no more lines than fit in RECORD_MAX_CHARS with the header
measured, so a record stays inside App Insights' 32,768 characters whatever
the fields and counts hold; omitted is the sum of the counts left out, and
requests is the sum of every line, omitted and failed. A line holds fixed
classes, a status and a count: no header text except one of the two host
names above, never a client name, a session id or a payload.
"""

from __future__ import annotations

import contextlib
import logging
import threading

from . import observability

COUNTER_LOGGER = "sugra_mcp.demand"

INITIALIZE = "initialize"
TOOLS_LIST = "tools/list"
UNREAD = "unread"
OTHER = "other"
BATCH = "batch"
COUNTED_METHODS = frozenset({INITIALIZE, TOOLS_LIST})

MAX_LINES = 400
# App Insights keeps at most 32,768 characters of a log message. Every class
# is far shorter than FIELD_MAX; the cut only bounds a line. The side is cut
# to SIDE_MAX, process_side's own bound, so it stays the side sgate1 carries.
FIELD_MAX = 16
SIDE_MAX = 128
RECORD_MAX_CHARS = 32_000

NOT_APPLICABLE = "-"
_NONE = "none"

logger = logging.getLogger(COUNTER_LOGGER)
# Set here, not inherited: on the hosted server the root logger stays at
# WARNING, which would drop every count (gate.py sets its logger the same way).
logger.setLevel(logging.INFO)

Key = tuple[str, str, str, str, str, str]
ExtensionKey = tuple[str, str]

# The extra demand counts, kept only when GateMiddleware is built with
# extended_demand (the request limits setting, sugra_api_mcp.limits). Each
# request adds to them beside its sdemand1 line, never in place of it:
#
#   discover   - a server/discover request, value attempt
#   events     - any events/... request, one value for every method
#   extension  - an extension an initialize declares in capabilities.extensions,
#                by a closed list: ui for io.modelcontextprotocol/ui, other
#                for every other name. A name is never written.
#
# They are written beside each sdemand1 record as one more record:
#
#     sdemandx1 side=<side> lines=<m>
#     <kind> <value> <count>
DISCOVER_KIND = "discover"
EVENTS_KIND = "events"
EXTENSION_KIND = "extension"
DISCOVER_METHOD = "server/discover"
EVENTS_PREFIX = "events/"
EXTENSION_CLASSES = {"io.modelcontextprotocol/ui": "ui"}
EXTENSION_OTHER = "other"
# Most items of a batch and most declared extensions looked at.
_EXTENSION_SCAN_MAX = 16
_EXTENSION_NAMES_MAX = 64


def extension_facts(message: object) -> list[ExtensionKey]:
    """The extra demand counts one decoded request body adds, as (kind, value) pairs."""
    items = message[:_EXTENSION_SCAN_MAX] if isinstance(message, list) else [message]
    facts: list[ExtensionKey] = []
    for item in items:
        method = item.get("method") if isinstance(item, dict) else None
        if type(method) is not str:
            continue
        if method == DISCOVER_METHOD:
            facts.append((DISCOVER_KIND, "attempt"))
        elif method.startswith(EVENTS_PREFIX):
            facts.append((EVENTS_KIND, "request"))
        elif method == INITIALIZE:
            facts.extend((EXTENSION_KIND, value) for value in _declared_extensions(item))
    return facts


def _declared_extensions(initialize: dict[str, object]) -> list[str]:
    params = initialize.get("params")
    capabilities = params.get("capabilities") if isinstance(params, dict) else None
    declared = capabilities.get("extensions") if isinstance(capabilities, dict) else None
    if not isinstance(declared, dict):
        return []
    classes = {
        EXTENSION_CLASSES.get(name, EXTENSION_OTHER) if type(name) is str else EXTENSION_OTHER
        for name in list(declared)[:_EXTENSION_NAMES_MAX]
    }
    return sorted(classes)


def request_facts(message: object) -> tuple[str, str]:
    """The method bucket of a decoded request body and its client class."""
    if isinstance(message, list):
        return BATCH, NOT_APPLICABLE
    method = message.get("method") if isinstance(message, dict) else None
    if type(method) is not str or method not in COUNTED_METHODS:
        return OTHER, NOT_APPLICABLE
    if method != INITIALIZE:
        return method, NOT_APPLICABLE
    params = message.get("params")
    client_info = params.get("clientInfo") if isinstance(params, dict) else None
    name = client_info.get("name") if isinstance(client_info, dict) else None
    if type(name) is not str or not name:
        return method, _NONE
    return method, observability._text_class(name, observability._CLIENT_NAME_PATTERNS)


def caller_classes(host: str | None, user_agent: str | None, origin: str | None) -> tuple[str, str, str]:
    """Host, User-Agent and Origin classes of the request headers, never their text."""
    return (
        observability._host_class(host) or _NONE,
        observability._text_class(user_agent, observability._UA_PATTERNS),
        observability._origin_class(origin),
    )


def status_of(status: object) -> str:
    """The HTTP status as digits, none when no answer started."""
    # isinstance, not type: the SDK sends some statuses as HTTPStatus members.
    if isinstance(status, int) and not isinstance(status, bool) and 100 <= status <= 599:
        return str(int(status))
    return _NONE


class DemandCounter:
    """Requests counted since the last write, by key, and the requests counted into no line.

    lost has two sources: the requests of a write the logger raised on, and a
    count taken after close (a request still running after the exit drain).
    _lock guards the counts and is all add and add_failure take, so a request
    never waits on a log write. _write_lock serialises whole writes, taken
    before _lock and never inside it.
    """

    def __init__(self) -> None:
        self._counts: dict[Key, int] = {}
        self._extensions: dict[ExtensionKey, int] = {}
        self._failed = 0
        self._lost = 0
        self._lost_written = 0
        self._closed = False
        self._lock = threading.Lock()
        self._write_lock = threading.Lock()

    def add(self, key: Key) -> None:
        with self._lock:
            if self._closed:
                self._lost += 1
            else:
                self._counts[key] = self._counts.get(key, 0) + 1

    def add_extension(self, key: ExtensionKey) -> None:
        """Count one of the extra demand counts. After close it is dropped, not lost: it is no request."""
        with self._lock:
            if not self._closed:
                self._extensions[key] = self._extensions.get(key, 0) + 1

    def add_failure(self) -> None:
        """Count a request whose demand step raised."""
        with self._lock:
            if self._closed:
                self._lost += 1
            else:
                self._failed += 1

    def close(self) -> None:
        """From now on a count goes to lost: the final write is next."""
        with self._lock:
            self._closed = True

    def open(self) -> None:
        """Count into lines again (a lifespan starting)."""
        with self._lock:
            self._closed = False

    def write(self) -> None:
        """Log the counts since the last write and start again from zero.

        The interval is taken whole under the lock. When formatting or the
        logger raises, its requests are dropped, never put back, and added to
        the lost total the next header reports; the error is raised. A record
        is written when the interval holds a request or lost grew since the
        last record. A whole write runs under the write lock, from the
        snapshot to the record of the lost it reported, so two writers never
        report one growth of lost twice and an older snapshot is never
        written after a newer one.
        """
        with self._write_lock:
            self._write()

    def _write(self) -> None:
        with self._lock:
            counts, self._counts = self._counts, {}
            extensions, self._extensions = self._extensions, {}
            failed, self._failed = self._failed, 0
            lost = self._lost
        if not counts and not failed and lost == self._lost_written:
            # Only the extra counts of a request counted in the interval before.
            if extensions:
                with contextlib.suppress(Exception):
                    _log_extensions(extensions)
            return
        try:
            _log(counts, failed, lost)
        except BaseException:
            with self._lock:
                self._lost += sum(counts.values()) + failed
            raise
        with self._lock:
            self._lost_written = lost
        if extensions:
            # Telemetry beside the record, never a reason to fail it: a write
            # the logger raised on drops this interval's extra counts.
            with contextlib.suppress(Exception):
                _log_extensions(extensions)


def _header(side: str, requests: int, lines: int, omitted: int, failed: int, lost: int) -> str:
    return (
        f"sdemand1 side={side} requests={requests} lines={lines} "
        f"omitted={omitted} failed={failed} lost={lost}"
    )


def _log(counts: dict[Key, int], failed: int, lost: int) -> None:
    ranked = sorted(counts.items(), key=lambda item: (-item[1], item[0]))[:MAX_LINES]
    total = sum(counts.values())
    side = observability.process_side()[:SIDE_MAX]
    # The header with the widest lines and omitted it can have: the real one
    # is never longer, so the room is measured, not assumed.
    widest = _header(side, total + failed, len(ranked), total, failed, lost)
    room = RECORD_MAX_CHARS - len(widest)
    lines: list[str] = []
    shown = 0
    for key, count in ranked:
        line = " ".join((*(field[:FIELD_MAX] for field in key), str(count)))
        room -= len(line) + 1
        if room < 0:
            break
        lines.append(line)
        shown += count
    header = _header(side, total + failed, len(lines), total - shown, failed, lost)
    logger.info("%s", "\n".join([header, *lines]))


def _log_extensions(counts: dict[ExtensionKey, int]) -> None:
    lines = [
        " ".join((kind[:FIELD_MAX], value[:FIELD_MAX], str(count)))
        for (kind, value), count in sorted(counts.items())
    ]
    side = observability.process_side()[:SIDE_MAX]
    logger.info("%s", "\n".join([f"sdemandx1 side={side} lines={len(lines)}", *lines]))


default_counter = DemandCounter()


def write_logged(counter: DemandCounter) -> None:
    """Write the counts; an Exception is logged by its class and never raised.

    A BaseException that is no Exception (an interrupt) passes through: the
    exit sequence catches it around this call (gate._write_at_exit).
    """
    try:
        counter.write()
    except Exception as e:
        # The same logger may be what failed: its report must not raise either.
        with contextlib.suppress(Exception):
            logger.warning("Demand count failed (%s).", type(e).__name__)
