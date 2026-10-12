"""The time-boxed client identity record (probe.py) behind the gate."""

from __future__ import annotations

import datetime as dt
import json
import logging
from typing import Any

import httpx
import pytest
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from sugra_api_mcp import demand, gate, probe

# A made-up JWT shape, joined at runtime so no literal reads as a token.
_TOKEN = ".".join(("eyJ" + "header", "payload-secret-777", "signature"))
_SESSION = "session-secret-888"
_ARGUMENT = "argument-secret-999"


class _Collect(logging.Handler):
    def __init__(self) -> None:
        super().__init__(logging.DEBUG)
        self.messages: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.messages.append(record.getMessage())


@pytest.fixture(autouse=True)
def _running(monkeypatch):
    """Keep the experiment running for these tests, whatever today's date is."""
    monkeypatch.setattr(probe, "PROBE_UNTIL", dt.datetime.now(dt.UTC) + dt.timedelta(days=1))


@pytest.fixture
def probed():
    """Every message the probe logger writes during the test."""
    handler = _Collect()
    probe.logger.addHandler(handler)
    try:
        yield handler.messages
    finally:
        probe.logger.removeHandler(handler)


def _rid(number: int) -> str:
    return f"{number:02x}" + "cd" * 15


def _records(messages: list[str]) -> list[dict[str, Any]]:
    out = []
    for message in messages:
        assert message.startswith(probe.RECORD_PREFIX), message
        out.append(json.loads(message[len(probe.RECORD_PREFIX) :]))
    return out


async def _post(
    path: str, body: Any, headers: dict[str, str], method: str = "POST"
) -> httpx.Response:
    async def answer(request: Request) -> JSONResponse:
        await request.body()
        return JSONResponse({"jsonrpc": "2.0", "id": 1, "result": {}})

    app = Starlette(routes=[Route(path, answer, methods=["GET", "POST"])])
    app.add_middleware(
        gate.GateMiddleware,
        max_body_bytes=8192,
        summary=gate.GateSummary(),
        demand_counter=demand.DemandCounter(),
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:8002"
    ) as client:
        if method == "GET":
            return await client.get(path, headers={"x-request-id": _rid(1), **headers})
        return await client.post(path, json=body, headers={"x-request-id": _rid(1), **headers})


_INITIALIZE = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
        "protocolVersion": "2025-06-18",
        "capabilities": {"roots": {}, "sampling": {}},
        "clientInfo": {"name": "openai-mcp", "version": "1.0.0"},
    },
}
_HEADERS = {
    "user-agent": "openai-mcp/1.0",
    "origin": "https://chatgpt.com",
    "mcp-protocol-version": "2025-06-18",
    "authorization": f"Bearer {_TOKEN}",
    "mcp-session-id": _SESSION,
    "x-api-key": "sugra_secret_key_555",
    "cookie": "sid=cookie-secret-444",
}


async def test_an_initialize_record_names_the_client_and_its_headers(probed) -> None:
    response = await _post("/mcp", _INITIALIZE, _HEADERS)
    assert response.status_code == 200
    [entry] = _records(probed)
    assert entry["method"] == "initialize"
    assert (entry["client_name"], entry["client_version"]) == ("openai-mcp", "1.0.0")
    assert entry["protocol_version"] == "2025-06-18"
    assert entry["capabilities"] == ["roots", "sampling"]
    assert entry["user_agent"] == "openai-mcp/1.0"
    assert entry["origin"] == "https://chatgpt.com"
    assert entry["mcp_protocol_version"] == "2025-06-18"
    assert entry["status"] == 200
    assert entry["authorization"] == {"scheme": "bearer", "kind": "jwt"}
    assert entry["x_api_key"] is True
    assert len(entry["session"]) == 8
    assert "cookie" in entry["header_names"] and "authorization" in entry["header_names"]


async def test_a_record_never_holds_a_credential_session_id_or_cookie(probed) -> None:
    await _post("/mcp", _INITIALIZE, _HEADERS)
    text = "\n".join(probed)
    for secret in (
        _TOKEN,
        "payload-secret-777",
        _SESSION,
        "sugra_secret_key_555",
        "cookie-secret-444",
    ):
        assert secret not in text


async def test_a_tool_call_keeps_its_tool_name_never_its_arguments(probed) -> None:
    body = {
        "jsonrpc": "2.0",
        "id": 2,
        "method": "tools/call",
        "params": {"name": "fetch_data", "arguments": {"query": _ARGUMENT}},
    }
    await _post("/mcp", body, {"user-agent": "x"})
    [entry] = _records(probed)
    assert (entry["method"], entry["tool"]) == ("tools/call", "fetch_data")
    assert _ARGUMENT not in "\n".join(probed)


