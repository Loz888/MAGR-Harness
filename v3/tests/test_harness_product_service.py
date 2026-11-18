"""Conversational product integration against the real Engine/PostgreSQL.

All model responses are local scripted adapters. Public requests contain only
natural-language text; no paid HTTP transport or external publication is used.
"""

from __future__ import annotations

import json
import threading
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from pydantic import SecretStr

from mgar_v3.coordination.engine import Engine
from mgar_v3.domain import DomainError
from mgar_v3.evidence.artifacts import ArtifactStore
from mgar_v3.harness.contracts import HarnessToolCall, ModelResponse, ModelUsage
from mgar_v3.harness.product import HarnessProductService
from mgar_v3.harness.product_models import ProductDeepSeekChatAdapter
from mgar_v3.harness.service import DeterministicCodingModel, HarnessCodingService
from mgar_v3.web_access import (
    AnalysisSettingsInput,
    BrowserSession,
    LoginInput,
    WebAccounts,
    WebAnalysisSettings,
)

ROOT = Path(__file__).resolve().parents[1]
IMAGE_LOCK = ROOT / ".local/r03/sandbox-lock.json"
KEY_A = "fixture-product-key-A-not-a-real-credential"
KEY_B = "fixture-product-key-B-not-a-real-credential"
BUGGY = "def add(a, b):\n    return a - b\n"
FIXED = "def add(a, b):\n    return a + b\n"
SUITE = (
    "import unittest\nfrom calc import add\n"
    "class CalcTest(unittest.TestCase):\n"
    "    def test_add(self): self.assertEqual(add(2, 3), 5)\n"
)


class RecordedModel:
    def __init__(self, request, observed, *, hook=None):
        self.observed, self.hook = observed, hook
        old = "return a + b" if "再次" in request.objective else "return a - b"
        new = "return (a + b)" if "再次" in request.objective else "return a + b"
        self.inner = DeterministicCodingModel(
            task_kind=request.task_kind,
            target_path="README.md" if request.task_kind == "inspect" else "calc.py",
            old_text=old,
            new_text=new,
            test_argv=tuple(
                request.product_plan.get("commands", {}).get("tests", {}).get("argv", ())
            ),
        )

    def complete(self, request, cancellation):
        self.observed.append(request)
        if self.hook is not None:
            self.hook(request)
        result = self.inner.complete(request, cancellation)
        return result.model_copy(update={"usage": ModelUsage(cost_micros=3)})


@pytest.fixture
def product_env(engine, settings, tmp_path, monkeypatch):
    # Test settings load the workstation .env by design. Explicitly clear its
    # provider flags so negative tests cannot inherit a user's real setup.
    settings.deepseek_api_key = None
    settings.allow_paid = False
    source = tmp_path / "project"
    source.mkdir()
    (source / "README.md").write_text("A small project for controlled product integration.\n")
    requests, model_requests = [], []
    hooks = {"model": None}

    def factory(request):
        requests.append(request)
        return RecordedModel(request, model_requests, hook=hooks["model"])

    harness = HarnessCodingService(
        settings.harness_root,
        actor=settings.local_actor,
        image_lock=IMAGE_LOCK,
        engine=engine,
        model_factory=factory,
    )
    project_id = harness.register_project(str(source))["project_id"]
    accounts = WebAccounts(engine)
    user_id = accounts.create_owner(
        LoginInput(username="owner", password=SecretStr("fixture-password"))
    )
    user = BrowserSession(user_id=user_id, username="owner", csrf_token="fixture-csrf")
    web = WebAnalysisSettings(engine, settings)
    product = HarnessProductService(harness, web)
    # Most tests use synchronous dispatch to inspect committed boundaries
    # deterministically. Running/revocation tests restore submit_task explicitly.
    monkeypatch.setattr(harness, "submit_task", harness.create_task)

    def save_model(key=KEY_A, *, allow_paid=True, model_id="deepseek-flash"):
        revision, _ = web.snapshot()
        web.save(
            AnalysisSettingsInput(
                deepseek_api_key=SecretStr(key),
                allow_paid=allow_paid,
                expected_revision=revision,
                model_id=model_id,
            ),
            user,
        )

    def authorize(*, allow_write=True, calls=32):
        return product.authorize(
            project_id,
            allow_write=allow_write,
            allow_paid=True,
            max_cost_micros=250000,
            max_model_calls=calls,
        )

    yield SimpleNamespace(
        engine=engine,
        settings=settings,
        source=source,
        harness=harness,
        project_id=project_id,
        web=web,
        product=product,
        user=user,
        requests=requests,
        model_requests=model_requests,
        hooks=hooks,
        save_model=save_model,
        authorize=authorize,
    )
    product.close()


