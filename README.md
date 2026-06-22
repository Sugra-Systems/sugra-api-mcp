# sugra-api-mcp

<!-- mcp-name: ai.sugra/api-mcp -->

<p align="center">
  <img src="https://app.sugra.ai/images/brand/sugra-app-icon.svg" alt="sugra.ai" width="112" height="112" />
</p>

<p align="center">
  <a href="https://pypi.org/project/sugra-api-mcp/">PyPI v0.8.2</a> |
  Python 3.11+ |
  <a href="https://github.com/Sugra-Systems/prod-sugra-ai-MCP/blob/main/LICENSE">MIT</a>
</p>

**Gateway connector between LLM agents and world data.** Official [Model Context Protocol](https://modelcontextprotocol.io) server for the [Sugra API](https://sugra.ai), backed by a bundled endpoint catalog and operation_id calls.

Works with Anthropic Claude, OpenAI GPT, Google Gemini, xAI, and any MCP-enabled IDE.

Client details:

- **Anthropic Claude**: Claude Desktop, Claude Code (CLI), claude.ai (web)
- **OpenAI GPT**: ChatGPT (via MCP connector)
- **Google Gemini**: Gemini CLI, Gemini Code Assist (VS Code + JetBrains)
- **xAI**: Remote MCP Tools in xAI SDK and Responses API
- **IDEs**: VS Code (native), Cursor, Zed, Cline, Continue.dev, Windsurf
- **Custom agents**: anything built on the Python or TypeScript MCP SDK

## What you get

Current release: eight-tool surface with hosted OAuth activity validation for `https://app.sugra.ai/mcp`, plus ChatGPT Apps-compatible OAuth tool metadata. Curated tool names such as `get_market_price`, `get_macro_indicator`, and `get_news` are not part of this package. The package exposes exactly eight tools:

| Tool | Purpose |
|---|---|
| `fetch_data` | One-step: find best endpoint for a natural-language query and call it. Combines search + call in one round trip. |
| `search_endpoints` | Search the bundled endpoint catalog. Runtime search does not fetch `/openapi.json`. |
| `describe_endpoint` | Inspect an endpoint by `operation_id`, including path, method, parameters, required inputs, `agent_hints`, and `request_body_schema` for JSON-body POST operations. |
| `call_endpoint` | Call a Sugra API operation by `operation_id`. Arbitrary path calls are no longer supported. |
| `list_toolsets` | List catalog groups with endpoint counts and descriptions. |
| `list_sources` | Show bundled catalog source metadata. |
| `sugra_entity_screen` | Screen a name against sanctions and watchlists (Sugra Entity). |
| `sugra_entity_lookup` | Composed entity lookup by identifier - `anchor` is `lei` or `vat`, plus the identifier `value`; returns registry identity + screening (Sugra Entity). |

`call_endpoint` and `fetch_data` both support response shaping with `limit`, `fields`, and `include_raw`. Shaping works on enveloped (`{"data": ...}`) and envelope-less payloads alike; `fields` entries may use dotted paths into nested objects (`geo.city`), and `meta.shaped` reports what was actually applied (`fields_applied` / `fields_unmatched`, `limit_applied`) rather than echoing the request.

`describe_endpoint` returns computed `agent_hints` per endpoint so agents can budget time and parallelism before calling:

- `duration_class` - `fast` (under ~2s, snapshot-backed), `slow` (live upstream proxying, occasionally 15s+), or `heavy` (per-item upstream work, large batches can exceed the gateway timeout)
- `max_concurrency` - advisory ceiling for parallel calls from one session
- `bulk_cost` - on per-item bulk endpoints: 1 request credit per item in the request body (the API reports the total in the `X-RateLimit-Cost` response header)

## Hosted-only agent tools (app.sugra.ai/mcp)

The hosted MCP endpoint at `https://app.sugra.ai/mcp` serves the same eight tools PLUS three composed agent tools that are not available on stdio or self-hosted installs:

| Tool | Purpose |
|---|---|
| `resolve_entity` | Free text (ticker, company, indicator, coin, currency pair) to a canonical market or macro entity. Ambiguous matches return ranked candidates, never a silent pick. |
| `get_snapshot` | Entity plus a named recipe to one composed current view with freshness, provenance, coverage, and billing blocks. Composed calls charge a fixed recipe cost (1-2 requests) from the daily quota. |
| `get_timeseries` | Entity plus metric (`price`, `macro_series`, `etf_flows`) to a bounded series with an explicit downsampling flag. |

These three tools wrap an internal composed plane that requires an infrastructure credential available only on the hosted deployment. The tool code ships inside the package, but it is registered only by the hosted HTTP entry point and only when that credential is present - `pip install sugra-api-mcp` (stdio and self-hosted HTTP) always exposes the classic eight-tool gateway. Hosted-only examples in any documentation are labeled as such. For compliance entity lookups (LEI / VAT, sanctions screening) use `sugra_entity_lookup` and `sugra_entity_screen`, which work on every transport.

## Installation

```bash
pip install sugra-api-mcp
```

Get a free API key at [app.sugra.ai/settings/billing](https://app.sugra.ai/settings/billing) (Free tier: 50 req/day).

## Usage with Claude Desktop (stdio)

Add to `claude_desktop_config.json`:

- macOS: `~/Library/Application Support/Claude/claude_desktop_config.json`
- Windows: `%APPDATA%\Claude\claude_desktop_config.json`
- Linux: Claude Desktop has no Linux build. On Linux, `pip install sugra-api-mcp` and use Claude Code (CLI), an IDE client, or the hosted HTTP endpoint below.

```json
{
  "mcpServers": {
    "sugra": {
      "command": "sugra-api-mcp",
      "env": {
        "SUGRA_API_KEY": "sugra_xxx_yourkey..."
      }
    }
  }
}
```

Restart Claude Desktop. Sugra tools appear in the tools menu.

## Usage with Claude Code (Anthropic CLI)

```bash
claude mcp add sugra -- sugra-api-mcp
# then set the env var that sugra-api-mcp reads
export SUGRA_API_KEY=sugra_xxx_...
```

Or edit `~/.claude/config.json` manually with the same shape as Claude Desktop above.

## Usage with Cursor, Zed, Cline, Continue.dev, Windsurf

Each of these has an MCP settings file (typically `mcp.json` or equivalent) with the same stdio config shape as Claude Desktop.

## Usage with ChatGPT

ChatGPT supports MCP through its connector UI. Use the hosted HTTP endpoint (below) since ChatGPT does not launch local stdio processes.

## Usage over HTTP (claude.ai, ChatGPT, remote agents)

Hosted Streamable HTTP endpoint:

```
https://app.sugra.ai/mcp
```

Add to claude.ai, ChatGPT, or any Streamable HTTP MCP client. Authenticate with `Authorization: Bearer sugra_xxx_...`.

In claude.ai: Settings -> Connectors -> Add custom connector.
In ChatGPT: Settings -> Connectors -> Add MCP server.

## CLI

Server startup is unchanged:

```bash
sugra-api-mcp
sugra-api-mcp --transport streamable-http --port 8001
```

Catalog and gateway helpers:

```bash
sugra-api-mcp doctor
sugra-api-mcp list-toolsets
sugra-api-mcp search "NASDAQ futures"
sugra-api-mcp describe cot_financial
sugra-api-mcp call quotes_symbol_price --params '{"symbol":"AAPL"}'
```

## Environment variables

| Variable | Required | Default | Description |
|---|---|---|---|
| `SUGRA_API_KEY` | Yes (stdio) | - | Your Sugra API key. In HTTP mode with OAuth this becomes a fallback for requests without Bearer |
| `SUGRA_API_BASE` | No | `https://sugra.ai` | Override for self-hosted or beta environments |
| `SUGRA_TIMEOUT` | No | `30` | Request timeout in seconds |
| `SUGRA_MCP_ALLOWED_HOSTS` | No (HTTP) | - | Comma-separated hostnames to allow behind a reverse proxy |
| `SUGRA_MCP_ALLOWED_ORIGINS` | No (HTTP) | chatgpt.com, claude.ai, cursor.sh + others | Comma-separated allowed Origins for browser-based MCP clients. Applies to BOTH the outer Starlette CORS layer and the inner FastMCP DNS rebinding Origin check, so the two stay in sync. `*` disables the inner Origin check entirely (self-hosted / dev only); Bearer auth still gates tool calls |

### HTTP transport with OAuth

When running with `--transport streamable-http` the server allows unauthenticated MCP discovery requests (`initialize`, `notifications/initialized`, `tools/list`, `resources/list`, `prompts/list`, and `ping`) so ChatGPT Apps and other mixed-auth clients can discover tool metadata. CORS is enabled for major MCP clients (ChatGPT, Claude, Cursor) so browser connector UIs can complete the OAuth flow; override the allowlist with `SUGRA_MCP_ALLOWED_ORIGINS`. Tool calls still require `Authorization: Bearer ...`. Two token formats are accepted:

- Raw API key (`sugra_...`) - passed through as the downstream `x-api-key`. Compatible with earlier local API-key setups.
- OAuth JWT - signature verified against the issuer's JWKS. The audience must match `https://app.sugra.ai/mcp`, the token must include `sugra:read`, and hosted access is validated against APP before resolving the user's primary API key. Successful hosted OAuth requests update MCP connection activity in APP.

| Variable | Required | Default | Description |
|---|---|---|---|
| `SUGRA_APP_URL` | HTTP + OAuth | `https://app.sugra.ai` | Base URL of the authorization server |
| `SUGRA_JWKS_URL` | No | `$SUGRA_APP_URL/oauth/jwks.json` | JWKS endpoint |
| `INTERNAL_API_TOKEN` | HTTP + OAuth | - | Shared secret for the user lookup and MCP activity endpoints on the authorization server. Same value must be set on both the MCP process and the app.sugra.ai Laravel process |

## Timeouts and the error contract

`SUGRA_TIMEOUT` caps each downstream HTTP call from this server to the Sugra API (default 30 seconds). It is one link in a longer chain; when a tool call fails, `elapsed_ms` in the error payload tells you which link cut it:

```
MCP client (agent harness)         own tool timeout, often 60-180s, client-controlled
  -> hosted proxy (app.sugra.ai)   86400s, effectively unlimited
    -> this server (httpx)         SUGRA_TIMEOUT, default 30s
      -> Sugra API -> upstreams    15-60s per upstream call, server-side
```

Tool failures return structured JSON instead of raising, so agents can pick a retry strategy:

| `error` value | Meaning | Retry strategy |
|---|---|---|
| `upstream_timeout` | No response within `SUGRA_TIMEOUT` (`elapsed_ms` close to `timeout_s` x 1000) | Retry once: the aborted attempt usually completes server-side and warms upstream caches. Then narrow the request (smaller batch, tighter filters). |
| `upstream_connect_error` | Could not reach the Sugra API (DNS failure, connection refused) | Retry after a short delay. |
| `upstream_transport_error` | Connection dropped mid-request | Retry once. |
| free-text string + `status_code` | The API answered with HTTP 4xx/5xx; `retry_after` included when the API sent a Retry-After header | Honor `retry_after` for 429/503; fix the request for 4xx. |
| `tool_execution_failed` | Unexpected failure inside the gateway (`exception_type` included) | Report if persistent. |

All error payloads carry `elapsed_ms`. `url` is present on transport and HTTP errors (not on `tool_execution_failed`, which can fire before a URL exists). On the three transport errors `status_code` is `null` (no HTTP status was received) - consumers comparing `status_code` numerically should guard for that. If a tool call instead fails with a bare client-side message and no structured JSON, the timeout fired in your agent harness above this server: raise the client's tool timeout, not `SUGRA_TIMEOUT`.

## Examples

Ask Claude:

- "Search Sugra endpoints for NASDAQ futures."
- "Describe the `cot_financial` operation."
- "Call `quotes_symbol_price` with symbol AAPL and return only symbol and price."
- "List available Sugra toolsets."

## Troubleshooting

**`SUGRA_API_KEY environment variable is required`**

The server could not find your API key. Depending on how you run it:
- As an MCP tool from your client (Claude, ChatGPT, Gemini, xAI, IDE, etc.): check the `env` block in your MCP config file. Value should be a full key like `sugra_ao1_...`, not empty and not wrapped in extra quotes.
- Shell / CI: `export SUGRA_API_KEY=sugra_...` before running `sugra-api-mcp`.
- HTTP mode: set via `.env` or systemd `EnvironmentFile`, not the shell.

**`401 Unauthorized` or `403 Forbidden` in tool responses**

Key accepted but rejected. Common causes:
- Key was regenerated in [app.sugra.ai/settings/billing](https://app.sugra.ai/settings/billing) and your config still has the old one.
- Typo - key contains only lowercase letters and digits, no spaces, no trailing newlines.
- Free tier was deactivated. Sign in to verify status.

**`429 Too Many Requests`**

Hit your plan's daily limit. Response headers include `X-RateLimit-Reset` with the UTC timestamp when the counter resets (midnight UTC). Upgrade your plan at [app.sugra.ai/settings/billing](https://app.sugra.ai/settings/billing).

**`Invalid Host header`** (only if self-hosting HTTP mode)

FastMCP has DNS rebinding protection. Set `SUGRA_MCP_ALLOWED_HOSTS` to a comma-separated list of the public hostnames your reverse proxy serves. Example: `SUGRA_MCP_ALLOWED_HOSTS=mcp.example.com,example.com`.

**Tool result truncated with `meta.truncated` notice**

Some endpoints return very large payloads (global wildfires, full table catalogs). The client enforces the MCP 25k token limit - when hit, the data list is trimmed and a retry hint appears in `meta.truncated.retry_hint`. Add narrower filters (country, date range, `limit`) to get the full result.

**`Python version 3.11 or higher is required`**

sugra-api-mcp requires Python 3.11+. Check: `python --version`. If you have 3.10 or older:
- Ubuntu: install Python 3.11 or newer from your distribution packages or the deadsnakes PPA.
- macOS: `brew install python@3.11`
- Windows: download from [python.org](https://www.python.org/downloads/)

Then recreate your venv.

**Hosted `app.sugra.ai/mcp` returns 5xx**

The hosted endpoint can briefly restart after deploys. Wait 60 seconds and retry. If persistent, email support@sugra.systems.

**Debugging tool calls locally**

Run with stdio and log JSON-RPC messages:
```bash
SUGRA_API_KEY=sugra_... sugra-api-mcp 2>&1 | tee mcp-debug.log
```
Send manual JSON-RPC from a second terminal using `nc` or an MCP inspector.

## Development

```bash
git clone https://github.com/Sugra-Systems/prod-sugra-ai-MCP
cd prod-sugra-ai-MCP
pip install -e ".[dev,http]"
export SUGRA_API_KEY=sugra_...
python -m sugra_api_mcp  # stdio mode
python -m sugra_api_mcp --transport streamable-http --port 8001  # HTTP mode
python scripts/build_endpoint_catalog.py  # rebuild bundled catalog from sibling API openapi.json
```

Run tests:

```bash
pytest
```

## License

MIT © 2026 Sugra Systems, Inc.
