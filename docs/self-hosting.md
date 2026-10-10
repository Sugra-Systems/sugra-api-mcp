# Self-hosting sugra-api-mcp (operators)

This document is for operators who run the Streamable HTTP transport themselves
(Docker Compose, reverse proxy, or a process manager). It is **not** required
for:

- `pip install sugra-api-mcp` + stdio MCP clients
- Hosted MCP at `https://mcp.sugra.ai/mcp`
- Public directory sandboxes (Glama Try in Browser, and similar)

User-facing configuration is a single secret: `SUGRA_API_KEY`. See the
[README environment variables](../README.md#environment-variables) section.

## Docker Compose (HTTP on port 8001)

```bash
export SUGRA_API_KEY=sugra_...   # optional process-level fallback
docker compose up -d
```

Point clients at `http://localhost:8001/mcp`. Clients may authenticate per
request with `Authorization: Bearer sugra_...`; the container env key is only a
fallback when no Bearer is present.

Compose passes through (when set in the shell): `SUGRA_API_KEY`,
`SUGRA_API_BASE`, `SUGRA_TIMEOUT`, `SUGRA_MCP_ALLOWED_ORIGINS`,
`SUGRA_MCP_ALLOWED_HOSTS`, `SUGRA_MCP_UI_WIDGETS`, `SUGRA_MCP_SERVER_VERSION`.
None are baked into the image.

`SUGRA_MCP_UI_WIDGETS` turns on the MCP Apps price-chart widget. Set it to
`1`, `true`, `yes` or `on` to register `ui://sugra/price-chart.html` and
declare it on `call_endpoint`. The comparison ignores case and surrounding
whitespace, so `TRUE` and ` on ` also turn it on; any other value, an empty
value, or leaving it unset keeps the widget off. The value is read once at
startup, so restart the container after changing it.

`SUGRA_MCP_SERVER_VERSION` adds the package version to the `Server` response
header (`sugra-api-mcp/<version>` instead of `sugra-api-mcp`) and to the
`/health` response. It is off by default: set it to `1`, `true`, `yes` or `on`
to turn it on. The comparison ignores case and surrounding whitespace; any
other value, an empty value, or leaving it unset keeps the version out. The
header is fixed when the server starts: after changing the value, run
`docker compose up -d` again, which recreates the container with it.

## Reverse proxy and browser clients

| Variable | Required | Default | Description |
|---|---|---|---|
| `SUGRA_MCP_ALLOWED_HOSTS` | Behind a reverse proxy | - | Comma-separated public hostnames FastMCP may accept (DNS rebinding protection). Example: `mcp.example.com,example.com`. |
| `SUGRA_MCP_ALLOWED_ORIGINS` | For browser OAuth UIs | built-in list (chatgpt.com, claude.ai, cursor.sh, and related clients) | Comma-separated allowed Origins for the outer Starlette CORS layer and the inner FastMCP Origin check (kept in sync). `*` disables the inner Origin check (dev only); Bearer auth still gates tool calls. |
| `SUGRA_MCP_TRUST_PROXY_HEADERS` | No | off | Read the caller's host from `X-Forwarded-Host` and its address from `X-Real-IP`. See below. |

### Trusting proxy headers

`SUGRA_MCP_TRUST_PROXY_HEADERS` is off by default. Set it to `1`, `true`, `yes`
or `on` (case and surrounding whitespace ignored; any other value, an empty
value or unset keeps it off). It is read on every request.

Off, the server reads the `Host` header and the connection peer, as it always
has, and ignores `X-Forwarded-Host` and `X-Real-IP`.

On, the host class and the network prefix recorded for a request come from the
two headers instead. A value that is not a single plain `host[:port]` or a
single IP address is ignored and the `Host` header and the peer are used.
`X-Forwarded-For` is never read, on or off.

What the setting assumes: every request reaches this process through a reverse
proxy of yours that sets `X-Real-IP` and `X-Forwarded-Host` itself on every
request, replacing whatever the client sent (for nginx, `proxy_set_header
X-Real-IP $remote_addr;` and `proxy_set_header X-Forwarded-Host $host;`), and
the process accepts no connection from anywhere else. A load balancer or
container ingress between that proxy and this process is fine as long as it
passes both headers through unchanged; it does not set them itself. If the
process is also reachable by other clients on the network, those clients can
send both headers themselves and the recorded host and address are theirs to
choose, so restrict access to the proxy's address first.

The setting changes what is recorded about a request. With
[request limits](#request-limits) on (`SUGRA_MCP_LIMITS`), a trusted `X-Real-IP`
also picks the address whose failed key checks are counted and held; with the
setting off, or with no usable `X-Real-IP`, that address is the connection peer.
The host allow-list (`SUGRA_MCP_ALLOWED_HOSTS`) is always checked against the
`Host` header itself, and `X-Forwarded-Host` never passes or fails it.

## Request limits

`SUGRA_MCP_LIMITS` is off by default. Set it to `1`, `true`, `yes` or `on`
(case and surrounding whitespace ignored; any other value, an empty value or
unset keeps it off). It is read once at startup, so restart after changing it.

Off, nothing below exists and requests are answered exactly as before. On, the
server adds, for requests to `/mcp`:

- A budget for requests without recognised credentials: 10 a second, with room
  for a burst of 20, checked before any signature check or Sugra API call. Over
  it: `429` with `Retry-After`.
- Two lanes for POST requests: 24 places for recognised
  credentials and 8 for everything else, at most 4 of those 8 for keys the
  Sugra API has not yet accepted. A request with no place gets `503` with
  `Retry-After` and `{"error": "server_busy"}`. A JWT is recognised once this
  server has verified it (until it expires, at most 10 minutes) and a key once
  the Sugra API has accepted a call made with it (5 minutes). Only SHA-256
  digests are kept, at most 10,000. A credential the Sugra API later refuses is
  dropped and stays out until its earlier term would have ended.
- A hold on addresses: when 30 refusals of unaccepted keys from one public
  address fall inside any one minute (a sliding minute, not one that starts at
  the first refusal), a new unaccepted key from it gets `429` with
  `Retry-After` for 10 minutes without reaching the Sugra API. Accepted keys,
  JWTs and requests without credentials are never held. The address is the
  connection peer, or the `X-Real-IP` when
  [proxy headers are trusted](#trusting-proxy-headers); a private or loopback
  address is never held.
- A limit on the checks of new keys: 20 a second. Over it the call returns
  `server_busy` (scope `key_checks`).
- Optional per-key counters, only for the numbers you set. A call is counted
  against its key before it runs, whether or not the server recognises the key
  yet: the limit is read and the call charged in one step, so calls that arrive
  together cannot all take a place that only one of them may have. Over a limit
  the call returns `rate_limited` with `retry_after`. A call stays counted,
  except that a call of a key the server has not yet recognised is given back
  when no request went to the Sugra API or the API answered 401 or 403 for the
  key, so a made-up key leaves nothing behind. A request that went out and was
  not refused stays counted, however the call ended.
  A key the table has no room for is refused before its call, never let through
  uncounted.
- Counts of `server/discover` requests, `events/...` requests (one value) and
  the extensions a client declares at `initialize` (`ui` or `other`), logged
  beside the existing demand count as `sdemandx1`.

| Variable | Required | Default | Description |
|---|---|---|---|
| `SUGRA_MCP_LIMITS` | No | off | Turns the request limits on. |
| `SUGRA_MCP_KEY_LIMIT_PER_MINUTE` | No | unset | Calls one key may make in a minute. Unset means no limit. |
| `SUGRA_MCP_KEY_LIMIT_PER_DAY` | No | unset | Calls one key may make in a day. Unset means no limit. |

Every counter lives in the memory of the server process, and only SHA-256
digests of keys and addresses are kept, never their text. Counting is exact
within one process. The counters start at zero when the process starts and are
lost when it stops or restarts. With more than one replica, each replica
enforces its own limits: a number set here is a limit for each replica, not a
total across them. The memory has room for a bounded number of counters for each
limit; a key or an address it has no room to count is refused through that
limit's own refusal until a window ends, never let through uncounted.

## OAuth authorization-server wiring

Only needed when this process validates OAuth JWTs and talks to app.sugra.ai
(or a private twin) for user lookup and MCP activity. Hosted production already
has this configured; do not set these in directory sandboxes.

| Variable | Required | Default | Description |
|---|---|---|---|
| `SUGRA_APP_URL` | HTTP + OAuth | `https://app.sugra.ai` | Base URL of the authorization server. |
| `SUGRA_JWKS_URL` | No | `$SUGRA_APP_URL/oauth/jwks.json` | JWKS endpoint for JWT signature verification. |
| `INTERNAL_API_TOKEN` | HTTP + OAuth | - | Shared secret for user lookup and MCP activity endpoints on the authorization server. Must match the value on the app.sugra.ai (Laravel) process. **Never commit, never put in public Try/sandbox forms.** |

Accepted Bearer forms on tool calls:

- Raw API key (`sugra_...`) - forwarded as the downstream `x-api-key`
- OAuth JWT - audience `https://app.sugra.ai/mcp`, scope includes `sugra:read`

Unauthenticated discovery (`initialize`, `tools/list`, `resources/list`,
`prompts/list`, `ping`, and related handshake notifications) is allowed so
connector UIs can list tools before the user finishes sign-in.
