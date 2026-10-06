"""What a cut or a refusal tells the agent to do next.

A hint names only what the call can take: the parameters of the operation
called, read from the bundled catalog, and the tool's own limit and fields.
params.limit is the API's parameter; "limit=N beside params" is the tool's
own argument. A number is clamped to the parameter's maximum, and following
a hint gives a response that fits.
"""

from __future__ import annotations

import json
import re
from datetime import timedelta

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


@pytest.mark.parametrize("operation_id", ["rba_cpi", "macro_indicators_available"])
async def test_an_operation_without_parameters_gets_no_filter_advice(monkeypatch, operation_id) -> None:
    body = indicators(4900) if operation_id == "macro_indicators_available" else {"data": bars(2000)}

    result = await call(monkeypatch, operation_id, body)

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


async def test_a_cut_of_several_lists_says_how_many(monkeypatch) -> None:
    result = await call(monkeypatch, "market_calendar", market_calendar(TODAY, 8))

    cut = notice(result)
    assert f" {len(cut['lists']) - 1} more lists were cut, see lists." in cut["retry_hint"]
    assert "a narrower params.start_date to params.end_date" in cut["retry_hint"]
