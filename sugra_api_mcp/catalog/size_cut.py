"""Cut an oversized response to the size cap, whatever its shape.

The response size gate (``client._enforce_size_limit``) measures a response
with ``client.response_chars``: the length of ``json.dumps`` with its
defaults, ASCII escapes and the default separators, exactly the measure the
cap always had. A CJK or accented character counts as its six-character
escape, which keeps the cap inside the token limit for such text too. The
fit itself is measured by ``client.response_chars_within``, which stops at
the cap. A response over the cap is cut here, and refused only when no cut
can fit.

What is cut: the lists of the response, at most two levels under its
``data``, or under the payload itself when it has no ``data`` (its ``meta``
and ``_meta`` are left alone). That is ``data`` itself when it is a list,
every list at ``data.<key>`` and every list at ``data.<key>.<key>``, whatever
the key is called: ``data.data`` of a quote history, ``data.hourly`` beside
``data.daily`` of a forecast, the four ``data.<kind>.rows`` of a market
calendar. A bare array is answered as ``{"data": [...]}``, as shaping
answers it. A list inside a record is never cut, nor is a record itself, and
a cut list keeps at least one record.

How much: every record is measured once, and the size of the response is
the sum of its records and of the rest around them. A ceiling in characters
is then found by binary search, so that each list larger than the ceiling
keeps the records that fit under it, every smaller list stays whole, and the
whole response with its notice fits the cap. A big list beside small ones
(hourly beside daily) is the one cut; lists of like size are cut alike. The
result is measured once more to verify it. When it is still over, because
the notice is estimated before it is written, the ceiling is lowered and the
result measured again, at most MAX_SHRINK_PASSES times.

Which end is kept: the end ``limit`` keeps (``response._limit_records``),
the newest when the order of the list can be read, else the first records.
For the operations in NEAREST_END_OPERATIONS, forecasts and calendars, a
list in either order whose records carry ISO 8601 dates keeps the record
nearest today and the ones after it in time, then, when room is left, the
ones before it, so a forecast keeps its next hours and not its last ones;
``kept_end`` is then ``nearest``. Today is the UTC date, and a caller may
pass its own. The catalog entry this rule and the hints read is passed by
call_endpoint only: fetch_data passes none yet, so its cuts keep the newest
or first records and their hints name no parameter.

Where it runs and for how long: only off the event loop, on the shaping
pool. call_endpoint runs it there after shaping
(``tools.gateway._shape_and_gate``), and ``SugraClient.request`` sends an
oversized response of the fixed tools there (``client._gate_off_loop``),
a success or a failure. The error payloads call_endpoint returns as they
came are gated on the event loop, where one over the cap is refused
(``refuse_unmeasured``) without being walked. A cut has a
MAX_SHAPING_SECONDS clock of its own, started before it measures anything,
and past it the response is refused with ``response_too_large``. That
clock is cooperative and best effort, between items: it is read before
every key of the objects searched for lists, every record of the order,
date and sizing scans, every list and record of the priority, replacement
and notice passes, and around each measure of the rest of the response, of
the hints and of the result. One record's serialise is bounded by that
record's size, and one measure of the rest by the size of the rest. The
hard bound for the fixed tools and call_endpoint is the caller's wait on
the pool (``client._cut_on_pool``), which answers without the cut once the
clock, two clocks for call_endpoint whose fields projection has its own,
and a short grace have passed.

What is said: ``meta.truncated`` names the cut. Its ``original_count``,
``kept_count``, ``order`` and ``kept_end`` describe the primary list, the
largest one cut, at ``path``; ``original_chars``, ``kept_chars`` and
``cap_chars`` give the sizes; ``lists`` gives the same per path when more
than one list was cut; ``retry_hint`` says what was kept and which arguments
of the tool and parameters of the operation choose a smaller answer. Its
``fields`` names the lists that stayed whole and the cut lists that fit the
cap on their own, at most _MAX_NAMED_FIELDS in a set and _MAX_NAMED_FIELDS
sets of one, measured from the record sizes the cut already has: that list
whole plus the response outside the lists plus the room the notice takes. A
refusal says the size and the limit in characters and, when it can, where
the size is and how to leave it out. Every refusal is measured too: one
that would itself be over the cap, through a very long URL or key, says the
size alone, with the URL cut to _MAX_URL_CHARS.
"""