@pytest.mark.parametrize(
    ("value", "expected", "secret"),
    [
        ("sugra_raw-secret-666", {"scheme": "none", "kind": "sugra_key"}, "raw-secret-666"),
        ("Bearer\ttab-secret-555", {"scheme": "bearer", "kind": "other"}, "tab-secret-555"),
        ("Scheme-secret-444 x", {"scheme": "other", "kind": "other"}, "Scheme-secret-444"),
    ],
)
async def test_an_odd_authorization_value_keeps_no_text_of_it(
    probed, value: str, expected: dict[str, str], secret: str
) -> None:
    await _post("/mcp", _INITIALIZE, {"authorization": value})
    [entry] = _records(probed)
    assert entry["authorization"] == expected
    assert secret not in "\n".join(probed)


async def test_a_streamed_answer_is_one_record(probed) -> None:
    async def stream(scope, receive, send) -> None:
        while (await receive()).get("more_body", False):
            pass
        await send({"type": "http.response.start", "status": 200, "headers": []})
        for part in (b"event: message\n", b"data: {}\n\n", b"data: {}\n\n"):
            await send({"type": "http.response.body", "body": part, "more_body": True})
        await send({"type": "http.response.body", "body": b""})

    app = gate.GateMiddleware(
        stream,
        max_body_bytes=8192,
        summary=gate.GateSummary(),
        demand_counter=demand.DemandCounter(),
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:8002"
    ) as client:
        response = await client.post(
            "/mcp", json=_INITIALIZE, headers={"x-request-id": _rid(2), **_HEADERS}
        )
    assert response.status_code == 200
    [entry] = _records(probed)
    assert (entry["method"], entry["status"]) == ("initialize", 200)


async def test_many_headers_keep_their_total(probed) -> None:
    extra = {f"x-extra-{i:03d}": "v" for i in range(120)}
    await _post("/mcp", _INITIALIZE, {"user-agent": "ua-many", **extra})
    [entry] = _records(probed)
    assert len(entry["header_names"]) == probe.HEADER_NAMES_MAX
    assert entry["header_count"] > probe.HEADER_NAMES_MAX
    assert entry["user_agent"] == "ua-many"


async def test_a_body_that_is_not_json_reads_unparsed(probed) -> None:
    async def answer(request: Request) -> JSONResponse:
        await request.body()
        return JSONResponse({})

    app = Starlette(routes=[Route("/mcp", answer, methods=["POST"])])
    app.add_middleware(
        gate.GateMiddleware,
        max_body_bytes=8192,
        summary=gate.GateSummary(),
        demand_counter=demand.DemandCounter(),
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:8002"
    ) as client:
        await client.post("/mcp", content=b"{not json", headers={"x-request-id": _rid(3)})
    [entry] = _records(probed)
    assert entry["method"] == "unparsed"


async def test_only_mcp_is_recorded(probed) -> None:
    await _post("/other", _INITIALIZE, {})
    assert probed == []


async def test_only_a_post_is_recorded(probed) -> None:
    response = await _post("/mcp", None, _HEADERS, method="GET")
    assert response.status_code == 200
    assert probed == []


async def test_nothing_is_recorded_after_the_experiment_ends(probed, monkeypatch) -> None:
    monkeypatch.setattr(probe, "PROBE_UNTIL", dt.datetime(2000, 1, 1, tzinfo=dt.UTC))
    response = await _post("/mcp", _INITIALIZE, _HEADERS)
    assert response.status_code == 200
    assert probed == []


def test_the_experiment_has_a_fixed_end() -> None:
    assert probe.active(lambda: probe.PROBE_UNTIL - dt.timedelta(seconds=1))
    assert not probe.active(lambda: probe.PROBE_UNTIL)


async def test_a_failing_record_never_changes_the_answer(probed, monkeypatch) -> None:
    def boom(*_: Any) -> dict[str, Any]:
        raise RuntimeError("probe broke")

    monkeypatch.setattr(probe, "record", boom)
    response = await _post("/mcp", _INITIALIZE, _HEADERS)
    assert response.status_code == 200
    assert response.json() == {"jsonrpc": "2.0", "id": 1, "result": {}}
    assert probed == ["Probe record failed (RuntimeError)."]


async def test_long_and_control_text_is_cut_and_replaced(probed) -> None:
    body = {
        **_INITIALIZE,
        "params": {**_INITIALIZE["params"], "clientInfo": {"name": "a\nb" + "x" * 500}},
    }
    await _post("/mcp", body, {"user-agent": "u\x00" + "y" * 500})
    [entry] = _records(probed)
    assert entry["client_name"].startswith("a?b") and len(entry["client_name"]) == probe.VALUE_MAX
    assert entry["user_agent"].startswith("u?") and len(entry["user_agent"]) == probe.VALUE_MAX
