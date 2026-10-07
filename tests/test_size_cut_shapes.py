"""The size gate cuts an oversized response whatever its shape, and refuses
only when no cut fits.

Every body is a recorded shape, not a live call, sized like the responses
that used to be refused: a quote history under data.data, a forecast's
data.hourly beside data.daily, the four data.<kind>.rows of a market
calendar, data.records of a LEI search. Each goes through call_endpoint over
the bundled catalog, so the cut runs where it runs in production, on the
shaping pool, with the operation's own parameters.
"""

from __future__ import annotations

import asyncio
import copy
import json
import random
import time
from datetime import date, timedelta

import pytest

from sugra_api_mcp import client as client_module
from sugra_api_mcp.catalog.loader import load_catalog
from sugra_api_mcp.catalog.response import MAX_SHAPING_SECONDS
from sugra_api_mcp.client import MAX_RESPONSE_CHARS, _enforce_size_limit
from tests.size_cut_bodies import (
    TODAY,
    at,
    bars,
    call,
    chars,
    client_serving,
    edinet_document,
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


def _fits(result) -> bool:
    return chars(result) <= MAX_RESPONSE_CHARS


async def test_quote_history_under_data_data_keeps_the_newest_bars(monkeypatch) -> None:
    body = quotes_history(2000)
    assert chars(body) > MAX_RESPONSE_CHARS

    result = await call(monkeypatch, "quotes_symbol_historical", body, {"symbol": "AAPL"})

    assert "error" not in result
    assert _fits(result)
    kept = result["data"]["data"]
    cut = notice(result)
    assert cut["reason"] == "exceeds_response_size_cap"
    assert cut["path"] == "data.data"
    assert cut["order"] == "asc" and cut["kept_end"] == "newest"
    assert cut["original_count"] == 2000 and cut["kept_count"] == len(kept) < 2000
    assert kept == body["data"]["data"][-len(kept):]
    assert result["data"]["symbol"] == "AAPL"
    assert cut["cap_chars"] == MAX_RESPONSE_CHARS
    assert cut["original_chars"] == chars(body)
    assert cut["kept_chars"] == chars(result)


async def test_forecast_keeps_the_hours_from_today_and_the_daily_list_whole(monkeypatch) -> None:
    # Two past days first (past_days=2), as the API serves them.
    body = weather(TODAY - timedelta(days=2), 9)
    assert chars(body) > MAX_RESPONSE_CHARS

    result = await call(monkeypatch, "v2_weather_forecast", body, {"city": "Tokyo"})

    assert "error" not in result
    assert _fits(result)
    data = result["data"]
    assert data["daily"] == body["data"]["daily"]
    assert data["units"] == body["data"]["units"]
    assert data["provenance"] == body["data"]["provenance"]
    cut = notice(result)
    assert cut["path"] == "data.hourly" and cut["kept_end"] == "nearest"
    assert data["hourly"][0]["time"] == f"{TODAY.isoformat()}T00:00"
    first = body["data"]["hourly"].index(data["hourly"][0])
    assert data["hourly"] == body["data"]["hourly"][first:first + len(data["hourly"])]
    assert "lists" not in cut


async def test_history_keeps_the_newest_hours_even_across_today(monkeypatch) -> None:
    # The nearest rule belongs to the forecast operations by name, never to
    # the dates: a history whose window runs past today keeps its newest end.
    body = weather(TODAY - timedelta(days=4), 8)

    result = await call(
        monkeypatch, "v2_weather_history", body,
        {"city": "Tokyo", "start_date": "2026-10-01", "end_date": "2026-10-08"},
    )

    assert "error" not in result
    assert _fits(result)
    cut = notice(result)
    assert cut["path"] == "data.hourly" and cut["kept_end"] == "newest"
    assert result["data"]["hourly"][-1] == body["data"]["hourly"][-1]
    assert result["data"]["daily"] == body["data"]["daily"]


async def test_lei_search_records_are_cut_from_the_first(monkeypatch) -> None:
    body = lei_search(50, width=2000)

    result = await call(monkeypatch, "gleif_lei_search", body, {"legal_name": "Synthetic"})

    assert "error" not in result
    assert _fits(result)
    cut = notice(result)
    assert cut["path"] == "data.records" and cut["kept_end"] == "first"
    assert result["data"]["records"] == body["data"]["records"][: cut["kept_count"]]
    assert result["data"]["total"] == 50


async def test_market_calendar_cuts_the_big_sibling_lists_and_empties_none(monkeypatch) -> None:
    body = market_calendar(TODAY - timedelta(days=2), 8)
    assert chars(body) > MAX_RESPONSE_CHARS

    result = await call(monkeypatch, "market_calendar", body)

    assert "error" not in result
    assert _fits(result)
    cut = notice(result)
    entries = {entry["path"]: entry for entry in cut["lists"]}
    assert set(entries) == {"data.earnings.rows", "data.dividends.rows", "data.economic_events.rows"}
    assert result["data"]["splits"] == body["data"]["splits"]
    assert cut["path"] == "data.earnings.rows"
    for path, entry in entries.items():
        original, kept = at(body, path), at(result, path)
        assert kept, path
        assert entry["original_count"] == len(original)
        assert entry["kept_count"] == len(kept) < len(original)
        assert entry["kept_end"] == "nearest"
        assert entry["kept_range"] == [kept[0]["date"], kept[-1]["date"]]
        start = original.index(kept[0])
        assert kept == original[start:start + len(kept)]
        assert any(row["date"] == TODAY.isoformat() for row in kept)


async def test_lists_without_a_readable_order_carry_no_kept_range(monkeypatch) -> None:
    rows = [{"id": i, "text": "t" * 400} for i in range(150)]
    body = {"data": {"left": list(rows), "right": list(rows)}, "meta": {}}

    result = await call(monkeypatch, "rba_cpi", body)

    assert "error" not in result
    assert _fits(result)
    cut = notice(result)
    assert len(cut["lists"]) == 2
    for entry in cut["lists"]:
        assert "kept_range" not in entry
        assert entry["kept_end"] == "first"
        assert entry["kept_count"] == len(at(result, entry["path"]))
    assert result["data"]["left"] == rows[: cut["lists"][0]["kept_count"]]


async def test_earnings_calendar_keeps_the_rows_from_today(monkeypatch) -> None:
    from tests.size_cut_bodies import earnings_calendar

    body = earnings_calendar(TODAY - timedelta(days=1), 8, 300)

    result = await call(monkeypatch, "market_calendar_earnings", body)

    assert "error" not in result
    assert _fits(result)
    cut = notice(result)
    assert cut["path"] == "data.rows" and cut["kept_end"] == "nearest"
    assert result["data"]["rows"][0]["date"] == TODAY.isoformat()


async def test_a_list_under_a_key_outside_the_records_allowlist_is_cut(monkeypatch) -> None:
    prices = [{"timestamp": 1_500_000_000 + i * 86_400, "price": 6000.5 + i} for i in range(5000)]
    body = {"data": {"currency": "USD", "prices": prices, "count": 5000}, "meta": {}}
    for record in prices:
        record["note"] = "n" * 20

    result = await call(monkeypatch, "onchain_bitcoin_price_history", body)

    assert "error" not in result
    assert _fits(result)
    assert notice(result)["path"] == "data.prices"
    assert 0 < len(result["data"]["prices"]) < 5000


async def test_cjk_text_is_measured_by_its_escapes() -> None:
    body = {"data": [{"date": f"2026-01-{i % 28 + 1:02d}", "text": "東京" * 200} for i in range(30)]}
    # Under the cap counted once a character, far over it as escapes: the
    # escapes are what is enforced, as they always were.
    assert len(json.dumps(body, ensure_ascii=False)) < MAX_RESPONSE_CHARS < chars(body)

    direct = await asyncio.to_thread(_enforce_size_limit, body, "test://cjk")
    client = client_serving(body)
    try:
        fetched = await client.get("/api/v1/some/text")
    finally:
        await client.aclose()

    for result in (direct, fetched):
        cut = notice(result)
        assert 1 <= cut["kept_count"] < 30
        assert cut["original_chars"] == chars(body)
        assert cut["kept_chars"] == chars(result) <= MAX_RESPONSE_CHARS
        # The dates wrap around, so no order is read and the first are kept.
        assert cut["kept_end"] == "first"
        assert result["data"] == body["data"][: cut["kept_count"]]


def test_an_ascii_response_meets_the_cap_where_it_always_did() -> None:
    def body(width: int) -> dict:
        return {"data": [{"v": "x" * width}, {"v": "y"}]}

    base = len(json.dumps(body(0)))
    at_cap = body(MAX_RESPONSE_CHARS - base)
    assert len(json.dumps(at_cap)) == MAX_RESPONSE_CHARS

    assert _enforce_size_limit(at_cap, "test://ascii") is at_cap
    over = body(MAX_RESPONSE_CHARS - base + 1)
    assert _enforce_size_limit(over, "test://ascii") is not over


async def test_the_cut_past_its_clock_is_a_size_refusal_not_a_projection_error(monkeypatch) -> None:
    module = size_cut_module()
    assert module is not None
    monkeypatch.setattr(module, "MAX_SHAPING_SECONDS", -1.0)

    result = await call(monkeypatch, "quotes_symbol_historical", quotes_history(2000), {"symbol": "AAPL"})

    assert result["error"] == "response_too_large"
    assert "could not be cut in time" in result["message"]
    assert "params.start to params.end" in result["message"]
    assert _fits(result)


async def test_on_the_event_loop_a_response_over_the_cap_is_refused_unwalked(monkeypatch) -> None:
    module = size_cut_module()
    assert module is not None

    def never(*args, **kwargs):
        raise AssertionError("walked")

    monkeypatch.setattr(module, "_find_lists", never)
    monkeypatch.setattr(module, "_List", never)
    monkeypatch.setattr(client_module, "response_chars", never)
    body = {"data": bars(30_000)}

    result = _enforce_size_limit(body, "test://loop")

    assert result["error"] == "response_too_large"
    assert "retry_hint" not in result
    assert result["response_chars"] is None
    assert result["message"] == "Response is over the limit of 18,000 characters."
    assert _fits(result)


async def test_off_the_loop_the_same_response_is_cut() -> None:
    body = {"data": bars(30_000)}

    result = await asyncio.to_thread(_enforce_size_limit, body, "test://pool")

    assert "error" not in result
    assert _fits(result)
    assert result["data"][-1] == body["data"][-1]


async def test_the_client_cuts_any_shape_on_the_shaping_pool() -> None:
    nested = quotes_history(2000)
    flat = {"data": bars(2000), "meta": {}}
    bare = bars(2000)
    results = []
    for body in (nested, flat, bare):
        client = client_serving(body)
        try:
            results.append(await client.get("/api/v2/quotes/AAPL/historical"))
        finally:
            await client.aclose()

    cut_nested, cut_flat, cut_bare = results
    assert notice(cut_nested)["path"] == "data.data" and _fits(cut_nested)
    assert cut_nested["data"]["data"][-1] == nested["data"]["data"][-1]
    assert notice(cut_flat)["path"] == "data" and _fits(cut_flat)
    assert notice(cut_bare)["path"] == "data" and _fits(cut_bare)
    assert cut_bare["data"][-1] == bare[-1]


async def test_one_record_over_the_cap_is_refused_in_characters(monkeypatch) -> None:
    body = {"data": [{"date": "2026-10-01", "blob": "x" * 120_000}]}

    result = await call(monkeypatch, "futures_root_historical", body, {"root": "CL"})

    assert result["error"] == "response_too_large"
    assert "18,000 characters" in result["message"]
    assert "25000" not in result["message"] and "token" not in result["message"]
    assert result["cap_chars"] == MAX_RESPONSE_CHARS
    assert result["response_chars"] == chars(body)
    assert isinstance(result["estimated_tokens"], int)
    record = chars(body["data"][0])
    assert result["retry_hint"] == (
        f"One record at data is larger than the limit by itself, about {record:,} characters. "
        "Pass fields naming only the keys you need from each record."
    )


async def test_one_event_over_the_cap_names_its_list(monkeypatch) -> None:
    result = await call(monkeypatch, "predictions_events", events(1, 600))

    assert result["error"] == "response_too_large"
    assert "One record at data.events is larger than the limit by itself, about " in result["message"]


async def test_a_text_document_refusal_names_the_fields_that_leave_the_text_out(monkeypatch) -> None:
    body = edinet_document(40_000)
    params = {"ticker": "7203", "doc_id": "S100PVTL"}

    refused = await call(monkeypatch, "edinet_ticker_document_doc_id", body, params)

    assert refused["error"] == "response_too_large"
    assert "Most of it is at data.parsed.text_blocks" in refused["message"]
    named = json.loads(refused["retry_hint"].split("fields=", 1)[1].rstrip("."))
    assert "parsed.identification" in named

    result = await call(monkeypatch, "edinet_ticker_document_doc_id", body, params, fields=named)

    assert "error" not in result
    assert _fits(result)
    assert "text_blocks" not in result["data"]["parsed"]
    assert result["data"]["parsed"]["identification"] == body["data"]["parsed"]["identification"]


async def test_a_large_cut_finishes_well_inside_the_shaping_clock(monkeypatch) -> None:
    body = indicators(4900)
    assert chars(body) > 1_500_000

    started = time.monotonic()
    result = await call(monkeypatch, "macro_indicators_available", body)
    elapsed = time.monotonic() - started

    assert "error" not in result
    assert _fits(result)
    assert elapsed < MAX_SHAPING_SECONDS


def test_every_nearest_end_operation_is_in_the_catalog() -> None:
    module = size_cut_module()
    assert module is not None
    catalog = load_catalog()
    known = {endpoint.operation_id for endpoint in catalog.endpoints}

    assert known >= module.NEAREST_END_OPERATIONS


def _random_records(rng: random.Random, dated: bool) -> list:
    count = rng.choice((1, 2, 5, 40, 300, 1500))
    size = rng.choice((10, 60, 400, 3000, 30_000, 200_000))
    # At most about 400,000 characters a list: past the cap many times over,
    # and quick to measure.
    count = min(count, max(1, 400_000 // size))
    alphabet = rng.choice(("a", "é", "東", "a東"))
    start = date(2026, 1, 1)
    records = []
    for i in range(count):
        record = {"text": alphabet * max(1, rng.randint(size // 2, size) // len(alphabet))}
        if dated:
            record["date"] = (start + timedelta(days=i)).isoformat()
        records.append(record)
    if dated and rng.random() < 0.3:
        records.reverse()
    return records


def _random_payload(rng: random.Random):
    depth = rng.choice((0, 1, 2))
    lists = rng.randint(1, 4)
    if depth == 0:
        return {"data": _random_records(rng, rng.random() < 0.5), "meta": {"source": "x"}}
    data: dict = {"label": "s" * rng.choice((0, 50, 5000))}
    for index in range(lists):
        records = _random_records(rng, rng.random() < 0.5)
        if depth == 1:
            data[f"list{index}"] = records
        else:
            data[f"group{index}"] = {"count": len(records), "rows": records}
    return {"data": data, "meta": {}}


def test_a_cut_never_returns_a_response_over_the_cap() -> None:
    rng = random.Random(4006)
    catalog = load_catalog()
    endpoints = [None, catalog.get("market_calendar"), catalog.get("quotes_symbol_historical"),
                 catalog.get("rba_cpi")]
    outcomes = {"pass": 0, "cut": 0, "refused": 0}
    for trial in range(300):
        payload = _random_payload(rng)
        before = json.dumps(payload)
        endpoint = rng.choice(endpoints)

        result = _enforce_size_limit(payload, "test://random", endpoint=endpoint)

        assert json.dumps(payload) == before, trial
        assert _fits(result), trial
        if result is payload:
            outcomes["pass"] += 1
        elif "error" in result:
            assert result["error"] == "response_too_large", trial
            outcomes["refused"] += 1
        else:
            cut = notice(result)
            entries = cut.get("lists") or [cut]
            for entry in entries:
                kept = at(result, entry["path"])
                assert len(kept) == entry["kept_count"] >= 1, trial
                assert entry["original_count"] == len(at(payload, entry["path"])), trial
            assert cut["kept_chars"] == chars(result), trial
            outcomes["cut"] += 1
    assert all(outcomes.values()), outcomes


def test_the_input_payload_is_never_changed_by_a_cut() -> None:
    body = market_calendar(TODAY, 8)
    snapshot = copy.deepcopy(body)

    result = _enforce_size_limit(body, "test://same", endpoint=load_catalog().get("market_calendar"))

    assert "truncated" in result["meta"]
    assert body == snapshot


def test_measure_is_the_one_the_client_exports() -> None:
    assert client_module.response_chars({"a": "東"}) == len('{"a": "\\u6771"}') == 15


def test_a_bare_array_server_response_still_reaches_the_cut() -> None:
    body = bars(3000)

    result = _enforce_size_limit(body, "test://bare")

    assert isinstance(result, dict) and "error" not in result
    assert notice(result)["path"] == "data"
    assert _fits(result)
    assert result["data"][-1] == body[-1]
