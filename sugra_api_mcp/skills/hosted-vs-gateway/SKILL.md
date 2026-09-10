---
name: hosted-vs-gateway
description: Choose hosted Sugra MCP versus the local gateway package, and which tools exist on each. Use when installing sugra-api-mcp, pointing a client at app.sugra.ai/mcp, or a hosted-only tool name fails on stdio.
---

# Hosted agent tools vs gateway

Two transports, one catalog.

| Transport | Tools | How the client authenticates |
|---|---|---|
| Local package stdio | 8 | `SUGRA_API_KEY` in the server process |
| Self-hosted Streamable HTTP | 8 | Client `Authorization: Bearer`; server `SUGRA_API_KEY` is only a downstream fallback |
| Hosted `https://app.sugra.ai/mcp` (canonical `https://mcp.sugra.ai/mcp`) | 11 | Client Bearer API key or OAuth |

The eight gateway tools on every transport: `fetch_data`, `search_endpoints`, `describe_endpoint`, `call_endpoint`, `list_toolsets`, `list_sources`, `sugra_entity_screen`, `sugra_entity_lookup`.

Hosted adds three composed tools that wrap an internal plane and register only on the hosted entry point:

- `resolve_entity` - free text to a canonical market or macro entity; ambiguous matches return ranked candidates, never a silent pick.
- `get_snapshot` - entity plus a named recipe to one current view.
- `get_timeseries` - entity plus a metric to a bounded series.

Do not call those three on a stdio or self-hosted session. Do not document them in stdio-only examples. For LEI/VAT identity and sanctions screening, use `sugra_entity_lookup` and `sugra_entity_screen` on every transport.

MCP prompts shipped in the package name only the eight. Skills `explore-catalog`, `envelope-attribution`, `auth-limits`, and `cross-domain-briefing` are written for the eight so they work as a Claude/Codex drop-in next to the PyPI package.

`app.sugra.ai/mcp` remains a permanent alias of the hosted server.
