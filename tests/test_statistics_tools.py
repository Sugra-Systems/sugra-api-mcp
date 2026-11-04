"""Statistics tools: one API request per call, a compact result, and errors in the tool's own words."""

from __future__ import annotations

import json
from typing import Any

import pytest
from mcp.shared.memory import create_connected_server_and_client_session
from mcp.types import Implementation

from sugra_api_mcp import tools  # noqa: F401
from sugra_api_mcp.client import MAX_RESPONSE_CHARS
from sugra_api_mcp.observability import _KNOWN_ERROR_CODES
from sugra_api_mcp.server import mcp
from sugra_api_mcp.tools import gateway, statistics

CHATGPT = Implementation(name="openai-mcp", version="1.0.0")
CLAUDE = Implementation(name="claude-ai", version="0.1.0")
NAMES = ("search_statistics", "find_statistics_place", "get_statistics")

_PLACE = {"id": "06037", "code_system": "fips", "name": "Los Angeles County", "type": "county", "country": "USA"}
_SOURCE = {"id": "census-bureau", "name": "U.S. Census Bureau", "url": "https://www.census.gov/"}


class FakeClient:
    def __init__(self, payload: dict[str, Any]) -> None:
        self.payload = payload
        self.calls: list[tuple[str, dict[str, Any] | None]] = []

    async def get(self, path: str, params: dict[str, Any] | None = None, **_: Any) -> dict[str, Any]:
        self.calls.append((path, params))
        return self.payload


@pytest.fixture
def answer(monkeypatch):
    def install(payload: dict[str, Any]) -> FakeClient:
        fake = FakeClient(payload)
        monkeypatch.setattr(gateway, "get_client", lambda: fake)
        return fake

    return install


def _rows(count: int) -> list[dict[str, Any]]:
    return [
        {"place": {"id": f"{n:05d}", "code_system": "fips", "name": f"County {n}", "type": "county", "country": "USA"},
         "date": "2024", "value": 50000 + n}
        for n in range(count)
    ]


async def test_search_sends_query_and_limit(answer) -> None:
    fake = answer({"data": {"results": []}, "meta": {"data_time": "2026-11-04T20:57:04Z"}})

    result = await statistics.search_statistics("median household income")

    assert fake.calls == [("/api/v1/statistics/search", {"q": "median household income", "limit": 10})]
    assert result == {"data": {"results": []}, "meta": {"data_time": "2026-11-04T20:57:04Z"}}


async def test_search_passes_place_only_when_given(answer) -> None:
    fake = answer({"data": {"results": []}, "meta": {}})

    await statistics.search_statistics("population", place="KEN", limit=3)

    assert fake.calls == [("/api/v1/statistics/search", {"q": "population", "limit": 3, "place": "KEN"})]


async def test_find_place_sends_the_name(answer) -> None:
    fake = answer({"data": {"places": [_PLACE]}, "meta": {}})

    result = await statistics.find_statistics_place("Los Angeles County")

    assert fake.calls == [("/api/v1/statistics/places/resolve", {"q": "Los Angeles County"})]
    assert result["data"]["places"] == [_PLACE]


async def test_get_without_place_type_reads_the_series(answer) -> None:
    fake = answer({
        "data": {"indicator": "median-household-income", "place": _PLACE, "source": _SOURCE,
                 "observations": [{"date": "2024", "value": 90112}]},
        "meta": {"data_time": "2024-01-01T00:00:00Z", "period": "2024"},
    })

    result = await statistics.get_statistics("median-household-income", "06037", start="2018", end="2024")

    assert fake.calls == [
        ("/api/v1/statistics/series/median-household-income", {"place": "06037", "start": "2018", "end": "2024"})
    ]
    assert result["data"]["source"] == _SOURCE
    assert result["data"]["observations"] == [{"date": "2024", "value": 90112}]
    assert result["meta"] == {"data_time": "2024-01-01T00:00:00Z", "period": "2024"}


async def test_get_with_place_type_lists_places_with_compact_rows(answer) -> None:
    fake = answer({
        "data": {"indicator": "median-household-income", "type": "county", "source": _SOURCE, "rows": _rows(2)},
        "meta": {},
    })

    result = await statistics.get_statistics(
        "median-household-income", "06", place_type="county", date="2023", agency="census-bureau"
    )

    assert fake.calls == [(
        "/api/v1/statistics/places/median-household-income",
        {"parent": "06", "type": "county", "date": "2023", "agency": "census-bureau"},
    )]
    assert result["data"]["rows"] == [
        {"place": {"id": "00000", "name": "County 0"}, "date": "2024", "value": 50000},
        {"place": {"id": "00001", "name": "County 1"}, "date": "2024", "value": 50001},
    ]
    assert result["data"]["source"] == _SOURCE
    assert "shaped" not in result.get("meta", {})


