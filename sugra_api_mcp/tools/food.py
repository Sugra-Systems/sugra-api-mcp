###########################################
### Sugra API MCP Version 0.3.0         ###
###   FOOD TOOLS Version 0.3.0          ###
###########################################

### BEGIN # sugra_api_mcp/tools/food.py ###
"""Agricultural and food tools: FAOSTAT production, trade, prices, food security."""

from __future__ import annotations

from typing import Any, Literal

from ..server import get_client, mcp, read_only

FoodDomain = Literal["production", "trade", "prices", "food-balance", "food-security"]


### BEGIN # get_food_indicator ###
@mcp.tool(annotations=read_only("Food indicator"))
async def get_food_indicator(
    indicator: FoodDomain,
    country: str | None = None,
    commodity: str | None = None,
    year: int | None = None,
) -> dict[str, Any]:
    """Get agricultural and food data from FAOSTAT (UN Food and Agriculture Organization).

    Covers crop and livestock production, food trade balances, retail food
    prices, dietary energy supply, and food security indicators. Unique Sugra
    signal - FAOSTAT is not exposed by any competing MCP server.

    Args:
        indicator: Which FAOSTAT domain.
            "production" - crop and livestock output (tonnes, head).
            "trade" - agricultural imports and exports.
            "prices" - retail and producer food prices.
            "food-balance" - per-capita dietary energy, protein, fat supply.
            "food-security" - prevalence of undernourishment, food insecurity metrics.
        country: Optional ISO-3 country code or country name. Examples: "USA",
            "BRA", "IND", "ETH".
        commodity: Optional FAO commodity name or code. Examples: "wheat",
            "maize", "cattle", "coffee".
        year: Optional year (YYYY). Defaults to latest available.

    Examples:
        get_food_indicator(indicator="production", country="BRA", commodity="soybeans")
        get_food_indicator(indicator="food-security", country="ETH")
        get_food_indicator(indicator="prices", commodity="wheat")
    """
    client = get_client()
    return await client.get(
        f"/api/v1/faostat/{indicator}",
        params={"country": country, "commodity": commodity, "year": year},
    )
### END # get_food_indicator ###

### END # sugra_api_mcp/tools/food.py ###
