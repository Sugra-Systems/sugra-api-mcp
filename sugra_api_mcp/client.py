"""Async HTTP client for the Sugra API, with size-limit enforcement on by
default.

Every call enforces `MAX_RESPONSE_CHARS` on the raw upstream body by
default - the backstop every caller gets for free, including the ones
that never shape their own response afterward. A caller that DOES shape
its own response after the fact (gateway.call_endpoint, which applies a
`fields` / `limit` projection) passes `enforce_size=False` and runs
`_enforce_size_limit` itself once the projection has run, so the raw,
unprojected body is never what gets measured for it.

Connections: every client on the default transport sends through the shared
pool of its API base (`shared_pool`), an `httpx.AsyncClient` that keeps its
connections open between requests. A pool belongs to the event loop it was
built on, and the served process runs one loop, so there every caller of an
API base shares one pool. The pool holds no key and stores no cookie: each
request carries its own caller's key as its x-api-key header, so no request
goes out with the key of another caller. `close_shared_pools` closes the
pools when the HTTP server shuts down.

Error contract: this client NEVER raises httpx exceptions to callers.
Transport failures (timeout, refused connection, mid-stream disconnect)
return structured dicts the agent can act on, mirroring the shape already
used for HTTP 4xx/5xx responses:

    {
        "error": "upstream_timeout" | "upstream_connect_error"
                 | "upstream_transport_error",
        "reason": "<exception class>: <message>",   # class name only if empty
        "status_code": None,                        # no HTTP status received
        "elapsed_ms": 30012,
        "url": "https://sugra.ai/api/v1/...",
        "retry_hint": "...",
        "timeout_s": 30.0,                          # upstream_timeout only
    }

Without this, httpx exceptions propagate to the MCP framework which renders
them as "Error executing tool call_endpoint: " with an EMPTY message
(httpx.ReadTimeout stringifies to ""), leaving the agent unable to pick a
retry strategy (field-test defect D2).
"""

from __future__ import annotations

import asyncio
import http.cookiejar
import json
import re
import ssl
import threading
import time
from collections.abc import Callable
from functools import partial
from typing import Any

import httpx

from . import __version__
from .config import Config

# The cap counts characters: json.dumps with its spaces and ASCII escapes
# (response_chars), so a CJK character counts six. Clients limit differently.
# Claude Code writes a text result over 50,000 characters to a file
# (https://code.claude.com/docs/en/mcp.md, "MCP output limits and warnings").
# Codex CLI limits a tool output by tokens, about 10,000 by default and set by
# the user, not by this server, and cuts the middle out beyond it. 18,000 kept
# every shape measured on 2026-10-06 under Codex's default budget, the densest
# included: records of small integers, about 2 characters a token. A denser
# result, or a lower budget the user sets, can still be trimmed by the client.
# The cap is the same for every client.
MAX_RESPONSE_CHARS = 18_000


_ssl_context: ssl.SSLContext | None = None
_ssl_context_lock = threading.Lock()


def shared_ssl_context() -> ssl.SSLContext:
    """The one TLS context every outbound client in this process shares.

    httpx builds a fresh SSLContext per AsyncClient whenever `verify` is left
    at its default. A context is expensive in both memory and construction
    time, and every client that builds its own pays that cost again.

    It is `httpx.create_ssl_context()`, NOT `ssl.create_default_context()`:
    httpx's own default loads certifi's CA bundle and honours SSL_CERT_FILE /
    SSL_CERT_DIR, while the stdlib constructor loads the system store, which
    is a different set of trust anchors. The anchors here stay exactly the
    ones an unconfigured httpx client would use.

    The context is built once per process, so those two environment variables
    are read once, at construction.

    ONE THING DOES write to the shared object. Before every TLS connect,
    httpcore calls `ssl_context.set_alpn_protocols(...)` on it (httpcore 1.0.9,
    `_async/connection.py`), with `["http/1.1", "h2"]` when that POOL was built
    with `http2=True` and `["http/1.1"]` otherwise. Every client in this package
    leaves httpx's `http2` default off, so every pool writes the same list and
    the write is idempotent - which is what makes one shared object safe here,
    not any promise that httpcore leaves it alone. Enabling HTTP/2 on ONE client
    while others share this context would let the pools overwrite each other's
    ALPN offer between connects; give that client its own context instead.
    A test pins the condition.

    Built on first use, not at import, so a process that never opens a pool
    on the default transport, such as a keyless stdio session, never builds
    one at all.

    `lru_cache` would not do here. Its miss path is not atomic, so two threads
    arriving first can each run the factory and each keep a DIFFERENT context,
    which is exactly the invariant this function exists to hold. Double-checked
    locking does hold it: the global is only ever assigned a fully-built
    context, so the fast path reads either None or the final object.
    """
    global _ssl_context
    if _ssl_context is None:
        with _ssl_context_lock:
            if _ssl_context is None:
                _ssl_context = httpx.create_ssl_context()
    return _ssl_context