def completed_inspection(env, text="解释项目", client_id="first"):
    env.save_model()
    env.authorize()
    session = env.product.create(env.project_id, text, client_id)
    assert session["status"] == "completed"
    return session


def test_model_environment_flags_alone_do_not_authorize_product_spending(product_env):
    env = product_env
    env.settings.deepseek_api_key = SecretStr(KEY_A)
    env.settings.allow_paid = True
    with pytest.raises(DomainError) as error:
        env.product.create(env.project_id, "解释项目", "no-project-authorization")
    assert error.value.code == "HARNESS_PROJECT_AUTHORIZATION_REQUIRED"
    assert not env.requests and not env.model_requests
    with env.engine.transaction() as conn:
        assert conn.execute("SELECT count(*) AS n FROM app.usage_calls").fetchone()["n"] == 0


def test_explicit_project_grant_still_needs_authorized_model_configuration(product_env):
    env = product_env
    env.authorize()
    with pytest.raises(DomainError) as error:
        env.product.create(env.project_id, "解释项目", "missing-model")
    assert error.value.code == "HARNESS_LIVE_MODEL_UNAVAILABLE"
    assert not env.requests
    failed = env.product.sessions()[0]["turns"][0]
    assert failed["state"] == "failed" and not failed["task_id"]


def test_text_only_session_creates_real_run_and_idempotency_does_not_call_model_again(product_env):
    env = product_env
    first = completed_inspection(env)
    duplicate = env.product.create(env.project_id, "解释项目", "first")
    assert duplicate["session_id"] == first["session_id"]
    assert duplicate["task_id"] == first["task_id"] and duplicate["duplicate"]
    assert len(env.requests) == 1 and len(env.model_requests) == 2
    task = env.engine.harness.get(first["task_id"])
    assert task["run_id"].int and task["thread_id"].int
    assert not task["request"]["target_path"]
    assert not task["request"]["old_text"] and not task["request"]["new_text"]
    assert task["request"]["objective"] == "解释项目"
    assert task["request"]["product_model_revision"] == 1
    assert task["status"] == "succeeded"
    with pytest.raises(DomainError) as error:
        env.product.create(env.project_id, "解释另一个项目", "first")
    assert error.value.code == "IDEMPOTENCY_CONFLICT" and len(env.requests) == 1


def test_new_finished_turn_uses_same_authorization_budget_and_distinct_run(product_env):
    env = product_env
    first = completed_inspection(env)
    second = env.product.send(first["session_id"], "再次解释项目", "followup")
    assert second["status"] == "completed" and len(second["turns"]) == 2
    before, after = (env.engine.harness.get(item["task_id"]) for item in (first, second))
    assert before["run_id"] != after["run_id"]
    assert before["thread_id"] == after["thread_id"]
    assert before["profile_hash"] == after["profile_hash"]
    authorization = env.product.authorization(env.project_id)
    assert authorization["used_calls"] == 4
    assert authorization["settled_micros"] == 12 and authorization["reserved_micros"] == 0
    assert len(env.model_requests) == 4
    replay = env.product.send(first["session_id"], "再次解释项目", "followup")
    assert replay["duplicate"] and replay["task_id"] == second["task_id"]
    assert len(env.model_requests) == 4


def test_initial_client_id_is_bound_to_one_project(product_env, tmp_path):
    env = product_env
    first = completed_inspection(env)
    other = tmp_path / "other-project"
    other.mkdir()
    (other / "README.md").write_text("Another repository\n")
    other_id = env.harness.register_project(str(other))["project_id"]
    env.product.authorize(
        other_id, allow_write=True, allow_paid=True, max_cost_micros=250000, max_model_calls=32
    )
    with pytest.raises(DomainError) as error:
        env.product.create(other_id, "解释项目", "first")
    assert error.value.code == "IDEMPOTENCY_CONFLICT"
    assert len(env.product.sessions()) == 1
    assert env.product.get(first["session_id"])["project_id"] == env.project_id
    assert len(env.model_requests) == 2


