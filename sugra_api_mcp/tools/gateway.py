"""Gateway MCP tools backed by the bundled endpoint catalog."""

from __future__ import annotations

import asyncio
import difflib
import threading
import time
from collections import deque
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from functools import partial
from typing import Annotated, Any

from pydantic import Field

from ..catalog.hints import hints_for
from ..catalog.loader import load_catalog
from ..catalog.response import shape_response
from ..catalog.search import known_sources, known_toolsets, query_limit_error, search_catalog
from ..catalog.toolsets import ordered_toolsets
from ..client import _enforce_size_limit
from ..errors import is_error_payload, server_busy_error
from ..observability import trace_mcp_tool
from ..server import current_caller, get_client, mcp, read_only

# Catalog search is pure CPU work, so it runs on one worker thread
# instead of the event loop that serves every session. Two bounds admit a search
# to that worker: SEARCH_MAX_PENDING searches running or queued in total, and
# SEARCH_MAX_PENDING_PER_CALLER of them for any one caller (server.current_caller:
# one API key, so every session of one account is one caller), so a single
# credential cannot hold the whole queue. A search that finds a bound reached
# waits in line, and each slot given back goes at once to the oldest waiting
# search it admits; a search whose caller is still at its own bound keeps its
# place without holding up other callers. The release submits that search to the
# worker itself, so its slot comes back through the worker even when the waiting
# call or its event loop is gone by then. A search that has waited
# SEARCH_WAIT_SECONDS without a slot answers server_busy. At most
# SEARCH_MAX_WAITING searches wait in total and SEARCH_MAX_WAITING_PER_CALLER for
# one caller, and a search past either answers server_busy at once. A search
# holds one tool-call slot of server.py (MAX_IN_FLIGHT_PER_CALLER 16,
# MAX_IN_FLIGHT_TOOL_CALLS 32) for as long as it is queued, running or waiting,
# so one caller's searches hold at most 8 of its 16 slots and all searches
# together at most 16 of the 32.
#
# The wait has to cover the first search after a quiet spell. In ten days of
# hosted telemetry, a search after an hour or more without one took p50 329 ms,
# p90 4.07 s and at most 10.94 s, against a p90 of at most 175 ms for searches
# under a minute apart; 12 s covers that maximum. Twice one client sent six
# searches at once: the four admitted took 2.2-5.2 s, and the two behind them
# were refused at the former 2 s wait.
SEARCH_MAX_PENDING = 8
SEARCH_MAX_PENDING_PER_CALLER = 4
SEARCH_MAX_WAITING = 8
SEARCH_MAX_WAITING_PER_CALLER = 4
SEARCH_WAIT_SECONDS = 12.0
_search_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="catalog-search")
_search_lock = threading.Lock()
_search_pending = 0
_search_pending_by_caller: dict[str, int] = {}


class _SearchWaiter:
    """A search waiting in line for a slot, woken on its own event loop.

    `state` is "waiting" while it is in the line, "handed" once a release has
    taken a slot for it and submitted `call` to the worker as `future`, and
    "gone" once it has left the line without a slot or stopped waiting after
    one. `state` and `future` are read and written under _search_lock only;
    code outside the lock uses the future it took under the lock. No
    release hands a slot to a search at or past its `deadline` (time.monotonic),
    and `scope` names the bound that kept it out when it joined the line.
    """

    __slots__ = ("call", "caller", "deadline", "future", "loop", "scope", "state", "woken")

    def __init__(
        self, caller: str, call: Callable[[], Any], loop: asyncio.AbstractEventLoop, deadline: float, scope: str
    ) -> None:
        self.caller = caller
        self.call = call
        self.loop = loop
        self.deadline = deadline
        self.scope = scope
        self.woken: asyncio.Future[None] = loop.create_future()
        self.future: Future[Any] | None = None
        self.state = "waiting"


_search_waiters: deque[_SearchWaiter] = deque()
_search_waiting_by_caller: dict[str, int] = {}


def search_pending(caller: str | None = None) -> int:
    """Searches submitted to the worker that have not finished or been dropped.

    Given a caller, only that caller's searches are counted.
    """
    with _search_lock:
        if caller is None:
            return _search_pending
        return _search_pending_by_caller.get(caller, 0)


def search_waiting(caller: str | None = None) -> int:
    """Searches waiting in line for a slot; given a caller, only that caller's."""
    with _search_lock:
        if caller is None:
            return len(_search_waiters)
        return _search_waiting_by_caller.get(caller, 0)


def _full_bound(caller: str) -> str | None:
    """The scope of the bound that keeps caller out, or None. Hold _search_lock."""
    if _search_pending >= SEARCH_MAX_PENDING:
        return "search"
    if _search_pending_by_caller.get(caller, 0) >= SEARCH_MAX_PENDING_PER_CALLER:
        return "caller_search"
    return None