# The limits of each shared pool: at most 32 connections open at once, of
# which 20 are kept open while idle, each for up to 5 seconds. A request that
# finds all 32 busy waits for one, within its own timeout.
POOL_LIMITS = httpx.Limits(max_connections=32, max_keepalive_connections=20, keepalive_expiry=5.0)

# The shared pools, by the event loop each was built on and its API base.
_pools: dict[tuple[asyncio.AbstractEventLoop, str], httpx.AsyncClient] = {}
_pools_lock = threading.Lock()


def _no_cookies() -> http.cookiejar.CookieJar:
    """A cookie jar that stores nothing: its policy allows no domain, so a
    Set-Cookie on one response is never stored and never sent again."""
    return http.cookiejar.CookieJar(http.cookiejar.DefaultCookiePolicy(allowed_domains=[]))


def _new_http_client(
    api_base: str, transport: httpx.AsyncBaseTransport | None = None
) -> httpx.AsyncClient:
    """An httpx client for one API base, with no key and no cookie of its own."""
    return httpx.AsyncClient(
        base_url=api_base,
        headers={
            "User-Agent": f"sugra-api-mcp/{_pkg_version()}",
            "Accept": "application/json",
        },
        cookies=_no_cookies(),
        limits=POOL_LIMITS,
        transport=transport,
        # Only when httpx would build a context of its own: a supplied
        # transport carries its own, and asking for the shared one there
        # would build a real TLS context for a mock-transport test.
        **({} if transport is not None else {"verify": shared_ssl_context()}),
    )


def shared_pool(api_base: str) -> httpx.AsyncClient:
    """The connection pool every caller of this API base sends through on
    the running event loop.

    The connections of an `httpx.AsyncClient` belong to the event loop that
    opened them and cannot be used from another, so each loop has pools of
    its own. The served process runs one loop, which every caller shares, so
    there each API base has one pool. A loop in another thread, or the next
    one after `asyncio.run` returns, is handed a new pool, never the
    connections a loop that has ended left open.

    Built on the first request, and kept until `close_shared_pools` closes
    it; a request after that builds a new one. A build also forgets the pools
    of every loop that has closed since. The lock guards only the registry,
    whose lookup and build never wait.
    """
    key = (asyncio.get_running_loop(), api_base)
    pool = _pools.get(key)
    if pool is None or pool.is_closed:
        with _pools_lock:
            pool = _pools.get(key)
            if pool is None or pool.is_closed:
                _forget_closed_loops()
                pool = _new_http_client(api_base)
                _pools[key] = pool
    return pool


def _forget_closed_loops() -> None:
    """Forget the pools of every event loop that has closed; the caller
    holds the lock.

    Such a pool can no longer be closed, since closing it needs its loop:
    forgetting it lets its connections be freed with it.
    """
    for key in [key for key in _pools if key[0].is_closed()]:
        del _pools[key]