from __future__ import annotations

import heapq
import json
import time
from bisect import bisect_right
from collections.abc import Callable
from datetime import UTC, date, datetime
from typing import Any

from ..client import _unshaped_records, response_chars, response_chars_within
from .response import (
    _DATE_KEY_CANDIDATES,
    KEPT_FIRST,
    KEPT_NEWEST,
    MAX_SHAPING_SECONDS,
    ORDER_ASC,
    ORDER_DESC,
    _each,
    _records_key,
    _records_order,
)

KEPT_NEAREST = "nearest"
CUT_REASON = "exceeds_response_size_cap"

# Verification passes after the first estimate of the notice.
MAX_SHRINK_PASSES = 8
# The clock a cut reads; a test replaces it.
_clock = time.monotonic
# The URL a refusal over the cap keeps.
_MAX_URL_CHARS = 300
# What answering a bare array as {"data": [...]} adds to its size.
_WRAP_CHARS = len('{"data": }')

# Forward-looking operations: their dated lists keep the records nearest
# today, not the newest ones. A test checks every id against the bundled
# catalog, so a resync that renames one fails it.
NEAREST_END_OPERATIONS = frozenset(
    {
        "air_quality_forecast",
        "cinema_films_upcoming_by_studio_slug",
        "fixed_income_treasury_auctions_upcoming",
        "macro_cb_calendar",
        "macro_cb_calendar_bank",
        "market_calendar",
        "market_calendar_dividends",
        "market_calendar_earnings",
        "market_calendar_splits",
        "transport_disruption_risk_forecast",
        "v2_weather_forecast",
        "weather_forecast",
        "weather_marine_forecast",
        "weather_nws_forecast",
        "weather_nws_forecast_hourly",
        "weather_us_forecast",
        "weather_us_forecast_hourly",
    }
)

# Parameter names a hint may name, read from the operation's catalog entry.
_COUNT_PARAMS = ("limit", "page_size", "last_n", "outputsize", "per_page", "max_results")
_WINDOW_PAIRS = (
    ("start_date", "end_date"),
    ("start", "end"),
    ("from", "to"),
    ("from_date", "to_date"),
    ("start_period", "end_period"),
    ("start_year", "end_year"),
    ("period1", "period2"),
)
_WINDOW_SINGLES = ("forecast_days", "days", "range", "period", "recent_years")
_MAX_NAMED_FIELDS = 6
_PROVENANCE_KEYS = ("meta", "_meta")

Tick = Callable[[], None]


class _Expired(Exception):
    """The cut ran past its clock."""


def _ticker(deadline: float) -> Tick:
    """A tick that raises _Expired once the clock passes deadline."""

    def tick() -> None:
        if _clock() > deadline:
            raise _Expired

    return tick


def _utc_today() -> date:
    return datetime.now(UTC).date()


def _day(value: Any) -> date | None:
    """The UTC day an ISO 8601 date or date-time names, else None."""
    if not isinstance(value, str):
        return None
    try:
        moment = datetime.fromisoformat(value)
    except ValueError:
        return None
    if moment.utcoffset() is not None:
        moment = moment.astimezone(UTC)
    return moment.date()


def _date_key(records: list[Any], tick: Tick) -> str | None:
    """The one known date key every record carries, as _records_order reads it."""
    if not records or not all(isinstance(record, dict) for record in _each(records, tick)):
        return None
    present = [
        key for key in _DATE_KEY_CANDIDATES
        if all(record.get(key) is not None for record in _each(records, tick))
    ]
    return present[0] if len(present) == 1 else None


