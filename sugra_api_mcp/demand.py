"""Demand count: initialize and tools/list requests at the gate, by caller class and status.

GateMiddleware counts a request only when it is a POST to /mcp whose first
X-Request-Id is a valid request id (nginx gives one to every request it
proxies; the gate tracks no other request) and that is an initialize or a
tools/list, or was answered before its body was read. A POST to any other
path, or without a valid request id, writes nothing at all: no line and no
zero. Every SUMMARY_INTERVAL_SECONDS of the gate, and at shutdown, the counts
since the previous write are logged at INFO on sugra_mcp.demand, only when
there are any:

    sdemand1 side=<side> requests=<n> lines=<m> omitted=<o> failed=<f>
    <method> <status> <host> <ua> <origin> <client> <tools> <count>

method is initialize, tools/list, or unread: a request answered before its
body was read, as the auth layer answers a bad bearer token (401), so which
method it carried is not known. A batch, a body over the limit and every other
method (tools/call, ping, notifications) are not counted.

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

tools is, on a 2xx tools/list answer, the first 8 hex of the sha256 of the
names of the tools it listed, sorted and joined by newlines (tools_digest), so
a change to the served set shows as a new value. Two fixed classes stand in
for a digest that cannot be taken: over when the answer sent was longer than
the gate reads for it (gate._TOOLS_LIST_SCAN_BYTES), unparsed when what was
sent holds no tool list. Everywhere else it is -.

failed counts the requests whose demand step raised: such a request is in
requests and in no line, and the answer it was sent is never touched by it.

Each line is a count since the previous write, per process, starting from zero
when the process starts. At most MAX_LINES lines are written, the largest
counts first, each field cut to FIELD_MAX characters, and no more than fit in
RECORD_MAX_CHARS, so a record stays inside App Insights' 32,768 characters
whatever the classes hold; omitted is the sum of the counts left out, and
requests is the sum of every line, omitted and failed. A line holds fixed
classes, a status and a count: no header text except one of the two host
names above, never a client name, a session id or a payload.

A write the logger raises on is not lost: its counts go back into the
counter and the next write carries them (so a record a first handler took
before a second one raised is counted again).
"""

from __future__ import annotations

import contextlib
import hashlib
import logging
import threading
from collections.abc import Iterable

from . import observability

COUNTER_LOGGER = "sugra_mcp.demand"

INITIALIZE = "initialize"
TOOLS_LIST = "tools/list"
UNREAD = "unread"
COUNTED_METHODS = frozenset({INITIALIZE, TOOLS_LIST})

MAX_LINES = 400
# App Insights keeps at most 32,768 characters of a log message. Every class
# is far shorter than FIELD_MAX; the cut only bounds a line by construction.
# The header is under _HEADER_MAX_CHARS (a side of at most 128 characters and
# four counts of fewer than 20 digits each).
FIELD_MAX = 16
RECORD_MAX_CHARS = 32_000
_HEADER_MAX_CHARS = 256

NOT_APPLICABLE = "-"
# The tools field of a 2xx tools/list answer whose digest cannot be taken.
TOOLS_OVER = "over"
TOOLS_UNPARSED = "unparsed"
_NONE = "none"

logger = logging.getLogger(COUNTER_LOGGER)
# Set here, not inherited: on the hosted server the root logger stays at
# WARNING, which would drop every count (gate.py sets its logger the same way).
logger.setLevel(logging.INFO)

Key = tuple[str, str, str, str, str, str, str]


def tools_digest(names: Iterable[str]) -> str:
    """The first 8 hex of the sha256 of the tool names, sorted and joined by newlines."""
    return hashlib.sha256("\n".join(sorted(names)).encode("utf-8")).hexdigest()[:8]


def request_facts(message: object) -> tuple[str | None, str]:
    """The counted method of a parsed request body and its client class, or (None, -)."""
    if not isinstance(message, dict):
        return None, NOT_APPLICABLE
    method = message.get("method")
    if type(method) is not str or method not in COUNTED_METHODS:
        return None, NOT_APPLICABLE
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
    """Requests counted since the last write, by key."""

    def __init__(self) -> None:
        self._counts: dict[Key, int] = {}
        self._failed = 0
        self._lock = threading.Lock()

    def add(self, key: Key) -> None:
        with self._lock:
            self._counts[key] = self._counts.get(key, 0) + 1

    def add_failure(self) -> None:
        """Count a request whose demand step raised."""
        with self._lock:
            self._failed += 1

    def write(self) -> None:
        """Log the counts since the last write, if there are any, and start again from zero.

        When formatting or the logger raises, the counts taken go back into
        the counter, beside any added meanwhile, and the error is raised.
        """
        with self._lock:
            counts, self._counts = self._counts, {}
            failed, self._failed = self._failed, 0
        if not counts and not failed:
            return
        try:
            _log(counts, failed)
        except BaseException:
            with self._lock:
                for key, count in counts.items():
                    self._counts[key] = self._counts.get(key, 0) + count
                self._failed += failed
            raise


def _log(counts: dict[Key, int], failed: int) -> None:
    ranked = sorted(counts.items(), key=lambda item: (-item[1], item[0]))
    lines: list[str] = []
    shown = 0
    room = RECORD_MAX_CHARS - _HEADER_MAX_CHARS
    for key, count in ranked[:MAX_LINES]:
        line = " ".join((*(field[:FIELD_MAX] for field in key), str(count)))
        room -= len(line) + 1
        if room < 0:
            break
        lines.append(line)
        shown += count
    total = sum(counts.values())
    header = (
        f"sdemand1 side={observability.process_side()} requests={total + failed} "
        f"lines={len(lines)} omitted={total - shown} failed={failed}"
    )
    logger.info("%s", "\n".join([header, *lines]))


default_counter = DemandCounter()


def write_logged(counter: DemandCounter) -> None:
    """Write the counts; a failure is logged by its class, and nothing here ever raises."""
    try:
        counter.write()
    except Exception as e:
        # The same logger may be what failed: its report must not raise either.
        with contextlib.suppress(Exception):
            logger.warning("Demand count failed (%s).", type(e).__name__)