def test_shared_project_call_limit_is_not_reset_by_new_turn_or_session(product_env):
    env = product_env
    env.save_model()
    env.authorize(calls=3)
    first = env.product.create(env.project_id, "解释项目", "first")
    assert first["status"] == "completed"
    second = env.product.send(first["session_id"], "继续解释项目", "second")
    assert second["status"] == "budget_exhausted"
    assert len(env.model_requests) == 3
    assert env.product.authorization(env.project_id)["used_calls"] == 3
    third = env.product.create(env.project_id, "解释项目", "another-session")
    assert third["status"] == "budget_exhausted"
    assert len(env.model_requests) == 3


def test_frozen_model_revision_survives_settings_update_and_secrets_stay_private(product_env):
    env = product_env
    first = completed_inspection(env)
    original_request = env.requests[0]
    env.save_model(KEY_B, model_id="deepseek-v4-pro")
    key, capabilities = env.product.resolve_model(original_request)
    assert key == KEY_A and capabilities.model_id == "deepseek-flash"
    second = env.product.send(first["session_id"], "解释项目内容", "new-model")
    key, capabilities = env.product.resolve_model(env.requests[-1])
    assert key == KEY_B and capabilities.model_id == "deepseek-v4-pro"
    assert original_request.product_model_revision == 1
    assert env.requests[-1].product_model_revision == 2
    public = json.dumps(
        {
            "profile": env.product.profile(),
            "session": second,
            "task": env.harness.get_task(second["task_id"]),
        },
        default=str,
    )
    assert KEY_A not in public and KEY_B not in public
    with env.engine.transaction() as conn:
        encrypted = conn.execute(
            "SELECT encrypted_payload FROM app.web_analysis_settings"
        ).fetchall()
    assert all(
        KEY_A not in row["encrypted_payload"] and KEY_B not in row["encrypted_payload"]
        for row in encrypted
    )


def test_turn_cannot_reuse_a_revoked_or_replaced_project_grant(product_env):
    env = product_env
    first = completed_inspection(env)
    old_request = env.requests[0]
    env.product.revoke(env.project_id)
    with pytest.raises(DomainError):
        env.product.send(first["session_id"], "解释更多", "revoked")
    assert len(env.model_requests) == 2
    env.authorize()
    with pytest.raises(DomainError) as error:
        env.product.resolve_model(old_request)
    assert error.value.code == "HARNESS_AUTHORIZATION_REVOKED"


def test_engine_recreation_preserves_session_history_and_finished_result(product_env):
    env = product_env
    first = completed_inspection(env)
    env.engine.close()
    replacement = Engine(env.settings, ArtifactStore(env.settings.artifact_root))
    try:
        replacement.recover()
        harness = HarnessCodingService(
            env.settings.harness_root,
            actor="owner",
            image_lock=IMAGE_LOCK,
            engine=replacement,
            model_factory=lambda request: None,
        )
        product = HarnessProductService(harness, WebAnalysisSettings(replacement, env.settings))
        restored = product.get(first["session_id"])
        assert restored["status"] == "completed"
        assert restored["task_id"] == first["task_id"]
        assert restored["turns"][0]["text"] == "解释项目"
        assert harness.get_task(first["task_id"])["tree_after"]
        assert product.sessions()[0]["session_id"] == first["session_id"]
        product.close()
    finally:
        replacement.close()


def test_disabling_model_authorization_blocks_previous_configuration_before_new_call(product_env):
    env = product_env
    completed_inspection(env)
    env.save_model(allow_paid=False)
    with pytest.raises(DomainError) as error:
        env.product.resolve_model(env.requests[0])
    assert error.value.code == "HARNESS_LIVE_MODEL_UNAVAILABLE"
    assert len(env.model_requests) == 2


def prepare_bugfix(env):
    (env.source / "calc.py").write_text(BUGGY)
    (env.source / "test_calc.py").write_text(SUITE)
    # Re-registering records the updated source inventory before authorizing.
    env.project_id = env.harness.register_project(str(env.source))["project_id"]
    env.save_model()
    env.authorize()
    session = env.product.create(env.project_id, "修复加法计算错误", "bugfix")
    assert session["status"] == "completed", env.harness.get_task(session["task_id"])
    assert (env.source / "calc.py").read_text() == BUGGY
    return session