def _nearest_priority(days: list[date], today: date, order: str, tick: Tick) -> list[int]:
    """The indexes of a dated list in the order the nearest rule keeps them:
    the record nearest today, the ones after it in time, then the ones
    before it. In an ascending list the first record dated today or later
    is the nearest, in a descending one the last; when none is, the newest
    record is."""
    count = len(days)
    if order == ORDER_ASC:
        start = next(
            (index for index, day in enumerate(_each(days, tick)) if day >= today), count - 1
        )
        return [*range(start, count), *range(start - 1, -1, -1)]
    stop = next(
        (index for index in _each(range(count - 1, -1, -1), tick) if days[index] >= today), 0
    )
    return [*range(stop, -1, -1), *range(stop + 1, count)]


def _dotted(path: tuple[str, ...]) -> str:
    return ".".join(path)


class _List:
    """One list of the response: its records, the end it keeps, and the size
    of its first k records in the order they are kept."""

    def __init__(
        self,
        path: tuple[str, ...],
        records: list[Any],
        source: list[Any] | None,
        today: date | None,
        tick: Tick,
    ) -> None:
        self.path = path
        self.records = records
        self.n = len(records)
        # The order is read from the list as the API sent it when the caller
        # passed it: shaping may have projected the date key away.
        self.order = _records_order(source if source is not None else records, tick=tick)
        # The records the dates are read from: these, or the unshaped list
        # when it still matches them one for one.
        self.dated: list[Any] | None = None
        self.date_key: str | None = None
        for candidate in (records, source):
            if candidate is not None and len(candidate) == self.n:
                key = _date_key(candidate, tick)
                if key is not None:
                    self.dated, self.date_key = candidate, key
                    break
        self.kept_end = KEPT_NEWEST if self.order in (ORDER_ASC, ORDER_DESC) else KEPT_FIRST
        priority: list[int] | None = None
        if today is not None and self.order in (ORDER_ASC, ORDER_DESC) and self.dated is not None:
            days = [_day(record[self.date_key]) for record in _each(self.dated, tick)]
            if all(day is not None for day in _each(days, tick)):
                priority = _nearest_priority(days, today, self.order, tick)
                self.kept_end = KEPT_NEAREST
        sizes = [response_chars(record) for record in _each(records, tick)]
        self.largest = max(sizes)
        if priority is None:
            # The newest end: the tail of an ascending list, else the head.
            priority = list(range(self.n - 1, -1, -1)) if self.order == ORDER_ASC else list(range(self.n))
        self.priority = priority
        # prefix[k]: the characters of the first k records kept with the
        # ", " between them, inside brackets already counted in the shell.
        self.prefix = [0]
        total = 0
        for position, index in enumerate(_each(priority, tick)):
            total += sizes[index] + (2 if position else 0)
            self.prefix.append(total)

    @property
    def chars(self) -> int:
        return self.prefix[-1]

    def keep(self, ceiling: int) -> int:
        """Records kept under a ceiling: all when the list fits it, else as
        many as fit, and never fewer than one."""
        if self.prefix[-1] <= ceiling:
            return self.n
        return max(1, bisect_right(self.prefix, ceiling) - 1)

    def window(self, kept: int) -> tuple[int, int]:
        """The slice the first ``kept`` records in priority order fill: they
        always run on, in the list's own order."""
        chosen = self.priority[:kept]
        return min(chosen), max(chosen) + 1

    def kept_range(self, kept: int) -> list[Any] | None:
        if self.dated is None or self.order not in (ORDER_ASC, ORDER_DESC):
            return None
        low, high = self.window(kept)
        return [self.dated[low][self.date_key], self.dated[high - 1][self.date_key]]


def _root(payload: Any) -> tuple[Any, tuple[str, ...]]:
    """Where the lists are looked for: ``data``, else the payload itself."""
    if isinstance(payload, dict) and "data" in payload:
        return payload["data"], ("data",)
    return payload, ()


