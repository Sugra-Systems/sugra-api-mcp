# CHANGE - MCP-19 (unknown_error is 66% of MCP tool failures and names nothing)

risk-tier: T1
board: https://github.com/Sugra-Systems/sugra-board/blob/main/cards/MCP-19.md

## Problem

Over the 90 days to 2026-09-11, two thirds of the hosted MCP's tool-call failures carried `mcp.error.code = unknown_error` (76% over the last 7 days). The span could say only that something failed.

The cause is structural, not one bug. `SugraClient._handle` keeps the API's own `error` text (or `HTTP <status>`) at `result["error"]` for the caller and puts the status beside it; `trace_mcp_tool` allowlists the text and never read the status. Every HTTP failure from the API - a caller's 429 quota, a bad-symbol 404, a 503 upstream outage - was therefore one bucket.

The API's own request log over the same window says what the bucket was, operation by operation: `kalshi_events` is the router's 503 when its upstream is unavailable, plus 429; `crypto_coin_id_history` is 502 and 429; `quotes_symbol_historical` is 429, 404 and 400; `indicators_pct_return` is 429; and the agent plane, which only the MCP calls, is 429 on resolve, 502 on snapshot, and 502 / 500 / 422 on timeseries. The per-operation `unknown_error` counts on the MCP side sit inside those all-caller counts, within the bound the API log allows.

## Change

- `observability.py`: a failed result whose dict carries an int `status_code` is named from a fixed table - `upstream_http_400` / `401` / `403` / `404` / `422` / `429` / `500` / `502` / `503` / `504` for the statuses the API returns deliberately, `upstream_http_3xx` / `4xx` / `5xx` for the rest of the class. A code the tool named itself still wins (`agent_plane_unavailable` sits beside a 403). `mcp.success` on a returned result is `not errors.is_error_payload(result)`, the definition the tool protocol already applies, instead of a third private one: a partial envelope pairing an error note with data is a success (the API emits no such body the catalog can reach today, so the count is unchanged and the definition is one), and an error key whose value is not a string, with no data beside it, is a failure (an empty string already was). `invalid_anchor`, `request_failed` and `missing_api_key`, which tools already returned, join the allowlist.
- `tools/agent.py`: because a partial envelope now reaches the agent extractor, `recipe_version` is attached only as a known value: `<recipe>@<n>` for the seven snapshot recipes the `get_snapshot` docstring lists, or `timeseries.<metric>@<n>` for the four metrics `get_timeseries` accepts, derived from the tool's own metric type so the two cannot drift apart. The version is 1 to 999 with no leading zero. This mirrors `_KNOWN_STATUSES`; a recipe the plane adds later is dropped from spans until it is added here, never exported unseen.
- `client.py`: any non-2xx answer is an HTTP failure (`>= 300`). A 3xx used to fall through the success path and come back as `{"error": ""}`.
- `tools/gateway.py`: `fetch_data` passes `operation_id` to `call_endpoint` by keyword, so the delegated call's span carries the operation (every delegated failure used to be a `call_endpoint` span with no operation).
- Version 0.11.1 -> 0.12.0. The release rule says minor for additive codes and major for a semantics change; `mcp.success` did change definition, but for every payload the API emits today the two definitions agree, so what an operator observes is additive. Whether a 0.x semantics change should mean 1.0.0 is the owner's call; tagging stays a separate step either way.

## Not changed

The tool result a caller sees is unchanged for every 4xx and 5xx: the API's text stays at `error`, status and url beside it. Only a 3xx answer changes shape, from a bare empty error to the standard HTTP-failure dict. The delegation still emits two spans per `fetch_data` call (one per decorated tool); that double count is visible, not hidden, and is out of this card's scope. A 2xx body that is not JSON, or a 2xx error dict with no status, stays `unknown_error`: the residual the code is for.

## Privacy

Nothing taken from the result dict reaches the span except through the fixed status table keyed on an int and the bounded `recipe_version` set. Tests assert the API text is absent from every span attribute, pin the attribute-name set per path and the exact scalar type of every value, and seed a sentinel in every field of the failure dict.

## Test evidence

Full suite passes; `ruff check` clean; `git diff --check` clean. New in `tests/test_observability.py`: eleven test functions - the status table and class buckets, type and range strictness of `status_code`, precedence of a named code, a table-to-allowlist drift guard, `is_error_payload` alignment from both sides, the entity and keyless codes, `recipe_version` bounded to the known set through the real extractor (free-text and regex-conforming values rejected in a partial envelope; the six distinct values production spans carried over 90 days kept, plus the rest of the manifest; twenty-one rejected values), the known set kept equal to what the tools document and accept, and the plane's real series envelope keeping its value. One test each in `tests/test_client_errors.py` (3xx is a structured HTTP failure) and `tests/test_gateway.py` (the delegated span names its operation). Every new case failed against the old code except the empty-string parametrization, which pins behaviour that already held.

Mutation evidence (one mutant per run, byte-copy restore), each failing at least one test: the error text attached as a list under a new key; the url under a new key; the tool name as a list on one status; `recipe_version` accepted as any string; accepted by shape only; the timeseries family dropped from the set; the version widened back to any digits; two table entries swapped; the status put before the tool-named code; the old string success test restored. Dropping the explicit bool guard in `_http_status_error_code` survived every test because True and False are 1 and 0, outside the 300..599 range the lookup already applies; the guard was dead code and is removed.

## Review

Round 1 (codex, head 02fda69): BLOCK, two findings, both accepted.

- S2 security: the new success classification let a partial envelope reach the agent extractor, which attached `recipe_version` as any string. First closed by a `<recipe>@<n>` shape check.
- S3 maintainability: the new privacy assertions scanned string values for one sentinel, so a mutation attaching the error text as a list and another attaching the url both survived. Closed by pinning the allowed attribute-name set per path and seeding a sentinel in every field of the failure dict.

Round 2 (codex, head 54964d0): BLOCK, both held open in narrower form, both accepted.

- S2: a shape still passes `user_ssn_123456789@1`. Closed by a known-recipe set.
- S3: attribute value TYPES were unchecked. Closed by an exact-type map per attribute name in the assertion helper.

Verification pass (independent, four lenses, head 54964d0): the round-1 and round-2 sets named only snapshot recipes and rejected the plane's real series value `timeseries.price@1`, 168 of 266 live values - every `get_timeseries` success span would have silently lost the dimension after the deploy. Accepted: the family is admitted from the tool's own metric type and the live values are pinned in a test. Also accepted from that pass: the public record no longer carries absolute traffic counts, the acceptance query is named consistently, a stale `>= 400` docstring in `entities.py` and the "exact type" wording in `observability.py` are corrected.

Round 3 (codex, head f362464): BLOCK.

- S3 correctness: the same timeseries regression, independently confirmed against the plane's own producer. Closed as above.
- S2 security: asked for a fixed list of approved recipe/version constants because `company_snapshot@123456` was accepted. Accepted in part: the version is bounded to 1..999 with no leading zero, so no text or identifier fits. Declined in part: a fixed version list would go quiet on the plane's first version bump, the exact "silently blinds KQL" failure the release rule warns about and the failure the verification pass had just measured at 63% of a dimension; a bounded number keeps the dimension useful across a bump while carrying nothing.

Records: `ops/reviews/MCP-19/REVIEW-codex-*.md`.

## Acceptance

`unknown_error` below 10% of failures over a 7-day window after the hosted deploy (query 5c in the operator reference measures it; 76% in the 7 days before the deploy), and the top three operations above resolve to named codes.
