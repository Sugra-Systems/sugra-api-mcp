# CHANGE - MCP-19.1.1 (the span wrapper's tests pin that the re-raised CancelledError is the same object with its args)

risk-tier: T1
board: https://github.com/Sugra-Systems/sugra-board/blob/main/cards/MCP-19.1.1.md

## Problem

MCP-19.1 makes the span wrapper in `observability.py` catch `asyncio.CancelledError`, stamp a verdict, and re-raise with a bare `raise` so cancellation propagates unchanged. The tests pinned only the exception class: codex round 3 showed that replacing the bare `raise` with `raise asyncio.CancelledError()` passed all 140 tests while the reason handed to `Task.cancel()` was lost on the way to whoever awaits the task, and nothing checked how an anyio cancel scope, which the MCP SDK wraps every request in, behaves with the wrapper in the middle.

## Change

Tests only, no runtime change.

- `Task.cancel("external-owner")` on a dispatched, wrapped tool: the `CancelledError` delivered inside the tool and the one the awaiter receives are the same object, with `args == ("external-owner",)`.
- The competing case (the budget's timer and an external cancel in the same loop turn): the object that leaves the wrapper is the one that entered it, with the args it carried when it entered (a snapshot taken inside the tool, so an in-place change of the object's args cannot pass), and the capture records that both cancellation requests had reached the task.
- An anyio `CancelScope` around the wrapped tool, cancelled from outside: the object that leaves the wrapper is the one that entered it with its args, the scope reports `cancelled_caught`, and the host task's `cancelling()` count is back at zero at exit, exactly as with the bare tool (parametrized over wrapped and bare).
- The competing arrangement is deterministic, with no sleeps to race: one loop turn puts the dispatch inside the tool, the external cancel is queued with `call_soon`, the budget's timer is rescheduled to now, and in the next turn the cancel runs, then the timer, then the task resumes. The pre-existing competing test from MCP-19.1 uses the same arrangement now.

## Test evidence

Four new cases in `tests/test_observability.py`, and the MCP-19.1 competing test rebuilt on the deterministic arrangement. Mutation evidence, one per run with byte-copy restore, each failing at least one test: a fresh empty `CancelledError`; a fresh one with a made-up reason; a fresh one copying the original args; a fresh copy only when no budget is published (the anyio shape); the args altered in place only in the competing case. Full suite, `ruff check` and `git diff --check` clean.

## Review

Round 1 (codex, head 9a19256): APPROVE_WITH_CHANGES, three S3, all accepted and closed in the same PR because they are the card's own deliverable.

- The anyio case verified scope cleanup but not identity or args. Closed: the exception is captured inside the tool and again just outside the wrapper inside the scope, and compared by identity and against the args snapshot.
- The competing args comparison read the same object's attribute on both sides. Closed by the snapshot taken at capture.
- The competing arrangement depended on waking within a 20 ms window. Closed by the sleep-free arrangement above; the capture asserts two cancellation requests and an expired budget.

Records: `ops/reviews/MCP-19.1.1/REVIEW-codex-*.md`.

## Acceptance

The identity and args assertions exist for `Task.cancel` and anyio cancellation, including the competing-budget case, and the named mutant fails.
