"""Real PG + real sandbox coding; GitHub mutations remain controlled fixtures."""

from __future__ import annotations

import copy
import json
import subprocess
import threading
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import httpx
import pytest
from psycopg.pq import TransactionStatus
from psycopg.types.json import Jsonb
from pydantic import SecretStr
from test_harness_product_service import BUGGY, SUITE
from test_harness_product_service import product_env as product_env

from mgar_v3.domain import DomainError
from mgar_v3.harness.github_delivery import HarnessGitHubService
from mgar_v3.harness.github_remote import GitHubRESTReader, GitHubRESTWriter
from mgar_v3.harness.product import infer_intent
from mgar_v3.repair.pr_delivery import DraftPullRequest
from mgar_v3.repair.workspace import blob
from mgar_v3.web_access import AnalysisSettingsInput


class ControlledGitHub:
    scope = "write"

    def __init__(self, env, base):
        self.engine, self.base = env.engine, base
        self.branches = {"main": base}
        self.commits = {
            base: {
                "parents": [],
                "files": {
                    path.name: {"sha": blob(path.read_bytes()), "mode": "100644", "type": "blob"}
                    for path in env.source.iterdir()
                    if path.is_file()
                },
            }
        }
        self.calls, self.pr = [], None
        self.fail_after, self.fail_before = "", ""
        self.draft, self.ci, self.clean = True, True, True
        self.base_branch = "main"
        self.repository = "example/mgar-test"
        self.head_repository = self.repository
        self.hook = None

    def _before(self, kind):
        assert self.engine.connection.info.transaction_status == TransactionStatus.IDLE
        self.calls.append(kind)
        if self.hook:
            self.hook(kind)
        if self.fail_before == kind:
            raise OSError("controlled missing remote response")

    def _after(self, kind):
        if self.fail_after == kind:
            raise OSError("controlled response lost after remote acceptance")

    def branch_sha(self, repository, branch):
        return self.branches.get(branch)

    def commit(self, repository, sha):
        return copy.deepcopy(self.commits[sha])

    def create_branch(self, repository, branch, base_sha):
        self._before("branch")
        self.branches[branch] = base_sha
        self._after("branch")

    def push_files(self, repository, branch, base_sha, files, message):
        self._before("commit")
        head = "a" * 40
        tree = copy.deepcopy(self.commits[base_sha]["files"])
        for path, content in files.items():
            tree[path] = {"sha": blob(content.encode()), "mode": "100644", "type": "blob"}
        self.commits[head] = {"parents": [base_sha], "files": tree}
        self.branches[branch] = head
        self._after("commit")
        return head

    def create_draft_pull_request(self, repository, base, head, title, body):
        self._before("pull_request")
        self.pr = DraftPullRequest(
            number=7,
            draft=True,
            title=title,
            body=body,
            repository=repository,
            base_ref=base,
            base_sha=self.base,
            head_ref=head,
            head_sha=self.branches[head],
        )
        self._after("pull_request")
        return 7

    def find_draft(self, repository, head, base):
        return self.pr

    def merge_checks(self, repository, number):
        return {
            "repository": self.repository,
            "head_repository": self.head_repository,
            "pull_number": number,
            "head_sha": self.pr.head_sha,
            "base_sha": self.base,
            "base_branch": self.base_branch,
            "draft": self.draft,
            "state": "open",
            "merged": False,
            "permission": True,
            "ci_passed": self.ci,
            "mergeable": self.clean,
            "mergeable_state": "clean" if self.clean else "blocked",
            "checks": [],
            "statuses": [],
        }

    def merge(self, repository, number, head_sha):
        self._before("merge")
        merge_sha = "b" * 40
        self.commits[merge_sha] = {
            "parents": [self.base, head_sha],
            "files": self.commits[head_sha]["files"],
        }
        self.branches["main"] = merge_sha
        self._after("merge")
        return merge_sha

    def pull(self, repository, number):
        return {
            "merged": self.branches["main"] == "b" * 40,
            "merge_commit_sha": "b" * 40,
            "head": {"sha": self.pr.head_sha, "repo": {"full_name": self.head_repository}},
            "base": {"ref": "main", "repo": {"full_name": self.repository}},
        }


