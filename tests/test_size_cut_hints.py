"""What a cut or a refusal tells the agent to do next.

A hint names only what the call can take: the parameters of the operation
called, read from the bundled catalog, and the tool's own limit and fields.
params.limit is the API's parameter; "limit=N beside params" is the tool's
own argument. A number is clamped to the parameter's maximum, and following
a hint gives a response that fits.
"""

from __future__ import annotations

import heapq
import json
import re
from datetime import timedelta
from types import SimpleNamespace

import pytest

from sugra_api_mcp.catalog.loader import load_catalog
from sugra_api_mcp.client import MAX_RESPONSE_CHARS
from tests.size_cut_bodies import (
    TODAY,
    bars,
    call,
    chars,
    events,
    indicators,
    lei_search,
    market_calendar,
    notice,
    quotes_history,
    size_cut_module,
    weather,
)


@pytest.fixture(autouse=True)
def _fixed_today(monkeypatch):
    module = size_cut_module()
    if module is not None:
        monkeypatch.setattr(module, "_utc_today", lambda: TODAY)


def _text(result: dict) -> str:
    """The advice a result carries, cut or refused."""
    if "error" in result:
        return result["message"]
    return notice(result)["retry_hint"]


_CASES = [
    ("quotes_symbol_historical", lambda: quotes_history(2000), {"symbol": "AAPL"}),
    ("market_chart_symbol", lambda: {"data": bars(2000)}, {"symbol": "AAPL"}),
    ("futures_root_historical", lambda: {"data": bars(2000)}, {"root": "CL"}),
    ("cboe_put_call_ratio_history", lambda: {"data": {"rows": bars(2000)}}, {"family": "equity"}),
    ("v2_weather_forecast", lambda: weather(TODAY, 9), {"city": "Tokyo"}),
    ("v2_weather_history", lambda: weather(TODAY - timedelta(days=30), 9),
     {"city": "Tokyo", "start_date": "2026-09-05", "end_date": "2026-09-13"}),
    ("market_calendar", lambda: market_calendar(TODAY, 8), {}),
    ("gleif_lei_search", lambda: lei_search(600), {}),
    ("predictions_events", lambda: events(1000, 1, 200), {}),
    ("kalshi_events", lambda: {"data": {"events": [{"id": i, "t": "e" * 300} for i in range(600)]}}, {}),
    ("snb_policy_rate", lambda: {"data": bars(2000)}, {}),
    ("rba_cpi", lambda: {"data": bars(2000)}, {}),
    ("macro_indicators_available", lambda: indicators(4900), {}),
    ("macro_cb_calendar", lambda: {"data": bars(2000, start=TODAY)}, {}),
]


@pytest.mark.parametrize(("operation_id", "make", "params"), _CASES, ids=[case[0] for case in _CASES])
async def test_a_hint_names_only_parameters_the_operation_has(monkeypatch, operation_id, make, params) -> None:
    endpoint = load_catalog().get(operation_id)
    declared = {parameter.name for parameter in endpoint.parameters}

    result = await call(monkeypatch, operation_id, make(), params)

    text = _text(result)
    assert chars(result) <= MAX_RESPONSE_CHARS
    assert set(re.findall(r"params\.([A-Za-z_][A-Za-z0-9_]*)", text)) <= declared
    assert "25000" not in text and "token" not in text and "filters" not in text
    if "error" not in result:
        cut = notice(result)
        assert cut["kept_count"] < cut["original_count"]


async def test_an_operation_without_parameters_gets_no_filter_advice(monkeypatch) -> None:
    result = await call(monkeypatch, "rba_cpi", {"data": bars(2000)})

    hint = notice(result)["retry_hint"]
    assert hint.endswith(" The source returns this whole dataset in one response.")
    assert "params." not in hint and "To choose" not in hint and "limit=" not in hint


async def test_a_count_parameter_is_clamped_to_its_maximum(monkeypatch) -> None:
    # page_size allows at most 200; more than 200 of these records fit.
    body = lei_search(600, width=0)
    body["data"]["records"] = [
        {"lei": record["lei"], "legal_name": record["legal_name"]} for record in body["data"]["records"]
    ]
    result = await call(monkeypatch, "gleif_lei_search", body)

    cut = notice(result)
    assert cut["kept_count"] > 200
    assert "params.page_size=200" in cut["retry_hint"]


