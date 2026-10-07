# Self-hosting sugra-api-mcp (operators)

This document is for operators who run the Streamable HTTP transport themselves
(Docker Compose, reverse proxy, or a process manager). It is **not** required
for:

- `pip install sugra-api-mcp` + stdio MCP clients
- Hosted MCP at `https://app.sugra.ai/mcp`
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

The setting changes only what is recorded about a request. The host allow-list
(`SUGRA_MCP_ALLOWED_HOSTS`) is always checked against the `Host` header itself,
and `X-Forwarded-Host` never passes or fails it.

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
