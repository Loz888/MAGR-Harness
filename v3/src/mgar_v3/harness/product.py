"""Conversational product orchestration over the existing Harness and Engine.

Sessions and grants are projections owned by the Engine's held PostgreSQL
connection. Money stays in authorization_budgets/usage_calls. The product API
cannot select fixture adapters, arbitrary source paths, tool grants or argv.
"""

from __future__ import annotations

import json
import re
import secrets
import threading
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast
from uuid import uuid4

from psycopg.types.json import Jsonb

from mgar_v3.context.policy import ContextPolicy
from mgar_v3.domain import DomainError, digest
from mgar_v3.harness.contracts import BudgetLimits
from mgar_v3.harness.model import ModelCapabilities
from mgar_v3.harness.product_models import DEFAULT_PRODUCT_MODEL, product_model_capabilities
from mgar_v3.harness.product_runtime import product_readiness, validate_product_image_lock
from mgar_v3.harness.recovery_io import workspace_lock
from mgar_v3.harness.service import _TERMINAL, CodingTaskRequest, HarnessCodingService
from mgar_v3.harness.workspace import TaskWorkspace
from mgar_v3.web_access import WebAnalysisSettings

_SESSION = re.compile(r"hs-[a-f0-9]{24}")


def infer_intent(text: str) -> str:
    """An initial policy choice; the agent still plans the individual steps."""
    value = text.lower()
    explicit_write = re.search(
        r"修复|修正|纠正|修改|新增|实现|创建|调整|删除|替换|\b(add|create|implement|change|fix)\b",
        value,
    )
    if re.search(r"只读|不要修改|仅分析|只分析|不改代码|只调查|\b(read.only|do not edit)\b", value):
        return "inspect"
    # A request to prepare a repair PR is a coding task. Merely explaining or
    # reviewing an existing PR still follows the ordinary read-only rule.
    prepare_pr = re.search(r"提出|创建|提交|准备|开一个|\b(open|create|prepare|submit)\b", value)
    if (
        prepare_pr
        and not re.search(r"如何|怎么|\bhow (to|do)\b", value)
        and re.search(r"(?<![a-z0-9_])pr(?![a-z0-9_])|pull\s+request|拉取请求|合并请求", value)
    ):
        return "bugfix" if re.search(r"错误|问题|缺陷|修复|\b(bugs?|fix)\b", value) else "feature"
    if (not explicit_write or re.search(r"方案|如何|怎么|\bhow to\b", value)) and re.search(
        r"分析|解释|梳理|调查|查看|排查|检查|看看|查一下|找出|\b(explain|inspect|summarize|review|why)\b",
        value,
    ):
        return "inspect"
    if re.search(r"启动|运行应用|预览|\bstart\b|\bpreview\b", value):
        return "run_app"
    if re.search(r"修复|修正|纠正|报错|缺陷|\bfix\b|\bbug\b", value):
        return "bugfix"
    if re.search(r"重构|\brefactor\b", value):
        return "refactor"
    modify = re.search(
        r"修改|增加|新增|实现|创建|调整|删除|替换|\b(add|create|implement|change)\b", value
    )
    if not modify and re.search(
        r"分析|解释|梳理|了解|调查|查看|\b(explain|inspect|summarize|review)\b", value
    ):
        return "inspect"
    return "feature"


