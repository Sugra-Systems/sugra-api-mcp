"""The gateway can reach only operations that change nothing upstream.

Every gateway tool advertises readOnlyHint and idempotentHint, and
call_endpoint and fetch_data run whatever the bundled catalog holds. So the
builder leaves out the operations in SIDE_EFFECT_OPERATIONS, the parity check
and the resync do not bring them back, and every POST operation in the bundle
is pinned here so a new one is judged by a person before the gateway runs it.
"""

from __future__ import annotations

import copy
import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

from sugra_api_mcp.catalog.builder import SIDE_EFFECT_OPERATIONS, build_catalog_from_openapi
from sugra_api_mcp.catalog.loader import load_catalog
from sugra_api_mcp.catalog.search import search_catalog
from sugra_api_mcp.tools import gateway

REPO_ROOT = Path(__file__).resolve().parents[1]
FIXTURE = Path(__file__).parent / "fixtures" / "openapi_minimal.json"
CHECKER = REPO_ROOT / "scripts" / "check_catalog_parity.py"
BUILDER = REPO_ROOT / "scripts" / "build_endpoint_catalog.py"
RESYNC = REPO_ROOT / "scripts" / "open_catalog_resync.py"

# POST operations the gateway may run. Each only sends a query in a body and
# changes nothing upstream.
PINNED_POST_OPERATIONS: frozenset[str] = frozenset({
    "post_congress_documents_fetch",
    "post_entity_resolve",
    "post_entity_rf_watchlists_screen",
    "post_entity_screen",
    "post_entity_screen_batch",
    "post_indicators_corrmatrix",
    "post_market_screener",
    "post_market_visualization",
    "post_network_bulk_asn",
    "post_network_bulk_ip",
    "post_openfigi_filter",
    "post_openfigi_mapping",
    "post_openfigi_search",
    "post_statistical_agencies_ssb_data_table_id",
})

_EXTRACTION_PATH = "/api/v1/destatis/extractions"


def _spec_with_side_effect_operations() -> dict[str, Any]:
    """The fixture spec plus one POST route per excluded operation."""
    spec = json.loads(FIXTURE.read_text(encoding="utf-8"))
    for index, operation_id in enumerate(sorted(SIDE_EFFECT_OPERATIONS)):
        spec["paths"][f"{_EXTRACTION_PATH}/{index}"] = {
            "post": {
                "tags": ["Statistical Agencies"],
                "summary": "Queue a Destatis extraction that is too large for a direct request",
                "operationId": operation_id,
                "requestBody": {
                    "required": True,
                    "content": {"application/json": {"schema": {
                        "type": "object",
                        "properties": {"code": {"type": "string"}},
                        "required": ["code"],
                    }}},
                },
            }
        }
    return spec


def _write(path: Path, payload: dict[str, Any]) -> Path:
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def test_the_builder_leaves_out_operations_with_side_effects() -> None:
    plain = build_catalog_from_openapi(json.loads(FIXTURE.read_text(encoding="utf-8")))
    built = build_catalog_from_openapi(_spec_with_side_effect_operations())

    assert not built.operation_ids & SIDE_EFFECT_OPERATIONS
    assert built.operation_ids == plain.operation_ids


def test_the_bundled_catalog_holds_no_operation_with_side_effects() -> None:
    catalog = load_catalog()

    assert not catalog.operation_ids & SIDE_EFFECT_OPERATIONS
    assert all(endpoint.path != _EXTRACTION_PATH for endpoint in catalog.endpoints)


def test_post_operations_in_the_bundle_are_pinned() -> None:
    posts = {e.operation_id for e in load_catalog().endpoints if e.method == "POST"}
    added = sorted(posts - PINNED_POST_OPERATIONS)
    removed = sorted(PINNED_POST_OPERATIONS - posts)

    assert not added and not removed, (
        f"The bundle's POST operations changed: added {added}, removed {removed}. "
        "The gateway tools advertise readOnlyHint and idempotentHint, so judge "
        "each added operation by hand. If it only reads (sends a query in a body "
        "and changes nothing upstream), add it to PINNED_POST_OPERATIONS in "
        "tests/test_read_only_catalog.py. If it queues work, stores anything or "
        "changes state, add it to SIDE_EFFECT_OPERATIONS in "
        "sugra_api_mcp/catalog/builder.py and rebuild the bundle. Drop removed "
        "operations from PINNED_POST_OPERATIONS."
    )
    assert not PINNED_POST_OPERATIONS & SIDE_EFFECT_OPERATIONS


