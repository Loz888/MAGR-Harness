"""Check GitHub coding handoff and exact publication approvals with controlled APIs."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from urllib.parse import urlparse

from playwright.sync_api import expect, sync_playwright

ROOT = Path(__file__).resolve().parents[1]
BASE = "/api/v3/agent"
GITHUB = BASE + "/github"
ANALYSIS = "/api/v3/repository-analyses"


def run(output: Path) -> None:
    output.mkdir(parents=True, exist_ok=True)
    checks: list[str] = []
    errors: list[str] = []
    calls: list[dict] = []
    commit, tree, head = "a" * 40, "b" * 64, "c" * 40
    project = {
        "project_id": "github-project",
        "repository": "example/repo",
        "path": "/isolated/repo",
        "base_commit": commit,
    }
    analysis = {
        "run_id": "analysis-one",
        "thread_id": "thread-one",
        "repository": "example/repo",
        "commit": commit,
        "task": "定位并修复仓库问题，运行测试后准备 PR",
        "execution_mode": "live",
        "run_status": "succeeded",
    }
    result = {
        "task_id": "task-one",
        "status": "completed",
        "summary": "修复边界行为并补充回归测试",
        "tree_after": tree,
        "changes": [{"path": "app.py", "change": "modified"}],
        "diff": "--- a/app.py\n+++ b/app.py\n@@ -1 +1 @@\n-return None\n+return 1\n",
        "tests": {"run": 4, "failed": 0},
        "commands": [{"argv": ["python", "-m", "unittest"], "exit_code": 0}],
        "patch_url": "/api/v3/harness/tasks/task-one/patch",
    }
    session = {
        "session_id": "session-one",
        "project_id": project["project_id"],
        "repository": "example/repo",
        "title": "修复仓库问题",
        "task_id": "task-one",
        "status": "completed",
        "turns": [
            {"task_id": "task-one", "text": "修复边界行为", "status": "completed", "result": result}
        ],
    }
    grant = {
        "eligible": True,
        "repository": "example/repo",
        "commit": commit,
        "base_branch": "main",
        "writer_available": False,
        "valid": False,
        "revision": 0,
        "allow_publish": False,
        "allow_merge": False,
    }
    delivery: dict = {}
    merge_delivery: dict = {}
    merge_ready = [False]
    profile = {
        "ready": True,
        "automatic_execution": True,
        "model": {"model_id": "deepseek-flash"},
        "allowed_repositories": ["*"],
        "limits": {"max_cost_micros": 1000000, "max_total_tokens": 10000000},
        "checks": {},
    }

    def route_request(route) -> None:
        request = route.request
        path, method = urlparse(request.url).path, request.method
        body = request.post_data_json if request.post_data else None
        if path.startswith("/api/"):
            calls.append({"path": path, "method": method, "body": body})
            if path == "/api/v3/auth/session":
                value = {"authenticated": True, "username": "fixture", "csrf_token": "fixture"}
            elif path == "/api/v3/settings/analysis":
                value = {"revision": 1, "profile": profile}
            elif path == ANALYSIS + "/profile":
                value = profile
            elif path == ANALYSIS:
                value = [analysis]
            elif path == ANALYSIS + "/analysis-one":
                value = {
                    "run": analysis,
                    "tool_calls": [],
                    "claims": [],
                    "report": "已定位边界行为，后续需要隔离调试。",
                    "continuation": {"state": "ready"},
                    "last_event_id": 0,
                }
            elif path == BASE + "/profile":
                value = {
                    "projects": [project],
                    "model": {
                        "available": True,
                        "credential_present": True,
                        "model_id": "deepseek-flash",
                    },
                }
            elif path == BASE + "/projects/github-project/readiness":
                value = {
                    "project_id": "github-project",
                    "authorization": {"valid": True},
                    "environment": {"status": "ready"},
                }
            elif path == BASE + "/sessions":
                value = [session]
            elif path == BASE + "/sessions/session-one":
                value = session
            elif path == "/api/v3/harness/tasks/task-one":
                value = result
            elif path == GITHUB + "/projects" and method == "POST":
                assert body == {
                    "repository_url": "https://github.com/example/repo",
                    "commit": commit,
                }
                value = {
                    **project,
                    "github_source": {"repository": "example/repo", "commit": commit},
                }
            elif path == GITHUB + "/projects/github-project/authorization":
                if method == "POST":
                    assert body["expected_revision"] == grant["revision"]
                    grant.update(body, revision=grant["revision"] + 1, valid=body["allow_publish"])
                value = grant
            elif path == GITHUB + "/profile":
                value = {
                    "writer_available": grant["writer_available"],
                    "credential_setting": "MGAR_V3_GITHUB_WRITE_TOKEN",
                    "enable_setting": "MGAR_V3_ALLOW_EXTERNAL_WRITE=1",
                    "analysis_read_only": True,
                }
            elif path == GITHUB + "/sessions/session-one/review":
                assert body["task_id"] == "task-one" and body["expected_tree"] == tree
                delivery.update(
                    publication_id="delivery-one",
                    action_type="draft_pr",
                    task_id="task-one",
                    status="review",
                    payload_hash="d" * 64,
                    bundle={
                        "repository": "example/repo",
                        "base_sha": commit,
                        "branch": "harness/review-one",
                        "title": body["title"],
                        "body": body["body"],
                        "diff": result["diff"],
                    },
                    diff=result["diff"],
                    tests=result["tests"],
                    writer_available=grant["writer_available"],
                    steps=[],
                )
                value = delivery
            elif path == GITHUB + "/sessions/session-one/deliveries":
                value = [delivery] if delivery else []
            elif path == GITHUB + "/deliveries/delivery-one/approve":
                assert body == {"payload_hash": delivery["payload_hash"]}
                delivery.update(
                    status="outcome_unknown",
                    steps=[
                        {
                            "ordinal": 1,
                            "kind": "draft_pull_request",
                            "status": "outcome_unknown",
                            "receipt": {"internal": "PRIVATE_RECEIPT_MUST_NOT_RENDER"},
                        }
                    ],
                )
                value = delivery
            elif path == GITHUB + "/deliveries/delivery-one/reconcile":
                delivery.update(
                    status="succeeded",
                    steps=[{"ordinal": 1, "kind": "draft_pull_request", "status": "succeeded"}],
                    pull_request={
                        "number": 12,
                        "draft": True,
                        "head_sha": head,
                        "base_sha": commit,
                        "url": "https://github.com/example/repo/pull/12",
                    },
                )
                value = delivery
            elif path == GITHUB + "/deliveries/delivery-one/resume":
                assert body == {"payload_hash": delivery["payload_hash"]}
                route.fulfill(
                    status=409,
                    json={"code": "GITHUB_STEP_UNCONFIRMED", "message": "远端步骤尚未确认"},
                )
                return
            elif path == GITHUB + "/deliveries/delivery-one/merge-review":
                value = {
                    "pull_number": 12,
                    "head_sha": head,
                    "base_sha": commit,
                    "review_hash": "e" * 64,
                    "can_merge": merge_ready[0],
                    "blockers": [] if merge_ready[0] else ["DRAFT_PULL_REQUEST"],
                    "checks": [{"name": "unit", "state": "success"}],
                }
            elif path == GITHUB + "/deliveries/delivery-one/merge":
                assert body == {
                    "pull_number": 12,
                    "head_sha": head,
                    "base_sha": commit,
                    "review_hash": "e" * 64,
                    "confirm": True,
                }
                merge_delivery.update(
                    publication_id="merge-one",
                    action_type="merge_pr",
                    status="ready",
                    payload_hash="e" * 64,
                    writer_available=True,
                    steps=[],
                    bundle={},
                )
                delivery["merge_actions"] = [{"publication_id": "merge-one", "status": "ready"}]
                value = merge_delivery
            elif path == GITHUB + "/deliveries/merge-one/dispatch":
                assert body == {"payload_hash": merge_delivery["payload_hash"]}
                merge_delivery["status"] = "outcome_unknown"
                route.fulfill(
                    status=409,
                    json={
                        "code": "GITHUB_DISPATCH_STALE",
                        "message": "仅能派发原已批准且从未发送的动作",
                    },
                )
                return
            elif path == GITHUB + "/deliveries/merge-one":
                value = merge_delivery
            elif path == GITHUB + "/deliveries/delivery-one":
                delivery["writer_available"] = grant["writer_available"]
                value = delivery
            else:
                errors.append("Unexpected API: " + method + " " + path)
                route.fulfill(status=404, json={"code": "UNEXPECTED_FIXTURE_REQUEST"})
                return
            route.fulfill(json=value)
            return
        file = ROOT / "web" / ("agent.html" if path == "/" else path.removeprefix("/static/"))
        if not file.is_file():
            route.abort()
            return
        route.fulfill(
            content_type={
                ".html": "text/html",
                ".js": "text/javascript",
                ".css": "text/css",
                ".svg": "image/svg+xml",
            }[file.suffix],
            body=file.read_bytes(),
        )

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        page = browser.new_page(viewport={"width": 1280, "height": 900}, locale="zh-CN")
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.route("**/*", route_request)
        page.goto("http://127.0.0.1:18087/")
        page.locator("#mode-analysis").click()
        page.locator("#history button").first.click()
        page.get_by_role("button", name="在隔离工作区修复并准备 PR").click()
        modal = page.locator(".product-github-dialog")
        expect(modal).to_contain_text(commit)
        assert not any(x["path"] == GITHUB + "/projects" for x in calls)
        checks.append(
            "analysis handoff displays fixed source and requires an explicit preparation click"
        )
        modal.get_by_role("button", name="准备隔离项目").click()
        expect(modal).to_have_count(0)
        expect(page.locator("#product-panel")).to_be_visible()
        expect(page.locator("#product-input")).to_have_value(analysis["task"])
        assert not any(x["method"] == "POST" and x["path"] == BASE + "/sessions" for x in calls)
        checks.append(
            "fixed snapshot preserves objective without starting a model or granting publication"
        )

        page.locator("#history button").first.click()
        page.get_by_role("button", name="审查 GitHub PR 交付").click()
        modal = page.locator(".product-github-dialog")
        expect(modal).to_contain_text("服务器尚未配置 GitHub 写入凭据")
        expect(modal).to_contain_text("MGAR_V3_GITHUB_WRITE_TOKEN")
        expect(modal).to_contain_text("MGAR_V3_ALLOW_EXTERNAL_WRITE=1")
        expect(modal).to_contain_text("GitHub 只读 Token 仅用于读取")
        modal.get_by_label("允许向此仓库创建 Draft PR（有效期一天）").check()
        modal.get_by_label("允许审查后合并 PR（每次仍需确认准确版本）").check()
        modal.get_by_role("button", name="保存 GitHub 交付权限").click()
        expect(modal).to_contain_text("交付权限已保存")
        assert not any(x["path"].endswith("/approve") for x in calls)
        checks.append(
            "separate expiring repository grant preserves exact revision and makes no remote write"
        )
        modal.get_by_role("button", name="生成完整 PR 审查提案").click()
        expect(modal).to_contain_text(delivery["payload_hash"])
        expect(modal.locator("pre.coding-diff")).to_have_text(result["diff"])
        modal.get_by_label("我已审查完整 Diff、验证回执和提案摘要，确认创建此 Draft PR。").check()
        expect(modal.get_by_role("button", name="批准并创建 Draft PR")).to_be_disabled()
        checks.append(
            "canonical diff and tests precede publication; missing writer blocks approval"
        )
        modal.get_by_role("button", name="关闭", exact=True).click()
        page.evaluate("sessionStorage.removeItem('mgar-github-delivery:session-one:task-one')")
        grant["writer_available"] = True
        page.get_by_role("button", name="审查 GitHub PR 交付").click()
        modal = page.locator(".product-github-dialog")
        expect(modal).to_contain_text(delivery["payload_hash"])
        assert len([x for x in calls if x["path"].endswith("/review")]) == 1
        expect(modal.get_by_role("button", name="批准并创建 Draft PR")).to_be_disabled()
        modal.get_by_label("我已审查完整 Diff、验证回执和提案摘要，确认创建此 Draft PR。").check()
        modal.get_by_role("button", name="批准并创建 Draft PR").click()
        expect(modal.locator(".github-delivery-state")).to_have_attribute(
            "data-status", "outcome_unknown"
        )
        expect(modal.get_by_role("button", name="批准并创建 Draft PR")).to_have_count(0)
        assert "PRIVATE_RECEIPT_MUST_NOT_RENDER" not in modal.inner_text()
        checks.append(
            "server-restored proposal needs exact approval; unknown publication is never resent"
        )
        expect(modal.get_by_role("button", name="核对并继续未发送步骤")).to_be_disabled()
        modal.get_by_label("核对已发出的步骤后，允许沿已批准提案继续尚未发送的步骤。").check()
        modal.get_by_role("button", name="核对并继续未发送步骤").click()
        expect(modal).to_contain_text("远端步骤尚未确认")
        expect(modal.locator(".github-delivery-state")).to_have_attribute(
            "data-status", "outcome_unknown"
        )
        checks.append(
            "explicit resume still blocks when a previously sent remote step is unconfirmed"
        )
        modal.get_by_role("button", name="只读核对未知回执").click()
        expect(modal.get_by_role("link", name="查看 PR #12")).to_have_attribute(
            "href", "https://github.com/example/repo/pull/12"
        )
        assert len([x for x in calls if x["path"].endswith("/approve")]) == 1
        checks.append(
            "unknown outcome offers read-only reconciliation and shows the returned PR URL"
        )
        modal.get_by_role("button", name="审查是否可合并").click()
        expect(modal).to_contain_text("DRAFT_PULL_REQUEST")
        expect(modal.get_by_role("button", name="确认合并此版本")).to_have_count(0)
        checks.append("server merge blockers remain visible and prevent confirmation")
        modal.get_by_role("button", name="关闭", exact=True).click()
        merge_ready[0] = True
        page.get_by_role("button", name="审查 GitHub PR 交付").click()
        modal = page.locator(".product-github-dialog")
        modal.get_by_role("button", name="审查是否可合并").click()
        expect(modal.locator(".product-github-merge")).to_contain_text(head)
        expect(modal.locator(".product-github-merge")).to_contain_text(commit)
        expect(modal.get_by_role("button", name="确认合并此版本")).to_be_disabled()
        modal.get_by_label(
            "我已审查这个 PR 的变更与检查结果，确认合并上面显示的准确 Head 和 Base。"
        ).check()
        modal.get_by_role("button", name="确认合并此版本").click()
        expect(modal.locator(".github-delivery-state")).to_have_attribute("data-status", "ready")
        assert len([x for x in calls if x["path"].endswith("/merge")]) == 1
        checks.append(
            "merge requires a fresh server review and explicit exact PR/head/base/hash confirmation"
        )
        modal.get_by_role("button", name="关闭", exact=True).click()
        page.get_by_role("button", name="审查 GitHub PR 交付").click()
        modal = page.locator(".product-github-dialog")
        expect(modal.get_by_role("button", name="审查是否可合并")).to_have_count(0)
        modal.get_by_role("button", name="查看合并回执").click()
        expect(modal.locator(".github-delivery-state")).to_have_attribute("data-status", "ready")
        expect(modal.get_by_role("button", name="继续已批准派发")).to_be_enabled()
        modal.get_by_role("button", name="继续已批准派发").click()
        expect(modal).to_contain_text("仅能派发原已批准且从未发送的动作")
        modal.get_by_role("button", name="刷新交付状态").click()
        expect(modal.locator(".github-delivery-state")).to_have_attribute(
            "data-status", "outcome_unknown"
        )
        expect(modal.get_by_role("button", name="继续已批准派发")).to_have_count(0)
        assert len([x for x in calls if x["path"].endswith("/dispatch")]) == 1
        checks.append(
            "ready merge can request exact approved dispatch; stale unknown state blocks sending"
        )
        merge_delivery["status"] = "succeeded"
        modal.get_by_role("button", name="刷新交付状态").click()
        expect(modal.locator(".github-delivery-state")).to_have_attribute(
            "data-status", "succeeded"
        )
        checks.append("pending merge is restored as a separate action and cannot be approved twice")
        page.set_viewport_size({"width": 390, "height": 844})
        assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
        box = modal.bounding_box()
        assert box and box["width"] <= 390 and box["x"] >= 0
        page.screenshot(path=str(output / "delivery-mobile.png"), full_page=True)
        checks.append("delivery review and exact version text fit a mobile viewport")
        assert not errors, errors
        checks.append("no browser errors or unexpected API calls")
        browser.close()
    report = {
        "mode": "controlled-github-delivery-ui-contract",
        "checks": checks,
        "passed": len(checks),
        "errors": errors,
        "paid_model_calls": 0,
        "external_writes": 0,
        "live_execution": "NOT_RUN",
        "requests": calls,
    }
    (output / "summary.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    print(
        json.dumps(
            {key: value for key, value in report.items() if key != "requests"}, ensure_ascii=False
        )
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", required=True, type=Path)
    run(parser.parse_args().output_dir)
