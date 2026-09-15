# LangChain agent with Sugra MCP

This example loads Sugra's MCP tools into a LangChain agent with
`langchain-mcp-adapters`. It supports both connection modes:

- `http`: the hosted Streamable HTTP endpoint at `https://app.sugra.ai/mcp`
- `stdio`: a local `sugra-api-mcp` process installed in the same Python environment

The system prompt requires an explicit discovery flow for every question:
`search_endpoints` -> `describe_endpoint` -> `call_endpoint`. The script prints the
observed tool sequence and exits with an error if the agent skips that flow.

## Install

Python 3.11 or newer is required.

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
```

On Windows PowerShell, activate the environment with:

```powershell
.\.venv\Scripts\Activate.ps1
```

Create a free Sugra API key at
[app.sugra.ai/settings/billing](https://app.sugra.ai/settings/billing). The example
uses OpenAI by default, so set both keys:

```bash
export SUGRA_API_KEY=sugra_...
export OPENAI_API_KEY=sk-...
```

PowerShell equivalent:

```powershell
$env:SUGRA_API_KEY = "sugra_..."
$env:OPENAI_API_KEY = "sk-..."
```

You can also place the variables in a local `.env` file. Do not commit real keys.

## Hosted Streamable HTTP

HTTP is the default. The API key is sent to the hosted endpoint as a Bearer token.

```bash
python agent.py --transport http "What is the current price of Apple stock?"
```

## Local stdio

The stdio mode starts the installed package with
`python -m sugra_api_mcp` and passes `SUGRA_API_KEY` to that subprocess.

```bash
python agent.py --transport stdio "What is the current price of Apple stock?"
```

Set `MODEL` or pass `--model` to use another LangChain model identifier:

```bash
python agent.py --model openai:gpt-5.4 "What is the latest US inflation reading?"
```

## Expected output

The live values will change, but a successful run has this shape:

```text
[transport] http
[flow] search_endpoints -> describe_endpoint -> call_endpoint
Apple is trading at ...
Source: ...
Freshness: ...
```

The free Sugra tier allows 50 requests per day. This three-step example normally uses
three MCP tool calls plus the model calls needed to plan and summarize the result.

## Check the example

The unit tests validate both transport configurations and the discovery-flow guard
without making network requests or using API keys:

```bash
python -m unittest -v test_agent.py
```
