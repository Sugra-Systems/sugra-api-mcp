# sugra-api-mcp

<!-- mcp-name: io.github.Sugra-Systems/sugra-api-mcp -->

<p align="center">
  <img src="https://sugra.systems/images/sugra-logo-bold.svg" alt="Sugra" width="128" height="128" />
</p>

[![PyPI](https://img.shields.io/pypi/v/sugra-api-mcp.svg)](https://pypi.org/project/sugra-api-mcp/)
[![Python](https://img.shields.io/pypi/pyversions/sugra-api-mcp.svg)](https://pypi.org/project/sugra-api-mcp/)
[![License](https://img.shields.io/pypi/l/sugra-api-mcp.svg)](https://github.com/Sugra-Systems/prod-sugra-ai-MCP/blob/main/LICENSE)

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

v0.4.0 is a breaking gateway release. Curated tools such as `get_market_price`, `get_macro_indicator`, and `get_news` were removed. The package now exposes exactly five tools:

| Tool | Purpose |
|---|---|
| `search_endpoints` | Search the bundled endpoint catalog. Runtime search does not fetch `/openapi.json`. |
| `describe_endpoint` | Inspect an endpoint by `operation_id`, including path, method, parameters, and required inputs. |
| `call_endpoint` | Call a Sugra API operation by `operation_id`. Arbitrary path calls are no longer supported. |
| `list_toolsets` | List catalog groups and endpoint counts. |
| `list_sources` | Show bundled catalog source metadata. |

`call_endpoint` supports response shaping with `limit`, `fields`, and `include_raw`.

## Installation

```bash
pip install sugra-api-mcp
```

Get a free API key at [app.sugra.ai/settings/billing](https://app.sugra.ai/settings/billing) (Free tier: 50 req/day).

## Usage with Claude Desktop (stdio)

Add to `claude_desktop_config.json`:

- macOS: `~/Library/Application Support/Claude/claude_desktop_config.json`
- Windows: `%APPDATA%\Claude\claude_desktop_config.json`

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

### HTTP transport with OAuth

When running with `--transport streamable-http` the server validates the incoming `Authorization: Bearer ...` header on every request. Two token formats are accepted:

- Raw API key (`sugra_...`) - passed through as the downstream `x-api-key`. Backward compatible with v0.1.x.
- OAuth JWT - signature verified against the issuer's JWKS. The `sub` claim identifies the user; the server then looks up that user's primary API key via an internal endpoint on the authorization server.

| Variable | Required | Default | Description |
|---|---|---|---|
| `SUGRA_APP_URL` | HTTP + OAuth | `https://app.sugra.ai` | Base URL of the authorization server |
| `SUGRA_JWKS_URL` | No | `$SUGRA_APP_URL/oauth/jwks.json` | JWKS endpoint |
| `INTERNAL_API_TOKEN` | HTTP + OAuth | - | Shared secret for the user lookup endpoint on the authorization server. Same value must be set on both the MCP process and the app.sugra.ai Laravel process |

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

MIT © 2026 Sugra Systems, Inc. Author: Arman Obosyan with Codex.
