###########################################
### Sugra API MCP Version 0.3.0         ###
###   MARKETS TOOLS Version 0.3.0       ###
###########################################

### BEGIN # sugra_api_mcp/tools/markets.py ###
"""Market data tools: prices, history, overview, symbol lookup, forex, commodities, predictions."""

from __future__ import annotations

from typing import Any, Literal

from ..server import get_client, mcp, read_only

AssetType = Literal["stock", "crypto"]

Commodity = Literal[
    "gold", "silver", "platinum", "palladium", "copper",
    "oil", "brent", "natgas",
    "wheat", "corn", "soybean", "coffee", "cotton", "sugar",
]


### BEGIN # get_market_price ###
@mcp.tool(annotations=read_only("Current market price"))
async def get_market_price(
    symbol: str,
    asset_type: AssetType = "stock",
    vs_currency: str = "usd",
) -> dict[str, Any]:
    """Get the current market price for a stock or cryptocurrency.

    Returns the latest price in a normalized envelope: `{data, meta: {source, data_time, ...}}`.

    For forex use `get_forex_rate`. For commodities use `get_commodity_price`.

    Args:
        symbol: Stock ticker (uppercase, e.g. "AAPL") or CoinGecko crypto slug
            (lowercase, e.g. "bitcoin", "ethereum", "solana").
        asset_type: "stock" (default) for equities, "crypto" for cryptocurrencies.
        vs_currency: Quote currency for crypto. ISO 4217 lowercase (default "usd").
            Ignored for stocks.

    Examples:
        get_market_price(symbol="AAPL")
        get_market_price(symbol="bitcoin", asset_type="crypto")
        get_market_price(symbol="ethereum", asset_type="crypto", vs_currency="eur")
    """
    client = get_client()
    if asset_type == "crypto":
        return await client.get(
            f"/api/v1/crypto/{symbol.lower()}/price",
            params={"vs_currency": vs_currency.lower()},
        )
    return await client.get(f"/api/v2/quotes/{symbol.upper()}/price")
### END # get_market_price ###


### BEGIN # get_historical_prices ###
@mcp.tool(annotations=read_only("Historical prices"))
async def get_historical_prices(
    symbol: str,
    asset_type: AssetType = "stock",
    period: str = "1mo",
    interval: str = "1d",
) -> dict[str, Any]:
    """Get historical OHLCV time series for a stock or cryptocurrency.

    Returns a list of price bars with open, high, low, close, and volume.

    Args:
        symbol: Stock ticker or crypto slug (see get_market_price).
        asset_type: "stock" (default) or "crypto".
        period: Lookback range. Common: "1d", "5d", "1mo", "3mo", "6mo", "1y", "2y", "5y", "max".
        interval: Bar size (stocks only). Common: "1m", "5m", "15m", "1h", "1d", "1wk", "1mo".

    Examples:
        get_historical_prices(symbol="AAPL", period="1y")
        get_historical_prices(symbol="bitcoin", asset_type="crypto", period="3mo")
    """
    client = get_client()
    if asset_type == "crypto":
        return await client.get(
            f"/api/v1/crypto/{symbol.lower()}/history",
            params={"days": _period_to_days(period)},
        )
    return await client.get(
        f"/api/v2/quotes/{symbol.upper()}/historical",
        params={"range": period, "interval": interval},
    )
### END # get_historical_prices ###


### BEGIN # search_symbol ###
@mcp.tool(annotations=read_only("Search symbol"))
async def search_symbol(
    query: str,
    asset_type: Literal["stock", "forex"] | None = None,
) -> dict[str, Any]:
    """Find a stock ticker or currency pair by company name, description, or free-text query.

    Returns candidate symbols with name, exchange, and asset type.

    For crypto, pass the CoinGecko slug directly to `get_market_price` (e.g.
    "bitcoin", "ethereum", "cardano") - the crypto catalogue uses human-readable
    slugs that LLMs know.

    Args:
        query: Search string. Examples: "apple", "tesla motors", "euro dollar".
        asset_type: Optional "stock" or "forex" filter. Omit for stocks (default).

    Examples:
        search_symbol(query="apple")
        search_symbol(query="euro to dollar", asset_type="forex")
    """
    client = get_client()
    if asset_type == "forex":
        return await client.get("/api/v1/forex/currencies", params={"q": query})
    return await client.get("/api/v2/market/search", params={"q": query})