@pytest.mark.parametrize(
    ("arguments", "reason"),
    [
        ({"date": "2023"}, "date applies with place_type; for a series pass start and end."),
        ({"place_type": "county", "start": "2020"}, "start and end apply to a series; with place_type pass date."),
        ({"place_type": "county", "end": "2024"}, "start and end apply to a series; with place_type pass date."),
    ],
)
async def test_get_refuses_mixed_arguments_before_any_request(answer, arguments, reason) -> None:
    fake = answer({"data": {}, "meta": {}})

    result = await statistics.get_statistics("median-household-income", "06", **arguments)

    assert result == {"error": "validation_failed", "reason": reason}
    assert fake.calls == []
    assert "validation_failed" in _KNOWN_ERROR_CODES


async def test_a_cut_answer_names_this_tools_arguments(answer) -> None:
    answer({"data": {"indicator": "population", "type": "zcta", "rows": _rows(2000)}, "meta": {}})

    result = await statistics.get_statistics("population", "USA", place_type="zcta")

    truncated = result["meta"]["truncated"]
    assert truncated["kept_count"] < truncated["original_count"] == 2000
    assert truncated["retry_hint"] == (
        "Not every place fits in one answer: ask for a smaller parent place or a place type with fewer places."
    )
    assert len(json.dumps(result)) <= MAX_RESPONSE_CHARS


async def test_an_api_error_keeps_only_what_went_wrong(answer) -> None:
    answer({
        "error": "Indicator not found",
        "status_code": 404,
        "url": "https://sugra.ai/api/v1/statistics/series/nope?place=USA",
        "elapsed_ms": 12,
    })

    result = await statistics.get_statistics("nope", "USA")

    assert result == {"error": "Indicator not found", "status_code": 404}


async def test_request_metadata_is_dropped(answer) -> None:
    answer({"data": {"results": []}, "meta": {"data_time": "t", "request_id": "r", "elapsed_ms": 5}, "url": "u"})

    result = await statistics.search_statistics("income")

    assert result == {"data": {"results": []}, "meta": {"data_time": "t"}}


async def _listed(client_info: Implementation) -> dict[str, dict[str, Any]]:
    async with create_connected_server_and_client_session(mcp, client_info=client_info) as session:
        listed = await session.list_tools()
    return {tool.name: tool.model_dump(by_alias=True, exclude_none=True) for tool in listed.tools}


@pytest.mark.parametrize("client_info", [CLAUDE, CHATGPT], ids=["default", "chatgpt"])
async def test_every_client_lists_the_three_read_only_tools(client_info) -> None:
    listed = await _listed(client_info)

    for name in NAMES:
        annotations = listed[name]["annotations"]
        assert annotations["readOnlyHint"] is True
        assert annotations["destructiveHint"] is False


async def test_listed_schema_bounds_the_inputs() -> None:
    schema = (await _listed(CLAUDE))["get_statistics"]["inputSchema"]
    properties = schema["properties"]

    assert schema["required"] == ["indicator", "place"]
    place_type = properties["place_type"]["anyOf"][0]
    assert place_type["enum"] == [
        "country", "state", "county", "city", "metro", "zcta", "admin1", "admin2", "nuts1", "nuts2", "nuts3"
    ]
    assert properties["start"]["anyOf"][0]["pattern"] == r"^\d{4}(-\d{2}(-\d{2})?)?$"
    assert properties["date"]["anyOf"][0]["pattern"] == r"^(latest|\d{4}(-\d{2}(-\d{2})?)?)$"


async def test_a_period_outside_the_pattern_is_refused_before_any_request(answer) -> None:
    fake = answer({"data": {}, "meta": {}})

    async with create_connected_server_and_client_session(mcp, client_info=CLAUDE) as session:
        result = await session.call_tool(
            "get_statistics", {"indicator": "population", "place": "USA", "start": "June 2020"}
        )

    assert result.isError is True
    assert fake.calls == []


def test_descriptions_name_no_tool_a_chatgpt_session_is_not_listed() -> None:
    for name in NAMES:
        description = mcp._tool_manager.get_tool(name).description
        for unlisted in ("search_endpoints", "describe_endpoint", "call_endpoint", "fetch_data"):
            assert unlisted not in description, (name, unlisted)
