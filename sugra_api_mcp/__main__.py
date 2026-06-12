"""Entry point: `python -m sugra_api_mcp` or `sugra-api-mcp` CLI."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from typing import Literal


def _print_json(payload: object) -> None:
    print(json.dumps(payload, indent=2, sort_keys=True))


def _add_server_args(parser: argparse.ArgumentParser) -> None:
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


def _run_server(args: argparse.Namespace) -> None:
    # Initialise Azure Monitor instrumentation BEFORE importing tools so the
    # @trace_mcp_tool decorator picks up the configured tracer. No-op when
    # APPLICATIONINSIGHTS_CONNECTION_STRING is not in the environment.
    from .observability import setup_observability

    setup_observability()

    # Import tools to register them with the FastMCP instance.
    from . import tools  # noqa: F401
    from .server import mcp

    transport: Literal["stdio", "streamable-http"] = args.transport
    if transport == "stdio":
        mcp.run(transport="stdio")
    else:
        import uvicorn
        from starlette.middleware.cors import CORSMiddleware

        from .auth import Authenticator, AuthMiddleware
        from .config import load_allowed_origins, load_auth_config
        from .tools.agent import register_agent_tools

        # Hosted-only Agent Context Layer tools: registered from the HTTP
        # branch ONLY (a stdio process never gets them even with the env var
        # present), and register_agent_tools itself refuses without the
        # SUGRA_AGENT_INTERNAL_TOKEN credential.
        register_agent_tools()

        auth = Authenticator(load_auth_config())
        app = mcp.streamable_http_app()
        # Order matters: Starlette applies middleware in REVERSE registration
        # order, so AuthMiddleware added first ends up as the inner layer and
        # CORSMiddleware added second wraps it as the outer layer. OPTIONS
        # preflight is then handled by CORS before reaching auth.
        app.add_middleware(AuthMiddleware, authenticator=auth)
        app.add_middleware(
            CORSMiddleware,
            allow_origins=load_allowed_origins(),
            allow_methods=["GET", "POST", "DELETE", "OPTIONS"],
            allow_headers=[
                "Authorization",
                "Content-Type",
                "Mcp-Session-Id",
                "MCP-Protocol-Version",
                "Accept",
                "Last-Event-ID",
            ],
            expose_headers=[
                "WWW-Authenticate",
                "Mcp-Session-Id",
                "MCP-Protocol-Version",
            ],
            max_age=86400,
        )
        uvicorn.run(app, host=args.host, port=args.port, log_level="info")


async def _call_operation(args: argparse.Namespace) -> dict[str, object]:
    from .tools.gateway import call_endpoint

    params = json.loads(args.params) if args.params else {}
    body = json.loads(args.body) if args.body else None
    fields = args.fields.split(",") if args.fields else None
    return await call_endpoint(
        args.operation_id,
        params=params,
        body=body,
        limit=args.limit,
        fields=fields,
        include_raw=args.include_raw,
    )


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="sugra-api-mcp",
        description="Sugra API MCP server - connector between LLM agents and world data.",
    )
    _add_server_args(parser)
    subparsers = parser.add_subparsers(dest="command")

    subparsers.add_parser("doctor", help="Print local MCP package and catalog diagnostics.")
    subparsers.add_parser("list-toolsets", help="List bundled catalog toolsets.")

    search_parser = subparsers.add_parser("search", help="Search bundled endpoint catalog.")
    search_parser.add_argument("query")
    search_parser.add_argument("--limit", type=int, default=10)
    search_parser.add_argument("--toolset")
    search_parser.add_argument("--source")

    describe_parser = subparsers.add_parser("describe", help="Describe an endpoint by operation_id.")
    describe_parser.add_argument("operation_id")

    call_parser = subparsers.add_parser("call", help="Call an endpoint by operation_id.")
    call_parser.add_argument("operation_id")
    call_parser.add_argument("--params", default="{}")
    call_parser.add_argument("--body")
    call_parser.add_argument("--limit", type=int)
    call_parser.add_argument("--fields")
    call_parser.add_argument("--include-raw", action="store_true")

    args = parser.parse_args()

    if args.command is None:
        _run_server(args)
        return

    from . import __version__
    from .catalog.loader import load_catalog
    from .catalog.search import search_catalog
    from .catalog.toolsets import ordered_toolsets

    catalog = load_catalog()
    if args.command == "doctor":
        _print_json(
            {
                "package": "sugra-api-mcp",
                "version": __version__,
                "catalog_source": catalog.source,
                "endpoint_count": catalog.endpoint_count,
            }
        )
    elif args.command == "list-toolsets":
        counts: dict[str, int] = {}
        for endpoint in catalog.endpoints:
            counts[endpoint.toolset] = counts.get(endpoint.toolset, 0) + 1
        _print_json(
            {
                "toolsets": ordered_toolsets(counts),
                "total_endpoints": catalog.endpoint_count,
            }
        )
    elif args.command == "search":
        _print_json(
            {
                "results": search_catalog(
                    catalog,
                    args.query,
                    toolset=args.toolset,
                    source=args.source,
                    limit=args.limit,
                ),
                "catalog_source": catalog.source,
            }
        )
    elif args.command == "describe":
        try:
            _print_json(catalog.get(args.operation_id).to_dict())
        except KeyError:
            _print_json({"error": "unknown_operation_id", "operation_id": args.operation_id})
            raise SystemExit(2) from None
    elif args.command == "call":
        _print_json(asyncio.run(_call_operation(args)))
    else:
        parser.error(f"Unknown command: {args.command}")


if __name__ == "__main__":
    sys.exit(main())