def _take_slot(caller: str) -> None:
    """Count one search in for caller. Hold _search_lock."""
    global _search_pending
    _search_pending += 1
    _search_pending_by_caller[caller] = _search_pending_by_caller.get(caller, 0) + 1


def _drop_slot(caller: str) -> None:
    """Count one search out for caller. Hold _search_lock."""
    global _search_pending
    _search_pending -= 1
    held = _search_pending_by_caller.get(caller, 0) - 1
    if held > 0:
        _search_pending_by_caller[caller] = held
    else:
        _search_pending_by_caller.pop(caller, None)


def _leave_line(waiter: _SearchWaiter) -> None:
    """Take waiter out of the line. Hold _search_lock."""
    _search_waiters.remove(waiter)
    held = _search_waiting_by_caller.get(waiter.caller, 0) - 1
    if held > 0:
        _search_waiting_by_caller[waiter.caller] = held
    else:
        _search_waiting_by_caller.pop(waiter.caller, None)


def _claim_search_slot(caller: str) -> str | None:
    """Claim a slot for caller: None when claimed, else the scope of the bound that is full."""
    with _search_lock:
        full = _full_bound(caller)
        if full is None:
            _take_slot(caller)
        return full


def _hand_off() -> list[tuple[_SearchWaiter, Future[Any]]]:
    """Hand free slots to the oldest waiting searches they admit, submitting each
    one's search to the worker, and return those searches with their futures, to
    be started once the lock is let go. Hold _search_lock.

    The slot is tied to the submitted search from here on, not to the waiting
    call, so it comes back through the worker whatever happens to that call or
    its event loop. A waiting search at or past its deadline, or whose event
    loop has closed, leaves the line here: its wait is over.
    """
    now = time.monotonic()
    granted: list[tuple[_SearchWaiter, Future[Any]]] = []
    for waiter in list(_search_waiters):
        if now >= waiter.deadline or waiter.loop.is_closed():
            _leave_line(waiter)
            waiter.state = "gone"
            continue
        if _full_bound(waiter.caller) is not None:
            continue
        try:
            future = _search_executor.submit(waiter.call)
        except RuntimeError:
            # The worker is shut down (interpreter exit); the waiting searches time out.
            break
        _leave_line(waiter)
        _take_slot(waiter.caller)
        waiter.future = future
        waiter.state = "handed"
        granted.append((waiter, future))
    return granted


def _start_handed(granted: list[tuple[_SearchWaiter, Future[Any]]]) -> None:
    """Tie each handed search's slot to its worker future and wake the search on
    its own event loop. Outside _search_lock: a future already done runs the
    callback, and so the release, at once."""
    for waiter, future in granted:
        future.add_done_callback(lambda _future, caller=waiter.caller: _release_search_slot(caller))
        try:
            waiter.loop.call_soon_threadsafe(_wake, waiter.woken)
        except RuntimeError:
            # Its event loop closed after the hand-off: nobody is left to read the answer.
            future.cancel()


def _release_search_slot(caller: str) -> None:
    """Give caller's slot back and hand it to the oldest waiting search it admits.

    Runs on the search worker thread (the future's done callback) as well as on
    an event loop thread, so the waiting search is woken through its own loop.
    """
    with _search_lock:
        _drop_slot(caller)
        granted = _hand_off()
    _start_handed(granted)


def _wake(woken: asyncio.Future[None]) -> None:
    if not woken.done():
        woken.set_result(None)


def _leave_waiting(waiter: _SearchWaiter) -> None:
    """A waiting search that stops waiting leaves the line, or cancels the search
    a release already submitted for it, as a queued search is cancelled with its
    caller."""
    with _search_lock:
        if waiter.state == "waiting":
            _leave_line(waiter)
        state, future, waiter.state = waiter.state, waiter.future, "gone"
    if state == "handed":
        future.cancel()


def _admit_search(
    caller: str, started: float, call: Callable[[], Any]
) -> _SearchWaiter | dict[str, Any] | None:
    """None when caller's search is admitted, a waiter when it waits in line,
    else the server_busy payload of the full waiting bound.

    The line is served first, so a new search never takes a slot that a search
    already waiting could take, and never counts a place a search whose wait is
    over still held.
    """
    with _search_lock:
        granted = _hand_off()
        admitted: _SearchWaiter | dict[str, Any] | None = None
        full = _full_bound(caller)
        if full is None:
            _take_slot(caller)
        elif len(_search_waiters) >= SEARCH_MAX_WAITING:
            admitted = server_busy_error("search", SEARCH_MAX_WAITING)
        elif _search_waiting_by_caller.get(caller, 0) >= SEARCH_MAX_WAITING_PER_CALLER:
            admitted = server_busy_error("caller_search", SEARCH_MAX_WAITING_PER_CALLER)
        else:
            admitted = _SearchWaiter(caller, call, asyncio.get_running_loop(), started + SEARCH_WAIT_SECONDS, full)
            _search_waiters.append(admitted)
            _search_waiting_by_caller[caller] = _search_waiting_by_caller.get(caller, 0) + 1
    _start_handed(granted)
    return admitted


