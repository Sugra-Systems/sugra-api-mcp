###########################################
### Sugra API MCP Version 0.3.0         ###
###   GOVERNMENT TOOLS Version 0.3.0    ###
###########################################

### BEGIN # sugra_api_mcp/tools/government.py ###
"""Government and trade data tools: federal spending, Treasury fiscal data."""

from __future__ import annotations

from typing import Any, Literal

from ..server import get_client, mcp, read_only

TreasuryDataset = Literal["debt", "debt-history", "rates", "interest-expense", "catalog"]


### BEGIN # get_government_spending ###
@mcp.tool(annotations=read_only("Government spending"))
async def get_government_spending(
    agency_code: str | None = None,
    fiscal_year: int | None = None,
) -> dict[str, Any]:
    """Get US federal government spending, budget, and agency data from USAspending.gov.

    Without agency_code, returns the list of federal agencies and budget categories.
    With agency_code, returns that agency's spending profile.

    Args:
        agency_code: Optional US federal agency code. Examples: "097" (Department of Defense),
            "019" (Department of State), "075" (Department of Health and Human Services).
            Use the agencies list (call without this arg) to discover codes.
        fiscal_year: Optional fiscal year (e.g. 2025).

    Examples:
        get_government_spending()
        get_government_spending(agency_code="097")
        get_government_spending(agency_code="019", fiscal_year=2024)
    """
    client = get_client()
    if agency_code:
        return await client.get(
            f"/api/v1/usaspending/agency/{agency_code}",
            params={"fiscal_year": fiscal_year},
        )
    return await client.get("/api/v1/usaspending/agencies")
### END # get_government_spending ###


### BEGIN # get_treasury_data ###
@mcp.tool(annotations=read_only("Treasury data"))
async def get_treasury_data(
    dataset: TreasuryDataset = "debt",
    limit: int = 100,
) -> dict[str, Any]:
    """Get US Treasury fiscal data: national debt, Treasury rates, interest expense.

    Args:
        dataset: Which Treasury dataset to fetch.
            "debt" - current total public debt.
            "debt-history" - historical debt time series (since 1790).
            "rates" - Treasury yield curve rates (3mo, 2yr, 10yr, 30yr, etc.).
            "interest-expense" - monthly federal interest payments.
            "catalog" - list all available Treasury datasets.
        limit: Maximum records to return (applies to time series). Default 100.

    Examples:
        get_treasury_data(dataset="debt")
        get_treasury_data(dataset="rates")
        get_treasury_data(dataset="debt-history", limit=50)
    """
    client = get_client()
    return await client.get(f"/api/v1/treasury/{dataset}", params={"limit": limit})
### END # get_treasury_data ###

### END # sugra_api_mcp/tools/government.py ###
