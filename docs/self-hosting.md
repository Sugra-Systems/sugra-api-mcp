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
`SUGRA_MCP_ALLOWED_HOSTS`. None are baked into the image.

## Reverse proxy and browser clients

| Variable | Required | Default | Description |
|---|---|---|---|
| `SUGRA_MCP_ALLOWED_HOSTS` | Behind a reverse proxy | - | Comma-separated public hostnames FastMCP may accept (DNS rebinding protection). Example: `mcp.example.com,example.com`. |
| `SUGRA_MCP_ALLOWED_ORIGINS` | For browser OAuth UIs | built-in list (chatgpt.com, claude.ai, cursor.sh, and related clients) | Comma-separated allowed Origins for the outer Starlette CORS layer and the inner FastMCP Origin check (kept in sync). `*` disables the inner Origin check (dev only); Bearer auth still gates tool calls. |

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
