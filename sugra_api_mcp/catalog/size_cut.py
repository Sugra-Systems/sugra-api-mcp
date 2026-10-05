"""Cut an oversized response to the size cap, whatever its shape.

The response size gate (``client._enforce_size_limit``) measures a response
with ``client.response_chars``: the length of ``json.dumps(value,
ensure_ascii=False)`` with the default separators. A CJK or accented
character counts once instead of six times as an ASCII escape, and an ASCII
response measures exactly as it always did. A response over the cap is cut
here, and refused only when no cut can fit.

What is cut: the lists of the response, at most two levels under its
``data``, or under the payload itself when it has no ``data`` (its ``meta``
and ``_meta`` are left alone). That is ``data`` itself when it is a list,
every list at ``data.<key>`` and every list at ``data.<key>.<key>``, whatever
the key is called: ``data.data`` of a quote history, ``data.hourly`` beside
``data.daily`` of a forecast, the four ``data.<kind>.rows`` of a market
calendar. A bare array is answered as ``{"data": [...]}``, as shaping
answers it. A list inside a record is never cut, nor is a record itself, and
a cut list keeps at least one record.

How much: every record is measured once. A ceiling in characters is then
found by binary search, so that each list larger than the ceiling keeps the
records that fit under it, every smaller list stays whole, and the whole
response with its notice fits the cap. A big list beside small ones (hourly
beside daily) is the one cut; lists of like size are cut alike. The result is
measured once more to verify it. When it is still over, because the notice is
estimated before it is written, the ceiling is lowered and the result
measured again, at most MAX_SHRINK_PASSES times.

Which end is kept: the end ``limit`` keeps (``response._limit_records``),
the newest when the order of the list can be read, else the first records.
For the operations in NEAREST_END_OPERATIONS, forecasts and calendars, an
ascending list whose records carry ISO 8601 dates keeps the records from the
one nearest today onward, so a forecast keeps its next hours and not its
last ones; ``kept_end`` is then ``nearest``. Today is the UTC date, and a
caller may pass its own.

Where it runs: the whole cut runs on the shaping pool
(``tools.gateway._shape_and_gate``), within a MAX_SHAPING_SECONDS clock of
its own; past it the response is refused with ``response_too_large``. On the
event loop (the client's default path for the fixed tools, and the error
payloads call_endpoint returns as they came) only the ``data`` list is cut,
by the same measure and the same ceiling, and only in a response of at most
MAX_LOOP_CUT_CHARS characters; a larger one is refused without being walked.

What is said: ``meta.truncated`` names the cut. Its ``original_count``,
``kept_count``, ``order`` and ``kept_end`` describe the primary list, the
largest one cut, at ``path``; ``original_chars``, ``kept_chars`` and
``cap_chars`` give the sizes; ``lists`` gives the same per path when more
than one list was cut; ``retry_hint`` says what was kept and which arguments
of the tool and parameters of the operation choose a smaller answer. A
refusal says the size and the limit in characters and, when it can, where
the size is and how to leave it out.
"""

from __future__ import annotations

import json
import time
from bisect import bisect_right
from datetime import UTC, date, datetime
from typing import Any

from ..client import _unshaped_records, response_chars
from .response import (
    _DATE_KEY_CANDIDATES,
    KEPT_FIRST,
    KEPT_NEWEST,
    MAX_PROJECTION_RAW_CHARS,
    MAX_SHAPING_SECONDS,
    ORDER_ASC,
    ORDER_DESC,
    _records_key,
    _records_order,
)

KEPT_NEAREST = "nearest"
CUT_REASON = "exceeds_response_size_cap"

# Verification passes after the first estimate of the notice.
MAX_SHRINK_PASSES = 8
# On the event loop a response above this is refused without being walked.
# It is the bound a fields projection puts on the response before it, so no
# path walks a larger one.
MAX_LOOP_CUT_CHARS = MAX_PROJECTION_RAW_CHARS
# Records measured between two reads of the clock.
_CLOCK_EVERY = 256

# Forward-looking operations: their ascending lists keep the records nearest
# today onward, not the newest ones. A test checks every id against the
# bundled catalog, so a resync that renames one fails it.
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


class _Expired(Exception):
    """The cut ran past its clock."""


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


