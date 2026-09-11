# CHANGE - MCP-19 (unknown_error is 66% of MCP tool failures and names nothing)

risk-tier: T1
board: https://github.com/Sugra-Systems/sugra-board/blob/main/cards/MCP-19.md

## Problem

Over the true 90-day window (2026-06-14 to 2026-09-11) the hosted MCP emitted 20356 tool-call spans, 3461 of them failures, and 2290 of those carried `mcp.error.code = unknown_error`. The span could say only that something failed.

The cause is structural, not one bug. `SugraClient._handle` keeps the API's own `error` text (or `HTTP <status>`) at `result["error"]` for the caller and puts the status beside it; `trace_mcp_tool` allowlists the text and never read the status. Every HTTP failure from the API - a caller's 429 quota, a bad-symbol 404, a 503 upstream outage - was therefore one bucket.

What the bucket was, from the API's own request log over the same window (all callers): `kalshi_events` 503 x250 and 429 x35; `crypto_coin_id_history` 502 x93 and 429 x82; `quotes_symbol_historical` 429 x557, 404 x181, 400 x124; `indicators_pct_return` 429 x86; the agent plane, which only the MCP calls: resolve 429 x86, snapshot 502 x44, timeseries 502 x89, 500 x11, 422 x9. The MCP-side `unknown_error` counts per operation match these one for one.

## Change

- `observability.py`: a failed result whose dict carries an int `status_code` is named from a fixed table - `upstream_http_400` / `401` / `403` / `404` / `422` / `429` / `500` / `502` / `503` / `504` for the statuses the API returns deliberately, `upstream_http_3xx` / `4xx` / `5xx` for the rest of the class. A code the tool named itself still wins (`agent_plane_unavailable` sits beside a 403). `mcp.success` on a returned result is `not errors.is_error_payload(result)`, the definition the tool protocol already applies, instead of a third private one: a partial envelope pairing an error note with data is a success (the API emits no such body the catalog can reach today, so the count is unchanged and the definition is one), and an error key whose value is not a string, with no data beside it, is a failure (an empty string already was). `invalid_anchor`, `request_failed` and `missing_api_key`, which tools already returned, join the allowlist.
- `tools/agent.py`: because a partial envelope now reaches the agent extractor, `recipe_version` is attached only in the plane's `<recipe>@<n>` shape (a fixed grammar, checked by regex) rather than as any string. The extractor is the privacy allowlist for its dimensions; a shape check keeps foreign text off the span without the MCP carrying a copy of the plane's recipe manifest that would drift.
- `client.py`: any non-2xx answer is an HTTP failure (`>= 300`). A 3xx used to fall through the success path and come back as `{"error": ""}`.
- `tools/gateway.py`: `fetch_data` passes `operation_id` to `call_endpoint` by keyword, so the delegated call's span carries the operation (155 such spans over 90 days had none).
- Version 0.11.1 -> 0.12.0: additive error codes bump minor per the release rule. Tagging is the owner's separate step.

## Not changed

The tool result a caller sees is unchanged for every 4xx and 5xx: the API's text stays at `error`, status and url beside it. Only a 3xx answer changes shape, from a bare empty error to the standard HTTP-failure dict. The delegation still emits two spans per `fetch_data` call (one per decorated tool); that double count is visible, not hidden, and is out of this card's scope.

## Privacy

Nothing taken from the result dict reaches the span except through the fixed table keyed on an int. Tests assert the API text is absent from every span attribute for every named status and for every rejected `status_code` type (string, bool, float, 2xx, out of range).

## Test evidence

Full suite passes; `ruff check` clean; `git diff --check` clean. New tests: 22 in `tests/test_observability.py` for the status table, type strictness, precedence of a named code, a table-to-allowlist drift guard, `is_error_payload` alignment from both sides, entity and keyless codes, plus 3 for the `recipe_version` shape check through the real agent extractor; 1 in `tests/test_client_errors.py` (3xx is a structured HTTP failure); 1 in `tests/test_gateway.py` (the delegated span names its operation). Every new test failed before the change and passes after it.

Mutation evidence (one mutant per run, byte-copy restore): attaching the error text as a list under a new key, attaching the url under a new key, accepting any string as `recipe_version`, swapping two table entries, putting the status before the tool-named code, and restoring the old string success test each fail exactly one test. Dropping the explicit bool guard in `_http_status_error_code` survived every test because True and False are 1 and 0, outside the 300..599 range the lookup already applies; the guard was dead code and is removed.

## Review

Round 1 (codex, head 02fda69): BLOCK, two findings, both accepted and closed.

- S2 security: the new success classification let a partial envelope reach the agent extractor, which attached `recipe_version` as any string. Closed by the `<recipe>@<n>` shape check in `tools/agent.py` and a regression through the real extractor with token-bearing text in a partial envelope.
- S3 maintainability: the new privacy assertions scanned string values for one sentinel, so a mutation attaching the error text as a list and another attaching the url both survived 109 tests. Closed by pinning the allowed attribute-name set per path and seeding a sentinel in every field of the failure dict; both mutations now fail.

Records: `ops/reviews/MCP-19/REVIEW-codex-*.md`.

## Acceptance

`unknown_error` below 10% of failures over a 7-day window after the hosted deploy (query 5b in `MCP_OBSERVABILITY.md` measures it), and the top three operations above resolve to named codes.
