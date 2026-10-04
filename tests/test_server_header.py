"""The Server response header: one stable value on every answer, the version only on request.

The HTTP tests serve the app the entry point builds on a real uvicorn, bound to
a free port on 127.0.0.1 and configured with the parameters the entry point
hands to uvicorn.run, and read the header the way a client receives it: on the
answers of the app and of the auth middleware, on the 500 for a handler that
fails, and on the 400 uvicorn writes itself for a request it cannot parse.
"""

from __future__ import annotations

import argparse
import contextlib
import os
import socket
import sys
import threading
import time
import types
from collections.abc import Iterator
from typing import Any

import httpx
import pytest
import uvicorn

from sugra_api_mcp import __main__ as entry
from sugra_api_mcp import __version__, config, gate, observability, server, web
from sugra_api_mcp import client as client_module

SETTING = "SUGRA_MCP_SERVER_VERSION"
PRODUCT = "sugra-api-mcp"
VERSIONED = f"{PRODUCT}/{__version__}"


@pytest.fixture(autouse=True)
def _setting_unset(monkeypatch) -> None:
    """Each test starts without the setting, whatever the environment running the tests holds."""
    monkeypatch.delenv(SETTING, raising=False)


# ---- The setting ----


def test_the_product_and_the_setting_are_named_once() -> None:
    assert (config.SERVER_PRODUCT, config.SERVER_VERSION_ENV) == (PRODUCT, SETTING)


@pytest.mark.parametrize("value", ["1", "true", "TRUE", "yes", " Yes ", "on", "ON"])
def test_an_on_value_adds_the_version(monkeypatch, value: str) -> None:
    monkeypatch.setenv(SETTING, value)
    assert config.server_version_disclosed() is True
    assert config.server_header_value() == VERSIONED


@pytest.mark.parametrize("value", [None, "", " ", "0", "false", "no", "off", "2", "enabled", "1.0"])
def test_any_other_value_leaves_the_version_out(monkeypatch, value: str | None) -> None:
    if value is not None:
        monkeypatch.setenv(SETTING, value)
    assert config.server_version_disclosed() is False
    assert config.server_header_value() == PRODUCT


# ---- What the entry point hands uvicorn ----


def _built_by_the_entry_point(monkeypatch) -> tuple[Any, dict[str, Any]]:
    """The app and the uvicorn.run parameters of the entry point.

    As `sugra-api-mcp --transport streamable-http --host 127.0.0.1 --port 0` builds them.
    """
    captured: dict[str, Any] = {}

    def run(app: Any, **kwargs: Any) -> None:
        captured.update(app=app, kwargs=kwargs)

    monkeypatch.setattr(observability, "setup_observability", lambda: False)
    monkeypatch.setattr(observability, "flush_telemetry", lambda timeout_s: True)
    # The lifespan exit writes a summary and closes the shared API connection
    # pools: here they are this test's own, never the ones other tests left behind.
    monkeypatch.setattr(gate, "default_summary", gate.GateSummary())
    monkeypatch.setattr(client_module, "_pools", {})
    monkeypatch.setattr(server.mcp, "_session_manager", None)
    monkeypatch.setattr(uvicorn, "run", run)
    monkeypatch.delenv("SUGRA_AGENT_INTERNAL_TOKEN", raising=False)
    monkeypatch.setenv("SUGRA_APP_URL", "http://127.0.0.1:9")
    entry._run_server(argparse.Namespace(transport="streamable-http", host="127.0.0.1", port=0))
    return captured["app"], captured["kwargs"]


def test_the_entry_point_passes_one_server_header_to_uvicorn(monkeypatch) -> None:
    _app, kwargs = _built_by_the_entry_point(monkeypatch)
    assert kwargs == entry.uvicorn_settings("127.0.0.1", 0)
    assert kwargs["server_header"] is False
    assert kwargs["headers"] == [("server", PRODUCT)]
    assert (kwargs["log_level"], kwargs["timeout_graceful_shutdown"]) == ("info", 45)


@contextlib.contextmanager
def _serving(monkeypatch) -> Iterator[httpx.Client]:
    """A client of the entry point's app, served by uvicorn with the entry point's parameters."""
    app, kwargs = _built_by_the_entry_point(monkeypatch)
    # Port 0, as the entry point was given, lets the system pick a free port.
    # log_config=None keeps uvicorn from replacing the logging setup of the
    # test process; it changes nothing in an answer.
    served = uvicorn.Server(uvicorn.Config(app, **{**kwargs, "log_config": None}))
    thread = threading.Thread(target=served.run, name="uvicorn-under-test", daemon=True)
    thread.start()
    try:
        deadline = time.monotonic() + 30
        while not served.started:
            assert thread.is_alive(), "uvicorn stopped before it served"
            assert time.monotonic() < deadline, "uvicorn did not start serving within 30 s"
            time.sleep(0.02)
        port = served.servers[0].sockets[0].getsockname()[1]
        base_url = f"http://127.0.0.1:{port}"
        with httpx.Client(base_url=base_url, trust_env=False, timeout=10) as client:
            yield client
    finally:
        served.should_exit = True
        thread.join(timeout=30)
    assert not thread.is_alive(), "uvicorn did not stop within 30 s"


def _raw_answer(port: int, request: bytes) -> tuple[str, list[tuple[str, str]]]:
    """The status line and the header fields of the answer to a request sent as raw bytes."""
    received = b""
    with socket.create_connection(("127.0.0.1", port), timeout=10) as connection:
        connection.sendall(request)
        while chunk := connection.recv(65536):
            received += chunk
    status, *fields = received.split(b"\r\n\r\n", 1)[0].decode("latin-1").split("\r\n")
    pairs = (field.partition(":") for field in fields)
    return status, [(name.strip().lower(), value.strip()) for name, _, value in pairs]


