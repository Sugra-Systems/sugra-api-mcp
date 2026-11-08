"""Minimal LangChain agent that connects to the Sugra MCP server and
answers questions using langchain-mcp-adapters.

Supports both hosted Streamable HTTP transport and local stdio transport.

Usage:
    python agent.py "What is the current US federal funds rate?"
"""

import asyncio
import os
import sys
from dotenv import load_dotenv

load_dotenv()

SUGRA_MCP_HTTP_URL = os.environ.get("SUGRA_MCP_URL", "https://app.sugra.ai/mcp")
TRANSPORT = os.environ.get("SUGRA_MCP_TRANSPORT", "http").lower()

DEFAULT_QUESTION = "What is the current US federal funds rate?"


async def main(question: str):
    sugra_key = os.environ.get("SUGRA_API_KEY")
    if not sugra_key:
        print("Error: SUGRA_API_KEY is not set. Get a free key at https://app.sugra.ai/register")
        sys.exit(1)

    try:
        from langchain_mcp_adapters.client import MultiServerMCPClient
        from langchain_openai import ChatOpenAI
        from langgraph.prebuilt import create_react_agent
    except ImportError:
        print("Please install requirements: pip install -r requirements.txt")
        sys.exit(1)

    # Configure server connection parameters
    if TRANSPORT == "stdio":
        server_config = {
            "sugra": {
                "command": "sugra-api-mcp",
                "args": [],
                "transport": "stdio",
                "env": {
                    "SUGRA_API_KEY": sugra_key,
                },
            }
        }
    else:
        server_config = {
            "sugra": {
                "url": SUGRA_MCP_HTTP_URL,
                "transport": "streamable_http",
                "headers": {
                    "Authorization": f"Bearer {sugra_key}",
                },
            }
        }

    print(f"Connecting to Sugra MCP server via {TRANSPORT}...")
    async with MultiServerMCPClient(server_config) as client:
        tools = client.get_tools()
        print(f"Successfully loaded {len(tools)} tools: {[t.name for t in tools]}")

        model = ChatOpenAI(model="gpt-4o-mini", temperature=0)
        agent_executor = create_react_agent(model, tools)

        print(f"\nUser Query: {question}")
        response = await agent_executor.ainvoke({"messages": [("user", question)]})

        print("\nAgent Response:")
        last_message = response["messages"][-1]
        print(last_message.content)


if __name__ == "__main__":
    query = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_QUESTION
    asyncio.run(main(query))
