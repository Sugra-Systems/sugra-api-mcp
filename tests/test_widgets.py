"""Tests for the MCP Apps price-chart widget (SEP-1865 wire contract).

Proof-oriented pins on the protocol wiring: exact ui:// URI, exact template
MIME type, the _meta.ui.resourceUri tool declaration on call_endpoint only,
self-contained HTML (no external loads), copy lint, bridge method names, and
the 32KB size budget. Visual host rendering is validated separately once a
host with the io.modelcontextprotocol/ui extension is available.

MCP-24.1: the widget is opt-in behind SUGRA_MCP_UI_WIDGETS and off by default.
The default is pinned on the global server of this test process and in a
fresh subprocess with the flag removed. The flag-on surface is pinned in a
fresh subprocess (the global server registers once per process, so flipping
it here would leak into every other test) and on explicit SugraFastMCP
instances, which are never latched and never touch the global. The template
tests need no flag.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest
from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ResourceError

from sugra_api_mcp.config import UI_WIDGETS_ENV, ui_widgets_enabled

REPO_ROOT = Path(__file__).resolve().parent.parent

WIDGET_URI = "ui://sugra/price-chart.html"
WIDGET_MIME_TYPE = "text/html;profile=mcp-app"

# The tool _meta keys that declare a UI template: the SEP-1865 nested object
# and its deprecated flat form.
UI_TEMPLATE_META_KEYS = ("ui", "ui/resourceUri")

# Commercial upstream names that must never appear in public copy.
TIER_C_NAME_FRAGMENTS = [
    "yahoo",
    "finnhub",
    "coingecko",
    "tomorrow.io",
    "alpha vantage",
    "polygon",
    "tiingo",
    "cboe",
]

# SEP-1865 bridge methods the template must speak (JSON-RPC over postMessage).
SEP_BRIDGE_METHODS = [
    "ui/initialize",
    "ui/notifications/initialized",
    "ui/notifications/tool-input",
    "ui/notifications/tool-result",
    "ui/notifications/tool-cancelled",
    "ui/notifications/size-changed",
]


@pytest.fixture()
def registered_mcp(monkeypatch):
    monkeypatch.setenv("SUGRA_API_KEY", "dummy")
    from sugra_api_mcp import tools  # noqa: F401  (import registers everything)
    from sugra_api_mcp.server import mcp

    return mcp


def _template() -> str:
    from sugra_api_mcp.tools.widgets import PRICE_CHART_TEMPLATE

    return PRICE_CHART_TEMPLATE


_FLAG_HINT = f"is {UI_WIDGETS_ENV} set in the test environment? The suite pins the default"


async def test_default_global_server_lists_no_ui_resource(registered_mcp) -> None:
    listed = [str(resource.uri) for resource in await registered_mcp.list_resources()]
    assert listed, "resources/list is empty: the fixture registered nothing"
    assert [uri for uri in listed if uri.startswith("ui://")] == [], _FLAG_HINT


async def test_default_global_server_cannot_read_the_widget(registered_mcp) -> None:
    with pytest.raises((ValueError, ResourceError), match="Unknown resource"):
        await registered_mcp.read_resource(WIDGET_URI)


def test_template_is_a_self_contained_html5_document() -> None:
    template = _template()
    assert template.lstrip().lower().startswith("<!doctype html>")

    # SEP-1865 hosts enforce a restrictive default CSP (connect-src 'none',
    # no external script/style/img origins): ban http(s) and protocol-relative
    # targets in src/href attributes; only data: URIs would be acceptable.
    for match in re.finditer(r"""(?:src|href)\s*=\s*["']([^"']*)["']""", template, re.IGNORECASE):
        target = match.group(1).strip().lower()
        assert not target.startswith(("http://", "https://", "//")), (
            f"external load in template: {match.group(0)!r}"
        )
        if ":" in target.split("/")[0]:
            assert target.startswith("data:"), f"non-data scheme in template: {match.group(0)!r}"

    # Belt and braces: no URLs or network APIs anywhere in the document.
    lowered = template.lower()
    assert "http://" not in lowered
    assert "https://" not in lowered
    for banned in (
        "<link",
        "@import",
        "fetch(",
        "xmlhttprequest",
        "websocket",
        "sendbeacon",
        "eventsource",
        "<script src",
        "importscripts",
    ):
        assert banned not in lowered, f"banned construct in template: {banned}"


def test_template_copy_lint() -> None:
    template = _template()
    assert template.isascii(), "template must be plain ASCII (no emoji, no em dash)"
    assert chr(0x2014) not in template
    lowered = template.lower()
    assert "real-time" not in lowered
    assert "realtime" not in lowered
    for fragment in TIER_C_NAME_FRAGMENTS:
        assert fragment not in lowered, f"commercial upstream name in template: {fragment}"


def test_template_speaks_the_sep_1865_bridge_protocol() -> None:
    template = _template()
    for method in SEP_BRIDGE_METHODS:
        assert f'"{method}"' in template, f"bridge method missing from template: {method}"
    assert "jsonrpc" in template
    assert '"2.0"' in template
    assert "postMessage" in template
    # Graceful fallback when the payload is not a recognizable time series.
    assert "not contain a recognizable time series" in template


def test_template_under_32kb() -> None:
    assert len(_template().encode("utf-8")) < 32 * 1024


async def test_default_global_server_declares_no_ui_template_on_any_tool(registered_mcp) -> None:
    from sugra_api_mcp.server import OAUTH_SECURITY_SCHEMES

    tool_list = await registered_mcp.list_tools()
    assert "call_endpoint" in {tool.name for tool in tool_list}
    for tool in tool_list:
        dumped = tool.model_dump(by_alias=True)
        meta = dumped.get("_meta") or {}
        for key in UI_TEMPLATE_META_KEYS:
            assert key not in meta, f"{key} declared on {tool.name}; {_FLAG_HINT}"
        # The OAuth metadata every tool carried before the opt-in, unchanged.
        assert dumped["securitySchemes"] == OAUTH_SECURITY_SCHEMES
        assert meta["securitySchemes"] == OAUTH_SECURITY_SCHEMES


def _meta(tool) -> dict:
    return tool.model_dump(by_alias=True).get("_meta") or {}


def _probe_server():
    """A fresh SugraFastMCP with call_endpoint and one other tool, off the global."""
    from sugra_api_mcp.server import SugraFastMCP

    instance = SugraFastMCP("widget-probe")

    def call_endpoint() -> dict:
        return {}

    def list_sources() -> dict:
        return {}

    instance.add_tool(call_endpoint)
    instance.add_tool(list_sources)
    return instance


# ---------------------------------------------------------------------------
# Fresh processes: the import-time path exactly as a launched server runs it.
# ---------------------------------------------------------------------------

_SURFACE_PROBE = textwrap.dedent(
    """
    import asyncio
    import json

    import sugra_api_mcp.tools
    from sugra_api_mcp.server import mcp
    from sugra_api_mcp.tools import widgets


    async def main():
        metas = {
            tool.name: tool.model_dump(by_alias=True).get("_meta") or {}
            for tool in await mcp.list_tools()
        }
        resources = {str(r.uri): r.mimeType for r in await mcp.list_resources()}
        read = None
        if widgets.PRICE_CHART_URI in resources:
            [content] = list(await mcp.read_resource(widgets.PRICE_CHART_URI))
            read = {
                "mime": content.mime_type,
                "is_template": content.content == widgets.PRICE_CHART_TEMPLATE,
            }
        again = widgets.register_ui_widgets()
        print(json.dumps({
            "metas": metas,
            "resources": resources,
            "read": read,
            "again": again,
            "latched": widgets._registered_global,
            "resource_count_after_again": len(await mcp.list_resources()),
        }))


    asyncio.run(main())
    """
)


def _fresh_process_surface(flag: str | None) -> dict:
    env = dict(os.environ)
    env.pop(UI_WIDGETS_ENV, None)
    if flag is not None:
        env[UI_WIDGETS_ENV] = flag
    result = subprocess.run(
        [sys.executable, "-c", _SURFACE_PROBE],
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
        env=env,
        timeout=120,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout.strip().splitlines()[-1])


@pytest.fixture(scope="module")
def fresh_surfaces() -> dict[str, dict]:
    return {"off": _fresh_process_surface(None), "on": _fresh_process_surface("1")}


def test_default_fresh_process_serves_no_widget(fresh_surfaces) -> None:
    from sugra_api_mcp.server import OAUTH_SECURITY_SCHEMES

    off = fresh_surfaces["off"]
    assert off["resources"], "the default process listed no resources at all"
    assert [uri for uri in off["resources"] if uri.startswith("ui://")] == []
    assert off["read"] is None
    assert "call_endpoint" in off["metas"]
    for name, meta in off["metas"].items():
        for key in UI_TEMPLATE_META_KEYS:
            assert key not in meta, f"{key} declared on {name} in a default process"
        assert meta["securitySchemes"] == OAUTH_SECURITY_SCHEMES
    # Registration refused, so the global was never latched.
    assert off["again"] is False
    assert off["latched"] is False


def test_flag_on_fresh_process_serves_the_widget_on_call_endpoint_only(fresh_surfaces) -> None:
    from sugra_api_mcp.server import OAUTH_SECURITY_SCHEMES

    on = fresh_surfaces["on"]
    assert on["resources"].get(WIDGET_URI) == WIDGET_MIME_TYPE
    assert on["read"] == {"mime": WIDGET_MIME_TYPE, "is_template": True}
    assert "call_endpoint" in on["metas"]
    for name, meta in on["metas"].items():
        # The UI declaration must not displace the OAuth metadata.
        assert meta["securitySchemes"] == OAUTH_SECURITY_SCHEMES
        if name == "call_endpoint":
            # SEP-1865 "Resource Discovery": the nested ui object, not the
            # deprecated flat "ui/resourceUri" key.
            assert meta["ui"] == {"resourceUri": WIDGET_URI}
            assert "ui/resourceUri" not in meta
        else:
            assert "ui" not in meta, f"unexpected ui template on tool {name}"
    # Idempotent for the global: a second call reports True and adds nothing.
    assert on["again"] is True
    assert on["latched"] is True
    assert on["resource_count_after_again"] == len(on["resources"])


def test_flag_on_adds_the_widget_and_changes_nothing_else(fresh_surfaces) -> None:
    off, on = fresh_surfaces["off"], fresh_surfaces["on"]
    assert set(on["resources"]) - set(off["resources"]) == {WIDGET_URI}
    assert {uri: mime for uri, mime in on["resources"].items() if uri != WIDGET_URI} == off[
        "resources"
    ]
    assert on["metas"].keys() == off["metas"].keys()
    for name, meta in on["metas"].items():
        without_ui = {key: value for key, value in meta.items() if key != "ui"}
        assert without_ui == off["metas"][name], name


# ---------------------------------------------------------------------------
# Explicit instances: the registration logic, without touching the global.
# ---------------------------------------------------------------------------


async def test_flag_on_explicit_instance_serves_and_links_the_widget(monkeypatch) -> None:
    from sugra_api_mcp.server import OAUTH_SECURITY_SCHEMES
    from sugra_api_mcp.tools.widgets import register_ui_widgets

    monkeypatch.setenv(UI_WIDGETS_ENV, "1")
    instance = _probe_server()
    assert register_ui_widgets(instance) is True

    metas = {tool.name: _meta(tool) for tool in await instance.list_tools()}
    assert metas["call_endpoint"]["ui"] == {"resourceUri": WIDGET_URI}
    assert metas["call_endpoint"]["securitySchemes"] == OAUTH_SECURITY_SCHEMES
    assert "ui" not in metas["list_sources"]
    assert metas["list_sources"]["securitySchemes"] == OAUTH_SECURITY_SCHEMES

    listed = {str(resource.uri): resource.mimeType for resource in await instance.list_resources()}
    assert listed == {WIDGET_URI: WIDGET_MIME_TYPE}
    contents = list(await instance.read_resource(WIDGET_URI))
    assert len(contents) == 1
    assert contents[0].mime_type == WIDGET_MIME_TYPE
    assert contents[0].content == _template()


@pytest.mark.parametrize("raw", [None, "", "0", "off"])
async def test_flag_off_explicit_instance_serves_and_links_nothing(monkeypatch, raw) -> None:
    from sugra_api_mcp.tools.widgets import register_ui_widgets

    if raw is None:
        monkeypatch.delenv(UI_WIDGETS_ENV, raising=False)
    else:
        monkeypatch.setenv(UI_WIDGETS_ENV, raw)
    instance = _probe_server()
    assert register_ui_widgets(instance) is False
    assert await instance.list_resources() == []
    for tool in await instance.list_tools():
        assert "ui" not in _meta(tool), tool.name


async def test_explicit_instances_are_never_latched_and_leave_the_global_alone(
    monkeypatch, registered_mcp
) -> None:
    from sugra_api_mcp.tools import widgets

    monkeypatch.setenv(UI_WIDGETS_ENV, "1")
    first, second = _probe_server(), _probe_server()
    assert widgets.register_ui_widgets(first) is True
    assert widgets.register_ui_widgets(second) is True
    for instance in (first, second):
        assert WIDGET_URI in {str(resource.uri) for resource in await instance.list_resources()}
    assert widgets._registered_global is False
    assert WIDGET_URI not in {str(resource.uri) for resource in await registered_mcp.list_resources()}
    for tool in await registered_mcp.list_tools():
        assert "ui" not in _meta(tool), tool.name


async def test_plain_fastmcp_instance_gets_the_resource_only(monkeypatch) -> None:
    from sugra_api_mcp.tools.widgets import register_ui_widgets

    monkeypatch.setenv(UI_WIDGETS_ENV, "yes")
    instance = FastMCP("plain-widget-probe")
    assert register_ui_widgets(instance) is True
    assert {str(resource.uri) for resource in await instance.list_resources()} == {WIDGET_URI}


async def test_link_ui_template_refuses_a_resource_the_server_does_not_serve() -> None:
    instance = _probe_server()
    with pytest.raises(ValueError, match="unregistered UI template"):
        instance.link_ui_template(WIDGET_URI)
    for tool in await instance.list_tools():
        assert "ui" not in _meta(tool), tool.name


# ---------------------------------------------------------------------------
# SUGRA_MCP_UI_WIDGETS parsing table.
# ---------------------------------------------------------------------------


def test_flag_name_is_the_documented_one() -> None:
    assert UI_WIDGETS_ENV == "SUGRA_MCP_UI_WIDGETS"


def test_flag_unset_is_off(monkeypatch) -> None:
    monkeypatch.delenv(UI_WIDGETS_ENV, raising=False)
    assert ui_widgets_enabled() is False


@pytest.mark.parametrize(
    "raw",
    ["1", "true", "yes", "on", "TRUE", "True", "YES", "On", "oN", " 1", "true ", "  yes  ", "\ton\n"],
)
def test_flag_truthy_values_turn_it_on(monkeypatch, raw) -> None:
    monkeypatch.setenv(UI_WIDGETS_ENV, raw)
    assert ui_widgets_enabled() is True


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "   ",
        "0",
        "false",
        "no",
        "off",
        "FALSE",
        "Off",
        "2",
        "-1",
        "1.0",
        "y",
        "t",
        "enabled",
        "truthy",
        "yes please",
        "on,",
        "o n",
        "none",
        "null",
    ],
)
def test_every_other_value_is_off(monkeypatch, raw) -> None:
    monkeypatch.setenv(UI_WIDGETS_ENV, raw)
    assert ui_widgets_enabled() is False