def test_actual_container_bugfix_continues_from_prior_isolated_tree_and_applies_once(product_env):
    env = product_env
    first = prepare_bugfix(env)
    second = env.product.send(first["session_id"], "再次调整加法表达式写法", "followup")
    assert second["status"] == "completed", env.harness.get_task(second["task_id"])
    task = env.harness.get_task(second["task_id"])
    assert "return (a + b)" in task["diff"]
    assert (env.source / "calc.py").read_text() == BUGGY
    applied = env.product.apply(second["session_id"], second["task_id"], task["tree_after"])
    assert applied["status"] == "applied"
    assert "return (a + b)" in (env.source / "calc.py").read_text()
    replay = env.product.apply(second["session_id"], second["task_id"], task["tree_after"])
    assert replay["duplicate"] and replay["status"] == "applied"
    with pytest.raises(DomainError) as error:
        env.product.apply(first["session_id"], first["task_id"], "a" * 64)
    assert error.value.code == "HARNESS_APPLY_STALE"


def test_apply_rejects_wrong_tree_and_preserves_original_project_on_conflict(product_env):
    env = product_env
    session = prepare_bugfix(env)
    task = env.harness.get_task(session["task_id"])
    with pytest.raises(DomainError) as error:
        env.product.apply(session["session_id"], session["task_id"], "0" * 64)
    assert error.value.code == "HARNESS_APPLY_UNVERIFIED"
    (env.source / "calc.py").write_text("# User's newer version\n" + FIXED)
    receipt = env.product.apply(session["session_id"], session["task_id"], task["tree_after"])
    assert receipt["status"] == "conflict"
    assert "User's newer version" in (env.source / "calc.py").read_text()
    replay = env.product.apply(session["session_id"], session["task_id"], task["tree_after"])
    assert replay["duplicate"] and replay["status"] == "conflict"


def test_continuation_rejects_user_changes_that_conflict_with_previous_patch(product_env):
    env = product_env
    session = prepare_bugfix(env)
    (env.source / "calc.py").write_text("def add(a,b): return 99\n")
    previous_calls = len(env.model_requests)
    with pytest.raises(DomainError) as error:
        env.product.send(session["session_id"], "再次调整加法表达式写法", "conflict")
    assert error.value.code == "HARNESS_CONTINUATION_CONFLICT"
    assert len(env.model_requests) == previous_calls
    assert (env.source / "calc.py").read_text() == "def add(a,b): return 99\n"


def test_apply_cannot_write_through_user_created_symlink(product_env, tmp_path):
    env = product_env
    session = prepare_bugfix(env)
    task = env.harness.get_task(session["task_id"])
    outside = tmp_path / "private-file"
    outside.write_text("KEEP_PRIVATE")
    (env.source / "calc.py").unlink()
    (env.source / "calc.py").symlink_to(outside)
    with pytest.raises(DomainError):
        env.product.apply(session["session_id"], session["task_id"], task["tree_after"])
    assert outside.read_text() == "KEEP_PRIVATE"
    with env.engine.transaction() as conn:
        receipt = conn.execute(
            "SELECT state FROM app.harness_apply_receipts WHERE task_id=%s", (session["task_id"],)
        ).fetchone()
    assert receipt["state"] == "unknown"


def test_revocation_during_model_io_prevents_any_later_tool_effect(product_env):
    env = product_env
    env.save_model()
    env.authorize()

    def revoke_after_request(request):
        env.product.revoke(env.project_id)

    env.hooks["model"] = revoke_after_request
    try:
        env.product.create(env.project_id, "解释项目", "revoked-during-model")
    except DomainError as error:
        assert error.code in {
            "HARNESS_AUTHORIZATION_REVOKED",
            "HARNESS_PROJECT_AUTHORIZATION_REQUIRED",
        }
    assert len(env.model_requests) == 1
    with env.engine.transaction() as conn:
        tool_effects = conn.execute(
            "SELECT count(*) AS n FROM app.harness_operations WHERE kind='tool' "
            "AND state IN ('dispatched','known')"
        ).fetchone()["n"]
        usage = conn.execute(
            "SELECT count(*) AS n,sum(reserved_micros) AS reserved,"
            "sum(coalesce(settled_micros,0)) AS settled FROM app.usage_calls"
        ).fetchone()
    assert tool_effects == 0
    assert usage["n"] == 1 and usage["reserved"] + usage["settled"] >= 3


