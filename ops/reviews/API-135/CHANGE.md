# CHANGE - API-135 MCP (bare node User-Agent is classed other)

risk-tier: T1
board: https://github.com/Sugra-Systems/sugra-board/blob/main/cards/API-135.md

## Problem

`_UA_PATTERNS` copied the API table, including `node/` which misses a bare `node`.

## Change

Same Codex pattern as the API: keep `node/`, add `(?:^|[\s;(])node(?:$|[\s;)])`. Parity test pins the string.

## Test evidence

The API usage-mix samples plus `node` / padded `node` / `node/18` / `Node.js/18`. Alternative-by-alternative pin updated.