@pytest.fixture
def github_env(product_env):
    env = product_env
    env.settings.allow_external_write = False
    env.settings.github_write_token = None
    (env.source / "calc.py").write_text(BUGGY)
    (env.source / "test_calc.py").write_text(SUITE)

    def git(*args):
        result = subprocess.run(
            ["git", "-c", "core.hooksPath=/dev/null", "-C", str(env.source), *args],
            capture_output=True,
            text=True,
            check=True,
        )
        return result.stdout.strip()

    git("init", "--quiet")
    git("add", "README.md", "calc.py", "test_calc.py")
    git(
        "-c",
        "user.name=Fixture",
        "-c",
        "user.email=fixture@example.invalid",
        "commit",
        "--quiet",
        "-m",
        "Controlled initial source",
    )
    git("remote", "add", "origin", "https://github.com/example/mgar-test")
    base = git("rev-parse", "HEAD")
    env.project_id = env.harness.register_project(str(env.source))["project_id"]
    env.save_model()
    env.product.authorize(
        env.project_id,
        allow_write=True,
        allow_paid=True,
        max_cost_micros=250_000,
        max_model_calls=32,
    )
    session = env.product.create(env.project_id, "找出加法错误并提出PR", "github-fix")
    task = env.harness.get_task(session["task_id"])
    assert task["status"] == "completed", task
    assert task["tests"]["status"] == "passed" and task["tests"]["run"] >= 1
    remote = ControlledGitHub(env, base)
    service = HarnessGitHubService(
        env.product, reader=remote, writer=remote, merge_reader=remote, mode="fixture"
    )
    grant = service.authorize(env.project_id, allow_publish=True, allow_merge=True)
    assert grant["valid"]
    review = service.review(
        session["session_id"],
        task_id=task["task_id"],
        expected_tree=task["tree_after"],
        base_branch="main",
        title="Fix addition",
        body="Fix addition and verify regression tests.",
    )
    return SimpleNamespace(
        env=env,
        service=service,
        remote=remote,
        session=session,
        task=task,
        review=review,
        base=base,
    )


def enqueue(value):
    return value.service.approve(
        value.review["publication_id"], value.review["payload_hash"], background=False
    )


def publish(value):
    enqueue(value)
    assert value.service.dispatcher.tick() == value.review["publication_id"]
    return value.service.get(value.review["publication_id"])


def test_real_workspace_tests_pg_review_draft_pr_and_exact_merge(github_env):
    value = github_env
    assert value.task["receipts"] and "return a + b" in value.review["diff"]
    assert value.review["reproduction"]
    assert value.remote.calls == []
    assert enqueue(value)["status"] == "ready" and value.remote.calls == []
    assert value.service.dispatcher.tick() == value.review["publication_id"]
    result = value.service.get(value.review["publication_id"])
    assert result["status"] == "succeeded"
    assert result["pull_request"]["number"] == 7 and result["pull_request"]["draft"] is True
    assert value.remote.calls == ["branch", "commit", "pull_request"]
    assert [step["kind"] for step in result["steps"]] == ["branch", "commit", "pull_request"]
    assert (
        "DRAFT_REVIEW_REQUIRED" in value.service.merge_review(result["publication_id"])["blockers"]
    )
    value.remote.draft = False  # controlled GitHub review, never a real external write
    review = value.service.merge_review(result["publication_id"])
    assert review["can_merge"]
    with pytest.raises(DomainError, match="PR"):
        value.service.merge(
            result["publication_id"],
            pull_number=8,
            head_sha=review["head_sha"],
            base_sha=review["base_sha"],
            review_hash=review["review_hash"],
            confirm=True,
            background=False,
        )
    action = value.service.merge(
        result["publication_id"],
        pull_number=7,
        head_sha=review["head_sha"],
        base_sha=review["base_sha"],
        review_hash=review["review_hash"],
        confirm=True,
        background=False,
    )
    assert action["status"] == "ready" and len(value.remote.calls) == 3
    value.service.dispatcher.tick()
    assert value.service.get(action["publication_id"])["status"] == "succeeded"
    assert value.remote.calls == ["branch", "commit", "pull_request", "merge"]
    assert (value.env.source / "calc.py").read_text() == BUGGY


