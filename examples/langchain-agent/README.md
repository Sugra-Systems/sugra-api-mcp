# Sugra MCP example: LangChain Agent

Connect LangChain and LangGraph to the hosted or local Sugra MCP server using `langchain-mcp-adapters`.

## What this example does

This example shows how to:
1. Connect to the Sugra MCP server via:
   - **Hosted Streamable HTTP**: `https://app.sugra.ai/mcp` (or `https://mcp.sugra.ai/mcp`) using `Authorization: Bearer sugra_...`
   - **Local stdio**: Running the local `sugra-api-mcp` package via standard input/output.
2. Load Sugra catalog tools (`search_endpoints`, `describe_endpoint`, `call_endpoint`, `fetch_data`) into LangChain using `MultiServerMCPClient` from `langchain_mcp_adapters.client`.
3. Create a prebuilt tool-calling agent using `langgraph.prebuilt.create_react_agent` to query live intelligence data.

## Prerequisites

- Python 3.10+
- A free Sugra API key (50 requests/day): [Register on Sugra](https://app.sugra.ai/register)
- An LLM API key (e.g. OpenAI or Anthropic)

## Setup

1. Install dependencies:
```bash
pip install -r requirements.txt
```

2. Configure environment variables in `.env` (or set in shell):
```bash
SUGRA_API_KEY="your-sugra-api-key"
OPENAI_API_KEY="your-openai-api-key"
```

## Running the Agent

### Mode 1: Hosted Streamable HTTP (Default)
```bash
python agent.py "What is the current US federal funds rate?"
```

### Mode 2: Local Stdio
Set `SUGRA_MCP_TRANSPORT=stdio`:
```bash
SUGRA_MCP_TRANSPORT=stdio python agent.py "Find inflation rate for Germany"
```

## Expected Output

```text
Connecting to Sugra MCP via http...
Loaded 4 tools: ['search_endpoints', 'describe_endpoint', 'call_endpoint', 'fetch_data']

User question: What is the current US federal funds rate?
Agent calling: search_endpoints({'query': 'federal funds rate'})
Agent calling: call_endpoint({'endpoint_id': 'fed_funds_rate', 'parameters': {}})

Final Answer: The current US federal funds effective rate is 5.33% as reported by the Federal Reserve.
```
