"""Reproduce historical mixed assets and verify compatible UI startup without provider calls."""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
from pathlib import Path
from urllib.parse import urlparse

from playwright.sync_api import expect, sync_playwright

ROOT = Path(__file__).resolve().parents[1]
HISTORICAL_REF = "2854ede"


def run(output: Path) -> None:
    output.mkdir(parents=True, exist_ok=True)
    old_script = subprocess.check_output(
        ["git", "show", f"{HISTORICAL_REF}:v3/web/agent.js"], cwd=ROOT,
    ).decode()
    old_html = subprocess.check_output(
        ["git", "show", f"{HISTORICAL_REF}:v3/web/agent.html"], cwd=ROOT,
    ).decode()
    current_html = (ROOT / "web/agent.html").read_text()
    for name in ("agent.css", "agent.js", "agent-product.js"):
        digest = hashlib.sha256((ROOT / "web" / name).read_bytes()).hexdigest()
        current_html = current_html.replace(f"/static/{name}\"", f"/static/{name}?v={digest}\"")
    records: list[dict] = []
    checks: list[str] = []
    api = "/api/v3/repository-analyses"
    profile = {
        "ready": True, "automatic_execution": True, "token_budget_mode": "manual",
        "allowed_repositories": ["*"],
        "checks": {"model_configured": True, "github_configured": True,
                   "paid_enabled": True, "authorization_valid": True},
        "model": {"model_id": "deepseek-flash", "context_tokens": 1048576},
        "limits": {"max_cost_micros": 1000000, "max_input_tokens": 60000,
                   "max_total_tokens": 60000},
    }
    detail = {
        "run": {"run_id": "run-fixture", "thread_id": "thread-fixture",
                "repository": "example/repo", "commit": "a" * 40,
                "task": "这个项目用的什么框架", "execution_mode": "live",
                "run_status": "failed", "input_tokens_used": 30505,
                "output_tokens_used": 0, "limits": profile["limits"],
                "stop_reason": "MODEL_RESPONSE_LIMIT"},
        "tool_calls": [], "events": [], "claims": [], "draft": {},
        "report": None, "last_event_id": 0,
        "continuation": {"state": "unresolved"},
    }
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        for kind, document, script in (
            ("historical-script-new-document", current_html, old_script),
            ("new-script-historical-document", old_html, None),
            ("new-script-missing-required-node",
             current_html.replace('id="send-icon"', 'id="obsolete-send-icon"'), None),
            ("matched-assets", current_html, None),
        ):
            context = browser.new_context(viewport={"width": 1280, "height": 900})
            page = context.new_page()
            errors: list[dict] = []
            requests: list[str] = []
            authenticated = [False]
            document_value = [document]
            page.add_init_script("""
                window.__assetStreams = [];
                class ControlledEventSource {
                  static OPEN = 1;
                  constructor(url) { this.url = url; this.readyState = 1; this.listeners = {};
                    window.__assetStreams.push(this); }
                  addEventListener(kind, callback) { (this.listeners[kind] ||= []).push(callback); }
                  close() { this.readyState = 2; }
                }
                window.EventSource = ControlledEventSource;
            """)
            page.on("pageerror", lambda error, errors=errors: errors.append(
                {"message": str(error), "stack": error.stack}
            ))

            def route_request(
                route, _request=None, *, requests=requests, authenticated=authenticated,
                document_value=document_value, script=script,
            ) -> None:
                request = route.request
                path = urlparse(request.url).path
                requests.append(request.url)
                if path.startswith("/api/"):
                    if path == "/api/v3/auth/login":
                        authenticated[0] = True
                    if path == "/api/v3/auth/logout":
                        authenticated[0] = False
                    if path.startswith("/api/v3/auth/"):
                        value = {"authenticated": authenticated[0], "setup_required": False,
                                 "username": "fixture", "csrf_token": "fixture"}
                    elif path == "/api/v3/settings/analysis":
                        value = {"revision": 1, "profile": profile}
                    elif path == api + "/profile":
                        value = profile
                    elif path == api:
                        value = [detail["run"]]
                    elif path == api + "/run-fixture":
                        value = detail
                    elif path == api + "/run-fixture/messages":
                        route.fulfill(status=409, json={"code": "ANALYSIS_USAGE_UNRESOLVED"})
                        return
                    elif path == "/api/v3/agent/profile":
                        value = {"projects": [], "model": {"available": True,
                                 "credential_present": True, "model_id": "deepseek-flash"}}
                    elif path == "/api/v3/agent/sessions":
                        value = []
                    else:
                        raise AssertionError("Unexpected API: " + path)
                    route.fulfill(json=value)
                elif path == "/":
                    route.fulfill(content_type="text/html", body=document_value[0])
                elif path == "/static/agent.js" and script is not None:
                    route.fulfill(content_type="text/javascript", body=script)
                else:
                    file = ROOT / "web" / path.removeprefix("/static/")
                    assert file.is_file(), path
                    content_type = {".css": "text/css", ".js": "text/javascript",
                                    ".svg": "image/svg+xml"}[file.suffix]
                    route.fulfill(content_type=content_type, body=file.read_bytes())

            page.route("**/*", route_request)
            page.goto("http://127.0.0.1:18086/")
            if kind == "historical-script-new-document":
                page.wait_for_timeout(100)
                assert any("textContent" in item["message"] and "updateMode" in item["stack"]
                           for item in errors), errors
                checks.append(
                    "historical agent.js with the new document reproduces null.textContent"
                )
            elif kind != "matched-assets":
                expect(page.locator("#page-reload-dialog")).to_be_visible()
                expect(page.locator("#page-reload-dialog")).to_contain_text("页面与界面脚本版本不一致")
                assert not any("/api/" in item for item in requests), requests
                assert not errors, errors
                page.screenshot(path=str(output / (kind + ".png")), full_page=True)
                document_value[0] = current_html
                page.get_by_role("button", name="刷新页面", exact=True).click()
                expect(page.locator("#login-submit")).to_have_text("登录")
                assert page.locator("#page-reload-dialog").count() == 0
                assert not errors, errors
                checks.append(kind + " blocks initialization and refreshes to compatible assets")
            else:
                expect(page.locator("#login-submit")).to_have_text("登录")
                page.locator("#username").fill("fixture")
                page.locator("#password").fill("controlled-password")
                page.locator("#login-submit").click()
                expect(page.locator("#app-shell")).to_be_visible()
                page.locator("#mode-analysis").click()
                page.locator("#history button").first.click()
                expect(page.locator("#conversation")).to_contain_text("这个项目用的什么框架")
                expect(page.locator(".run-error")).to_contain_text("回执保存超限")
                expect(page.locator(".continuation-note")).to_contain_text("旧调用用量尚未核实")
                before_details = sum(urlparse(url).path == api + "/run-fixture" for url in requests)
                page.evaluate("async () => { for (let i = 0; i < 3; i++) await poll(); }")
                assert not errors, errors
                after_details = sum(urlparse(url).path == api + "/run-fixture" for url in requests)
                assert before_details == after_details
                assert page.locator("#notice").inner_text() == ""
                page.locator("#question").fill("继续分析")
                page.locator("#send").click()
                expect(page.locator("#notice")).to_contain_text("费用预占已保留")
                expect(page.locator("#notice")).to_contain_text("可以继续追问")
                expect(page.locator("#question")).to_have_value("继续分析")
                checks.append(
                    "unresolved terminal calls do not trigger automatic detail retries"
                )
                detail["run"]["stop_reason"] = "MODEL_RESPONSE_RECORDING_FAILED"
                detail["continuation"]["state"] = "ready"
                page.evaluate("selectRun('run-fixture')")
                expect(page.locator(".run-error")).to_contain_text("模型用量已核实")
                assert page.locator(".continuation-note").count() == 0
                checks.append("known recording failure is distinguished from unknown usage")
                detail["run"]["run_status"] = "cancelled"
                detail["continuation"]["state"] = "inflight"
                page.evaluate("selectRun('run-fixture')")
                expect(page.locator(".continuation-note")).to_contain_text("最多自动追踪两分钟")
                page.evaluate("poll()")
                page.evaluate("""() => {
                    const wait = state.continuationWaits.get('run-fixture');
                    wait.started = Date.now() - 120001;
                }""")
                before_wait = sum(urlparse(url).path == api + "/run-fixture" for url in requests)
                page.evaluate("async () => { for (let i = 0; i < 3; i++) await poll(); }")
                after_wait = sum(urlparse(url).path == api + "/run-fixture" for url in requests)
                assert before_wait == after_wait
                expect(page.locator("#notice")).to_contain_text("已停止自动刷新")
                checks.append("cancelled in-flight calls stop polling after two minutes")
                detail["run"]["run_status"] = "running"
                detail["run"]["stop_reason"] = "MODEL_RESPONSE_LIMIT"
                page.evaluate("selectRun('run-fixture')")
                expect(page.locator("#send-label")).to_have_text("发送补充")
                expect(page.locator("#question")).to_be_enabled()
                detail["run"]["run_status"] = "failed"
                detail["continuation"]["state"] = "unresolved"
                page.evaluate("""async () => {
                    const stream = window.__assetStreams.filter(s => s.readyState === 1).at(-1);
                    await Promise.all(stream.listeners.done.map(fn => fn({data: '{}'})));
                }""")
                expect(page.locator(".run-error")).to_contain_text("回执保存超限")
                expect(page.locator("#send-label")).to_have_text("继续追问")
                assert page.evaluate("window.__assetStreams.every(s => s.readyState === 2)")
                assert not errors, errors
                checks.append("stream completion reloads the result and closes the connection")
                page.locator("#open-settings").click()
                expect(page.locator("#settings-dialog")).to_be_visible()
                page.locator("#close-settings").click()
                page.locator("#mode-coding").click()
                expect(page.locator("#product-panel")).to_be_visible()
                page.locator("#mode-analysis").click()
                expect(page.locator("#repository-url")).to_be_visible()
                assert not errors, errors
                assert page.locator("#composer-hint, #question-shortcuts").count() == 0
                assert page.locator("#question").get_attribute("placeholder") is None
                assets = [url for url in requests if "/static/agent" in url and ".svg" not in url]
                assert assets and all("?v=" in item for item in assets), assets
                page.locator("#open-settings").click()
                page.locator("#logout").click()
                expect(page.locator("#auth-screen")).to_be_visible()
                assert not errors, errors
                checks.append(
                    "matched versioned assets support login, logout, modes, history and settings"
                )
            records.append({"case": kind, "errors": errors, "requests": requests})
            context.close()
        browser.close()
    report = {"schema": "analysis-ui-asset-version-check-v1", "passed": len(checks),
              "checks": checks, "cases": records, "paid_model_calls": 0,
              "scope": "Controlled browser asset selection; no live HTTP cache/provider experiment"}
    (output / "summary.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps({k: v for k, v in report.items() if k != "cases"}, ensure_ascii=False))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, required=True)
    run(parser.parse_args().output_dir)
