"""Public web surface for the hosted HTTP transport: landing page and health.

Two unauthenticated GET routes registered ONLY by the HTTP entry point
(stdio installs never serve them): a minimal human-facing landing on the
host root and a liveness probe on /health. Everything else on the app stays
behind AuthMiddleware. The auth-side allowlist lives in
sugra_api_mcp.auth.PUBLIC_GET_PATHS - the two lists must stay in sync.

The landing carries one install tab per client. A command or config names
the API key only through the SUGRA_API_KEY environment variable (or, in VS
Code, an input prompt): a key is never written into anything on this page.
"""

from __future__ import annotations

import base64
import html
import json
from urllib.parse import quote

from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse

from . import __version__

ENDPOINT = "https://mcp.sugra.ai/mcp"
SKILLS_REPO = "Sugra-Systems/sugra-api-skills"

# Cursor resolves ${env:NAME} in url and headers when it starts the server.
CURSOR_CONFIG = {
    "url": ENDPOINT,
    "headers": {"Authorization": "Bearer ${env:SUGRA_API_KEY}"},
}
CURSOR_INSTALL_LINK = (
    "cursor://anysphere.cursor-deeplink/mcp/install?name=sugra&config="
    + quote(
        base64.b64encode(
            json.dumps(CURSOR_CONFIG, separators=(",", ":")).encode()
        ).decode(),
        safe="",
    )
)

_KEY_HEADER = '--header "Authorization: Bearer $SUGRA_API_KEY"'

_CURSOR_JSON = json.dumps({"mcpServers": {"sugra": CURSOR_CONFIG}}, indent=2)

_VSCODE_JSON = json.dumps(
    {
        "inputs": [
            {
                "type": "promptString",
                "id": "sugra-api-key",
                "description": "Sugra API key",
                "password": True,
            }
        ],
        "servers": {
            "sugra": {
                "type": "http",
                "url": ENDPOINT,
                "headers": {"Authorization": "Bearer ${input:sugra-api-key}"},
            }
        },
    },
    indent=2,
)


def _command(ident: str, text: str, *, scroll: bool = False) -> str:
    """A copyable block; the Copy button copies the block's text verbatim.

    A command wraps on a narrow screen; a JSON config scrolls instead, since
    wrapping it mid-token makes the structure unreadable.
    """
    kind = ' class="scroll"' if scroll else ""
    return (
        f'<div class="cmd"><pre id="{ident}"{kind}>{html.escape(text, quote=False)}</pre>'
        f'<button type="button" class="copy" data-copy="{ident}">Copy</button></div>'
    )


def _step(label: str) -> str:
    return f'<p class="step">{label}</p>'


_SKILLS_NOTE = (
    '<p class="note">The skills teach the agent how to find, call and cite '
    "Sugra endpoints.</p>"
)

_KEY_NOTE = (
    '<p class="note">Reads your API key from the <code>SUGRA_API_KEY</code> '
    'environment variable. No key yet? <a href="https://app.sugra.ai/register">'
    "Get API key</a></p>"
)

