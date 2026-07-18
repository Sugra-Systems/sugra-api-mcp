"""MCP Apps price-chart widget (SEP-1865, extension io.modelcontextprotocol/ui).

Implementation reference - wire contract extracted from SEP-1865
("MCP Apps - Interactive User Interfaces for MCP", Final, Extensions Track)
and the full specification at modelcontextprotocol/ext-apps
specification/draft/apps.mdx:

- "UI Resource Format": UI templates are predeclared MCP resources whose URI
  MUST use the ``ui://`` scheme (example in spec:
  ``ui://weather-server/dashboard-template``). The MIME type for HTML-based
  UIs is ``text/html;profile=mcp-app`` (the only content type in the MVP).
  Content MUST be a valid HTML5 document, delivered via ``resources/read``
  as UTF-8 text or base64 blob.
- "Resource Discovery": a tool declares its template through tool metadata
  ``_meta.ui.resourceUri`` (an ``ui`` object on ``_meta`` with an optional
  ``visibility: ["model" | "app"]``). The flat ``_meta["ui/resourceUri"]``
  form is explicitly deprecated. Wiring for this server lives in
  ``sugra_api_mcp.server`` (SugraFastMCP.list_tools), which attaches the
  declaration to the ``call_endpoint`` tool only.
- "Communication Protocol" / "Lifecycle": the iframe talks to the host with
  standard MCP JSON-RPC over ``postMessage``. Handshake: the app sends a
  ``ui/initialize`` request, the host responds, then the app sends the
  ``ui/notifications/initialized`` notification.
- "MCP Apps Specific Messages": host-to-app notifications
  ``ui/notifications/tool-input`` (full arguments),
  ``ui/notifications/tool-input-partial`` (streamed arguments),
  ``ui/notifications/tool-result`` (execution result),
  ``ui/notifications/tool-cancelled`` (aborted). App-to-host requests
  include ``ui/open-link``, ``ui/message``, ``ui/request-display-mode``,
  ``ui/update-model-context``; app-to-host notifications include
  ``ui/notifications/size-changed``. The app may also call server tools via
  ``tools/call`` through the host.
- "Extension Identifier" / "Client<>Server Capability Negotiation": the
  extension id is ``io.modelcontextprotocol/ui``; the host advertises it in
  ``initialize`` under ``capabilities.extensions`` with the supported
  ``mimeTypes`` (``["text/html;profile=mcp-app"]``).
- Security model: templates render in sandboxed iframes; when no CSP
  metadata is declared the host enforces a restrictive default CSP with
  ``connect-src 'none'`` and no external script/style/img origins. The
  template below is therefore fully self-contained: inline CSS, vanilla JS,
  hand-rolled inline SVG chart, zero external requests.

Importing this module registers the template resource against the global
FastMCP singleton, mirroring how the other tool modules register.
"""

from __future__ import annotations

from ..server import mcp

PRICE_CHART_URI = "ui://sugra/price-chart.html"

# SEP-1865 "UI Resource Format": the profile parameter marks the document as
# an MCP Apps template rather than a plain web page.
PRICE_CHART_MIME_TYPE = "text/html;profile=mcp-app"

PRICE_CHART_TEMPLATE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Sugra price chart</title>
<style>
  :root {
    --bg: #0B0F1A;
    --panel: #101623;
    --grid: #1F2937;
    --text: #E5E7EB;
    --muted: #8B93A7;
    --amber: #F5A623;
  }
  * { margin: 0; padding: 0; box-sizing: border-box; }
  html, body { background: var(--bg); }
  body {
    color: var(--text);
    font: 14px/1.5 system-ui, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
    padding: 16px;
  }
  .card {
    background: var(--panel);
    border: 1px solid var(--grid);
    border-radius: 10px;
    padding: 16px;
    max-width: 720px;
  }
  .head {
    display: flex;
    align-items: baseline;
    justify-content: space-between;
    gap: 12px;
    margin-bottom: 4px;
  }
  .title { font-size: 15px; font-weight: 600; letter-spacing: 0.01em; }
  .title .accent { color: var(--amber); }
  .last {
    font-size: 15px;
    font-weight: 600;
    color: var(--amber);
    font-variant-numeric: tabular-nums;
  }
  .sub {
    color: var(--muted);
    font-size: 12px;
    margin-bottom: 12px;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }
  .fallback {
    color: var(--muted);
    font-size: 13px;
    padding: 28px 8px;
    text-align: center;
  }
  svg { display: block; width: 100%; height: auto; }
  .axis { fill: var(--muted); font-size: 10px; }
  .foot {
    display: flex;
    justify-content: space-between;
    color: var(--muted);
    font-size: 11px;
    margin-top: 10px;
  }
</style>
</head>
<body>
<div class="card">
  <div class="head">
    <div class="title">Price <span class="accent">chart</span></div>
    <div class="last" id="last-value"></div>
  </div>
  <div class="sub" id="subtitle">Sugra API tool result</div>
  <div id="chart">
    <div class="fallback">Waiting for data from the host.</div>
  </div>
  <div class="foot"><span id="range-start"></span><span id="range-end"></span></div>
