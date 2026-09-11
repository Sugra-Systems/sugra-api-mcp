# CHANGE MCP-20.1

Patch 0.11.1 republishes the wheel so PyPI renders the three demo GIFs.

GitHub main already uses absolute https raw URLs (MCP-20, PR #116). PyPI 0.11.0 still has the relative `docs/media/*.gif` paths, which Warehouse camo 404s.

Lockstep: pyproject.toml, sugra_api_mcp/__init__.py, server.json all 0.11.1. Tag v0.11.1 after merge. Runtime tools unchanged.