async def _wait_for_slot(waiter: _SearchWaiter, started: float) -> Future[Any] | dict[str, Any]:
    """Wait until a release hands waiter a slot: the future of the search that
    release submitted, else the server_busy payload once the deadline has passed
    without one."""
    try:
        await asyncio.wait((waiter.woken,), timeout=max(0.0, waiter.deadline - time.monotonic()))
    except BaseException:
        _leave_waiting(waiter)
        raise
    with _search_lock:
        if waiter.state == "handed":
            return waiter.future
        if waiter.state == "waiting":
            _leave_line(waiter)
            waiter.state = "gone"
        full = _full_bound(waiter.caller) or waiter.scope
    limit = SEARCH_MAX_PENDING if full == "search" else SEARCH_MAX_PENDING_PER_CALLER
    return server_busy_error(full, limit, elapsed_ms=int((time.monotonic() - started) * 1000))


async def _search_off_loop(catalog: Any, query: str, **kwargs: Any) -> list[dict[str, Any]] | dict[str, Any]:
    """Run search_catalog on the search worker, or return the server_busy payload.

    The future's done callback releases the slot. A search the worker has already
    started runs to completion and holds its slot until then, even when its
    caller was cancelled meanwhile. A search still queued when its caller is
    cancelled is cancelled with it: it never runs, and its slot is freed at once.
    A search cancelled while it waits in line leaves the line; one cancelled after
    a release handed it a slot is cancelled like a queued search, since that
    release has already submitted it.
    """
    caller = current_caller()
    started = time.monotonic()
    call = partial(search_catalog, catalog, query, **kwargs)
    admitted = _admit_search(caller, started, call)
    if isinstance(admitted, dict):
        return admitted
    if admitted is not None:
        handed = await _wait_for_slot(admitted, started)
        if isinstance(handed, dict):
            return handed
        return await asyncio.wrap_future(handed)
    try:
        future = _search_executor.submit(call)
    except BaseException:
        _release_search_slot(caller)
        raise
    future.add_done_callback(lambda _future: _release_search_slot(caller))
    return await asyncio.wrap_future(future)


def _resolve_path(path: str, params: dict[str, Any]) -> str:
    resolved = path
    for name, value in params.items():
        resolved = resolved.replace(f"{{{name}}}", str(value))
    return resolved


def _group_violation(endpoint, params: dict[str, Any]) -> str | None:
    """Group-contract verdict BEFORE any HTTP call.

    Returns "uncovered" when NO declared group is fully covered, and
    "multiple" when the endpoint declares its groups mutually exclusive
    and the params complete MORE than one - both would be upstream 4xxs,
    so the gateway refuses with the groups spelled out. None = dispatch.
    """
    groups = getattr(endpoint, "required_groups", ()) or ()
    if not groups:
        return None
    covered = sum(1 for group in groups if all(name in params for name in group))
    if covered == 0:
        return "uncovered"
    if getattr(endpoint, "groups_mutually_exclusive", False):
        # Exclusivity judges ACTIVE groups (any member supplied), not just
        # complete ones: a complete group mixed with a stray member of a
        # competing group is still a mixed-mode request the upstream will
        # reject.
        active = sum(1 for group in groups if any(name in params for name in group))
        if active > 1:
            return "multiple"
    return None


# Operations whose API handler reads filters from the raw query string beyond
# the parameters it declares, so an undeclared key is a real filter there and
# not a typo. The StatBank DK data endpoint takes the table's own dimension
# codes (OMRÅDE, KØN, Tid, ...) this way. Every other operation ignores an
# undeclared key without an error, which is why the gateway refuses one.
_OPEN_QUERY_OPERATIONS: frozenset[str] = frozenset({
    "statistical_agencies_statbank_dk_data_table_id",
})


