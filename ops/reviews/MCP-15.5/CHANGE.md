# CHANGE: MCP-15.5

- change-id:        MCP-15.5
- board-card:       https://github.com/Sugra-Systems/sugra-board/blob/main/cards/MCP-15.5.md
- author-pilot:     grok
- risk-tier:        T1
- tier-justification: Version lockstep for PyPI. No tool or auth change. Existing metadata-sync tests cover the three files. T1.
- required-reviewers: T1: any one of claude / codex / agy (author is grok)

## touched-paths
- pyproject.toml
- sugra_api_mcp/__init__.py
- server.json
- FACTS.md

## intent
Cut 0.11.0 so pip gets MCP-15.4 skills and the 1625-op catalog already on main.
Owner pushes tag v0.11.0 after this PR merges. Agent does not push the tag.

## risk-notes
- Tag must be v0.11.0 exactly; publish-pypi.yml rejects mismatch.
- Do not retag v0.10.0.

## revert-command
git revert <squash-merge sha of this PR>

## test-evidence
pytest tests/test_metadata_sync.py
