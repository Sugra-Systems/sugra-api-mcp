# CHANGE: MCP-15.3

- change-id:        MCP-15.3
- board-card:       https://github.com/Sugra-Systems/sugra-board/blob/main/cards/MCP-15.3.md
- author-pilot:     grok
- risk-tier:        T2
- tier-justification: Catalog continuity is orchestration across two repos (parity job opens a PR; API deploy dispatches api-spec-changed). The bundled catalog is the discovery contract agents search. T2.
- required-reviewers: T2: any two of claude / codex / agy (author is grok)

## touched-paths
- sugra_api_mcp/catalog/data/endpoints.json
- FACTS.md
- scripts/open_catalog_resync.py
- scripts/build_endpoint_catalog.py (unchanged; called by the resync script)
- .github/workflows/catalog-parity.yml
- tests/test_open_catalog_resync.py
- ops/reviews/MCP-15.3/CHANGE.md

## intent
Rebuild the bundled OpenAPI catalog from the live spec so main matches production GET/POST operations, and teach catalog-parity.yml to open or update a single resync PR on DRIFT (branch ci/catalog-resync, author Arman Obosyan) instead of only going red. test.yml stays a fail-on-drift gate. The auto-PR must not close this card.

Companion PR in prod-sugra-ai-API dispatches api-spec-changed after a healthy production swap.

## risk-notes
- GITHUB_TOKEN must not author the resync PR: bot pushes do not trigger workflows, so Test would never run. MCP_CATALOG_TOKEN is required; without it the scheduled job stays red on DRIFT and no PR opens.
- The resync PR body must never carry `Closes board item`. A catalog bump is not MCP-15.3.
- force-with-lease from origin/main: the branch is always main plus the latest rebuild. A human commit on ci/catalog-resync can be overwritten if they push between fetch and push; the lease refuses only a push that landed after our fetch.
- test.yml on pull_request still fails on drift. Unrelated PRs stay blocked until the resync PR merges. That is the existing gate, not a regression.
- PyPI 0.10.0 is not retagged here. Owner release.

## revert-command
git revert <squash-merge sha of this PR>

## test-evidence
tests/test_open_catalog_resync.py - FACTS stamp, drift lists, PR body contract (no board close, no pilot name), token refusal.
tests/test_catalog_provenance.py - existing parity checker still fails on a missing operation.
Local: python scripts/open_catalog_resync.py --no-git against https://sugra.ai/openapi.json, then python scripts/check_catalog_parity.py.
