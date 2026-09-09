# FACTS - canonical public numbers and tool surface

Single source of truth for every public claim about the Sugra MCP. README,
PyPI description, GitHub About, cookbook, examples, and release notes MUST
agree with this file. Refresh the live numbers from https://sugra.ai/stats at
every release and update the snapshot date below.

## Tool surface

| Transport | Tools | Names |
|---|---|---|
| stdio / self-hosted HTTP (`pip install sugra-api-mcp`) | 8 | fetch_data, search_endpoints, describe_endpoint, call_endpoint, list_toolsets, list_sources, sugra_entity_screen, sugra_entity_lookup |
| Hosted (`https://mcp.sugra.ai/mcp` and `https://app.sugra.ai/mcp`) | 11 | the 8 above plus resolve_entity, get_snapshot, get_timeseries |

The three hosted-only tools wrap an internal composed plane and register only
on the hosted deployment. Never claim 6 tools or 27 tools anywhere. 27 was the
pre-gateway curated surface and never shipped in this package after v0.4.0.

## Prompts and resources (every transport)

- Prompts (6): market_snapshot, macro_briefing, sanctions_screening, sector_compare, earth_conditions, source_overview. Prompt text names only the 8 gateway tools, never hosted-only agent tools.
- Resources (4): sugra://catalog/domains, sugra://catalog/sources, sugra://attribution, ui://sugra/price-chart.html (SEP-1865 widget on call_endpoint).

## Catalog scale

Two different counters. Do not equate them.

- Live `GET https://sugra.ai/stats` (2026-09-09): 1,641 endpoints, 190 sources, 36 categories. Public copy: "1,500+" / "160+" / 36 domains.
- Bundled MCP catalog on git main (built_at 2026-09-04T13:11:41Z): 1,574 GET/POST operations from live OpenAPI. PyPI 0.10.0 (uploaded 2026-08-20) still carries the pre-2026-09-04 bundle.

## Fixed facts

- Package: sugra-api-mcp on PyPI (MIT), Python 3.11+, current version 0.10.0
- Repository: https://github.com/Sugra-Systems/sugra-api-mcp
- Canonical HTTP URL: `https://mcp.sugra.ai/mcp` (API-key Bearer)
- Permanent alias: `https://app.sugra.ai/mcp` (API-key Bearer or OAuth)
- OAuth resource / JWT audience: `https://app.sugra.ai/mcp` on BOTH hosts until dual-resource OAuth (board MCP-4.3, Parked)
- Protocol: Model Context Protocol, revision 2025-11-25
- MCP Registry name: ai.sugra/api-mcp
- Free tier: 50 requests/day
