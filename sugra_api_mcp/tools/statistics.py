"""Sugra statistics tools: find an indicator, find a place code, get the values.

Three read-only tools over the Sugra API statistics endpoints
(``/api/v1/statistics/*``), registered on every transport:

- ``search_statistics``     - plain words -> indicator codes, with the place
  types that have data and the agencies that publish them.
- ``find_statistics_place`` - a place name -> the place codes the values take.
- ``get_statistics``        - indicator + place -> a series over time for that
  place, or, with ``place_type``, one value for every place of that type
  inside it.

The agent's path is the one a researcher takes: find the indicator, see where
it has data, then fetch the values. Each call is ONE request to the API, run
through call_endpoint's code path (gateway.call_operation) under this tool's
own span, so the size cap, the error contract and the shaping are the same as
for any other operation; the result then loses its request metadata
(typed.minimize_response). A cut answer keeps ``meta.truncated``, with a
``retry_hint`` that names this tool's own arguments instead of call_endpoint's.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import Field

from ..observability import trace_mcp_tool
from ..server import mcp, read_only
from . import gateway
from .typed import minimize_response

_SEARCH = "statistics_search"
_RESOLVE = "statistics_places_resolve"
_SERIES = "statistics_series_indicator"
_PLACES = "statistics_places_indicator"

# The place types the API serves rows of (routers/statistics.py, /places).
PlaceType = Literal[
    "country",
    "state",
    "county",
    "city",
    "metro",
    "zcta",
    "admin1",
    "admin2",
    "nuts1",
    "nuts2",
    "nuts3",
]

# What a row of a /places answer keeps: the place's code and name beside its
# value. The place type is the answer's own `type` and the country is the
# parent's, so repeating them on every row would only spend the size cap.
_PLACE_ROW_FIELDS = ["place.id", "place.name", "date", "value"]

# meta.truncated.retry_hint of a cut answer, in this tool's own words: the
# hint the cut writes names call_endpoint's params, limit and fields.
_RETRY_HINTS = {
    _SEARCH: "Not every match fits in one answer: ask for a smaller limit.",
    _RESOLVE: "Not every match fits in one answer: give a fuller place name.",
    _SERIES: "Not every value fits in one answer: pass start and end for a shorter window.",
    _PLACES: (
        "Not every place fits in one answer: ask for a smaller parent place or a "
        "place type with fewer places."
    ),
}

_PERIOD = r"^\d{4}(-\d{2}(-\d{2})?)?$"


async def _call(
    operation_id: str, params: dict[str, Any], fields: list[str] | None = None
) -> dict[str, Any]:
    result = minimize_response(
        await gateway.call_operation(operation_id, params=params, fields=fields)
    )
    meta = result.get("meta") if isinstance(result, dict) else None
    truncated = meta.get("truncated") if isinstance(meta, dict) else None
    if isinstance(truncated, dict) and "retry_hint" in truncated:
        result["meta"] = {**meta, "truncated": {**truncated, "retry_hint": _RETRY_HINTS[operation_id]}}
    return result


@mcp.tool(annotations=read_only("Search statistics"))
@trace_mcp_tool("search_statistics")
async def search_statistics(
    query: Annotated[
        str,
        Field(
            min_length=2,
            max_length=200,
            description=(
                "What to look for, in plain words, for example median household "
                "income or internet access."
            ),
        ),
    ],
    place: Annotated[
        str | None,
        Field(
            max_length=40,
            description=(
                "Optional place code (USA, KEN, 06); keeps only indicators with "
                "data for that type of place."
            ),
        ),
    ] = None,
    limit: Annotated[
        int,
        Field(ge=1, le=50, description="Maximum number of indicators, 1 to 50."),
    ] = 10,
) -> dict[str, Any]:
    """Find official statistics indicators by meaning in the Sugra statistics catalog.

    The catalog holds indicators published by national statistical agencies
    and international organizations, such as population, income, prices,
    employment, health, education and housing, for countries, their regions,
    US states, counties, cities and ZIP code areas. Each result gives the
    indicator code, its name, unit and period, the place types with data, the
    first and last period, the agencies that publish it and a match score from
    0 to 1, best first. Use when the user asks for a statistic about a place;
    then pass the code to get_statistics.
    """
    params: dict[str, Any] = {"q": query, "limit": limit}
    if place is not None:
        params["place"] = place
    return await _call(_SEARCH, params)


@mcp.tool(annotations=read_only("Find statistics place"))
@trace_mcp_tool("find_statistics_place")
async def find_statistics_place(
    name: Annotated[
        str,
        Field(
            min_length=2,
            max_length=120,
            description="Name of the place, for example Los Angeles County or Nairobi.",
        ),
    ],
) -> dict[str, Any]:
    """Find the place codes get_statistics takes, from a place name.

    Returns up to 10 matching places, best first, each with its code, the code
    system, its name, its type (country, state, county, city and so on) and
    its country. Use when the user names a city or another place whose ISO,
    FIPS or NUTS code is not known.
    """
    return await _call(_RESOLVE, {"q": name})


@mcp.tool(annotations=read_only("Get statistics"))
@trace_mcp_tool("get_statistics")
async def get_statistics(
    indicator: Annotated[
        str,
        Field(
            max_length=80,
            description="Indicator code from search_statistics, for example median-household-income.",
        ),
    ],
    place: Annotated[
        str,
        Field(
            max_length=40,
            description=(
                "Place code: USA or KEN for a country, IN-MH for a region, 06 or "
                "06037 for a US state or county, DE11 for a European region, "
                "zcta:94043 for a US ZIP code area, or a code from "
                "find_statistics_place. With place_type, the region whose places "
                "are listed."
            ),
        ),
    ],
    place_type: Annotated[
        PlaceType | None,
        Field(
            description=(
                "Optional. List every place of this type inside place, one value "
                "each, for example county with place 06 for every county of "
                "California."
            ),
        ),
    ] = None,
    start: Annotated[
        str | None,
        Field(
            pattern=_PERIOD,
            description="First period of a series: 2020, 2020-06 or 2020-06-30. Not with place_type.",
        ),
    ] = None,
    end: Annotated[
        str | None,
        Field(
            pattern=_PERIOD,
            description="Last period of a series: 2024, 2024-06 or 2024-06-30. Not with place_type.",
        ),
    ] = None,
    date: Annotated[
        str | None,
        Field(
            pattern=r"^(latest|\d{4}(-\d{2}(-\d{2})?)?)$",
            description=(
                "With place_type only: latest (the default) for each place's "
                "latest value, or a period: 2023, 2023-06 or 2023-06-30."
            ),
        ),
    ] = None,
    agency: Annotated[
        str | None,
        Field(
            max_length=60,
            description=(
                "Optional agency code from search_statistics, to take that "
                "agency's values. Without it the first agency with data is used."
            ),
        ),
    ] = None,
) -> dict[str, Any]:
    """Get the values of one statistics indicator for a place, or for every place of a type in a region.

    Without place_type it returns the series of the place in date order,
    optionally from start to end. With place_type it returns one value for
    every place of that type inside place, for example every county of
    California (place 06, place_type county) or every NUTS 2 region of Germany
    (place DEU, place_type nuts2), at date. Every answer names the source of
    its values. A large answer is cut to fit and says so in meta.truncated.
    Use after search_statistics has given the indicator code.
    """
    if place_type is None:
        if date is not None:
            return {
                "error": "validation_failed",
                "reason": "date applies with place_type; for a series pass start and end.",
            }
        params: dict[str, Any] = {"indicator": indicator, "place": place}
        for key, value in (("start", start), ("end", end), ("agency", agency)):
            if value is not None:
                params[key] = value
        return await _call(_SERIES, params)
    if start is not None or end is not None:
        return {
            "error": "validation_failed",
            "reason": "start and end apply to a series; with place_type pass date.",
        }
    params = {"indicator": indicator, "parent": place, "type": place_type}
    for key, value in (("date", date), ("agency", agency)):
        if value is not None:
            params[key] = value
    return await _call(_PLACES, params, fields=_PLACE_ROW_FIELDS)