def test_running_message_is_idempotent_and_targets_same_active_run(product_env, monkeypatch):
    env = product_env
    env.save_model()
    env.authorize()
    entered, release = threading.Event(), threading.Event()

    def wait_at_model(request):
        entered.set()
        assert release.wait(5)

    env.hooks["model"] = wait_at_model
    monkeypatch.setattr(
        env.harness, "submit_task", HarnessCodingService.submit_task.__get__(env.harness)
    )
    first = env.product.create(env.project_id, "解释项目", "running-first")
    try:
        assert entered.wait(2)
        received = env.product.send(first["session_id"], "顺便解释入口文件", "running-second")
        replay = env.product.send(first["session_id"], "顺便解释入口文件", "running-second")
        assert received["task_id"] == first["task_id"] == replay["task_id"]
        assert replay["duplicate"]
        inbox = env.harness.task_messages(first["task_id"])
        assert len([item for item in inbox["messages"] if item["text"] == "顺便解释入口文件"]) == 1
    finally:
        release.set()
        env.harness.join_task(first["task_id"], timeout=10)


def test_tampered_finished_workspace_is_rejected_before_inheriting_it(product_env):
    env = product_env
    first = completed_inspection(env)
    workspace = env.harness.registry.open_task_workspace(
        env.harness.registry.load(env.project_id), first["task_id"]
    )
    (workspace.root / "README.md").write_text("Unverified post-completion edit\n")
    with pytest.raises(DomainError) as error:
        env.product.send(first["session_id"], "解释项目内容", "tampered-workspace")
    assert error.value.code == "HARNESS_CONTINUATION_CONFLICT"
    assert len(env.model_requests) == 2


def test_continuation_carries_created_and_deleted_files_without_changing_original(product_env):
    env = product_env
    (env.source / "obsolete.txt").write_text("Old migration artifact\n")
    (env.source / "test_migration.py").write_text(
        "import pathlib,unittest\n"
        "class MigrationTest(unittest.TestCase):\n"
        " def test_cleanup(self):\n"
        "  if pathlib.Path('migration.flag').exists():\n"
        "   pathlib.Path('obsolete.txt').unlink(missing_ok=True)\n"
        "   self.assertTrue(pathlib.Path('created.py').exists())\n"
        "  self.assertTrue(True)\n"
    )
    env.project_id = env.harness.register_project(str(env.source))["project_id"]
    env.save_model()
    env.authorize()

    class CreateModel:
        def complete(self, request, cancellation):
            cancellation.ensure_active()
            tool_messages = [message for message in request.messages if message.role == "tool"]
            if not tool_messages:
                return ModelResponse(
                    tool_calls=(
                        HarnessToolCall(
                            call_id=request.operation_id + ":create",
                            name="apply_edits",
                            arguments={
                                "expected_tree": request.workspace_tree,
                                "edits": [
                                    {
                                        "path": "migration.flag",
                                        "expected_sha256": None,
                                        "create": True,
                                        "content": "ready\n",
                                    },
                                    {
                                        "path": "created.py",
                                        "expected_sha256": None,
                                        "create": True,
                                        "content": "value = 1\n",
                                    },
                                ],
                            },
                        ),
                    ),
                    finish_reason="tool_calls",
                    usage=ModelUsage(cost_micros=3),
                )
            # The migration test removes an artifact on its first execution.
            # Verify again on the resulting stable tree; the first receipt is
            # truthfully bound to the pre-command tree and cannot prove it.
            if sum(message.name == "run_tests" for message in tool_messages) < 2:
                return ModelResponse(
                    tool_calls=(
                        HarnessToolCall(
                            call_id=request.operation_id + ":tests",
                            name="run_tests",
                            arguments={"command_id": "tests"},
                        ),
                    ),
                    finish_reason="tool_calls",
                    usage=ModelUsage(cost_micros=3),
                )
            return ModelResponse(
                text="Created the new module and verified migration cleanup.",
                usage=ModelUsage(cost_micros=3),
            )

    previous_factory = env.harness.model_factory

    def fixture_factory(request):
        if request.task_kind == "feature":
            # This fixture's project test intentionally deletes a migration
            # artifact. Make only the already-granted isolated task directory
            # writable by the fixed container UID; never chmod the source.
            with env.engine.transaction() as conn:
                row = conn.execute(
                    "SELECT request FROM app.harness_tasks WHERE "
                    "(request->>'product_turn_id')::bigint=%s",
                    (request.product_turn_id,),
                ).fetchone()
            Path(row["request"]["workspace_root"]).chmod(0o777)
            return CreateModel()
        return previous_factory(request)

    env.harness.model_factory = fixture_factory
    first = env.product.create(env.project_id, "创建迁移标志和新模块", "create-migration")
    assert first["status"] == "completed", env.harness.get_task(first["task_id"])
    second = env.product.send(first["session_id"], "解释项目内容", "inspect-created")
    assert second["status"] == "completed"
    current = env.harness.registry.open_task_workspace(
        env.harness.registry.load(env.project_id), second["task_id"]
    )
    assert (current.root / "created.py").read_text() == "value = 1\n"
    assert (current.root / "migration.flag").exists()
    assert not (current.root / "obsolete.txt").exists()
    assert not (env.source / "created.py").exists()
    assert (env.source / "obsolete.txt").exists()


