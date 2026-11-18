"""Exercise GitHub analysis controls with controlled APIs and EventSource; no model calls."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from urllib.parse import urlparse

from playwright.sync_api import expect, sync_playwright

ROOT = Path(__file__).resolve().parents[1]
API = "/api/v3/repository-analyses"


def run(output: Path) -> None:
    output.mkdir(parents=True, exist_ok=True)
    checks: list[str] = []
    errors: list[str] = []
    calls: list[dict] = []
    pending = [False]
    fail_active_once = [False]
    profile = {
        "ready": True,
        "automatic_execution": True,
        "token_budget_mode": "manual",
        "allowed_repositories": ["*"],
        "checks": {
            "model_configured": True,
            "github_configured": True,
            "paid_enabled": True,
            "authorization_valid": True,
        },
        "model": {"model_id": "deepseek-flash", "context_tokens": 1048576},
        "limits": {
            "max_cost_micros": 1000000,
            "max_input_tokens": 60000,
            "max_total_tokens": 60000,
        },
    }
    details: dict[str, dict] = {}

    def add_run(task: str, parent: str | None = None, status: str = "running") -> dict:
        ident = "00000000-0000-4000-8000-" + str(len(details) + 1).zfill(12)
        run = {
            "run_id": ident,
            "thread_id": details[parent]["run"]["thread_id"] if parent else "thread-" + ident,
            "parent_run_id": parent,
            "repository": "example/repo",
            "commit": "a" * 40,
            "task": task,
            "execution_mode": "live",
            "run_status": status,
            "input_tokens_used": 40500 if status == "failed" else 2000,
            "output_tokens_used": 604 if status == "failed" else 100,
            "limits": dict(profile["limits"]),
            "stop_reason": "TOKEN_BUDGET_EXHAUSTED" if status == "failed" else None,
        }
        details[ident] = {
            "run": run,
            "events": [],
            "last_event_id": 0,
            "tool_calls": [],
            "draft": {},
            "claims": [],
            "report": None,
            "budget": {"limit_micros": 1000000, "settled_micros": 13151, "reserved_micros": 0},
            "interaction": {
                "state": "running" if status == "running" else "settled",
                "can_message": True,
                "question": None,
                "messages": [],
            },
        }
        return run

    historical = add_run("核对累计用量", status="failed")

    def route_request(route) -> None:
        request = route.request
        path = urlparse(request.url).path
        body = request.post_data_json if request.post_data else None
        if path.startswith("/api/"):
            calls.append({"path": path, "method": request.method, "body": body})
            if path == "/api/v3/auth/session":
                value = {"authenticated": True, "username": "fixture", "csrf_token": "fixture"}
            elif path == "/api/v3/settings/analysis":
                if request.method == "PUT":
                    profile["token_budget_mode"] = body["token_budget_mode"]
                    profile["limits"] = dict(body["limits"])
                    if body["token_budget_mode"] == "cost":
                        profile["limits"].update(
                            max_input_tokens=10000000, max_total_tokens=10000000
                        )
                value = {"revision": 1, "profile": profile}
            elif path == "/api/v3/agent/profile":
                value = {
                    "projects": [],
                    "model": {
                        "available": True,
                        "credential_present": True,
                        "model_id": "deepseek-flash",
                    },
                }
            elif path == "/api/v3/agent/sessions":
                value = []
            elif path == API + "/profile":
                value = profile
            elif path == API:
                value = [x["run"] for x in reversed(list(details.values()))]
                if request.method == "POST":
                    value = add_run(body["task"])
            elif path.startswith(API + "/"):
                ident = path.removeprefix(API + "/").split("/")[0]
                detail = details[ident]
                if path.endswith("/cancel"):
                    detail["run"]["run_status"] = "cancelled"
                    detail["interaction"].update(state="settled", can_message=False, question=None)
                    pending[0] = True
                    value = detail["run"]
                elif path.endswith("/resume"):
                    detail["run"]["run_status"] = "running"
                    value = detail["run"]
                elif path.endswith("/messages"):
                    if pending[0]:
                        route.fulfill(status=409, json={"code": "ANALYSIS_CALL_PENDING"})
                        return
                    if detail["run"]["run_status"] in {"queued", "running", "waiting_input"}:
                        interaction = detail["interaction"]
                        if not any(
                            item["client_request_id"] == body["client_request_id"]
                            for item in interaction["messages"]
                        ):
                            interaction["messages"].append(
                                {
                                    "event_id": len(interaction["messages"]) + 1,
                                    "client_request_id": body["client_request_id"],
                                    "body": body["body"],
                                    "status": "queued",
                                }
                            )
                        interaction.update(state="running", question=None)
                        detail["run"].update(run_status="running", stop_reason=None)
                        if fail_active_once[0]:
                            fail_active_once[0] = False
                            route.fulfill(status=503, json={"message": "受控回执中断"})
                            return
                        value = detail["run"]
                    else:
                        value = add_run(body["body"], parent=ident)
                else:
                    value = detail
            else:
                errors.append("Unexpected API: " + path)
                route.fulfill(status=404, json={"code": "NOT_FOUND"})
                return
            route.fulfill(json=value)
            return
        file = ROOT / "web" / ("agent.html" if path == "/" else path.removeprefix("/static/"))
        if not file.is_file():
            route.abort()
            return
        kind = {
            ".html": "text/html",
            ".js": "text/javascript",
            ".css": "text/css",
            ".svg": "image/svg+xml",
        }[file.suffix]
        route.fulfill(content_type=kind, body=file.read_bytes())

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        page = browser.new_page(viewport={"width": 1280, "height": 900}, locale="zh-CN")
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.add_init_script("""
            window.__analysisStreams = [];
            class ControlledEventSource {
              static OPEN = 1;
              constructor(url) { this.url = url; this.readyState = 1; this.listeners = {};
                window.__analysisStreams.push(this); }
              addEventListener(kind, callback) { (this.listeners[kind] ||= []).push(callback); }
              close() { this.readyState = 2; }
            }
            window.EventSource = ControlledEventSource;
            window.__analysisEmit = (message) => {
              const source = window.__analysisStreams.filter(x => x.readyState === 1).at(-1);
              for (const fn of source.listeners.update || []) fn({data: JSON.stringify(message)});
            };
        """)
        page.route("**/*", route_request)
        page.goto("http://127.0.0.1:18085/")
        expect(page.locator("#app-shell")).to_be_visible()
        page.locator("#mode-analysis").click()
        expect(page.locator("#repository-url")).to_be_visible()
        assert page.locator("#question").get_attribute("placeholder") is None
        assert page.locator("#question-shortcuts, #composer-hint").count() == 0
        checks.append(
            "analysis composer has no placeholder, keyboard hint or provider/budget tagline"
        )

        page.locator("#history button").first.click()
        expect(page.locator(".run-meta")).to_contain_text("41,104 / 60,000")
        expect(page.locator(".run-meta")).to_contain_text("US$0.013151")
        expect(page.locator(".run-meta")).to_contain_text("费用上限 US$1.00")
        expect(page.locator(".run-error")).to_contain_text("分别约束")
        checks.append(
            "usage shows independent Token cap and actual ledger cost with dollar ceiling"
        )

        page.locator("#open-settings").click()
        expect(page.locator("#token-budget-mode")).to_have_value("manual")
        expect(page.locator("#token-limit")).to_have_value("60000")
        expect(page.locator("#token-limit")).to_be_enabled()
        page.locator("#token-budget-mode").select_option("cost")
        expect(page.locator("#token-budget-settings")).to_be_hidden()
        expect(page.locator("#token-budget-help")).to_contain_text("10,000,000")
        assert not any(x["method"] == "PUT" for x in calls)
        page.locator("#save-model-settings").click()
        expect(page.locator("#settings-dialog")).not_to_be_visible()
        saved = next(x for x in calls if x["method"] == "PUT")["body"]
        assert (
            saved["token_budget_mode"] == "cost" and saved["limits"]["max_cost_micros"] == 1000000
        )
        assert historical["limits"]["max_total_tokens"] == 60000
        checks.append(
            "old manual limit stays unchanged until explicit cost-mode save; old run stays frozen"
        )

        page.locator("#new-chat").click()
        page.locator("#repository-url").fill("https://github.com/example/repo")
        page.locator("#question").fill("调查请求入口")
        page.locator("#question").press("Enter")
        expect(page.locator("#send-label")).to_have_text("中止")
        expect(page.locator("#send")).to_be_enabled()
        assert page.locator("#question").input_value() == ""
        assert page.locator("#send").get_attribute("type") == "button"
        assert page.locator(".run-controls button").count() == 1
        expect(page.locator(".run-controls button")).to_have_text("在隔离工作区修复并准备 PR")
        expect(page.locator(".analysis-progress")).to_be_visible()
        checks.append(
            "sending turns the same button into enabled stop, with no separate pause/cancel"
        )

        ident = list(details)[-1]
        expect(page.locator("#question")).to_be_enabled()
        page.locator("#question").fill("同时检查取消时的边界行为")
        expect(page.locator("#send-label")).to_have_text("发送补充")
        fail_active_once[0] = True
        before_runs = len(details)
        page.locator("#send").click()
        expect(page.locator("#notice")).to_contain_text("受控回执中断")
        expect(page.locator("#question")).to_have_value("同时检查取消时的边界行为")
        page.locator("#send").click()
        expect(page.locator("#send-label")).to_have_text("中止")
        expect(page.locator(".analysis-user-update")).to_contain_text("等待安全点处理")
        attempts = [x for x in calls if x["path"] == API + "/" + ident + "/messages"]
        assert len(attempts) == 2
        assert (
            attempts[-2]["body"]["client_request_id"] == attempts[-1]["body"]["client_request_id"]
        )
        assert len(details) == before_runs
        assert len(details[ident]["interaction"]["messages"]) == 1
        assert len([x for x in calls if x["path"].endswith("/cancel")]) == 0
        checks.append("active input submits to same run; ambiguous retry retains text and identity")
        page.evaluate(
            "__analysisEmit({event_id:1,kind:'tool_reserved',payload:{tool:'search_code',reasoning:'PRIVATE_REASONING_MUST_NOT_RENDER'}})"
        )
        expect(page.locator(".analysis-progress")).to_contain_text("正在搜索代码")
        page.evaluate(
            "__analysisEmit({event_id:2,kind:'tool_observed',payload:{tool:'search_code',byte_count:2048}})"
        )
        expect(page.locator(".analysis-progress")).to_contain_text("已读取 2,048 字节")
        page.evaluate(
            "__analysisEmit({event_id:3,kind:'model_stream_started',payload:{"
            "phase:'validation',reasoning:'PRIVATE_REASONING_MUST_NOT_RENDER',"
            "text:'<img src=x onerror=window.uiInjected=true>'}})"
        )
        expect(page.locator(".analysis-progress strong")).to_have_text("正在核对回答完整性")
        page.evaluate(
            "__analysisEmit({event_id:4,kind:'reasoning_delta',payload:{text:'PRIVATE_REASONING_MUST_NOT_RENDER'}})"
        )
        assert "PRIVATE_REASONING_MUST_NOT_RENDER" not in page.locator("#conversation").inner_text()
        assert page.evaluate("window.uiInjected === undefined")
        checks.append("loading streams tools and verification using only allowed public fields")

        details[ident]["interaction"]["messages"][0]["status"] = "applied"
        details[ident]["last_event_id"] = 5
        page.evaluate(
            "__analysisEmit({event_id:5,kind:'analysis_input_applied',payload:{message_event_ids:[1],reasoning:'PRIVATE_REASONING_MUST_NOT_RENDER'}})"
        )
        expect(page.locator(".analysis-message-status")).to_have_text("已纳入本轮调查")
        details[ident]["run"].update(run_status="waiting_input", stop_reason="INPUT_REQUIRED")
        details[ident]["interaction"].update(
            state="waiting_input",
            question={
                "question": "优先检查哪个入口？",
                "options": ["HTTP 入口", "CLI 入口"],
                "reason_code": "INPUT_REQUIRED",
            },
        )
        page.evaluate("selectRun", ident)
        expect(page.locator(".analysis-question")).to_contain_text("优先检查哪个入口？")
        expect(page.locator("#send-label")).to_have_text("中止")
        before_messages = len([x for x in calls if x["path"].endswith("/messages")])
        page.locator(".analysis-question-options button").filter(has_text="HTTP 入口").click()
        expect(page.locator("#question")).to_have_value("HTTP 入口")
        assert len([x for x in calls if x["path"].endswith("/messages")]) == before_messages
        page.locator("#question").press("Enter")
        expect(page.locator("#send-label")).to_have_text("中止")
        expect(page.locator(".analysis-question")).to_have_count(0)
        assert len(details) == before_runs
        checks.append(
            "waiting question uses main composer and resumes same run only after explicit send"
        )

        page.screenshot(path=str(output / "running-desktop.png"), full_page=True)
        page.locator("#send").click()
        expect(page.locator("#send-label")).to_have_text("继续追问")
        expect(page.locator("#question")).to_be_enabled()
        assert len([x for x in calls if x["path"] == API + "/" + ident + "/cancel"]) == 1
        assert not any(x["path"].endswith("/pause") for x in calls)
        expect(page.locator("#notice")).to_contain_text("停稳")
        checks.append(
            "empty composer can stop through its single button and explains pending receipt"
        )

        page.locator("#question").fill("沿已有证据继续调查")
        page.locator("#send").click()
        expect(page.locator("#notice")).to_contain_text("仍在核实")
        expect(page.locator("#question")).to_have_value("沿已有证据继续调查")
        pending[0] = False
        page.locator("#send").click()
        expect(page.locator("#send-label")).to_have_text("中止")
        next_id = list(details)[-1]
        assert details[next_id]["run"]["parent_run_id"] == ident
        assert details[next_id]["run"]["commit"] == details[ident]["run"]["commit"]
        assert page.locator("#conversation .turn").count() == 2
        attempts = [x for x in calls if x["path"] == API + "/" + ident + "/messages"]
        assert attempts[0]["body"]["client_request_id"] == attempts[1]["body"]["client_request_id"]
        checks.append("pending call keeps text; settled retry retains identity and context")

        historical["run_status"] = "paused"
        historical["stop_reason"] = None
        page.locator("#history button").filter(has_text="核对累计用量").click()
        expect(page.locator("#send-label")).to_have_text("继续执行")
        page.locator("#question").fill("保留这条尚未发送的问题")
        previous_messages = len([x for x in calls if x["path"].endswith("/messages")])
        page.locator("#question").press("Enter")
        expect(page.locator("#send-label")).to_have_text("发送补充")
        expect(page.locator("#question")).to_have_value("保留这条尚未发送的问题")
        assert any(x["path"].endswith("/resume") for x in calls)
        assert len([x for x in calls if x["path"].endswith("/messages")]) == previous_messages
        checks.append("legacy paused run resumes identically by keyboard and preserves unsent text")

        details[next_id]["run"].update(run_status="failed", stop_reason="USAGE_UNKNOWN")
        details[next_id]["continuation"] = {
            "state": "unresolved",
            "explicit_followup_allowed": True,
            "unresolved_model_calls": 1,
        }
        details[next_id]["conversation_budget"] = {
            "max_total_tokens": 10000000,
            "input_tokens_used": 349447,
            "output_tokens_used": 3030,
            "settled_micros": 108483,
            "reserved_micros": 33117,
            "limit_micros": 1000000,
        }
        page.evaluate("selectRun", next_id)
        selected = page.locator('[data-run-id="' + next_id + '"]')
        expect(selected.locator(".continuation-note")).to_contain_text("可以继续追问")
        expect(selected.locator(".run-meta")).to_contain_text("352,477 / 10,000,000")
        expect(selected.locator(".run-meta")).to_contain_text("US$0.108483")
        expect(selected.locator(".run-meta")).to_contain_text("预占 US$0.033117")
        expect(page.locator("#send")).to_be_enabled()
        page.locator("#question").fill("沿用原来的证据继续回答")
        page.locator("#send").click()
        expect(page.locator("#send-label")).to_have_text("中止")
        continuation_id = list(details)[-1]
        assert details[continuation_id]["run"]["parent_run_id"] == next_id
        assert details[continuation_id]["run"]["thread_id"] == details[next_id]["run"]["thread_id"]
        assert page.locator("#conversation .turn").count() == 3
        page.evaluate(
            "__analysisEmit({event_id:11,kind:'tool_reused',payload:{"
            "tool:'read_file',reasoning:'PRIVATE_REASONING_MUST_NOT_RENDER'}})"
        )
        expect(page.locator(".analysis-progress")).to_contain_text("已复用本会话的授权证据")
        assert "PRIVATE_REASONING_MUST_NOT_RENDER" not in page.locator("body").inner_text()
        checks.append("unknown usage retains reservation and memory across explicit follow-up")
        checks.append("conversation totals and public cache reuse are shown without private text")

        waiting = add_run("等待补充时也允许明确中止", status="waiting_input")
        waiting["stop_reason"] = "INPUT_REQUIRED"
        details[waiting["run_id"]]["interaction"].update(
            state="waiting_input",
            can_message=True,
            question={"question": "要优先检查哪个入口？", "options": []},
        )
        page.evaluate("selectRun", waiting["run_id"])
        expect(page.locator("#question")).to_have_value("")
        expect(page.locator("#send-label")).to_have_text("中止")
        assert page.locator("#send").get_attribute("type") == "button"
        before_messages = len([x for x in calls if x["path"].endswith("/messages")])
        page.locator("#send").click()
        expect(page.locator("#send-label")).to_have_text("继续追问")
        assert waiting["run_status"] == "cancelled"
        assert (
            len([x for x in calls if x["path"] == API + "/" + waiting["run_id"] + "/cancel"]) == 1
        )
        assert len([x for x in calls if x["path"].endswith("/messages")]) == before_messages
        checks.append("INPUT_REQUIRED wait remains cancellable through the empty unified composer")

        legacy = add_run("旧调查停止记录不冒充活动等待", status="waiting_input")
        legacy["stop_reason"] = "AGENT_STALLED"
        details[legacy["run_id"]]["interaction"].update(state="settled", can_message=False)
        page.evaluate("selectRun", legacy["run_id"])
        expect(page.locator("#send-label")).to_have_text("继续追问")
        assert page.locator("#send").get_attribute("type") == "submit"
        page.locator("#send").click()
        assert not any(x["path"] == API + "/" + legacy["run_id"] + "/cancel" for x in calls)
        checks.append("historical AGENT_STALLED waiting records retain terminal follow-up behavior")
        page.evaluate("selectRun", continuation_id)

        page.set_viewport_size({"width": 390, "height": 844})
        expect(page.locator("#send")).to_be_visible()
        assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
        box = page.locator("#send").bounding_box()
        assert box and box["x"] >= 0 and box["x"] + box["width"] <= 390
        page.screenshot(path=str(output / "running-mobile.png"), full_page=True)
        checks.append(
            "mobile analysis progress and unified send/stop control remain within viewport"
        )
        assert not errors, errors
        checks.append("no browser errors, HTML execution, or unexpected requests")
        browser.close()
    report = {
        "mode": "controlled-analysis-ui-contract",
        "checks": checks,
        "passed": len(checks),
        "errors": errors,
        "paid_model_calls": 0,
        "backend_execution": "NOT_RUN_in_this_check",
        "requests": calls,
    }
    (output / "summary.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps({k: v for k, v in report.items() if k != "requests"}, ensure_ascii=False))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, required=True)
    run(parser.parse_args().output_dir)
