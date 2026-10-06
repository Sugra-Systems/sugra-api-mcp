"""The bounds the size gate keeps around a cut.

The fit is measured with a count that stops at the cap; nothing is
serialised whole on the event loop; a cut keeps the records nearest today in
a forecast or calendar in either order; the clock of a cut is read between
records, before anything is measured; every refusal fits the cap itself;
and a refusal names a record only when that record is what leaves no room.
"""

from __future__ import annotations

import asyncio
import importlib
import json
import math
import random
import threading
import time
from datetime import date, timedelta

import httpx
import pytest

from sugra_api_mcp import client as client_module
from sugra_api_mcp.client import (
    MAX_RESPONSE_CHARS,
    SugraClient,
    _enforce_size_limit,
    response_chars_within,
)
from sugra_api_mcp.config import Config
from tests.size_cut_bodies import TODAY, bars, call, chars, notice, size_cut_module

CAP = MAX_RESPONSE_CHARS
_ASTRAL = "\U00010348"


def _module():
    module = size_cut_module()
    assert module is not None
    return module


@pytest.fixture(autouse=True)
def _fixed_today(monkeypatch):
    module = size_cut_module()
    if module is not None:
        monkeypatch.setattr(module, "_utc_today", lambda: TODAY)


class _Endpoint:
    """The two attributes of a catalog entry the cut reads."""

    def __init__(self, operation_id: str) -> None:
        self.operation_id = operation_id
        self.parameters: list = []


FORECAST = _Endpoint("weather_forecast")


def _fits(result) -> bool:
    return chars(result) <= CAP


# ---- the bounded measure ----


def _random_json(rng: random.Random, depth: int = 0):
    kind = rng.randrange(9 if depth < 4 else 6)
    if kind == 0:
        return rng.choice((None, True, False))
    if kind == 1:
        return rng.choice((0, -1, 7, 10**30, -(10**18)))
    if kind == 2:
        return rng.choice((0.0, -0.0, 1.5, 1e15, 1e16, 1e-7, math.nan, math.inf, -math.inf, 0.1))
    if kind in (3, 4):
        parts = ("a", "é", "東", _ASTRAL, "\n", "\x01", '"', "\\", "/", " ")
        return "".join(rng.choice(parts) for _ in range(rng.randrange(12)))
    if kind == 5:
        return ""
    if kind == 6:
        return [_random_json(rng, depth + 1) for _ in range(rng.randrange(5))]
    if kind == 7:
        return {f"k{rng.randrange(99)}é": _random_json(rng, depth + 1) for _ in range(rng.randrange(5))}
    keys = (1, -2, 1.5, math.inf, True, False, None, "s")
    return {rng.choice(keys): _random_json(rng, depth + 1) for _ in range(rng.randrange(5))}


def test_the_bounded_measure_is_exactly_json_dumps() -> None:
    rng = random.Random(4062)
    for trial in range(2000):
        value = _random_json(rng)
        exact = len(json.dumps(value))

        assert response_chars_within(value, 10**9) == exact, trial
        assert response_chars_within(value, exact) == exact, trial
        assert response_chars_within(value, exact - 1) is None, trial


def test_the_bounded_measure_stops_at_its_budget(monkeypatch) -> None:
    encoded: list[int] = []
    real = client_module._encode_string

    def spy(text: str) -> str:
        encoded.append(len(text))
        return real(text)

    monkeypatch.setattr(client_module, "_encode_string", spy)

    assert response_chars_within({"data": ["x" * 10_000_000]}, CAP) is None
    assert max(encoded) < 100
    encoded.clear()
    assert response_chars_within({"data": ["abc"] * 1_000_000}, CAP) is None
    # Each "abc" adds seven characters with its separator: the walk stops
    # after about CAP / 7 of the million.
    assert len(encoded) < CAP // 7 + 10


# ---- nothing serialised whole on the event loop ----


def _serving_bytes(content: bytes, status: int = 200) -> SugraClient:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            status, content=content, headers={"content-type": "application/json"}, request=request
        )

    config = Config(api_base="https://api.test", api_key="test-key", timeout=5.0)
    return SugraClient(config, transport=httpx.MockTransport(handler))