</div>
<script>
(function () {
  "use strict";

  /* Minimal MCP Apps bridge (SEP-1865): JSON-RPC over postMessage with the
     host. No other communication channel exists; the template never issues
     network requests of its own. */

  var initializeId = 1;
  var initialized = false;

  function post(message) {
    if (window.parent && window.parent !== window) {
      window.parent.postMessage(message, "*");
    }
  }

  function notify(method, params) {
    post({ jsonrpc: "2.0", method: method, params: params || {} });
  }

  window.addEventListener("message", function (event) {
    // Only the embedding host may talk to this app: reject messages whose
    // source is not the parent window (codex review: prevents init and
    // tool-result spoofing from sibling or injected frames). The parent
    // origin is host-specific and unknown at build time, so the SOURCE
    // identity check is the reliable gate.
    if (event.source !== window.parent) { return; }
    var msg = event.data;
    if (!msg || msg.jsonrpc !== "2.0") { return; }
    if (!initialized && msg.id === initializeId && msg.method === undefined) {
      /* Response to ui/initialize: complete the handshake. */
      initialized = true;
      notify("ui/notifications/initialized");
      return;
    }
    if (msg.method === "ui/notifications/tool-input") {
      handleToolInput(msg.params || {});
    } else if (msg.method === "ui/notifications/tool-result") {
      handleToolResult(msg.params || {});
    } else if (msg.method === "ui/notifications/tool-cancelled") {
      showFallback("The tool call was cancelled by the host.");
    }
  });

  post({
    jsonrpc: "2.0",
    id: initializeId,
    method: "ui/initialize",
    params: {
      appInfo: { name: "sugra-price-chart", version: "1.0.0" },
      appCapabilities: {}
    }
  });

  function reportSize() {
    notify("ui/notifications/size-changed", {
      width: document.documentElement.scrollWidth,
      height: document.documentElement.scrollHeight
    });
  }

  function byId(id) { return document.getElementById(id); }

  function setText(id, value) { byId(id).textContent = value == null ? "" : String(value); }

  function escapeHtml(value) {
    return String(value).replace(/[&<>"']/g, function (ch) {
      return "&#" + ch.charCodeAt(0) + ";";
    });
  }

  function showFallback(message) {
    byId("chart").innerHTML =
      '<div class="fallback">' + escapeHtml(message) + "</div>";
    setText("last-value", "");
    setText("range-start", "");
    setText("range-end", "");
    reportSize();
  }

  function handleToolInput(params) {
    var args = params.arguments || params.input || params || {};
    if (args && args.operation_id) {
      setText("subtitle", "call_endpoint " + args.operation_id);
    }
  }

  /* --- payload extraction ------------------------------------------- */

  function extractPayload(params) {
    var result = params.result || params.toolResult || params;
    if (result && typeof result === "object") {
      if (result.structuredContent) { return result.structuredContent; }
      if (Object.prototype.toString.call(result.content) === "[object Array]") {
        for (var i = 0; i < result.content.length; i++) {
          var item = result.content[i];
          if (item && item.type === "text" && typeof item.text === "string") {
            try { return JSON.parse(item.text); } catch (err) { /* not JSON */ }
          }
        }
      }
    }
    return result;
  }

  var TIME_KEYS = ["date", "time", "timestamp", "datetime", "period", "day"];
  var VALUE_KEYS = ["close", "price", "value", "adj_close", "last", "rate", "level", "open"];

  function pickKey(obj, candidates, numeric) {
    for (var i = 0; i < candidates.length; i++) {
      var key = candidates[i];
      if (!(key in obj)) { continue; }
      var v = obj[key];
      if (numeric) {
        if (typeof v === "number" && isFinite(v)) { return key; }
        if (typeof v === "string" && v !== "" && isFinite(Number(v))) { return key; }
      } else if (typeof v === "string" || typeof v === "number") {
        return key;
      }
    }
    return null;
  }

  function seriesFromArray(arr) {
    if (Object.prototype.toString.call(arr) !== "[object Array]" || arr.length < 2) {
      return null;
    }
    var first = arr[0];
    if (!first || typeof first !== "object") { return null; }
    if (Object.prototype.toString.call(first) === "[object Array]") {
      /* [t, v] pairs */
      if (first.length < 2 || !isFinite(Number(first[1]))) { return null; }
      var pairs = [];
      for (var p = 0; p < arr.length; p++) {
        var row = arr[p];
        if (row && row.length >= 2 && isFinite(Number(row[1]))) {
          pairs.push({ t: String(row[0]), v: Number(row[1]) });
        }
      }
      return pairs.length >= 2 ? pairs : null;
    }
    var timeKey = pickKey(first, TIME_KEYS, false);
    var valueKey = pickKey(first, VALUE_KEYS, true);
    if (!timeKey || !valueKey) { return null; }
    var points = [];
    for (var i = 0; i < arr.length; i++) {
      var item = arr[i];
      if (!item || typeof item !== "object") { continue; }
      var v = Number(item[valueKey]);
      if (item[timeKey] != null && isFinite(v)) {
        points.push({ t: String(item[timeKey]), v: v });
      }
    }
    return points.length >= 2 ? points : null;
  }

  function findSeries(node, depth) {
    if (node == null || depth > 6) { return null; }
    var direct = seriesFromArray(node);
    if (direct) { return direct; }
    if (Object.prototype.toString.call(node) === "[object Array]") {
      for (var i = 0; i < node.length && i < 25; i++) {
        var hit = findSeries(node[i], depth + 1);
        if (hit) { return hit; }
      }
      return null;
    }
    if (typeof node === "object") {
      for (var key in node) {
        if (Object.prototype.hasOwnProperty.call(node, key)) {
          var found = findSeries(node[key], depth + 1);
          if (found) { return found; }
        }
      }
    }
    return null;
  }

  /* --- rendering ----------------------------------------------------- */

  function formatValue(v) {
    var abs = Math.abs(v);
    if (abs >= 1000) { return v.toFixed(0); }
    if (abs >= 1) { return v.toFixed(2); }
    return v.toPrecision(3);
  }

  function render(points) {
    var W = 640, H = 300, padL = 54, padR = 16, padT = 14, padB = 24;
    var innerW = W - padL - padR;
    var innerH = H - padT - padB;
    var min = Infinity, max = -Infinity;
    for (var i = 0; i < points.length; i++) {
      if (points[i].v < min) { min = points[i].v; }
      if (points[i].v > max) { max = points[i].v; }
    }
    if (min === max) { min -= 1; max += 1; }
    var span = max - min;
    min -= span * 0.06;
    max += span * 0.06;
    span = max - min;

    function x(i) { return padL + (innerW * i) / (points.length - 1); }
    function y(v) { return padT + innerH * (1 - (v - min) / span); }

    var coords = [];
    for (var j = 0; j < points.length; j++) {
      coords.push(x(j).toFixed(1) + "," + y(points[j].v).toFixed(1));
    }
    var line = coords.join(" ");
    var area = "M" + coords.join(" L") +
      " L" + x(points.length - 1).toFixed(1) + "," + (padT + innerH).toFixed(1) +
      " L" + x(0).toFixed(1) + "," + (padT + innerH).toFixed(1) + " Z";

    var svg = '<svg viewBox="0 0 ' + W + " " + H + '" role="img" aria-label="Price line chart">';
    svg += '<defs><linearGradient id="fill" x1="0" y1="0" x2="0" y2="1">';
    svg += '<stop offset="0" stop-color="#F5A623" stop-opacity="0.22"></stop>';
    svg += '<stop offset="1" stop-color="#F5A623" stop-opacity="0"></stop>';
    svg += "</linearGradient></defs>";

    var ticks = 4;
    for (var t = 0; t <= ticks; t++) {
      var tv = min + (span * t) / ticks;
      var ty = y(tv);
      svg += '<line x1="' + padL + '" y1="' + ty.toFixed(1) + '" x2="' + (W - padR) +
        '" y2="' + ty.toFixed(1) + '" stroke="#1F2937" stroke-width="1"></line>';
      svg += '<text class="axis" x="' + (padL - 8) + '" y="' + (ty + 3).toFixed(1) +
        '" text-anchor="end">' + escapeHtml(formatValue(tv)) + "</text>";
    }

    svg += '<path d="' + area + '" fill="url(#fill)" stroke="none"></path>';
    svg += '<polyline points="' + line +
      '" fill="none" stroke="#F5A623" stroke-width="2" stroke-linejoin="round" stroke-linecap="round"></polyline>';

    var lastPt = points[points.length - 1];
    svg += '<circle cx="' + x(points.length - 1).toFixed(1) + '" cy="' + y(lastPt.v).toFixed(1) +
      '" r="3.5" fill="#F5A623"></circle>';
    svg += "</svg>";

    byId("chart").innerHTML = svg;
    setText("last-value", formatValue(lastPt.v));
    setText("range-start", points[0].t);
    setText("range-end", lastPt.t);
    reportSize();
  }

  function handleToolResult(params) {
    var payload;
    try {
      payload = extractPayload(params);
    } catch (err) {
      showFallback("Could not read the tool result payload.");
      return;
    }
    if (payload && typeof payload === "object" && payload.error) {
      showFallback("The tool returned an error: " + payload.error);
      return;
    }
    var series = findSeries(payload, 0);
    if (!series) {
      showFallback("The tool result does not contain a recognizable time series.");
      return;
    }
    render(series);
  }

  reportSize();
})();
</script>
</body>
</html>
"""


@mcp.resource(
    PRICE_CHART_URI,
    name="price_chart_widget",
    title="Price chart widget",
    description=(
        "Self-contained MCP Apps HTML template (SEP-1865) that renders a line "
        "chart from a call_endpoint time-series result. Inline CSS and JS "
        "only - the template makes no external requests."
    ),
    mime_type=PRICE_CHART_MIME_TYPE,
)
def price_chart_widget() -> str:
    """The price-chart UI template as an HTML5 document."""
    return PRICE_CHART_TEMPLATE
