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

A ChatGPT session (server.client_profile) is not listed the generic catalog
tools (server.CHATGPT_UNLISTED_TOOLS), so a prompt whose recipe runs through
them renders its ChatGPT wording for that session instead: the same steps over
the typed tools it is listed, each call written as ``<tool> with {json}`` so a
test can hold every one to that tool's input schema. Every other client gets
the text below each ChatGPT branch, unchanged.

MCP prompt arguments arrive as strings over the wire, so every prompt
parameter is typed ``str`` and interpolated into the instruction text.
"""

from __future__ import annotations

from ..server import CHATGPT_PROFILE, client_profile, mcp


def _chatgpt() -> bool:
    return client_profile() == CHATGPT_PROFILE


@mcp.prompt(title="Market snapshot")
def market_snapshot(symbol: str) -> str:
    """Build a sourced price and profile snapshot for one ticker symbol."""
    if _chatgpt():
        return f"""Build a market snapshot for {symbol} using the Sugra API MCP tools.

1. Call get_stock_quotes with {{"symbols": "{symbol}"}} for the current price.
2. Call get_stock_statistics with {{"symbol": "{symbol}"}} for the key company facts: valuation multiples, market capitalisation, margins and dividend yield. For an ETF or a fund, call get_fund_summary with {{"symbol": "{symbol}"}} instead.
3. If a call errors because the symbol is not known, call search_tickers with the company name as q to find the symbol, then call again.
4. Add broad context with get_sector_relative_strength (no inputs needed) for how US sectors are moving.
5. Present a compact snapshot: latest price with its timestamp, key profile facts, and the market backdrop.
6. Attribute every figure to its source and state the as-of date for each number."""
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
    if _chatgpt():
        return f"""Prepare a macro briefing for {country} covering inflation, policy rate, GDP, and unemployment.

1. Start with get_country_macro_profile with {{"country": "{country}"}} for a composite overview. Its rates are fractions: 0.025 means 2.5%.
2. For the United States, read rates, not index levels, one FRED series per call:
   - inflation: get_economic_series with {{"series_id": "CPIAUCSL", "units": "pc1", "limit": 13}}, then the same for CPILFESL (core CPI) and PCEPILFE (core PCE); quote the percent change from a year ago, never the index value;
   - policy rate: get_economic_series with {{"series_id": "DFEDTARU", "limit": 1}} and with {{"series_id": "FEDFUNDS", "limit": 13}}; the upper bound of the target range and the effective rate, in percent;
   - growth: get_economic_series with {{"series_id": "GDPC1", "units": "pca", "limit": 4}}; real GDP, quarter over quarter at an annual rate;
   - labor market: get_economic_series with {{"series_id": "UNRATE", "limit": 13}}, the same for CIVPART (participation), with {{"series_id": "PAYEMS", "units": "chg", "limit": 13}} for the monthly payroll change, and with {{"series_id": "ICSA", "limit": 13}} for weekly initial claims.
3. For other countries, call get_consumer_prices_by_country for inflation, get_central_bank_policy_rates for the policy rate, get_country_indicator with {{"country": "{country}", "indicator": "gdp"}} for GDP, and get_unemployment_by_demographics for unemployment.
4. Read each tool's input schema before calling it to confirm its required inputs and allowed values.
5. Cover all four pillars, quoting the latest value, the reference period, and the direction of change.
6. Cite each figure to its sovereign or intergovernmental source, for example FRED, BIS, World Bank, ILO, or the national statistical office."""
    return f"""Prepare a macro briefing for {country} covering inflation, policy rate, GDP, and unemployment.

