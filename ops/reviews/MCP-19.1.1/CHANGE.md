# CHANGE - MCP-19.1.1 (the span wrapper's tests pin that the re-raised CancelledError is the same object with its args)

risk-tier: T1
board: https://github.com/Sugra-Systems/sugra-board/blob/main/cards/MCP-19.1.1.md

## Problem

MCP-19.1 makes the span wrapper in `observability.py` catch `asyncio.CancelledError`, stamp a verdict, and re-raise with a bare `raise` so cancellation propagates unchanged. The tests pinned only the exception class: codex round 3 showed that replacing the bare `raise` with `raise asyncio.CancelledError()` passed all 140 tests while the reason handed to `Task.cancel()` was lost on the way to whoever awaits the task, and nothing checked how an anyio cancel scope, which the MCP SDK wraps every request in, behaves with the wrapper in the middle.

## Change

Tests only, no runtime change.

- `Task.cancel("external-owner")` on a dispatched, wrapped tool: the `CancelledError` delivered inside the tool and the one the awaiter receives are the same object, with `args == ("external-owner",)`.
- The competing case (the budget's timer and an external cancel in the same loop turn): the object that leaves the wrapper is the one that entered it, with the same args.
- An anyio `CancelScope` around the wrapped tool, cancelled from outside: the scope reports `cancelled_caught` and the host task's `cancelling()` count is back at zero at exit, exactly as with the bare tool (parametrized over wrapped and bare).

## Test evidence

Four new cases in `tests/test_observability.py`. Mutation evidence, one per run with byte-copy restore: a fresh empty `CancelledError`, a fresh one with a made-up reason, and a fresh one copying the original args each fail at least one test. Full suite, `ruff check` and `git diff --check` clean.

## Acceptance

The identity and args assertions exist for `Task.cancel` and anyio cancellation, including the competing-budget case, and the named mutant fails.
