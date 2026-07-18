"""Public web surface for the hosted HTTP transport: landing page and health.

Two unauthenticated GET routes registered ONLY by the HTTP entry point
(stdio installs never serve them): a minimal human-facing landing on the
host root and a liveness probe on /health. Everything else on the app stays
behind AuthMiddleware. The auth-side allowlist lives in
sugra_api_mcp.auth.PUBLIC_GET_PATHS - the two lists must stay in sync.
"""

from __future__ import annotations

from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse

from . import __version__

_LANDING_HTML = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Sugra API MCP</title>
<style>
  body { margin: 0; min-height: 100vh; display: flex; align-items: center;
         justify-content: center; background: #0B0F1A; color: #E6E8EE;
         font-family: 'DM Sans', 'Segoe UI', system-ui, sans-serif; }
  main { max-width: 40rem; padding: 3rem 1.5rem; text-align: center; }
  img { width: 72px; height: 72px; }
  h1 { font-size: 1.6rem; margin: 1rem 0 0.4rem; font-weight: 600; }
  p { color: #9AA3B2; line-height: 1.55; margin: 0.4rem 0 1.4rem; }
  code { display: inline-block; background: #141A2A; border: 1px solid #232B3E;
         border-radius: 8px; padding: 0.55rem 1rem; font-size: 0.95rem;
         color: #F5A623; }
  nav { margin-top: 1.6rem; }
  nav a { color: #F5A623; text-decoration: none; margin: 0 0.7rem;
          font-size: 0.95rem; }
  nav a:hover { text-decoration: underline; }
  footer { margin-top: 2.2rem; color: #5A6376; font-size: 0.8rem; }
</style>
</head>
<body>
<main>
  <img src="https://app.sugra.ai/images/brand/sugra-app-icon.svg" alt="sugra.ai">
  <h1>Sugra API MCP</h1>
  <p>Connector between LLM agents and world data. 1,500+ endpoints aggregating
  160+ primary sources across 36 data domains, served over the Model Context
  Protocol.</p>
  <code>https://mcp.sugra.ai/mcp</code>
  <p>Add this URL as a remote MCP server in ChatGPT, Claude, or any
  MCP-enabled client.</p>
  <nav>
    <a href="https://github.com/Sugra-Systems/sugra-api-mcp">GitHub</a>
    <a href="https://pypi.org/project/sugra-api-mcp/">PyPI</a>
    <a href="https://app.sugra.ai/developer/mcp">Docs</a>
    <a href="https://sugra.ai">Sugra API</a>
  </nav>
  <footer>Sugra Systems, Inc.</footer>
</main>
</body>
</html>
"""


async def landing(_request: Request) -> HTMLResponse:
    return HTMLResponse(_LANDING_HTML)


async def health(_request: Request) -> JSONResponse:
    return JSONResponse(
        {"status": "ok", "service": "sugra-api-mcp", "version": __version__}
    )
