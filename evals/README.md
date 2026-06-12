# Evals - hosted MCP agent surface (MCP-2.4)

Two on-demand harnesses against the LIVE hosted MCP (`app.sugra.ai/mcp`).
Neither runs in CI: they need the live API, secrets, and (for the golden eval)
an LLM. The CI-runnable part is `tests/test_evals_scoring.py` (manifest schema
+ scorecard math).

## Environment

| Variable | Required | Purpose |
|---|---|---|
| `SUGRA_TEST_API_KEY` | yes | Internal-Testing API key, sent as Bearer to the hosted MCP (raw `sugra_` keys are accepted) |
| `ANTHROPIC_API_KEY` | golden eval only | The agent under test + the relevance judge |
| `EVAL_MODEL` | no | Agent model (default `claude-opus-4-8`) |
| `EVAL_JUDGE_MODEL` | no | Judge model (default = `EVAL_MODEL`) |
| `SUGRA_MCP_URL` | no | Default `https://app.sugra.ai/mcp` |
| `SUGRA_EVAL_CALL_TIMEOUT` | no | HTTP timeout seconds for the MCP connection (default 60). NOT a complete hard bound on a tool-result wait - the SDK's SSE read timeout is separate; the agent eval adds a 600 s outer wait_for per query |

The golden eval needs the `[mcp]` extra: `pip install "anthropic[mcp]"`.

## Smoke set (deterministic)

```bash
python -m evals.smoke_live
```

Seven checks: unauthenticated tools/call 401 (tools/list is public discovery
by design), tools/list == 11, weighted cost decrements
quota by the recipe cost, max_points bounded + honest downsampled flag, META
ambiguity contract (ranked candidates, no silent pick), garbage-resolve clean
not-found, freshness-block honesty. Exit 0 = green. Required-fail -> 502 /
optional-fail -> partial are not injectable against prod and stay pinned by
the API unit tests (prod-sugra-ai-API `tests/test_agent_compose.py`).

## Golden-query eval (agent-driven scorecard)

```bash
python -m evals.agent_eval            # all 29 queries
python -m evals.agent_eval --ids C5,E2
```

Every M0 golden query (executable manifest: `golden_queries.json`; canonical
prose: sugra-internal-docs `AGENT_GOLDEN_QUERIES.md`) is answered by a real
agent whose only capabilities are the 11 hosted tools. Tool-selection accuracy
is scored deterministically against the manifest's expected route; answer
relevance is LLM-judged 0-2. Results land in `evals/results/` as JSON + a
markdown scorecard.

This is a SCORECARD, not a pass/fail gate. `known_failure` queries (B1: agents
historically pick the UK CPI series; B6: plane macro loader is FRED-only) are
tracked in a separate bucket - they inform fixes, they never fail a run.

Denominators: queries that ERROR (timeout, transport) are excluded from the
selection-accuracy and relevance denominators and reported in the separate
`errored` count - an accuracy figure never silently absorbs infrastructure
failures.

Quota note: a full run consumes roughly 60-100 request units of the test key's
daily quota (composed recipes cost 1-2 units per call) plus Anthropic tokens
for ~29 agent loops and 29 judge calls.
