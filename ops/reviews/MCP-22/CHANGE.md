# CHANGE - MCP-22 (limit and fields reach the record list inside data, and never empty a response)

risk-tier: T1
board: https://github.com/Sugra-Systems/sugra-board/blob/main/cards/MCP-22.md

## Problem

`call_endpoint` and `fetch_data` shape results with `limit` and `fields` in `sugra_api_mcp/catalog/response.py`. Shaping knew two places for records: a list at `data` and a bare top-level array. Many Sugra API responses put an object at `data` and the records inside it. Checked live through the hosted server on 2026-09-12:

- `call_endpoint news_latest` with `limit=2` and `fields=["title"]` returned `data: {}`, with `meta.shaped` reporting `fields_unmatched ["title"]` and `limit_applied false`. The API sends `data: {total, count, items: [...]}` there.
- The same call with `limit=2` alone returned all 50 items and `limit_applied false`.
- A research pass the same day saw `fred_series_series_id` with `fields [date, value]` return `data: {}`.

The empty object came from projecting `data` itself as one record: no requested field was one of its keys, so every key was dropped, and nothing marked the result as failed.

## Change

Records list. When `data` is an object holding exactly one allowlisted key whose value is a list, that list is the records list. No such key, or two or more, means there is no records list: shaping never picks one of two lists.

The allowlist is `data`, `entries`, `events`, `history`, `items`, `observations`, `points`, `records`, `results`, `rows`, `series`, `timeseries`. It comes from a read-only census of prod-sugra-ai-API at a9837b73:

- The committed OpenAPI document (`static/openapi.json`, response models typed from live samples): 1556 operations, 1461 with a `data` property, 1362 of those an object. Array properties inside an object `data`, by name: `observations` 161, `rows` 58, `items` 31, `series` 29, `records` 29, `data` 24, `timeseries` 24, `events` 21, `results` 20, `entries` 13, `history` 8, `points` 4. Every typed occurrence holds objects (one `records` is untyped). With this allowlist 422 operations get exactly one records list and none carries two.
- The stored response samples (`scripts/samples`, 387 parsed, 321 with an object data): 72 get exactly one records list, none carries two, and every allowlisted list in them holds objects.
- `news_latest` returns `NewsLatestData`: `total`, `count` and `items`; the stored sample has 50 items keyed title, description, link, published, source, source_name, region, category.
- `fred_series_series_id` declares `FredSeriesSeriesIdData`: series metadata, `count`, `license` and `observations`, a list of `FredObservation` with `date` and `value`. That route's committed spec entry carries an empty schema, so the model is the evidence for it.
- Left out on purpose: `time_period` (108 indicator operations) is a list of integers, the look-back periods echoed beside `series_by_period`, so bounding it would cut configuration rather than records; `values` occurs once; entity names such as `countries`, `tags` and `exchanges` also hold lists of strings in the samples. A payload whose records sit under a name missing here keeps its data whole, and `meta.shaped` says so.

limit. Unchanged for a list `data` and a bare top-level array. For an object `data` with a records list, that list is bounded and `limit_applied` is true; keys beside it, such as `total` and `count`, stay exactly as the API sent them. Otherwise the limit is not applied.

fields on an object `data`. If a requested field names one of the object's own keys (literal or dotted path), the object is projected exactly as before, so a single-record payload such as a quote keeps its behaviour. Otherwise, if a records list exists, each record is projected and the object's other keys stay. Otherwise data is left unchanged.

Never empty. When no requested field matches anything, the target comes back unprojected and every field is reported unmatched, for list records, object data and envelope-less payloads alike. A partial match keeps the earlier behaviour. Envelope-less payloads otherwise shape as before, and `meta` and `_meta` still survive projection.

`meta.shaped` gains `records_path`: `"data"` when data is a list (a bare top-level array is wrapped as data), `"data.<key>"` when limit bounded the records list inside data or fields were matched against its records, and null otherwise (an object data projected by its own keys with no limit, a data without a records list, a scalar, an envelope-less payload). The existing keys keep their meaning.

The `limit` and `fields` descriptions on `call_endpoint` and `fetch_data` state this rule: the records list is the data list, a bare top-level array, or the one record list inside data such as data.items; keys beside it are not rewritten; `meta.shaped` reports what applied and where.

## Not changed

- No tool is added, removed or renamed, and parameter names, types and defaults are unchanged. The published 1.0.0 directory listing advertises no UI template and no descriptions on limit and fields; nothing it advertises is narrowed. Only results and the two descriptions change.
- The body annotations of both tools are not touched.
- Envelope-less payloads (no `data` key) get no records-list detection, and limit still does not apply to them.
- Error payloads are still returned untouched before shaping.
- `README.md` (the response shaping paragraph) and `sugra_api_mcp/skills/envelope-attribution/SKILL.md` still say limit bounds only the data list or a bare array. They are outside this change's file scope and are left for a follow-up.

## Test evidence

New cases in `tests/test_catalog.py`: a news_latest-shaped envelope with limit only, fields only, both (with one unmatched field), and fields that match nothing; own keys of data winning over records; a FRED-shaped envelope built from `FredSeriesSeriesIdData`, with fields [date, value] and with limit plus fields; a single-record quote whose fields match its own keys (no regression, limit not applied); an object data with no matching field left whole; two allowlisted list keys (no records list, limit not applied, data never emptied); list data and a bare array with no matching field (records kept, all unmatched); an envelope-less payload with no matching field left whole; every allowlisted key bounding and projecting; `time_period` not treated as records; a non-list value under an allowlisted key not counted; `records_path` for list, bare array and scalar shapes. `tests/test_group_precheck_and_telemetry.py` now also requires both tools' limit and fields descriptions to name `data.items` and `records_path`.

No existing test pinned the old empty-dict behaviour, so none was rewritten; every earlier shaping test passes unchanged.

Mutation evidence: a harness outside the repo applies one mutant per run to `response.py`, runs `tests/test_catalog.py`, `tests/test_group_precheck_and_telemetry.py` and `tests/test_gateway.py`, and restores the file from an in-memory byte copy verified by sha256. 17 mutants, 17 killed, none survived: records list ignored for limit; records list ignored for fields; the never-empty rule removed; the ambiguity check removed (first list wins); top-level-key precedence removed (records tried first); any list treated as records; the list check on allowlisted keys removed; sibling keys dropped; limit_applied false on the records list; records_path dropped, once in the data shaper and once in the meta block; records_path set on an own-key match; limit_applied true on an object without records; the never-empty rule removed separately for object data, list data and envelope-less payloads; `observations` dropped from the allowlist.

Full suite 667 passed; `ruff check sugra_api_mcp tests scripts` and `git diff --check` clean.

## Review

Independent review round 1: pending.
