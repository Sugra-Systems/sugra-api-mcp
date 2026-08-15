"""A failed tool call must be visible as a failure at the protocol level.

Every test here drives a real MCP client session over an in-memory transport, so
what is asserted is what a client actually receives - not what the tool function
returned before the SDK got hold of it. That distinction is the entire point:
the tool functions were always returning a clear explanation, and the protocol
was reporting every one of them as a success.

The success cases matter as much as the failures. Flagging a result that carries
data would break working calls for every client, so the payload shapes that look
error-ish while being perfectly good - a partial-degradation envelope and a
retry-with-parameters prompt - are pinned here too.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from mcp.shared.memory import create_connected_server_and_client_session

from sugra_api_mcp import tools  # noqa: F401  (registers the tools on the server)
from sugra_api_mcp.server import mcp
from sugra_api_mcp.tools import gateway

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend():
    return "asyncio"


class _Client:
    """Stands in for SugraClient, returning whatever the test needs."""

    def __init__(self, payload: Any) -> None:
        self._payload = payload

    async def get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        return self._payload

    async def post(self, path: str, json: dict[str, Any] | None = None) -> Any:
        return self._payload

    async def request(self, method: str, path: str, **kwargs: Any) -> Any:
        return self._payload


async def _call(tool: str, arguments: dict[str, Any]):
    async with create_connected_server_and_client_session(mcp) as session:
        return await session.call_tool(tool, arguments)


def _structured(result) -> dict[str, Any]:
    """The structured payload, falling back to the text block.

    A client may read either; both must carry the explanation.
    """
    if result.structuredContent is not None:
        return result.structuredContent
    assert result.content, "an error result must never be empty"
    return json.loads(result.content[0].text)


# --- failures are reported as failures --------------------------------------


async def test_unknown_operation_id_is_reported_as_an_error() -> None:
    result = await _call("call_endpoint", {"operation_id": "no_such_operation"})

    assert result.isError is True
    payload = _structured(result)
    assert payload["error"] == "unknown_operation_id"
    assert payload["operation_id"] == "no_such_operation"


async def test_missing_required_parameters_is_reported_as_an_error(monkeypatch) -> None:
    monkeypatch.setattr(gateway, "get_client", lambda: _Client({"data": []}))

    result = await _call("call_endpoint", {"operation_id": "quotes_symbol_price", "params": {}})

    assert result.isError is True
    payload = _structured(result)
    assert payload["error"] == "missing_required_parameters"
    assert payload["missing"], "the caller must be told WHICH parameters are missing"


@pytest.mark.parametrize(
    "upstream",
    [
        {"error": "not_found", "status_code": 404, "url": "https://sugra.ai/x"},
        {"error": "HTTP 503", "status_code": 503, "url": "https://sugra.ai/x"},
        {"error": "upstream_timeout", "reason": "ReadTimeout", "status_code": None},
        {"error": "upstream_connect_error", "reason": "ConnectError", "status_code": None},
    ],
)
async def test_an_upstream_failure_reaches_the_caller_intact(monkeypatch, upstream: dict) -> None:
    """Whatever the failure was, the caller gets the flag AND the explanation -
    the two used to be mutually exclusive."""
    monkeypatch.setattr(gateway, "get_client", lambda: _Client(dict(upstream)))

    result = await _call(
        "call_endpoint", {"operation_id": "quotes_symbol_price", "params": {"symbol": "AAPL"}}
    )

    assert result.isError is True
    payload = _structured(result)
    for key, value in upstream.items():
        assert payload[key] == value, f"{key} was lost or altered"


async def test_the_error_text_is_never_empty() -> None:
    """The reason failures were returned rather than raised: a raised exception
    with no message reaches the agent as an empty string."""
    result = await _call("call_endpoint", {"operation_id": "no_such_operation"})

    assert result.isError is True
    assert result.content, "no content block"
    assert result.content[0].text.strip(), "the explanation is empty"
    assert "unknown_operation_id" in result.content[0].text


async def test_the_failure_payload_loses_nothing_and_matches_its_text(monkeypatch) -> None:
    """Flagging a failure must not cost the caller any of the explanation.

    Every key the tool produced survives, and the text block renders the same
    payload - a reader comparing the two must not find them disagreeing.
    """
    upstream = {
        "error": "upstream_timeout",
        "reason": "ReadTimeout",
        "status_code": None,
        "elapsed_ms": 42,
        "url": "https://sugra.ai/api/v1/quotes/AAPL/price",
        "retry_hint": "retry in a few seconds",
    }
    monkeypatch.setattr(gateway, "get_client", lambda: _Client(dict(upstream)))

    result = await _call(
        "call_endpoint", {"operation_id": "quotes_symbol_price", "params": {"symbol": "AAPL"}}
    )

    assert result.isError is True
    payload = result.structuredContent
    for key, value in upstream.items():
        assert payload[key] == value, f"{key} was lost or altered"
    assert json.loads(result.content[0].text) == payload, "the text disagrees with the payload"


# --- successes stay successes ------------------------------------------------


async def test_a_successful_call_is_not_flagged(monkeypatch) -> None:
    monkeypatch.setattr(gateway, "get_client", lambda: _Client({"data": [{"symbol": "AAPL"}]}))

    result = await _call(
        "call_endpoint", {"operation_id": "quotes_symbol_price", "params": {"symbol": "AAPL"}}
    )

    assert result.isError is False


async def test_a_payload_carrying_both_data_and_an_error_note_is_a_success(monkeypatch) -> None:
    """A 200 can report a partial degradation alongside real data. Flagging it
    would fail a call that returned exactly what was asked for."""
    monkeypatch.setattr(
        gateway,
        "get_client",
        lambda: _Client({"data": [{"symbol": "AAPL"}], "error": "one source degraded"}),
    )

    result = await _call(
        "call_endpoint", {"operation_id": "quotes_symbol_price", "params": {"symbol": "AAPL"}}
    )

    assert result.isError is False, "a result carrying data is not a failure"


async def test_a_non_dict_result_is_passed_through_untouched() -> None:
    """The failure check must never manufacture a verdict from a result it
    cannot inspect. A bare array or a scalar has no keys, and a membership test
    against a string would match the word "error" inside ordinary prose.

    (A bare-array response is separately mishandled upstream of this check: the
    tool declares a dict return, so output validation rejects it before the flag
    is ever considered. That is a different contract from this one.)
    """
    from sugra_api_mcp.errors import is_error_payload

    for value in ([{"symbol": "AAPL"}], "an error occurred while parsing", 42, None, True):
        assert is_error_payload(value) is False, f"{value!r} is not a failure"


async def test_the_needs_parameters_prompt_is_not_flagged(monkeypatch) -> None:
    """fetch_data answers an under-specified request by naming the parameters to
    supply. It carries no `error` key and must stay a success: the agent's next
    move is to fill them in, not to treat the call as broken."""
    monkeypatch.setattr(gateway, "get_client", lambda: _Client({"data": []}))

    result = await _call("fetch_data", {"query": "price of a stock"})

    payload = _structured(result)
    assert "needs_params" in payload, (
        f"expected the retry prompt, got {sorted(payload)} - this test would "
        "otherwise assert nothing"
    )
    assert result.isError is False, "a retry prompt is not a failure"


# --- the entity tools normalise their errors and must not normalise away the
# --- facts a caller needs to act on


@pytest.mark.parametrize("status", [429, 503, 404])
async def test_entity_tool_failures_keep_their_correlation_fields(
    monkeypatch, status: int
) -> None:
    """These tools rewrite a client error into their own compact shape, and that
    rewrite dropped the status code and the correlation id - so a caller could
    not tell what the server actually answered, nor point anyone at the call.
    Both must survive the rewrite.
    """
    from sugra_api_mcp.tools import entities

    upstream = {
        "error": f"HTTP {status}",
        "status_code": status,
        "url": "https://sugra.ai/api/v1/entity/screen",
        "request_id": "req_01HZY7",
    }
    monkeypatch.setattr(entities, "get_client", lambda: _Client(dict(upstream)))

    result = await _call("sugra_entity_screen", {"name": "Acme Holdings"})

    assert result.isError is True
    payload = _structured(result)
    assert payload["status_code"] == status
    assert payload["request_id"] == "req_01HZY7", "the correlation id was normalised away"


async def test_a_documented_caller_error_keeps_the_status_that_explains_it(monkeypatch) -> None:
    """A 500 behind this API can be the caller's own mistake.

    The bundled catalog documents one in its own words: an IBGE/SIDRA aggregate
    that declares a classification dimension answers 500 when the call omits it.
    The caller needs the status to work that out, so it must survive.
    """
    from sugra_api_mcp.catalog.loader import load_catalog

    operation = "statistical_agencies_ibge_aggregates_aggregate_id_data"
    endpoint = load_catalog().get(operation)
    assert "500" in (endpoint.description or endpoint.summary), (
        "the catalog no longer documents the permanent 500 this case is built on"
    )

    monkeypatch.setattr(
        gateway,
        "get_client",
        lambda: _Client({"error": "missing classification dimension", "status_code": 500,
                         "url": "https://sugra.ai/x"}),
    )

    result = await _call(
        "call_endpoint",
        {"operation_id": operation, "params": {"aggregate_id": "1705", "variables": "63"}},
    )

    assert result.isError is True
    assert _structured(result)["status_code"] == 500, "the status the caller must act on was lost"
