"""Tests for the MCP Apps price-chart widget (SEP-1865 wire contract).

Proof-oriented pins on the protocol wiring: exact ui:// URI, exact template
MIME type, the _meta.ui.resourceUri tool declaration on call_endpoint only,
self-contained HTML (no external loads), copy lint, bridge method names, and
the 32KB size budget. Visual host rendering is validated separately once a
host with the io.modelcontextprotocol/ui extension is available.
"""

from __future__ import annotations

import re

import pytest

WIDGET_URI = "ui://sugra/price-chart.html"
WIDGET_MIME_TYPE = "text/html;profile=mcp-app"

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


async def test_widget_resource_registered_with_exact_uri_and_mime(registered_mcp) -> None:
    resource_list = await registered_mcp.list_resources()
    by_uri = {str(resource.uri): resource for resource in resource_list}
    assert WIDGET_URI in by_uri, f"ui:// template missing from resources/list: {sorted(by_uri)}"
    assert by_uri[WIDGET_URI].mimeType == WIDGET_MIME_TYPE


async def test_widget_resource_reads_back_the_template(registered_mcp) -> None:
    contents = list(await registered_mcp.read_resource(WIDGET_URI))
    assert len(contents) == 1
    content = contents[0]
    assert content.mime_type == WIDGET_MIME_TYPE
    assert content.content == _template()


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


async def test_call_endpoint_carries_ui_template_meta_and_others_do_not(registered_mcp) -> None:
    from sugra_api_mcp.server import OAUTH_SECURITY_SCHEMES

    tool_list = await registered_mcp.list_tools()
    assert tool_list
    seen_call_endpoint = False
    for tool in tool_list:
        dumped = tool.model_dump(by_alias=True)
        meta = dumped.get("_meta") or {}
        if tool.name == "call_endpoint":
            seen_call_endpoint = True
            # SEP-1865 "Resource Discovery": the nested ui object, not the
            # deprecated flat "ui/resourceUri" key.
            assert meta["ui"] == {"resourceUri": WIDGET_URI}
            assert "ui/resourceUri" not in meta
            # The UI declaration must not displace the OAuth metadata.
            assert meta["securitySchemes"] == OAUTH_SECURITY_SCHEMES
        else:
            assert "ui" not in meta, f"unexpected ui template on tool {tool.name}"
    assert seen_call_endpoint