async def close_shared_pools() -> None:
    """Close the shared pools of the running event loop, and forget them.

    The pools of loops that have closed are forgotten as well. A pool of
    another loop that still runs is closed by that loop, with a call of its
    own: only its own loop can close it. The served process runs one loop,
    so there one call closes every pool.

    Each pool is closed even when another fails to close, and the first
    failure is raised after the rest. A second call finds nothing left to
    close.
    """
    loop = asyncio.get_running_loop()
    with _pools_lock:
        pools = [_pools.pop(key) for key in [key for key in _pools if key[0] is loop]]
        _forget_closed_loops()
    first_failure: Exception | None = None
    for pool in pools:
        try:
            await pool.aclose()
        except Exception as e:
            first_failure = first_failure or e
    if first_failure is not None:
        raise first_failure


def _pkg_version() -> str:
    return __version__


def _elapsed_ms(start: float) -> int:
    return int((time.perf_counter() - start) * 1000)


def _retry_after(response: httpx.Response) -> int | str | None:
    """Parse the Retry-After header: delta-seconds -> int, anything else -> raw string.

    Never raises. str.isdigit() alone is NOT a safe gate for int(): it accepts
    unicode digit characters (e.g. superscript two) that int() rejects with
    ValueError, and this helper runs outside the transport try/except - a
    malformed header from a proxy must not break the error path. Hence the
    additional isascii() check.
    """
    raw = str(response.headers.get("Retry-After", "")).strip()
    if not raw:
        return None
    if raw.isascii() and raw.isdigit():
        return int(raw)
    return raw


# When an account's daily quota is spent the API answers 429 with a
# FastAPI `detail` that starts with this text, names the plan, and ends with
# an upgrade link into the billing cabinet. That wording is written for a
# developer's REST client. A model inside a chat app gets the same facts as
# plain information instead: the limit, the plan and when it resets, with no
# upgrade wording and no link at all, since even the public plans page offers
# subscriptions.
_DAILY_LIMIT_PREFIX = "Daily limit of "
_PLAN_IN_DETAIL = re.compile(r"Current plan: ([A-Za-z0-9_-]+)\.")

# A `detail` forwarded to the model never sells: one that still asks for an
# upgrade or links into the app is replaced by the bare status, which is
# all this client forwarded before it read `detail` at all.
_SALES_TEXT = re.compile(r"\bupgrade\b|app\.sugra\.ai", re.IGNORECASE)


def _detail_text(payload: Any) -> str | None:
    """The API's own explanation of a failure, or None.

    FastAPI puts an HTTPException's text at `detail`. An object or a
    validation list there is not a sentence: `_detail_fields` carries it.
    """
    if isinstance(payload, dict) and isinstance(payload.get("detail"), str):
        return payload["detail"].strip() or None
    return None


# The most characters (response_chars) of the fields `_detail_fields` returns,
# measured as one object. The largest object detail the API sends today, the
# allowed list of index sub-industries, measures about 3,800.
_DETAIL_CHARS = 8_000


def _detail_fields(payload: Any) -> dict[str, Any]:
    """The fields that carry an API `detail` that is not a sentence, or {}.

    An object names the failure in its own keys, such as {"error":
    "invalid_window", "allowed": [...], "received": "2w"}: its `error`
    becomes the error text and the object itself is carried at `detail`. A
    FastAPI validation list is carried as where each failure is and what it
    says, without the value the caller sent. What does not fit _DETAIL_CHARS
    is left out, and `detail_truncated` says so. A detail with selling text
    anywhere in it, in a part that is not carried too, is left out whole, as
    a selling sentence is.
    """
    detail = payload.get("detail") if isinstance(payload, dict) else None
    if isinstance(detail, dict):
        return _object_detail(detail)
    if isinstance(detail, list):
        return _validation_detail(detail)
    return {}


def _object_detail(detail: dict[str, Any]) -> dict[str, Any]:
    if not detail or _sells(detail):
        return {}
    code = detail.get("error")
    named = {"error": code.strip()} if isinstance(code, str) and code.strip() else {}
    if _fits({**named, "detail": detail}, _DETAIL_CHARS):
        return {**named, "detail": detail}
    if _fits({**named, "detail_truncated": True}, _DETAIL_CHARS):
        return {**named, "detail_truncated": True}
    return {"detail_truncated": True}