def _find_lists(payload: Any, tick: Tick) -> list[tuple[tuple[str, ...], list[Any]]]:
    """Every non-empty list at most two levels under the root, in order. The
    clock is read before every key, of the root and of each object in it."""
    if not isinstance(payload, dict):
        return []
    root, prefix = _root(payload)
    if isinstance(root, list):
        return [(prefix, root)] if root and prefix else []
    if not isinstance(root, dict):
        return []
    found: list[tuple[tuple[str, ...], list[Any]]] = []
    for key, value in _each(root.items(), tick):
        if not prefix and key in _PROVENANCE_KEYS:
            continue
        if isinstance(value, list):
            if value:
                found.append(((*prefix, key), value))
        elif isinstance(value, dict):
            for sub_key, sub_value in _each(value.items(), tick):
                if isinstance(sub_value, list) and sub_value:
                    found.append(((*prefix, key, sub_key), sub_value))
    return found


def _unshaped_at(unshaped: Any, path: tuple[str, ...]) -> list[Any] | None:
    if unshaped is None:
        return None
    if path == ("data",):
        return _unshaped_records(unshaped)
    node = unshaped
    for key in path:
        if not isinstance(node, dict):
            return None
        node = node.get(key)
    return node if isinstance(node, list) else None


def _with_lists(
    payload: dict[str, Any], replacements: dict[tuple[str, ...], list[Any]], tick: Tick
) -> dict[str, Any]:
    """A copy of payload with the lists at the given paths replaced. Only the
    objects on those paths are copied; the payload itself is never changed."""
    out = dict(payload)
    copies: dict[tuple[str, ...], dict[str, Any]] = {(): out}
    for path, value in _each(replacements.items(), tick):
        node = out
        for depth in range(1, len(path)):
            prefix = path[:depth]
            if prefix not in copies:
                copies[prefix] = dict(node[path[depth - 1]])
                node[path[depth - 1]] = copies[prefix]
            node = copies[prefix]
        node[path[-1]] = value
    return out


def _with_notice(payload: dict[str, Any], notice: dict[str, Any]) -> dict[str, Any]:
    existing = payload.get("meta")
    meta = dict(existing) if isinstance(existing, dict) else {}
    meta["truncated"] = notice
    payload["meta"] = meta
    return payload


def _params(endpoint: Any) -> dict[str, Any]:
    if endpoint is None:
        return {}
    return {parameter.name: parameter for parameter in endpoint.parameters}


def _clamped(parameter: Any, value: int) -> int:
    schema = getattr(parameter, "schema_", None) or {}
    maximum = schema.get("maximum")
    minimum = schema.get("minimum")
    if isinstance(maximum, (int, float)) and not isinstance(maximum, bool):
        value = min(value, int(maximum))
    floor = int(minimum) if isinstance(minimum, (int, float)) and not isinstance(minimum, bool) else 1
    return max(value, floor)


def _window_lever(params: dict[str, Any]) -> str | None:
    for first, second in _WINDOW_PAIRS:
        if first in params and second in params:
            return f"a narrower params.{first} to params.{second}"
    for name in _WINDOW_SINGLES:
        if name in params:
            return f"a smaller params.{name}"
    return None


def _is_records_list(payload: Any, path: tuple[str, ...]) -> bool:
    """Whether shaping's limit and fields work on the list at path."""
    if path == ("data",):
        return True
    if len(path) == 2 and path[0] == "data" and isinstance(payload, dict):
        return _records_key(payload.get("data")) == path[1]
    return False


def _lists_by_key(lists: list[_List], prefix: tuple[str, ...], tick: Tick) -> dict[str, int]:
    """The characters of the records of every list under each key of the
    root: what that key's lists add to the response outside its lists."""
    sizes: dict[str, int] = {}
    for lst in _each(lists, tick):
        if len(lst.path) > len(prefix):
            key = lst.path[len(prefix)]
            sizes[key] = sizes.get(key, 0) + lst.chars
    return sizes