async def test_a_limit_parameter_is_clamped_to_its_maximum(monkeypatch) -> None:
    result = await call(monkeypatch, "predictions_events", events(1000, 1, 10))

    cut = notice(result)
    assert cut["kept_count"] > 100
    assert "params.limit=100" in cut["retry_hint"]


async def test_a_count_parameter_names_the_number_that_fit(monkeypatch) -> None:
    result = await call(
        monkeypatch, "quotes_symbol_historical", quotes_history(2000), {"symbol": "AAPL"}
    )

    cut = notice(result)
    assert f"params.limit={cut['kept_count']}," in cut["retry_hint"]
    assert "a narrower params.start to params.end" in cut["retry_hint"]


async def test_the_tool_limit_named_in_a_hint_returns_a_response_that_fits(monkeypatch) -> None:
    body = {"data": bars(2000)}
    params = {"root": "CL"}

    result = await call(monkeypatch, "futures_root_historical", body, params)

    hint = notice(result)["retry_hint"]
    match = re.search(r" limit=(\d+) beside params", hint)
    assert match, hint
    again = await call(monkeypatch, "futures_root_historical", body, params, limit=int(match.group(1)))
    assert "truncated" not in again.get("meta", {})
    assert chars(again) <= MAX_RESPONSE_CHARS
    assert again["data"] == result["data"]


async def test_a_nearest_cut_does_not_offer_the_tool_limit(monkeypatch) -> None:
    # The tool's limit keeps the newest end: on a forward list it would keep
    # the farthest rows, not the ones the cut kept.
    from tests.size_cut_bodies import earnings_calendar

    result = await call(monkeypatch, "market_calendar_earnings", earnings_calendar(TODAY, 8, 300))

    hint = notice(result)["retry_hint"]
    assert "from the one nearest today" in hint
    assert "beside params" not in hint
    assert "a narrower params.from to params.to" in hint


async def test_the_weather_fields_hint_returns_a_response_that_fits(monkeypatch) -> None:
    body = weather(TODAY, 14)
    params = {"city": "Tokyo", "forecast_days": 14}

    result = await call(monkeypatch, "v2_weather_forecast", body, params)

    hint = notice(result)["retry_hint"]
    assert "a smaller params.forecast_days" in hint
    named = json.loads(re.search(r"fields=(\[[^\]]*\])", hint).group(1))
    assert named == ["daily"]
    again = await call(monkeypatch, "v2_weather_forecast", body, params, fields=named)
    assert "error" not in again
    assert "truncated" not in again.get("meta", {})
    assert again["data"]["daily"] == body["data"]["daily"]


async def test_a_cut_small_list_that_fits_alone_is_offered_by_fields(monkeypatch) -> None:
    # 16 days at the cap: the cut keeps 14 of the 16 daily rows beside the
    # hours, yet fields=["daily"] on its own returns all 16 within the cap.
    body = weather(TODAY, 16)
    params = {"city": "Tokyo", "forecast_days": 16}

    result = await call(monkeypatch, "v2_weather_forecast", body, params)

    cut = notice(result)
    daily = next(entry for entry in cut["lists"] if entry["path"] == "data.daily")
    assert daily["kept_count"] < daily["original_count"] == 16
    named = json.loads(re.search(r"fields=(\[[^\]]*\])", cut["retry_hint"]).group(1))
    assert named == ["daily"]
    assert "for the lists that fit whole on their own" in cut["retry_hint"]
    again = await call(monkeypatch, "v2_weather_forecast", body, params, fields=named)
    assert "error" not in again
    assert "truncated" not in again.get("meta", {})
    assert again["data"]["daily"] == body["data"]["daily"]
    assert chars(again) <= MAX_RESPONSE_CHARS


async def test_a_cut_list_too_big_alone_is_not_offered_by_fields(monkeypatch) -> None:
    # The hours cannot fit the cap on their own, so only the daily list is named.
    result = await call(monkeypatch, "v2_weather_forecast", weather(TODAY, 16), {"city": "Tokyo"})

    hint = notice(result)["retry_hint"]
    assert '"hourly"' not in hint


def _named_sets(hint: str) -> list[list[str]]:
    return [json.loads(found) for found in re.findall(r"fields=(\[[^\]]*\])", hint)]


