---
name: explore-catalog
description: Find and call the right Sugra API endpoint through the bundled catalog. Use when the agent does not already know the operation_id, when a prompt recipe is the wrong shape, or before guessing parameter names.
---

# Explore the Sugra catalog

The Sugra MCP gateway is not a per-endpoint tool list. Discovery is a catalog: search, describe, then call. The catalog is bundled in the package and does not hit the network. Only `call_endpoint` and `fetch_data` (and the entity tools) talk to https://sugra.ai.

## Loop

1. `search_endpoints(query=..., toolset=None, source=None, limit=10)` - natural language. Optional `toolset` and `source` must be names the catalog actually has; an unknown filter returns `error: unknown_toolset` or `unknown_source` with the valid set, not an empty hit list.
2. Pick an `operation_id` from `results`. Do not invent ids.
3. `describe_endpoint(operation_id=...)` - required and optional params, `request_body_schema` on POST, `agent_hints` (`duration_class` fast/slow/heavy, `max_concurrency`, `bulk_cost`).
4. `call_endpoint(operation_id=..., params={...}, body=...)` using those names.

`fetch_data(query=...)` is a one-shot shortcut for simple questions. If it misses, fall back to the four-step loop. `list_toolsets` and `list_sources` (and resources `sugra://catalog/domains`, `sugra://catalog/sources`) are the map, not the query.

## Recipes vs catalog

Six MCP prompts (`market_snapshot`, `macro_briefing`, `sanctions_screening`, `sector_compare`, `earth_conditions`, `source_overview`) are numbered recipes over the same eight gateway tools. They are not the catalog and they do not cover every domain. For anything they do not name, use this loop.

## Do not

- Do not add or request per-endpoint MCP tools.
- Do not skip `describe_endpoint` when the parameter list is unknown.
- Do not treat a catalog miss as "Sugra has no data"; widen the query or drop a bad `toolset`/`source` filter first.
