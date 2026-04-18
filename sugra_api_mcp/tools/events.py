###########################################
### Sugra API MCP Version 0.3.0         ###
###   EVENTS TOOLS Version 0.3.0        ###
###########################################

### BEGIN # sugra_api_mcp/tools/events.py ###
"""Corporate events tools: earnings calendar."""

from __future__ import annotations

from typing import Any

from ..server import get_client, mcp, read_only


### BEGIN # get_earnings_calendar ###
@mcp.tool(annotations=read_only("Earnings calendar"))
async def get_earnings_calendar(
    start_date: str | None = None,
    end_date: str | None = None,
    symbol: str | None = None,
) -> dict[str, Any]:
    """Get upcoming or recent corporate earnings events across all US public companies.

    Returns each event with ticker, company name, report date, time of day
    (pre-market / after-hours), EPS estimate, and revenue estimate where
    available.

    Args:
        start_date: Optional window start (YYYY-MM-DD). Default is today.
        end_date: Optional window end (YYYY-MM-DD). Default is two weeks from today.
        symbol: Optional single-ticker filter (uppercase). Example: "AAPL".

    Examples:
        get_earnings_calendar()
        get_earnings_calendar(start_date="2026-05-01", end_date="2026-05-07")
        get_earnings_calendar(symbol="NVDA")
    """
    client = get_client()
    return await client.get(
        "/api/v1/finnhub/calendar/earnings",
        params={"from": start_date, "to": end_date, "symbol": symbol},
    )
### END # get_earnings_calendar ###

### END # sugra_api_mcp/tools/events.py ###