async def test_two_cut_lists_that_fit_alone_but_not_together_are_each_offered(monkeypatch) -> None:
    # daily (about 9,600 characters) and notes (about 10,400) each fit the cap
    # on their own, not together: one fields set names one, a second the other.
    body = weather(TODAY, 16)
    body["data"]["notes"] = [{"id": i, "text": "n" * 150} for i in range(60)]
    params = {"city": "Tokyo", "forecast_days": 16}

    result = await call(monkeypatch, "v2_weather_forecast", body, params)

    sets = _named_sets(notice(result)["retry_hint"])
    assert sorted(sets) == [["daily"], ["notes"]]
    for names in sets:
        again = await call(monkeypatch, "v2_weather_forecast", body, params, fields=names)
        assert "error" not in again
        assert "truncated" not in again.get("meta", {})
        assert again["data"][names[0]] == body["data"][names[0]]
        assert chars(again) <= MAX_RESPONSE_CHARS


async def test_equal_cut_lists_are_named_in_the_response_key_order(monkeypatch) -> None:
    # Two cut lists of the same size: the one the response lists first is
    # named, whatever the hash seed of the process.
    body = weather(TODAY, 16)
    del body["data"]["daily"]
    rows = [{"id": i, "text": "n" * 150} for i in range(60)]
    body["data"]["zeta"] = list(rows)
    body["data"]["alpha"] = list(rows)
    params = {"city": "Tokyo", "forecast_days": 16}

    result = await call(monkeypatch, "v2_weather_forecast", body, params)

    sets = _named_sets(notice(result)["retry_hint"])
    assert sets == [["zeta"], ["alpha"]]


async def test_cut_lists_past_the_named_set_are_offered_each_on_their_own(monkeypatch) -> None:
    # Fourteen like lists, all cut, each fitting alone: the set of six is
    # full before the room is, so the next six are offered one by one, in
    # the response's key order, and only the last two past both bounds go
    # unnamed.
    keys = [f"list{i:02d}" for i in range(14)]
    body = {"data": {key: [{"id": i, "text": "n" * 150} for i in range(10)] for key in keys}}
    params = {"city": "Tokyo", "forecast_days": 16}

    result = await call(monkeypatch, "v2_weather_forecast", body, params)

    sets = _named_sets(notice(result)["retry_hint"])
    assert sets == [keys[:6], *([key] for key in keys[6:12])]


class _Visits(dict):
    """A dict that counts every key its iteration hands out."""

    def __init__(self, *args) -> None:
        super().__init__(*args)
        self.visits = 0
        self.trace: list[str] | None = None

    def _visit(self) -> None:
        self.visits += 1
        if self.trace is not None:
            self.trace.append("root")

    def __iter__(self):
        for key in super().__iter__():
            self._visit()
            yield key

    def items(self):
        for item in super().items():
            self._visit()
            yield item


class _OutOfTime(Exception):
    pass


def _trace_ranking(monkeypatch, module, root: _Visits) -> list[str]:
    """A trace of the root visits and of every cut key the ranking of the cut
    lists takes, in the order they happen; the caller's tick adds its own."""
    trace: list[str] = []
    root.trace = trace
    real_nsmallest = heapq.nsmallest

    def taken(candidates):
        for candidate in candidates:
            trace.append("cut")
            yield candidate

    def nsmallest(n, candidates, **kwargs):
        return real_nsmallest(n, taken(candidates), **kwargs)

    monkeypatch.setattr(module, "heapq", SimpleNamespace(nsmallest=nsmallest))
    return trace


def _wide_root(module, tick) -> tuple[_Visits, list]:
    # 5,000 uncut lists before 30 cut ones that grow by one row each.
    root = _Visits({f"w{i:04d}": [i] for i in range(5_000)})
    for i in range(30):
        root[f"c{i:02d}"] = [{"id": j, "text": "n" * 150} for j in range(i + 1)]
    cut = [
        module._List(("data", f"c{i:02d}"), root[f"c{i:02d}"], None, None, tick)
        for i in range(30)
    ]
    root.visits = 0
    return root, cut


