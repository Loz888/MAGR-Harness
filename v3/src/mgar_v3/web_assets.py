"""Bind the page to the actual bytes of its browser assets."""

import hashlib
from pathlib import Path


def agent_page_html(web_root: Path) -> str:
    page = (web_root / "agent.html").read_text(encoding="utf-8")
    for name in ("agent.css", "agent.js", "agent-product.js"):
        version = hashlib.sha256((web_root / name).read_bytes()).hexdigest()
        page = page.replace(f'"/static/{name}"', f'"/static/{name}?v={version}"')
    return page