def test_read_credential_and_flags_do_not_authorize_writes(github_env):
    value = github_env
    readonly = HarnessGitHubService(value.env.product, reader=value.remote, mode="fixture")
    assert readonly.get(value.review["publication_id"])["bundle"]["diff"]
    with pytest.raises(DomainError) as error:
        readonly.approve(
            value.review["publication_id"], value.review["payload_hash"], background=False
        )
    assert error.value.code == "GITHUB_WRITE_CREDENTIAL_REQUIRED"
    assert not value.remote.calls


def test_exact_approval_hash_and_revocation_are_checked_at_dispatch(github_env):
    value = github_env
    with pytest.raises(DomainError) as error:
        value.service.approve(value.review["publication_id"], "0" * 64, background=False)
    assert error.value.code == "APPROVAL_HASH_MISMATCH"
    enqueue(value)
    value.service.revoke(value.env.project_id)
    value.service.dispatcher.tick()
    result = value.service.get(value.review["publication_id"])
    assert result["status"] == "failed" and not value.remote.calls
    assert result["reason_code"] == "GITHUB_WRITE_AUTHORIZATION_REQUIRED"


def test_tested_tree_change_prevents_approval(github_env, monkeypatch):
    value = github_env
    workspace = value.env.product._verified_workspace(value.env.project_id, value.task["task_id"])
    review_args = {
        "task_id": value.task["task_id"],
        "expected_tree": value.task["tree_after"],
        "base_branch": "main",
        "title": "x" * 201,
        "body": "Review the complete patch.",
    }
    with pytest.raises(DomainError) as error:
        value.service.review(value.session["session_id"], **review_args)
    assert error.value.code == "GITHUB_DELIVERY_SCOPE_LIMIT"
    with monkeypatch.context() as patch:
        patch.setattr(value.env.product, "_verified_workspace", lambda *args: workspace)
        patch.setattr(workspace, "read", lambda path: "")
        with pytest.raises(DomainError) as error:
            value.service.review(value.session["session_id"], **{**review_args, "title": "Fix"})
        assert error.value.code == "GITHUB_UNSUPPORTED_CHANGE"
    (workspace.root / "calc.py").write_text("raise Exception('tampered')\n")
    with pytest.raises(DomainError):
        enqueue(value)
    assert not value.remote.calls


def test_http_rejection_retains_attempt_and_requires_fresh_same_payload_approval(github_env):
    value = github_env
    permitted, requests = False, []
    original = value.remote.create_branch

    def reply(request):
        requests.append(request)
        if not permitted:
            return httpx.Response(403, json={"message": "Controlled permission denied"})
        data = json.loads(request.content)
        original("example/mgar-test", data["ref"].removeprefix("refs/heads/"), data["sha"])
        return httpx.Response(201, json={"ref": data["ref"], "object": {"sha": data["sha"]}})

    writer = GitHubRESTWriter(
        "fixture-write-only", client=httpx.Client(transport=httpx.MockTransport(reply))
    )
    value.remote.create_branch = writer.create_branch
    failed = publish(value)
    assert failed["status"] == "failed" and failed["steps"][0]["status"] == "rejected"
    assert len(requests) == 1 and value.service.dispatcher.tick() is None
    permitted = True
    value.service.authorize(
        value.env.project_id, allow_publish=True, allow_merge=False, expected_revision=1
    )
    enqueue(value)
    value.service.dispatcher.tick()
    assert value.service.get(value.review["publication_id"])["status"] == "succeeded"
    assert len(requests) == 2
    with value.env.engine.transaction() as conn:
        attempts = conn.execute(
            "SELECT attempt,status,receipt FROM app.harness_github_step_attempts "
            "WHERE publication_id=%s AND ordinal=0 ORDER BY attempt",
            (value.review["publication_id"],),
        ).fetchall()
    assert [attempt["status"] for attempt in attempts] == ["rejected", "succeeded"]
    assert attempts[0]["receipt"]["definitive_rejection"] is True