def _fit_whole_alone(
    payload: Any,
    lists: list[_List],
    cut: list[_List],
    kept: dict[tuple[str, ...], int],
    room: int | None,
    tick: Tick,
) -> list[list[str]]:
    """Sets of keys of the root that fields can name to get their lists whole:
    the lists that stayed whole, and the cut lists under other keys of the
    root that fit the cap on their own. ``room`` is what the cap leaves for
    lists after the response outside them (every list empty, so a conservative
    shell: fields drops what it does not name) and after what the gate adds,
    the cut notice standing for that; None offers no cut list. The first set
    holds at most _MAX_NAMED_FIELDS names, in the response's key order: the
    first lists that stayed whole, then the cut lists, smallest first, while
    they fit in ``room`` together with them. Then the next cut lists, smallest
    first, each of which fits ``room`` alone, follow as sets of one, at most
    _MAX_NAMED_FIELDS of them; a larger cut list past those is not offered.
    Every pass over the root and the cut lists reads the clock, and nothing
    longer than the two bounded sets is ever ranked or sorted."""
    root, prefix = _root(payload)
    if not isinstance(root, dict):
        return []
    cut_keys = {lst.path[len(prefix)] for lst in _each(cut, tick) if len(lst.path) > len(prefix)}
    sizes = _lists_by_key(lists, prefix, tick)
    named: list[str] = []
    order: dict[str, int] = {}
    used = 0
    for index, (key, value) in enumerate(_each(root.items(), tick)):
        order[key] = index
        path = (*prefix, key)
        if key in cut_keys or not isinstance(value, list) or not value:
            continue
        # The first whole lists in the response's order; past the bound none
        # is kept, so no set of them grows with the response.
        if len(named) < _MAX_NAMED_FIELDS and kept.get(path, len(value)) == len(value):
            named.append(key)
            used += sizes.get(key, 0)
    alone: list[str] = []
    if room is not None:
        # Smallest first, so the most lists are named; each adds its records.
        # Ties keep the response's own key order, never the hash order. No
        # more than the two bounded sets can take is ever ranked.
        fitting = (
            (sizes.get(key, 0), order.get(key, 0), key)
            for key in _each(cut_keys, tick)
            if sizes.get(key, 0) <= room
        )
        grouped = True
        for size, _, key in heapq.nsmallest(2 * _MAX_NAMED_FIELDS, fitting):
            tick()
            if grouped and len(named) < _MAX_NAMED_FIELDS and used + size <= room:
                named.append(key)
                used += size
            else:
                grouped = False
                if len(alone) < _MAX_NAMED_FIELDS:
                    alone.append(key)
    # At most _MAX_NAMED_FIELDS names, back in the response's key order.
    groups = [sorted(named, key=order.__getitem__)]
    groups.extend([key] for key in alone)
    return [group for group in groups if group]


def _shown(lst: _List, kept: int) -> str:
    where = f"{kept:,} of {lst.n:,} records at {_dotted(lst.path)}"
    if lst.kept_end == KEPT_NEAREST:
        text = f"Showing {where}, from the one nearest today"
    elif lst.kept_end == KEPT_NEWEST:
        text = f"Showing the newest {where}"
    else:
        text = f"Showing the first {where}"
    span = lst.kept_range(kept)
    if span is not None:
        text += f" ({span[0]} to {span[1]})"
    return text + "."


def _cut_hint(
    payload: Any,
    endpoint: Any,
    primary: _List,
    cut: list[_List],
    kept: dict[tuple[str, ...], int],
    lists: list[_List],
    room: int | None,
    tick: Tick,
) -> str:
    hint = _shown(primary, kept[primary.path])
    if len(cut) > 1:
        hint += f" {len(cut) - 1} more lists were cut, see lists."
    if endpoint is None:
        return hint
    params = _params(endpoint)
    if not params:
        return hint + " The source returns this whole dataset in one response."
    levers: list[str] = []
    count_param = next((name for name in _COUNT_PARAMS if name in params), None)
    # A count, the operation's or the tool's own limit, keeps the end the
    # cut keeps, except the nearest one: there it would keep the farthest
    # records instead, so neither is offered.
    if count_param is not None and primary.kept_end != KEPT_NEAREST:
        value = _clamped(params[count_param], kept[primary.path])
        levers.append(f"params.{count_param}={value}")
    window = _window_lever(params)
    if window is not None:
        levers.append(window)
    if (
        count_param is None
        and len(cut) == 1
        and primary.kept_end != KEPT_NEAREST
        and _is_records_list(payload, primary.path)
    ):
        levers.append(f"limit={kept[primary.path]} beside params")
    for index, names in enumerate(_fit_whole_alone(payload, lists, cut, kept, room, tick)):
        # The first set fits together; each later one is a cut list that fits
        # the cap only by itself.
        reason = "the lists that fit whole on their own" if index == 0 else "that list alone"
        levers.append(f"fields={json.dumps(names)} for {reason}")
    if levers:
        hint += " To choose what is kept, pass " + ", or ".join(levers) + "."
    return hint


