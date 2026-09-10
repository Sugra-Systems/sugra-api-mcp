# CHANGE: MCP-15.4

- change-id:        MCP-15.4
- board-card:       https://github.com/Sugra-Systems/sugra-board/blob/main/cards/MCP-15.4.md
- author-pilot:     grok
- risk-tier:        T1
- tier-justification: New packaged markdown plus resource registration covered by tests. No tool surface change, no auth/contract change. T1.
- required-reviewers: T1: any one of claude / codex / agy (author is grok)

## touched-paths
- sugra_api_mcp/skills/**
- sugra_api_mcp/tools/skills.py
- sugra_api_mcp/tools/__init__.py
- tests/test_skills.py
- tests/test_resources.py
- tests/test_keyless.py
- README.md
- FACTS.md
- pyproject.toml

## intent
Ship the five official Sugra API skills as SKILL.md files (Claude/Codex drop-in)
and register them as MCP resources `sugra://skills/<slug>`. Teach the
search-describe-call loop, envelope/attribution, auth/quota, hosted vs
gateway, and one cross-domain briefing. Do not add per-endpoint tools. Do
not name hosted-only tools in stdio-only skills.

## risk-notes
- resources/list stays public; resources/read stays authed.
- Public copy: no emoji, no em dash, no Tier C commercial names, no real-time.
- PyPI version is not bumped; owner releases.

## revert-command
git revert <squash-merge sha of this PR>

## test-evidence
pytest tests/test_skills.py tests/test_resources.py tests/test_keyless.py
plus the full suite in CI.
