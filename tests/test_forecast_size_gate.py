"""call_endpoint's size gate must measure AFTER fields/limit projection, not
before it.

A weather forecast's raw envelope (`data.hourly` + `data.daily`, neither name
in the fields/limit records-list allowlist) can be well over the MCP size
cap while a `fields=["daily"]` projection of the very same call fits
trivially. Before this fix the gate ran on the raw, unprojected body inside
SugraClient._handle, so it rejected the narrowed request outright - a
real-world call once measured a 21659-token estimate (under the
25000-token directory ceiling) rejected anyway, for exactly this reason.

These tests exercise the REAL SugraClient over an httpx.MockTransport (the
same pattern test_client_errors.py uses), not a hand-rolled fake, because
the defect lived in SugraClient._handle's own unconditional call to
_enforce_size_limit - a fake client that never called it would not
reproduce the bug.
"""

from __future__ import annotations

import json

import httpx

from sugra_api_mcp.catalog.models import Catalog, Endpoint
from sugra_api_mcp.client import MAX_RESPONSE_CHARS, SugraClient
from sugra_api_mcp.config import Config
from sugra_api_mcp.tools import gateway

_OPERATION_ID = "v2_weather_forecast"


def _forecast_row(i: int, *, daily: bool) -> dict:
    key = "date" if daily else "time"
    row: dict = {key: f"2026-09-{28 + i:02d}" if daily else f"2026-09-{28 + i // 24:02d}T{i % 24:02d}:00"}
    for name in (
        "temperature_2m", "apparent_temperature", "relative_humidity_2m", "dew_point_2m",
        "precipitation", "precipitation_probability", "rain", "showers", "snowfall",
        "snow_depth", "weather_code", "pressure_msl", "surface_pressure", "cloud_cover",
        "cloud_cover_low", "cloud_cover_mid", "cloud_cover_high", "visibility",
        "wind_speed_10m", "wind_direction_10m", "wind_gusts_10m", "uv_index",
        "sunshine_duration", "shortwave_radiation", "et0_fao_evapotranspiration",
    ):
        row[name] = round(10.123 + i * 0.37, 2)
    row["condition"] = "Partly cloudy"
    row["condition_icon"] = "partly-cloudy-day"
    return row


def _forecast_body(days: int) -> dict:
    """Recorded-shape (not live) /api/v2/weather/forecast envelope."""
    return {
        "data": {
            "latitude": 35.68,
            "longitude": 139.69,
            "elevation": 40.0,
            "timezone": "Asia/Tokyo",
            "timezone_abbreviation": "JST",
            "utc_offset_seconds": 32400,
            "units": {"hourly": {"temperature_2m": "°C"}, "daily": {"temperature_2m_max": "°C"}},
            "hourly": [_forecast_row(i, daily=False) for i in range(days * 24)],
            "daily": [_forecast_row(i, daily=True) for i in range(days)],
            "provenance": {"source": "openmeteo", "generationtime_ms": 1.23},
            "resolved_location": None,
        },
        "meta": {"source": "openmeteo", "cached": False},
    }


def _weather_catalog() -> Catalog:
    return Catalog(
        source="test-weather",
        endpoints=[
            Endpoint(
                operation_id=_OPERATION_ID,
                method="GET",
                path="/api/v2/weather/forecast",
                summary="Hourly and daily weather forecast",
                toolset="environment",
                source_family="environment",
                sources=["environment"],
                parameters=[],
            )
        ],
    )


def _client_serving(days: int) -> SugraClient:
    body = _forecast_body(days)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=body)

    config = Config(api_base="https://api.test", api_key="test-key", timeout=5.0)
    return SugraClient(config, transport=httpx.MockTransport(handler))


async def test_14_day_daily_only_fits_in_one_call(monkeypatch) -> None:
    """Acceptance criterion: forecast_days=14 with fields=["daily"] must
    return the full 14-row daily block, never response_too_large, even
    though the raw hourly+daily body is far over the cap."""
    client = _client_serving(14)
    monkeypatch.setattr(gateway, "load_catalog", _weather_catalog)
    monkeypatch.setattr(gateway, "get_client", lambda: client)
    try:
        raw_len = len(json.dumps(_forecast_body(14)))
        assert raw_len > MAX_RESPONSE_CHARS  # the raw body alone would have tripped the old gate

        result = await gateway.call_endpoint(_OPERATION_ID, fields=["daily"])
    finally:
        await client.aclose()

    assert "error" not in result
    assert "hourly" not in result["data"]
    assert len(result["data"]["daily"]) == 14
    assert "truncated" not in result.get("meta", {})
    assert len(json.dumps(result)) <= MAX_RESPONSE_CHARS


async def test_16_day_daily_only_also_fits(monkeypatch) -> None:
    """The API serves up to 16 days; the daily-only projection must scale to
    the full horizon, not just the 14-day case."""
    client = _client_serving(16)
    monkeypatch.setattr(gateway, "load_catalog", _weather_catalog)
    monkeypatch.setattr(gateway, "get_client", lambda: client)
    try:
        result = await gateway.call_endpoint(_OPERATION_ID, fields=["daily"])
    finally:
        await client.aclose()

    assert "error" not in result
    assert len(result["data"]["daily"]) == 16
    assert len(json.dumps(result)) <= MAX_RESPONSE_CHARS


async def test_forecast_days_5_daily_only_no_longer_rejected(monkeypatch) -> None:
    """The exact field-test scenario (forecast_days=5, daily fields only)
    that the size gate used to reject even though the estimate it printed
    (21659 tokens) was under the 25000-token directory ceiling."""
    client = _client_serving(5)
    monkeypatch.setattr(gateway, "load_catalog", _weather_catalog)
    monkeypatch.setattr(gateway, "get_client", lambda: client)
    try:
        result = await gateway.call_endpoint(_OPERATION_ID, fields=["daily"])
    finally:
        await client.aclose()

    assert result.get("error") != "response_too_large"
    assert len(result["data"]["daily"]) == 5


async def test_full_hourly_and_daily_over_cap_is_cut_with_marker_not_rejected(monkeypatch) -> None:
    """No `fields` filter: the raw hourly+daily body is far over the cap and
    cannot be projected down. It must be CUT (hourly shrunk) with an
    explicit meta.truncated marker naming the untouched daily sibling -
    never response_too_large, and never passed through whole."""
    client = _client_serving(16)
    monkeypatch.setattr(gateway, "load_catalog", _weather_catalog)
    monkeypatch.setattr(gateway, "get_client", lambda: client)
    try:
        result = await gateway.call_endpoint(_OPERATION_ID)
    finally:
        await client.aclose()

    assert "error" not in result
    assert len(json.dumps(result)) <= MAX_RESPONSE_CHARS
    truncated = result["meta"]["truncated"]
    hourly_note = truncated["fields"]["hourly"]
    assert hourly_note["kept_count"] < hourly_note["original_count"]
    assert "daily" not in truncated["fields"]  # untouched, kept whole
    assert len(result["data"]["daily"]) == 16
    assert "daily" in truncated["retry_hint"]  # hints the fields=["daily"] escape
