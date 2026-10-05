"""Recorded-shape (not live) response bodies for the size cut tests, and the
call helpers they share."""

from __future__ import annotations

import importlib
import json
from datetime import date, timedelta
from typing import Any

import httpx

from sugra_api_mcp.client import SugraClient
from sugra_api_mcp.config import Config
from sugra_api_mcp.tools import gateway

TODAY = date(2026, 10, 5)


def size_cut_module() -> Any:
    """The cutter module, or None where it does not exist."""
    try:
        return importlib.import_module("sugra_api_mcp.catalog.size_cut")
    except ImportError:
        return None


def chars(value: Any) -> int:
    return len(json.dumps(value, ensure_ascii=False))


def notice(result: dict[str, Any]) -> dict[str, Any]:
    return result["meta"]["truncated"]


def at(payload: Any, path: str) -> Any:
    node = payload
    for key in path.split("."):
        node = node[key]
    return node


def client_serving(body: Any) -> SugraClient:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=body, request=request)

    config = Config(api_base="https://api.test", api_key="test-key", timeout=5.0)
    return SugraClient(config, transport=httpx.MockTransport(handler))


async def call(monkeypatch, operation_id: str, body: Any, params=None, **kwargs) -> dict[str, Any]:
    """call_endpoint over the bundled catalog, served body by a mock API."""
    client = client_serving(body)
    monkeypatch.setattr(gateway, "get_client", lambda: client)
    try:
        return await gateway.call_endpoint(operation_id, params=params, **kwargs)
    finally:
        await client.aclose()


def bars(count: int, start: date = date(2019, 1, 1)) -> list[dict[str, Any]]:
    return [
        {
            "date": (start + timedelta(days=i)).isoformat(),
            "open": 100.125 + i, "high": 101.5 + i, "low": 99.25 + i, "close": 100.75 + i,
            "adj_close": 100.7512 + i, "volume": 1_000_000 + i,
        }
        for i in range(count)
    ]


def quotes_history(count: int) -> dict[str, Any]:
    return {
        "data": {"symbol": "AAPL", "interval": "1d", "currency": "USD", "data": bars(count)},
        "meta": {"source": "sugra_finance"},
    }


def _weather_row(key: str, stamp: str, i: int) -> dict[str, Any]:
    row: dict[str, Any] = {key: stamp}
    for name in (
        "temperature_2m", "apparent_temperature", "relative_humidity_2m", "dew_point_2m",
        "precipitation", "precipitation_probability", "rain", "showers", "snowfall",
        "snow_depth", "weather_code", "pressure_msl", "surface_pressure", "cloud_cover",
        "cloud_cover_low", "cloud_cover_mid", "cloud_cover_high", "visibility",
        "wind_speed_10m", "wind_direction_10m", "wind_gusts_10m", "uv_index",
    ):
        row[name] = round(10.123 + i * 0.37, 2)
    row["condition"] = "Partly cloudy"
    return row


def weather(first_day: date, days: int) -> dict[str, Any]:
    """The v2 weather envelope: data.hourly beside data.daily."""
    hourly = []
    for i in range(days * 24):
        moment = first_day + timedelta(days=i // 24)
        hourly.append(_weather_row("time", f"{moment.isoformat()}T{i % 24:02d}:00", i))
    daily = [_weather_row("date", (first_day + timedelta(days=i)).isoformat(), i) for i in range(days)]
    return {
        "data": {
            "latitude": 35.68, "longitude": 139.69, "timezone": "Asia/Tokyo",
            "units": {"temperature_2m": "°C"},
            "hourly": hourly,
            "daily": daily,
            "provenance": {"source": "sugra_weather"},
            "resolved_location": None,
        },
        "meta": {"source": "sugra_weather"},
    }


def _calendar_rows(first_day: date, days: int, per_day: int, width: int, kind: str) -> dict[str, Any]:
    rows = [
        {"date": (first_day + timedelta(days=i)).isoformat(), "symbol": f"{kind[:3].upper()}{j}",
         "name": f"{kind} {i}-{j} " + "n" * width}
        for i in range(days) for j in range(per_day)
    ]
    return {"count": len(rows), "rows": rows}


def market_calendar(first_day: date, days: int) -> dict[str, Any]:
    """Four sibling {count, rows} blocks, as the market calendar bundle."""
    return {
        "data": {
            "start_date": first_day.isoformat(),
            "end_date": (first_day + timedelta(days=days - 1)).isoformat(),
            "earnings": _calendar_rows(first_day, days, 40, 260, "earnings"),
            "dividends": _calendar_rows(first_day, days, 15, 260, "dividends"),
            "splits": _calendar_rows(first_day, days, 2, 20, "splits"),
            "economic_events": _calendar_rows(first_day, days, 15, 260, "economic"),
        },
        "meta": {"source": "sugra_finance"},
    }


def earnings_calendar(first_day: date, days: int, per_day: int) -> dict[str, Any]:
    block = _calendar_rows(first_day, days, per_day, 200, "earnings")
    return {
        "data": {"from_date": first_day.isoformat(),
                 "to_date": (first_day + timedelta(days=days - 1)).isoformat(), **block},
        "meta": {},
    }


def lei_search(count: int, width: int = 300) -> dict[str, Any]:
    records = [
        {"lei": f"5493{i:016d}", "legal_name": f"Synthetic {i} " + "x" * width,
         "country": "US", "status": "ACTIVE"}
        for i in range(count)
    ]
    return {"data": {"records": records, "total": count}, "meta": {}}


def events(count: int, markets: int, width: int = 300) -> dict[str, Any]:
    market = {"id": "m", "question": "Will it happen " + "q" * width, "outcomes": ["Yes", "No"]}
    return {
        "data": {"events": [{"id": f"e{i}", "title": "Event", "markets": [market] * markets}
                            for i in range(count)]},
        "meta": {},
    }


def edinet_document(text_chars: int) -> dict[str, Any]:
    return {
        "data": {
            "company": {"ticker": "7203", "name": "Synthetic KK"},
            "doc_id": "S100PVTL",
            "parsed": {
                "doc_type": "annual", "doc_type_code": "120",
                "doc_type_name": "Annual securities report",
                "identification": {"edinet_code": "E02144", "filer": "Synthetic KK"},
                "text_blocks": {"business_overview": "x" * text_chars, "risks": "y" * text_chars,
                                "mdna": "z" * text_chars},
                "element_count": 4321, "elements_summary": {"total": 4321}, "is_amendment": False,
            },
        },
        "meta": {},
    }


def indicators(count: int) -> dict[str, Any]:
    results = [
        {"code": f"IND{i:05d}", "name": f"Indicator {i} " + "i" * 300, "source": "sugra_macro"}
        for i in range(count)
    ]
    return {"data": {"results": results, "count": count}, "meta": {}}