# (tab id, tab label, panel HTML). The first tab is open by default.
_TABS: list[tuple[str, str, str]] = [
    (
        "claude",
        "Claude",
        "<p>Sugra API is listed in Anthropic's Connectors Directory.</p>"
        '<a href="https://url.sugra.ai/claude" class="btn"'
        " title=\"Sugra API in Anthropic's Connectors Directory\">Add to Claude</a>",
    ),
    (
        "chatgpt",
        "ChatGPT",
        "<p>Sugra API is listed in the OpenAI Plugins Directory.</p>"
        '<a href="https://url.sugra.ai/openai" class="btn"'
        ' title="Sugra API in the OpenAI Plugins Directory">Add to ChatGPT</a>',
    ),
    (
        "claude-code",
        "Claude Code",
        _step("Server")
        + _command(
            "cmd-claude-code",
            f"claude mcp add --transport http sugra {ENDPOINT} {_KEY_HEADER}",
        )
        + _KEY_NOTE
        + _step("Skills (optional)")
        + _command(
            "cmd-claude-code-skills",
            f"claude plugin marketplace add {SKILLS_REPO}\n"
            "claude plugin install sugra-api@sugra-api-skills",
        )
        + _SKILLS_NOTE,
    ),
    (
        "codex",
        "Codex",
        _step("Server")
        + _command(
            "cmd-codex",
            f"codex mcp add sugra --url {ENDPOINT} --bearer-token-env-var SUGRA_API_KEY",
        )
        + _KEY_NOTE
        + _step("Skills (optional)")
        + _command(
            "cmd-codex-skills",
            f"codex plugin marketplace add {SKILLS_REPO}\n"
            "codex plugin add sugra-api@sugra-api-skills",
        )
        + _SKILLS_NOTE,
    ),
    (
        "grok",
        "Grok",
        _step("Server")
        + _command(
            "cmd-grok",
            f"grok mcp add --transport http sugra {ENDPOINT} {_KEY_HEADER}",
        )
        + _KEY_NOTE
        + _step("Skills (optional)")
        + _command(
            "cmd-grok-skills",
            f"grok plugin install {SKILLS_REPO}#plugins/sugra-api",
        )
        + _SKILLS_NOTE,
    ),
    (
        "gemini",
        "Gemini CLI",
        _step("Server")
        + _command(
            "cmd-gemini",
            f"gemini mcp add --transport http sugra {ENDPOINT} {_KEY_HEADER}",
        )
        + _KEY_NOTE,
    ),
    (
        "cursor",
        "Cursor",
        f'<a href="{CURSOR_INSTALL_LINK}" class="btn">Add to Cursor</a>'
        + _KEY_NOTE
        + _step("Or add to <code>~/.cursor/mcp.json</code>")
        + _command("cfg-cursor", _CURSOR_JSON, scroll=True),
    ),
    (
        "vscode",
        "VS Code",
        _step("Add to <code>.vscode/mcp.json</code>")
        + _command("cfg-vscode", _VSCODE_JSON, scroll=True)
        + '<p class="note">VS Code asks for the key when the server starts.</p>',
    ),
    (
        "other",
        "Other",
        "<p>Any MCP client that supports remote servers.</p>"
        + _step("URL")
        + _command("cfg-url", ENDPOINT)
        + _step("Header")
        + _command("cfg-header", "Authorization: Bearer <your Sugra API key>"),
    ),
]


def _tabs_css() -> str:
    rules = []
    for ident, _label, _panel in _TABS:
        rules.append(
            f"#t-{ident}:checked ~ .labels label[for=t-{ident}] "
            "{ color: #0F1117; background: #F5A623; border-color: #F5A623; }"
        )
        rules.append(
            f"#t-{ident}:focus-visible ~ .labels label[for=t-{ident}] "
            "{ outline: 2px solid #F5A623; outline-offset: 2px; }"
        )
        rules.append(f"#t-{ident}:checked ~ #p-{ident} {{ display: block; }}")
    return "\n  ".join(rules)


def _tabs_html() -> str:
    radios = "\n    ".join(
        f'<input type="radio" name="client" id="t-{ident}"'
        f'{" checked" if index == 0 else ""}>'
        for index, (ident, _label, _panel) in enumerate(_TABS)
    )
    labels = "\n      ".join(
        f'<label for="t-{ident}">{label}</label>' for ident, label, _panel in _TABS
    )
    panels = "\n    ".join(
        f'<section class="panel" id="p-{ident}" aria-label="{label}">{panel}</section>'
        for ident, label, panel in _TABS
    )
    return (
        f'<div class="tabs">\n    {radios}\n'
        f'    <div class="labels" role="group" aria-label="Client">\n      {labels}\n    </div>\n'
        f"    {panels}\n  </div>"
    )


