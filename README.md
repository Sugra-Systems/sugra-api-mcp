<!-- mcp-name: ai.sugra/api-mcp -->

<p align="center">
  <a href="https://mcp.sugra.ai"><img src="https://app.sugra.ai/images/brand/sugra-app-icon.svg" alt="Sugra" width="88" height="88"></a>
</p>

<h1 align="center">Sugra MCP</h1>

<p align="center">Connect AI agents to primary sources - markets, macro, news, earth, network, entities and research.</p>

<p align="center">
  <a href="https://pypi.org/project/sugra-api-mcp/"><img src="https://img.shields.io/pypi/v/sugra-api-mcp?label=PyPI&color=F5A623" alt="PyPI"></a>
  <a href="https://pypi.org/project/sugra-api-mcp/"><img src="https://img.shields.io/badge/Python-3.13_%7C_3.14_%7C_3.15-007ec6" alt="Python 3.13 | 3.14 | 3.15"></a>
  <a href="https://github.com/Sugra-Systems/sugra-api-mcp/blob/main/LICENSE"><img src="https://img.shields.io/github/license/Sugra-Systems/sugra-api-mcp?label=License" alt="License"></a>
</p>

<p align="center">
  <a href="https://url.sugra.ai/claude"><img src="https://img.shields.io/badge/Add_to_Claude-F5A623?style=for-the-badge" alt="Add to Claude"></a>
  <a href="https://url.sugra.ai/openai"><img src="https://img.shields.io/badge/Add_to_ChatGPT-F5A623?style=for-the-badge" alt="Add to ChatGPT"></a>
  <a href="https://vscode.dev/redirect/mcp/install?name=sugra&config=%7B%22type%22%3A%22http%22%2C%22url%22%3A%22https%3A%2F%2Fmcp.sugra.ai%2Fmcp%22%7D"><img src="https://img.shields.io/badge/Install_in_VS_Code-F5A623?style=for-the-badge" alt="Install in VS Code"></a>
  <a href="https://cursor.com/en/install-mcp?name=sugra&config=eyJ1cmwiOiJodHRwczovL21jcC5zdWdyYS5haS9tY3AifQ=="><img src="https://img.shields.io/badge/Install_in_Cursor-F5A623?style=for-the-badge" alt="Install in Cursor"></a>
</p>

<p align="center">
  <a href="https://sugra.systems"><b>Website</b></a>
  &nbsp;&middot;&nbsp;
  <a href="https://docs.sugra.ai/doc-2263382"><b>Documentation</b></a>
  &nbsp;&middot;&nbsp;
  <a href="https://www.youtube.com/playlist?list=PLQID7oZL4d9U"><b>Videos</b></a>
  &nbsp;&middot;&nbsp;
  <a href="https://sugra.systems/blog/"><b>Blog</b></a>
  &nbsp;&middot;&nbsp;
  <a href="https://app.sugra.ai/register"><b>Free API key</b></a>
</p>

