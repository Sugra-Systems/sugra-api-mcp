# CHANGE: MCP-15.7

- change-id:        MCP-15.7
- board-card:       https://github.com/Sugra-Systems/sugra-board/blob/main/cards/MCP-15.7.md
- author-pilot:     grok
- risk-tier:        T1
- tier-justification: Marketplace manifests and README install paths, covered by tests. No tool surface, auth, or SKILL.md text change. T1.
- required-reviewers: T1: any one of claude / codex / agy (author is grok)

## touched-paths
- .claude-plugin/marketplace.json
- .grok-plugin/marketplace.json
- sugra_api_mcp/skills/.claude-plugin/plugin.json
- tests/test_plugin_marketplace.py
- README.md
- FACTS.md

## intent
Publish the existing five SKILL.md files as a Claude Code and Grok plugin
marketplace from this public repo. Codex and Cursor get copy/installer
commands in the README. ChatGPT stays MCP connector only. Do not duplicate
skill text. Do not add per-endpoint tools. Do not add a plugin MCP server.

## risk-notes
- Plugin root is sugra_api_mcp/skills/ with plugin.json skills set to ./ so
  clients do not look for a nested skills/ directory.
- Public copy: no emoji, no em dash, no Tier C commercial names, no real-time.
- Public repo: no agent names on the branch, no tier labels on the PR.
- PyPI version is not bumped; owner releases if a wheel cut is wanted later.

## revert-command
git revert <squash-merge sha of this PR>

## test-evidence
pytest tests/test_plugin_marketplace.py tests/test_skills.py
plus the full suite in CI.
