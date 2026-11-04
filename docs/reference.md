# Sugra MCP package reference

The technical reference for the `sugra-api-mcp` package: what each tool returns, how results are shaped and bounded, the error contract, configuration, the CLI and Docker. Setup and the client list are in the [README](https://github.com/Sugra-Systems/sugra-api-mcp#readme); the Sugra API itself is documented at [docs.sugra.ai](https://docs.sugra.ai).

- [Tools](#tools)
- [Response shaping](#response-shaping)
- [Result size and truncation](#result-size-and-truncation)
- [Timeouts and the error contract](#timeouts-and-the-error-contract)
- [Environment variables](#environment-variables)
- [HTTP transport with OAuth](#http-transport-with-oauth)
- [CLI](#cli)
- [Docker](#docker)
- [Development](#development)
- [Troubleshooting](#troubleshooting)

## Tools

The local package exposes eight gateway tools:

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

The server ships a bundled catalog of the full endpoint surface, so discovery (search, describe, toolsets) runs locally without network calls; only actual data requests hit the API.

`describe_endpoint` returns computed `agent_hints` per endpoint so agents can budget time and parallelism before calling:

- `duration_class` - `fast` (under ~2s, snapshot-backed), `slow` (live upstream proxying, occasionally 15s+), or `heavy` (per-item upstream work, large batches can exceed the gateway timeout)
- `max_concurrency` - advisory ceiling for parallel calls from one session
- `bulk_cost` - on per-item bulk endpoints: 1 request credit per item in the request body (the API reports the total in the `X-RateLimit-Cost` response header)

### Hosted-only agent tools

The hosted server at `https://mcp.sugra.ai/mcp` serves the same eight tools plus three composed agent tools that are not available on stdio or self-hosted installs:

| Tool | Purpose |
|---|---|
| `resolve_entity` | Free text (ticker, company, indicator, coin, currency pair) to a canonical market or macro entity. Ambiguous matches return ranked candidates, never a silent pick. |
| `get_snapshot` | Entity plus a named recipe to one composed current view with freshness, provenance, coverage, and billing blocks. Composed calls charge a fixed recipe cost (1-2 requests) from the daily quota. |
| `get_timeseries` | Entity plus metric (`price`, `macro_series`, `etf_flows`, `etf_monthly_flows`) to a bounded series with an explicit downsampling flag. `etf_flows` estimates at filing cadence; `etf_monthly_flows` is the fund's own NPORT-P monthly creations and redemptions. |

These three tools wrap an internal composed plane that requires an infrastructure credential available only on the hosted deployment. The tool code ships inside the package, but it is registered only by the hosted HTTP entry point and only when that credential is present - `pip install sugra-api-mcp` (stdio and self-hosted HTTP) always exposes the classic eight-tool gateway. For compliance entity lookups (LEI / VAT, sanctions screening) use `sugra_entity_lookup` and `sugra_entity_screen`, which work on every transport.

### ChatGPT tool set

A session whose MCP `initialize` names the client `openai-mcp` (ChatGPT), or that sends no client name and a User-Agent starting with it, lists 142 typed tools in place of `search_endpoints`, `describe_endpoint`, `call_endpoint` and `fetch_data`, beside the other tools above: one tool per operation, with a verb name such as `get_stock_quotes` or `get_price_history`, its own input schema and a compact result. Typed tools take no raw location inputs (coordinates, addresses, postal codes). A typed tool may take fewer inputs than its operation: `get_latest_news`, `search_news` and `search_global_news_articles` take one required topic or name and a count, while other clients still reach every input of those operations through `call_endpoint`. Of the 145 typed tools defined, those three are not listed to such a session (`CHATGPT_UNLISTED_TYPED_TOOLS` in `sugra_api_mcp/server.py`). Nothing such a session is listed names a tool it is not listed: `list_toolsets` and `list_sources` carry a description without the `search_endpoints` filter tip, and the prompts and the `sugra://skills/` resources it reads come in a wording that names only the tools it is listed. Every other client lists the tools above unchanged; any client may still call any of these tools by name. The set is defined in `sugra_api_mcp/catalog/data/typed_tools.json`.

### Prompts and resources

Six workflow prompts ship with the server and turn common tasks into one-click flows in clients that surface MCP prompts:

| Prompt | Arguments | What it builds |
|---|---|---|
| `market_snapshot` | `symbol` | A sourced price and profile snapshot for one ticker symbol |
| `macro_briefing` | `country` | Inflation, policy rate, GDP and unemployment for a country, from sovereign sources |
| `sanctions_screening` | `name` | A screen of a name against the sanctions corpus, with the signal and the top matches |
| `sector_compare` | `sector_a`, `sector_b` | A comparison of two sectors through ETF and valuation endpoints |
| `earth_conditions` | `lat`, `lon` | Weather, air quality and nearby hazards for a coordinate |
| `source_overview` | `domain` | What the catalog offers for a data domain, sources included |

The server serves five of the [Sugra API Skills](https://github.com/Sugra-Systems/sugra-api-skills) as MCP resources under `sugra://skills/`, from a pinned commit of that repository: `resources/read` the URI after connect.

## Response shaping

`call_endpoint` and `fetch_data` both support response shaping with `limit`, `fields`, and `include_raw`. Shaping works on enveloped (`{"data": ...}`) and envelope-less payloads alike; `fields` entries may use dotted paths into nested objects (`geo.city`), and `meta.shaped` reports what was actually applied (`fields_applied` / `fields_unmatched`, `limit_applied`, `records_path`, `order`, `kept_end`) rather than echoing the request.

`limit` and `fields` work on the records list: the envelope `data` list, a bare top-level array, or the one list inside an object `data` when exactly one of `data`, `entries`, `events`, `history`, `items`, `observations`, `points`, `records`, `results`, `rows`, `series`, `timeseries` holds a list (for example `data.items` on the latest news, `data.observations` on a FRED series). When `data` has no such single list but every one of its values is an object holding exactly one list named `observations`, as with several named sub-series side by side, `limit` bounds each `data.<key>.observations` list on its own; `fields` there still names keys of `data`. Keys beside that list, such as `total` and `count`, stay as sent, and lists nested inside records are never truncated. A `fields` entry that names a key of `data` itself projects that object instead, and a projection that matches nothing leaves the payload whole.

`meta.shaped.limit_applied` says whether the bound took effect, and `meta.shaped.records_path` names the list used (`data`, `data.<key>`, `data.*.observations`, or null when no records list was used). `limit` keeps the newest end of the records list when every record carries one date or period key (such as `date`, `period` or `year`) in one format and the list runs one way by it: the last N records of an oldest-first list, in their order, or the first N of a newest-first list. Otherwise it keeps the first N records. Whenever a limit bounds a records list, `meta.shaped.order` says `asc`, `desc` or `unknown` and `meta.shaped.kept_end` says `newest` or `first`, each as a map by sub-series name for sibling sub-series.

A top-level JSON array (or scalar) is always wrapped as `{"data": ...}` so the MCP result stays an object; otherwise FastMCP output validation reports the successful call as an error and drops the rows.

`fields` takes at most 32 paths, each at most 256 characters and 16 dotted parts; past any of these the call answers `projection_too_large` before any request is made. With `fields`, one projection also visits at most 100,000 list items, runs for at most 5 seconds, and takes a response of at most 2,000,000 characters of JSON before projection. That bound sits far above the 18,000-character limit on the result, which applies after projection: a 16-day weather forecast is about 295,000 characters before `fields=["daily"]` cuts it to fit. None of these bounds applies without `fields`.

Shaping runs on its own pool of two worker threads, never on the event loop: at most 8 jobs run or wait for a worker, 4 of them for one caller, and a call that finds no free slot within 2 seconds answers `server_busy` with scope `shaping` or `caller_shaping`.

## Result size and truncation

Some endpoints return very large payloads (long price histories, hourly forecasts, full catalogs). A result is capped at 18,000 characters of JSON with ASCII escapes, so a CJK or accented character counts as its six-character escape and non-ASCII text is counted generously.

Clients limit what they pass to the model in their own ways: Claude Code writes a tool result over 50,000 characters to a file instead of handing it to the model, and Codex limits a tool result by tokens, with a budget the user sets, and cuts the middle out beyond it. Every shape we measured at 18,000 characters, the densest included, stayed within Codex's default budget; a denser result, or a lower budget set in the client, can still be trimmed by the client. The cap is the same for every client.

A larger result is cut to fit, whatever its shape: the lists at most two levels under `data` (`data`, `data.<key>`, `data.<key>.<key>`, whatever the key is called) are measured record by record, a big list is cut before small ones beside it, and a cut list keeps at least one record. A list keeps the newest end by the same order rule as `limit`, else its first records; through `call_endpoint`, a forecast or calendar keeps its records from today onward instead, oldest-first or newest-first alike, with `kept_end` `nearest`. `fetch_data` runs its operation through `call_endpoint` and is cut the same way, its `meta.fetch_data` (written when the response's `meta` is an object or absent) counted within the cap.

The cut runs on the shaping pool, never on the event loop, so a full pool answers `server_busy`; it has a 5-second clock, read between records, and past it the result is refused; the fixed tools stop waiting for a cut half a second after that clock, and `call_endpoint` half a second after two such clocks, as its `fields` projection has its own; both then answer `response_too_large`, and a cut that cannot start within the pool's 2-second wait answers `server_busy`.

A failure from the Sugra API, or on the way to it, is held to 17,000 characters before any gate sees it, and so is a refusal the server builds itself, which can echo what the call sent (an unknown `operation_id` or parameter, a path left unresolved): a value too long to fit, such as a long API message, URL or header, is cut to end in `...` and marked `<key>_truncated: true` beside it, and no key is dropped, so the status and every short value stay as they are. The fixed tools gate an error body like any other result; an error payload over the cap that `call_endpoint` passes on as it came is refused without being measured.

`meta.truncated` says what was cut: `reason` (`exceeds_response_size_cap`), `path` of the largest list cut with its `original_count`, `kept_count`, `order` and `kept_end`, the sizes `original_chars`, `kept_chars` and `cap_chars`, a `lists` entry per path (with `kept_range`, the first and last date kept) when several lists were cut, and a `retry_hint` that names what was kept and only the parameters the operation has. A forecast of up to about two weeks keeps the hours from today and its whole `daily` list, and the hint offers `fields=["daily"]` or a smaller `params.forecast_days`; a 16-day forecast can lose its last days as well, and its hint then offers only a smaller `params.forecast_days`, although `fields=["daily"]` returns all 16 days within the cap; a quote history keeps its newest bars, and the hint offers `params.limit` or a narrower `params.start` to `params.end`.

When no cut fits, the result is a `response_too_large` error whose message gives the size and the limit in characters and names the large part: a record that is larger than the limit by itself, a record that does not fit beside the rest of the response and the cut notice (with both sizes, and how many other lists hold such a record), the one record each list keeps when only together they do not fit (with their sum and the largest of them), or the part outside the lists that is too large, and when it can, the `fields` that leave the large part out. The refusal itself always fits the cap: when it would not, it keeps only the size line and a URL cut to 300 characters.

## Timeouts and the error contract

`SUGRA_TIMEOUT` caps each downstream HTTP call from this server to the Sugra API (default 30 seconds). It is one link in a longer chain; when a tool call fails, `elapsed_ms` in the error payload tells you which link cut it:

```
MCP client (agent harness)          own tool timeout, often 60-180s, client-controlled
  -> hosted edge (mcp.sugra.ai)     86400s, effectively unlimited
    -> container ingress            240s, the platform's ceiling
      -> this server (httpx)        SUGRA_TIMEOUT, default 30s
        -> Sugra API -> upstreams   15-60s per upstream call, server-side
```

Tool failures return structured JSON instead of raising, so agents can pick a retry strategy:

| `error` value | Meaning | Retry strategy |
|---|---|---|
| `upstream_timeout` | No response within `SUGRA_TIMEOUT` (`elapsed_ms` close to `timeout_s` x 1000) | Retry once: the aborted attempt usually completes server-side and warms upstream caches. Then narrow the request (smaller batch, tighter filters). |
| `upstream_connect_error` | Could not reach the Sugra API (DNS failure, connection refused) | Retry after a short delay. |
| `upstream_transport_error` | Connection dropped mid-request | Retry once. |
| `invalid_request_url` | The request could not be built, most often because its parameters make the URL longer than 65,536 characters; nothing was sent | Send fewer or shorter parameter values, or split the request. |
| `unusable_api_key` | The API key holds a character this server cannot send in a request header (one outside ASCII, a line break or another control character, or a space or a tab at either end); nothing was sent | Do not retry: the key has to be replaced (see [Troubleshooting](#troubleshooting)). |
| `response_too_deep` | The API's response is nested too deeply to measure, so it is not returned | Retry once; report if persistent. |
| free-text string + `status_code` | The API answered with HTTP 4xx/5xx; `retry_after` included when the API sent a Retry-After header | Honor `retry_after` for 429/503; fix the request for 4xx. |
| `tool_execution_failed` | Unexpected failure inside the gateway (`exception_type` included) | Report if persistent. |
| `query_too_long` | A `search_endpoints` or `fetch_data` query has more than 64 terms or 1000 characters (`max_terms` and `max_chars` included). A term is a run of two or more ASCII letters or digits, and repeats count; nothing was searched | Shorten the query to the instrument, series, place or task. |
| `projection_too_large` | A `fields` projection passed one of its bounds. `limit_kind` names it: `fields` (more than 32 paths), `path_chars` (a path over 256 characters), `path_parts` (a path over 16 dotted parts), `rows` (over 100,000 list items visited), `shaping_ms` (over 5 seconds) or `raw_chars` (a response over 2,000,000 characters before projection). `limit`, `actual`, `field_index` (from 0, for `path_chars` and `path_parts`) and `operation_id` are included, never the field text; the first three kinds are refused before any request is made | Name fewer or shorter fields, add `limit`, narrow the request, or omit `fields`. |
| `server_busy` | A concurrency limit was reached. `scope` is `tool_calls`, `search` or `shaping` for a server-wide limit, `caller_tool_calls`, `caller_search` or `caller_shaping` for the limit on one caller. With `shaping` or `caller_shaping` the API request was already made and its response was dropped; with any other scope the call did no work | Retry after a few seconds. |

All error payloads carry `elapsed_ms`. `url` is present on transport and HTTP errors (not on `tool_execution_failed`, which can fire before a URL exists). On the three transport errors and `invalid_request_url`, `status_code` is `null` (no HTTP status was received) - consumers comparing `status_code` numerically should guard for that. If a tool call instead fails with a bare client-side message and no structured JSON, the timeout fired in your agent harness above this server: raise the client's tool timeout, not `SUGRA_TIMEOUT`.

## Environment variables

User-facing configuration for local installs, MCP clients, Docker stdio, and directory sandboxes (for example Glama Try in Browser). Set only this:

| Variable | Required | Default | Description |
|---|---|---|---|
| `SUGRA_API_KEY` | For API calls | - | Your Sugra API key (`sugra_...`). Get a free key at [app.sugra.ai/register](https://app.sugra.ai/register) (Free tier: 50 req/day). Not needed to start the server: catalog tools (`search_endpoints`, `describe_endpoint`, `list_toolsets`, `list_sources`) work without it; API-calling tools return a structured `missing_api_key` error until it is set. In HTTP mode with a client Bearer token this is only a fallback. |

Optional overrides (leave unset unless you need them):

| Variable | Default | Description |
|---|---|---|
| `SUGRA_API_BASE` | `https://sugra.ai` | Override the Sugra API base URL (self-hosted or beta API only). |
| `SUGRA_TIMEOUT` | `30` | Downstream HTTP timeout in seconds for calls from this server to the Sugra API. |

Operator-only settings for self-hosted Streamable HTTP (reverse proxy CORS/hosts, OAuth authorization-server wiring, and shared secrets) are documented in [self-hosting.md](self-hosting.md). Do not put operator secrets into public directory sandboxes. `SUGRA_MCP_TRUST_PROXY_HEADERS` (off by default) makes the server read `X-Real-IP` and `X-Forwarded-Host`; set it only behind a reverse proxy that overwrites both on every request, and never where clients can reach the process directly (details in the same guide). `SUGRA_MCP_LIMITS` (off by default) turns on request limits counted in the server's own memory; the same guide lists its settings.

## HTTP transport with OAuth

When running with `--transport streamable-http` the server allows unauthenticated MCP discovery requests (`initialize`, `notifications/initialized`, `tools/list`, `resources/list`, `prompts/list`, and `ping`) so ChatGPT Apps and other mixed-auth clients can discover tool metadata. Tool calls still require `Authorization: Bearer ...`. Two token formats are accepted:

- Raw API key (`sugra_...`) - passed through as the downstream `x-api-key`. Compatible with earlier local API-key setups.
- OAuth JWT - signature verified against the issuer's JWKS. The audience must match `https://app.sugra.ai/mcp`, the token must include `sugra:read`, and hosted access is validated against APP before resolving the user's primary API key. Successful hosted OAuth requests update MCP connection activity in APP.

Most users should use the hosted endpoint `https://mcp.sugra.ai/mcp` instead of self-hosting OAuth: OAuth clients sign in there with their Sugra account, and an API key works as Bearer. If you run your own HTTP process, see [self-hosting.md](self-hosting.md).

## CLI

Server startup:

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

## Docker

Build the image from the repository root:

```bash
docker build -t sugra-api-mcp .
```

Run in stdio mode (the default entrypoint) for MCP clients that spawn a local process:

```bash
docker run -i --rm -e SUGRA_API_KEY=sugra_... sugra-api-mcp
```

Run the Streamable HTTP transport on port 8001 with Docker Compose:

```bash
export SUGRA_API_KEY=sugra_...
docker compose up -d
```

Then point your MCP client at `http://localhost:8001/mcp`. The compose service passes `SUGRA_API_KEY` and the optional overrides (`SUGRA_API_BASE`, `SUGRA_TIMEOUT`) from your shell when set, and checks container health against `http://localhost:8001/health`. Reverse-proxy and OAuth operator settings are documented in [self-hosting.md](self-hosting.md).

Every response the application sends, errors included, carries the header `Server: sugra-api-mcp`. So does uvicorn's own 400 for a request it cannot parse under the httptools parser, which the `http` extra installs and uvicorn picks by default; under the h11 parser that 400 goes out without the header. Set `SUGRA_MCP_SERVER_VERSION=1` (or `true`, `yes`, `on`) in the server's environment to add the package version to that header (`sugra-api-mcp/<version>`) and to the `/health` response, which leaves the version out otherwise.

A note on auth: no environment variable is baked into the image and none is required for the container to start. In HTTP mode clients authenticate per request with `Authorization: Bearer sugra_...`, so `SUGRA_API_KEY` on the container is only a fallback for requests without a Bearer token.

## Development

```bash
git clone https://github.com/Sugra-Systems/sugra-api-mcp
cd sugra-api-mcp
pip install -e ".[dev,http]"
export SUGRA_API_KEY=sugra_...
python -m sugra_api_mcp  # stdio mode
python -m sugra_api_mcp --transport streamable-http --port 8001  # HTTP mode
python scripts/build_endpoint_catalog.py  # rebuild bundled catalog from sibling API openapi.json
python scripts/build_endpoint_catalog.py --source https://sugra.ai/openapi.json  # from the live spec
# On DRIFT, catalog-parity.yml opens or updates PR branch ci/catalog-resync.
pytest
```

The test matrix runs on Python 3.13, 3.14 and 3.15.

## Troubleshooting

**Looking for `get_market_price`, `get_macro_indicator`, or `get_news`?** Those curated tool names appear in some older directory listings and never shipped in this package - use `fetch_data` for one-step natural-language calls or `search_endpoints` plus `call_endpoint` for explicit routing.

**`missing_api_key` in tool responses**

The server starts and lists its tools without a key, but API-calling tools (`call_endpoint`, `fetch_data`, the entity tools) return `{"error": "missing_api_key"}` until the server can find one. Depending on how you run it:

- As an MCP tool from your client (Claude, ChatGPT, Gemini, xAI, IDE, etc.): check the `env` block in your MCP config file. Value should be a full key like `sugra_ao1_...`, not empty and not wrapped in extra quotes.
- Shell / CI: `export SUGRA_API_KEY=sugra_...` before running `sugra-api-mcp`.
- HTTP mode: set via `.env` or systemd `EnvironmentFile`, not the shell.

`sugra-api-mcp doctor` reports whether the key is visible to the process.

**`unusable_api_key` in tool responses**

The key the server has for the call holds a character this server cannot send in a request header (a letter outside ASCII, an invisible character, a line break or another control character, or a space or a tab at either end), so API-calling tools send no request and return `{"error": "unusable_api_key"}`, and a retry fails the same way. Depending on where the key comes from:

- A key pasted into a config file or an environment variable can pick up an invisible character on the way. Copy it again from [app.sugra.ai/developer/keys](https://app.sugra.ai/developer/keys).
- The key itself holds such a character, as some older keys do: rotate it at [app.sugra.ai/developer/keys](https://app.sugra.ai/developer/keys) and use the new one. Over a sign-in connection (claude.ai, ChatGPT) the server uses your account's key and reads it again within five minutes. If the page does not offer to rotate the key, email support@sugra.systems.

**`401 Unauthorized` or `403 Forbidden` in tool responses**

Key accepted but rejected. Common causes:

- Key was regenerated in [app.sugra.ai/developer/keys](https://app.sugra.ai/developer/keys) and your config still has the old one.
- Typo - key contains only lowercase letters and digits, no spaces, no trailing newlines.
- Free tier was deactivated. Sign in to verify status.

**`429 Too Many Requests`**

Hit your plan's daily limit. Response headers include `X-RateLimit-Reset` with the UTC timestamp when the counter resets (midnight UTC). Over MCP the tool result carries `reason: daily_limit_reached`, `status_code: 429`, `retry_after`, a `retry_hint` that tells the model not to call a data tool again before the reset (the `X-RateLimit-Reset` time, or 00:00 UTC when the API sent no reset time or a value that is not one) and to tell the user, and `daily_limit` and `plan` when the API named them. Plans: [sugra.systems/api/pricing](https://sugra.systems/api/pricing).

Before the limit is reached, a `call_endpoint` or `fetch_data` result shows what is left: `meta.quota` holds `limit`, `remaining` (requests left today) and `resets_at`, copied from the API's `X-RateLimit-*` response headers. A result carries no `meta.quota` when the API did not report a quota.

**`Invalid Host header`** (only if self-hosting HTTP mode)

FastMCP has DNS rebinding protection for public hostnames behind a reverse proxy. See [self-hosting.md](self-hosting.md) for the allowed-hosts setting.

**Tool result truncated with a `meta.truncated` notice**

The result was over the size cap and was cut to fit; see [Result size and truncation](#result-size-and-truncation) for what is kept and how to ask for less.

**`Python version 3.13 or higher is required`**

sugra-api-mcp requires Python 3.13+. Check: `python --version`. If you have 3.12 or older:

- Ubuntu: install Python 3.13 or newer from your distribution packages or the deadsnakes PPA.
- macOS: `brew install python`
- Windows: download from [python.org](https://www.python.org/downloads/)

Then recreate your venv.

**Hosted `mcp.sugra.ai/mcp` returns 5xx**

The hosted endpoint can briefly restart after deploys. Wait 60 seconds and retry. If persistent, email support@sugra.systems.

**Debugging tool calls locally**

Run with stdio and log JSON-RPC messages:

```bash
SUGRA_API_KEY=sugra_... sugra-api-mcp 2>&1 | tee mcp-debug.log
```

Send manual JSON-RPC from a second terminal using `nc` or an MCP inspector.
