###########################################
### Sugra API MCP Version 0.3.0         ###
###   EVENTS TOOLS Version 0.3.0        ###
###########################################

### BEGIN # sugra_api_mcp/tools/events.py ###
"""Corporate events tools: earnings calendar."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
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

    Default window is the next 7 days because the full 2-week global feed
    exceeds the MCP 25000-token response limit. Pass explicit dates for a
    different window, and narrow by ticker if you want a broader range.

    Args:
        start_date: Optional window start (YYYY-MM-DD). Default is today (UTC).
        end_date: Optional window end (YYYY-MM-DD). Default is 7 days from today.
        symbol: Optional single-ticker filter (uppercase). Example: "AAPL".

    Examples:
        get_earnings_calendar()
        get_earnings_calendar(symbol="NVDA", start_date="2026-04-01", end_date="2026-06-30")
        get_earnings_calendar(start_date="2026-05-01", end_date="2026-05-07")
    """
    client = get_client()
    today = datetime.now(UTC).date()
    start = start_date or today.isoformat()
    end = end_date or (today + timedelta(days=7)).isoformat()
    return await client.get(
        "/api/v1/finnhub/calendar/earnings",
        params={"from_date": start, "to_date": end, "symbol": symbol},
    )
### END # get_earnings_calendar ###

### END # sugra_api_mcp/tools/events.py ###
