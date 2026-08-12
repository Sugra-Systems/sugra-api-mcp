"""The bundled catalog must carry machine-readable provenance, and drift from
the API spec must be detectable rather than silent.

The catalog is a build artifact of the API's OpenAPI document, but nothing
rebuilds it when routes land and its `source` was a free-text label - so a stale
bundle could only be spotted by reading it. That is how a deployed surface served
a catalog several releases behind while searches for the newer operations
returned nothing. These tests pin the provenance stamp and prove the parity
checker actually fails on an operation-set difference.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from pathlib import Path

import pytest

from sugra_api_mcp.catalog.builder import build_catalog_from_openapi
from sugra_api_mcp.catalog.loader import load_catalog
from sugra_api_mcp.catalog.models import Catalog

REPO_ROOT = Path(__file__).resolve().parents[1]
BUNDLE = REPO_ROOT / "sugra_api_mcp" / "catalog" / "data" / "endpoints.json"
FIXTURE = Path(__file__).parent / "fixtures" / "openapi_minimal.json"
CHECKER = REPO_ROOT / "scripts" / "check_catalog_parity.py"


def _fixture_spec() -> dict:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def test_bundled_catalog_carries_machine_readable_provenance() -> None:
    """A free-text source label cannot be checked by a machine; the sha and
    build time can."""
    data = json.loads(BUNDLE.read_text(encoding="utf-8"))

    assert data.get("spec_sha256"), "bundle has no spec_sha256 - staleness is undetectable"
    assert len(data["spec_sha256"]) == 64, f"not a sha256: {data['spec_sha256']!r}"
    assert data.get("built_at"), "bundle has no built_at"
    assert data["built_at"].endswith("Z"), f"built_at must be UTC ISO: {data['built_at']!r}"
    # the count is part of the contract, not a derived afterthought
    assert data["endpoint_count"] == len(data["endpoints"])


def test_loaded_catalog_exposes_the_provenance() -> None:
    catalog = load_catalog()

    assert catalog.spec_sha256 and catalog.built_at
    assert catalog.endpoint_count == len(catalog.endpoints)
    assert catalog.operation_ids <= {e.operation_id for e in catalog.endpoints}


def test_provenance_round_trips_through_to_dict_and_from_dict() -> None:
    built = build_catalog_from_openapi(
        _fixture_spec(), source="unit", spec_sha256="a" * 64, built_at="2026-01-01T00:00:00Z"
    )

    restored = Catalog.from_dict(built.to_dict())

    assert restored.spec_sha256 == "a" * 64
    assert restored.built_at == "2026-01-01T00:00:00Z"
    assert restored.source == "unit"
    assert restored.operation_ids == built.operation_ids


def test_provenance_is_omitted_not_nulled_when_absent() -> None:
    """An older bundle without the stamp must round-trip byte-identically, so
    adding the fields cannot churn an unrelated diff."""
    built = build_catalog_from_openapi(_fixture_spec(), source="unit")

    payload = built.to_dict()

    assert "spec_sha256" not in payload
    assert "built_at" not in payload
    assert Catalog.from_dict(payload).spec_sha256 is None


def _run_checker(spec: Path, bundle: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(CHECKER), "--spec", str(spec), "--bundle", str(bundle)],
        capture_output=True, text=True, timeout=300,
    )


def test_parity_checker_fails_when_an_operation_is_missing_from_the_bundle(tmp_path) -> None:
    """The real regression: a route lands in the API and the bundle is not
    rebuilt. The checker must fail and name the missing operation."""
    spec = _fixture_spec()
    bundle_catalog = build_catalog_from_openapi(spec, source="unit")
    # bundle built BEFORE a new operation landed in the spec
    dropped = sorted(bundle_catalog.operation_ids)[0]
    trimmed = {
        "source": "unit",
        "endpoint_count": bundle_catalog.endpoint_count - 1,
        "endpoints": [e.to_dict() for e in bundle_catalog.endpoints if e.operation_id != dropped],
    }
    bundle_path = tmp_path / "endpoints.json"
    bundle_path.write_text(json.dumps(trimmed), encoding="utf-8")
    spec_path = tmp_path / "spec.json"
    spec_path.write_text(json.dumps(spec), encoding="utf-8")

    result = _run_checker(spec_path, bundle_path)

    assert result.returncode == 1, result.stdout
    assert "DRIFT" in result.stdout
    assert dropped in result.stdout, "the checker must name what is missing"
    assert "build_endpoint_catalog.py" in result.stdout, "must say how to fix it"


def test_parity_checker_passes_when_the_bundle_matches(tmp_path) -> None:
    spec = _fixture_spec()
    catalog = build_catalog_from_openapi(spec, source="unit")
    bundle_path = tmp_path / "endpoints.json"
    bundle_path.write_text(json.dumps(catalog.to_dict()), encoding="utf-8")
    spec_path = tmp_path / "spec.json"
    spec_path.write_text(json.dumps(spec), encoding="utf-8")

    result = _run_checker(spec_path, bundle_path)

    assert result.returncode == 0, result.stdout
    assert "DRIFT" not in result.stdout


def test_parity_checker_fails_when_the_local_spec_path_is_wrong(tmp_path) -> None:
    """A missing local path is a DETERMINISTIC configuration error, not an
    outage. Skipping it would let a typo keep the gate green forever while the
    catalog rots - a gate that cannot check is not a passing gate."""
    bundle_path = tmp_path / "endpoints.json"
    bundle_path.write_text(
        json.dumps(build_catalog_from_openapi(_fixture_spec(), source="unit").to_dict()),
        encoding="utf-8",
    )

    result = _run_checker(tmp_path / "definitely_not_here.json", bundle_path)

    assert result.returncode == 1, result.stdout
    assert "FAIL" in result.stdout
    assert "SKIPPED" not in result.stdout


def test_transient_fetch_failure_skips_but_a_4xx_fails() -> None:
    """Only a TRANSIENT fetch problem may skip. A 4xx means the URL is wrong and
    will stay wrong, so it must fail like any other unrunnable gate."""
    import importlib.util

    spec = importlib.util.spec_from_file_location("parity_checker", CHECKER)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    import urllib.error

    def _raise(exc):
        def _open(*_args, **_kwargs):
            raise exc
        return _open

    module.urllib.request.urlopen = _raise(
        urllib.error.HTTPError("https://x/openapi.json", 404, "Not Found", {}, None)
    )
    with pytest.raises(module.SpecMisconfigured):
        module.read_spec("https://x/openapi.json")

    module.urllib.request.urlopen = _raise(urllib.error.URLError("connection reset"))
    with pytest.raises(module.SpecUnavailable):
        module.read_spec("https://x/openapi.json")

    module.urllib.request.urlopen = _raise(
        urllib.error.HTTPError("https://x/openapi.json", 503, "Unavailable", {}, None)
    )
    with pytest.raises(module.SpecUnavailable):
        module.read_spec("https://x/openapi.json")

    # A certificate problem arrives wrapped in URLError but is standing
    # misconfiguration - skipping it would keep every run green while nothing is
    # ever compared.
    import ssl

    module.urllib.request.urlopen = _raise(
        urllib.error.URLError(ssl.SSLError("certificate verify failed"))
    )
    with pytest.raises(module.SpecMisconfigured):
        module.read_spec("https://x/openapi.json")

    # A 3xx that surfaces as an error is a redirect loop or an unsupported
    # redirect target: persistent, so it must not skip either.
    module.urllib.request.urlopen = _raise(
        urllib.error.HTTPError("https://x/openapi.json", 310, "Too many redirects", {}, None)
    )
    with pytest.raises(module.SpecMisconfigured):
        module.read_spec("https://x/openapi.json")


def test_matching_spec_stamp_does_not_excuse_a_tampered_bundle(tmp_path) -> None:
    """The stamp authenticates the SOURCE bytes, not the generated catalog. A
    bundle with an endpoint removed by hand keeps its stamp intact, so trusting
    the stamp as proof of parity would wave exactly that through."""
    spec = _fixture_spec()
    spec_path = tmp_path / "spec.json"
    raw = json.dumps(spec).encode("utf-8")
    spec_path.write_bytes(raw)
    real_sha = hashlib.sha256(raw).hexdigest()

    catalog = build_catalog_from_openapi(spec, source="unit", spec_sha256=real_sha)
    dropped = sorted(catalog.operation_ids)[0]
    tampered = catalog.to_dict()
    tampered["endpoints"] = [e for e in tampered["endpoints"] if e["operation_id"] != dropped]
    tampered["endpoint_count"] = len(tampered["endpoints"])
    assert tampered["spec_sha256"] == real_sha, "the stamp must survive the tamper"

    bundle_path = tmp_path / "endpoints.json"
    bundle_path.write_text(json.dumps(tampered), encoding="utf-8")

    result = _run_checker(spec_path, bundle_path)

    assert result.returncode == 1, result.stdout
    assert "DRIFT" in result.stdout
    assert dropped in result.stdout


def _spec_with_first_operation_mutated(mutate) -> tuple[dict, str]:
    """Return a spec whose first operation is altered by `mutate`, keeping its
    operationId, plus that operationId."""
    spec = _fixture_spec()
    for path, item in spec["paths"].items():
        for method, operation in item.items():
            if isinstance(operation, dict) and operation.get("operationId"):
                op_id = operation["operationId"]
                mutate(spec, path, method, operation)
                return spec, op_id
    raise AssertionError("fixture has no operation with an operationId")


@pytest.mark.parametrize(
    ("label", "mutate"),
    [
        # the path moves but the operation keeps its name - a client built from
        # the stale bundle would call the old URL
        ("path", lambda spec, path, method, op: spec["paths"].__setitem__(
            path + "/moved", spec["paths"].pop(path))),
        # a parameter becomes required - the stale bundle would omit it
        ("required parameter", lambda spec, path, method, op: op.setdefault(
            "parameters", []).append(
                {"name": "newly_required", "in": "query", "required": True,
                 "schema": {"type": "string"}})),
        # the request body gains a required field
        ("request body", lambda spec, path, method, op: op.__setitem__(
            "requestBody", {"required": True, "content": {"application/json": {
                "schema": {"type": "object", "required": ["added"],
                           "properties": {"added": {"type": "string"}}}}}})),
    ],
)
def test_a_changed_contract_under_the_same_operation_id_is_drift(tmp_path, label, mutate) -> None:
    """Matching ids are not a matching contract: paths, parameters and bodies can
    change while the operationId stays put, leaving clients calling a stale
    signature. Every such change must be reported as drift."""
    spec, op_id = _spec_with_first_operation_mutated(mutate)
    # the bundle is the PRE-change build; the spec is the post-change one
    bundle_catalog = build_catalog_from_openapi(_fixture_spec(), source="unit")
    bundle_path = tmp_path / "endpoints.json"
    bundle_path.write_text(json.dumps(bundle_catalog.to_dict()), encoding="utf-8")
    spec_path = tmp_path / "spec.json"
    spec_path.write_text(json.dumps(spec), encoding="utf-8")

    result = _run_checker(spec_path, bundle_path)

    assert result.returncode == 1, f"{label} change not reported as drift: {result.stdout}"
    assert "DRIFT" in result.stdout
    assert op_id in result.stdout, f"the checker must name the operation ({label})"
