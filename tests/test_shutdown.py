"""Shutdown of the HTTP server: the last gate summary is exported and its connections close.

The SIGTERM tests run the server in a process of its own, started the way the
hosted service starts it, with an OpenTelemetry log exporter that writes to a
file and a batch that exports nothing on its timer. uvicorn raises SIGTERM
again once it has shut down, so the process never runs its exit hooks: the
summary arrives only when the lifespan exit wrote it and flushed the logs. The
first test builds a log pipeline of its own; the second lets the Azure Monitor
distro build the hosted one, with its live metrics and performance counter
processors ahead of the batch, and replaces only the exporter.

The other tests run, in process, the lifespan of the app the entry point
builds: on the way out it closes the shared Sugra API connection pools and
the authenticator's connection pool, after the last summary and before the
flush.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import signal
import socket
import subprocess
import sys
import time
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
import pytest

from sugra_api_mcp import client as client_module
from sugra_api_mcp import gate, observability, server
from sugra_api_mcp.auth import AuthMiddleware
from tests.test_request_credentials import HEADERS, INITIALIZE

REPO_ROOT = Path(__file__).resolve().parent.parent

# Runs in the server process; its arguments are the repository root, the
# export file and the port.
_SERVER = """
import json, logging, sys

repo_root, exported, port = sys.argv[1:4]
sys.path.insert(0, repo_root)

from opentelemetry._logs import set_logger_provider
from opentelemetry.sdk._logs import LoggerProvider
from opentelemetry.sdk._logs.export import BatchLogRecordProcessor

try:
    from opentelemetry.sdk._logs.export import LogRecordExporter as Exporter
    from opentelemetry.sdk._logs.export import LogRecordExportResult as Result
except ImportError:
    from opentelemetry.sdk._logs.export import LogExporter as Exporter
    from opentelemetry.sdk._logs.export import LogExportResult as Result
try:
    from opentelemetry.instrumentation.logging.handler import LoggingHandler
except ImportError:
    from opentelemetry.sdk._logs import LoggingHandler


class FileExporter(Exporter):
    def export(self, batch):
        with open(exported, "a", encoding="utf-8") as out:
            for item in batch:
                print(json.dumps(str(getattr(item, "log_record", item).body)), file=out)
        return Result.SUCCESS

    def shutdown(self):
        pass

    def force_flush(self, timeout_millis=10000):
        return True


# As on the hosted server: the exporting handler is on the root logger before
# the server is imported, and the root logger stays at WARNING.
provider = LoggerProvider(shutdown_on_exit=False)
provider.add_log_record_processor(
    BatchLogRecordProcessor(FileExporter(), schedule_delay_millis=600000)
)
set_logger_provider(provider)
logging.getLogger().addHandler(LoggingHandler(logger_provider=provider))

from sugra_api_mcp.__main__ import main

sys.argv = ["sugra-api-mcp", "--transport", "streamable-http", "--host", "127.0.0.1", "--port", port]
main()
"""

# Runs in the server process, with the same arguments. The server sets up its
# telemetry itself, through the Azure Monitor distro, as on the hosted server;
# only the distro's log exporter is replaced, by one that writes to the file.
_AZURE_SERVER = """
import json, sys

repo_root, exported, port = sys.argv[1:4]
sys.path.insert(0, repo_root)

import azure.monitor.opentelemetry.exporter

try:
    from opentelemetry.sdk._logs.export import LogRecordExporter as Exporter
    from opentelemetry.sdk._logs.export import LogRecordExportResult as Result
except ImportError:
    from opentelemetry.sdk._logs.export import LogExporter as Exporter
    from opentelemetry.sdk._logs.export import LogExportResult as Result


class FileExporter(Exporter):
    def __init__(self, **kwargs):
        pass

    def export(self, batch):
        with open(exported, "a", encoding="utf-8") as out:
            for item in batch:
                print(json.dumps(str(getattr(item, "log_record", item).body)), file=out)
        return Result.SUCCESS

    def shutdown(self):
        pass

    def force_flush(self, timeout_millis=10000):
        return True


# The distro looks this name up when it builds the log pipeline.
azure.monitor.opentelemetry.exporter.AzureMonitorLogExporter = FileExporter

from sugra_api_mcp.__main__ import main

