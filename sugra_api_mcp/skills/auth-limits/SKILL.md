---
name: auth-limits
description: Authenticate to Sugra API MCP and stay inside the daily request quota. Use when a tool returns 401, 429, missing_api_key, or when setting up a client.
---

# Auth and rate limits

## Credentials

- Local stdio / self-hosted: `SUGRA_API_KEY` (key `sugra_...` from https://app.sugra.ai/settings/billing).
- Hosted `https://app.sugra.ai/mcp`: `Authorization: Bearer` with the same API key, or OAuth through the client connector UI. Audience is `https://app.sugra.ai/mcp`; token needs `sugra:read`.
- Downstream API calls use `x-api-key`. Do not log the key or put it in a skill file, commit, or chat.

The process starts without a key. Catalog tools (`search_endpoints`, `describe_endpoint`, `list_toolsets`, `list_sources`) work keyless. `call_endpoint`, `fetch_data`, and the entity tools return a structured `missing_api_key` error until a key is present.

## Quota

Every plan sees every endpoint. Gating is volume, not surface.

| Plan | Requests / day |
|---|---|
| Free | 50 |
| Dev | 5,000 |
| Pro | 50,000 |

Read `X-RateLimit-Limit`, `X-RateLimit-Remaining`, `X-RateLimit-Reset`, and `X-RateLimit-Cost` on API responses. Some bulk endpoints cost more than 1; `describe_endpoint` `agent_hints.bulk_cost` warns before the call.

On 401, the key is missing or invalid - do not retry the same call. On 429, wait for Reset (or `Retry-After`) rather than spinning. `elapsed_ms` on a tool error says which timeout fired.

Get a key at https://app.sugra.ai/settings/billing.
