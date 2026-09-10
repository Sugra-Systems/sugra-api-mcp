---
name: envelope-attribution
description: Parse Sugra API payloads, keep source attribution, and tell observation time from request time. Use when reading a call_endpoint or fetch_data result, citing a figure, or shaping a large payload.
---

# Envelope and attribution

## Payload shapes

Most Sugra API responses are `{data, meta}`. Shape `limit` and `fields` on `call_endpoint` / `fetch_data` apply to `data`. Some payloads are envelope-less (a flat dict with `_meta` or `meta` on the same object). Provenance keys survive field projection.

`meta.shaped` reports what shaping actually did (`fields_applied`, `fields_unmatched`, `limit_applied`). It is not an echo of the request. `limit` bounds only the top-level list (`data` or a bare array), never lists nested inside records.

## Time

When present, `meta.data_time` is the observation or publication clock of the data, not the HTTP response time. A row-level `as_of` is the period the figure is about. Quote both when they differ. Do not describe a delayed series as a live tick.

## Attribution

Every figure needs a source and an as-of. Read them from `meta` / `_meta` and from the skill resource `sugra://attribution`.

- Sovereign, intergovernmental, and academic sources are named openly (for example FRED, IMF, ECB, NOAA, World Bank, SEC EDGAR).
- Commercial upstreams appear under Sugra-branded wrappers (Sugra Finance, Sugra News, Sugra Crypto, Sugra Forex, Sugra Weather). Do not substitute a commercial vendor name.
- The full source list is https://sugra.ai/sources.

This is data presentation, not investment, legal, or compliance advice. Screening tools return a signal, not a determination.