def _date_key(records: list[Any]) -> str | None:
    """The one known date key every record carries, as _records_order reads it."""
    if not records or not all(isinstance(record, dict) for record in records):
        return None
    present = [
        key for key in _DATE_KEY_CANDIDATES
        if all(record.get(key) is not None for record in records)
    ]
    return present[0] if len(present) == 1 else None


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
        deadline: float,
    ) -> None:
        self.path = path
        self.records = records
        self.n = len(records)
        # The order is read from the list as the API sent it when the caller
        # passed it: shaping may have projected the date key away.
        self.order = _records_order(source if source is not None else records)
        # The records the dates are read from: these, or the unshaped list
        # when it still matches them one for one.
        self.dated: list[Any] | None = None
        self.date_key: str | None = None
        for candidate in (records, source):
            if candidate is not None and len(candidate) == self.n:
                key = _date_key(candidate)
                if key is not None:
                    self.dated, self.date_key = candidate, key
                    break
        self.start: int | None = None
        self.tail = self.order == ORDER_ASC
        self.kept_end = KEPT_NEWEST if self.order in (ORDER_ASC, ORDER_DESC) else KEPT_FIRST
        if today is not None and self.order == ORDER_ASC and self.dated is not None:
            days = [_day(record[self.date_key]) for record in self.dated]
            if all(day is not None for day in days):
                self.start = next(
                    (index for index, day in enumerate(days) if day is not None and day >= today),
                    self.n - 1,
                )
                self.kept_end = KEPT_NEAREST
        sizes: list[int] = []
        for index, record in enumerate(records):
            if index % _CLOCK_EVERY == 0 and time.monotonic() > deadline:
                raise _Expired
            sizes.append(response_chars(record))
        if self.start is not None:
            priority = [*range(self.start, self.n), *range(self.start - 1, -1, -1)]
        elif self.tail:
            priority = list(range(self.n - 1, -1, -1))
        else:
            priority = list(range(self.n))
        # prefix[k]: the characters of the first k records kept with the
        # ", " between them, inside brackets already counted in the shell.
        self.prefix = [0]
        total = 0
        for position, index in enumerate(priority):
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
        if self.start is not None:
            if kept <= self.n - self.start:
                return self.start, self.start + kept
            return self.n - kept, self.n
        if self.tail:
            return self.n - kept, self.n
        return 0, kept

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


def _find_lists(payload: Any, *, data_only: bool) -> list[tuple[tuple[str, ...], list[Any]]]:
    """Every non-empty list at most two levels under the root, in order."""
    if not isinstance(payload, dict):
        return []
    root, prefix = _root(payload)
    if isinstance(root, list):
        return [(prefix, root)] if root and prefix else []
    if data_only or not isinstance(root, dict):
        return []
    found: list[tuple[tuple[str, ...], list[Any]]] = []
    for key, value in root.items():
        if not prefix and key in _PROVENANCE_KEYS:
            continue
        if isinstance(value, list):
            if value:
                found.append(((*prefix, key), value))
        elif isinstance(value, dict):
            for sub_key, sub_value in value.items():
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


def _with_lists(payload: dict[str, Any], replacements: dict[tuple[str, ...], list[Any]]) -> dict[str, Any]:
    """A copy of payload with the lists at the given paths replaced. Only the
    objects on those paths are copied; the payload itself is never changed."""
    out = dict(payload)
    copies: dict[tuple[str, ...], dict[str, Any]] = {(): out}
    for path, value in replacements.items():
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


def _uncut_lists(payload: Any, cut: list[_List], kept: dict[tuple[str, ...], int]) -> list[str]:
    """Keys of the root holding a list that stayed whole, when every cut list
    sits under another key of the root: fields naming them leave the cut out."""
    root, prefix = _root(payload)
    if not isinstance(root, dict):
        return []
    cut_keys = {lst.path[len(prefix)] for lst in cut if len(lst.path) > len(prefix)}
    names = []
    for key, value in root.items():
        path = (*prefix, key)
        if key in cut_keys or not isinstance(value, list) or not value:
            continue
        if kept.get(path, len(value)) == len(value):
            names.append(key)
    return names[:_MAX_NAMED_FIELDS]


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
    if count_param is not None:
        value = _clamped(params[count_param], kept[primary.path])
        levers.append(f"params.{count_param}={value}")
    window = _window_lever(params)
    if window is not None:
        levers.append(window)
    # The tool's own limit keeps the end the cut keeps, except the nearest
    # one: there it would keep the farthest records instead.
    if (
        count_param is None
        and len(cut) == 1
        and primary.kept_end != KEPT_NEAREST
        and _is_records_list(payload, primary.path)
    ):
        levers.append(f"limit={kept[primary.path]} beside params")
    whole = _uncut_lists(payload, cut, kept)
    if whole:
        levers.append(f"fields={json.dumps(whole)} for the lists kept whole")
    if levers:
        hint += " To choose what is kept, pass " + ", or ".join(levers) + "."
    return hint


def _heavy_hint(payload: Any, endpoint: Any) -> str:
    """Where most of a response without a cuttable list is, and the fields
    that leave it out."""
    root, prefix = _root(payload)
    if not isinstance(root, dict) or not root:
        return ""
    sizes = {
        key: response_chars(value) for key, value in root.items()
        if prefix or key not in _PROVENANCE_KEYS
    }
    if not sizes:
        return ""
    heavy = max(sizes, key=lambda key: sizes[key])
    heavy_path: tuple[str, ...] = (*prefix, heavy)
    heavy_chars = sizes[heavy]
    light = [key for key in sizes if key != heavy]
    inner = root[heavy]
    if isinstance(inner, dict) and inner:
        inner_sizes = {key: response_chars(value) for key, value in inner.items()}
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