sys.argv = ["sugra-api-mcp", "--transport", "streamable-http", "--host", "127.0.0.1", "--port", port]
main()
"""


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def _server_env() -> dict[str, str]:
    """The test's environment without any Sugra, SDK or telemetry setting."""
    env = {
        name: value
        for name, value in os.environ.items()
        if not name.upper().startswith(("SUGRA_", "FASTMCP_", "OTEL_", "APPLICATIONINSIGHTS_"))
        and name.upper() not in ("INTERNAL_API_TOKEN", "CONTAINER_APP_REVISION")
    }
    # Nothing here calls the API or the app; if something did, it would stay local.
    env["SUGRA_API_BASE"] = env["SUGRA_APP_URL"] = "http://127.0.0.1:9"
    return env


def _tail(log: Path) -> str:
    return log.read_text(encoding="utf-8", errors="replace")[-4000:]


def _wait_until_serving(server: subprocess.Popen[bytes], client: httpx.Client, log: Path) -> None:
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        assert server.poll() is None, _tail(log)
        try:
            if client.get("/health").status_code == 200:
                return
        except httpx.TransportError:
            pass
        time.sleep(0.1)
    raise AssertionError(f"the server never answered /health\n{_tail(log)}")


@pytest.mark.skipif(
    sys.platform == "win32",
    reason=(
        "Windows cannot deliver SIGTERM to another process: send_signal(SIGTERM) "
        "ends it at once, with no handler run. CI runs this test on Linux."
    ),
)
def test_sigterm_exports_the_last_gate_summary(tmp_path: Path) -> None:
    exported, log = tmp_path / "exported.jsonl", tmp_path / "server.log"
    port = _free_port()
    request_id = "7d793037a0760186574b0282f2f435e7"
    with log.open("wb") as output:
        server = subprocess.Popen(
            [sys.executable, "-c", _SERVER, str(REPO_ROOT), str(exported), str(port)],
            cwd=tmp_path,
            env=_server_env(),
            stdout=output,
            stderr=subprocess.STDOUT,
        )
        try:
            with httpx.Client(base_url=f"http://127.0.0.1:{port}", trust_env=False, timeout=10) as client:
                _wait_until_serving(server, client, log)
                opened = client.post("/mcp", json=INITIALIZE, headers={**HEADERS, "x-request-id": request_id})
            assert opened.status_code == 200
            server.send_signal(signal.SIGTERM)
            server.wait(timeout=60)
        finally:
            if server.poll() is None:
                server.kill()
                server.wait()
    assert exported.exists(), _tail(log)
    records = [json.loads(line) for line in exported.read_text(encoding="utf-8").splitlines()]
    summaries = [record for record in records if record.startswith("sgate1 ")]
    assert len(summaries) == 1, _tail(log)
    assert re.fullmatch(
        rf"sgate1 side=vm boot=[0-9a-f]{{8}} seq=1 part=1/1 lines=1 dropped=0\n{request_id} -",
        summaries[0],
    )


@pytest.mark.skipif(
    sys.platform == "win32",
    reason=(
        "Windows cannot deliver SIGTERM to another process: send_signal(SIGTERM) "
        "ends it at once, with no handler run. CI runs this test on Linux."
    ),
)
def test_sigterm_exports_the_last_gate_summary_through_the_azure_pipeline(tmp_path: Path) -> None:
    pytest.importorskip("azure.monitor.opentelemetry")
    exported, log = tmp_path / "exported.jsonl", tmp_path / "server.log"
    port = _free_port()
    request_id = "7d793037a0760186574b0282f2f435e7"
    env = {
        **_server_env(),
        # Telemetry on, as on the hosted server, with both endpoints on this
        # machine; statsbeat and the remote settings are off, and the resource
        # comes from the environment alone, so nothing is sent anywhere else.
        "APPLICATIONINSIGHTS_CONNECTION_STRING": (
            "InstrumentationKey=00000000-0000-0000-0000-000000000000;"
            "IngestionEndpoint=http://127.0.0.1:9/;LiveEndpoint=http://127.0.0.1:9/"
        ),
        "APPLICATIONINSIGHTS_STATSBEAT_DISABLED_ALL": "true",
        "APPLICATIONINSIGHTS_CONTROLPLANE_DISABLED": "true",
        "OTEL_EXPERIMENTAL_RESOURCE_DETECTORS": "otel",
        # The batch timer never fires, as when the server stops within seconds
        # of the last summary: only the flush on the way out can export it.
        "OTEL_BLRP_SCHEDULE_DELAY": "600000",
        # Exports this machine refuses keep their retry files here.
        "TMPDIR": str(tmp_path),
    }
    with log.open("wb") as output:
        server = subprocess.Popen(
            [sys.executable, "-c", _AZURE_SERVER, str(REPO_ROOT), str(exported), str(port)],
            cwd=tmp_path,
            env=env,
            stdout=output,
            stderr=subprocess.STDOUT,
        )
        try:
            with httpx.Client(base_url=f"http://127.0.0.1:{port}", trust_env=False, timeout=10) as client:
                _wait_until_serving(server, client, log)
                opened = client.post("/mcp", json=INITIALIZE, headers={**HEADERS, "x-request-id": request_id})
            assert opened.status_code == 200
            server.send_signal(signal.SIGTERM)
            server.wait(timeout=60)
        finally:
            if server.poll() is None:
                server.kill()
                server.wait()
    assert exported.exists(), _tail(log)
    records = [json.loads(line) for line in exported.read_text(encoding="utf-8").splitlines()]
    summaries = [record for record in records if record.startswith("sgate1 ")]
    assert len(summaries) == 1, _tail(log)
    assert re.fullmatch(
        rf"sgate1 side=vm boot=[0-9a-f]{{8}} seq=1 part=1/1 lines=1 dropped=0\n{request_id} -",
        summaries[0],
    )