### END # search_symbol ###


### BEGIN # get_market_overview ###
@mcp.tool(annotations=read_only("Market overview"))
async def get_market_overview(asset_type: AssetType = "crypto") -> dict[str, Any]:
    """Get a market snapshot: top movers, total market cap, sector performance, dominance.

    Args:
        asset_type: "crypto" for global crypto markets (total cap, BTC dominance),
            "stock" for US equity overview (movers, indices).

    Examples:
        get_market_overview(asset_type="crypto")
        get_market_overview(asset_type="stock")
    """
    client = get_client()
    if asset_type == "crypto":
        return await client.get("/api/v1/crypto/global")
    return await client.get("/api/v2/market/summary")
### END # get_market_overview ###


### BEGIN # get_forex_rate ###
@mcp.tool(annotations=read_only("Forex rate"))
async def get_forex_rate(
    base: str,
    quote: str,
    amount: float = 1.0,
) -> dict[str, Any]:
    """Get the current exchange rate between two currencies.

    Returns the rate from `base` to `quote` plus the converted amount.

    Args:
        base: 3-letter source currency code (ISO 4217). Example: "USD".
        quote: 3-letter target currency code. Example: "EUR".
        amount: Amount to convert. Default 1 (returns the rate directly).

    Examples:
        get_forex_rate(base="USD", quote="EUR")
        get_forex_rate(base="GBP", quote="JPY", amount=1000)
    """
    client = get_client()
    return await client.get(
        "/api/v1/forex/convert",
        params={"from": base.upper(), "to": quote.upper(), "amount": amount},
    )
### END # get_forex_rate ###


### BEGIN # get_commodity_price ###
@mcp.tool(annotations=read_only("Commodity price"))
async def get_commodity_price(
    commodity: Commodity,
    vs_currency: str = "usd",
) -> dict[str, Any]:
    """Get the spot price for a major commodity.

    Covers precious metals (gold, silver, platinum, palladium, copper), energy
    (oil, brent, natgas), and agricultural (wheat, corn, soybean, coffee,
    cotton, sugar).

    Args:
        commodity: Commodity slug. Must be one of the supported values.
        vs_currency: Quote currency (ISO 4217 lowercase). Default "usd".

    Examples:
        get_commodity_price(commodity="gold")
        get_commodity_price(commodity="oil", vs_currency="eur")
        get_commodity_price(commodity="wheat")
    """
    client = get_client()
    return await client.get(
        f"/api/v1/commodities/{commodity}",
        params={"vs_currency": vs_currency.lower()},
    )
### END # get_commodity_price ###


### BEGIN # get_prediction_market ###
@mcp.tool(annotations=read_only("Prediction market"))
async def get_prediction_market(
    query: str | None = None,
    event_ticker: str | None = None,
    limit: int = 20,
) -> dict[str, Any]:
    """Get prediction market contracts from Kalshi (CFTC-regulated exchange).

    Returns event-level markets with yes/no prices, volume, and close dates.

    Args:
        query: Optional free-text filter to narrow events (e.g. "election", "inflation").
        event_ticker: Optional specific event ticker for detailed view.
        limit: Maximum number of events to return. Default 20.

    Examples:
        get_prediction_market(query="fed rate")
        get_prediction_market(event_ticker="PRES-2028")
    """
    client = get_client()
    if event_ticker:
        return await client.get(f"/api/v1/kalshi/events/{event_ticker}")
    return await client.get("/api/v1/kalshi/events", params={"q": query, "limit": limit})
### END # get_prediction_market ###


def _period_to_days(period: str) -> int:
    mapping = {
        "1d": 1, "5d": 5, "1w": 7,
        "1mo": 30, "3mo": 90, "6mo": 180,
        "1y": 365, "2y": 730, "5y": 1825, "max": 3650,
    }
    return mapping.get(period, 30)

### END # sugra_api_mcp/tools/markets.py ###
