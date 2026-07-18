# FACTS - canonical public numbers and tool surface

Single source of truth for every public claim about the Sugra MCP. README,
PyPI description, GitHub About, cookbook, examples, and release notes MUST
agree with this file. Refresh the live numbers from https://sugra.ai/stats at
every release and update the snapshot date below.

## Tool surface

| Transport | Tools | Names |
|---|---|---|
| stdio / self-hosted HTTP (`pip install sugra-api-mcp`) | 8 | fetch_data, search_endpoints, describe_endpoint, call_endpoint, list_toolsets, list_sources, sugra_entity_screen, sugra_entity_lookup |
| Hosted (https://app.sugra.ai/mcp) | 11 | the 8 above plus resolve_entity, get_snapshot, get_timeseries |

The three hosted-only tools wrap an internal composed plane and register only
on the hosted deployment. Never claim 6 tools anywhere; that count predates
the entity and agent tools.

## Catalog scale (live snapshot 2026-07-18, sugra.ai/stats)

- Endpoints: 1,569 (public copy: "1,500+")
- Primary sources: 163 (public copy: "160+")
- Data domains: 36

## Fixed facts

- Package: sugra-api-mcp on PyPI (MIT), Python 3.11+
- Repository: https://github.com/Sugra-Systems/sugra-api-mcp
- Hosted endpoint: https://app.sugra.ai/mcp (OAuth or API key)
- Protocol: Model Context Protocol, revision 2025-11-25
- MCP Registry name: ai.sugra/api-mcp
- Free tier: 50 requests/day
