"""README images that PyPI rewrites through camo must be absolute https."""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
README = REPO_ROOT / "README.md"
MD_IMG = re.compile(r"!\[[^\]]*\]\(([^)]+)\)")
HTML_IMG = re.compile(r'<img[^>]+src="([^"]+)"', re.I)


def test_readme_images_are_absolute_https() -> None:
    text = README.read_text(encoding="utf-8")
    urls = MD_IMG.findall(text) + HTML_IMG.findall(text)
    assert urls, "README must contain at least one image"
    relative = [u for u in urls if not u.startswith("https://")]
    assert relative == [], f"PyPI camo 404s relative image URLs: {relative}"