def _spy_dumps(monkeypatch) -> list[tuple[bool, str, int]]:
    seen: list[tuple[bool, str, int]] = []
    real = json.dumps

    def spy(value, *args, **kwargs):
        text = real(value, *args, **kwargs)
        seen.append((client_module._on_event_loop(), threading.current_thread().name, len(text)))
        return text

    monkeypatch.setattr(json, "dumps", spy)
    return seen


async def test_an_oversized_fixed_tool_response_is_never_serialised_on_the_event_loop(monkeypatch) -> None:
    body = {"data": {"symbol": "AAPL", "data": bars(30_000)}, "meta": {}}
    client = _serving_bytes(json.dumps(body).encode())
    seen = _spy_dumps(monkeypatch)
    try:
        result = await client.get("/api/v2/quotes/AAPL/historical")
    finally:
        await client.aclose()
    monkeypatch.undo()

    assert notice(result)["path"] == "data.data"
    assert _fits(result)
    assert [entry for entry in seen if entry[0]] == []
    assert any(name.startswith("response-shaping") for _, name, _ in seen)


async def test_a_fixed_tool_response_that_fits_is_answered_on_the_loop_unserialised(monkeypatch) -> None:
    body = {"data": bars(50), "meta": {}}
    client = _serving_bytes(json.dumps(body).encode())
    seen = _spy_dumps(monkeypatch)
    try:
        result = await client.get("/api/v2/quotes/AAPL/historical")
    finally:
        await client.aclose()
    monkeypatch.undo()

    assert result == body
    assert seen == []


async def test_an_oversized_error_payload_is_refused_on_the_loop_unserialised(monkeypatch) -> None:
    client = _serving_bytes(json.dumps({"error": "x" * 300_000}).encode(), status=500)
    monkeypatch.setattr("sugra_api_mcp.tools.gateway.get_client", lambda: client)
    seen = _spy_dumps(monkeypatch)
    from sugra_api_mcp.tools import gateway

    try:
        result = await gateway.call_endpoint("quotes_symbol_historical", params={"symbol": "AAPL"})
    finally:
        await client.aclose()
    on_loop = [entry for entry in seen if entry[0] and entry[2] > CAP]
    monkeypatch.undo()

    assert result["error"] == "response_too_large"
    assert result["response_chars"] is None
    assert _fits(result)
    assert on_loop == []


# ---- nearest today, in either order ----


def _hours(first: date, days: int, width: int = 300) -> list[dict]:
    return [
        {"time": f"{(first + timedelta(days=d)).isoformat()}T{h:02d}:00", "v": "x" * width}
        for d in range(days)
        for h in range(24)
    ]


def _day_of(record: dict) -> date:
    return date.fromisoformat(record["time"][:10])


def _cut_hours(records: list[dict]) -> tuple[dict, list[dict]]:
    payload = {"data": {"hourly": records}, "meta": {}}
    result = _module().cut_to_fit(payload, "test://nearest", cap=CAP, endpoint=FORECAST, today=TODAY)
    assert "error" not in result
    assert _fits(result)
    return notice(result), result["data"]["hourly"]


def _is_slice(kept: list, whole: list) -> bool:
    start = whole.index(kept[0])
    return whole[start:start + len(kept)] == kept


def test_an_ascending_forecast_keeps_today_onward() -> None:
    records = _hours(TODAY - timedelta(days=10), 21)

    cut, kept = _cut_hours(records)

    assert cut["order"] == "asc" and cut["kept_end"] == "nearest"
    assert kept[0]["time"] == f"{TODAY.isoformat()}T00:00"
    assert all(_day_of(record) >= TODAY for record in kept)
    assert _is_slice(kept, records)


def test_a_descending_forecast_keeps_today_onward_too() -> None:
    records = _hours(TODAY - timedelta(days=10), 21)[::-1]

    cut, kept = _cut_hours(records)

    assert cut["order"] == "desc" and cut["kept_end"] == "nearest"
    assert kept[-1]["time"] == f"{TODAY.isoformat()}T00:00"
    assert all(_day_of(record) >= TODAY for record in kept)
    assert _is_slice(kept, records)
    assert cut["kept_count"] < len(records)


