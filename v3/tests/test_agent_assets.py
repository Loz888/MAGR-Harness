"""Page/asset revision binding prevents stale browser scripts after an upgrade."""

import hashlib
import re

from test_agent_page import client_for

from mgar_v3.web_assets import agent_page_html


def test_asset_url_changes_when_script_changes_without_an_html_edit(tmp_path):
    template = (
        '<link href="/static/agent.css"><script src="/static/agent.js"></script>'
        '<script src="/static/agent-product.js"></script>'
    )
    (tmp_path / "agent.html").write_text(template)
    for name in ("agent.css", "agent.js", "agent-product.js"):
        (tmp_path / name).write_text("original " + name)
    first = agent_page_html(tmp_path)
    (tmp_path / "agent.js").write_text("new script after HTML cleanup")
    second = agent_page_html(tmp_path)
    assert first != second
    old_version = hashlib.sha256(b"original agent.js").hexdigest()
    new_version = hashlib.sha256(b"new script after HTML cleanup").hexdigest()
    assert '/static/agent.js?v=' + old_version in first
    assert '/static/agent.js?v=' + new_version in second
    assert (tmp_path / "agent.html").read_text() == template


def test_served_page_pins_actual_asset_bytes_and_retains_security_headers(settings, engine):
    with client_for(settings, engine) as client:
        page = client.get("/repo")
        assert page.status_code == 200
        assert page.headers["cache-control"] == "no-store"
        assert "script-src 'self'" in page.headers["content-security-policy"]
        assert page.headers["x-content-type-options"] == "nosniff"
        assets = re.findall(
            r'(?:src|href)="(/static/agent[^"?]*\?v=([a-f0-9]{64}))"', page.text,
        )
        assert len(assets) == 3
        for url, version in assets:
            response = client.get(url)
            assert response.status_code == 200
            assert hashlib.sha256(response.content).hexdigest() == version
        assert client.get("/repo").text == client.get("/workbench").text == page.text
        general = client.get("/")
        assert general.headers["cache-control"] == "no-store"
        assert "script-src 'self'" in general.headers["content-security-policy"]
        general_assets = re.findall(
            r'(?:src|href)="(/static/general[^"?]*\?v=([a-f0-9]{64}))"', general.text,
        )
        assert len(general_assets) == 2
        for url, version in general_assets:
            assert hashlib.sha256(client.get(url).content).hexdigest() == version