def _unknown_params_error(
    operation_id: str, endpoint, params: dict[str, Any]
) -> dict[str, Any] | None:
    """Refuse keys the operation does not declare, BEFORE any HTTP call.

    The API drops an undeclared query parameter silently, so a misnamed
    filter (country for countries) returned the endpoint's default data
    with no sign that the filter was never applied.
    """
    if operation_id in _OPEN_QUERY_OPERATIONS:
        return None
    accepted = [parameter.name for parameter in endpoint.parameters]
    unknown = [key for key in params if key not in accepted]
    if not unknown:
        return None
    payload: dict[str, Any] = {
        "error": "unknown_parameters",
        "operation_id": operation_id,
        "unknown": unknown,
        "accepted": accepted,
    }
    by_lower = {name.lower(): name for name in accepted}
    did_you_mean: dict[str, str] = {}
    for key in unknown:
        match = by_lower.get(key.lower()) or next(
            iter(difflib.get_close_matches(key, accepted, n=1, cutoff=0.6)), None)
        if match:
            did_you_mean[key] = match
    if did_you_mean:
        payload["did_you_mean"] = did_you_mean
    payload["hint"] = (
        "The API ignores parameters it does not declare, so this call would "
        "have returned unfiltered data. Rename or drop the unknown keys; "
        "describe_endpoint(operation_id) lists every parameter."
    )
    return payload


def _missing_required(
    endpoint, params: dict[str, Any], body: dict[str, Any] | list[dict[str, Any]] | None
) -> list[str]:
    missing = [
        parameter.name
        for parameter in endpoint.parameters
        if parameter.required and parameter.name not in params
    ]
    if endpoint.request_body_required and body is None:
        missing.append("body")
    return missing


@mcp.tool(annotations=read_only("Search endpoints"))
@trace_mcp_tool("search_endpoints")
async def search_endpoints(
    query: Annotated[
        str,
        Field(
            description=(
                "Natural-language search over the bundled catalog. Name the "
                "instrument, series, place, or task (examples: 'US CPI', "
                "'AAPL quote', 'North Sea AIS'). Returns ranked operation_id "
                "hits with required_parameters. Then call describe_endpoint "
                "on a hit before call_endpoint."
            ),
        ),
    ],
    toolset: Annotated[
        str | None,
        Field(
            description=(
                "Optional catalog group filter (markets, macro, news, "
                "network, ...). Call list_toolsets for the live names. An "
                "unknown value returns error unknown_toolset with known_toolsets "
                "rather than an empty hit list."
            ),
        ),
    ] = None,
    source: Annotated[
        str | None,
        Field(
            description=(
                "Optional source-family filter as listed by list_sources "
                "(macro, markets, ...). An unknown value returns error "
                "unknown_source with known_sources."
            ),
        ),
    ] = None,
    limit: Annotated[
        int,
        Field(
            description=(
                "Maximum ranked hits to return. Default 10. Does not call "
                "the Sugra API; this only bounds the catalog search list."
            ),
        ),
    ] = 10,
) -> dict[str, Any]:
    """Search the bundled Sugra endpoint catalog by natural-language query.

    Use this to pick an operation_id. It does not fetch data. Typical loop:
    1. search_endpoints(query) -> ranked hits with required_parameters
    2. describe_endpoint(operation_id) -> params, request_body_schema, agent_hints
    3. call_endpoint(operation_id, params=..., body=...) or fetch_data(query, params=...)

    Filter with toolset or source only after list_toolsets / list_sources;
    a misspelled filter is an error, not a silent empty result.

    Examples:
    - search_endpoints("US CPI inflation")
    - search_endpoints("AAPL price", toolset="markets")
    - search_endpoints("container ship AIS", toolset="network")
    """
    refusal = query_limit_error(query)
    if refusal is not None:
        return refusal
    catalog = load_catalog()
    # An unknown filter value used to fall through the per-endpoint comparison and
    # return an empty result list - indistinguishable from "this catalog genuinely
    # has nothing for your query". A misspelling, or a client written against a
    # different catalog vintage (the toolset taxonomy is versioned WITH the
    # bundle), therefore surfaced as a silent zero instead of a diagnosable error.
    # Validate against the accept-set derived from the catalog and say what is
    # valid, so the caller can correct the filter in one step.
    # Activate validation on exactly the predicate the search filter uses
    # (truthiness, not `is not None`): an empty string has always meant "no
    # filter" - clients serialize unset optional strings that way - so validating
    # it would turn a working call into a bogus unknown_* error.
    if toolset:
        valid_toolsets = known_toolsets(catalog)
        if toolset not in valid_toolsets:
            return {
                "error": "unknown_toolset",
                "requested": toolset,
                "known_toolsets": sorted(valid_toolsets),
                "catalog_source": catalog.source,
            }
    if source:
        valid_sources = known_sources(catalog)
        if source not in valid_sources:
            return {
                "error": "unknown_source",
                "requested": source,
                "known_sources": sorted(valid_sources),
                "catalog_source": catalog.source,
            }
    results = await _search_off_loop(catalog, query, toolset=toolset, source=source, limit=limit)
    if isinstance(results, dict):
        return results
    return {"results": results, "total_matched": len(results), "catalog_source": catalog.source}


