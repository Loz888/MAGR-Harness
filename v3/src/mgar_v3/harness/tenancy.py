"""Managed workspace identities and membership on the Engine-owned connection."""

from __future__ import annotations

import json
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import TYPE_CHECKING
from uuid import UUID, uuid4

from cryptography.fernet import InvalidToken

from mgar_v3.context.policy import ContextPolicy
from mgar_v3.domain import DomainError
from mgar_v3.harness.product_models import product_model_capabilities
from mgar_v3.settings import Settings

if TYPE_CHECKING:
    from mgar_v3.coordination.engine import Connection, Engine, Row
    from mgar_v3.harness.workspace import Registration, WorkspaceRegistry
    from mgar_v3.web_access import WebAnalysisSettings


class HarnessTenancy:
    def __init__(
        self,
        engine: Engine,
        registry: WorkspaceRegistry,
        settings: WebAnalysisSettings,
        registered_projects: Callable[[], Sequence[Row]],
    ) -> None:
        self.engine, self.registry, self.settings = engine, registry, settings
        self.registered_projects = registered_projects

    def _validate_workspace(self, registration: Registration) -> None:
        entries = self.registered_projects()
        path = next(
            (item["path"] for item in entries if item["project_id"] == registration.workspace_id),
            None,
        )
        if not path:
            raise DomainError("HARNESS_PROJECT_UNKNOWN", "项目不存在。", 404)
        root = Path(path).resolve()
        if registration.kind == "local":
            self.registry._source_root(registration, Path(path))
        protected = [
            self.engine.settings.artifact_root,
            self.registry.root,
            self.settings.key_path.parent,
            self.engine.settings.data_profile_path,
        ]
        if registration.kind == "local":
            protected.append(self.registry.root.parent)
        if any(
            root.is_relative_to(directory.resolve()) or directory.resolve().is_relative_to(root)
            for directory in protected
        ):
            raise DomainError(
                "HARNESS_WORKSPACE_PROTECTED",
                "项目目录与服务数据范围重叠。",
                409,
            )
        for other in entries:
            if other["project_id"] == registration.workspace_id or not other["path"]:
                continue
            other_root = Path(other["path"]).resolve()
            if root.is_relative_to(other_root) or other_root.is_relative_to(root):
                raise DomainError(
                    "HARNESS_WORKSPACE_OVERLAP",
                    "项目目录与已登记的工作区范围重叠。",
                    409,
                )

    def managed(self, project_id: str) -> Row | None:
        with self.engine.transaction() as conn:
            row = conn.execute(
                "SELECT * FROM app.harness_managed_projects WHERE workspace_id=%s",
                (project_id,),
            ).fetchone()
        if row is not None:
            registration = self.registry.load(project_id)
            if registration.registration_hash != row["registration_hash"]:
                raise DomainError(
                    "HARNESS_PROJECT_BINDING_CHANGED", "Workspace registration changed", 409
                )
            self._validate_workspace(registration)
        return row

    def configure(self, project_id: str, max_parallel: int, max_queued: int) -> Row:
        registration = self.registry.load(project_id)
        self._validate_workspace(registration)
        with self.engine.transaction() as conn:
            row = conn.execute(
                "INSERT INTO app.harness_managed_projects "
                "(workspace_id,registration_hash,max_parallel,max_queued) VALUES (%s,%s,%s,%s) "
                "ON CONFLICT (workspace_id) DO UPDATE SET max_parallel=EXCLUDED.max_parallel,"
                "max_queued=EXCLUDED.max_queued,updated_at=now() "
                "WHERE harness_managed_projects.registration_hash=EXCLUDED.registration_hash "
                "RETURNING *",
                (project_id, registration.registration_hash, max_parallel, max_queued),
            ).fetchone()
        if row is None:
            raise DomainError(
                "HARNESS_PROJECT_BINDING_CHANGED", "Workspace registration changed", 409
            )
        return row

    def members(self, project_id: str) -> list[Row]:
        self._require_managed(project_id)
        with self.engine.transaction() as conn:
            return list(
                conn.execute(
                    "SELECT m.membership_id,m.user_id,a.username,m.role,m.created_at,m.revoked_at "
                    "FROM app.harness_project_memberships m "
                    "JOIN app.web_accounts a USING (user_id) "
                    "WHERE m.workspace_id=%s ORDER BY m.created_at DESC LIMIT 200",
                    (project_id,),
                ).fetchall()
            )

    def grant(self, project_id: str, user_id: UUID, role: str) -> Row:
        self._require_managed(project_id)
        with self.engine.transaction() as conn:
            user = conn.execute(
                "SELECT username FROM app.web_accounts WHERE user_id=%s AND NOT is_owner",
                (user_id,),
            ).fetchone()
            if user is None:
                raise DomainError("HARNESS_MEMBER_UNKNOWN", "Member account was not found", 404)
            existing = active_membership(conn, project_id, user_id=user_id)
            if existing and existing["role"] == role:
                return existing
            conn.execute(
                "UPDATE app.harness_project_memberships SET revoked_at=now() "
                "WHERE workspace_id=%s AND user_id=%s AND revoked_at IS NULL",
                (project_id, user_id),
            )
            row = conn.execute(
                "INSERT INTO app.harness_project_memberships "
                "(membership_id,workspace_id,user_id,role) VALUES (%s,%s,%s,%s) RETURNING *",
                (uuid4(), project_id, user_id, role),
            ).fetchone()
        assert row is not None
        return row

    def revoke(self, project_id: str, user_id: UUID) -> None:
        self._require_managed(project_id)
        with self.engine.transaction() as conn:
            conn.execute(
                "UPDATE app.harness_project_memberships SET revoked_at=now() "
                "WHERE workspace_id=%s AND user_id=%s AND revoked_at IS NULL",
                (project_id, user_id),
            )

    def projects(self, user_id: UUID) -> set[str]:
        with self.engine.transaction() as conn:
            return {
                row["workspace_id"]
                for row in conn.execute(
                    "SELECT workspace_id FROM app.harness_project_memberships "
                    "WHERE user_id=%s AND revoked_at IS NULL",
                    (user_id,),
                ).fetchall()
            }

    def require_project(self, project_id: str, user_id: UUID) -> Row:
        with self.engine.transaction() as conn:
            member = active_membership(conn, project_id, user_id=user_id)
        if member is None:
            raise DomainError("HARNESS_PROJECT_UNKNOWN", "项目不存在。", 404)
        self._require_managed(project_id)
        return member

    def task_ids(self, user_id: UUID) -> set[str]:
        with self.engine.transaction() as conn:
            return {
                row["task_id"]
                for row in conn.execute(
                    "SELECT t.task_id FROM app.harness_tasks t "
                    "JOIN app.harness_project_memberships m "
                    "ON m.membership_id::text=t.request->>'product_membership_id' "
                    "WHERE t.managed_workspace_id=m.workspace_id "
                    "AND m.user_id=%s AND m.revoked_at IS NULL "
                    "ORDER BY t.created_at DESC LIMIT 200",
                    (user_id,),
                ).fetchall()
            }

    def require_task(self, task_id: str, user_id: UUID) -> None:
        with self.engine.transaction() as conn:
            row = conn.execute(
                "SELECT t.workspace_id FROM app.harness_tasks t "
                "JOIN app.harness_sessions s ON s.session_id=t.request->>'product_session_id' "
                "JOIN app.harness_project_memberships m USING (membership_id) "
                "WHERE t.task_id=%s AND t.managed_workspace_id=m.workspace_id "
                "AND t.request->>'product_membership_id'=m.membership_id::text "
                "AND m.user_id=%s AND m.revoked_at IS NULL",
                (task_id, user_id),
            ).fetchone()
        if row is None:
            raise DomainError("HARNESS_TASK_UNKNOWN", "任务不存在。", 404)
        self._require_managed(row["workspace_id"])

    def require_binding(
        self,
        project_id: str,
        membership_id: str,
        *,
        write: bool = False,
    ) -> None:
        self._require_managed(project_id)
        if not membership_id:
            return  # A managed project can also be used by the instance operator.
        with self.engine.transaction() as conn:
            require_membership(conn, project_id, membership_id, write=write)

    def save_credentials(
        self,
        project_id: str,
        *,
        deepseek_api_key: str,
        github_read_token: str | None,
        model_id: str,
        context_policy: ContextPolicy,
    ) -> int:
        self._require_managed(project_id)
        capability = product_model_capabilities(model_id)
        context_policy.window(capability.context_tokens, capability.max_output_tokens)
        data = {
            "deepseek_api_key": deepseek_api_key,
            "github_read_token": github_read_token,
            "allow_paid": bool(deepseek_api_key),
            "authorization": {
                "model": {
                    "model_id": model_id,
                    "context_policy": context_policy.model_dump(mode="json"),
                }
            },
        }
        encrypted = self.settings._cipher(create=True).encrypt(json.dumps(data).encode()).decode()
        with self.engine.transaction() as conn:
            row = conn.execute(
                "INSERT INTO app.harness_project_credentials(workspace_id,encrypted_payload) "
                "VALUES (%s,%s) RETURNING revision",
                (project_id, encrypted),
            ).fetchone()
        assert row is not None
        return int(row["revision"])

    def model_snapshot(
        self,
        project_id: str,
        revision: int | None = None,
    ) -> tuple[int, Row]:
        self._require_managed(project_id)
        with self.engine.transaction() as conn:
            row = conn.execute(
                "SELECT revision,encrypted_payload FROM app.harness_project_credentials "
                "WHERE workspace_id=%s ORDER BY revision DESC LIMIT 1",
                (project_id,),
            ).fetchone()
        current = int(row["revision"]) if row else 0
        if revision is not None and revision != current:
            raise DomainError("HARNESS_PROJECT_MODEL_CHANGED", "项目模型凭据已更新。", 409)
        if row is None:
            return 0, {"allow_paid": False, "authorization": {}}
        try:
            return current, json.loads(
                self.settings._cipher().decrypt(
                    row["encrypted_payload"].encode(),
                )
            )
        except (InvalidToken, ValueError) as exc:
            raise DomainError("SETTINGS_UNREADABLE", "无法解密项目凭据。", 503) from exc

    def model_settings(self, payload: Row) -> Settings:
        # Erase process-global key defaults before applying project secrets.
        from pydantic import SecretStr

        model_key, github_token = payload.get("deepseek_api_key"), payload.get("github_read_token")
        return self.settings.base.model_copy(
            update={
                "deepseek_api_key": SecretStr(model_key) if model_key else None,
                "github_read_token": SecretStr(github_token) if github_token else None,
                "allow_paid": payload["allow_paid"],
            }
        )

    def _require_managed(self, project_id: str) -> Row:
        project = self.managed(project_id)
        if project is None:
            raise DomainError("HARNESS_PROJECT_NOT_MANAGED", "请先配置项目隔离和额度。", 409)
        return project


def active_membership(
    conn: Connection,
    project_id: str,
    *,
    user_id: UUID,
) -> Row | None:
    return conn.execute(
        "SELECT * FROM app.harness_project_memberships "
        "WHERE workspace_id=%s AND user_id=%s AND revoked_at IS NULL",
        (project_id, user_id),
    ).fetchone()


def require_membership(
    conn: Connection,
    project_id: str,
    membership_id: str,
    *,
    write: bool,
) -> Row:
    row = conn.execute(
        "SELECT * FROM app.harness_project_memberships "
        "WHERE workspace_id=%s AND membership_id=%s AND revoked_at IS NULL",
        (project_id, membership_id),
    ).fetchone()
    if row is None:
        raise DomainError("HARNESS_MEMBER_REVOKED", "项目成员权限已撤销。", 403)
    if write and row["role"] != "contributor":
        raise DomainError("HARNESS_MEMBER_READONLY", "当前成员获准进行只读调查。", 403)
    return row
