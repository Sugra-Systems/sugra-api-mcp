"""Public web surface for the hosted HTTP transport: landing page and health.

Two unauthenticated GET routes registered ONLY by the HTTP entry point
(stdio installs never serve them): a minimal human-facing landing on the
host root and a liveness probe on /health. Everything else on the app stays
behind AuthMiddleware. The auth-side allowlist lives in
sugra_api_mcp.auth.PUBLIC_GET_PATHS - the two lists must stay in sync.

The landing carries the standard Sugra header and footer and one install tab
per client. Every copyable command or config names the API key only through
the SUGRA_API_KEY environment variable (or, in VS Code, an input prompt),
spelled for the shell that runs it; the Other tab describes the header in
prose. A key is never written into anything on this page.
"""

from __future__ import annotations

import base64
import html
import json
import re
from datetime import date
from urllib.parse import quote

from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse

from . import __version__

ENDPOINT = "https://mcp.sugra.ai/mcp"
SKILLS_REPO = "Sugra-Systems/sugra-api-skills"
DOCS_URL = "https://docs.sugra.ai"
REGISTER_URL = "https://app.sugra.ai/register"

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

# The SS mark on the page plate: prod-sugra-design current/icons/sugra-favicon.svg.
_FAVICON_SVG = (
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100">'
    '<rect width="100" height="100" fill="#0F1117"/>'
    '<g stroke="#F5A623" stroke-width="20" stroke-linecap="round" fill="none">'
    '<line x1="44" y1="17" x2="22.555" y2="83"/>'
    '<line x1="77.445" y1="17" x2="56" y2="83"/></g></svg>'
)
FAVICON_URI = "data:image/svg+xml," + quote(_FAVICON_SVG)


def _wordmark(height: str) -> str:
    """The sugra.ai lockup, as the sugra.systems header and footer render it."""
    return (
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 380 100" role="img"'
        f' aria-label="sugra.ai" style="height:{height}; width:auto;'
        ' aspect-ratio:380/100; display:block;">'
        '<text x="0" y="78" font-family="\'DM Serif Display\', Georgia, serif"'
        ' font-size="100" letter-spacing="-2" fill="#EDF0F4">sugra'
        '<tspan font-style="italic" fill="#F5A623">.ai</tspan></text></svg>'
    )


# Code blocks: the owner's console palette. Classes: b binary, s string or
# name, v environment or input reference, k JSON key, o boolean, p punctuation;
# anything else is the block's main text.
_REF = re.compile(r"(\$\{[^}]+\}|\$env:\w+|\$\w+|%\w+%)")
_SHELL_TOKEN = re.compile(r'"[^"]*"|\s+|\S+')
_JSON_TOKEN = re.compile(
    r'("(?:[^"\\]|\\.)*")(:?)|(true|false|null)\b|([{}\[\],])|(\s+)|([^\s{}\[\],"]+)'
)


def _span(kind: str, text: str) -> str:
    return f'<span class="{kind}">{html.escape(text, quote=False)}</span>'


def _with_refs(kind: str, text: str) -> str:
    """Text in one syntax color, with every environment or input reference violet."""
    return "".join(
        _span("v" if index % 2 else kind, part)
        for index, part in enumerate(_REF.split(text))
        if part
    )


def _shell_line(line: str) -> str:
    out = []
    first = True
    for token in _SHELL_TOKEN.findall(line):
        if token.isspace():
            out.append(token)
            continue
        if first:
            out.append(_span("b", token))
            first = False
        elif token.startswith('"'):
            out.append(_with_refs("s", token))
        elif not token.startswith("-") and ("/" in token or "@" in token):
            out.append(_span("s", token))
        elif token == "SUGRA_API_KEY" or _REF.fullmatch(token):
            out.append(_span("v", token))
        else:
            out.append(html.escape(token, quote=False))
    return '<span class="ln">' + "".join(out) + "</span>"


