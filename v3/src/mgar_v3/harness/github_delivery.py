"""GitHub coding delivery, separate from read-only repository analysis.

Models only edit/test an isolated Harness workspace. A full verified diff is
reviewed, then an authenticated exact-payload approval queues a Draft PR.
Merging is a separate exact-PR/head action after GitHub CI/review checks.
"""

from __future__ import annotations

import base64
import threading
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from pydantic import ValidationError

from mgar_v3.coordination.engine import Row
from mgar_v3.domain import DomainError, digest
from mgar_v3.effects.dispatcher import DefinitiveFailure, PublicationDispatcher
from mgar_v3.harness.github_ledger import HarnessGitHubLedger
from mgar_v3.harness.github_remote import (
    GitHubReader,
    GitHubRESTReader,
    GitHubRESTWriter,
    GitHubWriter,
)
from mgar_v3.harness.product import HarnessProductService
from mgar_v3.harness.recovery_io import workspace_lock
from mgar_v3.repair.pr_delivery import (
    PublicationApproval,
    PublicationFile,
    ReviewBundle,
    seal_bundle,
    verify_approval,
)
from mgar_v3.repair.workspace import blob
from mgar_v3.repository_analysis.domain import parse_repository_url
from mgar_v3.repository_analysis.workspace import RepositoryWorkspace
from mgar_v3.workbench.publication import PublicationDenied


