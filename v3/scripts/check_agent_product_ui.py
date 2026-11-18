"""Browser contract check with explicitly controlled API responses; no model calls.

Run: uv run --with playwright python scripts/check_agent_product_ui.py --output-dir PATH
This checks the shipped frontend, not live model quality or backend execution.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from urllib.parse import urlparse

from playwright.sync_api import expect, sync_playwright

ROOT = Path(__file__).resolve().parents[1]


def run(output: Path) -> None:
    output.mkdir(parents=True, exist_ok=True)
    project = {"project_id": "workspace-demo", "repository": "local/demo", "path": "/example/demo"}
    model = {
        "available": True,
        "credential_present": True,
        "model_id": "deepseek-flash",
        "configured_model_id": "deepseek-flash",
        "recommended_model_id": "deepseek-flash",
    }
    authorization = {"valid": False, "allow_write": False, "allow_paid": False}
    calls: list[dict] = []
    sessions: list[dict] = []
    results: dict[str, dict] = {}
    errors: list[str] = []
    checks: list[str] = []
    fail_once = [False]
    question_ready = [False]
    reject_once = [False]

    def profile() -> dict:
        return {
            "ready": False,
            "automatic_execution": True,
            "model": {
                "model_id": model["configured_model_id"],
                "context_tokens": 1048576,
                "context_policy": model.get("context_policy"),
            },
            "checks": {
                "model_configured": model["credential_present"],
                "github_configured": False,
                "paid_enabled": model["available"],
                "authorization_valid": model["available"],
            },
            "limits": {"max_cost_micros": 250000, "max_total_tokens": 500000},
        }

    def route_request(route) -> None:
        request = route.request
        path = urlparse(request.url).path
        method = request.method
        body = request.post_data_json if request.post_data else None
        if path.startswith("/api/"):
            calls.append({"method": method, "path": path, "body": body})
            data: object = None
            if path == "/api/v3/auth/session":
                data = {
                    "authenticated": True,
                    "username": "demo",
                    "csrf_token": "fixture",
                    "setup_required": False,
                }
            elif path == "/api/v3/settings/analysis":
                if method == "PUT":
                    model.update(
                        available=True,
                        credential_present=True,
                        model_id=body["model_id"],
                        configured_model_id=body["model_id"],
                        context_policy=body.get("context_policy"),
                    )
                data = {"revision": 1, "profile": profile()}
            elif path == "/api/v3/agent/profile":
                data = {"model": model, "projects": [project]}
            elif path == "/api/v3/agent/projects/workspace-demo/readiness":
                data = {
                    "project_id": project["project_id"],
                    "authorization": authorization,
                    "environment": {"status": "ready", "issues": []},
                }
            elif path == "/api/v3/agent/projects/workspace-demo/authorization":
                authorization.update(body, valid=True)
                data = authorization
            elif path == "/api/v3/agent/projects/workspace-demo/environment":
                data = {"status": "ready", "image_lock": body["image_lock"]}
            elif path == "/api/v3/agent/sessions" and method == "GET":
                data = sessions
            elif path == "/api/v3/agent/sessions" and method == "POST":
                if reject_once[0]:
                    reject_once[0] = False
                    route.fulfill(
                        status=409,
                        json={"code": "FIXTURE_CONFIGURATION", "message": "受控配置阻塞"},
                    )
                    return
                if fail_once[0]:
                    fail_once[0] = False
                    route.fulfill(
                        status=503,
                        json={"code": "FIXTURE_TRANSIENT", "message": "受控临时失败，输入已保留"},
                    )
                    return
                session = {
                    "session_id": "session-demo",
                    "project_id": project["project_id"],
                    "repository": project["repository"],
                    "title": body["text"],
                    "turns": [],
                }
                sessions.append(session)
                data = add_turn(session, body["text"])
            elif path == "/api/v3/agent/sessions/session-demo":
                data = sessions[0]
            elif path == "/api/v3/agent/sessions/session-demo/messages":
                current = sessions[0]["task_id"]
                if results[current]["status"] == "running":
                    sessions[0]["turns"].append(
                        {"task_id": current, "text": body["text"], "status": "running"}
                    )
                    data = sessions[0]
                    question_ready[0] = True
                else:
                    data = add_turn(sessions[0], body["text"])
            elif path == "/api/v3/agent/sessions/session-demo/preview":
                data = {
                    "preview_url": "/api/v3/agent/sessions/session-demo/preview/",
                    "status": "ready" if method == "POST" else "stopped",
                }
            elif path == "/api/v3/agent/sessions/session-demo/preview/":
                route.fulfill(
                    content_type="text/html", body="<h1>Controlled application preview</h1>"
                )
                return
            elif path.startswith("/api/v3/harness/tasks/"):
                task_id = path.split("/")[5]
                result = results[task_id]
                if path.endswith("/events"):
                    events = [
                        ("update", {"cursor": 1, "kind": "run_started", "payload": {}}),
                        (
                            "update",
                            {
                                "cursor": 2,
                                "kind": "assistant_message",
                                "payload": {
                                    "text": '<img src=x onerror="window.uiInjected=true">',
                                    "reasoning": "PRIVATE_REASONING_MUST_NOT_RENDER",
                                },
                            },
                        ),
                    ]
                    if question_ready[0]:
                        events.append(
                            (
                                "update",
                                {
                                    "cursor": 3,
                                    "kind": "ask_user",
                                    "payload": {
                                        "question_id": "question-one",
                                        "question": "期望的行为是什么？",
                                    },
                                },
                            )
                        )
                    if result["status"] == "completed":
                        events.append(("done", {"status": "completed"}))
                    stream = "retry: 100\n\n" + "".join(
                        "event: " + name + "\ndata: " + json.dumps(item) + "\n\n"
                        for name, item in events
                    )
                    route.fulfill(content_type="text/event-stream", body=stream)
                    return
                if path.endswith("/messages"):
                    result["status"] = "completed"
                    sessions[0]["turns"][-1]["status"] = "completed"
                    sessions[0]["status"] = "completed"
                    data = {"message": {"message_id": body["message_id"]}}
                else:
                    data = result
            elif path == "/api/v3/agent/sessions/session-demo/apply":
                data = {"status": "applied", "conflicts": []}
            elif path == "/api/v3/repository-analyses":
                data = []
            else:
                errors.append("Unexpected API request: " + method + " " + path)
                route.fulfill(status=404, json={"code": "UNEXPECTED_FIXTURE_REQUEST"})
                return
            route.fulfill(json=data)
            return
        if path == "/":
            file = ROOT / "web/agent.html"
        elif path.startswith("/static/"):
            file = ROOT / "web" / path.removeprefix("/static/")
        else:
            route.abort()
            return
        if not file.is_file():
            route.abort()
            return
        kind = {
            ".html": "text/html",
            ".js": "text/javascript",
            ".css": "text/css",
            ".svg": "image/svg+xml",
        }.get(file.suffix)
        route.fulfill(
            body=file.read_bytes(),
            content_type=kind,
            headers={
                "Content-Security-Policy": (
                    "default-src 'self'; script-src 'self'; style-src 'self'; "
                    "connect-src 'self'; frame-ancestors 'none'; object-src 'none'; "
                    "base-uri 'none'; form-action 'self'"
                )
            },
        )

    def add_turn(session: dict, text: str) -> dict:
        task_id = "h08-" + f"{len(session['turns']) + 1:012x}"
        result = {
            "task_id": task_id,
            "status": "running" if text == "持续执行并询问信息" else "completed",
            "summary": "已完成受控 UI 契约示例。",
            "tree_after": "f" * 64,
            "live_model": False,
            "tests": {"run": 2, "failed": 0},
            "model_calls": 0,
            "cost_micros": 0,
            "changes": [{"path": "calc.py", "change": "modified"}],
            "diff": "--- calc.py\n+++ calc.py\n@@ -1 +1 @@\n-return a-b\n+return a+b\n",
            "commands": [{"argv": ["python3", "-m", "unittest"], "exit_code": 0}],
            "patch_url": "/api/v3/harness/tasks/" + task_id + "/patch",
        }
        results[task_id] = result
        session["turns"].append(
            {"task_id": task_id, "text": text, "status": result["status"], "result": result}
        )
        session.update(task_id=task_id, status=result["status"])
        return {"session_id": session["session_id"], "task_id": task_id, "status": result["status"]}

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 1440, "height": 1000}, device_scale_factor=1)
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.route("**/*", route_request)
        page.goto("http://127.0.0.1:18881/")
        expect(page.locator("#product-panel")).to_be_visible()
        expect(page.locator("#product-setup-action")).to_have_text("设置权限")
        expect(page.locator("#coding-panel")).to_be_hidden()
        expect(page.locator("#settings-dialog")).not_to_be_visible()
        assert page.get_by_text("任务类型", exact=True).count() == 1
        expect(page.get_by_text("任务类型", exact=True)).not_to_be_visible()
        checks.append(
            "default conversational entry; debug fields hidden; local project needs no GitHub token"
        )
        page.screenshot(path=str(output / "welcome.png"), full_page=True)

        # Width alone misses overlapping controls. Check actual hit targets,
        # long titles, both modes, a narrow viewport, and scaled layout.
        for width, zoom in [(1440, 1), (1024, 1), (768, 1), (390, 1), (320, 1), (1000, 1.25)]:
            page.set_viewport_size({"width": width, "height": 1000})
            page.evaluate("zoom => document.body.style.zoom = zoom", str(zoom))
            page.locator("#page-title").evaluate(
                "e => e.textContent = '非常长的项目名称 / context-recovery-and-api-integration'"
            )
            for mode in ["analysis", "coding"]:
                page.locator("#mode-" + mode).click()
                ids = ["mode-analysis", "mode-coding"]
                if mode == "coding":
                    ids += ["product-project", "product-add-project", "product-project-settings"]
                for name in ids:
                    target = page.locator("#" + name)
                    assert target.evaluate("""e => {
                        const r = e.getBoundingClientRect();
                        const hit = document.elementFromPoint(r.x + r.width/2, r.y + r.height/2);
                        return r.width > 20 && r.height > 20 && r.x >= 0 &&
                            r.right <= window.innerWidth + 1 && (hit === e || e.contains(hit));
                    }"""), (width, zoom, mode, name)
            assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
            page.screenshot(path=str(output / f"layout-{width}-{zoom}.png"), full_page=True)
        page.evaluate("document.body.style.zoom = '1'")
        page.set_viewport_size({"width": 390, "height": 844})
        page.locator("#toggle-sidebar").focus()
        page.keyboard.press("Enter")
        expect(page.locator("#close-sidebar")).to_be_visible()
        page.locator("#close-sidebar").click()
        expect(page.locator("#toggle-sidebar")).to_be_focused()
        page.locator("#toggle-sidebar").click()
        page.keyboard.press("Escape")
        expect(page.locator("#toggle-sidebar")).to_have_attribute("aria-expanded", "false")
        page.set_viewport_size({"width": 1440, "height": 1000})
        checks.append(
            "header hit targets fit six width/zoom combinations and sidebar closes by keyboard"
        )

        page.locator("#product-setup-action").click()
        expect(page.locator("#product-permission-dialog")).to_be_visible()
        expect(page.locator("#product-permission-github")).not_to_be_checked()
        page.locator(".product-runtime-settings summary").click()
        page.locator("#product-runtime-lock").fill("/example/prepared/image-lock.json")
        page.locator("#product-runtime-wheelhouse").fill("/example/prepared/wheels")
        page.locator("#product-runtime-save").click()
        expect(page.locator("#product-runtime-error")).to_have_text("项目运行环境已保存。")
        runtime = next(call for call in calls if call["path"].endswith("/environment"))
        assert runtime["body"] == {
            "image_lock": "/example/prepared/image-lock.json",
            "wheelhouse": "/example/prepared/wheels",
        }
        checks.append("optional offline runtime setup binds prepared image and hashed wheelhouse")
        page.locator("#product-permission-save").click()
        expect(page.locator("#product-permission-dialog")).not_to_be_visible()
        grant = next(call for call in calls if call["path"].endswith("/authorization"))
        assert grant["body"]["expected_revision"] == 0
        checks.append("one-time explicit project authorization with shared total budget")

        reject_once[0] = True
        fail_once[0] = True
        page.locator("#product-input").fill("修复加法错误")
        page.locator("#product-send").click()
        expect(page.locator("#product-error")).to_contain_text("受控配置阻塞")
        page.locator("#product-send").click()
        expect(page.locator("#product-error")).to_contain_text("受控临时失败")
        expect(page.locator("#product-input")).to_have_value("修复加法错误")
        page.locator("#product-send").click()
        expect(page.locator(".product-turn")).to_have_count(1)
        initial = [
            call
            for call in calls
            if call["method"] == "POST" and call["path"] == "/api/v3/agent/sessions"
        ]
        assert initial[0]["body"]["client_id"] != initial[1]["body"]["client_id"]
        assert initial[1]["body"] == initial[2]["body"]
        assert set(initial[2]["body"]) == {"text", "project_id", "client_id"}
        checks.append(
            "natural-language-only request; ambiguous retry retains client identity and input"
        )

        page.locator("#product-input").fill("再补一个回归测试")
        expect(page.locator("#product-send")).to_be_enabled()
        page.locator("#product-input").press("Enter")
        expect(page.locator(".product-turn")).to_have_count(2)
        assert (
            len(
                [
                    call
                    for call in calls
                    if call["path"].endswith("/authorization") and call["method"] == "POST"
                ]
            )
            == 1
        )
        expect(page.locator("#product-send-label")).to_have_text("继续对话")
        checks.append("completion follow-up keeps the same session without repeated approvals")

        buttons = page.get_by_role("button", name="应用变更到项目", exact=True)
        expect(buttons.first).to_be_disabled()
        buttons.last.click()
        expect(page.get_by_text("已应用到项目。", exact=True)).to_be_visible()
        apply_call = next(call for call in calls if call["path"].endswith("/apply"))
        assert apply_call["body"]["expected_tree"] == "f" * 64
        assert apply_call["body"]["task_id"] == sessions[0]["task_id"]
        checks.append("latest-turn-only explicit apply binds task and reviewed tree")

        page.get_by_role("button", name="启动应用预览", exact=True).click()
        expect(page.frame_locator(".product-preview").get_by_role("heading")).to_have_text(
            "Controlled application preview"
        )
        frame = page.locator(".product-preview")
        assert frame.get_attribute("sandbox") == "allow-scripts allow-forms"
        isolated = page.frames[-1].evaluate(
            "() => {try {parent.document.body; return false} catch {return true}}"
        )
        assert isolated
        page.get_by_role("button", name="停止预览", exact=True).click()
        expect(frame).to_have_count(0)
        checks.append("preview lifecycle and opaque-origin sandbox protect the application UI")

        page.reload()
        expect(page.locator(".product-turn")).to_have_count(2)
        expect(page.locator("#product-project")).to_have_value("workspace-demo")
        expect(page.locator("#product-project")).to_be_enabled()
        checks.append("page reload restores the persistent conversation and project binding")
        page.screenshot(path=str(output / "conversation.png"), full_page=True)
        latest = results[sessions[0]["task_id"]]
        latest.update(
            verification_scope="build_only", tests={"run": 0, "failed": 0, "status": "not_run"}
        )
        page.reload()
        expect(page.get_by_text("仅构建/内容检查；未运行行为测试", exact=True)).to_be_visible()
        assert "测试 0 项" not in page.locator(".product-turn").last.text_content()
        latest.update(verification_scope="test_suite", tests={"run": 2, "failed": 0})
        checks.append("build-only completion explicitly excludes behavioral test success")

        page.locator("#new-chat").click()
        expect(page.locator("#product-welcome")).to_be_visible()
        page.locator("#product-add-project").click()
        expect(page.locator("#product-project-dialog")).to_be_visible()
        page.locator("#product-project-close").click()
        checks.append(
            "new conversation and project registration remain available with existing projects"
        )

        model.update(available=False, credential_present=False)
        page.reload()
        expect(page.locator("#product-setup-action")).to_have_text("模型设置")
        page.locator("#product-setup-action").click()
        page.locator("#model-key").fill("controlled-ui-only-key")
        page.locator("#context-settings summary").click()
        page.locator("#context-mode").select_option("manual")
        page.locator("#context-window").fill("65536")
        page.locator("#context-prepare").fill("60")
        page.locator("#context-force").fill("80")
        page.locator("#context-target").fill("45")
        page.locator("#allow-paid").check()
        page.locator("#save-model-settings").click()
        expect(page.locator("#settings-dialog")).not_to_be_visible()
        checks.append("unified model settings activate local coding without GitHub credentials")
        assert model["context_policy"] == {
            "mode": "manual",
            "window_tokens": 65536,
            "prepare_ratio": 0.6,
            "force_ratio": 0.8,
            "target_ratio": 0.45,
            "reserve_tokens": 1024,
        }
        page.locator("#open-settings").click()
        expect(page.locator("#context-window")).to_have_value("65536")
        expect(page.locator("#context-force")).to_have_value("80")
        page.screenshot(path=str(output / "context-settings.png"), full_page=True)
        page.locator("#close-settings").click()
        checks.append(
            "context window and compression thresholds save and restore independently of budget"
        )
        model.update(
            available=False,
            credential_present=True,
            model_id="deepseek-chat",
            configured_model_id="deepseek-chat",
        )
        page.reload()
        page.locator("#product-setup-action").click()
        expect(page.locator("#product-model-notice")).to_contain_text("deepseek-chat")
        expect(page.locator("#model-id")).to_have_value("deepseek-flash")
        expect(
            page.locator('#product-model-options option[value="deepseek-v4-pro"]')
        ).to_have_count(1)
        page.locator("#allow-paid").check()
        page.locator("#save-model-settings").click()
        expect(page.locator("#settings-dialog")).not_to_be_visible()
        checks.append("retired model requires explicit save of a current Flash/Pro catalog choice")

        page.locator("#history .history-item").first.click()
        page.locator("#product-input").fill("持续执行并询问信息")
        page.locator("#product-send").click()
        expect(page.locator("#product-question")).to_be_hidden()
        expect(page.locator("#product-input")).to_be_enabled()
        expect(page.locator("#product-send-label")).to_have_text("中止")
        expect(page.locator("#product-pause")).to_be_hidden()
        expect(page.locator("#product-cancel")).to_be_hidden()
        expect(page.get_by_text("PRIVATE_REASONING_MUST_NOT_RENDER", exact=True)).to_have_count(0)
        assert not page.evaluate("Boolean(window.uiInjected)")
        assert page.locator(".product-log img").count() == 0
        checks.append(
            "streamed public events render as text without private reasoning or HTML execution"
        )
        page.locator("#product-input").fill("运行中补充一条要求")
        expect(page.locator("#product-send-label")).to_have_text("发送补充")
        page.locator("#product-send").click()
        expect(page.locator("#product-send")).to_be_enabled()
        expect(page.locator(".product-turn")).to_have_count(3)
        expect(page.locator("#product-question")).to_be_visible()
        checks.append("active instructions retain the current task and leave input available")
        expect(page.locator("#product-answer")).to_be_hidden()
        page.locator("#product-input").fill("返回正确结果并保持其他行为")
        expect(page.locator("#product-send-label")).to_have_text("回复并继续")
        page.locator("#product-input").press("Enter")
        expect(page.locator("#product-question")).to_be_hidden()
        expect(page.locator("#product-send-label")).to_have_text("继续对话")
        answer = next(
            call
            for call in calls
            if call["path"].endswith("/messages") and call["body"].get("kind") == "answer"
        )
        assert answer["body"]["reply_to"] == "question-one"
        checks.append("question answers bind the pending question and execution resumes")

        page.locator("#mode-analysis").click()
        expect(page.locator("#product-panel")).to_be_hidden()
        expect(page.locator("#repository-url")).to_be_visible()
        page.locator("#mode-coding").click()
        expect(page.locator("#product-panel")).to_be_visible()
        checks.append(
            "existing GitHub analysis remains accessible and project mode can be restored"
        )

        page.set_viewport_size({"width": 390, "height": 844})
        expect(page.locator("#product-input")).to_be_visible()
        expect(page.locator("#product-send")).to_be_visible()
        assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
        page.screenshot(path=str(output / "mobile.png"), full_page=True)
        checks.append("mobile composer and actions fit the viewport")
        assert not errors, errors
        checks.append("no unhandled browser errors or unplanned API requests")
        browser.close()
    summary = {
        "mode": "controlled-ui-contract",
        "checks": checks,
        "passed": len(checks),
        "errors": errors,
        "model_calls": 0,
        "backend_execution": "NOT_RUN_in_this_check",
    }
    (output / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps(summary, ensure_ascii=False))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, required=True)
    run(parser.parse_args().output_dir)