# ---- What closes on the way out ----


@pytest.fixture
async def api_pools(monkeypatch) -> AsyncIterator[list[httpx.AsyncClient]]:
    """Three shared Sugra API connection pools, one per API base, kept where get_client finds them."""
    monkeypatch.setattr(client_module, "_pools", {})
    pools = [
        client_module.shared_pool(base)
        for base in ("https://api.test", "https://api-two.test", "https://api-three.test")
    ]
    yield pools
    for pool in pools:
        # Through the class, so that a pool whose aclose a test replaced is still closed here.
        await httpx.AsyncClient.aclose(pool)


def _http_app(monkeypatch) -> Any:
    """The app `sugra-api-mcp --transport streamable-http` builds; uvicorn.run only captures it."""
    import uvicorn

    from sugra_api_mcp import __main__ as entry

    captured: dict[str, Any] = {}

    def run(app: Any, **kwargs: Any) -> None:
        captured["app"] = app

    monkeypatch.setattr(observability, "setup_observability", lambda: False)
    monkeypatch.setattr(uvicorn, "run", run)
    monkeypatch.setattr(server.mcp, "_session_manager", None)
    monkeypatch.delenv("SUGRA_AGENT_INTERNAL_TOKEN", raising=False)
    monkeypatch.setenv("SUGRA_APP_URL", "http://127.0.0.1:9")
    entry._run_server(argparse.Namespace(transport="streamable-http", host="127.0.0.1", port=0))
    return captured["app"]


async def test_the_http_server_closes_its_connections_after_the_last_summary_and_before_the_flush(
    monkeypatch, api_pools
) -> None:
    app = _http_app(monkeypatch)
    [authenticator] = [m.kwargs["authenticator"] for m in app.user_middleware if m.cls is AuthMiddleware]

    def closed() -> list[bool]:
        return [pool.is_closed for pool in api_pools] + [authenticator._http.is_closed]

    events: list[tuple[str, list[bool]]] = []
    monkeypatch.setattr(gate.default_summary, "write", lambda: events.append(("summary", closed())))

    def flush(timeout_s: float) -> bool:
        events.append(("flush", closed()))
        return True

    monkeypatch.setattr(observability, "flush_telemetry", flush)
    try:
        async with app.router.lifespan_context(app):
            assert closed() == [False] * 4
    finally:
        await authenticator.aclose()
    assert events == [("summary", [False] * 4), ("flush", [True] * 4)]
    assert client_module._pools == {}


async def test_closing_the_clients_closes_each_pool_and_forgets_it(api_pools) -> None:
    await server.close_clients()
    assert [pool.is_closed for pool in api_pools] == [True] * 3
    assert client_module._pools == {}
    # Nothing is left for a second call, and a pool asked for after it is a new one.
    await server.close_clients()
    fresh = client_module.shared_pool("https://api.test")
    assert fresh is not api_pools[0]
    assert not fresh.is_closed
    await server.close_clients()
    assert fresh.is_closed


async def test_a_pool_that_fails_to_close_leaves_no_other_open(monkeypatch, api_pools) -> None:
    async def fail() -> None:
        raise OSError("socket already gone")

    monkeypatch.setattr(api_pools[1], "aclose", fail)
    with pytest.raises(OSError, match="socket already gone"):
        await server.close_clients()
    assert [pool.is_closed for pool in api_pools] == [True, False, True]
    assert client_module._pools == {}
