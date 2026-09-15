# CHANGE - MCP-26.1.4 MCP (OAuth spans carry the APP-verified connector platform)

risk-tier: T2
board: https://github.com/Sugra-Systems/sugra-board/blob/main/cards/MCP-26.1.4.md

## Problem

OAuth tool spans named the door but not which connector. JWT has no client claim. clientInfo is self-asserted. The APP knows the platform on the connection row.

## Change

`_validate_mcp_access` still admits on any 2xx. If the JSON body has an allowlisted `platform`, that value is cached with the 60 s pass and stored on the request principal. `_caller_attrs` attaches `mcp.caller.platform` only when auth is oauth and the value is in the same six-class set. Missing, 204, `{"ok": true}` without platform, or an unknown string: the call still succeeds and the attribute is omitted. A `sugra_` key never gets the attribute.

Requires the APP activity body from the sibling PR. Safe if this lands first: platform is then always omitted.

## Test evidence

JWT 204 still admits with platform None. JWT 200 with anthropic caches the value for the next validate. Unknown platform omitted. Span unit tests pin oauth/allowlisted vs api_key/unknown. Request tests pin oauth spans `mcp.caller.platform=anthropic` and api_key spans without it.
