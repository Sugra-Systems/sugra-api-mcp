###########################################
### Sugra API MCP Version 0.3.0         ###
###   MACRO TOOLS Version 0.3.0         ###
###########################################

### BEGIN # sugra_api_mcp/tools/macro.py ###
"""Macroeconomic data tools: GDP, CPI, central bank rates, bond yields, calendar, search."""

from __future__ import annotations

from typing import Any, Literal

from ..server import get_client, mcp, read_only

CentralBank = Literal["fed", "ecb", "boj", "boe", "snb", "pboc", "rba", "boc"]
YieldCountry = Literal["us", "uk", "ca", "se", "au", "no", "eu"]


### BEGIN # get_macro_indicator ###
@mcp.tool(annotations=read_only("Macro indicator"))
async def get_macro_indicator(
    country: str,
    section: str,
    start_date: str | None = None,
    end_date: str | None = None,
) -> dict[str, Any]:
    """Get a macroeconomic time series for a country.

    Returns a time series for the requested indicator, aggregated from primary sources
    (FRED, BIS, BOJ, BoE, StatCan, ABS, Eurostat, Destatis, and others).

    For central bank policy rates (Fed, ECB, BoJ, BoE), prefer get_central_bank_rate
    for richer data. For sovereign bond yields use get_bond_yields.

    Args:
        country: Country code. Use lowercase 2-letter ISO codes ("us", "de", "jp", "uk",
            "ca", "au", "ch") or "eu" for Eurozone aggregate.
        section: Indicator category. Common values: "gdp", "cpi", "unemployment",
            "interest-rate", "trade", "industrial-production", "retail-sales", "pmi".
        start_date: Optional start date in YYYY-MM-DD format.
        end_date: Optional end date in YYYY-MM-DD format.

    Examples:
        get_macro_indicator(country="us", section="cpi")
        get_macro_indicator(country="de", section="gdp", start_date="2020-01-01")
    """
    client = get_client()
    return await client.get(
        f"/api/v1/macro/{country.lower()}/{section.lower()}",
        params={"start_date": start_date, "end_date": end_date},
    )
### END # get_macro_indicator ###


### BEGIN # get_central_bank_rate ###
@mcp.tool(annotations=read_only("Central bank rate"))
async def get_central_bank_rate(
    bank: CentralBank = "fed",
    rate_type: str | None = None,
) -> dict[str, Any]:
    """Get current and historical policy rate for a major central bank.

    Dedicated tool for policy rates. For broader macro series (GDP, CPI,
    unemployment, trade), use get_macro_indicator. For sovereign yield curve
    data use get_bond_yields.

    Args:
        bank: Central bank code. "fed" (US Federal Reserve), "ecb" (European Central
            Bank), "boj" (Bank of Japan), "boe" (Bank of England), "snb" (Swiss
            National Bank), "pboc" (People's Bank of China), "rba" (Reserve Bank
            of Australia), "boc" (Bank of Canada). Default "fed".
        rate_type: Optional Fed-specific rate type ("effr", "sofr"). Ignored for
            other banks.

    Examples:
        get_central_bank_rate(bank="fed")
        get_central_bank_rate(bank="fed", rate_type="sofr")
        get_central_bank_rate(bank="ecb")
    """
    client = get_client()
    if bank == "fed":
        if rate_type:
            return await client.get(f"/api/v1/fed/rates/{rate_type.lower()}")
        return await client.get("/api/v1/fed/rates")
    country_map = {
        "ecb": "eu", "boj": "jp", "boe": "uk", "snb": "ch",
        "pboc": "cn", "rba": "au", "boc": "ca",
    }
    return await client.get(f"/api/v1/macro/{country_map[bank]}/interest-rate")
### END # get_central_bank_rate ###


### BEGIN # search_economic_series ###
@mcp.tool(annotations=read_only("Search economic series"))
async def search_economic_series(
    query: str,
    limit: int = 20,
) -> dict[str, Any]:
    """Search across all economic data series catalogs (FRED, BIS, BOJ, BoE, Eurostat).

    Discovery tool - use before get_macro_indicator when you don't know the exact
    country/section parameters.

    Args:
        query: Free-text search query.
        limit: Maximum number of results to return. Default 20.

    Examples:
        search_economic_series(query="inflation breakeven")
        search_economic_series(query="chinese export prices")
    """
    client = get_client()
    return await client.get("/api/v1/macro/search", params={"q": query, "limit": limit})
### END # search_economic_series ###


### BEGIN # get_bond_yields ###
@mcp.tool(annotations=read_only("Bond yields"))
async def get_bond_yields(country: YieldCountry = "us") -> dict[str, Any]:
    """Get sovereign bond yields across tenors for a country.

    Returns yield-curve data from the country's primary source: Bank of Canada
    for "ca", Bank of England for "uk", Riksbank for "se", Norges Bank for "no",
    MULTPL (US Treasury archive) for "us". For broader yield analysis across
    multiple countries, call this tool per country.

    Args:
        country: Lowercase 2-letter country code. Supported: "us", "uk", "ca",
            "se" (Sweden), "no" (Norway), "au" (Australia), "eu" (Eurozone).
            Default "us".

    Examples:
        get_bond_yields()
        get_bond_yields(country="uk")
        get_bond_yields(country="se")
    """
    client = get_client()
    path_map = {
        "us": "/api/v1/multpl/treasury",
        "uk": "/api/v1/boe/yields",
        "ca": "/api/v1/boc/yields",
        "se": "/api/v1/riksbank/government-bonds",
        "no": "/api/v1/macro/no/interest-rate",
        "au": "/api/v1/macro/au/interest-rate",
        "eu": "/api/v1/macro/eu/interest-rate",
    }
    return await client.get(path_map[country])
### END # get_bond_yields ###


### BEGIN # get_economic_calendar ###
@mcp.tool(annotations=read_only("Economic calendar"))
async def get_economic_calendar(
    start_date: str | None = None,
    end_date: str | None = None,
) -> dict[str, Any]:
    """Get upcoming macroeconomic data releases: CPI, NFP, FOMC decisions, GDP, PMI.

    Covers all major economies via Finnhub's calendar aggregation. Includes
    release time (UTC), prior value, forecast consensus where available.

    Args:
        start_date: Optional window start (YYYY-MM-DD). Default is today.
        end_date: Optional window end (YYYY-MM-DD). Default is one week from today.

    Examples:
        get_economic_calendar()
        get_economic_calendar(start_date="2026-05-01", end_date="2026-05-15")
    """
    client = get_client()
    return await client.get(
        "/api/v1/finnhub/calendar/economic",
        params={"from": start_date, "to": end_date},
    )
### END # get_economic_calendar ###

### END # sugra_api_mcp/tools/macro.py ###