@mcp.tool(annotations=read_only("Describe endpoint"))
@trace_mcp_tool("describe_endpoint")
async def describe_endpoint(
    operation_id: Annotated[
        str,
        Field(
            description=(
                "Catalog operation_id from search_endpoints (or from "
                "list_toolsets drill-down). Unknown ids return error "
                "unknown_operation_id."
            ),
        ),
    ],
) -> dict[str, Any]:
    """Describe one Sugra API endpoint by operation_id.

    Includes agent_hints (duration_class fast/slow/heavy, max_concurrency,
    bulk billing) so you can budget timeouts and parallelism before calling.
    POST endpoints with a JSON body also carry request_body_schema (the
    resolved JSON schema) - construct the `body` argument from it instead
    of guessing key names. Call this after search_endpoints and before
    call_endpoint when you need the exact parameter names and examples.
    """
    catalog = load_catalog()
    try:
        endpoint = catalog.get(operation_id)
    except KeyError:
        return {"error": "unknown_operation_id", "operation_id": operation_id}
    described = endpoint.to_dict()
    described["agent_hints"] = hints_for(endpoint)
    return described


@mcp.tool(annotations=read_only("Call endpoint"))
@trace_mcp_tool("call_endpoint")
async def call_endpoint(
    operation_id: str,
    params: Annotated[
        dict[str, Any] | None,
        Field(
            description=(
                "Query and path parameters for this operation_id. Keys and types are "
                "operation-specific - call describe_endpoint(operation_id) first to get the "
                "exact parameter names, types, and examples. Omit if the operation takes none. "
                "A key the operation does not declare returns error unknown_parameters "
                "with the accepted names and did_you_mean, before any request is made."
            ),
        ),
    ] = None,
    body: Annotated[
        dict[str, Any] | list[dict[str, Any]] | None,
        Field(
            description=(
                "JSON request body for a POST operation, matching the request_body_schema "
                "returned by describe_endpoint(operation_id): a JSON object for most "
                "operations, or a JSON array when that schema's top-level type is array. "
                "Omit for GET operations."
            ),
        ),
    ] = None,
    limit: Annotated[
        int | None,
        Field(
            description=(
                "Bounds ONLY the records list: the data list, a bare top-level "
                "array, or the list inside an object data when exactly one of "
                "these keys holds a list: data, entries, events, history, items, "
                "observations, points, records, results, rows, series, timeseries "
                "(for example data.items). When data has no such single list but "
                "every one of its values is an object holding exactly one list "
                "named observations, limit bounds each data.<key>.observations "
                "list on its own (records_path data.*.observations); fields there "
                "still names keys of data. Otherwise, no such list, or several, "
                "means the limit does not apply. Keys beside the list such as "
                "total and count are not rewritten, and lists nested inside "
                "records are never truncated. limit keeps the newest N records "
                "when every record carries one date or period key in one format "
                "and the list runs one way by it, else the first N, and "
                "meta.shaped reports limit_applied, records_path and, for a "
                "bounded records list, order (asc, desc or unknown) and kept_end "
                "(newest or first), as maps by name for sibling sub-series."
            ),
        ),
    ] = None,
    fields: Annotated[
        list[str] | None,
        Field(
            description=(
                "Optional projection of keys to keep on each record of the "
                "records list: the data list, a bare top-level array, or the "
                "list inside an object data when exactly one of these keys "
                "holds a list: data, entries, events, history, items, "
                "observations, points, records, results, rows, series, "
                "timeseries (for example data.items). Keys beside that list "
                "such as total and count stay. If a field names a key of data "
                "itself, or of a payload without data, that object is "
                "projected instead; an object data without such a list is "
                "otherwise kept whole. Dotted paths (geo.city) walk nested "
                "objects. If no field matches, nothing is removed. meta.shaped "
                "reports fields_applied, fields_unmatched and records_path. "
                "Omit to keep every key."
            ),
        ),
    ] = None,
    include_raw: Annotated[
        bool,
        Field(
            description=(
                "If true, attach the original unshaped payload under raw "
                "when it fits the size cap; otherwise meta.raw_omitted "
                "explains why. Default false."
            ),
        ),
    ] = False,
) -> dict[str, Any]:
    """Call a Sugra API endpoint by operation_id from the bundled catalog.

    Plan calls with describe_endpoint's agent_hints: duration_class "fast"
    usually responds in under ~2s, "slow" usually 1-5s and occasionally 15s+
    on a cold upstream, "heavy" can exceed the gateway timeout - keep parallel
    calls within max_concurrency and prefer small batches. Bulk endpoints bill
    1 request credit per body item. Failures return structured errors {error,
    reason, status_code, elapsed_ms, retry_hint}; after "upstream_timeout" a
    single retry often succeeds because the aborted attempt warms upstream
    caches.
    """
    # The whole body sits in one safety net: a raised exception surfaces to
    # MCP clients as "Error executing tool call_endpoint: <message>" where
    # the message can be EMPTY (field-test defect D2). Returning a structured
    # dict keeps the error contract intact for any unexpected failure class,
    # including catalog-load and parameter-resolution failures.
    start = time.perf_counter()
    try:
        catalog = load_catalog()
        try:
            endpoint = catalog.get(operation_id)
        except KeyError:
            return {"error": "unknown_operation_id", "operation_id": operation_id}

        clean_params = {key: value for key, value in (params or {}).items() if value is not None}
        # Checked before the required ones: a misnamed key is usually the
        # missing parameter itself, and did_you_mean names it.
        unknown = _unknown_params_error(operation_id, endpoint, clean_params)
        if unknown:
            return unknown
        missing = _missing_required(endpoint, clean_params, body)
        if missing:
            payload: dict[str, Any] = {
                "error": "missing_required_parameters",
                "operation_id": operation_id,
                "missing": missing,
            }
            # One diagnostic carries EVERYTHING the next call needs: hiding
            # the group constraint here would force a second failing round
            # trip.
            if endpoint.required_groups:
                payload["required_groups"] = [list(g) for g in endpoint.required_groups]
                payload["groups_hint"] = (
                    "also supply every parameter of "
                    + ("EXACTLY one group" if endpoint.groups_mutually_exclusive
                       else "at least one group"))
            return payload
        violation = _group_violation(endpoint, clean_params)
        if violation:
            return {
                "error": "missing_required_parameter_groups",
                "operation_id": operation_id,
                "groups": [list(group) for group in endpoint.required_groups],
                "hint": ("supply every parameter of EXACTLY one group"
                         if violation == "multiple"
                         else "supply every parameter of at least one group"
                         + (" (groups are mutually exclusive)"
                            if endpoint.groups_mutually_exclusive else "")),
            }

        path_param_names = {
            parameter.name for parameter in endpoint.parameters if parameter.location == "path"
        }
        query_param_names = {
            parameter.name for parameter in endpoint.parameters if parameter.location == "query"
        }
        path = _resolve_path(
            endpoint.path,
            {key: value for key, value in clean_params.items() if key in path_param_names},
        )
        if "{" in path:
            return {
                "error": "unresolved_path_parameters",
                "operation_id": operation_id,
                "path": path,
            }

        query_params = {
            key: value
            for key, value in clean_params.items()
            if key in query_param_names or key not in path_param_names
        }

        client = get_client()
        if endpoint.method == "GET":
            payload = await client.get(path, params=query_params, enforce_size=False)
        elif endpoint.method == "POST":
            payload = await client.request(
                endpoint.method, path, params=query_params, json=body, enforce_size=False
            )
        else:
            return {
                "error": "unsupported_method",
                "operation_id": operation_id,
                "method": endpoint.method,
            }

        # The client above measured nothing (enforce_size=False): here is
        # where the raw body is measured, AFTER any fields/limit projection
        # this call applies, never before it - a request that projects a
        # large envelope down to a small field must not be rejected for the
        # size of the body it never returns. Applied on every return path,
        # including the structured-error one below, for parity with the
        # client's own unconditional enforcement on every other caller.
        if is_error_payload(payload):
            # Structured error contract from SugraClient (transport failure
            # or HTTP 4xx/5xx). Return it untouched apart from the same size
            # cap: shaping an error dict would only decorate it with
            # misleading meta while the agent needs the raw {error, reason,
            # elapsed_ms}. The "no data key" guard mirrors entities._is_error:
            # a success envelope always carries data, so a hypothetical 200
            # partial payload with both keys still gets shaped normally.
            return _enforce_size_limit(payload, path)

        shaped = shape_response(payload, limit=limit, fields=fields, include_raw=include_raw)
        # The unshaped payload lets the gate read the records' order as the
        # API sent them, so it keeps the same end meta.shaped reports.
        return _enforce_size_limit(shaped, path, unshaped=payload)
    except Exception as exc:
        return {
            "error": "tool_execution_failed",
            "operation_id": operation_id,
            "exception_type": type(exc).__name__,
            "reason": str(exc)[:300].strip() or type(exc).__name__,
            "elapsed_ms": int((time.perf_counter() - start) * 1000),
        }