def _json_line(line: str) -> str:
    out = []
    for string, colon, literal, punct, space, other in _JSON_TOKEN.findall(line):
        if string:
            out.append(_span("k", string) + _span("p", ":") if colon else _with_refs("s", string))
        elif literal:
            out.append(_span("o", literal))
        elif punct:
            out.append(_span("p", punct))
        else:
            out.append(html.escape(space or other, quote=False))
    return '<span class="ln">' + "".join(out) + "</span>"


def _code(ident: str, label: str, kind: str, body: str) -> str:
    """A code block: a strip with the language on the left and Copy on the right.

    Copy copies the block's text exactly. The prompt and the JSON line numbers
    are CSS pseudo-elements, so they are never part of that text.
    """
    return (
        f'<div class="code"><div class="code-head"><span>{label}</span>'
        f'<button type="button" class="copy" data-copy="{ident}">Copy</button></div>'
        f'<pre id="{ident}" class="{kind}">{body}</pre></div>'
    )


def _terminal(ident: str, text: str, label: str = "Terminal", kind: str = "sh") -> str:
    return _code(ident, label, kind, "\n".join(_shell_line(line) for line in text.split("\n")))


def _json_block(ident: str, text: str) -> str:
    return _code(ident, "JSON", "json", "\n".join(_json_line(line) for line in text.split("\n")))


def _step(label: str) -> str:
    return f'<p class="step">{label}</p>'


def _key_header(variable: str) -> str:
    return f'--header "Authorization: Bearer {variable}"'


_GET_KEY = (
    f'No key yet? <a href="{REGISTER_URL}?from=mcp.landing.get-api-key">Get API key</a>'
)

_KEY_NOTE = (
    '<p class="note">Reads your API key from the <code>SUGRA_API_KEY</code> '
    f"environment variable. {_GET_KEY}</p>"
)

_SHELL_KEY_NOTE = (
    '<p class="note">Your shell fills in the API key from the '
    "<code>SUGRA_API_KEY</code> environment variable when you run the command. "
    f"In cmd.exe, write <code>%SUGRA_API_KEY%</code>. {_GET_KEY}</p>"
)

_SKILLS_NOTE = (
    '<p class="note">The skills teach the agent how to find, call and cite '
    "Sugra endpoints.</p>"
)