1. Start with call_endpoint operation_id "macro_country_profile" and params {{"country": "{country}"}} for a composite overview. Its rates are fractions: 0.025 means 2.5%.
2. For the United States, read rates, not index levels, with call_endpoint operation_id "macro_multi" (at most five series per call):
   - inflation: {{"series": "us/cpi,us/core-cpi,us/core-pce", "transform": "yoy_pct", "last_n": 13}}; quote value_yoy_pct, never the index value;
   - policy rate: {{"series": "us/fedfunds-target,us/fedfunds", "last_n": 13}}; the upper bound of the target range and the effective rate, in percent;
   - growth: {{"series": "us/real-gdp", "transform": "qoq_ann_pct", "last_n": 4}}; real GDP, quarter over quarter at an annual rate;
   - labor market: {{"series": "us/unrate,us/lfpr,us/payrolls,us/initial-claims", "last_n": 13}}; unemployment beside participation, the monthly payroll change (value_mom_abs) and weekly initial claims.
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
    if _chatgpt():
        return f"""Compare the {sector_a} and {sector_b} sectors using sector ETF and valuation tools.

1. Call get_sector_relative_strength with {{"window": "1m"}} to see where both sectors rank among US sectors; call it again with "ytd" or "1y" for a longer view.
2. Call search_etfs with {{"q": "{sector_a}"}} and with {{"q": "{sector_b}"}} to pick one representative sector ETF per side.
3. For each ETF, call get_stock_quotes with its symbol for the latest price and get_fund_summary for its expense ratio, total assets and top holdings.
4. For valuation depth, pick two or three large holdings per sector (get_etf_holdings lists them for a US ETF) and call get_financial_ratios on each ticker.
5. Present a side-by-side comparison: price and fund metrics, relative strength, and holding ratios, each with source attribution and as-of dates.
6. Close with the caveat that this is data presentation, not investment advice."""
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
    if _chatgpt():
        return f"""Report current Earth conditions around latitude {lat}, longitude {lon}.

1. Call get_hazard_alerts for the current multi-hazard alerts and keep those near the point.
2. Call get_recent_earthquakes with {{"magnitude": "2.5", "period": "week"}} and keep the events within about 5 degrees of the point.
3. Call get_tropical_cyclones for active storms, and get_disaster_events for recent floods, droughts, volcanic activity and wildfires, keeping those near the point.
4. For current weather, find an airport near the point with search_airports (by name or two-letter country code), then call get_airport_weather_reports with its four-letter ICAO code as ids.
5. The tools listed here take no coordinates and give no air quality reading for a point: say so rather than estimate one.
6. Summarize current weather, active hazards and recent seismicity, attributing each block to its source (for example NOAA, USGS, GDACS) with observation times."""
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
    if _chatgpt():
        return f"""Explain what the Sugra catalog offers for the {domain} domain.

1. Call list_toolsets to see every endpoint group with its endpoint count.
2. Call list_sources for the source families behind those groups.
3. Pick the tools listed in this session whose descriptions fall under {domain}, and read their input schemas for the inputs each takes.
4. Frame the sources by tier and name them openly: sovereign (for example FRED, BLS, ECB, NOAA, USGS), intergovernmental (for example IMF, GDACS, EUMETSAT), and academic or nonprofit programs.
5. Summarize the {domain} coverage: what data exists, typical inputs, and three to five example tools to call next.
6. Note that the catalog spans 1,600+ endpoints across 160+ primary sources, and that the tools listed in this session reach a part of them."""
    return f"""Explain what the Sugra catalog offers for the {domain} domain.

1. Call list_toolsets to see every endpoint group with its endpoint count.
2. Call list_sources for the source families behind those groups.
3. Run search_endpoints with query "{domain}" (add the toolset filter when one matches) to sample concrete endpoints, then describe_endpoint on the most relevant hits.
4. Frame the sources by tier and name them openly: sovereign (for example FRED, BLS, ECB, NOAA, USGS), intergovernmental (for example IMF, GDACS, EUMETSAT), and academic or nonprofit programs.
5. Summarize the {domain} coverage: what data exists, typical parameters, and three to five example operation_ids to call next.
6. Note that the catalog spans 1,600+ endpoints across 160+ primary sources, all reachable through call_endpoint or fetch_data."""