# ---- On the wire ----


def test_every_answer_carries_one_server_header_without_the_version(monkeypatch) -> None:
    with _serving(monkeypatch) as client:
        answers = {
            "/health": client.get("/health"),
            "/mcp without a token": client.get("/mcp"),
            "an unknown path": client.get("/no-such-page"),
        }
        status, fields = _raw_answer(
            client.base_url.port,
            b"GET /health HTTP/1.1\r\nHost: 127.0.0.1\r\nnot a header line\r\n\r\n",
        )
    assert {name: answer.status_code for name, answer in answers.items()} == {
        "/health": 200,
        "/mcp without a token": 401,
        "an unknown path": 401,
    }
    for name, answer in answers.items():
        assert answer.headers.get_list("server") == [PRODUCT], name
    assert answers["/health"].json() == {"status": "ok", "service": PRODUCT}
    # uvicorn's own answer to a request it cannot parse, written before any app code runs.
    assert status.startswith("HTTP/1.1 400 "), status
    assert [value for name, value in fields if name == "server"] == [PRODUCT]


def test_a_failing_handler_answers_500_with_one_server_header(monkeypatch) -> None:
    async def failing(_request: Any) -> Any:
        raise RuntimeError("the health handler failed")

    monkeypatch.setattr(web, "health", failing)
    with _serving(monkeypatch) as client:
        answer = client.get("/health")
    assert answer.status_code == 500
    assert answer.headers.get_list("server") == [PRODUCT]


def test_the_setting_adds_the_version_to_the_header_and_to_health(monkeypatch) -> None:
    monkeypatch.setenv(SETTING, "1")
    with _serving(monkeypatch) as client:
        health = client.get("/health")
        refused = client.get("/mcp")
    assert (health.status_code, refused.status_code) == (200, 401)
    assert health.headers.get_list("server") == [VERSIONED]
    assert refused.headers.get_list("server") == [VERSIONED]
    assert health.json() == {"status": "ok", "service": PRODUCT, "version": __version__}


# ---- The version in the telemetry resource ----


def _attributes_the_exporter_reads(monkeypatch, operator_value: str | None) -> str | None:
    """OTEL_RESOURCE_ATTRIBUTES when setup_observability configures the exporter."""
    seen: dict[str, str | None] = {}

    def configure_azure_monitor(**_kwargs: Any) -> None:
        seen["attributes"] = os.environ.get("OTEL_RESOURCE_ATTRIBUTES")

    sdk = types.ModuleType("azure.monitor.opentelemetry")
    sdk.configure_azure_monitor = configure_azure_monitor
    otel = types.ModuleType("opentelemetry")
    otel.trace = types.SimpleNamespace(get_tracer=lambda _name: object())
    monkeypatch.setitem(sys.modules, "azure.monitor.opentelemetry", sdk)
    monkeypatch.setitem(sys.modules, "opentelemetry", otel)
    monkeypatch.setitem(sys.modules, "opentelemetry.trace", otel.trace)
    monkeypatch.setattr(observability, "_INITIALISED", False)
    monkeypatch.setattr(observability, "_TRACER", None)
    monkeypatch.setenv("APPLICATIONINSIGHTS_CONNECTION_STRING", "configured-for-this-test")
    monkeypatch.setenv("OTEL_SERVICE_NAME", "sugra-mcp")
    for name in observability._TRACE_OVERRIDE_VARS:
        monkeypatch.delenv(name, raising=False)
    if operator_value is None:
        monkeypatch.delenv("OTEL_RESOURCE_ATTRIBUTES", raising=False)
    else:
        monkeypatch.setenv("OTEL_RESOURCE_ATTRIBUTES", operator_value)
    assert observability.setup_observability() is True
    return seen["attributes"]


@pytest.mark.parametrize("operator_value", [None, "", " , "])
def test_the_version_goes_into_the_telemetry_resource(
    monkeypatch, operator_value: str | None
) -> None:
    resources = pytest.importorskip("opentelemetry.sdk.resources")
    attributes = _attributes_the_exporter_reads(monkeypatch, operator_value)
    assert attributes == f"service.version={__version__}"
    # Read back the way the OpenTelemetry SDK reads the variable.
    assert resources.OTELResourceDetector().detect().attributes["service.version"] == __version__


def test_the_operator_attributes_stay_and_the_version_joins_them(monkeypatch) -> None:
    attributes = _attributes_the_exporter_reads(
        monkeypatch, "deployment.environment=staging, team=data,"
    )
    assert attributes == f"deployment.environment=staging, team=data,service.version={__version__}"


def test_a_second_setup_adds_no_second_version(monkeypatch) -> None:
    first = _attributes_the_exporter_reads(monkeypatch, "team=data")
    monkeypatch.setattr(observability, "_INITIALISED", False)
    assert observability.setup_observability() is True
    expected = f"team=data,service.version={__version__}"
    assert os.environ["OTEL_RESOURCE_ATTRIBUTES"] == first == expected


@pytest.mark.parametrize(
    "operator_value",
    ["service.version=2026.10", "deployment.environment=staging, service.version = 2026.10 "],
)
def test_an_operator_service_version_is_kept(monkeypatch, operator_value: str) -> None:
    assert _attributes_the_exporter_reads(monkeypatch, operator_value) == operator_value