def test_both_orders_keep_the_same_records_when_the_future_is_short() -> None:
    ascending = _hours(TODAY - timedelta(days=15), 18)
    cut_up, kept_up = _cut_hours(ascending)
    cut_down, kept_down = _cut_hours(ascending[::-1])

    future = [record for record in ascending if _day_of(record) >= TODAY]
    assert cut_up["kept_end"] == cut_down["kept_end"] == "nearest"
    assert kept_up == kept_down[::-1]
    # Every future hour, then the nearest past ones.
    assert kept_up[-len(future):] == future
    assert kept_up == ascending[-len(kept_up):]


def test_a_list_wholly_past_or_wholly_ahead_keeps_the_record_nearest_today() -> None:
    past = _hours(TODAY - timedelta(days=30), 20)
    ahead = _hours(TODAY + timedelta(days=1), 20)

    _, kept = _cut_hours(past)
    assert kept[-1] == past[-1]
    _, kept = _cut_hours(past[::-1])
    assert kept[0] == past[-1]
    _, kept = _cut_hours(ahead)
    assert kept[0] == ahead[0]
    _, kept = _cut_hours(ahead[::-1])
    assert kept[-1] == ahead[0]


async def test_a_descending_forecast_through_call_endpoint_keeps_today_onward(monkeypatch) -> None:
    from tests.size_cut_bodies import weather

    body = weather(TODAY - timedelta(days=2), 9)
    body["data"]["hourly"].reverse()

    result = await call(monkeypatch, "v2_weather_forecast", body, {"city": "Tokyo"})

    cut = notice(result)
    assert cut["path"] == "data.hourly" and cut["order"] == "desc" and cut["kept_end"] == "nearest"
    assert result["data"]["hourly"][-1]["time"] == f"{TODAY.isoformat()}T00:00"
    assert _fits(result)


# ---- the clock, read between records ----


class _Clock:
    """A clock that reads 0 until its expire_at-th reading, then far later."""

    def __init__(self, expire_at: int | None) -> None:
        self.expire_at = expire_at
        self.reads = 0
        self.expired = False

    def __call__(self) -> float:
        self.reads += 1
        if self.expire_at is not None and self.reads >= self.expire_at:
            self.expired = True
            return 1e12
        return 0.0


def _clocked_payload() -> dict:
    return {"data": {"hourly": _hours(TODAY - timedelta(days=5), 40, width=60), "units": {}}, "meta": {}}


def _run_clocked(monkeypatch, expire_at: int | None) -> tuple[dict, _Clock, list[bool]]:
    module = _module()
    clock = _Clock(expire_at)
    measured: list[bool] = []
    real = module.response_chars

    def counting(value):
        measured.append(clock.expired)
        return real(value)

    monkeypatch.setattr(module, "_clock", clock)
    monkeypatch.setattr(module, "response_chars", counting)
    result = module.cut_to_fit(_clocked_payload(), "test://clock", cap=CAP, endpoint=FORECAST, today=TODAY)
    return result, clock, measured


def test_the_clock_is_read_at_every_record_of_every_scan(monkeypatch) -> None:
    result, clock, _ = _run_clocked(monkeypatch, None)

    records = len(_clocked_payload()["data"]["hourly"])
    assert "error" not in result
    # The order scan, the date scan and the sizing each read it per record.
    assert clock.reads >= 3 * records