def toolsets_payload() -> dict[str, Any]:
    """Toolset groups with endpoint counts from the bundled catalog.

    Shared by the list_toolsets tool and the sugra://catalog/domains
    resource so both surfaces always report identical data.
    """
    catalog = load_catalog()
    counts: dict[str, int] = {}
    for endpoint in catalog.endpoints:
        counts[endpoint.toolset] = counts.get(endpoint.toolset, 0) + 1
    return {"toolsets": ordered_toolsets(counts), "total_endpoints": catalog.endpoint_count}


@mcp.tool(annotations=read_only("List toolsets"))
@trace_mcp_tool("list_toolsets")
async def list_toolsets() -> dict[str, Any]:
    """List catalog groups with endpoint counts and short descriptions.

    Use the group names as the toolset filter on search_endpoints. This
    does not call the Sugra API; it reads the bundled catalog.
    """
    return toolsets_payload()


@mcp.tool(annotations=read_only("Fetch data"))
@trace_mcp_tool("fetch_data")
async def fetch_data(
    query: Annotated[
        str,
        Field(
            description=(
                "Natural-language request for data (examples: 'US CPI', "
                "'Bitcoin price', 'latest news'). The tool picks the top "
                "catalog match and calls it. If required params are missing "
                "it returns needs_params instead of guessing."
            ),
        ),
    ],
    params: Annotated[
        dict[str, Any] | None,
        Field(
            description=(
                "Parameters for the auto-selected endpoint. If omitted and the best-match "
                "endpoint has required parameters, the tool returns that endpoint's "
                "required_parameters and examples so you can retry with them filled in."
            ),
        ),
    ] = None,
    body: Annotated[
        dict[str, Any] | list[dict[str, Any]] | None,
        Field(
            description=(
                "JSON body for an auto-selected POST operation; the tool returns the "
                "request_body_schema to fill when the match needs one. Pass a JSON "
                "object or a JSON array as that schema's top-level type dictates."
            ),
        ),
    ] = None,
    limit: Annotated[
        int | None,
        Field(
            description=(
                "Bounds ONLY the records list: the data list, a bare top-level "
                "array, or the list inside an object data when exactly one of "
                "these keys holds a list: data, entries, events, history, items, "
                "observations, points, records, results, rows, series, timeseries "
                "(for example data.items). When data has no such single list but "
                "every one of its values is an object holding exactly one list "
                "named observations, limit bounds each data.<key>.observations "
                "list on its own (records_path data.*.observations); fields there "
                "still names keys of data. Otherwise, no such list, or several, "
                "means the limit does not apply. Keys beside the list such as "
                "total and count are not rewritten, and lists nested inside "
                "records are never truncated. limit keeps the newest N records "
                "when every record carries one date or period key in one format "
                "and the list runs one way by it, else the first N, and "
                "meta.shaped reports limit_applied, records_path and, for a "
                "bounded records list, order (asc, desc or unknown) and kept_end "
                "(newest or first), as maps by name for sibling sub-series."
            ),
        ),
    ] = None,
    fields: Annotated[
        list[str] | None,
        Field(
            description=(
                "Optional projection of keys to keep on each record of the "
                "records list: the data list, a bare top-level array, or the "
                "list inside an object data when exactly one of these keys "
                "holds a list: data, entries, events, history, items, "
                "observations, points, records, results, rows, series, "
                "timeseries (for example data.items). Keys beside that list "
                "such as total and count stay. If a field names a key of data "
                "itself, or of a payload without data, that object is "
                "projected instead; an object data without such a list is "
                "otherwise kept whole. Dotted paths (geo.city) walk nested "
                "objects. If no field matches, nothing is removed. meta.shaped "
                "reports fields_applied, fields_unmatched and records_path. "
                "Omit to keep every key."
            ),
        ),
    ] = None,
    include_raw: Annotated[
        bool,
        Field(
            description=(
                "If true, attach the original unshaped payload under raw "
                "when it fits the size cap; otherwise meta.raw_omitted "
                "explains why. Default false."
            ),
        ),
    ] = False,
) -> dict[str, Any]:
    """One-step fetch: find the best Sugra endpoint for the query and call it.

    Combines search_endpoints + call_endpoint into a single round trip. Use
    this when you want data without manually picking an operation_id. The
    full search_endpoints + describe_endpoint + call_endpoint dance is still
    available when you need explicit control, but for most natural-language
    queries this tool is enough.

    Behavior:
    1. Search the bundled catalog for the query. Top match wins.
    2. If the matched endpoint has required parameters and they are all
       provided in `params`, call it and return the response.
    3. If required parameters are missing, return the candidate endpoints
       and the missing-params list so the LLM can retry with the correct
       `params` dict on the next call.

    Examples:
    - `fetch_data("US CPI inflation", params={"series_id": "CPIAUCSL"})`
      → calls /api/v1/fred/series/CPIAUCSL, returns observations.
    - `fetch_data("Bitcoin price", params={"coin_id": "bitcoin"})`
      → calls /api/v1/crypto/bitcoin/price.
    - `fetch_data("Latest financial news")`
      → news_latest has no required params, returns latest news directly.
    """
    # Same whole-body safety net as call_endpoint (defect D2): the search and
    # selection path must never raise through FastMCP as an empty message.
    start = time.perf_counter()
    try:
        refusal = query_limit_error(query)
        if refusal is not None:
            return refusal
        catalog = load_catalog()
        results = await _search_off_loop(catalog, query, limit=3)
        if isinstance(results, dict):
            return results

        if not results:
            return {
                "error": "no_endpoint_found",
                "query": query,
                "hint": "Try a more specific query or use search_endpoints + describe_endpoint to explore the catalog manually.",
            }

        top = results[0]
        operation_id = top["operation_id"]

        try:
            endpoint = catalog.get(operation_id)
        except KeyError:
            # Should never happen — search returned an op_id that load_catalog
            # doesn't recognise. Surface as a clear error rather than crashing.
            return {
                "error": "stale_search_result",
                "operation_id": operation_id,
                "candidate_endpoints": results,
            }

        clean_params = {key: value for key, value in (params or {}).items() if value is not None}
        missing = _missing_required(endpoint, clean_params, body)

        if missing:
            # LLM didn't supply enough — return both the selected endpoint's
            # schema and the alternative candidates so the next call can either
            # fill the gap or pick a different endpoint.
            selected: dict[str, Any] = {
                "operation_id": operation_id,
                "method": endpoint.method,
                "path": endpoint.path,
                "summary": endpoint.summary,
                "agent_hints": hints_for(endpoint),
                **({"required_groups": [list(g) for g in endpoint.required_groups],
                    "groups_mutually_exclusive": endpoint.groups_mutually_exclusive}
                   if endpoint.required_groups else {}),
                "required_parameters": endpoint.required_parameters,
                "parameter_examples": [
                    {
                        "name": p.name,
                        "description": p.description,
                        "example": p.example,
                        "required": p.required,
                    }
                    for p in endpoint.parameters
                    if p.required
                ],
            }
            if endpoint.request_body_schema:
                # "body" in missing means the agent must construct a JSON
                # body - hand it the exact schema instead of letting it guess.
                selected["request_body_schema"] = endpoint.request_body_schema
            return {
                "needs_params": missing,
                "selected_endpoint": selected,
                "candidate_endpoints": results,
                "hint": (
                    f"The top match `{operation_id}` requires {missing}. "
                    f"Retry as fetch_data(query, params={{...}}) with those keys filled in, "
                    f"or call describe_endpoint(operation_id) for full schema."
                ),
            }

        violation = _group_violation(endpoint, clean_params)
        if violation:
            return {
                "error": "missing_required_parameter_groups",
                "operation_id": operation_id,
                "groups": [list(group) for group in endpoint.required_groups],
                "hint": ("supply every parameter of EXACTLY one group"
                         if violation == "multiple"
                         else "supply every parameter of at least one group"
                         + (" (groups are mutually exclusive)"
                            if endpoint.groups_mutually_exclusive else "")),
                "candidate_endpoints": results,
            }

        # All required params satisfied - delegate to the same call path as
        # call_endpoint so behavior is identical (path resolution, query/body
        # routing, response shaping). operation_id goes by KEYWORD: the
        # delegate is the decorated tool, and its span reads the operation
        # from kwargs only (a positional first argument is a raw query on
        # other tools). Passed positionally, every delegated failure was a
        # call_endpoint span with no operation at all.
        return await call_endpoint(
            operation_id=operation_id,
            params=clean_params,
            body=body,
            limit=limit,
            fields=fields,
            include_raw=include_raw,
        )
    except Exception as exc:
        return {
            "error": "tool_execution_failed",
            "exception_type": type(exc).__name__,
            "reason": str(exc)[:300].strip() or type(exc).__name__,
            "elapsed_ms": int((time.perf_counter() - start) * 1000),
        }


def sources_payload() -> dict[str, Any]:
    """Source families with endpoint counts from the bundled catalog.

    Shared by the list_sources tool and the sugra://catalog/sources
    resource so both surfaces always report identical data.
    """
    catalog = load_catalog()
    counts: dict[str, int] = {}
    for endpoint in catalog.endpoints:
        family = endpoint.source_family
        counts[family] = counts.get(family, 0) + 1
    return {
        "source_families": ordered_toolsets(counts),
        "endpoint_count": catalog.endpoint_count,
        "catalog_source": catalog.source,
    }


@mcp.tool(annotations=read_only("List sources"))
@trace_mcp_tool("list_sources")
async def list_sources() -> dict[str, Any]:
    """List source families in the bundled catalog with endpoint counts.

    Use the family names as the source filter on search_endpoints. This
    does not call the Sugra API.
    """
    return sources_payload()