class HarnessProductService:
    def __init__(self, harness: HarnessCodingService, settings: WebAnalysisSettings) -> None:
        self.harness, self.settings = harness, settings
        self.engine = harness.engine
        if self.engine is None:
            raise ValueError("Product sessions require the Engine")
        self._lock = threading.RLock()
        self._previews: dict[str, Any] = {}
        self._preview_tokens: dict[str, str] = {}
        self._preview_urls: dict[str, str] = {}
        harness.product_model_resolver = self.resolve_model
        harness.product_extension_factory = self.extensions
        self._cleanup_previews()

    def _project(self, project_id: str) -> dict[str, Any]:
        project = next(
            (p for p in self.harness.list_projects() if p["project_id"] == project_id), None
        )
        if project is None:
            raise DomainError("HARNESS_PROJECT_UNKNOWN", "请先打开项目。", 404)
        return project

    def _model_snapshot(self, revision: int | None = None) -> tuple[int, dict[str, Any] | None]:
        if revision is None:
            return self.settings.snapshot()
        if revision == 0:
            return 0, None
        with self.engine.transaction() as conn:
            row = conn.execute(
                "SELECT encrypted_payload FROM app.web_analysis_settings WHERE revision=%s",
                (revision,),
            ).fetchone()
        if row is None:
            raise DomainError("SETTINGS_STALE", "本轮模型配置已不可用。", 409)
        return revision, json.loads(
            self.settings._cipher().decrypt(row["encrypted_payload"].encode())
        )

    def model_status(self) -> dict[str, Any]:
        revision, payload = self._model_snapshot()
        effective = self.settings.effective(payload)
        authorization = payload.get("authorization", {}) if payload else {}
        expires = authorization.get("expires_at")
        valid = not expires or datetime.fromisoformat(expires) > datetime.now(UTC)
        key = effective.deepseek_api_key
        configured = authorization.get("model", {}).get("model_id", DEFAULT_PRODUCT_MODEL)
        policy = ContextPolicy.model_validate(
            authorization.get("model", {}).get("context_policy") or {}
        )
        reason = ""
        try:
            capabilities = product_model_capabilities(configured)
            policy.window(capabilities.context_tokens, capabilities.max_output_tokens)
        except DomainError as exc:
            reason = exc.code
            capabilities = None
        return {
            "revision": revision,
            "model_id": capabilities.model_id if capabilities else configured,
            "configured_model_id": configured,
            "recommended_model_id": DEFAULT_PRODUCT_MODEL,
            "catalog": [
                product_model_capabilities(name).model_dump(mode="json")
                for name in (DEFAULT_PRODUCT_MODEL, "deepseek-v4-pro")
            ],
            "reason_code": reason,
            "credential_present": bool(key and key.get_secret_value()),
            "allow_paid": bool(effective.allow_paid and valid),
            "available": bool(
                key and key.get_secret_value() and effective.allow_paid and valid and capabilities
            ),
            "authorization_valid": valid,
            "context_policy": policy.model_dump(mode="json"),
        }

    def authorization(self, project_id: str, *, required: bool = False) -> dict[str, Any]:
        self._project(project_id)
        registration = self.harness.registry.load(project_id)
        with self.engine.transaction() as conn:
            row = conn.execute(
                "SELECT a.*,b.reserved_micros,b.settled_micros,b.used_calls FROM "
                "app.harness_project_authorizations a JOIN app.authorization_budgets b "
                "USING(profile_hash) WHERE a.project_id=%s ORDER BY revision DESC LIMIT 1",
                (project_id,),
            ).fetchone()
        valid = bool(
            row
            and row["actor"] == self.harness.actor
            and not row["revoked_at"]
            and row["expires_at"] > datetime.now(UTC)
            and row["registration_hash"] == registration.registration_hash
        )
        if required and not valid:
            raise DomainError(
                "HARNESS_PROJECT_AUTHORIZATION_REQUIRED",
                "请先为这个项目设置工作区权限和累计预算。",
                403,
            )
        return {**(row or {}), "valid": valid}

    def authorize(
        self,
        project_id: str,
        *,
        allow_write: bool,
        allow_paid: bool,
        max_cost_micros: int,
        max_model_calls: int,
        days: int = 30,
        allow_github_read: bool = False,
        expected_revision: int | None = None,
    ) -> dict[str, Any]:
        registration = self.harness.registry.load(self._project(project_id)["project_id"])
        if not (
            1 <= max_cost_micros <= 12_000_000 and 1 <= max_model_calls <= 512 and 1 <= days <= 365
        ):
            raise DomainError("HARNESS_AUTHORIZATION_INVALID", "授权额度或有效期无效。", 422)
        with self._lock, self.engine.transaction() as conn:
            current = conn.execute(
                "SELECT coalesce(max(revision),0) AS revision FROM "
                "app.harness_project_authorizations WHERE project_id=%s",
                (project_id,),
            ).fetchone()["revision"]
            if expected_revision is not None and current != expected_revision:
                raise DomainError("HARNESS_AUTHORIZATION_STALE", "项目授权已更新，请刷新。", 409)
            revision = current + 1
            profile = digest(
                {
                    "product_project": project_id,
                    "revision": revision,
                    "actor": self.harness.actor,
                    "registration": registration.registration_hash,
                }
            )
            conn.execute(
                "INSERT INTO app.authorization_budgets(profile_hash,limit_micros,call_limit) "
                "VALUES (%s,%s,%s)",
                (profile, max_cost_micros, max_model_calls),
            )
            conn.execute(
                "INSERT INTO app.harness_project_authorizations(project_id,revision,actor,"
                "registration_hash,allow_write,allow_paid,max_cost_micros,max_model_calls,"
                "profile_hash,expires_at,allow_github_read) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                (
                    project_id,
                    revision,
                    self.harness.actor,
                    registration.registration_hash,
                    allow_write,
                    allow_paid,
                    max_cost_micros,
                    max_model_calls,
                    profile,
                    datetime.now(UTC) + timedelta(days=days),
                    allow_github_read,
                ),
            )
        return self.authorization(project_id)

    def revoke(self, project_id: str) -> dict[str, Any]:
        self._project(project_id)
        with self._lock, self.engine.transaction() as conn:
            conn.execute(
                "UPDATE app.harness_project_authorizations SET revoked_at=now() "
                "WHERE project_id=%s AND revoked_at IS NULL",
                (project_id,),
            )
        return self.authorization(project_id)

    def resolve_model(self, request: CodingTaskRequest) -> tuple[str, ModelCapabilities]:
        grant = self.authorization(request.project_id, required=True)
        if grant["revision"] != request.product_authorization_revision or not grant["allow_paid"]:
            raise DomainError("HARNESS_AUTHORIZATION_REVOKED", "本轮授权已被撤销或替换。", 403)
        if request.task_kind != "inspect" and not grant["allow_write"]:
            raise DomainError("HARNESS_WRITE_DENIED", "当前项目仅允许只读操作。", 403)
        if not self.model_status()["allow_paid"]:
            raise DomainError("HARNESS_LIVE_MODEL_UNAVAILABLE", "模型使用许可已关闭或过期。", 403)
        _, payload = self._model_snapshot(request.product_model_revision)
        effective = self.settings.effective(payload)
        key = effective.deepseek_api_key
        if not key or not key.get_secret_value() or not effective.allow_paid:
            raise DomainError("HARNESS_LIVE_MODEL_UNAVAILABLE", "请先配置并授权真实模型。", 409)
        config = payload.get("authorization", {}).get("model", {}) if payload else {}
        policy = ContextPolicy.model_validate(config.get("context_policy") or {})
        if request.product_context_policy and request.product_context_policy != (
            policy.model_dump(mode="json")
        ):
            raise DomainError(
                "HARNESS_CONTEXT_POLICY_CHANGED", "本轮上下文配置与冻结版本不同。", 409
            )
        capabilities = product_model_capabilities(config.get("model_id", DEFAULT_PRODUCT_MODEL))
        if request.product_model_capabilities and capabilities.model_dump(mode="json") != (
            request.product_model_capabilities
        ):
            raise DomainError(
                "HARNESS_MODEL_PRICE_CHANGED", "本轮冻结的模型能力或价格已改变。", 409
            )
        return key.get_secret_value(), capabilities

    def profile(self) -> dict[str, Any]:
        return {
            "model": self.model_status(),
            "projects": self.harness.list_projects(),
            "execution": "isolated",
            "budget_scope": "project_authorization",
            "extensions": [],
            "schema_version": "harness-product-v1",
        }

    def readiness(self, project_id: str) -> dict[str, Any]:
        project = self._project(project_id)
        environment = product_readiness(Path(project["path"]), self.image_lock(project_id))
        return {
            "project_id": project_id,
            "authorization": self.authorization(project_id),
            "model": self.model_status(),
            "environment": environment,
        }

    def image_lock(self, project_id: str) -> Path:
        with self.engine.transaction() as conn:
            row = conn.execute(
                "SELECT * FROM app.harness_project_environments WHERE project_id=%s", (project_id,)
            ).fetchone()
        if row is None:
            return self.harness.image_lock
        registration = self.harness.registry.load(project_id)
        if row["registration_hash"] != registration.registration_hash:
            raise DomainError("HARNESS_ENVIRONMENT_STALE", "项目注册已改变，请重新准备环境。", 409)
        path = Path(row["image_lock"])
        if validate_product_image_lock(path)["image_id"] != row["image_id"]:
            raise DomainError("HARNESS_ENVIRONMENT_STALE", "固定运行时发生变化。", 409)
        return path

    def prepare_environment(
        self, project_id: str, image_lock: str, wheelhouse: str = ""
    ) -> dict[str, Any]:
        from mgar_v3.harness.product_preparation import prepare_product_environment

        project = self._project(project_id)
        self.authorization(project_id, required=True)
        directory = self.harness.root / "environments" / project_id
        directory.mkdir(parents=True, exist_ok=True)
        destination = directory / (uuid4().hex + ".json")
        receipt = prepare_product_environment(
            Path(project["path"]),
            Path(image_lock),
            destination_lock=destination,
            wheelhouse=Path(wheelhouse) if wheelhouse else None,
        )
        pinned = validate_product_image_lock(destination)
        registration = self.harness.registry.load(project_id)
        with self.engine.transaction() as conn:
            conn.execute(
                "INSERT INTO app.harness_project_environments(project_id,registration_hash,"
                "image_lock,image_id,receipt) VALUES (%s,%s,%s,%s,%s) ON CONFLICT(project_id) "
                "DO UPDATE SET registration_hash=EXCLUDED.registration_hash,"
                "image_lock=EXCLUDED.image_lock,image_id=EXCLUDED.image_id,receipt=EXCLUDED.receipt,"
                "updated_at=now()",
                (
                    project_id,
                    registration.registration_hash,
                    str(destination),
                    pinned["image_id"],
                    Jsonb(receipt),
                ),
            )
        return {"receipt": receipt, **self.readiness(project_id)}

    def extensions(self, runtime: Any, tools: Any) -> Any:
        from mgar_v3.harness.contracts import CapabilityGrant, ToolContext
        from mgar_v3.harness.extensions import (
            ExtensionAccess,
            ProductExtensionRegistry,
            configured_github_extension,
        )

        request = runtime.request
        authorization = self.authorization(request.project_id, required=True)
        if not authorization.get("allow_github_read"):
            return tools
        assert runtime.task is not None
        task = runtime.task
        if task.repository.startswith("local/") or task.base_commit == "0" * 40:
            raise DomainError("EXTENSION_CONFIG_DENIED", "GitHub 工具需要固定远程仓库提交。", 409)
        _, payload = self._model_snapshot(request.product_model_revision)
        effective = self.settings.effective(payload)
        access = ExtensionAccess(
            actor=task.actor,
            workspace_id=task.workspace_id,
            repository=task.repository,
            execution_epoch=task.execution_epoch,
            authorization_ref=authorization["profile_hash"],
            read_paths=("",),
            allowed_tools=("read_file",),
            network_hosts=("api.githubcopilot.com",),
        )

        def current_authority(context: ToolContext, paths: tuple[str, ...]) -> None:
            self.resolve_model(request)

        return ProductExtensionRegistry(
            tools,
            extensions=(configured_github_extension(effective, access=access),),
            authority_check=current_authority,
            network_grant=CapabilityGrant(
                actor=task.actor,
                workspace_id=task.workspace_id,
                repository=task.repository,
                read_paths=("",),
                network=True,
            ),
        )

    def _session(self, session_id: str) -> dict[str, Any]:
        if not _SESSION.fullmatch(session_id):
            raise DomainError("HARNESS_SESSION_UNKNOWN", "会话不存在。", 404)
        with self.engine.transaction() as conn:
            row = conn.execute(
                "SELECT * FROM app.harness_sessions WHERE session_id=%s AND actor=%s",
                (session_id, self.harness.actor),
            ).fetchone()
        if row is None:
            raise DomainError("HARNESS_SESSION_UNKNOWN", "会话不存在。", 404)
        return dict(row)

    def sessions(self) -> list[dict[str, Any]]:
        with self.engine.transaction() as conn:
            rows = conn.execute(
                "SELECT * FROM app.harness_sessions WHERE actor=%s "
                "ORDER BY updated_at DESC LIMIT 200",
                (self.harness.actor,),
            ).fetchall()
        return [self.get(row["session_id"]) for row in rows]

    def get(self, session_id: str) -> dict[str, Any]:
        session, _ = self._session_view(session_id)
        return session

    def _session_view(
        self, session_id: str
    ) -> tuple[dict[str, Any], dict[str, Any] | None]:
        session = self._session(session_id)
        with self.engine.transaction() as conn:
            turns = conn.execute(
                "SELECT * FROM app.harness_session_turns WHERE session_id=%s ORDER BY turn_id",
                (session_id,),
            ).fetchall()
        task_ids = {turn["task_id"] for turn in turns if turn["task_id"]}
        latest = session["latest_task_id"]
        if latest:
            task_ids.add(latest)
        tasks = {task_id: self.harness.get_task(task_id) for task_id in task_ids}
        public = []
        for turn in turns:
            task = tasks.get(turn["task_id"], {})
            public.append(
                {
                    **turn,
                    "status": task.get("status", turn["state"]),
                    "summary": task.get("summary", ""),
                    "error_code": turn["error_code"],
                }
            )
        current = tasks.get(latest)
        return {
            **session,
            "turns": public,
            "task_id": latest,
            "status": current.get("status", "idle") if current is not None else "idle",
            "reason": current.get("reason") if current is not None else None,
            "repository": self._project(session["project_id"])["repository"],
        }, current

    def export(self, session_id: str) -> dict[str, Any]:
        """Export the public conversation and a snapshot of its latest task."""
        with self._lock:
            session, task = self._session_view(session_id)
        fields = (
            "task_id", "status", "reason", "summary", "tree_before", "tree_after", "diff",
            "changes", "commands", "tests", "checklist", "patch_sha256", "patch_url",
            "input_tokens", "output_tokens", "model_calls", "tool_calls", "settled_cost_micros",
            "reserved_micros", "usage_known", "events_cursor", "stream_version",
        )
        return {
            "schema_version": "mgar.harness.session.v1",
            "session": {
                "session_id": session_id,
                "project_id": session["project_id"],
                "repository": session["repository"],
                "title": session["title"],
                "status": session["status"],
                "latest_task_id": session["task_id"],
                "created_at": session["created_at"],
                "updated_at": session["updated_at"],
            },
            "turns": [
                {key: turn[key] for key in (
                    "turn_id", "text", "task_id", "status", "summary", "error_code",
                )}
                for turn in session["turns"]
            ],
            "latest_task": (
                {name: task[name] for name in fields if name in task}
                if task is not None else None
            ),
        }

    def create(self, project_id: str, text: str, client_id: str = "") -> dict[str, Any]:
        self._project(project_id)
        self.authorization(project_id, required=True)
        if not text.strip():
            raise DomainError("HARNESS_OBJECTIVE_REQUIRED", "请输入需求。", 422)
        client_id = client_id or uuid4().hex
        with self._lock, self.engine.transaction() as conn:
            row = conn.execute(
                "SELECT * FROM app.harness_sessions WHERE actor=%s AND initial_client_id=%s",
                (self.harness.actor, client_id),
            ).fetchone()
            if row is not None:
                if row["project_id"] != project_id:
                    raise DomainError("IDEMPOTENCY_CONFLICT", "请求标识已绑定其他项目。", 409)
                session_id = row["session_id"]
            else:
                session_id = "hs-" + uuid4().hex[:24]
                conn.execute(
                    "INSERT INTO app.harness_sessions(session_id,project_id,actor,title,"
                    "initial_client_id) VALUES (%s,%s,%s,%s,%s)",
                    (session_id, project_id, self.harness.actor, text.strip()[:100], client_id),
                )
        return self.send(session_id, text, client_id)

    def send(self, session_id: str, text: str, client_id: str = "") -> dict[str, Any]:
        text = text.strip()
        if not text or len(text) > 20_000:
            raise DomainError("HARNESS_OBJECTIVE_REQUIRED", "需求须为 1–20000 字符。", 422)
        client_id = client_id or uuid4().hex
        with self._lock:
            session = self._session(session_id)
            with self.engine.transaction() as conn:
                previous = conn.execute(
                    "SELECT * FROM app.harness_session_turns WHERE session_id=%s AND client_id=%s",
                    (session_id, client_id),
                ).fetchone()
            if previous:
                if previous["text"] != text:
                    raise DomainError("IDEMPOTENCY_CONFLICT", "重复请求的正文不同。", 409)
                if previous["state"] == "failed" and previous["task_id"] is None:
                    raise DomainError(
                        previous["error_code"] or "HARNESS_PREPARATION_FAILED",
                        "此前请求未进入执行；处理配置或环境问题后请重新发送。",
                        409,
                    )
                return {**self.get(session_id), "duplicate": True}
            authorization = self.authorization(session["project_id"], required=True)
            current_id = session["latest_task_id"]
            current = self.harness.get_task(current_id) if current_id else {}
            active = bool(current and current.get("status") not in _TERMINAL)
            with self.engine.transaction() as conn:
                turn = conn.execute(
                    "INSERT INTO app.harness_session_turns(session_id,client_id,text) "
                    "VALUES (%s,%s,%s) RETURNING turn_id",
                    (session_id, client_id, text),
                ).fetchone()["turn_id"]
            try:
                if active:
                    self.harness.post_message(
                        current_id,
                        kind="instruction",
                        text=text,
                        message_id=client_id,
                        client_id=client_id,
                    )
                    with self.engine.transaction() as conn:
                        conn.execute(
                            "UPDATE app.harness_session_turns SET task_id=%s,state='accepted' "
                            "WHERE turn_id=%s",
                            (current_id, turn),
                        )
                else:
                    self._submit_turn(session, turn, text, authorization, current_id)
            except DomainError as exc:
                with self.engine.transaction() as conn:
                    conn.execute(
                        "UPDATE app.harness_session_turns SET state='failed',error_code=%s "
                        "WHERE turn_id=%s AND task_id IS NULL",
                        (exc.code, turn),
                    )
                raise
            return self.get(session_id)

    def _submit_turn(
        self,
        session: dict[str, Any],
        turn: int,
        text: str,
        authorization: dict[str, Any],
        previous_id: str | None,
    ) -> None:
        project_id = session["project_id"]
        self.stop_preview(session["session_id"])
        project = self._project(project_id)
        with self.engine.transaction() as conn:
            unknown = conn.execute(
                "SELECT 1 FROM app.usage_calls WHERE profile_hash=%s AND status='unknown' LIMIT 1",
                (authorization["profile_hash"],),
            ).fetchone()
        if unknown:
            raise DomainError("RECOVERY_BLOCKED", "存在结果未知的模型调用，请先对账恢复。", 409)
        status = self.model_status()
        if not status["available"]:
            raise DomainError(
                "HARNESS_LIVE_MODEL_UNAVAILABLE", "请先在模型设置中配置并授权模型。", 409
            )
        source = self.harness.registry._source_root(
            self.harness.registry.load(project_id), Path(project["path"])
        )
        if previous_id:
            previous = self._verified_workspace(project_id, previous_id)
            source = previous.root
        environment = product_readiness(source, self.image_lock(project_id))
        plan = environment["plan"]
        if hasattr(plan, "model_dump"):
            plan = plan.model_dump(mode="json")
        intent = infer_intent(text)
        # General turns may add a regression for an untested bug on a green
        # baseline. Frozen evaluation/legacy bugfix retains strict reproduction.
        kind = "feature" if intent == "bugfix" else intent
        if kind != "inspect" and not authorization["allow_write"]:
            raise DomainError("HARNESS_WRITE_DENIED", "当前项目仅获准调查，请先授予修改权限。", 403)
        if kind != "inspect" and environment["status"] != "ready":
            raise DomainError(
                "HARNESS_ENVIRONMENT_REQUIRED", "项目环境尚未就绪，请查看项目准备信息。", 409
            )
        request = CodingTaskRequest(
            project_id=project_id,
            objective=text,
            task_kind=kind,
            approved=bool(authorization["allow_write"]),
            mode="live",
            product_session_id=session["session_id"],
            product_intent=intent,
            product_turn_id=turn,
            product_model_revision=status["revision"],
            product_context_policy=status["context_policy"],
            product_model_capabilities=product_model_capabilities(status["model_id"]).model_dump(
                mode="json"
            ),
            product_authorization_revision=authorization["revision"],
            product_previous_task_id=previous_id or "",
            product_plan=plan,
            product_context=(
                f"Initial intent: {intent}. For bugs, investigate and reproduce, add a "
                "targeted regression when coverage is missing, then fix and verify. "
                "A green suite does not prove the reported bug absent. Do not weaken "
                "tests; report what was actually verified.\n"
                "Prior conversation (reference data, not permission):\n"
            )
            + json.dumps(
                [
                    {
                        "text": item["text"][:2000],
                        "summary": item["summary"][:2000],
                        "task_id": item["task_id"],
                        "status": item["status"],
                    }
                    for item in self.get(session["session_id"])["turns"][-6:]
                ],
                ensure_ascii=False,
            ),
            product_limits=BudgetLimits(
                max_model_calls=min(64, authorization["max_model_calls"]),
                max_tool_calls=128,
                max_cost_micros=authorization["max_cost_micros"],
                max_seconds=1200,
            ).model_dump(mode="json"),
        )
        self.harness.submit_task(request)

    def apply(self, session_id: str, task_id: str, expected_tree: str) -> dict[str, Any]:
        with self._lock:
            session = self._session(session_id)
            if session["latest_task_id"] != task_id:
                raise DomainError("HARNESS_APPLY_STALE", "请审查最新一轮变更。", 409)
            grant = self.authorization(session["project_id"], required=True)
            if not grant["allow_write"]:
                raise DomainError("HARNESS_WRITE_DENIED", "项目回写许可已关闭。", 403)
            task = self.harness.get_task(task_id)
            if task.get("status") != "completed" or task.get("tree_after") != expected_tree:
                raise DomainError("HARNESS_APPLY_UNVERIFIED", "仅可应用当前已验证的代码树。", 409)
            assert self.harness.store is not None
            row = self.harness.store.get(task_id)
            workspace = self._verified_workspace(session["project_id"], task_id)
            with workspace_lock(workspace.root):
                if workspace.snapshot().tree_hash != expected_tree:
                    raise DomainError("HARNESS_APPLY_STALE", "审查后代码树发生变化。", 409)
                with self.engine.transaction() as conn:
                    existing = conn.execute(
                        "SELECT * FROM app.harness_apply_receipts WHERE task_id=%s", (task_id,)
                    ).fetchone()
                    if existing:
                        if existing["tree_hash"] != expected_tree:
                            raise DomainError("IDEMPOTENCY_CONFLICT", "回写树不同。", 409)
                        if existing["state"] in {"applied", "conflict"}:
                            return {**existing["receipt"], "duplicate": True}
                        raise DomainError(
                            "HARNESS_APPLY_UNKNOWN", "回写曾中断，请核对原项目后恢复。", 409
                        )
                    conn.execute(
                        "INSERT INTO app.harness_apply_receipts(task_id,session_id,tree_hash,"
                        "state) VALUES (%s,%s,%s,'prepared')",
                        (task_id, session_id, expected_tree),
                    )
                try:
                    baseline = workspace._load_baseline()
                    preimages = {
                        item["path"]: baseline.get(item["path"]) for item in workspace.status()
                    }
                    receipt = workspace.apply_back(Path(row["request"]["source_root"]), preimages)
                    value = receipt.model_dump(mode="json")
                except Exception:
                    with self.engine.transaction() as conn:
                        conn.execute(
                            "UPDATE app.harness_apply_receipts SET state='unknown',"
                            "updated_at=now() WHERE task_id=%s",
                            (task_id,),
                        )
                    raise
                state = "applied" if value["status"] == "applied" else "conflict"
                with self.engine.transaction() as conn:
                    conn.execute(
                        "UPDATE app.harness_apply_receipts SET state=%s,receipt=%s,"
                        "updated_at=now() WHERE task_id=%s",
                        (state, Jsonb(value), task_id),
                    )
                return value

    def _verified_workspace(self, project_id: str, task_id: str) -> TaskWorkspace:
        assert self.harness.store is not None
        row = self.harness.store.get(task_id)
        if row["workspace_id"] != project_id or row["public_status"] not in _TERMINAL:
            raise DomainError("HARNESS_CONTINUATION_SCOPE", "任务或工作区范围不匹配。", 409)
        workspace = self.harness.registry.open_task_workspace(
            self.harness.registry.load(project_id), task_id, allow_unreconciled=True
        )
        if workspace.snapshot().tree_hash != row["tree_hash"]:
            raise DomainError("HARNESS_CONTINUATION_CONFLICT", "任务终态代码树已改变。", 409)
        return workspace

    def start_preview(self, session_id: str, task_id: str) -> dict[str, Any]:
        from mgar_v3.harness.broker import CapabilityAuthority, CommandIdentity, issue_lease
        from mgar_v3.harness.commands import CommandSession
        from mgar_v3.harness.contracts import CancellationToken, CapabilityGrant
        from mgar_v3.harness.product_runtime import ProductPreview, discover_product_plan
        from mgar_v3.harness.service import WorkspaceBoundBroker

        with self._lock:
            session = self._session(session_id)
            if session["latest_task_id"] != task_id:
                raise DomainError("HARNESS_PREVIEW_STALE", "请预览最新一轮代码。", 409)
            authorization = self.authorization(session["project_id"], required=True)
            if not authorization["allow_write"]:
                raise DomainError("HARNESS_WRITE_DENIED", "启动应用需要项目执行授权。", 403)
            task = self.harness.get_task(task_id)
            if task.get("status") not in _TERMINAL:
                raise DomainError("HARNESS_PREVIEW_BUSY", "请等待当前任务停止修改后预览。", 409)
            if session_id in self._previews:
                active = self._previews[session_id].start()
                if active["status"] == "running":
                    return {**active, **self._preview_links(session_id)}
                self.stop_preview(session_id)
            with self.engine.transaction() as conn:
                prior = conn.execute(
                    "SELECT * FROM app.harness_previews WHERE session_id=%s", (session_id,)
                ).fetchone()
            if prior is not None and prior["state"] != "closed":
                self._cleanup_preview_record(dict(prior))
            assert self.harness.store is not None
            row = self.harness.store.get(task_id)
            workspace = self._verified_workspace(session["project_id"], task_id)
            plan = discover_product_plan(
                workspace.snapshot().files, image_lock=str(self.image_lock(session["project_id"]))
            )
            grant = CapabilityGrant(
                actor=self.harness.actor,
                workspace_id=session["project_id"],
                repository=row["repository"],
                read_paths=("",),
                write_paths=("",),
                execute=True,
                allowed_commands=plan.programs,
                max_seconds=600,
            )
            lease = issue_lease(
                mode="modify_test",
                actor=grant.actor,
                workspace_id=grant.workspace_id,
                repository=grant.repository,
                execution_epoch=row["execution_epoch"],
                read_paths=grant.read_paths,
                write_paths=grant.write_paths,
                allowed_commands=grant.allowed_commands,
                timeout_seconds=600,
                ttl_seconds=600,
                memory_mb=1024,
                nofile_limit=1024,
                approved_by=grant.actor,
                approval_ref=authorization["profile_hash"],
            )

            def authority() -> Any:
                current = self.authorization(session["project_id"], required=True)
                if current["revision"] != authorization["revision"] or not current["allow_write"]:
                    raise DomainError("HARNESS_AUTHORIZATION_REVOKED", "预览授权已撤销。", 403)
                if self._session(session_id)["latest_task_id"] != task_id:
                    raise DomainError("HARNESS_PREVIEW_STALE", "新轮次已开始，旧预览停止。", 409)
                return lease

            owner = self

            class PreviewBroker(WorkspaceBoundBroker):
                def launch(self, request: Any, grant: Any, cancellation: Any, **kwargs: Any) -> Any:
                    identity = CommandIdentity(
                        task_id=task_id,
                        run_id=str(row["run_id"]),
                        operation_id=request.operation_id,
                        execution_epoch=row["execution_epoch"],
                        workspace_hash=digest(str(workspace.root)),
                    )

                    def before_create(launched: Any) -> None:
                        authority()
                        with owner.engine.transaction() as conn:
                            written = conn.execute(
                                "INSERT INTO app.harness_previews(session_id,task_id,identity,"
                                "lease,state) VALUES (%s,%s,%s,%s,'prepared') "
                                "ON CONFLICT(session_id) DO UPDATE SET task_id=EXCLUDED.task_id,"
                                "identity=EXCLUDED.identity,lease=EXCLUDED.lease,state='prepared',"
                                "updated_at=now() WHERE app.harness_previews.state='closed' "
                                "RETURNING session_id",
                                (
                                    session_id,
                                    task_id,
                                    Jsonb(asdict(identity)),
                                    Jsonb(lease.model_dump(mode="json")),
                                ),
                            ).fetchone()
                            if written is None:
                                raise DomainError(
                                    "HARNESS_PREVIEW_RECOVERY_BLOCKED",
                                    "旧预览的清理结果仍未知。",
                                    409,
                                )

                    return super().launch(
                        request,
                        grant,
                        cancellation,
                        identity=identity,
                        before_create=before_create,
                        **kwargs,
                    )

            broker = PreviewBroker(
                workspace,
                CapabilityAuthority(lease_source=authority, budget_check=lambda: True),
                lock_path=Path(plan.image_lock),
                allow_internal_write_symlinks=True,
            )
            preview = ProductPreview(CommandSession(broker), grant, CancellationToken(), plan)
            self._previews[session_id] = preview
            try:
                result = preview.start()
            except Exception:
                self.stop_preview(session_id)
                raise
            with self.engine.transaction() as conn:
                conn.execute(
                    "UPDATE app.harness_previews SET state='running',updated_at=now() "
                    "WHERE session_id=%s",
                    (session_id,),
                )
            return {**result, **self._preview_links(session_id)}

    def _preview_links(self, session_id: str) -> dict[str, str]:
        token = self._preview_urls.get(session_id)
        if token is None:
            token = secrets.token_urlsafe(32)
            self._preview_urls[session_id] = token
            self._preview_tokens[token] = session_id
        return {
            "preview_url": f"/api/v3/agent/preview/{token}/",
            "preview_control_url": f"/api/v3/agent/sessions/{session_id}/preview",
        }

    def fetch_preview_token(self, token: str, path: str) -> tuple[int, str, bytes]:
        if not re.fullmatch(r"[A-Za-z0-9_-]{43}", token):
            raise DomainError("HARNESS_PREVIEW_UNKNOWN", "预览读取凭证无效或已过期。", 404)
        session_id = self._preview_tokens.get(token)
        if session_id is None:
            raise DomainError("HARNESS_PREVIEW_UNKNOWN", "预览读取凭证无效或已过期。", 404)
        return self.fetch_preview(session_id, path)

    def fetch_preview(self, session_id: str, path: str) -> tuple[int, str, bytes]:
        self._session(session_id)
        preview = self._previews.get(session_id)
        if preview is None:
            raise DomainError("HARNESS_PREVIEW_STOPPED", "预览已停止，请重新启动。", 409)
        return cast(tuple[int, str, bytes], preview.fetch("/" + path.lstrip("/")))

    def stop_preview(self, session_id: str) -> dict[str, Any]:
        self._session(session_id)
        with self._lock:
            token = self._preview_urls.pop(session_id, None)
            if token:
                self._preview_tokens.pop(token, None)
            preview = self._previews.pop(session_id, None)
            if preview:
                preview.close()
            with self.engine.transaction() as conn:
                record = conn.execute(
                    "SELECT * FROM app.harness_previews WHERE session_id=%s", (session_id,)
                ).fetchone()
            if record is not None and record["state"] != "closed":
                self._cleanup_preview_record(dict(record))
        return {"status": "stopped"}

    def _cleanup_preview_record(self, preview: dict[str, Any], *, strict: bool = True) -> None:
        from mgar_v3.harness.broker import (
            CapabilityLease,
            CommandIdentity,
            IsolatedExecutionBroker,
        )

        try:
            task = self.engine.harness.get(preview["task_id"])
            broker = IsolatedExecutionBroker(
                Path(task["request"]["workspace_root"]),
                CapabilityLease.model_validate(preview["lease"]),
                lock_path=self.harness.image_lock,
            )
            identity = CommandIdentity(**preview["identity"])

            def exists() -> bool:
                probe = broker._docker(["inspect", "--format", "{{.Id}}", identity.name])
                if probe.returncode == 0:
                    return True
                error = probe.stderr.decode("utf-8", "replace").lower()
                if any(
                    message in error
                    for message in (
                        "no such object: " + identity.name,
                        "no such container: " + identity.name,
                    )
                ):
                    return False
                raise DomainError(
                    "HARNESS_PREVIEW_RECOVERY_BLOCKED", "无法读取 Docker 进程状态。", 503
                )

            if exists():
                broker.remove_owned_command(identity)
            if exists():
                raise DomainError("HARNESS_PREVIEW_RECOVERY_BLOCKED", "清理尚未确认。", 409)
        except (DomainError, ValueError, TypeError, OSError):
            state = "unknown"
        else:
            state = "closed"
        with self.engine.transaction() as conn:
            conn.execute(
                "UPDATE app.harness_previews SET state=%s,updated_at=now() WHERE session_id=%s",
                (state, preview["session_id"]),
            )
        if state == "unknown" and strict:
            raise DomainError(
                "HARNESS_PREVIEW_RECOVERY_BLOCKED",
                "无法确认旧预览已清理，请恢复运行环境后重试。",
                409,
            )

    def _cleanup_previews(self) -> None:
        with self.engine.transaction() as conn:
            rows = conn.execute(
                "SELECT * FROM app.harness_previews WHERE state!='closed'"
            ).fetchall()
        for preview in rows:
            self._cleanup_preview_record(dict(preview), strict=False)

    def close(self) -> None:
        for session_id in list(self._previews):
            try:
                self.stop_preview(session_id)
            except DomainError:
                pass  # Unknown ownership remains persisted for startup reconciliation.
