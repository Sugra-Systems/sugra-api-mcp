###########################################
### Sugra API MCP Version 0.3.0         ###
###   PHYSICAL TOOLS Version 0.3.0      ###
###########################################

### BEGIN # sugra_api_mcp/tools/physical.py ###
"""Physical world tools: weather, natural events, vessel activity."""

from __future__ import annotations

from typing import Any, Literal

from ..server import get_client, mcp, read_only

WeatherMode = Literal["current", "forecast", "history"]
EventType = Literal["earthquake", "wildfire", "disaster", "all"]


### BEGIN # get_weather ###
@mcp.tool(annotations=read_only("Weather"))
async def get_weather(
    mode: WeatherMode = "current",
    city: str | None = None,
    country: str | None = None,
    latitude: float | None = None,
    longitude: float | None = None,
    temperature_unit: Literal["celsius", "fahrenheit"] = "celsius",
) -> dict[str, Any]:
    """Get current weather, forecast, or historical weather for any location globally.

    Provide either `city` (optionally with `country`) OR `latitude`+`longitude`.

    Args:
        mode: "current" for now, "forecast" for 7-day outlook, "history" for past weather.
        city: City name (e.g. "Tokyo", "New York"). Alternative to lat/lon.
        country: Country code or name, used with city for disambiguation.
        latitude: Latitude (-90 to 90). Used with longitude.
        longitude: Longitude (-180 to 180).
        temperature_unit: "celsius" (default) or "fahrenheit".

    Examples:
        get_weather(mode="current", city="Tokyo")
        get_weather(mode="forecast", city="New York", country="US")
        get_weather(mode="current", latitude=40.7, longitude=-74.0)
    """
    client = get_client()
    params: dict[str, Any] = {"temperature_unit": temperature_unit}
    if latitude is not None and longitude is not None:
        params["latitude"] = latitude
        params["longitude"] = longitude
    if city:
        params["city"] = city
    if country:
        params["country"] = country
    return await client.get(f"/api/v1/weather/{mode}", params=params)
### END # get_weather ###


### BEGIN # get_natural_events ###
@mcp.tool(annotations=read_only("Natural events"))
async def get_natural_events(
    event_type: EventType = "all",
    country: str | None = None,
    days: int = 7,
    min_magnitude: float | None = None,
) -> dict[str, Any]:
    """Get recent natural events: earthquakes (USGS), wildfires (NASA FIRMS), disasters (GDACS).

    Args:
        event_type: "earthquake" for USGS quakes, "wildfire" for active fires,
            "disaster" for GDACS-tracked storms/floods/droughts, "all" for
            GDACS unified feed. Default "all".
        country: Optional ISO-3 country code filter ("USA", "JPN", "CHN").
            Applies to wildfires and disasters; ignored for earthquakes (global).
        days: Lookback window in days. Default 7. Max 10 for wildfires.
        min_magnitude: For earthquakes, minimum magnitude filter (e.g. 4.5).
            For wildfires, minimum FRP (fire radiative power) in MW. Ignored
            for other event types.

    Examples:
        get_natural_events(event_type="earthquake", min_magnitude=5.0, days=3)
        get_natural_events(event_type="wildfire", country="USA", days=3)
        get_natural_events(event_type="disaster")
    """
    client = get_client()
    if event_type == "earthquake":
        params: dict[str, Any] = {"days": days}
        if min_magnitude is not None:
            params["min_magnitude"] = min_magnitude
        return await client.get("/api/v1/earthquakes/recent", params=params)
    if event_type == "wildfire":
        return await client.get(
            "/api/v1/fires/global",
            params={"country": country, "day_range": days, "min_frp": min_magnitude},
        )
    return await client.get(
        "/api/v1/disasters/events",
        params={"country": country, "days": days},
    )
### END # get_natural_events ###


### BEGIN # get_vessel_activity ###
@mcp.tool(annotations=read_only("Vessel activity"))
async def get_vessel_activity(
    scope: Literal["events", "search", "detail", "chokepoints"] = "events",
    flag: str | None = None,
    vessel_id: str | None = None,
    days: int = 30,
) -> dict[str, Any]:
    """Get maritime vessel activity from Global Fishing Watch and port-traffic data.

    Covers fishing, cargo, and tanker movements plus major chokepoint transits
    (Suez, Panama, Hormuz, Malacca). Unique Sugra signal - most data APIs do
    not surface this.

    Args:
        scope: "events" for recent fishing/port/loitering events (default),
            "search" for vessel lookup by flag, "detail" for single vessel
            (requires vessel_id), "chokepoints" for chokepoint transit activity.
        flag: ISO-3 country code flag filter for "search" (e.g. "CHN", "USA").
        vessel_id: MMSI or vessel UUID for "detail" scope.
        days: Lookback window in days. Default 30.

    Examples:
        get_vessel_activity(scope="events", days=7)
        get_vessel_activity(scope="search", flag="CHN")
        get_vessel_activity(scope="chokepoints")
    """
    client = get_client()
    if scope == "detail":
        if not vessel_id:
            return {"error": "vessel_id is required for scope=detail"}
        return await client.get(f"/api/v1/gfw/vessels/{vessel_id}")
    if scope == "search":
        return await client.get("/api/v1/gfw/vessels/search", params={"flag": flag})
    if scope == "chokepoints":
        return await client.get("/api/v1/maritime/chokepoints/activity")
    return await client.get("/api/v1/gfw/events", params={"days": days})
### END # get_vessel_activity ###

### END # sugra_api_mcp/tools/physical.py ###