def _validation_detail(detail: list[Any]) -> dict[str, Any]:
    if _sells(detail):
        return {}
    items: list[dict[str, str]] = []
    for failure in detail:
        if not isinstance(failure, dict) or not isinstance(failure.get("msg"), str):
            continue
        loc = failure.get("loc")
        # FastAPI's loc is names and list indexes; any other part is no place,
        # a bool included, though Python counts it an int.
        if isinstance(loc, list) and loc and all(
            isinstance(part, str) or (isinstance(part, int) and not isinstance(part, bool))
            for part in loc
        ):
            items.append({"loc": ".".join(str(part) for part in loc), "msg": failure["msg"]})
        else:
            items.append({"msg": failure["msg"]})
    if not items:
        return {}
    if _fits({"detail": items}, _DETAIL_CHARS):
        return {"detail": items}
    kept: list[dict[str, str]] = []
    # What the items have left once the fields' own keys and brackets are
    # counted; each item after the first also carries its ", " separator.
    room = _DETAIL_CHARS - response_chars({"detail": [], "detail_truncated": True})
    for item in items:
        size = _chars(item, room - (2 if kept else 0))
        if size is None:
            break
        room -= size + (2 if kept else 0)
        kept.append(item)
    # The whole list did not fit, so at least one item is always left out.
    return {"detail": kept, "detail_truncated": True} if kept else {"detail_truncated": True}


def _chars(value: Any, budget: int) -> int | None:
    """response_chars_within, with a value too deeply nested to measure
    counted as over the budget."""
    if budget < 0:
        return None
    try:
        return response_chars_within(value, budget)
    except RecursionError:
        return None


def _fits(value: Any, budget: int) -> bool:
    return _chars(value, budget) is not None


def _sells(value: Any) -> bool:
    """Whether any key or string in value matches _SALES_TEXT.

    Walked with a stack, not by recursion, so no nesting depth can raise.
    """
    stack = [value]
    while stack:
        item = stack.pop()
        if isinstance(item, str):
            if _SALES_TEXT.search(item):
                return True
        elif isinstance(item, dict):
            stack.extend(item.keys())
            stack.extend(item.values())
        elif isinstance(item, list):
            stack.extend(item)
    return False


def _positive_int_header(response: httpx.Response, name: str) -> int | None:
    raw = str(response.headers.get(name, "")).strip()
    if raw.isascii() and raw.isdigit() and int(raw) > 0:
        return int(raw)
    return None


def _daily_limit_error(detail: str, response: httpx.Response) -> dict[str, Any]:
    """The quota refusal, stated as information for a model to relay."""
    limit = _positive_int_header(response, "X-RateLimit-Limit")
    match = _PLAN_IN_DETAIL.search(detail)
    plan = match.group(1) if match and match.group(1) != "unknown" else None
    reached = "The daily request limit"
    if limit is not None:
        reached += f" of {limit}"
    reached += " for this Sugra account"
    if plan is not None:
        reached += f" on the {plan} plan"
    fields: dict[str, Any] = {
        "error": f"{reached} has been reached. It resets at 00:00 UTC.",
        "reason": "daily_limit_reached",
    }
    if limit is not None:
        fields["daily_limit"] = limit
    if plan is not None:
        fields["plan"] = plan
    return fields


def _unshaped_records(unshaped: Any) -> list[Any] | None:
    """The records list of a response as the API sent it: its ``data`` list
    or a bare top-level array. None for any other shape."""
    if isinstance(unshaped, list):
        return unshaped
    if isinstance(unshaped, dict) and isinstance(unshaped.get("data"), list):
        return unshaped["data"]
    return None


def response_chars(value: Any) -> int:
    """The size the cap measures: the length of ``json.dumps`` with its
    defaults, ASCII escapes and the default separators. A non-ASCII
    character counts as its escape, six characters, which keeps the cap
    inside the token limit for CJK text too."""
    return len(json.dumps(value))


class _OverBudget(Exception):
    """A bounded measure passed its budget."""