class HarnessGitHubService:
    def __init__(
        self,
        product: HarnessProductService,
        *,
        reader: GitHubReader | None = None,
        writer: GitHubWriter | None = None,
        merge_reader: GitHubReader | None = None,
        mode: str = "live",
    ) -> None:
        if mode not in {"fixture", "live"}:
            raise ValueError("Unknown GitHub delivery mode")
        self.product, self.engine, self.harness = product, product.engine, product.harness
        self.mode = mode
        settings = self.engine.settings
        self._injected_reader = reader is not None
        self._read_revision = -1
        self._read_token = ""
        self.reader: GitHubReader = reader or GitHubRESTReader()
        self._refresh_reader()
        # A deployment toggle enables the port only; the PG grant + exact
        # approval remain mandatory and are rechecked before every write.
        if writer is None and settings.allow_external_write and settings.github_write_token:
            writer = GitHubRESTWriter(settings.github_write_token.get_secret_value())
            merge_reader = GitHubRESTReader(settings.github_write_token.get_secret_value())
        self.writer = writer
        self.merge_reader = merge_reader or self.reader
        self.ledger = HarnessGitHubLedger(self.engine, self._validate)
        self.transport = HarnessGitHubTransport(self)
        self.dispatcher = PublicationDispatcher(self.ledger, self.transport)
        self._worker_lock = threading.Lock()
        self.ledger.recover()

    def _refresh_reader(self) -> None:
        if self._injected_reader:
            return
        revision, payload = self.product._model_snapshot()
        if revision != self._read_revision:
            effective = self.product.settings.effective(payload)
            token = effective.github_read_token
            self._read_token = token.get_secret_value() if token else ""
            self.reader = GitHubRESTReader(self._read_token)
            self._read_revision = revision
            if hasattr(self, "merge_reader") and self.writer is None:
                self.merge_reader = self.reader

    def register_project(self, repository_url: str, commit: str) -> Row:
        self._refresh_reader()
        try:
            identity, _, _ = parse_repository_url(repository_url)
        except ValueError as exc:
            raise DomainError("GITHUB_REPOSITORY_INVALID", "请填写 GitHub 仓库地址。", 422) from exc
        workspace = RepositoryWorkspace(
            self.harness.root / "github-sources",
            repository=identity.full_name,
            commit=commit,
            remote=f"https://github.com/{identity.full_name}",
            run_id="coding",
        )
        # Credentials exist only in this trusted fetch process environment.
        # They are never written to a Git URL, git config, checkpoint or log.
        if self._read_token:
            basic = base64.b64encode(("x-access-token:" + self._read_token).encode()).decode()
            workspace._environment.update(
                {
                    "GIT_CONFIG_COUNT": "1",
                    "GIT_CONFIG_KEY_0": "http.https://github.com/.extraheader",
                    "GIT_CONFIG_VALUE_0": "Authorization: Basic " + basic,
                }
            )
        lock_root = (
            self.harness.root
            / "github-import-locks"
            / digest(
                {
                    "repository": identity.full_name.lower(),
                    "commit": commit,
                }
            )
        )
        lock_root.mkdir(parents=True, exist_ok=True)
        with workspace_lock(lock_root):
            try:
                manifest = workspace.prepare()
            except DomainError as exc:
                # Git may reflect a remote diagnostic. Do not publish it when
                # a credential was used by the subprocess.
                if self._read_token:
                    raise DomainError(
                        exc.code, "GitHub 固定快照读取失败，请核对读权限。", exc.status
                    ) from None
                raise
            result = self.harness.register_project(str(workspace.worktree))
        if result["repository"] != identity.full_name.lower() or result["base_commit"] != commit:
            raise DomainError("GITHUB_SNAPSHOT_MISMATCH", "固定仓库快照注册不一致。", 409)
        return {
            **result,
            "github_source": {"repository": result["repository"], "commit": commit},
            "source_complete": not manifest.exclusions,
            "coverage": manifest.coverage.model_dump(mode="json"),
            "exclusions": [entry.model_dump(mode="json") for entry in manifest.exclusions],
        }

    def authorization(self, project_id: str) -> Row:
        project = self.product._project(project_id)
        registration = self.harness.registry.load(project_id)
        eligible = (
            not registration.repository.startswith("local/")
            and registration.base_commit != "0" * 40
            and registration.remote.startswith(("https://github.com/", "git@github.com:"))
        )
        with self.engine.transaction() as conn:
            grant = conn.execute(
                "SELECT * FROM app.harness_github_authorizations WHERE project_id=%s "
                "ORDER BY revision DESC LIMIT 1",
                (project_id,),
            ).fetchone()
        valid = bool(
            eligible
            and grant
            and grant["actor"] == self.harness.actor
            and grant["registration_hash"] == registration.registration_hash
            and grant["repository"] == registration.repository
            and grant["expires_at"] > datetime.now(UTC)
            and not grant["revoked_at"]
        )
        return {
            "revision": 0,
            "base_branch": "main",
            "allow_publish": False,
            "allow_merge": False,
            **(grant or {}),
            "eligible": eligible,
            "valid": valid,
            "repository": project["repository"],
            "commit": project["base_commit"],
            "writer_available": self.writer is not None,
        }

    def authorize(
        self,
        project_id: str,
        *,
        allow_publish: bool,
        allow_merge: bool,
        base_branch: str = "main",
        days: int = 1,
        expected_revision: int = 0,
    ) -> Row:
        # Reuse the original project/cost grant; GitHub authority adds no money.
        project_grant = self.product.authorization(project_id, required=True)
        if not project_grant["allow_write"]:
            raise DomainError("HARNESS_WRITE_DENIED", "请先授予隔离工作区修改权限。", 403)
        current = self.authorization(project_id)
        if not current["eligible"]:
            raise DomainError("GITHUB_PROJECT_REQUIRED", "项目没有可核对的 GitHub 固定版本。", 409)
        if current["revision"] != expected_revision:
            raise DomainError("GITHUB_AUTHORIZATION_STALE", "发布许可已更新，请刷新。", 409)
        registration = self.harness.registry.load(project_id)
        with self.engine.transaction() as conn:
            conn.execute(
                "INSERT INTO app.harness_github_authorizations(project_id,revision,actor,"
                "registration_hash,repository,base_branch,allow_publish,allow_merge,expires_at) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                (
                    project_id,
                    expected_revision + 1,
                    self.harness.actor,
                    registration.registration_hash,
                    registration.repository,
                    base_branch,
                    allow_publish,
                    allow_merge,
                    datetime.now(UTC) + timedelta(days=days),
                ),
            )
        return self.authorization(project_id)

    def revoke(self, project_id: str) -> Row:
        self.authorization(project_id)
        with self.engine.transaction() as conn:
            conn.execute(
                "UPDATE app.harness_github_authorizations SET revoked_at=now() WHERE project_id=%s",
                (project_id,),
            )
        return self.authorization(project_id)

    def review(
        self,
        session_id: str,
        *,
        task_id: str,
        expected_tree: str,
        base_branch: str,
        title: str,
        body: str,
    ) -> Row:
        self._refresh_reader()
        session = self.product._session(session_id)
        if session["latest_task_id"] != task_id:
            raise DomainError("GITHUB_REVIEW_STALE", "只能审查会话最新一轮已验证变更。", 409)
        grant = self.product.authorization(session["project_id"], required=True)
        if not grant["allow_write"]:
            raise DomainError("HARNESS_WRITE_DENIED", "工作区修改许可已关闭。", 403)
        source = self.authorization(session["project_id"])
        if not source["eligible"]:
            raise DomainError("GITHUB_PROJECT_REQUIRED", "项目不是固定版本 GitHub 工作区。", 409)
        task = self.harness.get_task(task_id)
        verification = task.get("verification") or {}
        tests = task.get("tests") or {}
        passed_receipts = [
            receipt
            for receipt in task.get("receipts", [])
            if receipt.get("purpose") == "test"
            and receipt.get("status") == "passed"
            and receipt.get("tree_hash") == expected_tree
            and receipt.get("exit_code") == 0
        ]
        if (
            task.get("status") != "completed"
            or task.get("tree_after") != expected_tree
            or verification.get("status") != "verified"
            or verification.get("tree_hash") != expected_tree
            or tests.get("status") != "passed"
            or tests.get("run", 0) < 1
            or not passed_receipts
        ):
            raise DomainError(
                "GITHUB_UNVERIFIED_TREE", "必须先完成当前代码树的实际测试和核验。", 409
            )
        workspace = self.product._verified_workspace(session["project_id"], task_id)
        with workspace_lock(workspace.root):
            if workspace.manifest.dirty.entries:
                raise DomainError(
                    "GITHUB_DIRTY_BASELINE",
                    "测试基线含未提交或未完整复制的源码，请从固定版本重新导入。",
                    409,
                )
            if any(path != ".git" for path in workspace.manifest.excluded):
                raise DomainError(
                    "GITHUB_PARTIAL_WORKSPACE",
                    "工作区有被排除的项目文件，不能声称已核验完整交付。",
                    409,
                )
            if workspace.snapshot().tree_hash != expected_tree:
                raise DomainError("GITHUB_REVIEW_STALE", "代码树在测试后发生变化。", 409)
            changes = workspace.status()
            if not changes:
                raise DomainError("GITHUB_NO_CHANGES", "当前没有可交付的代码变更。", 409)
            try:
                files = [
                    PublicationFile(path=item["path"], content=workspace.read(item["path"]))
                    for item in changes
                ]
            except (ValidationError, UnicodeError) as exc:
                raise DomainError(
                    "GITHUB_UNSUPPORTED_CHANGE",
                    "变更超出常规文本交付范围，请导出完整补丁审查。",
                    422,
                ) from exc
            diff = workspace.diff()
            proposal = digest(
                {
                    "task_id": task_id,
                    "tree": expected_tree,
                    "verification": verification,
                    "tests": tests,
                }
            )
            try:
                bundle = seal_bundle(
                    repository=source["repository"],
                    base_branch=base_branch,
                    base_sha=source["commit"],
                    tree_hash=expected_tree,
                    proposal_id=proposal,
                    branch=f"harness/{task_id}-{expected_tree[:12]}",
                    title=title,
                    body=body,
                    diff=diff,
                    files=files,
                )
            except ValidationError as exc:
                raise DomainError(
                    "GITHUB_DELIVERY_SCOPE_LIMIT", "变更超出交付大小限制，请导出完整补丁审查。", 422
                ) from exc
        # Review is available without a writer, including the full exportable patch.
        if any(f"b/{item.path}" not in bundle.diff for item in bundle.files):
            raise DomainError("DELIVERY_DIFF_INCOMPLETE", "完整 Diff 缺少变更文件。", 409)
        row = self.ledger.review(
            session=session,
            task_id=task_id,
            bundle=bundle.model_dump(mode="json"),
            action_type="draft_pr",
            mode=self.mode,
        )
        return {
            **self.get(row["publication_id"]),
            "diff": bundle.diff,
            "tests": tests,
            "reproduction": [
                r["receipt_id"]
                for r in task["receipts"]
                if r.get("purpose") == "test" and r.get("exit_code") != 0
            ],
        }

    def _owned(self, identifier: UUID) -> Row:
        row = self.ledger.publication(identifier)
        self.product._session(row["session_id"])
        if row["actor"] != self.harness.actor:
            raise DomainError("ACTOR_DENIED", "GitHub 交付属于其他操作者。", 403)
        return row

    def get(self, identifier: UUID) -> Row:
        self.ledger.recover()
        row = self._owned(identifier)
        with self.engine.transaction() as conn:
            merges = conn.execute(
                "SELECT publication_id,status,payload_hash,reason_code,reason,external_id FROM "
                "app.harness_github_actions WHERE parent_id=%s ORDER BY created_at DESC",
                (identifier,),
            ).fetchall()
        task = self.harness.get_task(row["task_id"])
        public_row = {
            key: value for key, value in row.items() if key not in {"lease_token", "lease_until"}
        }
        return {
            **public_row,
            "steps": self.ledger.steps(identifier),
            "merge_actions": merges,
            "writer_available": self.writer is not None,
            "diff": row["bundle"].get("diff", ""),
            "tests": task.get("tests", {}),
            "reproduction": [
                r["receipt_id"]
                for r in task.get("receipts", [])
                if r.get("purpose") == "test" and r.get("exit_code") != 0
            ],
        }

    def for_session(self, session_id: str) -> list[Row]:
        self.product._session(session_id)
        with self.engine.transaction() as conn:
            rows = conn.execute(
                "SELECT publication_id FROM app.harness_github_actions WHERE session_id=%s "
                "AND action_type='draft_pr' ORDER BY created_at DESC LIMIT 50",
                (session_id,),
            ).fetchall()
        return [self.get(row["publication_id"]) for row in rows]

    def approve(self, identifier: UUID, payload_hash: str, *, background: bool = True) -> Row:
        self._refresh_reader()
        row = self._owned(identifier)
        if row["action_type"] != "draft_pr":
            raise DomainError("GITHUB_APPROVAL_KIND", "合并需要独立的精确 PR/head 确认。", 409)
        bundle = ReviewBundle.model_validate(row["bundle"])
        if self.writer is None:
            raise DomainError(
                "GITHUB_WRITE_CREDENTIAL_REQUIRED", "请配置独立写凭据；补丁已保留。", 403
            )
        grant = self.authorization(row["project_id"])
        approval = PublicationApproval.grant(
            bundle, actor=self.harness.actor, approved_at=datetime.now(UTC).isoformat()
        )
        self._assert_workspace(row)
        self.ledger.approve(
            identifier, payload_hash, approval.model_dump(mode="json"), grant["revision"]
        )
        if background:
            self.start_dispatch()
        return self.get(identifier)

    def _validate(self, action: Row) -> None:
        """SQL-only current authority/fence check, also called before every step."""
        if action["actor"] != self.harness.actor or self.writer is None:
            raise DomainError("GITHUB_WRITE_CREDENTIAL_REQUIRED", "写凭据或操作者不匹配。", 403)
        grant = self.authorization(action["project_id"])
        kind = "allow_merge" if action["action_type"] == "merge_pr" else "allow_publish"
        if not grant["valid"] or not grant[kind] or grant["revision"] != action["grant_revision"]:
            raise DomainError(
                "GITHUB_WRITE_AUTHORIZATION_REQUIRED", "GitHub 写许可已撤回或改变。", 403
            )
        project_grant = self.product.authorization(action["project_id"], required=True)
        if not project_grant["allow_write"]:
            raise DomainError("HARNESS_WRITE_DENIED", "项目修改权限已撤回。", 403)
        bundle = action["bundle"]
        if (
            bundle["repository"] != grant["repository"]
            or bundle["base_branch"] != grant["base_branch"]
        ):
            raise DomainError("GITHUB_WRITE_SCOPE_MISMATCH", "仓库或目标分支超出许可。", 403)
        approval = action.get("approval") or {}
        if datetime.fromisoformat(approval["approved_at"]) + timedelta(hours=1) <= datetime.now(
            UTC
        ):
            raise DomainError(
                "GITHUB_APPROVAL_EXPIRED", "精确内容确认已超过一小时，请重新审查。", 409
            )
        if action["action_type"] == "draft_pr":
            review = ReviewBundle.model_validate(bundle)
            verify_approval(review, PublicationApproval.model_validate(approval))
        elif digest(bundle) != action["payload_hash"] or approval.get("payload_hash") != digest(
            bundle
        ):
            raise DomainError("APPROVAL_HASH_MISMATCH", "合并批准与当前 PR/head 不一致。", 409)
        with self.engine.transaction() as conn:
            current = conn.execute(
                "SELECT s.latest_task_id,t.tree_hash,t.public_status FROM app.harness_sessions s "
                "JOIN app.harness_tasks t ON t.task_id=%s WHERE s.session_id=%s",
                (action["task_id"], action["session_id"]),
            ).fetchone()
            unknown = conn.execute(
                "SELECT 1 FROM app.harness_github_actions WHERE status='outcome_unknown' "
                "AND bundle->>'repository'=%s AND publication_id<>%s LIMIT 1",
                (bundle["repository"], action["publication_id"]),
            ).fetchone()
        if (
            not current
            or current["latest_task_id"] != action["task_id"]
            or current["tree_hash"] != bundle["tree_hash"]
            or current["public_status"] != "completed"
        ):
            raise DomainError("GITHUB_REVIEW_STALE", "会话或已核验代码树已改变。", 409)
        if unknown:
            raise DomainError("GITHUB_EFFECT_UNKNOWN", "同仓库有尚未核实的写入，需先对账。", 409)

    def _assert_workspace(self, action: Row) -> None:
        workspace = self.product._verified_workspace(action["project_id"], action["task_id"])
        if workspace.snapshot().tree_hash != action["bundle"]["tree_hash"]:
            raise DomainError("GITHUB_REVIEW_STALE", "隔离代码树已改变，请重新测试。", 409)

    def merge_review(self, identifier: UUID) -> Row:
        row = self._owned(identifier)
        pull = row.get("pull_request")
        if row["action_type"] != "draft_pr" or row["status"] != "succeeded" or not pull:
            raise DomainError("GITHUB_PR_REQUIRED", "请先完成并核对 Draft PR 交付。", 409)
        state = self.merge_reader.merge_checks(row["bundle"]["repository"], pull["number"])
        blockers = []
        if state["repository"] != row["bundle"]["repository"]:
            blockers.append("BASE_REPOSITORY_CHANGED")
        if state["head_repository"] != row["bundle"]["repository"]:
            blockers.append("HEAD_REPOSITORY_CHANGED")
        if state["head_sha"] != pull["head_sha"]:
            blockers.append("HEAD_CHANGED")
        if state["base_sha"] != row["bundle"]["base_sha"]:
            blockers.append("BASE_CHANGED_RETEST_REQUIRED")
        if state["base_branch"] != row["bundle"]["base_branch"]:
            blockers.append("BASE_BRANCH_CHANGED")
        if state["draft"]:
            blockers.append("DRAFT_REVIEW_REQUIRED")
        if state["state"] != "open" or state["merged"]:
            blockers.append("PR_NOT_OPEN")
        if not state["permission"]:
            blockers.append("GITHUB_WRITE_PERMISSION_REQUIRED")
        if not state["ci_passed"]:
            blockers.append("CI_NOT_PASSED")
        if not state["mergeable"] or state["mergeable_state"] != "clean":
            blockers.append("REVIEW_OR_BRANCH_RULES_NOT_SATISFIED")
        grant = self.authorization(row["project_id"])
        if not grant["valid"] or not grant["allow_merge"] or self.writer is None:
            blockers.append("EXPLICIT_MERGE_AUTHORIZATION_REQUIRED")
        identity = {
            "repository": row["bundle"]["repository"],
            "head_repository": state["head_repository"],
            "pull_number": state["pull_number"],
            "head_sha": state["head_sha"],
            "base_sha": state["base_sha"],
            "base_branch": row["bundle"]["base_branch"],
            "tree_hash": row["bundle"]["tree_hash"],
            "publication_id": str(identifier),
        }
        return {
            **state,
            "review_hash": digest(identity),
            "identity": identity,
            "can_merge": not blockers,
            "blockers": blockers,
        }

    def merge(
        self,
        identifier: UUID,
        *,
        pull_number: int,
        head_sha: str,
        base_sha: str,
        review_hash: str,
        confirm: bool,
        background: bool = True,
    ) -> Row:
        row = self._owned(identifier)
        review = self.merge_review(identifier)
        if not confirm or (pull_number, head_sha, base_sha, review_hash) != (
            review["pull_number"],
            review["head_sha"],
            review["base_sha"],
            review["review_hash"],
        ):
            raise DomainError(
                "GITHUB_MERGE_APPROVAL_MISMATCH", "请准确确认当前 PR、head 和目标版本。", 409
            )
        if not review["can_merge"]:
            raise DomainError("GITHUB_MERGE_BLOCKED", ", ".join(review["blockers"]), 409)
        self._assert_workspace(row)
        action = self.ledger.review(
            session=self.product._session(row["session_id"]),
            task_id=row["task_id"],
            bundle=review["identity"],
            action_type="merge_pr",
            mode=self.mode,
            parent_id=identifier,
        )
        grant = self.authorization(row["project_id"])
        approval = {
            "payload_hash": review_hash,
            "actor": self.harness.actor,
            "approved_at": datetime.now(UTC).isoformat(),
        }
        self.ledger.approve(action["publication_id"], review_hash, approval, grant["revision"])
        if background:
            self.start_dispatch()
        return self.get(action["publication_id"])

    def reconcile(self, identifier: UUID) -> Row:
        self._refresh_reader()
        self._owned(identifier)
        self.dispatcher.reconcile(identifier)
        return self.get(identifier)

    def dispatch(self, identifier: UUID, payload_hash: str) -> Row:
        action = self._owned(identifier)
        if action["payload_hash"] != payload_hash or action["status"] != "ready":
            raise DomainError("GITHUB_DISPATCH_STALE", "仅能派发原已批准且从未发送的动作。", 409)
        self._validate(action)
        self._assert_workspace(action)
        self.start_dispatch()
        return self.get(identifier)

    def resume(self, identifier: UUID, payload_hash: str, *, background: bool = True) -> Row:
        self._refresh_reader()
        action = self._owned(identifier)
        if action["payload_hash"] != payload_hash or action["action_type"] != "draft_pr":
            raise DomainError("APPROVAL_HASH_MISMATCH", "请确认原完整 Draft PR 提案。", 409)
        if self.transport.find(action) is not None:
            return self.reconcile(identifier)
        bundle = ReviewBundle.model_validate(action["bundle"])
        head = self.reader.branch_sha(bundle.repository, bundle.branch)
        proof: dict[int, Row] = {}
        if head == bundle.base_sha:
            proof[0] = {"sha": head}
        elif head and self.transport._commit_matches(bundle, head):
            proof = {0: {"base_sha": bundle.base_sha}, 1: {"head_sha": head}}
        approval = PublicationApproval.grant(
            bundle, actor=self.harness.actor, approved_at=datetime.now(UTC).isoformat()
        )
        workspace = self.product._verified_workspace(action["project_id"], action["task_id"])
        with workspace_lock(workspace.root), self.engine._mutex:
            self._assert_workspace(action)
            self.ledger.resume(
                identifier,
                proof,
                approval.model_dump(mode="json"),
                self.authorization(action["project_id"])["revision"],
            )
        if background:
            self.start_dispatch()
        return self.get(identifier)

    def start_dispatch(self) -> None:
        def work() -> None:
            with self._worker_lock:
                while self.dispatcher.tick() is not None:
                    pass

        threading.Thread(target=work, name="harness-github-outbox", daemon=True).start()


