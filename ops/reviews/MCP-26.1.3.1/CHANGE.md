# CHANGE - MCP-26.1.3.1 (MCP spans name the caller, so a per-caller refusal and the support funnel can be traced)

risk-tier: T2
board: https://github.com/Sugra-Systems/sugra-board/blob/main/cards/MCP-26.1.3.1.md

## Problem

MCP-26.1.3 stage A records how a call arrived (`mcp.caller.*`) and not which caller. A per-caller `server_busy` refusal cannot be grouped to an account, and query 6 on `user_Id` matches nothing.

## Change

Owner rulings 2026-09-15: D1 ignore (no public privacy/data-use change), D2 network `/24`, D3 keep 90-day App Insights retention and do not disclose identity telemetry to users (operator logs).

- `enduser.pseudo.id` (Azure `user_Id`) is the admission name from `current_caller()`: `http:` plus 16 hex of SHA-256 of the resolved API key, `http:anonymous`, or `local`. Never the key.
- `enduser.id` (Azure `user_AuthenticatedId`) is the numeric OAuth user id as a 1-10 digit decimal string, OAuth only. A `sugra_` key does not get one.
- `mcp.caller.session` is 16 hex of SHA-256 of `Mcp-Session-Id`. The raw id is never attached.
- `mcp.caller.net` is `loopback`, `private`, an IPv4 `/24` or an IPv6 `/48`, and only when the ASGI peer equals `X-Real-IP`. A forged `X-Forwarded-For` under `trusted_hosts=*` is dropped.
- Facts still come from the SDK request context per call, never a middleware ContextVar. `current_caller_facts` calls `current_caller()` so the span and admission share one name.

No public legal document is changed.

## Test evidence

Unit pins on the four new normalizers (admission name, OAuth id, session digest, net plus the X-Real-IP check). Hostile inputs still cannot put a key, email, CRLF or 64 KB of text on a span. Every traced exit and a refused call carry identity. Request tests: one session two principals, concurrent calls, auth `none`, inherited HTTP marker, admission name equals `user_Id`, and ProxyHeadersMiddleware (`127.0.0.1` gives `8.8.8.0/24`, `*` gives no net). The Azure exporter maps `enduser.*` to `ai.user.id` / `ai.user.authUserId` and keeps `mcp.caller.*` in customDimensions. Full suite 964 passed, `ruff check` clean.

## Review

Two independent vendor reviews of the implementation. Hosted nginx overwrites X-Real-IP, so a client cannot make both headers agree on a forged address there. A self-hosted process with FORWARDED_ALLOW_IPS=* and no such proxy can still record a client-claimed prefix; the tests pin the disagreeing-header case as dropped.

## Acceptance

A per-caller `server_busy` refusal can be grouped by `user_Id`. One OAuth account's calls can be followed on `user_AuthenticatedId`. No span carries an API key, token, raw address or free text.
