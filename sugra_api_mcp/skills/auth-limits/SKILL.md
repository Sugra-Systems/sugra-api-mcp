---
name: auth-limits
description: Authenticate to Sugra API MCP and stay inside the daily request quota. Use when a tool returns 401, 429, missing_api_key, or when setting up a client.
---

# Auth and rate limits

## Credentials

- stdio: set `SUGRA_API_KEY` (`sugra_...` from https://app.sugra.ai/settings/billing) in the server process. Catalog tools (`search_endpoints`, `describe_endpoint`, `list_toolsets`, `list_sources`) work without it. `call_endpoint`, `fetch_data`, and the entity tools return a structured `missing_api_key` error until it is set.
- Streamable HTTP (hosted `https://app.sugra.ai/mcp` or self-hosted): the MCP client sends `Authorization: Bearer`. That is a raw API key or an OAuth JWT (audience `https://app.sugra.ai/mcp`, scope `sugra:read`). Discovery (`initialize`, `tools/list`, `resources/list`, `prompts/list`, `ping`) is public. `tools/call` and `resources/read` return 401 `missing_bearer_token` without Bearer. `SUGRA_API_KEY` on the HTTP process is only a downstream fallback, not a substitute for the client's Bearer.
- Downstream API calls use `x-api-key`. Do not log the key or put it in a skill file, commit, or chat.

## Quota

Every plan sees every endpoint. Gating is volume, not surface.

| Plan | Requests / day |
|---|---|
| Free | 50 |
| Dev | 5,000 |
| Pro | 50,000 |

Some bulk endpoints cost more than 1 request; `describe_endpoint` `agent_hints.bulk_cost` warns before the call.

On 401, the credential is missing or invalid - do not retry the same call. On 429 from an MCP tool, wait for the JSON field `retry_after` (seconds). Direct HTTP to https://sugra.ai also sends `X-RateLimit-Limit`, `X-RateLimit-Remaining`, `X-RateLimit-Reset`, and `X-RateLimit-Cost`; the MCP tool envelope does not forward those headers. `elapsed_ms` on a tool error says which timeout fired.

Get a key at https://app.sugra.ai/settings/billing.
