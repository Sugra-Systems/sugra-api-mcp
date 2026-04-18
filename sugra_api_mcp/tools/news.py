###########################################
### Sugra API MCP Version 0.3.0         ###
###   NEWS TOOLS Version 0.3.0          ###
###########################################

### BEGIN # sugra_api_mcp/tools/news.py ###
"""News tools: search, latest, by region or category."""

from __future__ import annotations

from typing import Any

from ..server import get_client, mcp, read_only


### BEGIN # get_news ###
@mcp.tool(annotations=read_only("News"))
async def get_news(
    query: str | None = None,
    region: str | None = None,
    category: str | None = None,
    limit: int = 20,
) -> dict[str, Any]:
    """Get news articles from aggregated sources (GDELT, financial newswires, press releases).

    If `query` is provided, performs a full-text search. Otherwise returns latest news,
    optionally scoped by region or category.

    Args:
        query: Optional search text. Examples: "federal reserve", "tesla earnings".
        region: Optional region slug (e.g. "us", "eu", "asia").
        category: Optional category (e.g. "business", "technology", "politics").
        limit: Max articles to return. Default 20.

    Examples:
        get_news(query="inflation", limit=10)
        get_news(region="us")
        get_news(category="business")
    """
    client = get_client()
    if query:
        return await client.get("/api/v1/news/search", params={"q": query, "limit": limit})
    if region:
        return await client.get(f"/api/v1/news/region/{region}", params={"limit": limit})
    if category:
        return await client.get(f"/api/v1/news/category/{category}", params={"limit": limit})
    return await client.get("/api/v1/news/latest", params={"limit": limit})
### END # get_news ###

### END # sugra_api_mcp/tools/news.py ###
