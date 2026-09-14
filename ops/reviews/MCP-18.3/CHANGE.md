# CHANGE: MCP-18.3

- change-id:        MCP-18.3
- board-card:       https://github.com/Sugra-Systems/sugra-board/blob/main/cards/MCP-18.3.md
- risk-tier:        T1
- tier-justification: Hosted tool argument schema completeness. Same required keys and payload shape. Extra keys still ignored. Covered by existing agent-tool tests plus new schema and extra-key pins. No auth or URL change. T1.

## touched-paths
- sugra_api_mcp/tools/agent.py
- tests/test_tool_arg_schemas.py
- tests/test_agent_tools.py

## intent
Anthropic Directory flags `entity` on `get_snapshot` and `get_timeseries` as Parameters missing type because FastMCP emitted `{$ref: #/$defs/AgentEntity}` with no type on the property. Inline an object schema (`type: object`, namespace + ids, additionalProperties true) via WithJsonSchema. Runtime BaseModel extra=ignore so extra keys from resolve_entity do not fail; only namespace + ids go to the plane.

## risk-notes
- OpenAI listing 1.0.0 keeps its snapshot; live calls still accept the same JSON. Extra keys must stay ignore, not forbid.
- Do not bump PyPI in this PR. Hosted deploy follows merge to main.
- Do not add, remove, or rename tools.

## revert-command
git revert <squash-merge sha of this PR>

## test-evidence
pytest tests/test_tool_arg_schemas.py tests/test_agent_tools.py
python -m ruff check sugra_api_mcp tests

Independent reviews of this change (PR 132):
- Gemini 3.8 Flash (High) via the Directory review lane: S4 extra-key coverage on get_timeseries; S1-S3 did not reproduce under pytest/CI.
- Codex exec review vs origin/main (read-only): no findings; inline object schema and extra-key ignore hold.
- Third independent review is recorded on the PR after this extra-key test lands.