def _refusal(url: str, size: int, cap: int, hint: str) -> dict[str, Any]:
    message = f"Response is about {size:,} characters; the limit is {cap:,} characters."
    if hint:
        message += " " + hint
    refusal: dict[str, Any] = {
        "error": "response_too_large",
        "message": message,
        "estimated_tokens": size // 4,
        "response_chars": size,
        "cap_chars": cap,
        "url": url,
    }
    if hint:
        refusal["retry_hint"] = hint
    return refusal


def _refuse_for_record(
    url: str, size: int, cap: int, payload: Any, endpoint: Any, lists: list[_List]
) -> dict[str, Any]:
    worst = max(lists, key=lambda lst: lst.prefix[1])
    hint = f"One record at {_dotted(worst.path)} is larger than the limit by itself."
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
    size: int,
    cap: int,
    unshaped: Any = None,
    endpoint: Any = None,
    on_loop: bool = False,
    today: date | None = None,
) -> Any:
    """Cut payload, which measures ``size`` characters, to fit ``cap``, or
    return the ``response_too_large`` refusal when no cut fits.

    ``endpoint`` is the catalog entry of the operation called through
    call_endpoint or fetch_data; its parameters, and those tools' own limit
    and fields arguments, are what the hints may name. None (the fixed tools)
    gives hints without any. ``on_loop`` selects the event-loop path: only
    the ``data`` list, and only up to MAX_LOOP_CUT_CHARS.
    """
    if on_loop and size > MAX_LOOP_CUT_CHARS:
        return _refusal(url, size, cap, "")
    if isinstance(payload, list):
        # A bare array is answered as shaping answers it, as {"data": [...]},
        # so it is cut on either path like a data list.
        payload = {"data": payload}
    deadline = time.monotonic() + MAX_SHAPING_SECONDS
    found = _find_lists(payload, data_only=on_loop)
    if not found:
        return _refusal(url, size, cap, _heavy_hint(payload, endpoint))
    nearest = endpoint is not None and getattr(endpoint, "operation_id", None) in NEAREST_END_OPERATIONS
    day = (today or _utc_today()) if nearest else None
    try:
        lists = []
        for path, records in found:
            if time.monotonic() > deadline:
                raise _Expired
            lists.append(_List(path, records, _unshaped_at(unshaped, path), day, deadline))
        shell = response_chars(_with_lists(payload, {lst.path: [] for lst in lists}))

        def total(ceiling: int) -> int:
            return shell + sum(lst.prefix[lst.keep(ceiling)] for lst in lists)

        def ceiling_for(target: int) -> int:
            low, high = 0, max(lst.chars for lst in lists)
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
        biggest = max(lists, key=lambda lst: lst.chars)
        estimate = {lst.path: lst.n for lst in lists}
        reserve = response_chars(
            _notice(payload, endpoint, biggest, lists, estimate, size=size, kept_chars=cap, cap=cap)
        ) + 25
        target = cap - reserve
        ceiling = ceiling_for(target)
        previous: dict[tuple[str, ...], int] | None = None
        for _ in range(MAX_SHRINK_PASSES):
            if time.monotonic() > deadline:
                raise _Expired
            kept = {lst.path: lst.keep(ceiling) for lst in lists}
            cut = [lst for lst in lists if kept[lst.path] < lst.n]
            if not cut:
                break
            if kept == previous:
                if ceiling == 0:
                    break
                ceiling = ceiling * 9 // 10
                continue
            previous = kept
            primary = max(cut, key=lambda lst: lst.chars)
            replacements = {}
            for lst in cut:
                low, high = lst.window(kept[lst.path])
                replacements[lst.path] = lst.records[low:high]
            result = _with_lists(payload, replacements)
            notice = _notice(payload, endpoint, primary, cut, kept, size=size, kept_chars=0, cap=cap)
            _with_notice(result, notice)
            # kept_chars is written as 0 and measured; the true value has
            # more digits, so solve for it instead of measuring twice.
            measured = response_chars(result)
            kept_chars = _solve_kept_chars(measured)
            if kept_chars <= cap:
                notice["kept_chars"] = kept_chars
                return result
            target -= kept_chars - cap
            ceiling = min(ceiling_for(target), ceiling)
        # Nothing fits: either the response around its lists is too large
        # for any cut, or a record that has to stay is.
        if shell + reserve > cap:
            return _refusal(url, size, cap, _heavy_hint(payload, endpoint))
        return _refuse_for_record(url, size, cap, payload, endpoint, lists)
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
    size: int,
    kept_chars: int,
    cap: int,
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
        "retry_hint": _cut_hint(payload, endpoint, primary, cut, kept),
    }
    if len(cut) > 1:
        entries = []
        for lst in cut:
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