# The C escape json.dumps itself uses for every string.
_encode_string = json.encoder.encode_basestring_ascii


def response_chars_within(value: Any, budget: int) -> int | None:
    """``response_chars(value)`` when it is at most ``budget``, else None.

    The value is walked in order and the walk stops as soon as the count
    passes the budget, so the work is bounded by the budget, never by the
    size of the value: a string longer than the room left is counted over
    without being escaped. The count is exactly the one ``response_chars``
    gives.
    """
    try:
        return _chars_within(value, budget)
    except _OverBudget:
        return None


def _float_text(value: float) -> str:
    if value != value:
        return "NaN"
    if value == float("inf"):
        return "Infinity"
    if value == float("-inf"):
        return "-Infinity"
    return float.__repr__(value)


def _key_chars(key: Any, room: int) -> int:
    if isinstance(key, str):
        if len(key) + 2 > room:
            raise _OverBudget
        return len(_encode_string(key))
    if isinstance(key, float):
        return len(_float_text(key)) + 2
    if key is True or key is None:
        return 6
    if key is False:
        return 7
    if isinstance(key, int):
        return len(int.__repr__(key)) + 2
    # The error json.dumps raises for such a key.
    return len(json.dumps({key: None}))


def _chars_within(value: Any, room: int) -> int:
    """The measure of value, raising _OverBudget once it passes room."""
    if isinstance(value, str):
        if len(value) + 2 > room:
            raise _OverBudget
        size = len(_encode_string(value))
    elif value is None or value is True:
        size = 4
    elif value is False:
        size = 5
    elif isinstance(value, int):
        size = len(int.__repr__(value))
    elif isinstance(value, float):
        size = len(_float_text(value))
    elif isinstance(value, (list, tuple)):
        size = 2
        for index, item in enumerate(value):
            if index:
                size += 2
            if size > room:
                raise _OverBudget
            size += _chars_within(item, room - size)
    elif isinstance(value, dict):
        size = 2
        for index, (key, item) in enumerate(value.items()):
            if index:
                size += 2
            size += _key_chars(key, room - size) + 2
            if size > room:
                raise _OverBudget
            size += _chars_within(item, room - size)
    else:
        # The error json.dumps raises for what it cannot encode.
        size = len(json.dumps(value))
    if size > room:
        raise _OverBudget
    return size