def test_a_clock_that_runs_out_mid_scan_stops_the_cut_between_records(monkeypatch) -> None:
    _, full, measured_full = _run_clocked(monkeypatch, None)
    monkeypatch.undo()
    records = len(_clocked_payload()["data"]["hourly"])
    assert len(measured_full) > records
    points = sorted({2, 3, full.reads // 8, full.reads // 4, full.reads // 2, full.reads - 2})
    for point in points:
        result, clock, measured = _run_clocked(monkeypatch, point)
        monkeypatch.undo()

        assert clock.expired, point
        assert result["error"] == "response_too_large", point
        assert "could not be cut in time" in result["message"], point
        assert _fits(result), point
        # Nothing is measured once the clock has run out.
        assert not any(measured), point


@pytest.mark.parametrize(
    ("owner", "name"),
    [
        # The order scan parses each date; the date scan reads each day; the
        # sizing measures each record.
        ("sugra_api_mcp.catalog.response", "_iso_moment"),
        ("sugra_api_mcp.catalog.size_cut", "_day"),
        ("sugra_api_mcp.catalog.size_cut", "response_chars"),
    ],
)
def test_a_clock_that_runs_out_mid_scan_stops_that_scan(monkeypatch, owner, name) -> None:
    module = _module()
    target = importlib.import_module(owner)
    real = getattr(target, name)
    records = len(_clocked_payload()["data"]["hourly"])

    def run(expire_at: int | None) -> tuple[dict, list[int]]:
        clock = _Clock(expire_at)
        readings: list[int] = []

        def counting(*args):
            readings.append(clock.reads)
            return real(*args)

        monkeypatch.setattr(module, "_clock", clock)
        monkeypatch.setattr(target, name, counting)
        result = module.cut_to_fit(_clocked_payload(), "test://clock", cap=CAP, endpoint=FORECAST, today=TODAY)
        monkeypatch.undo()
        return result, readings

    _, readings = run(None)
    assert len(readings) >= records
    # The reading taken just before the call in the middle of the scan.
    result, calls = run(readings[records // 2])

    assert "could not be cut in time" in result["message"]
    assert 0 < len(calls) < records
    assert result["response_chars"] is None
    assert _fits(result)


# ---- every refusal fits the cap ----


def test_a_refusal_naming_a_huge_key_still_fits() -> None:
    key = "k" * 100_000
    payload = {"data": {key: "x" * 90_000, "small": 1}}

    result = _enforce_size_limit(payload, "test://key")

    assert result["error"] == "response_too_large"
    assert _fits(result)
    assert "retry_hint" not in result
    assert result["message"] == f"Response is about {chars(payload):,} characters; the limit is 85,000 characters."


def test_a_refusal_for_a_huge_url_still_fits() -> None:
    url = "https://api.test/" + "p" * 200_000
    payload = {"data": {"blob": "x" * 200_000}}

    result = _enforce_size_limit(payload, url)

    assert result["error"] == "response_too_large"
    assert _fits(result)
    assert result["url"].startswith("https://api.test/ppp") and result["url"].endswith("...")
    assert len(result["url"]) == 303


def test_a_list_under_a_huge_key_is_refused_within_the_cap() -> None:
    payload = {"data": {"k" * 100_000: bars(3000)}, "meta": {}}

    result = _enforce_size_limit(payload, "test://key-list")

    assert result["error"] == "response_too_large"
    assert _fits(result)


def test_no_refusal_or_cut_is_ever_over_the_cap() -> None:
    rng = random.Random(4063)
    for trial in range(40):
        key = "q" * rng.choice((3, 40_000, 120_000))
        url = "https://api.test/" + "u" * rng.choice((10, 90_000))
        shape = rng.randrange(3)
        if shape == 0:
            payload = {"data": {key: "x" * rng.choice((10, 100_000))}}
        elif shape == 1:
            payload = {"data": {key: bars(rng.choice((10, 2000)))}, "meta": {}}
        else:
            payload = {"data": bars(rng.choice((10, 2000))), "meta": {key: 1}}

        result = _enforce_size_limit(payload, url, endpoint=FORECAST if trial % 2 else None)

        assert _fits(result), trial


# ---- what a refusal names ----


def test_a_large_rest_of_the_response_is_named_not_a_record() -> None:
    payload = {"data": bars(2000), "meta": {"notes": "n" * 86_000}}

    result = _module().cut_to_fit(payload, "test://rest", cap=CAP)

    assert result["error"] == "response_too_large"
    assert "One record" not in result["message"]
    assert "Outside its lists the response is about " in result["retry_hint"]
    assert "most of it is at meta, about " in result["retry_hint"]


def test_records_that_each_fit_beside_the_rest_are_not_blamed() -> None:
    row = {"date": "2026-10-01", "blob": "y" * 25_000}
    payload = {"data": {"a": [row, row], "b": [row, row]}, "meta": {"notes": "n" * 40_000}}

    result = _module().cut_to_fit(payload, "test://rest", cap=CAP)

    assert result["error"] == "response_too_large"
    assert "One record" not in result["message"]
    assert "most of it is at meta, about 40,0" in result["retry_hint"]


def test_the_record_named_is_the_largest_one_not_the_first_kept() -> None:
    # Neither list has an order, so each keeps its first record, and the
    # two first records together are over the cap.
    first = {"id": 1, "blob": "f" * 50_000}
    huge = {"id": 2, "blob": "h" * 200_000}
    other = {"id": 3, "blob": "o" * 60_000}
    payload = {"data": {"a": [first, huge], "b": [other, other]}}

    result = _module().cut_to_fit(payload, "test://worst", cap=CAP)

    assert result["retry_hint"].startswith("One record at data.a is larger than the limit by itself")


def test_a_record_that_does_not_fit_beside_the_rest_is_named_with_both_sizes() -> None:
    row = {"date": "2026-10-01", "blob": "y" * 30_000}
    payload = {"data": [row, row, row], "meta": {"notes": "n" * 60_000}}

    result = _module().cut_to_fit(payload, "test://record", cap=CAP)

    shell = chars({"data": [], "meta": payload["meta"]})
    assert result["retry_hint"] == (
        f"One record at data, about {chars(row):,} characters, does not fit beside "
        f"the rest of the response, about {shell:,} characters."
    )


# ---- a fixed tool's failure is gated too ----


async def test_an_oversized_fixed_tool_error_is_gated_off_the_loop(monkeypatch) -> None:
    client = _serving_bytes(json.dumps({"error": "x" * 300_000}).encode(), status=500)
    seen = _spy_dumps(monkeypatch)
    try:
        result = await client.get("/api/v2/quotes/AAPL/historical")
    finally:
        await client.aclose()
    monkeypatch.undo()

    assert result["error"] == "response_too_large"
    assert result["response_chars"] > CAP
    assert "Most of it is at error" in result["message"]
    assert _fits(result)
    assert [entry for entry in seen if entry[0] and entry[2] > CAP] == []
    assert any(name.startswith("response-shaping") for _, name, _ in seen)


async def test_a_fixed_tool_error_that_fits_is_answered_as_built() -> None:
    client = _serving_bytes(json.dumps({"error": "not found"}).encode(), status=404)
    try:
        result = await client.get("/api/v2/quotes/NOPE/historical")
    finally:
        await client.aclose()

    assert result["error"] == "not found"
    assert result["status_code"] == 404


# ---- the caller's wait is the hard bound ----


async def test_a_cut_that_blocks_past_its_clock_is_answered_within_the_grace(monkeypatch) -> None:
    import time

    from sugra_api_mcp.catalog import response as shaping
    from sugra_api_mcp.tools import gateway

    release = threading.Event()
    entered = threading.Event()

    def blocking(*args, **kwargs):
        entered.set()
        release.wait(10)
        return {"data": "late"}

    monkeypatch.setattr(_module(), "cut_to_fit", blocking)
    monkeypatch.setattr(shaping, "MAX_SHAPING_SECONDS", 0.2)
    monkeypatch.setattr(client_module, "CUT_GRACE_SECONDS", 0.1)
    # The first wait (0.2 s) shorter than the run bound (0.3 s), as the
    # shipped numbers have it.
    monkeypatch.setattr(gateway, "SHAPING_WAIT_SECONDS", 0.1)
    body = {"data": {"symbol": "AAPL", "data": bars(30_000)}, "meta": {}}
    client = _serving_bytes(json.dumps(body).encode())
    try:
        started = time.monotonic()
        result = await client.get("/api/v2/quotes/AAPL/historical")
        waited = time.monotonic() - started
    finally:
        release.set()
        await client.aclose()

    assert entered.is_set()
    assert result["error"] == "response_too_large"
    assert result["message"] == "Response is over the limit of 85,000 characters."
    assert result["response_chars"] is None
    # The clock plus the grace, and the request and the fit check around it;
    # without the bound the answer would wait the ten seconds of the block.
    assert waited < 2.0
    # The worker finishes in the background and gives its slot back.
    deadline = time.monotonic() + 5
    while gateway.shaping_pending() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert gateway.shaping_pending() == 0


# ---- a dict with many keys is clocked key by key ----


class _CountingDict(dict):
    """A dict that counts the items its items() has handed out."""

    visited = 0

    def items(self):
        for key, value in super().items():
            type(self).visited += 1
            yield key, value


@pytest.mark.parametrize("nested", [False, True])
def test_a_clock_that_runs_out_among_many_keys_stops_the_search_for_lists(
    monkeypatch, nested
) -> None:
    module = _module()
    many = _CountingDict((f"k{i}", i) for i in range(200_000))
    _CountingDict.visited = 0
    data = {"inner": many} if nested else many
    clock = _Clock(1_000)
    monkeypatch.setattr(module, "_clock", clock)

    result = module.cut_to_fit({"data": data}, "test://keys", cap=CAP)

    assert clock.expired
    assert "could not be cut in time" in result["message"]
    assert _CountingDict.visited < 1_100


def test_the_priority_replacement_and_notice_passes_are_clocked(monkeypatch) -> None:
    import sys

    from tests.size_cut_bodies import market_calendar

    module = _module()
    real = module._each
    passes: set[tuple[str, str]] = set()

    def spy(items, tick):
        assert tick is not None
        first = items[0] if isinstance(items, list) and items else None
        passes.add((sys._getframe(1).f_code.co_name, type(first).__name__))
        return real(items, tick)

    monkeypatch.setattr(module, "_each", spy)
    body = market_calendar(TODAY - timedelta(days=2), 8)
    result = module.cut_to_fit(body, "test://passes", cap=CAP, endpoint=_Endpoint("market_calendar"), today=TODAY)

    assert len(notice(result)["lists"]) > 1
    # The nearest priority, the prefix sums over it, the replacements and
    # the per-list entries of the notice each read the clock.
    assert ("_nearest_priority", "date") in passes
    assert ("__init__", "int") in passes
    assert ("_with_lists", "NoneType") in passes
    assert ("_notice", "_List") in passes


# ---- call_endpoint's wait on the pool is bounded too ----

QUOTES = "quotes_symbol_historical"
QUOTES_PARAMS = {"symbol": "AAPL"}


class _Cutter:
    """A cutter that sleeps for seconds, or else blocks until released (only
    its first `blocked` runs, when given), counting its runs and the most
    that ran at once. A run past the first `blocked` waits for `hold`, when
    one is set, so a test can read the pool while that run still holds its
    slot."""

    def __init__(self, seconds: float | None, blocked: int | None) -> None:
        self.release = threading.Event()
        self.hold: threading.Event | None = None
        self.seconds = seconds
        self.blocked = blocked
        self.runs = 0
        self.running = 0
        self.peak = 0
        self._lock = threading.Lock()

    def __call__(self, *args, **kwargs):
        with self._lock:
            self.runs += 1
            run = self.runs
            self.running += 1
            self.peak = max(self.peak, self.running)
        try:
            if self.seconds is not None:
                time.sleep(self.seconds)
                return {"data": "cut in time"}
            if self.blocked is None or run <= self.blocked:
                self.release.wait(10)
                return {"data": "late"}
            if self.hold is not None:
                self.hold.wait(10)
            return {"data": "cut in time"}
        finally:
            with self._lock:
                self.running -= 1


def _cut_that_blocks(monkeypatch, seconds: float | None = None, blocked: int | None = None) -> _Cutter:
    cutter = _Cutter(seconds, blocked)
    monkeypatch.setattr(_module(), "cut_to_fit", cutter)
    return cutter


async def _calls(monkeypatch, count: int) -> list:
    """count call_endpoint calls at once over one mock client."""
    from sugra_api_mcp.tools import gateway
    from tests.size_cut_bodies import client_serving, quotes_history

    # 2,000 bars are about three times the cap and quick to shape, so the
    # time the tests read is the cutter's.
    client = client_serving(quotes_history(2_000))
    monkeypatch.setattr(gateway, "get_client", lambda: client)
    try:
        return await asyncio.gather(
            *(gateway.call_endpoint(QUOTES, params=QUOTES_PARAMS) for _ in range(count))
        )
    finally:
        await client.aclose()


async def _pool_drained() -> int:
    """The shaping jobs still held once the background workers have finished, at most 5 s on."""
    from sugra_api_mcp.tools import gateway

    deadline = time.monotonic() + 5
    while gateway.shaping_pending() and time.monotonic() < deadline:
        await asyncio.sleep(0.01)
    return gateway.shaping_pending()


def _bounds(monkeypatch, clock: float, grace: float, wait: float) -> None:
    from sugra_api_mcp.catalog import response as shaping
    from sugra_api_mcp.tools import gateway

    monkeypatch.setattr(shaping, "MAX_SHAPING_SECONDS", clock)
    monkeypatch.setattr(client_module, "CUT_GRACE_SECONDS", grace)
    monkeypatch.setattr(gateway, "SHAPING_WAIT_SECONDS", wait)


async def test_a_call_endpoint_cut_that_blocks_is_answered_within_its_bound(monkeypatch) -> None:
    # First wait 0.2 s; run bound two clocks and the grace, 0.5 s.
    _bounds(monkeypatch, clock=0.2, grace=0.1, wait=0.1)
    cutter = _cut_that_blocks(monkeypatch)
    try:
        started = time.monotonic()
        (result,) = await _calls(monkeypatch, 1)
        waited = time.monotonic() - started
    finally:
        cutter.release.set()

    assert result["error"] == "response_too_large"
    assert result["message"] == "Response is over the limit of 85,000 characters."
    assert result["response_chars"] is None
    # Without the bound the answer would wait the ten seconds of the block.
    assert waited < 2.0
    # The worker finishes in the background and gives its slot back.
    assert await _pool_drained() == 0
    assert cutter.runs == 1


async def test_call_endpoint_waits_for_the_projection_clock_and_then_the_cut_clock(monkeypatch) -> None:
    _bounds(monkeypatch, clock=1.0, grace=0.1, wait=0.1)
    _cut_that_blocks(monkeypatch, seconds=1.5)

    (result,) = await _calls(monkeypatch, 1)

    # Past one clock and the grace (1.1 s), within two clocks and the grace
    # (2.1 s): the job's own answer comes back.
    assert result == {"data": "cut in time"}
    assert await _pool_drained() == 0


async def test_a_started_jobs_bound_counts_from_its_start(monkeypatch) -> None:
    # First wait 0.6 s, run bound 1.5 s from the start: a job of 1.8 s is
    # past it, and would be within it counted from the end of the first wait.
    _bounds(monkeypatch, clock=0.6, grace=0.3, wait=0.3)
    cutter = _cut_that_blocks(monkeypatch, seconds=1.8)

    (result,) = await _calls(monkeypatch, 1)

    assert result["error"] == "response_too_large"
    assert await _pool_drained() == 0
    assert cutter.runs == 1


async def test_a_cut_queued_behind_busy_workers_answers_server_busy_and_never_runs(monkeypatch) -> None:
    from sugra_api_mcp.tools import gateway

    _bounds(monkeypatch, clock=0.2, grace=0.1, wait=0.2)
    cutter = _cut_that_blocks(monkeypatch)
    try:
        # Each of these holds a worker past its own caller's bound.
        held = await _calls(monkeypatch, gateway.SHAPING_WORKERS)
        started = time.monotonic()
        (queued,) = await _calls(monkeypatch, 1)
        waited = time.monotonic() - started
        # Its slot is freed at once, while the held workers still run.
        deadline = time.monotonic() + 1
        while gateway.shaping_pending() > gateway.SHAPING_WORKERS and time.monotonic() < deadline:
            await asyncio.sleep(0.01)
        pending_after_busy = gateway.shaping_pending()
    finally:
        cutter.release.set()

    assert [result["error"] for result in held] == ["response_too_large"] * gateway.SHAPING_WORKERS
    # Admitted to the pool but queued behind the held workers: refused once
    # the first wait is over, and never run, even once the workers are free.
    assert queued["error"] == "server_busy" and queued["scope"] == "shaping"
    assert waited < 2.0
    assert pending_after_busy == gateway.SHAPING_WORKERS
    assert await _pool_drained() == 0
    assert cutter.runs == gateway.SHAPING_WORKERS
    assert cutter.peak <= gateway.SHAPING_WORKERS


async def test_a_job_that_starts_as_its_first_wait_ends_is_waited_for_as_started(monkeypatch) -> None:
    from sugra_api_mcp.tools import gateway

    _bounds(monkeypatch, clock=0.2, grace=0.1, wait=0.2)
    cutter = _cut_that_blocks(monkeypatch, blocked=gateway.SHAPING_WORKERS)
    # The raced job stays in its run until its slot is read: run free, it
    # can finish and give the slot back before the read.
    cutter.hold = threading.Event()
    real_abandon = client_module._StartGate.abandon
    pending_at_start: list[int] = []

    def abandon(gate):
        if gate.started_at is None:
            # The queued job's first wait is over. Free the held workers and
            # let it start before its caller asks: the order the race allows.
            cutter.release.set()
            deadline = time.monotonic() + 5
            while gate.started_at is None and time.monotonic() < deadline:
                time.sleep(0.001)
            pending_at_start.append(gateway.shaping_pending())
            cutter.hold.set()
        return real_abandon(gate)

    monkeypatch.setattr(client_module._StartGate, "abandon", abandon)
    try:
        held = await _calls(monkeypatch, gateway.SHAPING_WORKERS)
        (raced,) = await _calls(monkeypatch, 1)
    finally:
        cutter.release.set()
        cutter.hold.set()

    assert [result["error"] for result in held] == ["response_too_large"] * gateway.SHAPING_WORKERS
    # Started: no busy answer, the job's own one.
    assert raced == {"data": "cut in time"}
    # The started job held its slot while it ran, and gave it back at its end.
    assert len(pending_at_start) == 1 and pending_at_start[0] >= 1
    assert await _pool_drained() == 0
    assert cutter.runs == gateway.SHAPING_WORKERS + 1
    assert cutter.peak <= gateway.SHAPING_WORKERS


async def test_a_job_a_worker_picks_up_after_its_caller_gave_up_never_runs(monkeypatch) -> None:
    from sugra_api_mcp.tools import gateway

    _bounds(monkeypatch, clock=0.2, grace=0.1, wait=0.2)
    cutter = _cut_that_blocks(monkeypatch, blocked=gateway.SHAPING_WORKERS)
    real_abandon = client_module._StartGate.abandon
    real_enter = client_module._StartGate.enter
    entered: list = []

    def enter(gate):
        entered.append(gate)
        return real_enter(gate)

    def abandon(gate):
        started_at = real_abandon(gate)
        if started_at is None:
            # The caller has given up. Free the held workers and let one pick
            # the job up before the caller cancels it, so that cancel cannot
            # stop it: only the gate can.
            cutter.release.set()
            deadline = time.monotonic() + 5
            while gate not in entered and time.monotonic() < deadline:
                time.sleep(0.001)
        return started_at

    monkeypatch.setattr(client_module._StartGate, "enter", enter)
    monkeypatch.setattr(client_module._StartGate, "abandon", abandon)
    try:
        held = await _calls(monkeypatch, gateway.SHAPING_WORKERS)
        (late,) = await _calls(monkeypatch, 1)
    finally:
        cutter.release.set()

    assert [result["error"] for result in held] == ["response_too_large"] * gateway.SHAPING_WORKERS
    assert late["error"] == "server_busy" and late["scope"] == "shaping"
    assert len(entered) == gateway.SHAPING_WORKERS + 1
    assert await _pool_drained() == 0
    # The worker took the job and the gate kept the call from running.
    assert cutter.runs == gateway.SHAPING_WORKERS
    assert cutter.peak <= gateway.SHAPING_WORKERS


def test_the_start_gate_lets_exactly_one_side_win() -> None:
    started = client_module._StartGate()
    assert started.enter() is True
    assert started.started_at is not None
    assert started.abandon() == started.started_at
    given_up = client_module._StartGate()
    assert given_up.abandon() is None
    assert given_up.enter() is False
    assert given_up.started_at is None


# ---- a nearest cut offers no count ----


class _Param:
    def __init__(self, name: str) -> None:
        self.name = name
        self.schema_: dict = {}


@pytest.mark.parametrize(
    ("operation_id", "offered"), [("market_calendar_earnings", False), ("earnings_list", True)]
)
def test_a_count_parameter_is_offered_only_off_the_nearest_end(operation_id, offered) -> None:
    from tests.size_cut_bodies import earnings_calendar

    endpoint = _Endpoint(operation_id)
    endpoint.parameters = [_Param("limit"), _Param("from"), _Param("to")]
    result = _module().cut_to_fit(
        earnings_calendar(TODAY, 8, 300), "test://count", cap=CAP, endpoint=endpoint, today=TODAY
    )

    hint = notice(result)["retry_hint"]
    assert (notice(result)["kept_end"] == "nearest") is not offered
    # A count keeps the newest end: past the nearest one it would keep the
    # farthest records, not the ones the cut kept.
    assert ("params.limit=" in hint) is offered
    assert "a narrower params.from to params.to" in hint