def test_the_fields_offer_over_a_wide_root_reads_the_clock_and_sorts_only_its_bound(
    monkeypatch,
) -> None:
    module = size_cut_module()
    bound = module._MAX_NAMED_FIELDS
    sorted_lengths: list[int] = []
    real_sorted = sorted

    def spy(items, **kwargs):
        items = list(items)
        sorted_lengths.append(len(items))
        return real_sorted(items, **kwargs)

    monkeypatch.setattr(module, "sorted", spy, raising=False)
    root, cut = _wide_root(module, lambda: None)
    trace = _trace_ranking(monkeypatch, module, root)

    groups = module._fit_whole_alone({"data": root}, cut, cut, {}, 10_000, lambda: trace.append("tick"))

    # Every key of the root and every cut key the ranking takes has a reading
    # of the clock of its own: a root key is read just before its tick, a cut
    # key just after it. Nothing longer than one bounded set is sorted.
    assert root.visits == len(root)
    assert trace.count("cut") == len(cut)
    for index, event in enumerate(trace):
        if event == "root":
            assert trace[index + 1] == "tick", f"root visit {index} has no tick of its own"
        elif event == "cut":
            assert trace[index - 1] == "tick", f"cut key {index} has no tick of its own"
    assert sorted_lengths and max(sorted_lengths) <= bound
    assert groups == [[f"w{i:04d}" for i in range(bound)], *([f"c{i:02d}"] for i in range(bound))]


@pytest.mark.parametrize("budget", [10, 1_000, 4_999, 5_020])
def test_the_fields_offer_over_a_wide_root_stops_when_the_clock_runs_out(budget) -> None:
    module = size_cut_module()
    root, cut = _wide_root(module, lambda: None)
    ticks = 0

    def tick() -> None:
        nonlocal ticks
        ticks += 1
        if ticks > budget:
            raise _OutOfTime

    with pytest.raises(_OutOfTime):
        module._fit_whole_alone({"data": root}, cut, cut, {}, 10_000, tick)
    assert root.visits <= budget + 1


def test_the_fields_offer_stops_while_it_ranks_the_cut_lists_when_the_clock_runs_out(
    monkeypatch,
) -> None:
    # A first run counts the ticks the passes before the ranking take; a
    # budget a few ticks past them runs out while the cut lists are ranked.
    module = size_cut_module()
    root, cut = _wide_root(module, lambda: None)
    trace = _trace_ranking(monkeypatch, module, root)
    module._fit_whole_alone({"data": root}, cut, cut, {}, 10_000, lambda: trace.append("tick"))
    before_ranking = trace[: trace.index("cut")].count("tick") - 1
    ranking = before_ranking + len(cut)
    budget = before_ranking + 5
    assert root.visits == len(root) and budget < ranking

    root, cut = _wide_root(module, lambda: None)
    trace = _trace_ranking(monkeypatch, module, root)
    ticks = 0

    def tick() -> None:
        nonlocal ticks
        ticks += 1
        if ticks > budget:
            raise _OutOfTime

    with pytest.raises(_OutOfTime):
        module._fit_whole_alone({"data": root}, cut, cut, {}, 10_000, tick)
    # The root walk finished, and the ranking stopped after the cut keys the
    # budget left room for, not after all of them.
    assert root.visits == len(root)
    assert trace.count("cut") == budget - before_ranking


def test_a_cut_list_past_the_room_of_the_first_set_is_offered_alone_before_larger_ones() -> None:
    # One, two, four and five rows: the first two fit together, the four-row
    # list does not fit beside them but fits alone, and so does the five-row
    # one; each of those is a set of its own, the smaller first.
    module = size_cut_module()
    rows = {"a": 1, "b": 2, "c": 4, "d": 5}
    root = {key: [{"id": j, "text": "n" * 150} for j in range(n)] for key, n in rows.items()}
    cut = [module._List(("data", key), root[key], None, None, lambda: None) for key in root]
    room = next(lst.chars for lst in cut if lst.path[-1] == "d")

    groups = module._fit_whole_alone({"data": root}, cut, cut, {}, room, lambda: None)

    assert groups == [["a", "b"], ["c"], ["d"]]


async def test_a_cut_of_several_lists_says_how_many(monkeypatch) -> None:
    result = await call(monkeypatch, "market_calendar", market_calendar(TODAY, 8))

    cut = notice(result)
    assert f" {len(cut['lists']) - 1} more lists were cut, see lists." in cut["retry_hint"]
    assert "a narrower params.start_date to params.end_date" in cut["retry_hint"]