def _heavy_hint(payload: Any, endpoint: Any, tick: Tick) -> str:
    """Where most of a response without a cuttable list is, and the fields
    that leave it out."""
    root, prefix = _root(payload)
    if not isinstance(root, dict) or not root:
        return ""
    sizes = {}
    for key, value in root.items():
        if prefix or key not in _PROVENANCE_KEYS:
            tick()
            sizes[key] = response_chars(value)
    tick()
    if not sizes:
        return ""
    heavy = max(sizes, key=lambda key: sizes[key])
    heavy_path: tuple[str, ...] = (*prefix, heavy)
    heavy_chars = sizes[heavy]
    light = [key for key in sizes if key != heavy]
    inner = root[heavy]
    if isinstance(inner, dict) and inner:
        inner_sizes = {}
        for key, value in inner.items():
            tick()
            inner_sizes[key] = response_chars(value)
        tick()
        inner_heavy = max(inner_sizes, key=lambda key: inner_sizes[key])
        if inner_sizes[inner_heavy] * 2 >= heavy_chars:
            heavy_path = (*heavy_path, inner_heavy)
            heavy_chars = inner_sizes[inner_heavy]
            light += [f"{heavy}.{key}" for key in inner if key != inner_heavy]
    hint = f"Most of it is at {_dotted(heavy_path)}, about {heavy_chars:,} characters."
    if endpoint is not None and light:
        names = json.dumps(light[:_MAX_NAMED_FIELDS])
        if len(light) <= _MAX_NAMED_FIELDS:
            hint += f" Pass fields={names} to leave it out."
        else:
            hint += f" Pass fields naming only the keys you need, for example fields={names}."
    return hint


def _rest_hint(shell_payload: dict[str, Any], shell: int, tick: Tick) -> str:
    """Where most of the response outside its lists is: a key of the
    payload, or of its ``data`` object. The lists are empty in
    shell_payload, so a list is never what is named."""
    parts: dict[str, int] = {}
    for key, value in shell_payload.items():
        if key == "data" and isinstance(value, dict):
            for sub_key, sub_value in value.items():
                tick()
                parts[f"data.{sub_key}"] = response_chars(sub_value)
        else:
            tick()
            parts[key] = response_chars(value)
    tick()
    hint = f"Outside its lists the response is about {shell:,} characters"
    if parts:
        heavy = max(parts, key=lambda key: parts[key])
        hint += f"; most of it is at {heavy}, about {parts[heavy]:,} characters"
    return hint + "."


def _refusal_object(url: str, size: int | None, cap: int, hint: str) -> dict[str, Any]:
    if size is None:
        message = f"Response is over the limit of {cap:,} characters."
    else:
        message = f"Response is about {size:,} characters; the limit is {cap:,} characters."
    if hint:
        message += " " + hint
    refusal: dict[str, Any] = {
        "error": "response_too_large",
        "message": message,
        "estimated_tokens": None if size is None else size // 4,
        "response_chars": size,
        "cap_chars": cap,
        "url": url,
    }
    if hint:
        refusal["retry_hint"] = hint
    return refusal


def _refusal(url: str, size: int | None, cap: int, hint: str) -> dict[str, Any]:
    """The ``response_too_large`` refusal, measured: when the URL or the hint
    (which names keys of the response) would put it over the cap itself, it
    says the size alone and keeps the first _MAX_URL_CHARS of the URL."""
    refusal = _refusal_object(url, size, cap, hint)
    if response_chars_within(refusal, cap) is not None:
        return refusal
    short = url if len(url) <= _MAX_URL_CHARS else url[:_MAX_URL_CHARS] + "..."
    return _refusal_object(short, size, cap, "")