def _header_server(ident: str, add: str) -> str:
    """The server step for a CLI that takes the key as a --header value.

    The shell expands the variable before the CLI sees it, so the spelling
    depends on the shell: $NAME in bash and zsh, $env:NAME in PowerShell (a
    bare $NAME there is an unset PowerShell variable and sends an empty key).
    """
    return (
        _step("Server")
        + _terminal(
            f"cmd-{ident}",
            f"{add} {ENDPOINT} {_key_header('$SUGRA_API_KEY')}",
            label="bash / zsh",
        )
        + _terminal(
            f"cmd-{ident}-powershell",
            f"{add} {ENDPOINT} {_key_header('$env:SUGRA_API_KEY')}",
            label="PowerShell",
            kind="ps",
        )
        + _SHELL_KEY_NOTE
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
        _header_server("claude-code", "claude mcp add --transport http sugra")
        + _step("Skills (optional)")
        + _terminal(
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
        + _terminal(
            "cmd-codex",
            f"codex mcp add sugra --url {ENDPOINT} --bearer-token-env-var SUGRA_API_KEY",
        )
        + _KEY_NOTE
        + _step("Skills (optional)")
        + _terminal(
            "cmd-codex-skills",
            f"codex plugin marketplace add {SKILLS_REPO}\n"
            "codex plugin add sugra-api@sugra-api-skills",
        )
        + _SKILLS_NOTE,
    ),
    (
        "grok",
        "Grok",
        _header_server("grok", "grok mcp add --transport http sugra")
        + _step("Skills (optional)")
        + _terminal("cmd-grok-skills", f"grok plugin install {SKILLS_REPO}#plugins/sugra-api")
        + _SKILLS_NOTE,
    ),
    (
        "gemini",
        "Gemini CLI",
        _header_server("gemini", "gemini mcp add --transport http sugra"),
    ),
    (
        "cursor",
        "Cursor",
        f'<a href="{CURSOR_INSTALL_LINK}" class="btn">Add to Cursor</a>'
        + _KEY_NOTE
        + _step("Or add to <code>~/.cursor/mcp.json</code>")
        + _json_block("cfg-cursor", _CURSOR_JSON),
    ),
    (
        "vscode",
        "VS Code",
        _step("Add to <code>.vscode/mcp.json</code>")
        + _json_block("cfg-vscode", _VSCODE_JSON)
        + '<p class="note">VS Code asks for the key when the server starts.</p>',
    ),
    (
        "other",
        "Other",
        "<p>Any MCP client that supports remote servers.</p>"
        + _step("URL")
        + _code("cfg-url", "URL", "plain", _span("s", ENDPOINT))
        + '<p class="note">Send the key in the <code>Authorization</code> header as '
        "<code>Bearer</code> followed by the key, from wherever your client keeps "
        f"secrets. {_GET_KEY}</p>",
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


# The standard chrome, mirroring the sugra.systems header and footer.
_SITE = "https://sugra.systems"
_NAV = [
    ("Products", f"{_SITE}/products"),
    ("API", f"{_SITE}/api"),
    ("Pricing", f"{_SITE}/api/pricing"),
    ("For Business", f"{_SITE}/business"),
    ("About", f"{_SITE}/about"),
    ("Contact", f"{_SITE}/contact"),
    ("Blog", f"{_SITE}/blog"),
]
_FOOTER_COLUMNS = [
    (
        "API",
        [
            ("API hub", f"{_SITE}/api"),
            ("Connect via MCP", f"{_SITE}/api#mcp"),
            ("Data providers", f"{_SITE}/api/data-providers"),
            ("Coverage", f"{_SITE}/api/coverage"),
            ("Architecture", f"{_SITE}/api/platform"),
            ("Build", f"{_SITE}/api/build"),
            ("Use cases", f"{_SITE}/api/use-cases"),
            ("Pricing", f"{_SITE}/api/pricing"),
            ("Docs", DOCS_URL),
            ("Blog", f"{_SITE}/blog"),
        ],
    ),
    (
        "Products",
        [
            ("All products", f"{_SITE}/products"),
            ("Sugra API", f"{_SITE}/api"),
            ("Sugra Assist", f"{_SITE}/sugra-assist"),
        ],
    ),
    (
        "Company",
        [
            ("About", f"{_SITE}/about"),
            ("For Business", f"{_SITE}/business"),
            ("Contact", f"{_SITE}/contact"),
        ],
    ),
]
_SOCIAL = [
    (
        "GitHub",
        "https://github.com/Sugra-Systems",
        "M12 0C5.37 0 0 5.37 0 12c0 5.31 3.435 9.795 8.205 11.385.6.105.825-.255.825-.57 0-.285-.015-1.23-.015-2.235-3.015.555-3.795-.735-4.035-1.41-.135-.345-.72-1.41-1.23-1.695-.42-.225-1.02-.78-.015-.795.945-.015 1.62.87 1.845 1.23 1.08 1.815 2.805 1.305 3.495.99.105-.78.42-1.305.765-1.605-2.67-.3-5.46-1.335-5.46-5.925 0-1.305.465-2.385 1.23-3.225-.12-.3-.54-1.53.12-3.18 0 0 1.005-.315 3.3 1.23.96-.27 1.98-.405 3-.405s2.04.135 3 .405c2.295-1.56 3.3-1.23 3.3-1.23.66 1.65.24 2.88.12 3.18.765.84 1.23 1.905 1.23 3.225 0 4.605-2.805 5.625-5.475 5.925.435.375.81 1.095.81 2.22 0 1.605-.015 2.895-.015 3.3 0 .315.225.69.825.57A12.02 12.02 0 0024 12c0-6.63-5.37-12-12-12z",
    ),
    (
        "LinkedIn",
        "https://linkedin.com/company/sugrasystems",
        "M20.447 20.452h-3.554v-5.569c0-1.328-.027-3.037-1.852-3.037-1.853 0-2.136 1.445-2.136 2.939v5.667H9.351V9h3.414v1.561h.046c.477-.9 1.637-1.85 3.37-1.85 3.601 0 4.267 2.37 4.267 5.455v6.286zM5.337 7.433a2.062 2.062 0 01-2.063-2.065 2.064 2.064 0 112.063 2.065zm1.782 13.019H3.555V9h3.564v11.452zM22.225 0H1.771C.792 0 0 .774 0 1.729v20.542C0 23.227.792 24 1.771 24h20.451C23.2 24 24 23.227 24 22.271V1.729C24 .774 23.2 0 22.222 0h.003z",
    ),
    (
        "X / Twitter",
        "https://x.com/sugrasystems",
        "M18.244 2.25h3.308l-7.227 8.26 8.502 11.24H16.17l-4.714-6.231-5.401 6.231H2.745l7.73-8.835L1.254 2.25H8.08l4.259 5.63 5.905-5.63zm-1.161 17.52h1.833L7.084 4.126H5.117z",
    ),
    (
        "Instagram",
        "https://instagram.com/sugra.ai",
        "M12 2.163c3.204 0 3.584.012 4.85.07 3.252.148 4.771 1.691 4.919 4.919.058 1.265.069 1.645.069 4.849 0 3.205-.012 3.584-.069 4.849-.149 3.225-1.664 4.771-4.919 4.919-1.266.058-1.644.07-4.85.07-3.204 0-3.584-.012-4.849-.07-3.26-.149-4.771-1.699-4.919-4.92-.058-1.265-.07-1.644-.07-4.849 0-3.204.013-3.583.07-4.849.149-3.227 1.664-4.771 4.919-4.919 1.266-.057 1.645-.069 4.849-.069zM12 0C8.741 0 8.333.014 7.053.072 2.695.272.273 2.69.073 7.052.014 8.333 0 8.741 0 12c0 3.259.014 3.668.072 4.948.2 4.358 2.618 6.78 6.98 6.98C8.333 23.986 8.741 24 12 24c3.259 0 3.668-.014 4.948-.072 4.354-.2 6.782-2.618 6.979-6.98.059-1.28.073-1.689.073-4.948 0-3.259-.014-3.667-.072-4.947-.196-4.354-2.617-6.78-6.979-6.98C15.668.014 15.259 0 12 0zm0 5.838a6.162 6.162 0 100 12.324 6.162 6.162 0 000-12.324zM12 16a4 4 0 110-8 4 4 0 010 8zm6.406-11.845a1.44 1.44 0 100 2.881 1.44 1.44 0 000-2.881z",
    ),
    (
        "Facebook",
        "https://facebook.com/SugraAI",
        "M24 12.073c0-6.627-5.373-12-12-12s-12 5.373-12 12c0 5.99 4.388 10.954 10.125 11.854v-8.385H7.078v-3.47h3.047V9.43c0-3.007 1.792-4.669 4.533-4.669 1.312 0 2.686.235 2.686.235v2.953H15.83c-1.491 0-1.956.925-1.956 1.874v2.25h3.328l-.532 3.47h-2.796v8.385C19.612 23.027 24 18.062 24 12.073z",
    ),
    (
        "YouTube",
        "https://www.youtube.com/@SugraSystems",
        "M23.498 6.186a3.016 3.016 0 00-2.122-2.136C19.505 3.545 12 3.545 12 3.545s-7.505 0-9.377.505A3.017 3.017 0 00.502 6.186C0 8.07 0 12 0 12s0 3.93.502 5.814a3.016 3.016 0 002.122 2.136c1.871.505 9.376.505 9.376.505s7.505 0 9.377-.505a3.015 3.015 0 002.122-2.136C24 15.93 24 12 24 12s0-3.93-.502-5.814zM9.545 15.568V8.432L15.818 12l-6.273 3.568z",
    ),
]
_LEGAL = [
    ("Privacy", f"{_SITE}/privacy-policy"),
    ("Terms", f"{_SITE}/terms-of-service"),
    ("Data Use", f"{_SITE}/data-use-policy"),
    ("AUP", f"{_SITE}/acceptable-use-policy"),
    ("DPA", f"{_SITE}/data-processing-agreement"),
    ("SLA", f"{_SITE}/service-level-agreement"),
    ("Status", "https://status.sugra.ai"),
]


def _site_header() -> str:
    nav = "".join(f'<a href="{url}">{label}</a>' for label, url in _NAV)
    return (
        '<header class="site"><div class="bar wrap">'
        f'<a href="{_SITE}" class="brand" aria-label="sugra.ai">{_wordmark("1.375rem")}</a>'
        f'<nav class="nav" aria-label="Main">{nav}</nav>'
        '<div class="actions">'
        '<a href="https://app.sugra.ai/login?from=mcp.header.sign-in" class="signin">Sign in</a>'
        f'<a href="{REGISTER_URL}?from=mcp.header.get-api-key" class="btn-header">Get API key</a>'
        "</div></div></header>"
    )


def _site_footer() -> str:
    columns = "".join(
        f'<div><p class="col-title">{title}</p><div class="col">'
        + "".join(f'<a href="{url}">{label}</a>' for label, url in links)
        + "</div></div>"
        for title, links in _FOOTER_COLUMNS
    )
    social = "".join(
        f'<a href="{url}" target="_blank" rel="noopener noreferrer" aria-label="{label}">'
        f'<svg width="18" height="18" viewBox="0 0 24 24" fill="currentColor" aria-hidden="true">'
        f'<path d="{path}"/></svg></a>'
        for label, url, path in _SOCIAL
    )
    dot = '<span class="dot" aria-hidden="true">&middot;</span>'
    legal = dot.join(
        [f"<span>&copy; {date.today().year} Sugra Systems, Inc.</span>"]
        + [f'<a href="{url}">{label}</a>' for label, url in _LEGAL]
        + ["<span>Delaware, USA &middot; Incorporated 2021</span>"]
    )
    return (
        '<footer class="site"><div class="wrap">'
        '<div class="grid"><div>'
        f'<a href="{_SITE}" class="brand" aria-label="sugra.ai">{_wordmark("1.25rem")}</a>'
        '<p class="tagline">Intelligence infrastructure for the '
        "<em>decisions that matter.</em></p></div>"
        f"{columns}</div>"
        f'<div class="social">{social}</div>'
        f'<div class="legal">{legal}</div>'
        "</div></footer>"
    )


_LANDING_HTML = f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Sugra API MCP</title>
<meta name="description" content="Connect Claude, ChatGPT, Claude Code, Codex, Grok, Gemini CLI, Cursor and VS Code to Sugra API over the Model Context Protocol.">
<link rel="icon" href="{_SITE}/favicon.ico" sizes="32x32">
<link rel="icon" type="image/svg+xml" href="{FAVICON_URI}">
<link rel="apple-touch-icon" href="{_SITE}/apple-touch-icon.png">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=DM+Mono&family=DM+Sans:wght@400;600&family=DM+Serif+Display:ital@0;1&family=JetBrains+Mono&display=swap">
<style>
  body {{ margin: 0; min-height: 100vh; display: flex; flex-direction: column;
         background: #0F1117; color: #EDF0F4;
         font-family: 'DM Sans', system-ui, sans-serif; }}
  a:focus-visible, button:focus-visible {{ outline: 2px solid #F5A623; outline-offset: 2px; }}
  .wrap {{ width: 100%; max-width: 1200px; margin: 0 auto; padding: 0 1.5rem;
           box-sizing: border-box; }}
  header.site {{ position: sticky; top: 0; z-index: 50; background: rgba(6,9,15,0.85);
                 backdrop-filter: blur(12px); -webkit-backdrop-filter: blur(12px);
                 border-bottom: 1px solid rgba(255,255,255,0.07); }}
  .bar {{ display: flex; align-items: center; height: 64px; gap: 0.75rem; }}
  .brand {{ display: inline-block; line-height: 0; flex-shrink: 0; text-decoration: none; }}
  .nav {{ display: none; align-items: center; gap: 1.1rem; margin-left: 1.5rem; }}
  .nav a, .signin {{ color: #8A95A3; text-decoration: none; font-size: 0.9375rem;
                     transition: color 0.2s; }}
  .nav a:hover, .signin:hover {{ color: #EDF0F4; }}
  .actions {{ display: flex; align-items: center; gap: 1rem; margin-left: auto; }}
  .signin {{ display: none; }}
  .btn-header {{ display: inline-flex; align-items: center; background: #F5A623;
                 color: #0F1117; font-weight: 600; font-size: 0.875rem;
                 padding: 0.5rem 1.05rem; border-radius: 4px; text-decoration: none;
                 white-space: nowrap; }}
  .btn-header:hover {{ opacity: 0.9; }}
  @media (min-width: 40rem) {{ .signin {{ display: inline-block; }} }}
  @media (min-width: 64rem) {{ .nav {{ display: flex; }} }}
  main {{ flex: 1; width: 100%; max-width: 50rem; margin: 0 auto; padding: 3rem 1.5rem;
          box-sizing: border-box; }}
  .hero {{ text-align: center; }}
  h1 {{ font-family: 'DM Serif Display', Georgia, serif; font-weight: 400;
        font-size: 2rem; margin: 0 0 0.4rem; }}
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
                  margin: 1.2rem 0 0.5rem; }}
  .panel .step:first-child {{ margin-top: 0; }}
  .panel .step code {{ text-transform: none; letter-spacing: 0; }}
  .panel .note {{ font-size: 0.85rem; margin: 0.8rem 0 0; }}
  .btn {{ display: inline-block; background: #F5A623; color: #0F1117; font-weight: 600;
          text-decoration: none; border-radius: 8px; padding: 0.6rem 1.2rem; }}
  .btn:hover {{ opacity: 0.9; }}
  .code {{ background: #0F1115; border: 1px solid #30363D; border-radius: 10px;
           overflow: hidden; }}
  .code + .code {{ margin-top: 0.6rem; }}
  .code-head {{ display: flex; align-items: center; justify-content: space-between;
                gap: 0.5rem; min-height: 2.1rem; padding: 0 0.5rem 0 1rem;
                background: #171B22; border-bottom: 1px solid #30363D;
                font-family: 'JetBrains Mono', SFMono-Regular, Menlo, monospace;
                font-size: 0.72rem; color: #718096; }}
  .code pre {{ margin: 0; padding: 16px; color: #D7DAE0;
               font-family: 'JetBrains Mono', SFMono-Regular, Menlo, monospace;
               font-size: 0.84rem; line-height: 1.6;
               white-space: pre-wrap; overflow-wrap: anywhere; }}
  pre.sh .ln::before {{ content: "$ "; color: #718096; }}
  pre.ps .ln::before {{ content: "PS> "; color: #718096; }}
  pre.json {{ counter-reset: line; white-space: pre; overflow-wrap: normal; overflow-x: auto; }}
  pre.json .ln {{ counter-increment: line; }}
  pre.json .ln::before {{ content: counter(line); display: inline-block; width: 2ch;
                          margin-right: 1.5ch; text-align: right; color: #718096; }}
  .b {{ color: #86EF8A; }}
  .s {{ color: #F6B94A; }}
  .v {{ color: #D879F0; }}
  .k {{ color: #38BDF8; }}
  .o {{ color: #FB7185; }}
  .p {{ color: #CBD5E1; }}
  .copy {{ display: none; background: transparent; color: #D7DAE0;
           border: 1px solid #30363D; border-radius: 6px; padding: 0.15rem 0.6rem;
           font: inherit; cursor: pointer; }}
  .copy:hover {{ border-color: #718096; }}
  .js .copy {{ display: inline-block; }}
  .note a, .links a {{ color: #F5A623; text-decoration: none; }}
  .note a:hover, .links a:hover {{ text-decoration: underline; }}
  code {{ font-family: 'DM Mono', ui-monospace, monospace; color: #EDF0F4; }}
  .links {{ margin-top: 2rem; text-align: center; }}
  .links a {{ margin: 0 0.7rem; font-size: 0.95rem; }}
  footer.site {{ border-top: 1px solid rgba(255,255,255,0.07); background: #06090F;
                 padding: 3.5rem 0 1.5rem; }}
  .grid {{ display: grid; gap: 2.5rem; grid-template-columns: 1fr; }}
  @media (min-width: 48rem) {{ .grid {{ grid-template-columns: repeat(2, 1fr); }} }}
  @media (min-width: 64rem) {{ .grid {{ grid-template-columns: repeat(4, 1fr); }} }}
  .grid .brand {{ margin-bottom: 0.75rem; }}
  .tagline {{ font-size: 0.8125rem; color: #8A95A3; line-height: 1.6; max-width: 220px;
              margin: 0; }}
  .tagline em {{ color: #F5A623; }}
  .col-title {{ font-family: 'DM Mono', ui-monospace, monospace; font-size: 10px;
                letter-spacing: 0.15em; text-transform: uppercase; color: #F5A623;
                margin: 0 0 1rem; }}
  .col {{ display: flex; flex-direction: column; gap: 0.6rem; }}
  .col a, .legal a {{ color: #8A95A3; font-size: 0.875rem; text-decoration: none;
                      transition: color 0.2s; }}
  .col a:hover, .legal a:hover, .social a:hover {{ color: #EDF0F4; }}
  .social {{ margin-top: 2.5rem; padding: 1.5rem 0;
             border-top: 1px solid rgba(255,255,255,0.07);
             border-bottom: 1px solid rgba(255,255,255,0.07);
             display: flex; justify-content: center; gap: 1.25rem; }}
  .social a {{ color: #8A95A3; display: inline-flex; transition: color 0.2s; }}
  .legal {{ margin-top: 1.25rem; display: flex; flex-wrap: wrap; gap: 0.5rem 1rem;
            justify-content: center; align-items: center;
            font-family: 'DM Mono', ui-monospace, monospace; font-size: 0.75rem;
            color: #8A95A3; }}
  .legal a {{ font-size: 0.75rem; }}
  .legal .dot {{ color: rgba(255,255,255,0.12); }}
  @media (max-width: 30rem) {{
    .wrap, main {{ padding-left: 1rem; padding-right: 1rem; }}
    .panel {{ padding: 0.9rem; }}
    .code pre {{ font-size: 0.78rem; padding: 14px; }}
  }}
  {_tabs_css()}
</style>
</head>
<body>
{_site_header()}
<main>
  <div class="hero">
    <h1>Sugra API MCP</h1>
    <p>Connector between LLM agents and world data. 1,600+ endpoints aggregating
    160+ primary sources across 36 data domains, served over the Model Context
    Protocol.</p>
  </div>
  {_tabs_html()}
  <div class="links">
    <a href="https://github.com/Sugra-Systems/sugra-api-mcp">GitHub</a>
    <a href="https://pypi.org/project/sugra-api-mcp/">PyPI</a>
    <a href="{DOCS_URL}">Docs</a>
  </div>
</main>
{_site_footer()}
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
