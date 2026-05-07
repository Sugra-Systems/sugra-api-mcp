"""Load the bundled endpoint catalog."""

from __future__ import annotations

import json
from functools import lru_cache
from importlib import resources

from .models import Catalog

CATALOG_PACKAGE = "sugra_api_mcp.catalog.data"
CATALOG_FILE = "endpoints.json"


@lru_cache(maxsize=1)
def load_catalog() -> Catalog:
    """Load bundled endpoint catalog data without network access."""
    data_path = resources.files(CATALOG_PACKAGE).joinpath(CATALOG_FILE)
    raw = data_path.read_text(encoding="utf-8")
    return Catalog.from_dict(json.loads(raw))