def _on_event_loop() -> bool:
    """Whether this thread runs an event loop: the shaping pool's threads do
    not, the thread serving the tools does."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return False
    return True


def _enforce_size_limit(
    payload: Any, url: str, *, unshaped: Any = None, endpoint: Any = None
) -> Any:
    """Return payload whole when it fits MAX_RESPONSE_CHARS, else cut it to
    fit, else the ``response_too_large`` refusal.

    The fit is measured by ``response_chars_within``, which stops at the
    cap, so a response far over it is never serialised whole here. The cut
    (``catalog.size_cut.cut_to_fit``) takes any shape: every list at most
    two levels under ``data``, a big list cut before small ones, each
    keeping the end ``limit`` keeps (``catalog.response._limit_records``),
    the newest when the order of the records can be read, the first records
    otherwise. ``meta.truncated`` reports it. The cut runs only off the
    event loop, on the shaping pool: ``SugraClient.request`` and
    call_endpoint send an oversized response there. On the event loop,
    where only the error payloads call_endpoint returns as they came are
    gated, a response over the cap is refused without being walked.

    ``unshaped`` is the same response before shaping cut or projected it,
    passed by a caller that shaped it (call_endpoint). The order of a list is
    then read from that response instead: a cut or a projection keeps the
    records' order but may drop the date key it is read from, and it is the
    very read ``meta.shaped`` reports, so the two agree on the end kept.

    ``endpoint`` is the catalog entry of the operation called, passed by
    call_endpoint only, also when fetch_data runs an operation through it:
    the hints name its parameters and that tool's own arguments, and its
    operation id selects the nearest rule. The fixed tools pass None, so
    their hints name no parameter.
    """
    if response_chars_within(payload, MAX_RESPONSE_CHARS) is not None:
        return payload
    # Imported here, not at the top: the catalog package imports this module.
    from .catalog.size_cut import cut_to_fit, refuse_unmeasured

    if _on_event_loop():
        return refuse_unmeasured(url, MAX_RESPONSE_CHARS)
    return cut_to_fit(
        payload, url, cap=MAX_RESPONSE_CHARS, unshaped=unshaped, endpoint=endpoint
    )


# How long past the pool's wait in line (SHAPING_WAIT_SECONDS) the client
# waits for a job to start, and past the job's own clocks
# (catalog.response.MAX_SHAPING_SECONDS each) for it to finish, before it
# answers without it.
CUT_GRACE_SECONDS = 0.5


class _StartGate:
    """The one decision between a worker starting a job and the job's caller
    giving up on the start, taken under a lock: whichever comes first wins,
    so a job is either started or never runs."""

    __slots__ = ("_lock", "abandoned", "started_at")

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.abandoned = False
        self.started_at: float | None = None

    def enter(self) -> bool:
        """On the worker, before the job: True, with the start time noted,
        unless the caller has given up first."""
        with self._lock:
            if self.abandoned:
                return False
            self.started_at = time.monotonic()
            return True

    def abandon(self) -> float | None:
        """On the caller: None when the job had not started, and now never
        will, else the time it started."""
        with self._lock:
            if self.started_at is None:
                self.abandoned = True
            return self.started_at


async def _gate_off_loop(payload: Any, url: str) -> Any:
    """The size gate for a response the client returns as it came, a success
    or a failure: measured on the event loop only up to the cap, and when
    over it measured and cut on the shaping pool, as call_endpoint's are; a
    full pool answers server_busy.

    The wait is bounded, see _cut_on_pool.
    """
    if response_chars_within(payload, MAX_RESPONSE_CHARS) is not None:
        return payload
    return await _cut_on_pool(partial(_enforce_size_limit, payload, url), url)


async def _cut_on_pool(call: Callable[[], Any], url: str, *, clocks: int = 1) -> Any:
    """Run a size-gating job on the shaping pool and wait for it within a
    bound; server_busy when the pool is full.

    The client first waits SHAPING_WAIT_SECONDS plus CUT_GRACE_SECONDS for
    the job's answer; a job in the pool's line answers server_busy itself
    at SHAPING_WAIT_SECONDS. Past that wait the client asks the job's
    _StartGate whether the job has started on a worker. Not started: the
    gate keeps it from ever running the call, and the client answers
    server_busy. Started: the client waits until ``clocks`` times
    MAX_SHAPING_SECONDS plus CUT_GRACE_SECONDS after the start and then
    answers ``response_too_large`` with the size message alone. ``clocks``
    counts the clocks the job runs one after the other: one for a cut
    alone, two for call_endpoint, whose fields projection runs on its own
    MAX_SHAPING_SECONDS clock before the cut starts its own. So the client
    waits at most the longer of the first wait and that run bound after
    the start; with the shipped numbers (2.5 s, then 5.5 s or 10.5 s from
    the start) the run bound is always the longer.

    The clocks are cooperative, read between records, so a single record
    or key can hold a worker past them; the bound here is what holds for
    the caller whatever the job does. What happens to the job when the
    client stops waiting is the pool's (tools.gateway._shape_off_loop): a
    job still queued is cancelled with its waiting task and frees its slot,
    and one already running finishes in the background, its result
    dropped, holding its slot until its future is done. The pool's
    SHAPING_WORKERS threads are the most that run at once.
    """
    # Imported here, not at the top: the gateway module and the catalog
    # package import this one.
    from .catalog import response as shaping
    from .catalog.size_cut import refuse_unmeasured
    from .errors import server_busy_error
    from .tools import gateway as pool

    gate = _StartGate()

    def job() -> Any:
        if not gate.enter():
            # Its caller has answered server_busy: the call never runs.
            return None
        return call()

    begun = time.monotonic()
    run_bound = clocks * shaping.MAX_SHAPING_SECONDS + CUT_GRACE_SECONDS
    task = asyncio.ensure_future(pool._shape_off_loop(job))
    try:
        await asyncio.wait((task,), timeout=pool.SHAPING_WAIT_SECONDS + CUT_GRACE_SECONDS)
        if not task.done():
            started_at = gate.abandon()
            if started_at is None:
                task.cancel()
                return server_busy_error(
                    "shaping", pool.SHAPING_WORKERS, elapsed_ms=int((time.monotonic() - begun) * 1000)
                )
            await asyncio.wait((task,), timeout=max(0.0, started_at + run_bound - time.monotonic()))
    except BaseException:
        task.cancel()
        raise
    if task.done():
        return task.result()
    # Cancelling the wait on a running job only drops its result.
    task.cancel()
    return refuse_unmeasured(url, MAX_RESPONSE_CHARS)


class SugraClient:
    """Thin async wrapper over the Sugra API with x-api-key auth.

    Light enough to build for every call: it holds this caller's key and
    sends each request through the shared pool of its API base on the running
    event loop, with the key as that request's own x-api-key header. A client
    given a transport of its own sends through a client of its own on that
    transport instead, which its aclose closes.
    """

    def __init__(self, config: Config, *, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self._config = config
        self._key_header = httpx.Headers({"x-api-key": config.api_key})
        self._own_http = None if transport is None else _new_http_client(config.api_base, transport)

    def _http(self) -> httpx.AsyncClient:
        """The httpx client this client's next request goes out on."""
        if self._own_http is not None:
            return self._own_http
        return shared_pool(self._config.api_base)

    async def get(
        self,
        path: str,
        params: dict[str, Any] | None = None,
        *,
        enforce_size: bool = True,
    ) -> dict[str, Any]:
        return await self.request("GET", path, params=params, enforce_size=enforce_size)

    async def post(
        self,
        path: str,
        json: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        *,
        enforce_size: bool = True,
    ) -> dict[str, Any]:
        return await self.request(
            "POST", path, json=json, headers=headers, enforce_size=enforce_size
        )

    async def request(
        self,
        method: str,
        path: str,
        params: dict[str, Any] | None = None,
        json: dict[str, Any] | list[Any] | None = None,
        headers: dict[str, str] | None = None,
        *,
        enforce_size: bool = True,
    ) -> dict[str, Any]:
        clean_params = {k: v for k, v in (params or {}).items() if v is not None}
        # The key rides on this request alone, and it is always this client's
        # own: extras such as X-Internal-Token for the agent plane ride
        # alongside it, and a key header among them, in any case, is replaced
        # by it. httpx then merges the result over the pool's User-Agent and
        # Accept.
        request_headers = httpx.Headers(headers)
        request_headers.update(self._key_header)
        http_client = self._http()
        start = time.perf_counter()
        try:
            response = await http_client.request(
                method.upper(),
                path,
                params=clean_params,
                json=json if json is not None else None,
                headers=request_headers,
                # The configured timeout (SUGRA_TIMEOUT), for the connect, each
                # read and write, and the wait for a free connection.
                timeout=self._config.timeout,
            )
        # Order matters: ConnectTimeout subclasses TimeoutException (NOT
        # ConnectError), so all timeout flavors land in upstream_timeout.
        except httpx.TimeoutException as exc:
            return self._transport_error(
                "upstream_timeout",
                exc,
                start,
                path,
                retry_hint=(
                    f"No response within the gateway's configured {self._config.timeout:g}s "
                    "upstream timeout (SUGRA_TIMEOUT). A single retry often succeeds (the "
                    "aborted attempt usually completes server-side and warms upstream "
                    "caches). Otherwise narrow the request: smaller batch, fewer items, "
                    "tighter filters."
                ),
                extra={"timeout_s": self._config.timeout},
            )
        except httpx.ConnectError as exc:
            return self._transport_error(
                "upstream_connect_error",
                exc,
                start,
                path,
                retry_hint="Could not connect to the Sugra API. Retry after a short delay.",
            )
        except httpx.HTTPError as exc:
            return self._transport_error(
                "upstream_transport_error",
                exc,
                start,
                path,
                retry_hint=(
                    "Transient transport failure (connection dropped mid-request). "
                    "Retry once; if it persists, report the reason field."
                ),
            )
        result = self._handle(response, elapsed_ms=_elapsed_ms(start))
        # Enforced HERE by default - the backstop every caller gets for free.
        # A caller that shapes its own response after the fact
        # (gateway.call_endpoint) passes enforce_size=False and gates it
        # itself once its own `fields` / `limit` projection has run, so the
        # raw, unprojected body is never what gets measured for it. A failure
        # dict is gated the same way: it carries the API's error text, which
        # has no bound of its own.
        if not enforce_size:
            return result
        return await _gate_off_loop(result, str(response.request.url))

    def _transport_error(
        self,
        code: str,
        exc: httpx.HTTPError,
        start: float,
        path: str,
        *,
        retry_hint: str,
        extra: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Build the structured error dict for a transport-layer failure."""
        message = str(exc).strip()
        reason = f"{type(exc).__name__}: {message}" if message else type(exc).__name__
        result: dict[str, Any] = {
            "error": code,
            "reason": reason,
            "status_code": None,
            "elapsed_ms": _elapsed_ms(start),
            "url": self._request_url(exc, path),
            "retry_hint": retry_hint,
        }
        if extra:
            result.update(extra)
        return result

    def _request_url(self, exc: httpx.HTTPError, path: str) -> str:
        try:
            return str(exc.request.url)
        except RuntimeError:
            # httpx raises RuntimeError when the exception carries no request.
            return f"{self._config.api_base}{path}"

    @staticmethod
    def _handle(response: httpx.Response, *, elapsed_ms: int) -> dict[str, Any]:
        try:
            payload = response.json()
        except ValueError:
            payload = {"error": response.text[:500]}
        # Any non-2xx answer is an HTTP failure. The client never follows
        # redirects and a 3xx carries no JSON body, so under the old `>= 400`
        # gate it fell through to the success path, where the failed decode
        # above had produced {"error": ""} - a failure with no reason, no
        # status and no url, which telemetry could only file as unknown.
        if response.status_code >= 300:
            error = payload.get("error") if isinstance(payload, dict) else str(payload)
            quota: dict[str, Any] = {}
            # An `error` key wins whatever its value, as it did before `detail`
            # was read at all.
            has_error_key = isinstance(payload, dict) and "error" in payload
            detail = None if has_error_key else _detail_text(payload)
            carried: dict[str, Any] = {} if has_error_key else _detail_fields(payload)
            if detail is not None:
                if response.status_code == 429 and detail.startswith(_DAILY_LIMIT_PREFIX):
                    quota = _daily_limit_error(detail, response)
                elif not _SALES_TEXT.search(detail):
                    error = detail
            retry_after = _retry_after(response)
            if quota:
                # A spent quota is not a fault to trace: the url, the timing and
                # the request id give a model nothing to act on, only the limit,
                # the plan and when it resets.
                quota["status_code"] = response.status_code
                if retry_after is not None:
                    quota["retry_after"] = retry_after
                return quota
            error = carried.pop("error", error)
            result: dict[str, Any] = {
                "error": error or f"HTTP {response.status_code}",
                "status_code": response.status_code,
                "url": str(response.request.url),
                "elapsed_ms": elapsed_ms,
                **carried,
            }
            if retry_after is not None:
                result["retry_after"] = retry_after
            # The API stamps every response with a request id; carrying it on the
            # failure is what lets a user quote one line and have the exact call
            # found in the logs, instead of describing what they think happened.
            request_id = response.headers.get("X-Request-ID")
            if request_id:
                result["request_id"] = str(request_id)
            return result
        return payload

    async def aclose(self) -> None:
        """Close the client built on a transport of this client's own.

        The shared pools are not this client's to close: `close_shared_pools`
        closes them, once the server has stopped.
        """
        if self._own_http is not None:
            await self._own_http.aclose()
