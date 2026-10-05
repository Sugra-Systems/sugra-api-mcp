"""Demand count: initialize and tools/list requests at the gate, by caller class and status.

GateMiddleware sees every POST nginx proxies to /mcp, before any other layer
can refuse it, and the status the client was sent. For each such request that
is an initialize or a tools/list, or that was answered before its body was
read, it adds one to a count keyed by fixed classes. Every
SUMMARY_INTERVAL_SECONDS of the gate, and once more at shutdown, the counts
since the previous write are logged at INFO on sugra_mcp.demand, only when
there are any:

    sdemand1 side=<side> requests=<n> lines=<m> omitted=<o>
    <method> <status> <host> <ua> <origin> <client> <tools> <count>

method is initialize, tools/list, or unread: a request answered before its
body was read, as the auth layer answers a bad bearer token (401), so which
method it carried is not known. A batch, a body over the limit and every other
method are not counted.

status is the HTTP status the client was sent, or none when no answer started.

host, ua and origin are observability's caller classes of the Host,
User-Agent and Origin headers (_host_class, _text_class over _UA_PATTERNS,
_origin_class), the same classes the tool spans carry; a User-Agent no pattern
names is other.

client is the class of the initialize clientInfo name (_CLIENT_NAME_PATTERNS),
none when an initialize names no client, and - for every other method: only
initialize carries clientInfo.

tools is, on a 2xx tools/list answer, the first 8 hex of the sha256 of the
names of the tools it listed, sorted and joined by newlines (tools_digest), so
a change to the served set shows as a new value; - everywhere else.

Each line is a count since the previous write, per process, starting from zero
when the process starts. At most MAX_LINES lines are written, the largest
counts first; omitted is the sum of the counts left out. A line holds fixed
classes, a status and a count: never a header value, a client name, a session
id or a payload.
"""

from __future__ import annotations

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

# A count record stays far inside App Insights' 32,768 characters.
MAX_LINES = 400

NOT_APPLICABLE = "-"
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
        self._lock = threading.Lock()

    def add(self, key: Key) -> None:
        with self._lock:
            self._counts[key] = self._counts.get(key, 0) + 1

    def write(self) -> None:
        """Log the counts since the last write, if there are any, and start again from zero."""
        with self._lock:
            counts, self._counts = self._counts, {}
        if not counts:
            return
        ranked = sorted(counts.items(), key=lambda item: (-item[1], item[0]))
        shown = ranked[:MAX_LINES]
        omitted = sum(count for _, count in ranked[MAX_LINES:])
        header = (
            f"sdemand1 side={observability.process_side()} requests={sum(counts.values())} "
            f"lines={len(shown)} omitted={omitted}"
        )
        lines = [" ".join((*key, str(count))) for key, count in shown]
        logger.info("%s", "\n".join([header, *lines]))


default_counter = DemandCounter()


def write_logged(counter: DemandCounter) -> None:
    """Write the counts; a failure is logged by its class and never raised."""
    try:
        counter.write()
    except Exception as e:
        logger.warning("Demand count failed (%s).", type(e).__name__)
