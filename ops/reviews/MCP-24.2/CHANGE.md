# CHANGE - MCP-24.2 (the body array branch is typed as an array of objects)

risk-tier: T1
board: https://github.com/Sugra-Systems/sugra-board/blob/main/cards/MCP-24.2.md

## Problem

`call_endpoint` and `fetch_data` annotated `body` as `dict[str, Any] | list[Any] | None`. Pydantic renders `list[Any]` as `{"type": "array", "items": {}}`, so both tool input schemas advertised an array branch whose items could be any JSON value. No tool in the published 1.0.0 listing snapshot carries an untyped array branch (that snapshot describes `body` as object or null), and the next listing version rescans the live server, so the untyped branch is what directory reviewers would see. The server also accepted whatever that schema allowed: an array of strings or numbers passed argument validation and went upstream as a POST body.

The untyped branch was wider than any operation needs. Re-counted from `sugra_api_mcp/catalog/data/endpoints.json` (1626 operations, 15 of them POST):

- 13 operations carry a request body schema; 12 require the body and 1 makes it optional.
- 11 of the 13 have a top-level `object` body.
- Exactly 1 has a top-level `array` body: `post_openfigi_mapping`, whose `items` is an object schema (`MappingJob`, `type: object`, required `idType` and `idValue`).
- 1 (`post_statistical_agencies_ssb_data_table_id`, optional body) is `anyOf [object with additionalProperties true, null]`.
- The remaining 2 POST operations (`post_openfigi_filter`, `post_openfigi_search`) declare no request body schema and do not require a body.
- Arrays of scalars appear only nested inside object bodies (`urls`, `symbols` and `ips` of strings, `asns` of integers), plus one nested array of objects (`items` in `post_entity_screen_batch`). The object branch keeps accepting all of them.

## Change

Three lines in `sugra_api_mcp/tools/gateway.py`:

- `call_endpoint` and `fetch_data`: `body` is `dict[str, Any] | list[dict[str, Any]] | None`. The descriptions are unchanged.
- `_missing_required` declares the same type for its `body` parameter.

Generated schema, read from `mcp.list_tools()`, identical on both tools (description omitted):

```json
{"anyOf": [{"additionalProperties": true, "type": "object"}, {"items": {"additionalProperties": true, "type": "object"}, "type": "array"}, {"type": "null"}], "default": null, "title": "Body"}
```

Before the change the array branch was `{"items": {}, "type": "array"}`; the other two branches, `default` and `title` are byte-for-byte what they were.

Runtime effect: the MCP SDK validates tool arguments with pydantic before the tool function runs. A top-level array holding a non-object item (a string, number, null or nested array, at any position) is now refused with `isError: true` and a validation message naming `body`, with no catalog load and no upstream call. A JSON string holding such an array is pre-parsed by the SDK and refused the same way. Arrays of objects, object bodies and null bodies behave exactly as before.

## Not changed

- Tool names, the tool count, every other argument and both `body` descriptions. The `limit` and `fields` Field blocks in the same file are untouched; sibling changes own them.
- The object and null branches the 1.0.0 listing describes. The object branch still has `additionalProperties: true` and accepts nested arrays of any values; a null body still passes validation and the tool answers that a required body is missing. Nothing 1.0.0 advertises is narrowed, and the array branch stays an addition over 1.0.0.
- `SugraClient.request` in `sugra_api_mcp/client.py` keeps `json: dict[str, Any] | list[Any] | None`. It is not a tool schema, and the gateway now only hands it what validation admitted.
- No other file pins the old schema: `list[Any]` appeared only in the three gateway lines and `client.py`; `tests/test_tool_arg_schemas.py` checks only the `body` description; the examples pass `inputSchema` through; README, FACTS.md, docs, evals, `server.json` and `glama.json` do not describe the array shape.
- No UI template, no catalog change, no version bump.

## Test evidence

`tests/test_gateway.py`:

- `test_gateway_body_tool_schemas_accept_arrays` now pins on both tools that the array branch's `items` is `{"type": "object", "additionalProperties": true}` and that `anyOf` holds exactly the three branches above, in any order.
- `test_array_body_with_a_non_object_item_is_refused_before_the_tool_runs` drives a connected in-memory client session (`create_connected_server_and_client_session`) against both tools with five bodies: `["x"]`, `[7]`, `[None]`, `[["idType", "TICKER"]]`, and an object followed by `"x"`. Every case is `isError`, names `body`, and records no catalog load and no client call.
- `test_array_body_of_objects_reaches_the_client_over_the_protocol`: an array of objects, with null, array and number values inside the objects, reaches the fake client unchanged through both tools.
- `test_object_and_null_bodies_keep_the_published_contract_over_the_protocol`: an object body with nested scalar arrays reaches the client unchanged, and a null body passes validation, so `call_endpoint` answers `missing: ["body"]` and `fetch_data` answers `needs_params: ["body"]`.
- `test_call_endpoint_accepts_array_body`, `test_fetch_data_passes_array_body_through` and `test_fetch_data_passes_dict_body_through` are unchanged and green.

Local gate: full suite 666 passed; `ruff check sugra_api_mcp tests scripts` clean; `git diff --check` clean; touched files are LF.

Mutation evidence. One mutant per process; `gateway.py` restored from an in-memory byte copy and verified against HEAD after each; the run covers `tests/test_gateway.py`, `tests/test_tool_arg_schemas.py` and `tests/test_tool_error_protocol.py` (71 tests).

- M1, `call_endpoint` body reverted to `list[Any]`: killed, 6 failures (schema pin and the five refusal cases for that tool).
- M2, `fetch_data` body reverted to `list[Any]`: killed, 6 failures (schema pin and the five refusal cases for that tool).
- M3, `call_endpoint` items narrowed to `dict[str, str]`: killed, 2 failures (schema pin, array-of-objects delivery).
- M4, `fetch_data` items widened to objects or strings: killed, 3 failures (schema pin, the string and object-then-string refusals).
- M5, `call_endpoint` array branch removed: killed, 2 failures (schema pin, array-of-objects delivery).
- M6, `fetch_data` object branch narrowed to `dict[str, str]`: killed, 2 failures (schema pin, object and null contract).
- M7, `call_endpoint` items widened to objects or arrays: killed, 2 failures (schema pin, nested-array refusal).
- M8, `_missing_required` body annotation reverted to `list[Any]`: survived, and it is equivalent. The module uses `from __future__ import annotations`, so the helper's annotation is a string that is never evaluated; the helper is private, is not registered as a tool, feeds no schema, and only tests `body is None`. Nothing observes the change short of reading the annotation text.

## Review

Independent review round 1 (head c0af288): APPROVE with no findings, every dimension PASS. The reviewer re-derived the catalog counts (1626 operations, 15 POST, 13 body schemas, 12 required, 11 object, 1 array of objects, 1 optional object or null union, 2 POST without a body schema) and found no declared body shape excluded. It confirmed from the SDK source that argument validation precedes the tool function, ran the three touched test files (71 passed), and probed further shapes: a JSON string holding an array and an array with a boolean item are refused, an empty array passes, and a null body on the optional-body path is delivered. It found no other file pinning the old schema, `client.py` keeping its broader transport type on purpose, and only the three permitted gateway lines changed. No S1 to S4 finding is open.

Records: `ops/reviews/MCP-24.2/REVIEW-*.md`.
