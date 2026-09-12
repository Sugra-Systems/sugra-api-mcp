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
- Resources (8 by default): sugra://catalog/domains, sugra://catalog/sources, sugra://attribution, and five official skills under sugra://skills/ (explore-catalog, envelope-attribution, auth-limits, hosted-vs-gateway, cross-domain-briefing). Each skill is also a SKILL.md file under sugra_api_mcp/skills/. This git repo is the Claude Code and Grok plugin marketplace for those files (plugin name `sugra-api`). Codex and Cursor copy the same folders. ChatGPT stays MCP connector only.
- Price-chart widget (opt-in, off by default): ui://sugra/price-chart.html, the SEP-1865 template declared on call_endpoint through `_meta.ui`, is served only when the process starts with `SUGRA_MCP_UI_WIDGETS` set to 1, true, yes or on. Off, the template is neither listed in resources/list nor declared on any tool. On, it is a ninth resource and call_endpoint declares it.

## Catalog scale

Two different counters. Do not equate them.

- Live `GET https://sugra.ai/stats` (2026-09-09): 1,641 endpoints, 190 sources, 36 categories. Public copy: "1,500+" / "160+" / 36 domains.
- Bundled MCP catalog (built_at 2026-09-12T07:44:56Z): 1,626 GET/POST operations from live OpenAPI (spec_sha256 a955e49e5162...). Version 0.11.0 is the first cut with this bundle and the official skill pack; the wheel publishes when the owner pushes tag v0.11.0.

## Fixed facts

- Package: sugra-api-mcp on PyPI (MIT), Python 3.11+, current version 0.12.0
- Repository: https://github.com/Sugra-Systems/sugra-api-mcp
- Canonical HTTP URL: `https://mcp.sugra.ai/mcp` (API-key Bearer)
- Permanent alias: `https://app.sugra.ai/mcp` (API-key Bearer or OAuth)
- OAuth resource / JWT audience: `https://app.sugra.ai/mcp` on BOTH hosts until dual-resource OAuth (board MCP-4.3, Parked)
- Protocol: Model Context Protocol, revision 2025-11-25
- MCP Registry name: ai.sugra/api-mcp
- Free tier: 50 requests/day