@pytest.mark.parametrize("step", ["branch", "commit"])
def test_missing_receipt_can_resume_only_unsent_steps_after_readonly_proof(github_env, step):
    value = github_env
    value.remote.fail_after = step
    result = publish(value)
    assert result["status"] == "outcome_unknown"
    calls = list(value.remote.calls)
    assert value.service.dispatcher.tick() is None and value.remote.calls == calls
    # A reconstructed service shares the same locked Engine and PG journal.
    restored = HarnessGitHubService(
        value.env.product,
        reader=value.remote,
        writer=value.remote,
        merge_reader=value.remote,
        mode="fixture",
    )
    value.remote.fail_after = ""
    restored.resume(result["publication_id"], result["payload_hash"], background=False)
    restored.dispatcher.tick()
    final = restored.get(result["publication_id"])
    assert final["status"] == "succeeded"
    assert value.remote.calls == ["branch", "commit", "pull_request"]


def test_absent_unknown_commit_is_not_retried_or_declared_absent(github_env):
    value = github_env
    value.remote.fail_before = "commit"
    result = publish(value)
    assert result["status"] == "outcome_unknown"
    with pytest.raises(DomainError) as error:
        value.service.resume(result["publication_id"], result["payload_hash"], background=False)
    assert error.value.code == "GITHUB_STEP_UNCONFIRMED"
    assert value.service.get(result["publication_id"])["status"] == "outcome_unknown"
    assert value.remote.calls == ["branch", "commit"]


def test_lost_pr_response_reconciles_exact_tree_without_any_second_write(github_env):
    value = github_env
    value.remote.fail_after = "pull_request"
    result = publish(value)
    assert result["status"] == "outcome_unknown"
    result = value.service.reconcile(result["publication_id"])
    assert result["status"] == "succeeded"
    assert all(step["status"] == "succeeded" for step in result["steps"])
    assert value.remote.calls == ["branch", "commit", "pull_request"]


def test_foreign_tree_addition_does_not_satisfy_reviewed_commit(github_env):
    value = github_env

    def inject(kind):
        if kind == "pull_request":
            raise AssertionError("A foreign tree must not be published as a PR")

    original = value.remote.push_files

    def poisoned(*args):
        sha = original(*args)
        value.remote.commits[sha]["files"]["foreign.py"] = {
            "sha": "0" * 40,
            "type": "blob",
            "mode": "100644",
        }
        return sha

    value.remote.hook = inject
    value.remote.push_files = poisoned
    result = publish(value)
    assert result["status"] == "outcome_unknown" and value.remote.calls == ["branch", "commit"]
    assert value.service.reconcile(result["publication_id"])["status"] == "outcome_unknown"


def test_lost_merge_receipt_reconciles_exact_parents(github_env):
    value = github_env
    result = publish(value)
    value.remote.draft = False
    review = value.service.merge_review(result["publication_id"])
    merge = value.service.merge(
        result["publication_id"],
        pull_number=7,
        head_sha=review["head_sha"],
        base_sha=review["base_sha"],
        review_hash=review["review_hash"],
        confirm=True,
        background=False,
    )
    value.remote.fail_after = "merge"
    value.service.dispatcher.tick()
    assert value.service.get(merge["publication_id"])["status"] == "outcome_unknown"
    assert value.service.reconcile(merge["publication_id"])["status"] == "succeeded"
    assert value.remote.calls.count("merge") == 1