def test_explicit_write_demand_is_rejected_by_readonly_project_grant(product_env):
    env = product_env
    env.save_model()
    env.authorize(allow_write=False)
    with pytest.raises(DomainError) as error:
        env.product.create(env.project_id, "修复登录错误", "readonly-write-request")
    assert error.value.code == "HARNESS_WRITE_DENIED"
    assert not env.model_requests


def test_followup_preserves_prior_constraints_as_context_without_rewriting_new_goal(product_env):
    env = product_env
    first = completed_inspection(env, text="解释项目，后续保持兼容性", client_id="constraints")
    second = env.product.send(first["session_id"], "解释入口文件", "followup-context")
    assert second["status"] == "completed"
    assert env.requests[-1].objective == "解释入口文件"
    assert any("后续保持兼容性" in message.content for message in env.model_requests[-1].messages)


@pytest.mark.parametrize(
    "model_id,cost_per_call",
    [
        ("deepseek-flash", 46),
        ("deepseek-v4-pro", 191),
    ],
)
def test_native_product_adapter_and_pricing_are_wired_through_real_pg(
    product_env,
    monkeypatch,
    model_id,
    cost_per_call,
):
    env = product_env
    env.save_model(model_id=model_id)
    env.authorize()
    env.harness.model_factory = None
    sent = []

    def handle(wire):
        body = json.loads(wire.content)
        sent.append(body)
        assert body["thinking"] == {"type": "enabled"}
        assert body["reasoning_effort"] == "max"
        assert body["model"] == model_id
        assert wire.headers["Authorization"] == "Bearer " + KEY_A
        if len(sent) == 1:
            delta = {
                "tool_calls": [
                    {
                        "index": 0,
                        "id": "native_read_1",
                        "type": "function",
                        "function": {"name": "read_file", "arguments": '{"path":"README.md"}'},
                    }
                ]
            }
            finish = "tool_calls"
        else:
            assistant_index = next(
                index for index, message in enumerate(body["messages"]) if message.get("tool_calls")
            )
            assistant = body["messages"][assistant_index]
            result = body["messages"][assistant_index + 1]
            assert assistant["tool_calls"][0]["function"]["name"] == "read_file"
            assert result["role"] == "tool" and result["tool_call_id"] == "native_read_1"
            assert "controlled product integration" in result["content"]
            delta, finish = (
                {"content": "README.md:1 describes the controlled integration project."},
                "stop",
            )
        chunks = [
            {"choices": [{"delta": delta}]},
            {"choices": [{"delta": {}, "finish_reason": finish}]},
            {"choices": [], "usage": {"prompt_tokens": 120, "completion_tokens": 8}},
        ]
        content = "".join("data: " + json.dumps(chunk) + "\n\n" for chunk in chunks)
        return httpx.Response(200, content=(content + "data: [DONE]\n\n").encode())

    original = ProductDeepSeekChatAdapter.__init__

    def configured(self, **kwargs):
        return original(
            self,
            **{
                **kwargs,
                "client": httpx.Client(
                    transport=httpx.MockTransport(handle),
                ),
            },
        )

    monkeypatch.setattr(ProductDeepSeekChatAdapter, "__init__", configured)
    session = env.product.create(env.project_id, "解释项目", "native-provider")
    assert session["status"] == "completed", env.harness.get_task(session["task_id"])
    assert len(sent) == 2
    authorization = env.product.authorization(env.project_id)
    assert authorization["used_calls"] == 2
    assert authorization["reserved_micros"] == 0
    assert authorization["settled_micros"] == 2 * cost_per_call
    result = env.harness.get_task(session["task_id"])
    assert result["protocol"] == "live:" + model_id and result["model_id"] == model_id
    assert "README.md:1" in result["summary"]
    assert KEY_A not in json.dumps(result, default=str)