def refuse_unmeasured(url: str, cap: int) -> dict[str, Any]:
    """The refusal for a response over the cap that is not walked: on the
    event loop, where no cut runs, its size is not measured past the cap."""
    return _refusal(url, None, cap, "")


def _refuse_for_record(
    url: str,
    size: int,
    cap: int,
    payload: Any,
    endpoint: Any,
    lists: list[_List],
    shell_payload: dict[str, Any],
    shell: int,
    tick: Tick,
) -> dict[str, Any]:
    """The refusal when no cut fits although the rest of the response does:
    a record is named only when it alone leaves no room beside the rest,
    else the rest of the response is."""
    worst = max(lists, key=lambda lst: lst.largest)
    if worst.largest <= cap - shell:
        return _refusal(url, size, cap, _rest_hint(shell_payload, shell, tick))
    where = _dotted(worst.path)
    if worst.largest > cap:
        hint = f"One record at {where} is larger than the limit by itself, about {worst.largest:,} characters."
    else:
        hint = (
            f"One record at {where}, about {worst.largest:,} characters, does not fit beside "
            f"the rest of the response, about {shell:,} characters."
        )
    if endpoint is not None and _is_records_list(payload, worst.path):
        hint += " Pass fields naming only the keys you need from each record."
    return _refusal(url, size, cap, hint)


def _window_only_hint(endpoint: Any) -> str:
    window = _window_lever(_params(endpoint))
    return f" Pass {window}." if window is not None else ""


