"""Minimal agent that connects to the hosted Sugra MCP server and answers a
question using Anthropic tool-use. The official MCP SDK provides the tools; the
agent loop is plain Anthropic Messages tool-use. Provider-agnostic by design.

Usage:
    python agent.py "What is the current US federal funds rate?"
"""

import asyncio
import json
import os
import sys

from anthropic import Anthropic
from dotenv import load_dotenv
from mcp import ClientSession
from mcp.client.streamable_http import create_mcp_http_client, streamable_http_client

# Hosted Sugra MCP endpoint (Streamable HTTP transport).
SUGRA_MCP_URL = "https://app.sugra.ai/mcp"

# Change me: any current Anthropic model id works here.
MODEL = "claude-sonnet-5"

MAX_TURNS = 5
MAX_TOOL_OUTPUT_CHARS = 8000

DEFAULT_QUESTION = "What is the current US federal funds rate?"


def serialize_tool_result(result) -> str:
    """Render an MCP CallToolResult as text for the model. Prefer text content,
    add structuredContent when present, and cap very large output."""
    parts = []
    for block in result.content or []:
        text = getattr(block, "text", None)
        if text:
            parts.append(text)
    structured = getattr(result, "structuredContent", None)
    if structured:
        parts.append(json.dumps(structured, ensure_ascii=False))
    out = "\n".join(parts).strip() or "(empty tool result)"
    if len(out) > MAX_TOOL_OUTPUT_CHARS:
        out = out[:MAX_TOOL_OUTPUT_CHARS] + "\n... (output truncated)"
    return out


async def run(question: str) -> int:
    sugra_key = os.environ.get("SUGRA_API_KEY")
    if not sugra_key:
        print("Error: SUGRA_API_KEY is not set. Copy .env.example to .env and fill it in.")
        return 1
    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("Error: ANTHROPIC_API_KEY is not set. Copy .env.example to .env and fill it in.")
        return 1

    anthropic = Anthropic()
    # Pass the Sugra API key as a Bearer token via a pre-configured HTTP client
    # (the supported, non-deprecated way to set headers on the Streamable HTTP
    # transport). The client must be context-managed so its connections close.
    async with create_mcp_http_client(
        headers={"Authorization": f"Bearer {sugra_key}"}
    ) as http_client:
        async with streamable_http_client(SUGRA_MCP_URL, http_client=http_client) as (read, write, _):
            async with ClientSession(read, write) as session:
                await session.initialize()
                mcp_tools = (await session.list_tools()).tools
                print(f"Connected to the Sugra MCP. {len(mcp_tools)} tools available.")
                tools = [
                    {"name": t.name, "description": t.description or "", "input_schema": t.inputSchema}
                    for t in mcp_tools
                ]
                tools_by_name = {t.name for t in mcp_tools}

                messages = [{"role": "user", "content": question}]

                for _turn in range(MAX_TURNS):
                    response = anthropic.messages.create(
                        model=MODEL, max_tokens=1024, tools=tools, messages=messages
                    )

                    tool_uses = [b for b in response.content if b.type == "tool_use"]
                    if not tool_uses:
                        text = "".join(b.text for b in response.content if b.type == "text")
                        print(text.strip() or "(no answer)")
                        return 0

                    # Assistant turn (with tool_use blocks) must be appended BEFORE
                    # the user turn that carries the matching tool_result blocks.
                    messages.append({"role": "assistant", "content": response.content})

                    tool_results = []
                    for tu in tool_uses:
                        if tu.name not in tools_by_name:
                            content, is_error = f"Unknown tool: {tu.name}", True
                        else:
                            print(f"[tool] {tu.name} {json.dumps(tu.input)}")
                            try:
                                result = await session.call_tool(tu.name, tu.input)
                                content = serialize_tool_result(result)
                                is_error = bool(getattr(result, "isError", False))
                            except Exception as exc:  # MCP / HTTP error: report, do not crash.
                                content, is_error = f"Tool call failed: {exc}", True
                        tool_results.append(
                            {
                                "type": "tool_result",
                                "tool_use_id": tu.id,
                                "content": content,
                                "is_error": is_error,
                            }
                        )

                    # tool_result blocks come first in the user turn.
                    messages.append({"role": "user", "content": tool_results})

                print("Tool loop did not converge within the turn budget. Try a simpler question.")
                return 1


def main() -> int:
    load_dotenv(os.path.join(os.path.dirname(__file__), "..", ".env"))
    question = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_QUESTION
    try:
        return asyncio.run(run(question))
    except Exception as exc:  # connection / list_tools / model API failure
        # Keep the message short and never echo secrets. Common causes: bad or
        # missing keys, no network, or the daily request limit (HTTP 429).
        print(f"Request failed ({type(exc).__name__}). Check your API keys, network, "
              "and daily request limit, then try again.")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