_LANDING_HTML = f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Sugra API MCP</title>
<meta name="description" content="Connect Claude, ChatGPT, Claude Code, Codex, Grok, Gemini CLI, Cursor and VS Code to Sugra API over the Model Context Protocol.">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=DM+Mono&family=DM+Sans:wght@400;600&family=DM+Serif+Display&display=swap">
<style>
  body {{ margin: 0; min-height: 100vh; display: flex; justify-content: center;
         background: #0F1117; color: #EDF0F4;
         font-family: 'DM Sans', system-ui, sans-serif; }}
  main {{ width: 100%; max-width: 50rem; padding: 3rem 1rem; box-sizing: border-box; }}
  header {{ text-align: center; }}
  img {{ width: 72px; height: 72px; }}
  h1 {{ font-family: 'DM Serif Display', Georgia, serif; font-weight: 400;
        font-size: 2rem; margin: 1rem 0 0.4rem; }}
  p {{ color: #9AA3B2; line-height: 1.55; margin: 0.4rem 0 1rem; }}
  .tabs {{ margin-top: 1.8rem; }}
  .tabs input {{ position: absolute; opacity: 0; pointer-events: none; }}
  .labels {{ display: flex; flex-wrap: wrap; gap: 0.4rem; margin-bottom: 1rem; }}
  .labels label {{ cursor: pointer; border: 1px solid #1E2330; background: #161B27;
                   color: #EDF0F4; border-radius: 999px; padding: 0.35rem 0.75rem;
                   font-size: 0.85rem; }}
  .labels label:hover {{ border-color: #F5A623; }}
  .panel {{ display: none; background: #161B27; border: 1px solid #1E2330;
            border-radius: 12px; padding: 1.2rem; }}
  .panel p {{ margin: 0 0 0.8rem; }}
  .panel .step {{ font-family: 'DM Mono', ui-monospace, monospace; font-size: 0.75rem;
                  text-transform: uppercase; letter-spacing: 0.08em; color: #9AA3B2;
                  margin: 1rem 0 0.4rem; }}
  .panel .step:first-child {{ margin-top: 0; }}
  .panel .step code {{ text-transform: none; letter-spacing: 0; }}
  .panel .note {{ font-size: 0.85rem; margin: 0.8rem 0 0; }}
  .btn {{ display: inline-block; background: #F5A623; color: #0F1117; font-weight: 600;
          text-decoration: none; border-radius: 8px; padding: 0.6rem 1.2rem; }}
  .btn:hover {{ opacity: 0.9; }}
  .cmd {{ position: relative; }}
  pre {{ margin: 0; background: #0F1117; border: 1px solid #1E2330; border-radius: 8px;
         padding: 0.8rem 4.5rem 0.8rem 0.9rem; color: #F5A623;
         font-family: 'DM Mono', ui-monospace, monospace; font-size: 0.85rem;
         line-height: 1.5; white-space: pre-wrap; overflow-wrap: anywhere; }}
  pre.scroll {{ white-space: pre; overflow-wrap: normal; overflow-x: auto; }}
  @media (max-width: 30rem) {{
    .panel {{ padding: 0.9rem; }}
    pre {{ font-size: 0.78rem; }}
  }}
  .copy {{ display: none; position: absolute; top: 0.45rem; right: 0.45rem;
           background: #1E2330; color: #EDF0F4; border: 1px solid #2A3142;
           border-radius: 6px; padding: 0.25rem 0.6rem; font: inherit;
           font-size: 0.8rem; cursor: pointer; }}
  .copy:hover {{ border-color: #F5A623; }}
  .js .copy {{ display: block; }}
  .note a, nav a {{ color: #F5A623; text-decoration: none; }}
  .note a:hover, nav a:hover {{ text-decoration: underline; }}
  code {{ font-family: 'DM Mono', ui-monospace, monospace; color: #EDF0F4; }}
  nav {{ margin-top: 2rem; text-align: center; }}
  nav a {{ margin: 0 0.7rem; font-size: 0.95rem; }}
  footer {{ margin-top: 2rem; color: #5A6376; font-size: 0.8rem; text-align: center; }}
  {_tabs_css()}
</style>
</head>
<body>
<main>
  <header>
    <img src="https://app.sugra.ai/images/brand/sugra-app-icon.svg" alt="sugra.ai">
    <h1>Sugra API MCP</h1>
    <p>Connector between LLM agents and world data. 1,600+ endpoints aggregating
    160+ primary sources across 36 data domains, served over the Model Context
    Protocol.</p>
  </header>
  {_tabs_html()}
  <nav>
    <a href="https://github.com/Sugra-Systems/sugra-api-mcp">GitHub</a>
    <a href="https://pypi.org/project/sugra-api-mcp/">PyPI</a>
    <a href="https://app.sugra.ai/developer/mcp">Docs</a>
    <a href="https://sugra.ai">Sugra API</a>
  </nav>
  <footer>Sugra Systems, Inc.</footer>
</main>
<script>
  document.documentElement.classList.add("js");
  document.addEventListener("click", function (event) {{
    var button = event.target.closest("button.copy");
    if (!button) return;
    var text = document.getElementById(button.dataset.copy).textContent;
    navigator.clipboard.writeText(text).then(function () {{
      button.textContent = "Copied";
      setTimeout(function () {{ button.textContent = "Copy"; }}, 1500);
    }});
  }});
</script>
</body>
</html>
"""


async def landing(_request: Request) -> HTMLResponse:
    return HTMLResponse(_LANDING_HTML)


async def health(_request: Request) -> JSONResponse:
    return JSONResponse(
        {"status": "ok", "service": "sugra-api-mcp", "version": __version__}
    )
