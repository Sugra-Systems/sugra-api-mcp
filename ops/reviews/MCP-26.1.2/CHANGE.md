# CHANGE - MCP-26.1.2 (the query_too_long hint says letters where the bound counts ASCII, and a str-subclass error code reaches the span unconverted)

risk-tier: T1
board: https://github.com/Sugra-Systems/sugra-board/blob/main/cards/MCP-26.1.2.md

## Problem

The search bound tokenizes with `[a-z0-9]+` after lowercasing, so a term is an ASCII run. The `query_too_long` hint and the README said "letters or digits", which an agent writing in another script would read as covering its words. Separately, `_error_code_of` returned the `error` object itself when it was an allowlisted str, so a str subclass would have reached the span. The `_BUSY_SCOPES` comment also said a per-caller share is one API key; a caller can be `http:anonymous` or `local`.

## Change

No change to the bounds or to which codes are attached.

- Hint and README: "runs of two or more ASCII letters or digits".
- `_error_code_of` returns the interned constant from `_KNOWN_ERROR_CODES`, never the caller's object.
- `_BUSY_SCOPES` comment names digest, `http:anonymous` and `local`.

## Test evidence

Non-ASCII words past the term count are not refused (no ASCII terms). An accented word splits on the non-ASCII letter. A `_Code("query_too_long")` str subclass is attached as an exact str, not the subclass instance. README contains the ASCII wording.
