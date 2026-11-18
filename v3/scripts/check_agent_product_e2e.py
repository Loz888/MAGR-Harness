"""Natural-language browser -> real API/PG/Docker acceptance, with a controlled model.

Run with an ephemeral Playwright dependency and installed Chromium:
uv run --with playwright python scripts/check_agent_product_e2e.py --output-dir PATH
No real model requests are allowed. Only a uniquely named disposable DB is mutated.
"""

from __future__ import annotations

import argparse
import json
import secrets
import socket
import tempfile
import threading
import time
from contextlib import ExitStack, asynccontextmanager
from pathlib import Path
from urllib.parse import urlparse
from uuid import uuid4

import httpx
import psycopg
import uvicorn
from playwright.sync_api import expect, sync_playwright
from psycopg import sql
from psycopg.conninfo import make_conninfo
from pydantic import SecretStr

from mgar_v3.api import create_app
from mgar_v3.harness.contracts import sha256_text
from mgar_v3.harness.service import CodingTaskRequest, DeterministicCodingModel
from mgar_v3.settings import ROOT, Settings
from mgar_v3.store.migrate import migrate


class ControlledWorkflow(DeterministicCodingModel):
    """Tool proposals for new-file and green-baseline acceptance, with no inference."""

    def __init__(self, scenario: str) -> None:
        self.scenario = scenario
        self.step = 0

    def complete(self, request, cancellation):
        cancellation.ensure_active()
        step = self.step
        self.step += 1
        if self.scenario == "static":
            if step == 0:
                files = {
                    "index.html": "<!doctype html><html><head>"
                    '<link rel="stylesheet" href="app.css">'
                    '<script src="app.js" defer></script></head><body><h1>Preview fixture</h1>'
                    '<p id="status">Loading</p></body></html>',
                    "app.css": "h1 { color: rgb(17, 94, 89); }\n",
                    "app.js": 'document.getElementById("status").textContent="JS READY";\n',
                }
                return self._tool(
                    request,
                    "apply_edits",
                    {
                        "expected_tree": request.workspace_tree,
                        "edits": [
                            {
                                "path": path,
                                "expected_sha256": None,
                                "create": True,
                                "content": content,
                            }
                            for path, content in files.items()
                        ],
                    },
                )
            if step == 1:
                return self._tool(request, "run_command", {"command_id": "build"})
            return self._text("页面已创建并通过构建/内容检查；未运行行为测试。")
        if step == 0:
            return self._tool(request, "read_file", {"path": "calc.py"})
        if step == 1:
            return self._tool(
                request,
                "apply_edits",
                {
                    "expected_tree": request.workspace_tree,
                    "edits": [
                        {
                            "path": "test_regression.py",
                            "expected_sha256": None,
                            "create": True,
                            "content": "import unittest\nfrom calc import add\n"
                            "class Regression(unittest.TestCase):\n"
                            "    def test_both_operands(self): self.assertEqual(add(2, 3), 5)\n",
                        }
                    ],
                },
            )
        if step in {2, 4}:
            return self._tool(request, "run_tests", {"command_id": "tests"})
        if step == 3:
            old = "def add(a, b):\n    return a - b\n"
            return self._tool(
                request,
                "apply_edits",
                {
                    "expected_tree": request.workspace_tree,
                    "edits": [
                        {
                            "path": "calc.py",
                            "expected_sha256": sha256_text(old),
                            "content": "def add(a, b):\n    return a + b\n",
                        }
                    ],
                },
            )
        return self._text("新增回归测试复现了未覆盖的问题，修复后两项测试通过。")


