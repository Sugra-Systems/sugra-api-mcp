# CHANGE - MCP-24.1 (the price-chart widget is opt-in behind SUGRA_MCP_UI_WIDGETS)

risk-tier: T1
board: https://github.com/Sugra-Systems/sugra-board/blob/main/cards/MCP-24.1.md

## Problem

`tools/widgets.py` registered the MCP Apps price-chart template (`ui://sugra/price-chart.html`, MIME `text/html;profile=mcp-app`) with `@mcp.resource` at import, and `SugraFastMCP.list_tools` attached `_meta.ui = {"resourceUri": ...}` to `call_endpoint` on every server, unconditionally. A directory scan of the hosted server therefore imported a UI template. The widget is not ready for that review: it renders a "Price chart" fallback card for most payloads, draws newest-first series backwards, and declares neither `_meta.ui.domain` nor a CSP. App directories also expect only a dedicated render tool to carry a template, and `call_endpoint` is a general gateway tool.

The published directory listing (version 1.0.0) was captured with no UI template on any tool, so the new default matches that snapshot.

## Change

- `config.py`: `UI_WIDGETS_ENV = "SUGRA_MCP_UI_WIDGETS"` and `ui_widgets_enabled()`. On only for `1`, `true`, `yes`, `on`, in any case and with surrounding whitespace ignored. Unset, empty and every other value mean off. Default off.
- `tools/widgets.py`: the resource is no longer registered by a decorator. `register_ui_widgets(instance=None)` follows `register_agent_tools`: it returns False and registers nothing when the flag is off; when on, it registers the template on the target and, for a `SugraFastMCP` target, links it to `call_endpoint`. It is idempotent for the global server (a latch) and never latches or touches the global for an explicit instance. The module calls it once at import, which is how the tools package has always registered.
- `server.py`: `SugraFastMCP` holds the linked template URI (None by default) and gains `link_ui_template(uri)`, which refuses a URI that is not a registered resource of that server. `list_tools` attaches `_meta.ui` to `UI_TEMPLATE_TOOL` only when a template is linked. `_with_ui_template` takes the URI as an argument and no longer imports `tools/widgets.py` lazily.
- `FACTS.md`: resources are 8 by default; the widget is described as opt-in behind `SUGRA_MCP_UI_WIDGETS`.

When the flag is read, and why the two halves agree. The flag is read once, by `register_ui_widgets`, when the tools package is imported. That is process start on both transports: `__main__` imports the tools after the environment is loaded, and the hosted service gets its environment from its unit file before the process starts. `list_tools` does not read the flag at all. It attaches only what registration linked, and registration links only after it registered the resource on the same server, so the tool declaration and `resources/list` agree by construction instead of by reading the environment twice. A change to the flag takes a restart, like `SUGRA_MCP_ALLOWED_HOSTS`, which is also read at import.

## Not changed

- No tool is added, removed or renamed. Tool input schemas, annotations and descriptions are untouched, including the `call_endpoint` and `fetch_data` body, which stays object or null.
- OAuth `securitySchemes` on every tool, top level and in `_meta`, are exactly as before in both flag states.
- With the flag on the surface is identical to the previous one: `call_endpoint` carries the nested `ui` object, no other tool does, and the resource lists and reads back with MIME `text/html;profile=mcp-app`.
- `PRICE_CHART_TEMPLATE` and its tests (self-contained HTML, copy lint, bridge methods, size budget) are unchanged and need no flag.
- The widget's rendering defects are not fixed here; they are the reason it is off.
- Hosted-only agent tools, prompts, the sugra:// resources, auth and transports.

## Test evidence

`tests/test_widgets.py`:

- Default on the global server of the test process: no `ui://` entry in `resources/list`, reading the widget URI fails as an unknown resource, and no tool carries `ui` or `ui/resourceUri` in `_meta` while every tool keeps its `securitySchemes`.
- Fresh subprocess with the flag removed: the same default surface through the real import path, and a second `register_ui_widgets()` returns False without latching.
- Fresh subprocess with `SUGRA_MCP_UI_WIDGETS=1`: the resource lists with the MCP Apps MIME and reads back the template, `call_endpoint` carries `{"resourceUri": "ui://sugra/price-chart.html"}` and no other tool carries `ui`, OAuth metadata intact, a second call is a latched no-op. Compared with the default process, the only difference is the widget resource and the `ui` key on `call_endpoint`.
- Explicit `SugraFastMCP` instances: flag on registers, links and reads back; flag unset, empty, `0` or `off` registers and links nothing; two explicit registrations never latch and leave the global without the widget; a plain `FastMCP` instance gets the resource only; `link_ui_template` refuses an unregistered URI.
- Parsing table: unset is off; `1`, `true`, `yes`, `on` in mixed case and with surrounding whitespace are on; empty, blank, `0`, `false`, `no`, `off`, `2`, `-1`, `1.0`, `y`, `t`, `enabled`, `yes please`, `on,` and similar are off.

`tests/test_resources.py` expects the 8 default resources; `tests/test_keyless.py` expects 8 resources and removes the flag from its subprocess environment so a runner's environment cannot change the default surface.

Mutation evidence: one mutant per run, applied by a scratch script that restores the file from an in-memory byte copy and compares it byte for byte, running `tests/test_widgets.py`, `tests/test_resources.py` and `tests/test_keyless.py`. All 14 were killed:

- default flipped to on;
- `list_tools` attaching the template regardless of the flag;
- the resource registered regardless of the flag, with the link still gated;
- truthy parsing accepting any non-empty string;
- the template attached to every tool;
- the link guard removed;
- an explicit instance latching the global;
- the global never latched;
- registration always onto the global;
- case-sensitive parsing;
- whitespace not stripped;
- `on` missing from the truthy set;
- the link made before the resource exists;
- the flag on but the template never linked.

Full suite 698 passed; `ruff check sugra_api_mcp tests scripts` and `git diff --check` clean.

## Review

Independent review round 1 pending. Records: `ops/reviews/MCP-24.1/REVIEW-*.md`.
