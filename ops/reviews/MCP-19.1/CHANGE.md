# CHANGE - MCP-19.1 (a budget-cancelled tool call leaves a span with no success and no error code)

risk-tier: T1
board: https://github.com/Sugra-Systems/sugra-board/blob/main/cards/MCP-19.1.md

## Problem

`server.py` bounds every dispatch with `asyncio.timeout`, which cancels the running task. `CancelledError` is a `BaseException`, so the span wrapper's `except Exception` never saw it: the span ended through `finally` with only the tool name and no `mcp.success`, no code, no status. Over the 90 days to 2026-09-11 that was 66 spans with no verdict at all, and `deadline_exceeded`, an allowlisted code since MCP-10, never once appeared on a span. The one failure that fires when the API is slowest was invisible to every failure query (`where not(success)` skips a span that has no success).

## Change

- `observability.py`: the wrapper catches `asyncio.CancelledError` separately, stamps `mcp.success = False`, an error code, the duration and status ERROR, and re-raises unchanged so cancellation semantics are untouched. The code is `deadline_exceeded` when the `asyncio.Timeout` published in `dispatch_timeout` reports that it fired (`expired()`), else `cancelled` (the client went away, the session shut down). Attribution comes from the timeout's own state, never from a clock: a clock comparison misfiles in both directions (see Review). `cancelled` joins the allowlist.
- `server.py`: `call_tool` publishes the `asyncio.Timeout` that bounds the dispatch in that `ContextVar` for the duration of one dispatch and resets it after. Set and reset on one task around one dispatch: it cannot inherit across calls the way the MCP-17 stamp did. Reached through the module attribute rather than a from-import, so the wrapper and `call_tool` read the same object even after a module reload (the test suite's autouse reload bound two different `ContextVar` objects under a from-import and the integration test failed only in the full run).

## Not changed

The client still receives the same `deadline_exceeded` envelope from `call_tool`; the budget argument handed to `asyncio.timeout` is unchanged and the test that captures it still passes. No new dimension; two codes on an existing one.

## Privacy

The two new values are constants chosen from the timeout's state. Nothing from the call reaches the span. The pinned attribute-name and type assertions from MCP-19 apply to both new paths.

## Test evidence

In `tests/test_observability.py`: a call cancelled by the published timeout records `deadline_exceeded` with the operation, duration, ERROR status and an ended span; the same under a coarse loop clock (resolution forced to 50 ms, so the timer fires before the clock reaches the deadline); a cancellation with no timeout published, or under one that has not fired, records `cancelled` with duration and ERROR status and re-raises; an external cancel that lands after the deadline passed but before the timer ran (the loop blocked) records `cancelled`; both codes are allowlisted. In `tests/test_tool_deadline.py`: over a real in-memory MCP session the client gets the `deadline_exceeded` envelope and the `call_endpoint` span says the same; and the published timeout is back to unset in the dispatch's own context after a success, a tool error, a budget timeout and an external cancel. Full suite, `ruff check` and `git diff --check` clean.

Mutation evidence (one mutant per run, byte-copy restore), each failing at least one test: the cancel clause removed; every cancellation called the budget; every cancellation called `cancelled`; the old clock comparison restored; the server not publishing the timeout; the server not resetting it; the cancellation swallowed; no ERROR status on the cancel path; no duration on the cancel path.

## Review

Round 1 (codex, head d185171): BLOCK, two findings, both accepted.

- S2 correctness: attribution by comparing `loop.time()` with a published deadline misfiles in both directions - an external cancel landing after the deadline but before the timer ran read as the budget, and a coarse loop clock (the loop runs a timer up to one clock resolution early, 15.6 ms on Windows) fired the budget before the clock reached the deadline and read as the caller. Closed by publishing the `asyncio.Timeout` itself and asking `expired()`; both reproductions are now regression tests.
- S3 maintainability: dropping the `ContextVar` reset, the ERROR status or the duration on the cancel path survived every test. Closed by asserting all three on the external-cancel path and by a test that the timeout is unset again after each of four dispatch outcomes in the dispatch's own context.

Records: `ops/reviews/MCP-19.1/REVIEW-codex-*.md`.

## Acceptance

A cancelled tool call produces a span with `mcp.success=False` and `mcp.error.code` in `{deadline_exceeded, cancelled}`; zero spans without `mcp.success` over a 7-day window after the deploy.
