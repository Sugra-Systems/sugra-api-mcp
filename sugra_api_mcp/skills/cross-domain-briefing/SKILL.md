---
name: cross-domain-briefing
description: Compose one briefing from two or three Sugra domains using only the eight gateway tools. Use when a question spans maritime, weather, macro, markets, or government and a single prompt recipe is not enough.
---

# Cross-domain briefing

Stay on the eight gateway tools so this works on stdio and hosted alike. Do not reach for hosted-only names.

## Pattern

1. Split the question into 2-3 concrete asks (place, series, snapshot).
2. For each ask, `search_endpoints` then `describe_endpoint` then `call_endpoint`. Prefer sovereign or intergovernmental sources when the catalog offers them.
3. Keep units, geography, and clocks separate. Do not blend a port throughput z-score with a weather reading into one invented index.
4. Quote each figure with its source from `meta` and its `as_of` / `meta.data_time`.
5. Close with what the catalog did not cover, not a prediction.

## Example shape (not a canned operation_id list)

"What is happening around a chokepoint this week?" can be three catalog calls: port throughput deviation for the waterway's ports, current conditions at a coordinate on the route, and one related sovereign or intergovernmental series (for example a commodity or traffic indicator the catalog actually returns). Search for each; describe before calling; present side by side.

The `earth_conditions` prompt is a one-coordinate weather recipe. This skill is the longer form when weather is only one pane.

## Do not

- Do not add per-endpoint tools to make the briefing shorter.
- Do not treat screening or entity tools as a briefing source unless the question is about a named party.
- Do not frame the output as investment, legal, or routing advice.
