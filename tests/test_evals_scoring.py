"""CI-runnable harness self-tests: golden manifest schema + scorecard math.

The live harnesses (evals/smoke_live.py, evals/agent_eval.py) never run in CI;
these tests pin the parts that can rot silently - manifest integrity (ids,
tool names, contract flags) and the deterministic selection/aggregation logic.
"""

from __future__ import annotations

from evals.scoring import GATEWAY_TOOLS, KNOWN_TOOLS, aggregate, load_manifest, selection_ok

EXPECTED_QUERY_COUNT = 29  # M0 set minus deferred B8


def test_manifest_ids_unique_and_complete():
    manifest = load_manifest()
    ids = [q["id"] for q in manifest]
    assert len(ids) == len(set(ids)), "duplicate golden ids"
    assert len(ids) == EXPECTED_QUERY_COUNT
    assert "B8" not in ids, "B8 was deferred by owner decision 2026-06-05"


def test_manifest_tool_references_are_known():
    for q in load_manifest():
        referenced = list(q["required_tools"])
        for alt in q.get("alt_tools") or []:
            referenced.extend(alt)
        referenced.extend(q.get("forbidden_tools") or [])
        referenced.extend((q.get("min_calls") or {}).keys())
        unknown = set(referenced) - KNOWN_TOOLS
        assert not unknown, f"{q['id']}: unknown tool refs {unknown}"
        assert q["query"].strip(), f"{q['id']}: empty query"
        assert q["required_tools"], f"{q['id']}: no required_tools"


def test_contract_queries_present():
    by_id = {q["id"]: q for q in load_manifest()}
    assert by_id["C5"]["contract"] == "ambiguity"
    assert by_id["E2"]["contract"] == "clean_not_found"
    assert by_id["D2"]["required_tools"] == ["sugra_entity_screen"]
    assert by_id["B1"].get("known_failure") is True


def test_selection_gateway_alias():
    spec = {"required_tools": ["gateway"]}
    for gateway_tool in GATEWAY_TOOLS:
        assert selection_ok(spec, [gateway_tool])
    assert not selection_ok(spec, ["get_snapshot"])


def test_selection_min_calls():
    spec = {"required_tools": ["get_timeseries"], "min_calls": {"get_timeseries": 2}}
    assert not selection_ok(spec, ["get_timeseries"])
    assert selection_ok(spec, ["get_timeseries", "get_timeseries"])


def test_selection_alt_sets_and_forbidden():
    spec = {
        "required_tools": ["get_snapshot"],
        "alt_tools": [["gateway"]],
        "forbidden_tools": ["resolve_entity"],
    }
    assert selection_ok(spec, ["fetch_data"])          # alt set satisfied
    assert selection_ok(spec, ["get_snapshot"])        # primary satisfied
    assert not selection_ok(spec, ["list_sources"])    # neither
    assert not selection_ok(spec, ["get_snapshot", "resolve_entity"])  # forbidden


def test_selection_extra_tools_are_harmless():
    spec = {"required_tools": ["get_snapshot"]}
    assert selection_ok(spec, ["search_endpoints", "resolve_entity", "get_snapshot"])


def test_aggregate_buckets_and_failed_ids():
    results = [
        {"id": "A1", "known_failure": False, "selection_ok": True, "relevance": 2, "error": None},
        {"id": "A2", "known_failure": False, "selection_ok": False, "relevance": 1, "error": None},
        {"id": "B1", "known_failure": True, "selection_ok": False, "relevance": 0, "error": None},
        {"id": "C1", "known_failure": False, "selection_ok": False, "relevance": None,
         "error": "TimeoutError: ..."},
    ]
    summary = aggregate(results)
    assert summary["total"] == 4
    assert summary["errored"] == 1
    assert summary["selection_accuracy_core"] == 0.5      # A1 ok, A2 miss; C1 errored out
    assert summary["selection_accuracy_known_failures"] == 0.0
    assert summary["relevance_mean"] == 1.0
    assert summary["failed_ids"] == ["A2"]


def test_aggregate_empty_is_honest():
    summary = aggregate([])
    assert summary["total"] == 0
    assert summary["selection_accuracy_core"] is None
    assert summary["relevance_mean"] is None
