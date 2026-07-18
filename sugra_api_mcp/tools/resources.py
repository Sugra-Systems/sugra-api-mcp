"""Read-only MCP resources served from the bundled endpoint catalog.

Importing this module registers the resources against the global FastMCP
singleton, mirroring how the tool modules register. All three resources are
static reads of bundled data - no network access and no downstream API calls.

Discovery (resources/list) is public on the hosted transport; reading a
resource (resources/read) requires authentication. That boundary is pinned
in sugra_api_mcp.auth.PUBLIC_MCP_METHODS and tests/test_resources.py.
"""

from __future__ import annotations

import json

from ..server import mcp
from .gateway import sources_payload, toolsets_payload

DOMAINS_URI = "sugra://catalog/domains"
SOURCES_URI = "sugra://catalog/sources"
ATTRIBUTION_URI = "sugra://attribution"

ATTRIBUTION_MARKDOWN = """\
# Source attribution

Sugra API serves data from primary sources across markets, macroeconomics,
entity screening, network intelligence, news, and earth observation.

## How sources are named

- Sovereign, intergovernmental, and academic sources are named openly in the
  catalog and in API responses - for example SEC EDGAR, ECB, IMF, BLS, NOAA,
  and the World Bank.
- Commercial upstreams are presented under Sugra-branded wrappers such as
  Sugra Finance, Sugra News, Sugra Crypto, Sugra Forex, and Sugra Weather.

## Per-call attribution

API responses carry per-call attribution metadata, so a payload states
which source family produced it.

## Full source list

The complete, current list of sources is published at
https://sugra.ai/sources.
"""


@mcp.resource(
    DOMAINS_URI,
    name="catalog_domains",
    title="Catalog domains",
    description=(
        "Endpoint groups (toolsets) in the bundled Sugra catalog: name, "
        "description, and endpoint count per group. Same data as the "
        "list_toolsets tool."
    ),
    mime_type="application/json",
)
def catalog_domains() -> str:
    """Toolset groups from the bundled catalog as JSON."""
    return json.dumps(toolsets_payload(), indent=2)


@mcp.resource(
    SOURCES_URI,
    name="catalog_sources",
    title="Catalog sources",
    description=(
        "Source families in the bundled Sugra catalog with endpoint counts "
        "per family. Same data as the list_sources tool."
    ),
    mime_type="application/json",
)
def catalog_sources() -> str:
    """Source families from the bundled catalog as JSON."""
    return json.dumps(sources_payload(), indent=2)


@mcp.resource(
    ATTRIBUTION_URI,
    name="attribution",
    title="Source attribution",
    description=(
        "How Sugra names data sources, where per-call attribution metadata "
        "appears, and where the full source list is published."
    ),
    mime_type="text/markdown",
)
def attribution() -> str:
    """Short attribution reference as Markdown."""
    return ATTRIBUTION_MARKDOWN