class HarnessGitHubTransport:
    def __init__(self, service: HarnessGitHubService) -> None:
        self.service, self.ledger = service, service.ledger
        self.mode = service.mode

    def _commit_matches(self, bundle: ReviewBundle, sha: str) -> bool:
        remote = self.service.reader
        commit = remote.commit(bundle.repository, sha)
        if commit["parents"] != [bundle.base_sha]:
            return False
        expected = dict(remote.commit(bundle.repository, bundle.base_sha)["files"])
        for item in bundle.files:
            old = expected.get(item.path)
            mode = old["mode"] if old else "100644"
            if mode not in {"100644", "100755"}:
                return False
            expected[item.path] = {"sha": blob(item.content.encode()), "mode": mode, "type": "blob"}
        return bool(commit["files"] == expected)

    def _write(self, action: Row, ordinal: int, kind: str, request: Row, call: Any) -> None:
        workspace = self.service.product._verified_workspace(
            action["project_id"], action["task_id"]
        )
        # Existing local apply takes workspace -> Engine; preserve that lock
        # order. No PG transaction remains open during any GitHub request.
        with workspace_lock(workspace.root), self.service.engine._mutex:
            self.service._assert_workspace(action)
            if action["action_type"] == "merge_pr":
                review = self.service.merge_review(action["parent_id"])
                if not review["can_merge"] or review["review_hash"] != action["payload_hash"]:
                    raise PublicationDenied("GITHUB_MERGE_STALE", "合并前 PR/head/CI 已改变。")
            elif (
                self.service.reader.branch_sha(
                    action["bundle"]["repository"], action["bundle"]["base_branch"]
                )
                != action["bundle"]["base_sha"]
            ):
                raise PublicationDenied("GITHUB_BASE_CHANGED", "目标分支已改变。")
            try:
                should_send = self.ledger.begin_step(action, ordinal, kind, request)
            except DomainError as exc:
                raise PublicationDenied(exc.code, str(exc)) from exc
            if should_send:
                try:
                    call()
                except DefinitiveFailure as exc:
                    self.ledger.reject(action, ordinal, str(exc))
                    raise
                self.ledger.receipt(action, ordinal, {"confirmed": True})

    def _match_pull(self, bundle: ReviewBundle, pull: Any, head: str) -> bool:
        return bool(
            pull
            and pull.draft is True
            and pull.repository.lower() == bundle.repository
            and pull.head_sha == head
            and pull.head_ref == bundle.branch
            and pull.base_sha == bundle.base_sha
            and pull.base_ref == bundle.base_branch
            and pull.title == bundle.title
            and pull.body == bundle.body
        )

    def send(self, action: Row) -> str:
        try:
            self.service._validate(action)
            self.service._assert_workspace(action)
        except DomainError as exc:
            raise PublicationDenied(exc.code, str(exc)) from exc
        writer, reader = self.service.writer, self.service.reader
        assert writer is not None
        if action["action_type"] == "merge_pr":
            data = action["bundle"]
            review = self.service.merge_review(action["parent_id"])
            if not review["can_merge"] or review["review_hash"] != action["payload_hash"]:
                raise PublicationDenied("GITHUB_MERGE_STALE", "PR/head、CI 或合并权限已改变。")
            self._write(
                action,
                0,
                "merge",
                data,
                lambda: writer.merge(data["repository"], data["pull_number"], data["head_sha"]),
            )
            result = self.find(action)
            if result is None:
                raise OSError("Merge result needs read-only reconciliation")
            return result
        bundle = ReviewBundle.model_validate(action["bundle"])
        if reader.branch_sha(bundle.repository, bundle.base_branch) != bundle.base_sha:
            raise PublicationDenied("GITHUB_BASE_CHANGED", "目标分支已改变，需要重新测试和批准。")
        head = reader.branch_sha(bundle.repository, bundle.branch)
        if head is None:
            self._write(
                action,
                0,
                "branch",
                {"branch": bundle.branch, "base_sha": bundle.base_sha},
                lambda: writer.create_branch(bundle.repository, bundle.branch, bundle.base_sha),
            )
            head = reader.branch_sha(bundle.repository, bundle.branch)
        if head == bundle.base_sha:
            self._write(
                action,
                1,
                "commit",
                {"payload_hash": bundle.payload_hash},
                lambda: writer.push_files(
                    bundle.repository,
                    bundle.branch,
                    bundle.base_sha,
                    {f.path: f.content for f in bundle.files},
                    bundle.title,
                ),
            )
            head = reader.branch_sha(bundle.repository, bundle.branch)
        if head is None or not self._commit_matches(bundle, head):
            raise OSError("Published parent/full Git tree differs from reviewed changes")
        pull = reader.find_draft(bundle.repository, bundle.branch, bundle.base_branch)
        if pull is None:
            self._write(
                action,
                2,
                "pull_request",
                {"payload_hash": bundle.payload_hash},
                lambda: writer.create_draft_pull_request(
                    bundle.repository, bundle.base_branch, bundle.branch, bundle.title, bundle.body
                ),
            )
            pull = reader.find_draft(bundle.repository, bundle.branch, bundle.base_branch)
        if not self._match_pull(bundle, pull, head):
            raise OSError("Created PR differs from the approved Draft PR")
        assert pull is not None
        self.ledger.attach_pull(
            action["publication_id"],
            {
                **pull.model_dump(mode="json"),
                "url": f"https://github.com/{bundle.repository}/pull/{pull.number}",
            },
        )
        return str(pull.number)

    def find(self, action: Row) -> str | None:
        """Read-only recovery. No absent lookup proves that a write was absent."""
        if action["action_type"] == "merge_pr":
            data = action["bundle"]
            merged_pull = self.service.merge_reader.pull(data["repository"], data["pull_number"])
            if (
                merged_pull.get("merged") is True
                and str(merged_pull["base"]["repo"]["full_name"]).lower() == data["repository"]
                and str(merged_pull["head"]["repo"]["full_name"]).lower() == data["head_repository"]
                and merged_pull["head"]["sha"] == data["head_sha"]
                and merged_pull["base"]["ref"] == data["base_branch"]
                and merged_pull.get("merge_commit_sha")
            ):
                commit = self.service.merge_reader.commit(
                    data["repository"], merged_pull["merge_commit_sha"]
                )
                if commit["parents"] == [data["base_sha"], data["head_sha"]]:
                    return str(merged_pull["merge_commit_sha"])
            return None
        bundle = ReviewBundle.model_validate(action["bundle"])
        head = self.service.reader.branch_sha(bundle.repository, bundle.branch)
        if head is None or head == bundle.base_sha or not self._commit_matches(bundle, head):
            return None
        pull = self.service.reader.find_draft(bundle.repository, bundle.branch, bundle.base_branch)
        if not self._match_pull(bundle, pull, head):
            return None
        assert pull is not None
        self.ledger.attach_pull(
            action["publication_id"],
            {
                **pull.model_dump(mode="json"),
                "url": f"https://github.com/{bundle.repository}/pull/{pull.number}",
            },
        )
        return str(pull.number)
