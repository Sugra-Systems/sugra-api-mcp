# CHANGE - MCP-19.1 (a budget-cancelled tool call leaves a span with no success and no error code)

risk-tier: T1
board: https://github.com/Sugra-Systems/sugra-board/blob/main/cards/MCP-19.1.md

## Problem

`server.py` bounds every dispatch with `asyncio.timeout`, which cancels the running task. `CancelledError` is a `BaseException`, so the span wrapper's `except Exception` never saw it: the span ended through `finally` with only the tool name and no `mcp.success`, no code, no status. Over the 90 days to 2026-09-11 that was 66 spans with no verdict at all, and `deadline_exceeded`, an allowlisted code since MCP-10, never once appeared on a span. The one failure that fires when the API is slowest was invisible to every failure query (`where not(success)` skips a span that has no success).

## Change

- `observability.py`: the wrapper catches `asyncio.CancelledError` separately, stamps `mcp.success = False`, an error code, the duration and status ERROR, and re-raises unchanged so cancellation semantics are untouched. The code is `deadline_exceeded` when the dispatch budget published in `budget_deadline_at` has elapsed at the moment of cancellation, else `cancelled` (the client went away, the session shut down). `cancelled` joins the allowlist.
- `server.py`: `call_tool` publishes the budget's absolute loop-time deadline in that `ContextVar` for the duration of one dispatch and resets it after. It is set a hair before `asyncio.timeout` computes its own instant, so at cancellation the loop time is past both. Set and reset on one task around one dispatch: it cannot inherit across calls the way the MCP-17 stamp did.

## Not changed

The client still receives the same `deadline_exceeded` envelope from `call_tool`; the budget argument handed to `asyncio.timeout` is unchanged and the test that captures it still passes. No new dimension; two codes on an existing one.

## Privacy

The two new values are constants chosen by a clock comparison. Nothing from the call reaches the span. The pinned attribute-name and type assertions from MCP-19 apply to both new paths.

## Test evidence

Three new tests in `tests/test_observability.py`: a call cancelled by a published budget records `deadline_exceeded` with the operation, duration, ERROR status and an ended span; a cancellation with no budget published, or well before it, records `cancelled` and re-raises; both codes are allowlisted. One in `tests/test_tool_deadline.py` over a real in-memory MCP session: the client gets the `deadline_exceeded` envelope and the `call_endpoint` span says the same. Each failed before the change. Full suite, `ruff check` and `git diff --check` clean.

## Acceptance

A cancelled tool call produces a span with `mcp.success=False` and `mcp.error.code` in `{deadline_exceeded, cancelled}`; zero spans without `mcp.success` over a 7-day window after the deploy.