def cut_to_fit(
    payload: Any,
    url: str,
    *,
    cap: int,
    unshaped: Any = None,
    endpoint: Any = None,
    today: date | None = None,
) -> Any:
    """Cut payload to fit ``cap``, or return the ``response_too_large``
    refusal when no cut fits. Run off the event loop only.

    The clock starts before anything is measured: the size of the response
    is read from its records as they are measured, and the clock is read
    between them. ``endpoint`` is the catalog entry of the operation called
    through call_endpoint; its parameters, and that tool's own limit and
    fields arguments, are what the hints may name, and its operation id
    selects the nearest rule. None (fetch_data for now, and the fixed tools)
    gives hints without any and keeps the newest or first records.
    """
    tick = _ticker(_clock() + MAX_SHAPING_SECONDS)
    original = payload
    wrapped = isinstance(payload, list)
    if wrapped:
        # A bare array is answered as shaping answers it, as {"data": [...]},
        # so it is cut like a data list.
        payload = {"data": payload}
    size: int | None = None
    try:
        found = _find_lists(payload, tick)
        if not found:
            tick()
            size = response_chars(original)
            tick()
            return _refusal(url, size, cap, _heavy_hint(payload, endpoint, tick))
        nearest = endpoint is not None and getattr(endpoint, "operation_id", None) in NEAREST_END_OPERATIONS
        day = (today or _utc_today()) if nearest else None
        lists = []
        for path, records in found:
            tick()
            lists.append(_List(path, records, _unshaped_at(unshaped, path), day, tick))
        shell_payload = _with_lists(payload, {lst.path: [] for lst in _each(lists, tick)}, tick)
        tick()
        shell = response_chars(shell_payload)
        tick()
        size = shell + sum(lst.chars for lst in _each(lists, tick)) - (_WRAP_CHARS if wrapped else 0)
        if size <= cap:
            return original

        def total(ceiling: int) -> int:
            return shell + sum(lst.prefix[lst.keep(ceiling)] for lst in _each(lists, tick))

        def ceiling_for(target: int) -> int:
            low, high = 0, max(lst.chars for lst in _each(lists, tick))
            if total(0) > target:
                return 0
            while low < high:
                middle = (low + high + 1) // 2
                if total(middle) <= target:
                    low = middle
                else:
                    high = middle - 1
            return low

        # The notice is estimated from one written as if nothing were cut;
        # the verification below measures the real one.
        biggest = max(_each(lists, tick), key=lambda lst: lst.chars)
        estimate = {lst.path: lst.n for lst in _each(lists, tick)}
        estimated = _notice(
            payload, endpoint, biggest, lists, estimate,
            lists=lists, room=None, size=size, kept_chars=cap, cap=cap, tick=tick,
        )
        tick()
        reserve = response_chars(estimated) + 25
        tick()
        target = cap - reserve
        # What fields naming one list alone leaves that list: the cap less
        # the response outside the lists and less the notice's reserve.
        room = target - shell
        ceiling = ceiling_for(target)
        previous: dict[tuple[str, ...], int] | None = None
        for _ in range(MAX_SHRINK_PASSES):
            tick()
            kept = {lst.path: lst.keep(ceiling) for lst in _each(lists, tick)}
            cut = [lst for lst in _each(lists, tick) if kept[lst.path] < lst.n]
            if not cut:
                break
            if kept == previous:
                if ceiling == 0:
                    break
                ceiling = ceiling * 9 // 10
                continue
            previous = kept
            primary = max(_each(cut, tick), key=lambda lst: lst.chars)
            replacements = {}
            for lst in _each(cut, tick):
                low, high = lst.window(kept[lst.path])
                replacements[lst.path] = lst.records[low:high]
            result = _with_lists(payload, replacements, tick)
            notice = _notice(
                payload, endpoint, primary, cut, kept,
                lists=lists, room=room, size=size, kept_chars=0, cap=cap, tick=tick,
            )
            _with_notice(result, notice)
            # kept_chars is written as 0 and measured; the true value has
            # more digits, so solve for it instead of measuring twice.
            tick()
            measured = response_chars(result)
            tick()
            kept_chars = _solve_kept_chars(measured)
            if kept_chars <= cap:
                notice["kept_chars"] = kept_chars
                return result
            target -= kept_chars - cap
            ceiling = min(ceiling_for(target), ceiling)
        # Nothing fits: either the response around its lists is too large
        # for any cut, or a record that has to stay is.
        if shell + reserve > cap:
            return _refusal(url, size, cap, _rest_hint(shell_payload, shell, tick))
        return _refuse_for_record(url, size, cap, payload, endpoint, lists, shell_payload, shell, tick)
    except _Expired:
        hint = "The response could not be cut in time."
        if endpoint is not None:
            hint += _window_only_hint(endpoint)
        return _refusal(url, size, cap, hint)


def _solve_kept_chars(measured_with_zero: int) -> int:
    """The size of a result whose kept_chars field holds that size itself,
    from its size with the field written as 0."""
    for digits in range(1, 12):
        value = measured_with_zero - 1 + digits
        if len(str(value)) == digits:
            return value
    return measured_with_zero


def _notice(
    payload: Any,
    endpoint: Any,
    primary: _List,
    cut: list[_List],
    kept: dict[tuple[str, ...], int],
    *,
    lists: list[_List],
    room: int | None,
    size: int,
    kept_chars: int,
    cap: int,
    tick: Tick,
) -> dict[str, Any]:
    notice: dict[str, Any] = {
        "reason": CUT_REASON,
        "path": _dotted(primary.path),
        "original_count": primary.n,
        "kept_count": kept[primary.path],
        "order": primary.order,
        "kept_end": primary.kept_end,
        "original_chars": size,
        "kept_chars": kept_chars,
        "cap_chars": cap,
        "retry_hint": _cut_hint(payload, endpoint, primary, cut, kept, lists, room, tick),
    }
    if len(cut) > 1:
        entries = []
        for lst in _each(cut, tick):
            entry: dict[str, Any] = {
                "path": _dotted(lst.path),
                "original_count": lst.n,
                "kept_count": kept[lst.path],
                "order": lst.order,
                "kept_end": lst.kept_end,
            }
            span = lst.kept_range(kept[lst.path])
            if span is not None:
                entry["kept_range"] = span
            entries.append(entry)
        notice["lists"] = entries
    return notice
