# Sugra MCP example: Python agent

Set two keys, run one command, and watch live Sugra data flow into an LLM agent.

This is the shortest path from zero to a working agent that can query the Sugra
intelligence platform over the Model Context Protocol (MCP). The hosted Sugra MCP
server exposes a unified catalog of data and analysis endpoints as tools. This
example wires those tools into a plain Anthropic tool-use loop. The same pattern
is provider-agnostic - swap the LLM client and keep the MCP wiring.

## What you get

The hosted Sugra MCP server at `https://app.sugra.ai/mcp` exposes eleven tools:
eight gateway tools plus three composed agent tools.

| Tool | What it does |
|------|--------------|
| `fetch_data` | One-step natural-language query: picks the best endpoint and calls it |
| `search_endpoints` | Find endpoints by keyword |
| `describe_endpoint` | Inspect an endpoint's parameters |
| `call_endpoint` | Call a specific endpoint by id |
| `list_toolsets` | List the available Sugra toolsets |
| `list_sources` | List the available Sugra data sources |
| `sugra_entity_screen` | Screen a person or organization name for a sanctions screening signal |
| `sugra_entity_lookup` | Resolve an entity by LEI or VAT id into a composed KYB view |
| `resolve_entity` | Resolve free text to a canonical market or macro entity |
| `get_snapshot` | Composed current view of an entity via a named recipe |
| `get_timeseries` | Bounded timeseries for an entity: price, macro series, or ETF flows |

The last three are composed agent tools and register on the hosted endpoint
only; a self-hosted `sugra-api-mcp` install serves the eight gateway tools.

Sugra toolsets cover Sugra Finance, Sugra Economics, Sugra News, Sugra Crypto,
Sugra Forex, Sugra Weather, and more - all behind one gateway.

## Prerequisites

- Python 3.11 or newer
- An Anthropic API key and a free Sugra API key (next section)

## Get your keys (2 minutes)

1. Sugra API key - free tier, 50 requests/day, no credit card, no time limit:
   https://app.sugra.ai/settings/billing
2. Anthropic API key: https://console.anthropic.com

Then, from the `examples/` directory:

```bash
cp .env.example .env
# open .env and paste in both keys
```

Both example agents read the shared `examples/.env` file.

## Run it

```bash
cd python-agent
python -m venv .venv
# Windows:        .venv\Scripts\activate
# macOS / Linux:  source .venv/bin/activate
pip install -r requirements.txt
python agent.py "What is the current US federal funds rate?"
```

It defaults to the same demo question if you do not pass one.

## Example output

Captured run (the model's wording varies; data values change as new data arrives):

```
> python agent.py "What is the current US federal funds rate?"

Connected to the Sugra MCP. 11 tools available.
[tool] fetch_data {"query": "US federal funds rate", "params": {"series_id": "FEDFUNDS"}}

The current US federal funds rate is 3.64 percent (Federal Funds Effective Rate),
based on the latest monthly reading for April 2026. It has held at 3.64 percent
since January 2026, easing from 3.72 percent in December 2025. Source: Sugra Economics.
```

## How it works

1. The agent connects to the hosted Sugra MCP server over Streamable HTTP,
   sending your Sugra API key as an `Authorization: Bearer` header.
2. It calls `list_tools` to discover the eleven Sugra tools and maps each tool's
   input schema into the Anthropic tool-use format.
3. It runs a standard tool-use loop: the model decides which Sugra tool to call,
   the agent runs it over MCP, returns the result, and repeats until the model
   produces a final answer (capped at five turns).

MCP is simply where the tools come from. The loop itself is ordinary tool-use,
so you can point it at any provider that supports tools.

## Going further

- Ask broader questions: economics, markets, crypto, weather, and more are all
  reachable through `fetch_data`.
- The free tier allows 50 requests per day. A single run uses only a few calls.
  Upgrade at https://app.sugra.ai/settings/billing when you need more.
- Change the model: the `MODEL` constant at the top of `agent.py` is marked
  "change me" - set it to any current Anthropic model id.
- Bring your own provider: keep the MCP wiring, replace the LLM client.
- Prefer TypeScript? See the sibling [typescript-agent](../typescript-agent/)
  example.

## Links

- Sugra platform: https://sugra.ai
- Sugra MCP package: https://pypi.org/project/sugra-api-mcp
- Model Context Protocol: https://modelcontextprotocol.io

## License

MIT. See the repository [LICENSE](../../LICENSE).
