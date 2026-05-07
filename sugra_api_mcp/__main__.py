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