def test_retargeted_pr_same_sha_cannot_write_to_another_branch(github_env):
    value = github_env
    result = publish(value)
    value.remote.draft = False
    value.remote.repository = "other/target"
    assert (
        "BASE_REPOSITORY_CHANGED"
        in value.service.merge_review(result["publication_id"])["blockers"]
    )
    value.remote.repository = "example/mgar-test"
    value.remote.head_repository = "same-owner/another-fork"
    assert (
        "HEAD_REPOSITORY_CHANGED"
        in value.service.merge_review(result["publication_id"])["blockers"]
    )
    value.remote.head_repository = "example/mgar-test"
    review = value.service.merge_review(result["publication_id"])
    action = value.service.merge(
        result["publication_id"],
        pull_number=7,
        head_sha=review["head_sha"],
        base_sha=review["base_sha"],
        review_hash=review["review_hash"],
        confirm=True,
        background=False,
    )
    value.remote.base_branch = "other-branch-at-the-same-sha"
    changed = value.service.merge_review(result["publication_id"])
    assert "BASE_BRANCH_CHANGED" in changed["blockers"] and not changed["can_merge"]
    value.service.dispatcher.tick()
    assert value.service.get(action["publication_id"])["status"] == "failed"
    assert "merge" not in value.remote.calls


def test_remote_base_race_is_recorded_unknown_and_never_retried(github_env):
    value = github_env
    result = publish(value)
    value.remote.draft = False
    review = value.service.merge_review(result["publication_id"])
    action = value.service.merge(
        result["publication_id"],
        pull_number=7,
        head_sha=review["head_sha"],
        base_sha=review["base_sha"],
        review_hash=review["review_hash"],
        confirm=True,
        background=False,
    )

    def concurrent_base_move(kind):
        if kind == "merge":
            value.remote.base = "c" * 40

    value.remote.hook = concurrent_base_move
    value.service.dispatcher.tick()
    assert value.service.get(action["publication_id"])["status"] == "outcome_unknown"
    assert value.service.reconcile(action["publication_id"])["status"] == "outcome_unknown"
    assert value.service.dispatcher.tick() is None and value.remote.calls.count("merge") == 1


def test_final_dispatch_gate_serializes_revocation_without_open_pg_transaction(github_env):
    value = github_env
    entered, released, revoked = threading.Event(), threading.Event(), threading.Event()

    def hook(kind):
        if kind == "branch":
            entered.set()
            assert released.wait(5)

    value.remote.hook = hook
    enqueue(value)
    dispatch = threading.Thread(target=value.service.dispatcher.tick)
    dispatch.start()
    assert entered.wait(5)

    def revoke():
        value.service.revoke(value.env.project_id)
        revoked.set()

    revoker = threading.Thread(target=revoke)
    revoker.start()
    assert not revoked.wait(0.05)
    released.set()
    dispatch.join(10)
    revoker.join(10)
    assert not dispatch.is_alive() and revoked.is_set()
    # Whichever thread reacquires the mutex first, no write occurs after a
    # revocation has committed. The in-flight branch can finish with its receipt.
    assert value.remote.calls[0] == "branch"
    assert value.service.authorization(value.env.project_id)["valid"] is False


def test_fresh_service_fences_expired_claim_without_resending(github_env):
    value = github_env
    enqueue(value)
    claim = value.service.ledger.claim_publication("fixture")
    assert claim is not None
    with value.env.engine.transaction() as conn:
        conn.execute(
            "UPDATE app.harness_github_outbox SET lease_until=now()-interval '1 second' "
            "WHERE publication_id=%s",
            (claim["publication_id"],),
        )
    restored = HarnessGitHubService(
        value.env.product,
        reader=value.remote,
        writer=value.remote,
        merge_reader=value.remote,
        mode="fixture",
    )
    assert restored.get(claim["publication_id"])["status"] == "outcome_unknown"
    assert restored.dispatcher.tick() is None and value.remote.calls == []
    with pytest.raises(DomainError) as error:
        value.service.ledger.publication_result(
            claim["publication_id"], claim["lease_token"], external_id="7"
        )
    assert error.value.code == "FENCED"


