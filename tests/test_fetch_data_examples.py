"""The examples in fetch_data's description must run as written.

fetch_data runs the top search hit unless a sent key is rare and foreign to
it and a GET hit among the first five, scoring at least half the top,
declares every sent key (test_fetch_data_selection.py); a param the operation
that runs does not declare returns unknown_parameters. A model that copies an example should get
the operation the example names, selected the way fetch_data selects it, with
every param it passes declared there.
Offline: the bundled catalog and the search, no HTTP.
"""

from __future__ import annotations

import ast
import re
from typing import Any

from sugra_api_mcp.catalog.loader import load_catalog
from sugra_api_mcp.catalog.search import search_catalog
from sugra_api_mcp.tools import gateway

_FETCH_EXAMPLE = re.compile(r"- `fetch_data\((?P<args>[^`]*)\)`\s+runs (?P<operation>\w+)")
_CALL_EXAMPLE = re.compile(r"`call_endpoint\((?P<args>[^`]*)\)`")


def _arguments(source: str) -> tuple[str, dict[str, Any]]:
    call = ast.parse(f"f({source})", mode="eval").body
    assert isinstance(call, ast.Call) and len(call.args) == 1, source
    keywords = {kw.arg: ast.literal_eval(kw.value) for kw in call.keywords}
    assert set(keywords) <= {"params"}, source
    return ast.literal_eval(call.args[0]), keywords.get("params") or {}


def _assert_runs(operation_id: str, params: dict[str, Any]) -> None:
    endpoint = load_catalog().get(operation_id)
    declared = {parameter.name for parameter in endpoint.parameters}
    required = {parameter.name for parameter in endpoint.parameters if parameter.required}

    assert set(params) <= declared, (operation_id, sorted(set(params) - declared))
    assert required <= set(params), (operation_id, sorted(required - set(params)))
    assert not endpoint.request_body_required, operation_id


def test_every_fetch_data_example_picks_the_operation_it_names() -> None:
    doc = gateway.fetch_data.__doc__ or ""
    examples = list(_FETCH_EXAMPLE.finditer(doc))

    assert len(examples) == doc.count("- `fetch_data("), "an example names no operation"
    assert len(examples) >= 3
    catalog = load_catalog()
    for example in examples:
        query, params = _arguments(example["args"])
        results = search_catalog(catalog, query, limit=gateway._SELECTION_WINDOW)
        top = catalog.get(results[0]["operation_id"])
        reselected = gateway._reselect(catalog, results, top, params, None)
        runs = reselected[0] if reselected is not None else top.operation_id

        assert runs == example["operation"], (query, runs)
        _assert_runs(runs, params)


def test_every_call_endpoint_example_in_fetch_data_runs() -> None:
    doc = gateway.fetch_data.__doc__ or ""
    examples = list(_CALL_EXAMPLE.finditer(doc))

    assert examples
    for example in examples:
        operation_id, params = _arguments(example["args"])
        _assert_runs(operation_id, params)
