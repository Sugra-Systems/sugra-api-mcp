"""Workflow prompts: reusable multi-step recipes over the gateway tools.

Six MCP prompts that compose the package tool surface (fetch_data,
search_endpoints, describe_endpoint, call_endpoint, list_toolsets,
list_sources, sugra_entity_screen, sugra_entity_lookup) into short numbered
workflows an agent can follow. Prompts register at import against the global
``mcp`` singleton, the same decorator-at-import pattern the tool modules use.

These prompts ship in the PyPI package, so they reference only the tools that
exist on every transport - never the hosted-only tools. Prompt text is public
copy: hyphens only, no emoji, and source names follow the Sugra naming policy
(sovereign, intergovernmental, and academic sources are named openly).

MCP prompt arguments arrive as strings over the wire, so every prompt
parameter is typed ``str`` and interpolated into the instruction text.
"""

from __future__ import annotations

from ..server import mcp


@mcp.prompt(title="Market snapshot")
def market_snapshot(symbol: str) -> str:
    """Build a sourced price and profile snapshot for one ticker symbol."""
    return f"""Build a market snapshot for {symbol} using the Sugra API MCP tools.

1. Call call_endpoint with operation_id "quotes_symbol_price" and params {{"symbol": "{symbol}"}} for the current price.
2. Call call_endpoint with operation_id "fundamentals_ticker_profile" and params {{"ticker": "{symbol}"}} for the company profile.
3. If either call errors, run search_endpoints with a query like "{symbol} price" or "company profile", then describe_endpoint on the best match to confirm parameters before calling it.
4. Add broad context with operation_id "market_summary" (no parameters).
5. Present a compact snapshot: latest price with its timestamp, key profile facts, and the market backdrop.
6. Attribute every figure to the source named in the response meta and state the as-of date for each number."""


@mcp.prompt(title="Macro briefing")
def macro_briefing(country: str) -> str:
    """Brief the key macro indicators for a country from sovereign sources."""
    return f"""Prepare a macro briefing for {country} covering inflation, policy rate, GDP, and unemployment.

1. Start with call_endpoint operation_id "macro_country_profile" and params {{"country": "{country}"}} for a composite overview.
2. For the United States, pull individual series with operation_id "fred_series_series_id": CPIAUCSL (CPI), UNRATE (unemployment), GDP (output).
3. For other countries, try operation_id "imf_country_indicator" (params: country, indicator), or run search_endpoints with queries like "CPI {country}", "policy rate {country}", "GDP {country}", "unemployment {country}".
4. Use describe_endpoint on any match before calling it to confirm required parameters; fetch_data is a one-step alternative for simple queries.
5. Cover all four pillars, quoting the latest value, the reference period, and the direction of change.
6. Cite each figure to its sovereign or intergovernmental source, for example FRED, ECB, IMF, or the national statistical office."""


@mcp.prompt(title="Sanctions screening")
def sanctions_screening(name: str) -> str:
    """Screen a name against the sanctions corpus and report the signal."""
    return f"""Screen the name "{name}" against the Sugra sanctions corpus.

1. Call sugra_entity_screen with name="{name}". Add country, dob, or nationality when known to narrow matching.
2. Read the status field: "clear", "review", or "hit".
3. Report the top matches with their scores and the lists they came from.
4. Always relay the disclaimer: the result is a screening signal, not a compliance determination. A "clear" result is not proof of absence, and a "hit" is a candidate match to review, not a finding. PEP and adverse-media coverage is supplementary and non-comprehensive.
5. If a registry identifier is known, call sugra_entity_lookup with anchor "lei" (GLEIF registry) or "vat" (EU VIES validation) to confirm registry identity alongside the screening signal.
6. Recommend that any "review" or "hit" outcome goes to a qualified compliance reviewer."""


@mcp.prompt(title="Sector compare")
def sector_compare(sector_a: str, sector_b: str) -> str:
    """Compare two sectors through ETF and valuation endpoints."""
    return f"""Compare the {sector_a} and {sector_b} sectors using catalog ETF and valuation endpoints.

1. Run search_endpoints with queries like "{sector_a} ETF" and "{sector_b} ETF" to pick one representative sector ETF per side.
2. For each ETF, call call_endpoint operation_id "etf_symbol_snapshot" with params {{"symbol": ...}} for the latest snapshot.
3. Add composition trend with operation_id "etf_symbol_sector_weightings_history" for each symbol.
4. For valuation depth, pick two or three large holdings per sector and call operation_id "fundamentals_ticker_ratios" on each ticker.
5. Use operation_id "multpl_sp500" as the broad-market valuation baseline.
6. Present a side-by-side comparison: snapshot metrics, weighting trends, and holding ratios versus the baseline, each with source attribution and as-of dates.
7. Close with the caveat that this is data presentation, not investment advice."""


@mcp.prompt(title="Earth conditions")
def earth_conditions(lat: str, lon: str) -> str:
    """Report weather, air quality, and nearby hazards for a coordinate."""
    return f"""Report current Earth conditions at latitude {lat}, longitude {lon}.

1. Call call_endpoint operation_id "weather" with params {{"lat": {lat}, "lon": {lon}}} for point weather.
2. Call operation_id "air_quality" with the same lat and lon for satellite observations plus the model layer.
3. Check seismicity with operation_id "hazards_earthquakes", passing a bounding box of about 5 degrees around the point (min_lat, max_lat, min_lon, max_lon).
4. Call operation_id "hazards_alerts" with the same bounding box for multi-hazard alerts.
5. When fire risk matters, call operation_id "hazards_wildfires" (the bounding box is required there).
6. Use describe_endpoint on any of these first if a call returns a parameter error.
7. Summarize current weather, air quality, and active hazards, attributing each block to its source (for example NOAA, USGS, GDACS, EUMETSAT, NASA FIRMS) with observation times."""


@mcp.prompt(title="Source overview")
def source_overview(domain: str) -> str:
    """Explain what the catalog offers for a data domain, sources included."""
    return f"""Explain what the Sugra catalog offers for the {domain} domain.

1. Call list_toolsets to see every endpoint group with its endpoint count.
2. Call list_sources for the source families behind those groups.
3. Run search_endpoints with query "{domain}" (add the toolset filter when one matches) to sample concrete endpoints, then describe_endpoint on the most relevant hits.
4. Frame the sources by tier and name them openly: sovereign (for example FRED, BLS, ECB, NOAA, USGS), intergovernmental (for example IMF, GDACS, EUMETSAT), and academic or nonprofit programs.
5. Summarize the {domain} coverage: what data exists, typical parameters, and three to five example operation_ids to call next.
6. Note that the catalog spans 1,500+ endpoints across 160+ primary sources, all reachable through call_endpoint or fetch_data."""