def test_expired_ready_and_unsent_rejection_accept_fresh_exact_approval(github_env, monkeypatch):
    value = github_env
    enqueue(value)
    old = value.service.get(value.review["publication_id"])["approval"]
    old["approved_at"] = (datetime.now(UTC) - timedelta(hours=2)).isoformat()
    with value.env.engine.transaction() as conn:
        conn.execute(
            "UPDATE app.harness_github_actions SET approval=%s WHERE publication_id=%s",
            (Jsonb(old), value.review["publication_id"]),
        )
    with pytest.raises(DomainError) as error:
        value.service.dispatch(value.review["publication_id"], value.review["payload_hash"])
    # Changed timestamp is not a forged valid approval: its exact digest also
    # binds the original timestamp. A fresh authenticated approval repairs it.
    assert error.value.code in {"GITHUB_APPROVAL_EXPIRED", "APPROVAL_HASH_MISMATCH"}
    enqueue(value)
    value.service.revoke(value.env.project_id)
    value.service.dispatcher.tick()
    assert value.service.get(value.review["publication_id"])["status"] == "failed"
    assert value.remote.calls == []
    value.service.authorize(
        value.env.project_id, allow_publish=True, allow_merge=False, expected_revision=1
    )
    enqueue(value)
    value.service.dispatcher.tick()
    assert value.service.get(value.review["publication_id"])["status"] == "succeeded"


def test_expiry_after_confirmed_branch_reapproves_only_unsent_steps(github_env):
    value = github_env

    def expire(kind):
        if kind == "branch":
            value.remote.hook = None
            old = value.service.get(value.review["publication_id"])["approval"]
            old["approved_at"] = (datetime.now(UTC) - timedelta(hours=2)).isoformat()
            with value.env.engine.transaction() as conn:
                conn.execute(
                    "UPDATE app.harness_github_actions SET approval=%s WHERE publication_id=%s",
                    (Jsonb(old), value.review["publication_id"]),
                )

    value.remote.hook = expire
    result = publish(value)
    assert result["status"] == "failed" and value.remote.calls == ["branch"]
    assert result["steps"][0]["status"] == "succeeded"
    enqueue(value)
    value.service.dispatcher.tick()
    assert value.service.get(value.review["publication_id"])["status"] == "succeeded"
    assert value.remote.calls == ["branch", "commit", "pull_request"]


def test_default_reader_uses_encrypted_web_token_and_refreshes_without_enabling_writer(product_env):
    env = product_env
    env.settings.allow_external_write = False
    env.settings.github_write_token = None
    env.web.save(AnalysisSettingsInput(github_read_token=SecretStr("fixture-read-first")), env.user)
    service = HarnessGitHubService(env.product)
    assert service._read_token == "fixture-read-first" and service.writer is None
    env.web.save(
        AnalysisSettingsInput(
            github_read_token=SecretStr("fixture-read-second"), expected_revision=1
        ),
        env.user,
    )
    service._refresh_reader()
    assert service._read_token == "fixture-read-second" and service.writer is None
    with env.engine.transaction() as conn:
        encrypted = conn.execute(
            "SELECT encrypted_payload FROM app.web_analysis_settings"
        ).fetchall()
    assert all("fixture-read-" not in row["encrypted_payload"] for row in encrypted)