class _RecordingClient:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    async def get(self, path: str, **_kwargs: Any) -> dict[str, Any]:
        self.calls.append(("GET", path))
        return {"data": [], "meta": {}}

    async def request(self, method: str, path: str, **_kwargs: Any) -> dict[str, Any]:
        self.calls.append((method, path))
        return {"data": [], "meta": {}}


async def test_call_endpoint_answers_unknown_operation_for_an_excluded_operation(
    monkeypatch,
) -> None:
    client = _RecordingClient()
    monkeypatch.setattr(gateway, "get_client", lambda: client)

    for operation_id in sorted(SIDE_EFFECT_OPERATIONS):
        result = await gateway.call_endpoint(
            operation_id, body={"code": "42131-0004"})
        described = await gateway.describe_endpoint(operation_id)

        assert result == {"error": "unknown_operation_id", "operation_id": operation_id}
        assert described == {"error": "unknown_operation_id", "operation_id": operation_id}
    assert client.calls == []


async def test_fetch_data_cannot_pick_the_extraction_queue(monkeypatch) -> None:
    client = _RecordingClient()
    monkeypatch.setattr(gateway, "get_client", lambda: client)
    query = "Queue a Destatis extraction that is too large for a direct request"

    hits = search_catalog(load_catalog(), query, limit=50)
    await gateway.fetch_data(query, body={"code": "42131-0004"})

    assert not {hit["operation_id"] for hit in hits} & SIDE_EFFECT_OPERATIONS
    assert all(path != _EXTRACTION_PATH for _method, path in client.calls)


def _run_checker(spec: Path, bundle: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(CHECKER), "--spec", str(spec), "--bundle", str(bundle)],
        capture_output=True, text=True, timeout=300,
    )


def test_parity_does_not_report_an_excluded_operation_as_missing(tmp_path) -> None:
    spec = _spec_with_side_effect_operations()
    bundle = build_catalog_from_openapi(spec, source="unit")

    result = _run_checker(_write(tmp_path / "spec.json", spec),
                          _write(tmp_path / "endpoints.json", bundle.to_dict()))

    assert result.returncode == 0, result.stdout
    assert "DRIFT" not in result.stdout


def test_parity_fails_when_the_bundle_carries_an_excluded_operation(tmp_path) -> None:
    spec = _spec_with_side_effect_operations()
    bundle = build_catalog_from_openapi(spec, source="unit").to_dict()
    for index, operation_id in enumerate(sorted(SIDE_EFFECT_OPERATIONS)):
        entry = copy.deepcopy(bundle["endpoints"][0])
        entry.update(operation_id=operation_id, method="POST",
                     path=f"{_EXTRACTION_PATH}/{index}")
        bundle["endpoints"].append(entry)
    bundle["endpoint_count"] = len(bundle["endpoints"])

    result = _run_checker(_write(tmp_path / "spec.json", spec),
                          _write(tmp_path / "endpoints.json", bundle))

    assert result.returncode == 1, result.stdout
    assert "operations with side effects in the bundle" in result.stdout
    assert "not in the spec" not in result.stdout
    for operation_id in SIDE_EFFECT_OPERATIONS:
        assert operation_id in result.stdout


def test_a_resync_rebuild_neither_adds_an_excluded_operation_nor_reports_drift(
    tmp_path,
) -> None:
    spec = _spec_with_side_effect_operations()
    old = build_catalog_from_openapi(spec, source="unit").to_dict()
    output = tmp_path / "endpoints.json"
    # The same command open_catalog_resync.rebuild_bundle runs.
    built = subprocess.run(
        [sys.executable, str(BUILDER), "--source",
         str(_write(tmp_path / "spec.json", spec)), "--output", str(output)],
        capture_output=True, text=True, timeout=300,
    )
    assert built.returncode == 0, built.stdout + built.stderr
    new = json.loads(output.read_text(encoding="utf-8"))

    loader = importlib.util.spec_from_file_location("open_catalog_resync", RESYNC)
    assert loader is not None and loader.loader is not None
    resync = importlib.util.module_from_spec(loader)
    loader.loader.exec_module(resync)

    assert not {e["operation_id"] for e in new["endpoints"]} & SIDE_EFFECT_OPERATIONS
    assert resync.drift_lists(old, new) == ([], [], [])
