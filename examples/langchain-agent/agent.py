"""Run a LangChain agent against Sugra over local stdio or hosted HTTP."""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from collections.abc import Sequence
from typing import Any

from dotenv import load_dotenv
from langchain.agents import create_agent
from langchain_mcp_adapters.client import MultiServerMCPClient

SUGRA_MCP_URL = "https://app.sugra.ai/mcp"
DEFAULT_MODEL = "openai:gpt-5.4"
DEFAULT_QUESTION = "What is the current price of Apple stock?"
REQUIRED_FLOW = ("search_endpoints", "describe_endpoint", "call_endpoint")

SYSTEM_PROMPT = """You are a careful data research assistant using Sugra tools.
For every data question, use this exact discovery flow in order:
1. Call search_endpoints to find the best operation.
2. Call describe_endpoint with that operation_id to inspect its required inputs.
3. Call call_endpoint with the operation_id and valid parameters.
Do not use fetch_data as a shortcut. In the final answer, include the source and
freshness information returned by the tool. Never invent unavailable data.
"""


def build_connections(transport: str, sugra_api_key: str) -> dict[str, dict[str, Any]]:
    """Build one Sugra MCP connection for the selected transport."""
    if transport == "stdio":
        return {
            "sugra": {
                "transport": "stdio",
                "command": sys.executable,
                "args": ["-m", "sugra_api_mcp"],
                "env": {"SUGRA_API_KEY": sugra_api_key},
            }
        }
    if transport == "http":
        return {
            "sugra": {
                "transport": "http",
                "url": SUGRA_MCP_URL,
                "headers": {"Authorization": f"Bearer {sugra_api_key}"},
            }
        }
    raise ValueError("transport must be 'stdio' or 'http'")


def tool_call_sequence(messages: Sequence[Any]) -> list[str]:
    """Return MCP tool names in the order the agent called them."""
    names: list[str] = []
    for message in messages:
        for call in getattr(message, "tool_calls", []) or []:
            name = call.get("name") if isinstance(call, dict) else getattr(call, "name", None)
            if isinstance(name, str):
                names.append(name)
    return names


def completed_discovery_flow(names: Sequence[str]) -> bool:
    """Check that the required discovery calls appear in order."""
    next_required = 0
    for name in names:
        if name == REQUIRED_FLOW[next_required]:
            next_required += 1
            if next_required == len(REQUIRED_FLOW):
                return True
    return False


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "question",
        nargs="?",
        default=DEFAULT_QUESTION,
        help="Data question for the agent",
    )
    parser.add_argument(
        "--transport",
        choices=("stdio", "http"),
        default=os.getenv("SUGRA_MCP_TRANSPORT", "http"),
        help="Use the local package over stdio or the hosted Streamable HTTP endpoint",
    )
    parser.add_argument(
        "--model",
        default=os.getenv("MODEL", DEFAULT_MODEL),
        help="LangChain model identifier",
    )
    return parser.parse_args()


async def main() -> None:
    load_dotenv()
    args = parse_args()
    sugra_api_key = os.getenv("SUGRA_API_KEY")
    if not sugra_api_key:
        raise SystemExit("Set SUGRA_API_KEY before running this example.")

    client = MultiServerMCPClient(build_connections(args.transport, sugra_api_key))
    tools = await client.get_tools()
    available = {tool.name for tool in tools}
    missing = set(REQUIRED_FLOW) - available
    if missing:
        raise RuntimeError(f"Sugra connection is missing required tools: {sorted(missing)}")

    agent = create_agent(args.model, tools, system_prompt=SYSTEM_PROMPT)
    result = await agent.ainvoke(
        {"messages": [{"role": "user", "content": args.question}]},
        config={"recursion_limit": 20},
    )

    sequence = tool_call_sequence(result["messages"])
    print(f"[transport] {args.transport}")
    print(f"[flow] {' -> '.join(sequence)}")
    if not completed_discovery_flow(sequence):
        raise RuntimeError(
            "The agent did not complete search_endpoints -> describe_endpoint -> call_endpoint."
        )
    print(result["messages"][-1].content)


if __name__ == "__main__":
    asyncio.run(main())