def test_fixed_import_serializes_same_snapshot_and_never_persists_read_token(
    github_env, monkeypatch
):
    value = github_env
    from mgar_v3.harness import github_delivery

    original = github_delivery.RepositoryWorkspace
    active, peak, guard = 0, 0, threading.Lock()
    observed = []

    class LocalSnapshot(original):
        def __init__(self, *args, **kwargs):
            kwargs["remote"] = str(value.env.source)
            super().__init__(*args, **kwargs)

        def prepare(self):
            nonlocal active, peak
            with guard:
                active += 1
                peak = max(peak, active)
            try:
                observed.append(dict(self._environment))
                result = super().prepare()
                self._run_git(
                    ["remote", "set-url", "origin", "https://github.com/example/mgar-test"]
                )
                return result
            finally:
                with guard:
                    active -= 1

    monkeypatch.setattr(github_delivery, "RepositoryWorkspace", LocalSnapshot)
    value.service._read_token = "fixture-private-read-never-write"
    results, failures = [], []

    def register():
        try:
            results.append(
                value.service.register_project("https://github.com/example/mgar-test", value.base)
            )
        except Exception as exc:
            failures.append(exc)

    workers = [threading.Thread(target=register) for _ in range(2)]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(15)
    assert not failures and len(results) == 2 and peak == 1
    assert results[0]["project_id"] == results[1]["project_id"]
    assert all(result["source_complete"] for result in results)
    site = {"kind": "server_sandbox", "name": "服务器隔离环境"}
    assert all(result["execution_site"] == site for result in results)
    project = next(
        item for item in value.env.harness.list_projects()
        if item["project_id"] == results[0]["project_id"]
    )
    assert project["execution_site"] == site
    assert all("GIT_CONFIG_VALUE_0" in environment for environment in observed)
    config = (
        value.env.harness.registry._sources[results[0]["project_id"]] / ".git/config"
    ).read_text()
    assert "fixture-private" not in config and "Authorization" not in config
    assert "fixture-private" not in json.dumps(results)


@pytest.mark.parametrize(
    "text,expected",
    [
        ("找出仓库里可能存在的错误并提出pr", "bugfix"),
        ("Find bugs and open a PR", "bugfix"),
        ("解释如何创建PR", "inspect"),
        ("只读分析错误并准备PR，不改代码", "inspect"),
        ("解释已有PR", "inspect"),
    ],
)
def test_pr_task_intent_preserves_readonly_requests(text, expected):
    assert infer_intent(text) == expected


def test_timeout_status_is_unknown_and_request_pins_exact_head():
    requests = []

    def reply(request):
        requests.append(request)
        return httpx.Response(408, json={"message": "timeout"})

    writer = GitHubRESTWriter(
        "fixture-token", client=httpx.Client(transport=httpx.MockTransport(reply))
    )
    with pytest.raises(httpx.HTTPStatusError):
        writer.merge("example/repo", 7, "a" * 40)
    assert b'"sha":"' + b"a" * 40 + b'"' in requests[0].content
    assert requests[0].url.host == "api.github.com"


def test_incomplete_remote_inventory_cannot_authorize_a_merge():
    def reply(request):
        if request.url.path.endswith("/pulls/7"):
            body = {
                "head": {"sha": "a" * 40, "repo": {"full_name": "example/repo"}},
                "base": {"sha": "b" * 40, "ref": "main", "repo": {"full_name": "example/repo"}},
                "draft": False,
                "state": "open",
                "merged": False,
                "mergeable": True,
                "mergeable_state": "clean",
            }
        elif request.url.path.endswith("/check-runs"):
            body = {
                "total_count": 2,
                "check_runs": [{"name": "test", "status": "completed", "conclusion": "success"}],
            }
        elif request.url.path.endswith("/status"):
            body = {"total_count": 0, "statuses": []}
        else:
            body = {"permissions": {"push": True}}
        return httpx.Response(200, json=body)

    reader = GitHubRESTReader(client=httpx.Client(transport=httpx.MockTransport(reply)))
    assert not reader.merge_checks("example/repo", 7)["ci_passed"]


def test_rest_pull_identity_normalizes_github_mixed_case_repository():
    def reply(request):
        if request.url.path.endswith("/pulls/7"):
            body = {
                "number": 7,
                "draft": True,
                "title": "Reviewed fix",
                "body": "Reviewed body",
                "head": {
                    "sha": "a" * 40,
                    "ref": "harness/fix",
                    "repo": {"full_name": "Loz888/MAGR-v3"},
                },
                "base": {"sha": "b" * 40, "ref": "main", "repo": {"full_name": "Loz888/MAGR-v3"}},
            }
        else:
            body = [{"number": 7}]
        return httpx.Response(200, json=body)

    reader = GitHubRESTReader(client=httpx.Client(transport=httpx.MockTransport(reply)))
    pull = reader.find_draft("loz888/magr-v3", "harness/fix", "main")
    assert pull is not None and pull.repository == "loz888/magr-v3"
