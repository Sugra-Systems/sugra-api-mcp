###########################################
### Sugra API MCP Version 0.3.0         ###
###   DISCOVERY TOOLS Version 0.3.0     ###
###########################################

### BEGIN # sugra_api_mcp/tools/discovery.py ###
"""Discovery tools: search across all 643 endpoints and call any of them directly."""

from __future__ import annotations

import re
from typing import Any

from ..server import get_client, mcp, read_only

_OPENAPI_CACHE: dict[str, Any] | None = None


async def _load_openapi() -> dict[str, Any]:
    global _OPENAPI_CACHE
    if _OPENAPI_CACHE is None:
        client = get_client()
        _OPENAPI_CACHE = await client.get("/openapi.json")
    return _OPENAPI_CACHE


### BEGIN # search_endpoint ###
@mcp.tool(annotations=read_only("Search endpoint"))
async def search_endpoint(query: str, limit: int = 10) -> dict[str, Any]:
    """Search across all 643 Sugra API endpoints by natural-language query.

    Use this when the curated tools (get_market_price, get_macro_indicator, etc.) don't
    cover a specific need. Returns matching endpoints with path, method, summary, and
    parameter hints. Then pass the path to call_endpoint.

    Args:
        query: Free-text description of what you need. Examples: "kalshi orderbook",
            "earthquakes in japan", "bank for international settlements CPI", "defi tvl".
        limit: Maximum matches to return. Default 10.

    Examples:
        search_endpoint(query="earthquake magnitude")
        search_endpoint(query="bis effective exchange rate")
    """
    spec = await _load_openapi()
    if not isinstance(spec, dict) or "paths" not in spec:
        return {"error": "Could not load OpenAPI spec"}

    terms = [t.lower() for t in re.findall(r"\w+", query) if len(t) >= 2]
    if not terms:
        return {"results": []}

    matches: list[tuple[int, dict[str, Any]]] = []
    for path, methods in spec["paths"].items():
        if not isinstance(methods, dict):
            continue
        for method, op in methods.items():
            if method not in {"get", "post"}:
                continue
            if not isinstance(op, dict):
                continue
            haystack = " ".join([
                path.lower(),
                str(op.get("summary", "")).lower(),
                str(op.get("description", "")).lower(),
                " ".join(op.get("tags", [])).lower(),
            ])
            score = sum(haystack.count(t) for t in terms)
            if score > 0:
                matches.append((score, {
                    "path": path,
                    "method": method.upper(),
                    "summary": op.get("summary", ""),
                    "tags": op.get("tags", []),
                    "parameters": [
                        {
                            "name": p.get("name"),
                            "in": p.get("in"),
                            "required": p.get("required", False),
                            "description": p.get("description", ""),
                        }
                        for p in op.get("parameters", [])
                        if isinstance(p, dict)
                    ],
                }))

    matches.sort(key=lambda x: x[0], reverse=True)
    return {"results": [m[1] for m in matches[:limit]], "total_matched": len(matches)}
### END # search_endpoint ###


### BEGIN # call_endpoint ###
@mcp.tool(annotations=read_only("Call endpoint"))
async def call_endpoint(
    path: str,
    method: str = "GET",
    path_params: dict[str, str] | None = None,
    query_params: dict[str, Any] | None = None,
    body: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Call any Sugra API endpoint directly. Use in combination with search_endpoint.

    Args:
        path: Endpoint path template from search_endpoint results (e.g. "/api/v1/bis/{flow}/data").
            Use {name} placeholders for path parameters.
        method: HTTP method. "GET" or "POST". Default "GET".
        path_params: Dict substituting {name} placeholders in path.
        query_params: Query string parameters.
        body: Request body for POST requests.

    Examples:
        call_endpoint(path="/api/v1/earthquakes/significant", query_params={"min_magnitude": 5.0})
        call_endpoint(path="/api/v1/bis/{flow}/data", path_params={"flow": "EER_D"})
    """
    client = get_client()
    resolved = path
    for name, value in (path_params or {}).items():
        resolved = resolved.replace(f"{{{name}}}", str(value))

    if "{" in resolved:
        return {"error": f"Unresolved path parameters in: {resolved}"}

    upper = method.upper()
    if upper == "GET":
        return await client.get(resolved, params=query_params)
    if upper == "POST":
        return await client.post(resolved, json=body)
    return {"error": f"Unsupported method: {method}"}
### END # call_endpoint ###

### END # sugra_api_mcp/tools/discovery.py ###
