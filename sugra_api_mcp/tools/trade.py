###########################################
### Sugra API MCP Version 0.3.0         ###
###   TRADE TOOLS Version 0.3.0         ###
###########################################

### BEGIN # sugra_api_mcp/tools/trade.py ###
"""International trade tools: bilateral trade flows via UN Comtrade and WTO."""

from __future__ import annotations

from typing import Any

from ..server import get_client, mcp, read_only


### BEGIN # get_trade_flows ###
@mcp.tool(annotations=read_only("Trade flows"))
async def get_trade_flows(
    reporter: str,
    partner: str | None = None,
    commodity: str | None = None,
    year: int | None = None,
) -> dict[str, Any]:
    """Get bilateral international trade flows (exports / imports) via UN Comtrade.

    Trade-flow data is the foundation for supply-chain, tariff-impact, and
    macro-balance analysis. Sugra aggregates UN Comtrade + WTO into one call.

    Args:
        reporter: ISO-3 code of the exporting / reporting country. Examples:
            "USA", "CHN", "DEU", "JPN".
        partner: Optional ISO-3 code of the importing / partner country. Omit
            for world-aggregate. Example: "CHN".
        commodity: Optional commodity filter. HS (Harmonized System) code or
            keyword. Example: "semiconductors", "8542" (HS chapter for
            integrated circuits).
        year: Optional year (YYYY). Defaults to latest available (typically
            previous calendar year).

    Examples:
        get_trade_flows(reporter="USA")
        get_trade_flows(reporter="USA", partner="CHN")
        get_trade_flows(reporter="DEU", commodity="vehicles", year=2024)
    """
    client = get_client()
    return await client.get(
        "/api/v1/comtrade/trade",
        params={
            "reporter": reporter.upper(),
            "partner": partner.upper() if partner else None,
            "commodity": commodity,
            "year": year,
        },
    )
### END # get_trade_flows ###

### END # sugra_api_mcp/tools/trade.py ###
