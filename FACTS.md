# FACTS - canonical public numbers and tool surface

Single source of truth for every public claim about the Sugra MCP. README,
PyPI description, GitHub About, cookbook, examples, and release notes MUST
agree with this file. Refresh the live numbers from https://sugra.ai/stats at
every release and update the snapshot date below.

## Tool surface

| Transport | Tools | Names |
|---|---|---|
| stdio / self-hosted HTTP (`pip install sugra-api-mcp`) | 10 | fetch_data, search_endpoints, describe_endpoint, call_endpoint, list_toolsets, list_sources, sugra_entity_screen, sugra_entity_lookup, list_plans, buy_plan |
| Hosted (`https://mcp.sugra.ai/mcp` and `https://app.sugra.ai/mcp`) | 13 | the 10 above plus resolve_entity, get_snapshot, get_timeseries |

`list_plans` returns the paid plans (Dev and Pro, monthly or annual) with
their prices, daily request limits and a checkout link each. It is bundled
data: no network call and no API key.

`buy_plan` buys a Dev or Pro plan for a new account and returns its API key.
The agent pays through the Payment HTTP authentication scheme (HTTP 402) with
Stripe: the first call answers JSON-RPC error -32042 with the challenge, the
second carries the credential in `_meta` and gets the key and a receipt. It
needs no API key, on the hosted server too. Purchases do not auto-renew.

The three hosted-only tools wrap an internal composed plane and register only
on the hosted deployment. Never claim 6 tools or 27 tools anywhere. 27 was the
pre-gateway curated surface and never shipped in this package after v0.4.0.

## Prompts and resources (every transport)

- Prompts (6): market_snapshot, macro_briefing, sanctions_screening, sector_compare, earth_conditions, source_overview. Prompt text names only the 8 gateway tools other than list_plans and buy_plan, never hosted-only agent tools.
- Resources (8 by default): sugra://catalog/domains, sugra://catalog/sources, sugra://attribution, and five official skills under sugra://skills/ (explore-catalog, envelope-attribution, auth-limits, hosted-vs-gateway, cross-domain-briefing). Each skill is also a SKILL.md file under sugra_api_mcp/skills/. This git repo is the Claude Code and Grok plugin marketplace for those files (plugin name `sugra-api`). Codex and Cursor copy the same folders. ChatGPT stays MCP connector only.
- Price-chart widget (opt-in, off by default): ui://sugra/price-chart.html, the SEP-1865 template declared on call_endpoint through `_meta.ui`, is served only when the process starts with `SUGRA_MCP_UI_WIDGETS` set to 1, true, yes or on. Off, the template is neither listed in resources/list nor declared on any tool. On, it is a ninth resource and call_endpoint declares it.

## Catalog scale

Two different counters. Do not equate them.

- Live `GET https://sugra.ai/stats` (2026-09-24): 1,670 endpoints, 195 sources, 36 categories. Public copy: "1,600+" / "160+" / 36 domains.
- Bundled MCP catalog (built_at 2026-09-13T22:08:58Z): 1,626 GET/POST operations from live OpenAPI (spec_sha256 e5fc92479214...). Version 0.11.0 is the first cut with this bundle and the official skill pack; the wheel publishes when the owner pushes tag v0.11.0.

## Fixed facts

- Package: sugra-api-mcp on PyPI (MIT), Python 3.11+, current version 0.12.0
- Repository: https://github.com/Sugra-Systems/sugra-api-mcp
- Canonical HTTP URL: `https://mcp.sugra.ai/mcp` (API-key Bearer or OAuth)
- Permanent alias: `https://app.sugra.ai/mcp` (API-key Bearer or OAuth)
- OAuth resource / JWT audience: `https://app.sugra.ai/mcp` on BOTH hosts until dual-resource OAuth lands
- Protocol: Model Context Protocol, revision 2025-11-25
- MCP Registry name: ai.sugra/api-mcp
- Free tier: 50 requests/day

## Directory listings

- Anthropic's Connectors Directory: "Sugra API", https://claude.ai/directory/sugra-api (short link https://url.sugra.ai/claude), added September 2026. The directory serves Claude on the web, desktop and mobile, Claude Code and Cowork. It connects to the canonical HTTP URL above, with OAuth sign-in. It is a Community listing: never describe it as verified, approved, endorsed or certified by Anthropic, or as a partnership.
- Official OpenAI Plugins Directory: "Sugra API", https://url.sugra.ai/openai, available in ChatGPT and Codex since July 2026.
- Install buttons on every surface read "Add to Claude" (https://url.sugra.ai/claude) and "Add to ChatGPT" (https://url.sugra.ai/openai).
- Prose links to the hosted server point at the landing page https://mcp.sugra.ai. The connector URL (the canonical HTTP URL above) appears only where a person or a client pastes it: config snippets, a code block for adding a custom connector, `server.json`. New text never names the permanent alias. Links to the directories read "Add to Claude" and "Add to ChatGPT" and use the two short links.
