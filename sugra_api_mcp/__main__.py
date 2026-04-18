###########################################
### Sugra API MCP Version 0.3.0         ###
###   CLI ENTRY POINT Version 0.3.0     ###
###########################################

### BEGIN # sugra_api_mcp/__main__.py ###
"""Entry point: `python -m sugra_api_mcp` or `sugra-api-mcp` CLI."""

from __future__ import annotations

import argparse
import sys
from typing import Literal


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="sugra-api-mcp",
        description="Sugra API MCP server - connector between LLM agents and world data.",
    )
    parser.add_argument(
        "--transport",
        choices=["stdio", "streamable-http"],
        default="stdio",
        help="MCP transport (default: stdio for Claude Desktop, Claude Code, Cursor, Zed, etc.)",
    )
    parser.add_argument(
        "--host",
        default="127.0.0.1",
        help="Bind host for streamable-http (default: 127.0.0.1)",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=8001,
        help="Bind port for streamable-http (default: 8001)",
    )
    args = parser.parse_args()

    # Import tools to register them with the FastMCP instance.
    from . import tools  # noqa: F401
    from .server import mcp

    transport: Literal["stdio", "streamable-http"] = args.transport
    if transport == "stdio":
        mcp.run(transport="stdio")
    else:
        import uvicorn

        from .auth import Authenticator, AuthMiddleware
        from .config import load_auth_config

        auth = Authenticator(load_auth_config())
        app = mcp.streamable_http_app()
        app.add_middleware(AuthMiddleware, authenticator=auth)
        uvicorn.run(app, host=args.host, port=args.port, log_level="info")


if __name__ == "__main__":
    sys.exit(main())

### END # sugra_api_mcp/__main__.py ###
