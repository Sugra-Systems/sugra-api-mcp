"""Place guard for fetch_data.

An operation that takes its place through an optional country parameter
answers for a default place, or for every place, when the parameter is
absent. fetch_data picks
the operation from the query alone, so a query naming Germany ran such an
operation without a country and returned another country's data with no
sign of the swap. The guard asks for the parameter instead of filling it:
operations spell places differently (ISO2, ISO3, lowercase codes, names),
and only the operation's own parameter description says which.
"""

from __future__ import annotations

from typing import Any, NamedTuple

from .aliases import detect_query_countries
from .models import Endpoint

# Parameter names that choose an operation's place, the list form first so
# an operation taking both is asked for the list.
PLACE_PARAMETERS: tuple[str, ...] = ("countries", "country")


class PlaceGap(NamedTuple):
    parameter: str
    countries: list[str]


def place_gap(endpoint: Endpoint, query: str, params: dict[str, Any]) -> PlaceGap | None:
    """The place parameter fetch_data must ask for, with the countries named.

    None when the operation declares no place parameter, the params already
    carry one, or the query names no country. A place key the operation does
    not declare counts as carried: the unknown-parameter refusal that follows
    names the right key, which beats asking for it here.

    None as well for an operation with parameter groups: each of its groups
    names the place itself (coordinates, a city, a port, a country or a
    network), so the group check already makes the caller choose it, and a
    country asked on top would clash with mutually exclusive groups.
    """
    declared = {parameter.name for parameter in endpoint.parameters}
    name = next((candidate for candidate in PLACE_PARAMETERS if candidate in declared), None)
    if name is None or endpoint.required_groups or any(key in params for key in PLACE_PARAMETERS):
        return None
    countries = detect_query_countries(query)
    if not countries:
        return None
    return PlaceGap(name, sorted(countries))
