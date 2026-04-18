###########################################
### Sugra API MCP Version 0.3.0         ###
###   FUNDAMENTALS TOOLS Version 0.3.0  ###
###########################################

### BEGIN # sugra_api_mcp/tools/fundamentals.py ###
"""Company fundamentals tools: overview, filings, financial statements, analyst ratings."""

from __future__ import annotations

import asyncio
from typing import Any, Literal

from ..server import get_client, mcp, read_only


### BEGIN # get_company_overview ###
@mcp.tool(annotations=read_only("Company overview"))
async def get_company_overview(ticker: str) -> dict[str, Any]:
    """Get headline financial metrics for a publicly-traded company.

    Returns market cap, P/E ratio, revenue, margins, dividend yield, and other
    headline metrics sourced from SEC EDGAR filings and market data.

    Args:
        ticker: US stock ticker symbol (uppercase). Examples: "AAPL", "MSFT", "GOOGL".

    Examples:
        get_company_overview(ticker="AAPL")
    """
    client = get_client()
    return await client.get(f"/api/v1/fundamentals/{ticker.upper()}/overview")
### END # get_company_overview ###


### BEGIN # get_company_filings ###
@mcp.tool(annotations=read_only("Company filings"))
async def get_company_filings(
    ticker: str,
    jurisdiction: Literal["us", "jp"] = "us",
) -> dict[str, Any]:
    """Get regulatory filings for a company (SEC EDGAR for US, EDINET for Japan).

    Returns filings metadata: 10-K, 10-Q, 8-K for US; annual and quarterly
    reports for Japan. For financial statement line items use
    `get_company_financials` instead.

    Args:
        ticker: Company ticker. For US use SEC ticker ("AAPL"). For Japan use
            EDINET ticker or 4-digit code ("7203" for Toyota).
        jurisdiction: "us" for SEC EDGAR, "jp" for EDINET. Default "us".

    Examples:
        get_company_filings(ticker="AAPL")
        get_company_filings(ticker="7203", jurisdiction="jp")
    """
    client = get_client()
    if jurisdiction == "jp":
        return await client.get(f"/api/v1/edinet/{ticker}/filings")
    return await client.get(f"/api/v1/fundamentals/{ticker.upper()}/profile")
### END # get_company_filings ###


### BEGIN # get_company_financials ###
@mcp.tool(annotations=read_only("Company financials"))
async def get_company_financials(
    ticker: str,
    statement: Literal["income", "balance", "cashflow"],
) -> dict[str, Any]:
    """Get income statement, balance sheet, or cash flow statement for a US company.

    Returns structured financial data parsed from SEC XBRL filings.

    Args:
        ticker: US stock ticker (uppercase). Example: "AAPL".
        statement: Which financial statement: "income", "balance", or "cashflow".

    Examples:
        get_company_financials(ticker="AAPL", statement="income")
        get_company_financials(ticker="MSFT", statement="cashflow")
    """
    client = get_client()
    return await client.get(f"/api/v1/fundamentals/{ticker.upper()}/{statement}")
### END # get_company_financials ###


### BEGIN # get_analyst_ratings ###
@mcp.tool(annotations=read_only("Analyst ratings"))
async def get_analyst_ratings(symbol: str) -> dict[str, Any]:
    """Get analyst consensus, price targets, and recent upgrades/downgrades in one call.

    Aggregates four Sugra endpoints (analyst-targets, ratings, recommendations,
    upgrades-downgrades) into a single consolidated view so the LLM does not
    have to fan out.

    Args:
        symbol: US stock ticker (uppercase). Example: "NVDA".

    Returns:
        A dict with keys `targets`, `ratings`, `recommendations`, and
        `upgrades_downgrades` - each either upstream payload or an error object
        (one missing source does not fail the whole call).

    Examples:
        get_analyst_ratings(symbol="NVDA")
    """
    client = get_client()
    sym = symbol.upper()
    paths = {
        "targets": f"/api/v2/quotes/{sym}/analyst-targets",
        "ratings": f"/api/v2/quotes/{sym}/ratings",
        "recommendations": f"/api/v2/quotes/{sym}/recommendations",
        "upgrades_downgrades": f"/api/v2/quotes/{sym}/upgrades-downgrades",
    }
    results = await asyncio.gather(
        *(client.get(p) for p in paths.values()),
        return_exceptions=True,
    )
    return {
        key: (
            {"error": str(r)} if isinstance(r, BaseException) else r
        )
        for key, r in zip(paths.keys(), results, strict=True)
    }
### END # get_analyst_ratings ###

### END # sugra_api_mcp/tools/fundamentals.py ###
