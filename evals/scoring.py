"""Scorecard math for the golden-query eval - pure functions, CI-testable.

Selection scoring is code (deterministic); answer relevance is the LLM judge's
job in agent_eval.py. The harness is a SCORECARD, not a pass/fail gate:
known_failure queries are reported in their own bucket and never fail a run.
"""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path
from typing import Any

# The classic catalog surface. A golden query whose route is "gateway" is
# satisfied by ANY of these (the agent may search first, then call).
GATEWAY_TOOLS = frozenset({"search_endpoints", "describe_endpoint", "call_endpoint", "fetch_data"})

# Every tool name a manifest entry may reference (11 hosted tools + alias).
KNOWN_TOOLS = GATEWAY_TOOLS | {
    "list_toolsets", "list_sources",
    "sugra_entity_screen", "sugra_entity_lookup",
    "resolve_entity", "get_snapshot", "get_timeseries",
    "gateway",
}


def load_manifest(path: str | Path | None = None) -> list[dict[str, Any]]:
    manifest_path = Path(path) if path else Path(__file__).parent / "golden_queries.json"
    data = json.loads(manifest_path.read_text(encoding="utf-8"))
    return data["queries"]


def _tool_satisfied(tool: str, called: Counter) -> bool:
    if tool == "gateway":
        return any(called[g] for g in GATEWAY_TOOLS)
    return called[tool] > 0


def _set_satisfied(tools: list[str], spec: dict[str, Any], called: Counter) -> bool:
    if not all(_tool_satisfied(t, called) for t in tools):
        return False
    for tool, minimum in (spec.get("min_calls") or {}).items():
        if tool in tools and called[tool] < minimum:
            return False
    return True


def selection_ok(spec: dict[str, Any], called_tools: list[str]) -> bool:
    """True when the agent's tool calls satisfy the query's expected route.

    Satisfied = (required_tools set, with min_calls) OR any alt_tools set,
    AND no forbidden tool was called.
    """
    called = Counter(called_tools)
    for tool in spec.get("forbidden_tools") or []:
        if _tool_satisfied(tool, called):
            return False
    if _set_satisfied(spec["required_tools"], spec, called):
        return True
    return any(_set_satisfied(alt, spec, called) for alt in spec.get("alt_tools") or [])


def aggregate(results: list[dict[str, Any]]) -> dict[str, Any]:
    """Scorecard rollup. Each result: {id, known_failure, selection_ok,
    relevance (0-2 or None), error (str or None)}."""
    scored = [r for r in results if not r.get("error")]
    core = [r for r in scored if not r.get("known_failure")]
    known = [r for r in scored if r.get("known_failure")]
    relevance_values = [r["relevance"] for r in scored if r.get("relevance") is not None]
    return {
        "total": len(results),
        "errored": len(results) - len(scored),
        "selection_accuracy_core": (
            round(sum(r["selection_ok"] for r in core) / len(core), 3) if core else None
        ),
        "selection_accuracy_known_failures": (
            round(sum(r["selection_ok"] for r in known) / len(known), 3) if known else None
        ),
        "relevance_mean": (
            round(sum(relevance_values) / len(relevance_values), 3) if relevance_values else None
        ),
        "failed_ids": sorted(
            r["id"] for r in core if not r["selection_ok"]
        ),
    }