def run(output: Path) -> None:
    output.mkdir(parents=True, exist_ok=True)
    with socket.socket() as probe:
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        probe.bind(("127.0.0.1", 18081))
    base_dsn = Settings().database_url.get_secret_value()
    database = "product_test_" + uuid4().hex
    dsn = make_conninfo(base_dsn, dbname=database)
    username, password = "product-ui-owner", secrets.token_urlsafe(24)
    checks: list[str] = []
    errors: list[str] = []
    preview_responses: list[dict] = []
    browser_console: list[str] = []
    preview_dom: list[dict] = []
    failed_requests: list[dict] = []
    requests: list[dict] = []
    model_inputs: list[dict] = []
    remote_attempts: list[str] = []
    result_rows: list[dict] = []
    server = None
    thread = None
    created = False
    send = httpx.HTTPTransport.handle_request
    async_send = httpx.AsyncHTTPTransport.handle_async_request

    def guarded(self, request):
        if request.url.host not in {"127.0.0.1", "localhost", "::1"}:
            remote_attempts.append(request.url.host)
            raise AssertionError("Real provider/network requests are forbidden in this check")
        return send(self, request)

    async def async_guarded(self, request):
        if request.url.host not in {"127.0.0.1", "localhost", "::1"}:
            remote_attempts.append(request.url.host)
            raise AssertionError("Real provider/network requests are forbidden in this check")
        return await async_send(self, request)

    httpx.HTTPTransport.handle_request = guarded
    httpx.AsyncHTTPTransport.handle_async_request = async_guarded
    try:
        with psycopg.connect(base_dsn, autocommit=True) as conn:
            conn.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(database)))
        created = True
        migrate(dsn)
        with (
            tempfile.TemporaryDirectory(prefix="harness-product-e2e-") as temporary,
            ExitStack() as cleanup,
        ):
            area = Path(temporary)
            project = area / "project"
            project.mkdir()
            (project / "calc.py").write_text("def add(a, b):\n    return a - b\n")
            (project / "test_calc.py").write_text(
                "import unittest\nfrom calc import add\n"
                "class AddTests(unittest.TestCase):\n"
                "    def test_positive(self): self.assertEqual(add(2, 3), 5)\n"
                "    def test_negative(self): self.assertEqual(add(-2, 3), 1)\n"
            )
            config = Settings(
                _env_file=None,
                database_url=SecretStr(dsn),
                artifact_root=area / "artifacts",
                harness_root=area / "harness",
                harness_image_lock=ROOT / ".local/r03/sandbox-lock.json",
                local_actor=username,
                allowed_actors=[username],
                local_token=SecretStr(secrets.token_urlsafe(32)),
                project_id="product-browser-check",
                repository="local/product-browser-check",
                deepseek_api_key=None,
                allow_paid=True,
                allow_external_write=False,
                github_read_token=None,
                github_write_token=None,
                github_use_gh_token=False,
                live_profile_path=None,
                live_authorization_path=None,
                repo_analysis_authorization_path=None,
                repair_profile_path=None,
                repair_authorization_path=None,
                router_mode="disabled",
            )
            app = create_app(config, run_background=False)
            original_lifespan = app.router.lifespan_context

            def factory(request: CodingTaskRequest):
                assert not request.old_text and not request.new_text and not request.target_path
                model_inputs.append(
                    {
                        "objective": request.objective,
                        "kind": request.task_kind,
                        "intent": request.product_intent,
                    }
                )
                if "静态页面" in request.objective:
                    return ControlledWorkflow("static")
                if "回归复现" in request.objective:
                    return ControlledWorkflow("regression")
                comment = "注释" in request.objective
                return DeterministicCodingModel(
                    task_kind=request.task_kind,
                    target_path="calc.py",
                    old_text="    return a + b\n" if comment else "    return a - b\n",
                    new_text=(
                        "    # Preserve addition behavior.\n    return a + b\n"
                        if comment
                        else "    return a + b\n"
                    ),
                    test_argv=tuple(request.product_plan["commands"]["tests"]["argv"]),
                )

            @asynccontextmanager
            async def lifespan(application):
                async with original_lifespan(application):
                    application.state.harness.model_factory = factory
                    yield

            app.router.lifespan_context = lifespan
            server = uvicorn.Server(
                uvicorn.Config(app, host="127.0.0.1", port=18081, log_level="warning")
            )
            thread = threading.Thread(target=server.run, daemon=True)
            thread.start()
            # Shut down in-flight readiness/preview work before removing its source paths.
            cleanup.callback(thread.join, timeout=30)
            cleanup.callback(setattr, server, "should_exit", True)
            deadline = time.monotonic() + 30
            while not server.started and thread.is_alive() and time.monotonic() < deadline:
                time.sleep(0.05)
            assert server.started, "Disposable UI server did not start"
            with sync_playwright() as p:
                browser = p.chromium.launch(headless=True)
                page = browser.new_page(viewport={"width": 1440, "height": 1000})
                page.on("pageerror", lambda error: errors.append(str(error)))
                page.on(
                    "console",
                    lambda message: browser_console.append(message.type + ": " + message.text),
                )

                def observe_response(response):
                    path = urlparse(response.url).path
                    if "/preview/" in path:
                        item = {
                            "status": response.status,
                            "file": path.rsplit("/", 1)[-1],
                            "type": response.request.resource_type,
                        }
                        preview_responses.append(item)
                        print(json.dumps({"preview_response": item}), flush=True)

                page.on("response", observe_response)
                page.on(
                    "requestfailed",
                    lambda request: failed_requests.append(
                        {
                            "file": urlparse(request.url).path.rsplit("/", 1)[-1],
                            "type": request.resource_type,
                            "failure": request.failure,
                        }
                    ),
                )

                def observe(request):
                    path = urlparse(request.url).path
                    if request.method == "POST" and path.startswith("/api/v3/agent/"):
                        requests.append({"path": path, "body": request.post_data_json})

                page.on("request", observe)
                page.goto("http://127.0.0.1:18081/")
                expect(page.locator("#auth-title")).to_have_text("创建你的管理员账户")
                page.locator("#username").fill(username)
                page.locator("#password").fill(password)
                page.locator("#confirm-password").fill(password)
                page.locator("#login-submit").click()
                expect(page.locator("#product-panel")).to_be_visible()
                checks.append("browser account setup, real cookie session and CSRF-protected APIs")
                page.locator("#open-settings").click()
                expect(page.locator("#settings-dialog")).to_be_visible()
                page.locator("#model-id").fill("deepseek-flash")
                page.locator("#model-key").fill("controlled-model-no-network")
                page.locator("#allow-paid").check()
                page.locator("#save-model-settings").click()
                expect(page.locator("#settings-dialog")).not_to_be_visible()
                checks.append(
                    "saved current model and explicit model permission without GitHub credentials"
                )
                page.locator("#product-add-project").click()
                page.locator("#product-project-path").fill(str(project))
                page.locator("#product-project-save").click()
                expect(page.locator("#product-project-dialog")).not_to_be_visible()
                page.locator("#product-setup-action").click()
                expect(page.locator("#product-permission-dialog")).to_be_visible()
                page.locator("#product-permission-save").click()
                expect(page.locator("#product-permission-dialog")).not_to_be_visible()
                checks.append("project registration and once-per-project cumulative budget grant")

                def finish(previous_task_id: str = "") -> dict:
                    until = time.monotonic() + 100
                    detail = None
                    while time.monotonic() < until:
                        page.wait_for_timeout(200)
                        error = page.locator("#product-error").text_content()
                        assert not error, error
                        session_id = page.evaluate("sessionStorage.getItem('mgar-agent-session')")
                        if not session_id:
                            error = page.locator("#product-error").text_content()
                            assert not error, error
                            continue
                        response = page.request.get(
                            "http://127.0.0.1:18081/api/v3/agent/sessions/" + session_id
                        )
                        assert response.ok
                        detail = response.json()
                        if detail.get("task_id") == previous_task_id:
                            continue
                        if detail["status"] not in {"queued", "running", "waiting_input"}:
                            assert detail["status"] == "completed", detail
                            result = page.request.get(
                                "http://127.0.0.1:18081/api/v3/harness/tasks/" + detail["task_id"]
                            ).json()
                            result_rows.append(result)
                            expect(
                                page.locator(".product-turn").last.locator(".run-status")
                            ).to_have_text("已完成", timeout=10000)
                            return detail
                    raise AssertionError("Task did not finish: " + str(detail))

                page.locator("#product-input").fill("修复 add 的加法错误，并运行测试验证。")
                page.locator("#product-send").click()
                first = finish()
                assert (project / "calc.py").read_text().endswith("return a - b\n")
                assert result_rows[0]["tests"]["run"] == 2
                assert result_rows[0]["tests"]["failed"] == 0
                checks.append(
                    "natural language triggers real Docker fail-edit-pass; "
                    "original project unchanged"
                )

                page.locator("#product-input").fill(
                    "增加一条说明注释，保持加法行为与所有测试通过。"
                )
                page.locator("#product-send").click()
                second = finish(first["task_id"])
                assert second["session_id"] == first["session_id"]
                assert second["task_id"] != first["task_id"]
                assert len(second["turns"]) == 2
                assert result_rows[1]["tests"]["failed"] == 0
                assert "Preserve addition behavior" in result_rows[1]["diff"]
                checks.append(
                    "follow-up starts a new Run in the same session and inherits the first change"
                )

                expect(page.get_by_role("button", name="应用变更到项目").first).to_be_disabled()
                page.get_by_role("button", name="应用变更到项目").last.click()
                expect(page.get_by_text("已应用到项目。", exact=True)).to_be_visible()
                source = (project / "calc.py").read_text()
                assert "Preserve addition behavior" in source and "return a + b" in source
                csrf = page.evaluate("state.csrf")
                repeat = page.request.post(
                    "http://127.0.0.1:18081/api/v3/agent/sessions/"
                    + second["session_id"]
                    + "/apply",
                    headers={"Origin": "http://127.0.0.1:18081", "X-CSRF-Token": csrf},
                    data={
                        "task_id": second["task_id"],
                        "expected_tree": result_rows[1]["tree_after"],
                    },
                )
                assert repeat.ok, repeat.text()
                assert repeat.json()["duplicate"] is True
                assert (project / "calc.py").read_text() == source
                checks.append(
                    "reviewed cumulative diff applies once; "
                    "identical replay returns the recorded receipt"
                )

                page.reload()
                expect(page.locator(".product-turn")).to_have_count(2)
                assert (
                    page.evaluate("sessionStorage.getItem('mgar-agent-session')")
                    == second["session_id"]
                )
                page.screenshot(path=str(output / "browser-conversation.png"), full_page=True)
                checks.append("browser refresh restores the same session and both turns")

                def open_project(path: Path) -> None:
                    page.locator("#new-chat").click()
                    page.locator("#product-add-project").click()
                    page.locator("#product-project-path").fill(str(path))
                    page.locator("#product-project-save").click()
                    expect(page.locator("#product-project-dialog")).not_to_be_visible()
                    page.locator("#product-project-settings").click()
                    page.locator("#product-permission-save").click()
                    expect(page.locator("#product-permission-dialog")).not_to_be_visible()

                site = area / "empty-site"
                site.mkdir()
                open_project(site)
                page.locator("#product-input").fill("创建一个带独立 CSS 和 JavaScript 的静态页面。")
                page.locator("#product-send").click()
                static = finish()
                assert result_rows[-1]["verification_scope"] == "build_only"
                assert result_rows[-1]["tests"]["run"] == 0
                expect(
                    page.get_by_text("仅构建/内容检查；未运行行为测试", exact=True)
                ).to_be_visible()
                assert not (site / "index.html").exists()
                checks.append(
                    "empty-folder creation passes exact build validation "
                    "and exposes build-only scope"
                )
                page.get_by_role("button", name="启动应用预览", exact=True).click()
                frame = page.frame_locator("iframe.product-preview")
                try:
                    expect(frame.locator("#status")).to_have_text("JS READY", timeout=15000)
                finally:
                    preview_dom.append(
                        page.frames[-1].evaluate("""() => ({
                        html: document.documentElement.outerHTML,
                        url: location.href, base: document.baseURI,
                        scripts: [...document.scripts].map(s => ({src:s.src,type:s.type})),
                    })""")
                    )
                expect(frame.locator("h1")).to_have_css("color", "rgb(17, 94, 89)")
                isolated = page.frames[-1].evaluate(
                    "() => {try {parent.document.body; return false} catch {return true}}"
                )
                assert isolated
                control_access = page.frames[-1].evaluate("""async () => {
                    try {
                        const response = await fetch('http://127.0.0.1:18081/api/v3/agent/profile',
                            {credentials:'include'});
                        return {blocked: false, status: response.status};
                    } catch {return {blocked: true};}
                }""")
                assert control_access.get("blocked") or control_access.get("status") in {401, 403}
                preview_url = page.locator("iframe.product-preview").get_attribute("src")
                page.screenshot(path=str(output / "browser-static-preview.png"), full_page=True)
                page.get_by_role("button", name="停止预览", exact=True).click()
                expect(page.locator("iframe.product-preview")).to_have_count(0)
                expired = page.request.get("http://127.0.0.1:18081" + preview_url)
                assert expired.status in {403, 404, 409}
                checks.append(
                    "real static preview loads HTML/CSS/JS in an opaque origin "
                    "and stops its container"
                )
                checks.append(
                    "preview cannot read control APIs; stopped capability cannot serve files"
                )
                page.get_by_role("button", name="应用变更到项目").click()
                expect(page.get_by_text("已应用到项目。", exact=True)).to_be_visible()
                assert all((site / path).is_file() for path in ("index.html", "app.css", "app.js"))
                checks.append("reviewed newly created files apply to the original empty project")

                uncovered = area / "untested-bug"
                uncovered.mkdir()
                (uncovered / "calc.py").write_text("def add(a, b):\n    return a - b\n")
                (uncovered / "test_smoke.py").write_text(
                    "import unittest\nfrom calc import add\n"
                    "class Smoke(unittest.TestCase):\n"
                    "    def test_zero(self): self.assertEqual(add(2, 0), 2)\n"
                )
                open_project(uncovered)
                page.locator("#product-input").fill(
                    "修复未被现有测试覆盖的加法错误，先新增回归复现再修复。"
                )
                page.locator("#product-send").click()
                green = finish()
                assert result_rows[-1]["intent"] == "bugfix"
                assert result_rows[-1]["tests"]["run"] == 2
                assert result_rows[-1]["tests"]["failed"] == 0
                assert "test_regression.py" in result_rows[-1]["diff"]
                page.screenshot(path=str(output / "browser-green-baseline.png"), full_page=True)
                checks.append(
                    "green baseline permits adding a failing reproducer, fixing, "
                    "then passing regression"
                )
                assert len({first["session_id"], static["session_id"], green["session_id"]}) == 3
                grant_calls = [item for item in requests if item["path"].endswith("/authorization")]
                task_calls = [
                    item
                    for item in requests
                    if item["path"].endswith("/sessions") or item["path"].endswith("/messages")
                ]
                assert len(grant_calls) == 3 and len(task_calls) == 4
                assert set(task_calls[0]["body"]) == {"project_id", "text", "client_id"}
                assert set(task_calls[1]["body"]) == {"text", "client_id"}
                assert [row["kind"] for row in model_inputs] == ["feature"] * 4
                assert [row["intent"] for row in model_inputs] == [
                    "bugfix",
                    "feature",
                    "feature",
                    "bugfix",
                ]
                checks.append(
                    "no task-kind, replacement hint or repeated consent supplied by the browser"
                )
                assert not remote_attempts and not errors, (remote_attempts, errors)
                checks.append(
                    "controlled model only; zero remote provider calls and browser exceptions"
                )
                browser.close()
            server.should_exit = True
            thread.join(timeout=30)
            assert not thread.is_alive()
            with psycopg.connect(dsn, autocommit=True) as conn:
                rows = conn.execute(
                    "SELECT status,count(*) FROM app.usage_calls GROUP BY status"
                ).fetchall()
                usage = {status: count for status, count in rows}
                assert not usage.get("unknown", 0) and not usage.get("reserved", 0), usage
                count = conn.execute(
                    "SELECT count(*) FROM app.harness_project_authorizations"
                ).fetchone()[0]
                assert count == 3
                grant_count = conn.execute(
                    "SELECT count(DISTINCT profile_hash) FROM app.harness_tasks"
                ).fetchone()[0]
                assert grant_count == 3
            checks.append(
                "real PG records one shared authorization per project; no unknown/reserved usage"
            )
    finally:
        if server is not None:
            server.should_exit = True
        if thread is not None:
            thread.join(timeout=30)
        httpx.HTTPTransport.handle_request = send
        httpx.AsyncHTTPTransport.handle_async_request = async_send
        if created:
            assert database.startswith("product_test_") and len(database) == 45
            with psycopg.connect(base_dsn, autocommit=True) as conn:
                conn.execute(
                    sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(database))
                )
        (output / "diagnostics.json").write_text(
            json.dumps(
                {
                    "preview_responses": preview_responses,
                    "browser_console": browser_console,
                    "preview_dom": preview_dom,
                    "failed_requests": failed_requests,
                    "errors": errors,
                    "paid_calls": 0,
                },
                ensure_ascii=False,
                indent=2,
            )
            + "\n"
        )
    summary = {
        "schema": "harness-product-browser-e2e-v1",
        "controlled_model": True,
        "model_adapter": "DeterministicCodingModel_test_injection_only",
        "paid_calls": 0,
        "postgres": "real_disposable_database_removed",
        "docker": "real_pinned_H04_H06_runtime",
        "passed": len(checks),
        "checks": checks,
        "errors": errors,
        "turns": [
            {
                "task_id": row["task_id"],
                "status": row["status"],
                "verification_scope": row.get("verification_scope"),
                "intent": row.get("intent"),
                "tests": row["tests"],
                "model_calls": row["model_calls"],
            }
            for row in result_rows
        ],
    }
    (output / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps(summary, ensure_ascii=False))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, required=True)
    run(parser.parse_args().output_dir)