def test_incomplete_known_turn_continues_same_thread_and_fences_old_worker(
    product_env, monkeypatch
):
    from mgar_v3.harness.broker import CapabilityAuthority
    from mgar_v3.harness.contracts import CancellationToken, CapabilityGrant, ExecutionRequest
    from mgar_v3.harness.ledger import PersistentBudgetLedger
    from mgar_v3.harness.service import WorkspaceBoundBroker

    env = product_env
    env.save_model()
    env.authorize()
    normal_factory = env.harness.model_factory

    class UncitedModel:
        def complete(self, request, cancellation):
            cancellation.ensure_active()
            return ModelResponse(
                text="An uncited answer without collected source evidence.",
                usage=ModelUsage(cost_micros=3),
            )

    env.harness.model_factory = lambda request: UncitedModel()
    stopped = env.product.create(env.project_id, "解释项目", "known-incomplete")
    assert stopped["status"] in {"blocked", "incomplete"}
    old = env.engine.harness.get(stopped["task_id"])
    with env.engine.transaction() as conn:
        assert (
            conn.execute(
                "SELECT count(*) AS n FROM app.usage_calls WHERE status='unknown'"
            ).fetchone()["n"]
            == 0
        )
    env.harness.model_factory = normal_factory
    continued = env.product.send(stopped["session_id"], "解释 README.md 的内容", "after-incomplete")
    assert continued["status"] == "completed", env.harness.get_task(continued["task_id"])
    current = env.engine.harness.get(continued["task_id"])
    assert current["thread_id"] == old["thread_id"] and current["run_id"] != old["run_id"]
    assert continued["turns"][0]["status"] == stopped["status"]

    ledger = PersistentBudgetLedger(env.engine.harness, stopped["task_id"], old["epoch"])
    with pytest.raises(DomainError):
        ledger.reserve(stopped["task_id"], "stale-model", "model", reserved_micros=100)
    with pytest.raises(DomainError):
        env.engine.harness.model_result(stopped["task_id"], old["epoch"], "stale-model")
    runtime = env.harness._runtimes[stopped["task_id"]]

    def old_authority():
        env.engine.harness.guard(stopped["task_id"], old["epoch"])
        return runtime.lease

    broker = WorkspaceBoundBroker(
        runtime.workspace,
        CapabilityAuthority(lease_source=old_authority, budget_check=lambda: True),
        lock_path=IMAGE_LOCK,
    )
    docker_calls = []

    def no_docker(*args, **kwargs):
        docker_calls.append(args)
        raise AssertionError("Superseded worker must be fenced before starting any process")

    monkeypatch.setattr(broker, "_docker", no_docker)
    with pytest.raises(DomainError):
        broker.execute(
            ExecutionRequest(
                operation_id="stale-command",
                purpose="command",
                argv=("python3", "-B", "-c", "print('stale')"),
                execution_epoch=old["epoch"],
            ),
            CapabilityGrant.model_validate(old["grant_scope"]),
            CancellationToken(),
        )
    assert not docker_calls
    with env.engine.transaction() as conn:
        assert (
            conn.execute(
                "SELECT count(*) AS n FROM app.harness_operations "
                "WHERE operation_id IN ('stale-model','stale-command')"
            ).fetchone()["n"]
            == 0
        )
