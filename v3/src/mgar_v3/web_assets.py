"""Bind the page to the actual bytes of its browser assets."""

import hashlib
from pathlib import Path


def _page_with_assets(web_root: Path, template: str, assets: tuple[str, ...]) -> str:
    page = (web_root / template).read_text(encoding="utf-8")
    for name in assets:
        version = hashlib.sha256((web_root / name).read_bytes()).hexdigest()
        page = page.replace(f'"/static/{name}"', f'"/static/{name}?v={version}"')
    return page


def agent_page_html(web_root: Path) -> str:
    return _page_with_assets(web_root, "agent.html", ("agent.css", "agent.js", "agent-product.js"))


def general_page_html(web_root: Path) -> str:
    return _page_with_assets(web_root, "general.html", ("general.css", "general.js"))