Sugra MCP is built and run by [Sugra Systems, Inc.](https://sugra.systems) It brings Sugra Data into AI assistants and agents over the Model Context Protocol: 1,600+ endpoints from 200 primary sources across 36 data domains - central banks, national statistics offices, regulators, intergovernmental bodies and more. The agent searches the catalog, reads an endpoint's description and calls it, and every answer carries its source and date.

Published in Anthropic's Connectors Directory, the OpenAI Plugins Directory and the GitHub MCP Registry. Works with Claude, Claude Code, ChatGPT, Codex, GitHub Copilot, VS Code, Grok, Cursor, Antigravity, Zed, Cline, Continue and any MCP client.

**Watch:** [How Sugra MCP works](https://www.youtube.com/watch?v=xRC_VNtfI6Q) &middot; [Seven directions, one key](https://www.youtube.com/watch?v=rsTZL3w61PQ)

## Seven directions

| Direction | What an agent can ask for | Learn more |
|---|---|---|
| **Sugra Finance** | Quotes, price history, company financials, SEC filings, funds, options, futures and Treasuries | [Docs](https://docs.sugra.ai/doc-2263396) &middot; [Video](https://www.youtube.com/watch?v=SrzPRJqbOFk) &middot; [Article](https://sugra.systems/blog/sugra-finance) |
| **Sugra Macro** | Inflation, GDP, interest rates, jobs, trade and public finance of whole countries | [Docs](https://docs.sugra.ai/doc-2263397) &middot; [Video](https://www.youtube.com/watch?v=ZAnxj02DqbA) &middot; [Article](https://sugra.systems/blog/sugra-macro) |
| **Sugra News** | Headlines, feeds and the world's media attention | [Docs](https://docs.sugra.ai/doc-2263399) &middot; [Video](https://www.youtube.com/watch?v=--6gZUoqzPk) &middot; [Article](https://sugra.systems/blog/sugra-news) |
| **Sugra Earth** | Weather, climate, air quality, energy, natural hazards and vessel traffic | [Docs](https://docs.sugra.ai/doc-2263394) &middot; [Video](https://www.youtube.com/watch?v=OtdeGwffI7c) &middot; [Article](https://sugra.systems/blog/sugra-earth) |
| **Sugra NetAtlas** | IP and ASN lookups, routing history, geolocation and internet outages | [Docs](https://docs.sugra.ai/doc-2263398) &middot; [Video](https://www.youtube.com/watch?v=8Z98lcvMYXA) &middot; [Article](https://sugra.systems/blog/sugra-netatlas) |
| **Sugra Entity** | Legal entity identifiers, company registries, sanctions and PEP screening | [Docs](https://docs.sugra.ai/doc-2263395) &middot; [Video](https://www.youtube.com/watch?v=ApI7ueQ5ds8) &middot; [Article](https://sugra.systems/blog/sugra-entity) |
| **Sugra Research** | Papers, preprints, citations and authors | [Docs](https://docs.sugra.ai/doc-2263400) &middot; [Article](https://sugra.systems/blog/sugra-research) |

## Install

The server is hosted, so there is nothing to run. Add Sugra from your agent's directory or install the `sugra-api-skills` plugin, then connect as shown below. The plugin also carries the [Sugra API Skills](https://github.com/Sugra-Systems/sugra-api-skills), which give the agent the procedure - find an operation, read the response, cite the source.

<details>
<summary><b>Anthropic</b> - Claude and Claude Code</summary>

Claude (claude.ai, Desktop, mobile and Cowork): [Add to Claude](https://url.sugra.ai/claude) opens Sugra in Anthropic's Connectors Directory.

Claude Code, the plugin with the skills and the server:

```text
/plugin marketplace add Sugra-Systems/sugra-api-plugins
/plugin install sugra-api-skills@sugra-api-plugins
```

</details>

<details>
<summary><b>OpenAI</b> - ChatGPT and Codex</summary>

[Add to ChatGPT](https://url.sugra.ai/openai) opens Sugra in the OpenAI Plugins Directory, which serves both ChatGPT and Codex. In ChatGPT the skills install together with the server.

Codex, the skills plugin:

```bash
codex plugin marketplace add Sugra-Systems/sugra-api-plugins
codex plugin add sugra-api-skills@sugra-api-plugins
```

</details>

<details>
<summary><b>GitHub</b> - Copilot and VS Code</summary>

GitHub Copilot CLI, the plugin with the skills and the server:

```bash
copilot plugin marketplace add Sugra-Systems/sugra-api-plugins
copilot plugin install sugra-api-skills@sugra-api-plugins
```

VS Code: [Install in VS Code](https://vscode.dev/redirect/mcp/install?name=sugra&config=%7B%22type%22%3A%22http%22%2C%22url%22%3A%22https%3A%2F%2Fmcp.sugra.ai%2Fmcp%22%7D) adds the server.

</details>

<details>
<summary><b>xAI</b> - Grok and Cursor</summary>

Grok, the plugin with the skills and the server:

```bash
grok plugin install Sugra-Systems/sugra-api-plugins#xai
```

Cursor: [Install in Cursor](https://cursor.com/en/install-mcp?name=sugra&config=eyJ1cmwiOiJodHRwczovL21jcC5zdWdyYS5haS9tY3AifQ==) adds the server. The plugin with the skills and the server:

```bash
git clone https://github.com/Sugra-Systems/sugra-api-plugins
mkdir -p ~/.cursor/plugins/local
cp -R sugra-api-plugins/cursor ~/.cursor/plugins/local/sugra-api-skills
```

</details>

<details>
<summary><b>Antigravity</b></summary>

The skills plugin:

```bash
git clone https://github.com/Sugra-Systems/sugra-api-plugins
agy plugin install ./sugra-api-plugins/antigravity
```

Antigravity does not start the server from the plugin: add it as shown under Connect.

</details>

<details>
<summary><b>Other agents</b></summary>

The skills, for any agent that reads Agent Skills:

```bash
npx skills add https://mcp.sugra.ai
```

The server: add it as shown under Connect.

</details>

### Run it locally

The PyPI package runs the server on your machine over stdio with an API key. Python 3.13 or newer:

```bash
pip install sugra-api-mcp
export SUGRA_API_KEY=sugra_...        # free key: https://app.sugra.ai/register
sugra-api-mcp call quotes_symbol_price --params '{"symbol":"AAPL"}'
```

## Connect

Every client connects to one address. Sign in with your Sugra account, or send an API key as `Authorization: Bearer <key>`:

```text
https://mcp.sugra.ai/mcp
```

<details>
<summary><b>Anthropic</b> - Claude and Claude Code</summary>

Claude: connect Sugra in the directory and sign in with your Sugra account. Added Sugra earlier as a custom connector? Remove that one before you connect from the directory, or you will have two.

Claude Code signed in with a claude.ai account picks the connector up on its own. With the plugin, sign in to its server from `/mcp`.

</details>

<details>
<summary><b>OpenAI</b> - ChatGPT and Codex</summary>

In ChatGPT or Codex, connect Sugra and sign in with your Sugra account.

</details>

<details>
<summary><b>GitHub</b> - Copilot and VS Code</summary>

GitHub Copilot CLI, by hand:

```bash
copilot mcp add --transport http sugra-api https://mcp.sugra.ai/mcp
```

VS Code, by hand, `.vscode/mcp.json`:

```json
{ "servers": { "sugra": { "type": "http", "url": "https://mcp.sugra.ai/mcp" } } }
```

Both open the Sugra sign-in page in your browser the first time they connect.

</details>

<details>
<summary><b>xAI</b> - Grok and Cursor</summary>

Grok connects through the plugin.

Cursor, by hand, `~/.cursor/mcp.json`:

```json
{ "mcpServers": { "sugra": { "url": "https://mcp.sugra.ai/mcp" } } }
```

To sign in, open Cursor Settings, then Tools & MCPs, and select Connect next to `sugra`.

</details>

<details>
<summary><b>Antigravity</b></summary>

```bash
agy mcp add sugra-api https://mcp.sugra.ai/mcp
```

The CLI, the app and the IDE share the server. Antigravity opens the Sugra sign-in page the first time the agent uses it.

</details>

<details>
<summary><b>Other agents</b></summary>

Add the address above as a remote MCP server in any client that speaks Streamable HTTP.

</details>

<details>
<summary><b>Local package</b> - Claude Desktop, Cursor and other stdio clients</summary>

```json
{
  "mcpServers": {
    "sugra": {
      "command": "sugra-api-mcp",
      "env": { "SUGRA_API_KEY": "sugra_..." }
    }
  }
}
```

Claude Desktop reads it from `claude_desktop_config.json`, Cursor from `~/.cursor/mcp.json`. Other stdio clients take the same command and environment variable in their own MCP settings.

</details>

## Tools

| Tool | What it does |
|---|---|
| `fetch_data` | Finds the best endpoint for a plain-language request and calls it, in one step |
| `search_endpoints` | Searches the catalog of 1,600+ endpoints |
| `describe_endpoint` | Shows an endpoint's parameters, required inputs and cost hints |
| `call_endpoint` | Calls an endpoint by its operation id, with optional `limit` and `fields` |
| `list_toolsets` | Lists the catalog groups with their endpoint counts |
| `list_sources` | Lists the sources behind the catalog |
| `sugra_entity_screen` | Screens a name against sanctions lists and watchlists |
| `sugra_entity_lookup` | Looks up a company by LEI or VAT number, with screening |
| `resolve_entity` | Hosted only: turns a ticker, company, indicator, coin or currency pair into one entity |
| `get_snapshot` | Hosted only: one current view of an entity, with freshness and provenance |
| `get_timeseries` | Hosted only: a bounded series of an entity's price or macro metric |

The server also ships six workflow prompts (market snapshot, macro briefing, sanctions screening, sector comparison, earth conditions, source overview) and serves five of the Sugra API Skills as MCP resources under `sugra://skills/`.

## Documentation

- [Sugra MCP](https://docs.sugra.ai/doc-2263382)
- [Quickstart](https://docs.sugra.ai/doc-2263383)
- [Rate limits and errors](https://docs.sugra.ai/doc-2263384)
- [Plans and billing](https://docs.sugra.ai/doc-2444799)
- [Changelog](https://docs.sugra.ai/doc-2263381)
- [Self-hosting](https://github.com/Sugra-Systems/sugra-api-mcp/blob/main/docs/self-hosting.md) - running your own HTTP server

## Blog

- [Sugra Data: seven directions, one key](https://sugra.systems/blog/platform-intro)
- [Sugra MCP in Anthropic's Connectors Directory](https://sugra.systems/blog/sugra-api-mcp-server-anthropic-connectors-directory)
- [Sugra API Skills](https://sugra.systems/blog/sugra-api-skills)

## Development

```bash
git clone https://github.com/Sugra-Systems/sugra-api-mcp
cd sugra-api-mcp
pip install -e ".[dev,http]"
pytest
```

The test matrix runs on Python 3.13, 3.14 and 3.15.

## Help

- [Status and support](https://docs.sugra.ai/doc-2444802)
- Email: [support@sugra.systems](mailto:support@sugra.systems)
- Reporting a vulnerability: [SECURITY.md](https://github.com/Sugra-Systems/sugra-api-mcp/blob/main/SECURITY.md)

## License

MIT &copy; 2026 [Sugra Systems, Inc.](https://sugra.systems)
