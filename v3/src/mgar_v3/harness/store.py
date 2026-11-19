"""H11 recovery metadata, owned by the existing single PostgreSQL Engine.

There is no second money ledger here.  Model reservations/results are references
to ``usage_calls`` and are settled by Engine's existing atomic primitive.
Filesystem and provider I/O must happen outside the short transactions below.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from contextlib import AbstractContextManager, nullcontext
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any
from uuid import UUID, uuid4

from psycopg.types.json import Jsonb

from mgar_v3.domain import DomainError, digest
from mgar_v3.harness.contracts import (
    BudgetLimits,
    CapabilityGrant,
    EvaluationProfile,
    EventDraft,
    EventEnvelope,
    ModelRequest,
    ModelResponse,
)
from mgar_v3.harness.events import _public
from mgar_v3.harness.result_artifacts import load_result
from mgar_v3.harness.tenancy import require_membership

if TYPE_CHECKING:
    from mgar_v3.coordination.engine import Connection, Engine

GRAPH_VERSION = "harness-v4-v1"
H12_COST_CAP_MICROS = 12_000_000
H12_CALL_CAP = 1536
Row = dict[str, Any]


def _document(value: Any) -> Row:
    if hasattr(value, "model_dump"):
        return dict(value.model_dump(mode="json"))
    if isinstance(value, Mapping):
        return dict(value)
    raise TypeError("Harness persistence requires a JSON object")


class HarnessStore:
    def __init__(self, engine: Engine) -> None:
        self.engine = engine
        self.artifacts = engine.artifacts

    def create_experiment(self, experiment_id: str, configdict: Row, authorizationdict: Row) -> Row:
        """Freeze one run matrix; all H12 matrices share the same $12 authority."""
        config, authorization = _document(configdict), _document(authorizationdict)
        actor = self.engine.settings.local_actor
        if (
            not experiment_id or config.get("schema") != "harness-h12-experiment-v1"
            or authorization.get("approved_by") != actor
            or authorization.get("allow_paid") is not True
            or authorization.get("max_cost_micros") != H12_COST_CAP_MICROS
            or authorization.get("max_calls", H12_CALL_CAP) != H12_CALL_CAP
        ):
            raise DomainError("EVALUATION_AUTHORIZATION_DENIED", "H12 authorization differs", 403)
        try:
            profiles = [EvaluationProfile.model_validate(item) for item in config["profiles"]]
        except (KeyError, TypeError, ValueError) as exc:
            raise DomainError(
                "EVALUATION_CONFIG_DENIED", "Invalid frozen H12 profiles", 403
            ) from exc
        keys = {(profile.task_id, profile.split, profile.arm) for profile in profiles}
        arms = {profile.arm for profile in profiles}
        task_ids = {split: {profile.task_id for profile in profiles if profile.split == split}
                    for split in ("dev", "holdout")}
        revision = config.get("environment_revision")
        dev_only = revision in {"h12-dev-environment-v3", "h12-dev-environment-v4"}
        expected_splits = ("dev",) if dev_only else ("dev", "holdout")
        if (
            revision not in {
                None, "h12-dev-environment-v3", "h12-dev-environment-v4", "h12-benchmark-v2"
            }
            or any(profile.experiment_id != experiment_id for profile in profiles)
            or len(keys) != len(profiles) or not 2 <= len(arms) <= 3
            or any(len(task_ids[split]) != 12 for split in expected_splits)
            or dev_only and (task_ids["holdout"] or arms != {"candidate", "legacy-v35"})
            or task_ids["dev"] & task_ids["holdout"]
            or any(
                sum(profile.arm == arm for profile in profiles) != 12 * len(expected_splits)
                for arm in arms
            )
        ):
            raise DomainError(
                "EVALUATION_CONFIG_DENIED", "H12 requires a complete frozen matrix", 403
            )
        config["profiles"] = [profile.model_dump(mode="json") for profile in profiles]
        config_hash, authorization_hash = digest(config), digest(authorization)
        # A changed experiment id, arm or retry cannot mint another $12 budget.
        profile_hash = digest({"card": "HV4-H12", "project": self.engine.settings.project_id,
                               "shared_authorization": "user-approved-12-usd-v1"})
        with self.engine.transaction() as conn:
            existing = conn.execute(
                "SELECT * FROM app.harness_experiments WHERE experiment_id=%s FOR UPDATE",
                (experiment_id,),
            ).fetchone()
            if existing:
                if (existing["config_hash"], existing["authorization_hash"]) != (
                    config_hash, authorization_hash
                ) or existing["project_id"] != self.engine.settings.project_id:
                    raise DomainError("IDEMPOTENCY_CONFLICT", "Frozen experiment changed", 409)
                return self._experiment(conn, experiment_id)
            conn.execute(
                "INSERT INTO app.authorization_budgets(profile_hash,limit_micros,call_limit,"
                "budget_scope) VALUES (%s,%s,%s,'harness-h12') ON CONFLICT DO NOTHING",
                (profile_hash, H12_COST_CAP_MICROS, H12_CALL_CAP),
            )
            budget = conn.execute(
                "SELECT * FROM app.authorization_budgets WHERE profile_hash=%s FOR UPDATE",
                (profile_hash,),
            ).fetchone()
            if budget is None or (budget["limit_micros"], budget["call_limit"],
                                  budget["budget_scope"]) != (
                H12_COST_CAP_MICROS, H12_CALL_CAP, "harness-h12"
            ):
                raise DomainError("EVALUATION_AUTHORIZATION_DENIED", "Shared budget differs", 403)
            conn.execute(
                "INSERT INTO app.harness_experiments(experiment_id,project_id,actor,config,"
                "config_hash,approval_document,authorization_hash,profile_hash) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s)",
                (experiment_id, self.engine.settings.project_id, actor, Jsonb(config), config_hash,
                 Jsonb(authorization), authorization_hash, profile_hash),
            )
            return self._experiment(conn, experiment_id)

    def _experiment(self, conn: Connection, experiment_id: str) -> Row:
        experiment = conn.execute(
            "SELECT e.*,b.limit_micros,b.reserved_micros,b.settled_micros,"
            "b.used_calls,b.call_limit "
            "FROM app.harness_experiments e JOIN app.authorization_budgets b USING(profile_hash) "
            "WHERE e.experiment_id=%s", (experiment_id,),
        ).fetchone()
        if experiment is None or experiment["project_id"] != self.engine.settings.project_id:
            raise DomainError("EVALUATION_NOT_FOUND", "Evaluation experiment was not found", 404)
        usage = conn.execute(
            "SELECT coalesce(sum(coalesce((usage->>'input_tokens')::bigint,"
            "(usage->>'prompt_tokens')::bigint,0)),0) AS input_tokens,"
            "coalesce(sum(coalesce((usage->>'output_tokens')::bigint,"
            "(usage->>'completion_tokens')::bigint,0)),0) AS output_tokens,"
            "count(*) FILTER (WHERE status='unknown') AS unknown_calls "
            "FROM app.usage_calls WHERE profile_hash=%s", (experiment["profile_hash"],),
        ).fetchone()
        return {**experiment, "usage": {key: int(value) for key, value in (usage or {}).items()}}

    def get_experiment(self, experiment_id: str) -> Row:
        with self.engine.transaction() as conn:
            return self._experiment(conn, experiment_id)

    @staticmethod
    def _unsealed(row: Row) -> None:
        if row["sealed_tree_hash"] is not None:
            raise DomainError("EVALUATION_SEALED", "Independent judging has sealed this tree", 409)

    @staticmethod
    def _group_known(conn: Connection, row: Row) -> None:
        shared = row["experiment_id"] is not None or row["request"].get("product_session_id")
        if shared and conn.execute(
            "SELECT 1 FROM app.usage_calls u WHERE u.profile_hash=%s AND "
            "(u.status='unknown' OR EXISTS (SELECT 1 FROM app.harness_operations o "
            "WHERE o.model_call_id=u.call_id AND o.state='unknown')) LIMIT 1",
            (row["profile_hash"],),
        ).fetchone():
            raise DomainError(
                "EVALUATION_USAGE_UNKNOWN", "Shared paid outcome needs reconciliation", 409
            )

    def _row(self, conn: Connection, task_id: str, epoch: int | None = None) -> Row:
        task = conn.execute(
            "SELECT * FROM app.harness_tasks WHERE task_id=%s FOR UPDATE", (task_id,)
        ).fetchone()
        if task is None:
            raise DomainError("HARNESS_TASK_NOT_FOUND", "Harness task was not found", 404)
        run = self.engine._run(conn, task["run_id"], epoch)
        if run["graph_version"] != GRAPH_VERSION:
            raise DomainError("HARNESS_SCOPE_DENIED", "Run is not a harness execution", 403)
        return {**run, **task, "epoch": int(run["execution_epoch"])}

    def _guard(self, conn: Connection, task_id: str, epoch: int) -> Row:
        row = self._row(conn, task_id, epoch)
        if row["status"] != "running":
            raise DomainError("FENCED", "Harness worker does not own a running epoch", 409)
        if datetime.now(UTC) >= row["deadline_at"]:
            raise DomainError("DEADLINE_EXCEEDED", "Harness deadline has been reached", 403)
        self._general_scope(task_id, row["request"])
        if row["request"].get("product_session_id"):
            current = conn.execute(
                "SELECT latest_task_id FROM app.harness_sessions WHERE session_id=%s",
                (row["request"]["product_session_id"],),
            ).fetchone()
            if current is None or current["latest_task_id"] != task_id:
                raise DomainError("HARNESS_TURN_SUPERSEDED", "A later turn owns this session", 409)
            authorization = conn.execute(
                "SELECT a.* FROM app.harness_project_authorizations a WHERE project_id=%s "
                "ORDER BY revision DESC LIMIT 1", (row["workspace_id"],),
            ).fetchone()
            if (
                authorization is None or authorization["revoked_at"] is not None
                or authorization["expires_at"] <= datetime.now(UTC)
                or authorization["revision"] != row["request"]["product_authorization_revision"]
                or authorization["profile_hash"] != row["profile_hash"]
            ):
                raise DomainError("HARNESS_AUTHORIZATION_REVOKED", "Project grant was revoked", 403)
        return row

    def _general_scope(self, task_id: str, request: Row) -> None:
        raw = request.get("general_dispatch")
        if raw is None:
            return
        from mgar_v3.general_agent.coding_executor_contract import CodingDispatch

        binding = CodingDispatch.model_validate(raw)
        task = binding.require_current(self.engine)
        if (task_id, task.project_id, task.spec.coding_policy) != (
            binding.harness_task_id, request["project_id"], request["task_kind"]
        ):
            raise DomainError("HARNESS_SCOPE_DENIED", "General Coding binding differs.", 403)

    def _managed_scope(self, conn: Connection, row: Row) -> Row:
        workspace_id = row["workspace_id"]
        managed = conn.execute(
            "SELECT * FROM app.harness_managed_projects WHERE workspace_id=%s",
            (workspace_id,),
        ).fetchone()
        registration_hash = row["registration"]["registration_hash"]
        if managed is None or managed["registration_hash"] != registration_hash:
            raise DomainError("HARNESS_PROJECT_BINDING_CHANGED", "Workspace binding changed", 409)
        request = row["request"]
        membership = request.get("product_membership_id", "")
        if membership:
            require_membership(
                conn, workspace_id, membership, write=bool(row["grant_scope"]["write_paths"]),
            )
        return managed

    def _managed_model_authority(self, conn: Connection, row: Row) -> None:
        self._managed_scope(conn, row)
        approval = conn.execute(
            "SELECT profile_hash,allow_paid,expires_at,revoked_at FROM "
            "app.harness_project_authorizations WHERE project_id=%s "
            "ORDER BY revision DESC LIMIT 1", (row["workspace_id"],),
        ).fetchone()
        if (
            approval is None or approval["profile_hash"] != row["profile_hash"]
            or not approval["allow_paid"] or approval["revoked_at"] is not None
            or approval["expires_at"] <= datetime.now(UTC)
        ):
            raise DomainError("HARNESS_AUTHORIZATION_REVOKED", "项目执行授权已变更。", 403)
        credential = conn.execute(
            "SELECT max(revision) AS revision FROM app.harness_project_credentials "
            "WHERE workspace_id=%s", (row["workspace_id"],),
        ).fetchone()
        if credential is None or credential["revision"] != row["request"]["product_model_revision"]:
            raise DomainError("HARNESS_PROJECT_MODEL_CHANGED", "项目模型凭据已更新。", 409)

    def _managed_capacity(self, conn: Connection, row: Row, *, ready: bool) -> None:
        project = self._managed_scope(conn, row)
        statuses = ["running"] if ready else ["queued", "paused"]
        usage = conn.execute(
            "SELECT count(*) AS total,count(*) FILTER(WHERE t.workspace_id=%s) AS project "
            "FROM app.harness_tasks t JOIN app.runs r USING (run_id) "
            "WHERE t.managed_workspace_id IS NOT NULL AND r.status=ANY(%s)",
            (row["workspace_id"], statuses),
        ).fetchone()
        assert usage is not None
        limit = self.engine.settings.max_concurrent_runs if ready else (
            self.engine.settings.harness_queue_limit
        )
        project_limit = project["max_parallel" if ready else "max_queued"]
        if usage["total"] >= limit or usage["project"] >= project_limit:
            code = "HARNESS_CAPACITY_BUSY" if ready else "HARNESS_QUEUE_FULL"
            raise DomainError(code, "运行槽已满，请等待现有任务释放额度。", 429)

    def check_managed_admission(self, registration: Row, request: Row) -> None:
        with self.engine.transaction() as conn:
            self._managed_capacity(conn, {
                "workspace_id": registration["workspace_id"], "registration": registration,
                "request": request, "grant_scope": {
                    "write_paths": request.get("task_kind") != "inspect",
                },
            }, ready=False)

    def scheduled(self) -> list[Row]:
        with self.engine.transaction() as conn:
            return list(conn.execute(
                "SELECT t.task_id,t.workspace_id,p.max_parallel,r.status "
                "FROM app.harness_tasks t JOIN app.runs r USING (run_id) "
                "JOIN app.harness_managed_projects p ON p.workspace_id=t.managed_workspace_id "
                "WHERE r.status='queued' OR (r.status='paused' "
                "AND t.resume_requested_at IS NOT NULL) "
                "ORDER BY coalesce(t.resume_requested_at,t.created_at),t.task_id "
                "LIMIT %s", (self.engine.settings.harness_queue_limit,),
            ).fetchall())

    def schedule_resume(self, task_id: str) -> Row:
        with self.engine.transaction() as conn:
            row = self._row(conn, task_id)
            if row["managed_workspace_id"] is None or row["status"] != "paused":
                raise DomainError("NOT_RESUMABLE", "Harness task is not paused", 409)
            self._managed_scope(conn, row)
            # Reads and effects with an uncertain outcome are still handled by
            # the original H11 reconciliation, under the worker's workspace lock.
            conn.execute(
                "UPDATE app.harness_tasks SET resume_requested_at="
                "coalesce(resume_requested_at,now()) WHERE task_id=%s",
                (task_id,),
            )
            return self._row(conn, task_id)

    def dispatch_failed(self, task_id: str, code: str) -> None:
        with self.engine.transaction() as conn:
            row = self._row(conn, task_id)
            if row["status"] == "paused":
                conn.execute(
                    "UPDATE app.harness_tasks SET resume_requested_at=NULL WHERE task_id=%s",
                    (task_id,),
                )
                self.engine._event(conn, row["run_id"], "harness_resume_blocked", reason_code=code)
            elif row["status"] in {"queued", "running"}:
                self.engine.fail_run(row["run_id"], code, row["execution_epoch"])
                conn.execute(
                    "UPDATE app.harness_tasks SET public_status='failed',resume_requested_at=NULL "
                    "WHERE task_id=%s", (task_id,),
                )

    def create(
        self,
        task_id: str,
        registration: Any,
        requestdict: Row,
        grantdict: Row,
        limitsdict: Row,
        *,
        evaluation_profile: EvaluationProfile | None = None,
    ) -> Row:
        registration = _document(registration)
        request, grant = _document(requestdict), _document(grantdict)
        limits = BudgetLimits.model_validate(limitsdict)
        if evaluation_profile is not None and not isinstance(evaluation_profile, EvaluationProfile):
            raise DomainError(
                "EVALUATION_PROFILE_DENIED", "Trusted evaluation profile required", 403
            )
        if evaluation_profile is not None:
            grant = CapabilityGrant.model_validate(grant).model_dump(mode="json")
        actor = str(grant.get("actor", ""))
        workspace_id = str(registration["workspace_id"])
        repository = str(registration["repository"])
        if (
            not actor
            or actor != self.engine.settings.local_actor
            or grant.get("workspace_id") != workspace_id
            or grant.get("repository") != repository
            or request.get("project_id", workspace_id) != workspace_id
        ):
            raise DomainError("HARNESS_SCOPE_DENIED", "Task and approval scope differ", 403)
        identity_payload = {
            "task": task_id, "registration": registration, "request": request,
            "grant": grant, "limits": limits.model_dump(mode="json"),
        }
        if evaluation_profile is not None:
            identity_payload["evaluation_profile"] = evaluation_profile.model_dump(mode="json")
        identity = digest(identity_payload)
        run_id, ticket_id, thread_id, mandate_id = uuid4(), uuid4(), uuid4(), uuid4()
        profile = digest({"harness_task": task_id, "identity": identity})
        mode = "live" if request.get("mode") == "live" else "fixture"
        with self.engine.transaction() as conn:
            self._general_scope(task_id, request)
            if evaluation_profile is not None:
                evaluation = evaluation_profile.model_dump(mode="json")
                experiment = self._experiment(conn, evaluation_profile.experiment_id)
                if evaluation not in experiment["config"]["profiles"]:
                    raise DomainError("EVALUATION_PROFILE_DENIED", "Profile is not frozen", 403)
                expected_limits = evaluation_profile.resources.budget_limits()
                commands = tuple(dict.fromkeys(
                    argv[0].rsplit("/", 1)[-1] for argv in (
                        evaluation_profile.public_test_argv,
                        evaluation_profile.public_build_argv,
                        (evaluation_profile.public_probe.argv
                         if evaluation_profile.verification_protocol == "public-probe-v1"
                         and evaluation_profile.public_probe is not None else ()),
                        evaluation_profile.service_argv
                    ) if argv
                )) if evaluation_profile.task_kind != "inspect" else ()
                if (
                    limits != expected_limits
                    or request.get("task_kind") != evaluation_profile.task_kind
                    or str(request.get("objective", "")).strip()
                    != evaluation_profile.objective.strip()
                    or tuple(request.get("trusted_build_argv", ()))
                    != evaluation_profile.public_build_argv
                    or tuple(grant.get("read_paths", ())) != evaluation_profile.read_paths
                    or tuple(grant.get("write_paths", ())) != evaluation_profile.write_paths
                    or tuple(grant["allowed_commands"]) != commands
                    or grant["execute"] != bool(commands) or grant["network"]
                    or grant["max_seconds"] != evaluation_profile.resources.deadline_seconds
                ):
                    raise DomainError(
                        "EVALUATION_PROFILE_DENIED", "Task differs from frozen scope", 403
                    )
                profile = experiment["profile_hash"]
            elif request.get("product_session_id"):
                authorization = conn.execute(
                    "SELECT * FROM app.harness_project_authorizations WHERE project_id=%s "
                    "ORDER BY revision DESC LIMIT 1", (workspace_id,),
                ).fetchone()
                if (
                    authorization is None or authorization["actor"] != actor
                    or authorization["revision"] != request.get("product_authorization_revision")
                    or authorization["registration_hash"] != registration["registration_hash"]
                    or authorization["revoked_at"] is not None
                    or authorization["expires_at"] <= datetime.now(UTC)
                    or not authorization["allow_paid"] or mode != "live"
                    or grant["write_paths"] and not authorization["allow_write"]
                    or limits.max_cost_micros > authorization["max_cost_micros"]
                    or limits.max_model_calls > authorization["max_model_calls"]
                ):
                    raise DomainError("HARNESS_AUTHORIZATION_REVOKED", "Product grant differs", 403)
                profile = authorization["profile_hash"]
            existing = conn.execute(
                "SELECT identity_hash FROM app.harness_tasks WHERE task_id=%s", (task_id,)
            ).fetchone()
            if existing:
                if existing["identity_hash"] != identity:
                    raise DomainError("IDEMPOTENCY_CONFLICT", "Task id has another scope", 409)
                return self._row(conn, task_id)
            product_session = None
            managed_workspace_id = None
            if request.get("product_session_id"):
                product_session = conn.execute(
                    "SELECT * FROM app.harness_sessions WHERE session_id=%s AND actor=%s "
                    "AND project_id=%s FOR UPDATE",
                    (request["product_session_id"], actor, workspace_id),
                ).fetchone()
                if product_session is None:
                    raise DomainError("HARNESS_CONTINUATION_SCOPE", "Session differs", 409)
                if str(product_session["membership_id"] or "") != request.get(
                    "product_membership_id", "",
                ):
                    raise DomainError("HARNESS_CONTINUATION_SCOPE", "Member binding differs", 403)
                if request.get("product_managed"):
                    managed_workspace_id = workspace_id
                    self._managed_capacity(conn, {
                        "workspace_id": workspace_id, "request": request,
                        "registration": registration, "grant_scope": grant,
                    }, ready=False)
                elif product_session["membership_id"] is not None:
                    raise DomainError("HARNESS_CONTINUATION_SCOPE", "Managed binding required", 403)
                if product_session["thread_id"]:
                    thread_id = product_session["thread_id"]
                previous_id = request.get("product_previous_task_id")
                if previous_id:
                    if previous_id != product_session["latest_task_id"]:
                        raise DomainError(
                            "HARNESS_CONTINUATION_SCOPE", "Previous turn differs", 409
                        )
                    previous = self._row(conn, previous_id)
                    self._resolved(conn, previous["run_id"])
                    self.engine._end_ticket(conn, previous, previous["public_status"])
                    self.engine._event(conn, previous["run_id"], "harness_turn_superseded",
                                       task_id=previous_id, next_task_id=task_id)
            if product_session is None or product_session["thread_id"] is None:
                conn.execute(
                    "INSERT INTO app.threads(thread_id,project_id,repository,external_key,"
                    "revision) "
                    "VALUES (%s,%s,%s,%s,1)",
                    (thread_id, self.engine.settings.project_id, repository,
                     "harness-session:" + request["product_session_id"]
                     if product_session else "harness:" + task_id),
                )
                if product_session:
                    conn.execute("UPDATE app.harness_sessions SET thread_id=%s "
                                 "WHERE session_id=%s",
                                 (thread_id, request["product_session_id"]))
            conn.execute(
                "INSERT INTO app.mandates(mandate_id,project_id,actor,scope,policy_version,"
                "execution_mode) VALUES (%s,%s,%s,%s,'harness-v4-v1',%s)",
                (mandate_id, self.engine.settings.project_id, actor,
                 Jsonb({"profile_hash": profile, "grant": grant, "identity_hash": identity}), mode),
            )
            conn.execute(
                "INSERT INTO app.budgets(mandate_id,limit_micros) VALUES (%s,%s)",
                (mandate_id, limits.max_cost_micros),
            )
            if evaluation_profile is None and not request.get("product_session_id"):
                conn.execute(
                    "INSERT INTO app.authorization_budgets(profile_hash,limit_micros,call_limit) "
                    "VALUES (%s,%s,%s)",
                    (profile, max(1, limits.max_cost_micros), limits.max_model_calls),
                )
            conn.execute(
                "INSERT INTO app.tickets(ticket_id,mandate_id,thread_id,status) "
                "VALUES (%s,%s,%s,'open')", (ticket_id, mandate_id, thread_id),
            )
            conn.execute(
                "INSERT INTO app.runs(run_id,ticket_id,attempt_no,graph_version,execution_mode,"
                "status,question,input_revision) VALUES (%s,%s,1,%s,%s,'queued',%s,1)",
                (run_id, ticket_id, GRAPH_VERSION, mode, str(request.get("objective", ""))),
            )
            conn.execute(
                "INSERT INTO app.harness_tasks(task_id,run_id,actor,workspace_id,repository,"
                "registration,request,grant_scope,limits,identity_hash,profile_hash,deadline_at,"
                "experiment_id,evaluation_profile,managed_workspace_id,tree_hash,manifest_hash) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                (task_id, run_id, actor, workspace_id, repository, Jsonb(registration),
                 Jsonb(request), Jsonb(grant), Jsonb(limits.model_dump(mode="json")), identity,
                 profile, datetime.now(UTC) + timedelta(seconds=limits.max_seconds),
                 evaluation_profile.experiment_id if evaluation_profile else None,
                 Jsonb(evaluation_profile.model_dump(mode="json")) if evaluation_profile else None,
                 managed_workspace_id,
                 request["workspace_manifest"]["tree_hash"] if managed_workspace_id else None,
                 request["workspace_manifest"]["manifest_hash"] if managed_workspace_id else None),
            )
            conn.execute(
                "INSERT INTO app.subscriptions(thread_id,run_id,role) VALUES (%s,%s,'primary')",
                (thread_id, run_id),
            )
            self.engine._event(conn, run_id, "harness_accepted", task_id=task_id)
            if request.get("product_session_id"):
                linked = conn.execute(
                    "UPDATE app.harness_session_turns t SET task_id=%s,state='accepted' FROM "
                    "app.harness_sessions s WHERE t.turn_id=%s AND t.session_id=s.session_id "
                    "AND s.session_id=%s AND s.actor=%s AND s.project_id=%s "
                    "AND t.task_id IS NULL AND t.state='preparing' RETURNING t.turn_id",
                    (task_id, request.get("product_turn_id"), request["product_session_id"],
                     actor, workspace_id),
                ).fetchone()
                if linked is None:
                    raise DomainError("HARNESS_CONTINUATION_SCOPE", "Product turn differs", 409)
                conn.execute("UPDATE app.harness_sessions SET latest_task_id=%s,updated_at=now() "
                             "WHERE session_id=%s", (task_id, request["product_session_id"]))
            return self._row(conn, task_id)

    def get(self, task_id: str) -> Row:
        with self.engine.transaction() as conn:
            return self._row(conn, task_id)

    def tasks(self) -> list[Row]:
        with self.engine.transaction() as conn:
            ids = conn.execute(
                "SELECT task_id FROM app.harness_tasks ORDER BY created_at,task_id"
            ).fetchall()
            return [self._row(conn, row["task_id"]) for row in ids]

    def guard(self, task_id: str, epoch: int) -> Row:
        with self.engine.transaction() as conn:
            return self._guard(conn, task_id, epoch)

    def record_live_pause(self, task_id: str, epoch: int, *, paused: bool) -> None:
        """Record the worker's safe point while it still owns the same Run and epoch."""
        with self.engine.transaction() as conn:
            self._guard(conn, task_id, epoch)
            conn.execute(
                "UPDATE app.harness_tasks SET public_status=%s,updated_at=now() WHERE task_id=%s",
                ("paused" if paused else "running", task_id),
            )

    def begin(self, task_id: str) -> Row:
        with self.engine.transaction() as conn:
            row = self._row(conn, task_id)
            if row["status"] != "queued":
                raise DomainError("NOT_RUNNABLE", "Task is not queued", 409)
            self._general_scope(task_id, row["request"])
            if datetime.now(UTC) >= row["deadline_at"]:
                raise DomainError("DEADLINE_EXCEEDED", "Harness deadline has been reached", 403)
            if row["managed_workspace_id"] is not None:
                self._managed_capacity(conn, row, ready=True)
            self.engine.begin_run(row["run_id"])
            if row["managed_workspace_id"] is not None:
                elapsed = datetime.now(UTC) - row["created_at"]
                self.engine._event(
                    conn, row["run_id"], "harness_dispatch_started",
                    queued_ms=max(0, int(elapsed.total_seconds() * 1000)),
                )
            conn.execute(
                "UPDATE app.harness_tasks SET public_status='running',updated_at=now() "
                "WHERE task_id=%s", (task_id,),
            )
            return self._row(conn, task_id)

    def workspace_lock(self, row: Row) -> AbstractContextManager[None]:
        # Import locally: the IO adapter also refers to this store protocol.
        from mgar_v3.harness.recovery_io import workspace_lock

        root = row["request"].get("workspace_root")
        return workspace_lock(Path(root)) if root else nullcontext()

    def pause(self, task_id: str) -> Row:
        with self.workspace_lock(self.get(task_id)), self.engine.transaction() as conn:
            row = self._row(conn, task_id)
            if row["status"] not in {"succeeded", "failed", "cancelled", "paused"}:
                conn.execute(
                    "UPDATE app.runs SET status='paused',execution_epoch=execution_epoch+1,"
                    "reason_code='USER_PAUSED' WHERE run_id=%s", (row["run_id"],),
                )
                conn.execute(
                    "UPDATE app.harness_tasks SET public_status='paused',updated_at=now() "
                    "WHERE task_id=%s", (task_id,),
                )
                self.engine._event(conn, row["run_id"], "harness_paused")
            return self._row(conn, task_id)

    def cancel(self, task_id: str) -> Row:
        with self.workspace_lock(self.get(task_id)), self.engine.transaction() as conn:
            row = self._row(conn, task_id)
            if row["status"] in {"succeeded", "failed", "cancelled"}:
                return row
            self.engine.cancel(row["run_id"])
            conn.execute(
                "UPDATE app.usage_calls u SET status='unknown' WHERE run_id=%s "
                "AND status='reserved' AND EXISTS (SELECT 1 FROM app.harness_operations o "
                "WHERE o.model_call_id=u.call_id AND o.state IN ('dispatched','unknown'))",
                (row["run_id"],),
            )
            conn.execute(
                "UPDATE app.harness_operations SET state='unknown',reason='CANCELLED_IN_FLIGHT',"
                "updated_at=now() WHERE run_id=%s AND state='dispatched' AND NOT read_only",
                (row["run_id"],),
            )
            conn.execute(
                "UPDATE app.harness_tasks SET public_status='cancelled',updated_at=now() "
                "WHERE task_id=%s", (task_id,),
            )
            return self._row(conn, task_id)

    def claim(self, task_id: str) -> Row:
        """Claim this same Run after reconciliation, under the workspace flock."""
        with self.workspace_lock(self.get(task_id)), self.engine.transaction() as conn:
            row = self._row(conn, task_id)
            if row["status"] != "paused":
                raise DomainError("NOT_RESUMABLE", "Harness task is not paused", 409)
            self._general_scope(task_id, row["request"])
            if row["sealed_tree_hash"] is not None:
                raise DomainError(
                    "NOT_RESUMABLE", "Independent evaluation has sealed this tree", 409
                )
            self._resolved(conn, row["run_id"], allow_prepared=True)
            if row["managed_workspace_id"] is not None:
                self._managed_capacity(conn, row, ready=True)
            if datetime.now(UTC) >= row["deadline_at"]:
                raise DomainError("DEADLINE_EXCEEDED", "Harness deadline has been reached", 403)
            conn.execute(
                "UPDATE app.runs SET status='running',execution_epoch=execution_epoch+1,"
                "reason_code='RESUME_FROM_CHECKPOINT',report_hash=NULL,outcome=NULL "
                "WHERE run_id=%s", (row["run_id"],),
            )
            conn.execute(
                "UPDATE app.harness_tasks SET public_status='running',result_hash=NULL,"
                "resume_requested_at=NULL,updated_at=now() "
                "WHERE task_id=%s", (task_id,),
            )
            self.engine._event(conn, row["run_id"], "harness_recovery_claimed")
            return self._row(conn, task_id)

    def _resolved(self, conn: Connection, run_id: UUID, *, allow_prepared: bool = False) -> None:
        pending = conn.execute(
            "SELECT 1 FROM app.harness_operations WHERE run_id=%s "
            "AND (state='unknown' OR (state='dispatched' AND NOT read_only) OR "
            "(state IN ('prepared','dispatched') AND NOT %s)) UNION ALL "
            "SELECT 1 FROM app.usage_calls u WHERE run_id=%s AND (status='unknown' OR "
            "(status='reserved' AND NOT EXISTS (SELECT 1 FROM app.harness_operations o "
            "WHERE o.model_call_id=u.call_id AND o.state='prepared' AND %s)))",
            (run_id, allow_prepared, run_id, allow_prepared),
        ).fetchone()
        if pending:
            raise DomainError("RECOVERY_BLOCKED", "Unknown effects require reconciliation", 409)

    def recover(self) -> dict[str, int]:
        counts = {"paused_harness_runs": 0, "unknown_harness_operations": 0}
        for task in self.tasks():
            if task["task_id"].startswith("h15-"):
                continue  # Runner recovery must first reconcile the delivery ticket.
            if task["status"] not in {"running", "paused"}:
                continue
            with self.workspace_lock(task), self.engine.transaction() as conn:
                row = self._row(conn, task["task_id"])
                if row["status"] == "running":
                    conn.execute(
                        "UPDATE app.runs SET status='paused',execution_epoch=execution_epoch+1,"
                        "reason_code='PROCESS_RESTARTED' WHERE run_id=%s", (row["run_id"],),
                    )
                    counts["paused_harness_runs"] += 1
                conn.execute(
                    "UPDATE app.usage_calls u SET status='unknown' WHERE run_id=%s "
                    "AND status='reserved' AND EXISTS (SELECT 1 FROM app.harness_operations o "
                    "WHERE o.model_call_id=u.call_id AND o.state IN ('dispatched','unknown'))",
                    (row["run_id"],),
                )
                changed = conn.execute(
                    "UPDATE app.harness_operations SET state='unknown',"
                    "reason='PROCESS_RESTARTED',updated_at=now() WHERE run_id=%s "
                    "AND state='dispatched' AND NOT read_only RETURNING operation_id",
                    (row["run_id"],),
                ).fetchall()
                counts["unknown_harness_operations"] += len(changed)
                conn.execute(
                    "UPDATE app.harness_operations SET state='prepared',updated_at=now() "
                    "WHERE run_id=%s AND state='dispatched' AND read_only", (row["run_id"],),
                )
                conn.execute(
                    "UPDATE app.harness_tasks SET public_status='paused',updated_at=now() "
                    "WHERE task_id=%s", (row["task_id"],),
                )
                self.engine._event(conn, row["run_id"], "harness_recovery_required")
        return counts

    def _artifact_size(self, content_hash: str | None) -> int | None:
        return len(self.engine.artifacts.read(content_hash)) if content_hash else None

    @staticmethod
    def _register(conn: Connection, content_hash: str | None, size: int | None) -> None:
        if content_hash is not None and size is not None:
            conn.execute(
                "INSERT INTO app.artifacts(content_hash,byte_count) VALUES (%s,%s) "
                "ON CONFLICT DO NOTHING", (content_hash, size),
            )

    def record_tree(
        self, task_id: str, epoch: int, tree_hash: str, *,
        expected_before: str | None = None, manifest_hash: str | None = None,
    ) -> None:
        with self.engine.transaction() as conn:
            row = self._guard(conn, task_id, epoch)
            if row["sealed_tree_hash"] is not None and tree_hash != row["sealed_tree_hash"]:
                self._unsealed(row)
            if expected_before is not None and row["tree_hash"] != expected_before:
                raise DomainError("WORKSPACE_TREE_STALE", "Recorded tree has changed", 409)
            conn.execute(
                "UPDATE app.harness_tasks SET tree_hash=%s,"
                "manifest_hash=coalesce(%s,manifest_hash),"
                "updated_at=now() WHERE task_id=%s", (tree_hash, manifest_hash, task_id),
            )

    def save_checkpoint(
        self, task_id: str, epoch: int, content_hash: str, revision: int, *,
        tree_hash: str | None = None, inbox_revision: int | None = None,
    ) -> None:
        size = self._artifact_size(content_hash)
        with self.engine.transaction() as conn:
            row = self._guard(conn, task_id, epoch)
            self._unsealed(row)
            if revision < row["checkpoint_revision"] or (
                revision == row["checkpoint_revision"] and row["checkpoint_hash"] not in
                (None, content_hash)
            ):
                raise DomainError("CHECKPOINT_STALE", "Checkpoint revision was replaced", 409)
            self._register(conn, content_hash, size)
            if inbox_revision is not None and not (
                row["checkpoint_inbox_revision"] <= inbox_revision <= row["inbox"]["revision"]
            ):
                raise DomainError("CHECKPOINT_STALE", "Inbox watermark does not match", 409)
            conn.execute(
                "UPDATE app.harness_tasks SET checkpoint_hash=%s,checkpoint_revision=%s,"
                "tree_hash=coalesce(%s,tree_hash),checkpoint_inbox_revision=coalesce(%s,"
                "checkpoint_inbox_revision),updated_at=now() WHERE task_id=%s",
                (content_hash, revision, tree_hash, inbox_revision, task_id),
            )
            self.engine._event(conn, row["run_id"], "harness_checkpoint_saved",
                               content_hash=content_hash, revision=revision, epoch=epoch)

    def emit(self, task_id: str, epoch: int, draft: EventDraft) -> EventEnvelope:
        with self.engine.transaction() as conn:
            row = self._guard(conn, task_id, epoch)
            if draft.run_id != row["run_id"] or draft.execution_epoch != epoch:
                raise DomainError("FENCED", "Event belongs to another execution", 409)
            envelope = EventEnvelope(
                **draft.model_dump(exclude={"payload"}), cursor=0,
                payload=_public(draft.payload),
            )
            event = conn.execute(
                "INSERT INTO app.run_events(run_id,kind,safe_payload) VALUES (%s,%s,%s) "
                "RETURNING event_id,created_at",
                (row["run_id"], draft.kind, Jsonb(envelope.to_run_event_safe_payload())),
            ).fetchone()
            assert event is not None
            return envelope.model_copy(update={"cursor": event["event_id"],
                                               "occurred_at": event["created_at"]})

    def record_stream_disconnect(self, task_id: str, request_id: str, cursor: int) -> None:
        # Observation only. The stream already passed the caller's task authorization.
        with self.engine.transaction() as conn:
            conn.execute("SET LOCAL lock_timeout='400ms'")
            match = conn.execute(
                "SELECT run_id FROM app.harness_tasks WHERE task_id=%s", (task_id,),
            ).fetchone()
            if match is not None:
                self.engine._event(
                    conn, match["run_id"], "harness_stream_disconnected",
                    stream_request_id=request_id, cursor=cursor,
                )

    def events(
        self, task_id: str, after_cursor: int = 0, *, limit: int | None = None,
    ) -> tuple[EventEnvelope, ...]:
        with self.engine.transaction() as conn:
            task = self._row(conn, task_id)
            rows = conn.execute(
                "SELECT * FROM app.run_events WHERE run_id=%s AND event_id>%s "
                "AND safe_payload->>'schema'='harness-event-v1' ORDER BY event_id LIMIT %s",
                (task["run_id"], max(0, after_cursor), limit),
            ).fetchall()
        return tuple(EventEnvelope(
            envelope_id=row["safe_payload"]["envelope_id"], run_id=row["run_id"],
            operation_id=row["safe_payload"]["operation_id"],
            execution_epoch=row["safe_payload"]["execution_epoch"],
            revision=row["safe_payload"]["revision"], cursor=row["event_id"], kind=row["kind"],
            payload=row["safe_payload"]["payload"], occurred_at=row["created_at"],
        ) for row in rows)

    def operation(self, task_id: str, operation_id: str) -> Row | None:
        with self.engine.transaction() as conn:
            row = self._row(conn, task_id)
            return conn.execute(
                "SELECT * FROM app.harness_operations WHERE run_id=%s AND operation_id=%s",
                (row["run_id"], operation_id),
            ).fetchone()

    def operations(self, task_id: str) -> list[Row]:
        with self.engine.transaction() as conn:
            row = self._row(conn, task_id)
            return conn.execute(
                "SELECT * FROM app.harness_operations WHERE run_id=%s ORDER BY created_at",
                (row["run_id"],),
            ).fetchall()

    def start_operation(
        self, task_id: str, epoch: int, operation_id: str, kind: str, request_hash: str, *,
        read_only: bool = False, intent: Row | None = None,
    ) -> Row:
        with self.engine.transaction() as conn:
            row = self._guard(conn, task_id, epoch)
            self._unsealed(row)
            op = conn.execute(
                "SELECT * FROM app.harness_operations WHERE run_id=%s AND operation_id=%s "
                "FOR UPDATE", (row["run_id"], operation_id),
            ).fetchone()
            if op:
                if op["request_hash"] not in (None, request_hash) or (
                    op["kind"] != kind and op["kind"] not in {"tool", "model"}
                ):
                    raise DomainError("IDEMPOTENCY_CONFLICT", "Operation input changed", 409)
                if op["state"] == "unknown" or (
                    op["state"] == "dispatched" and not op["read_only"]
                ):
                    raise DomainError("RECOVERY_BLOCKED", "Operation requires reconciliation", 409)
                if op["state"] == "known":
                    return op
                conn.execute(
                    "UPDATE app.harness_operations SET kind=%s,request_hash=%s,read_only=%s,"
                    "intent=%s,execution_epoch=%s,state='prepared',updated_at=now() "
                    "WHERE reservation_id=%s",
                    (kind, request_hash, read_only, Jsonb(intent or op["intent"]), epoch,
                     op["reservation_id"]),
                )
            else:
                conn.execute(
                    "INSERT INTO app.harness_operations(reservation_id,run_id,operation_id,"
                    "execution_epoch,kind,request_hash,read_only,state,intent) "
                    "VALUES (%s,%s,%s,%s,%s,%s,%s,'prepared',%s)",
                    (uuid4(), row["run_id"], operation_id, epoch, kind, request_hash, read_only,
                     Jsonb(intent or {})),
                )
            result = conn.execute(
                "SELECT * FROM app.harness_operations WHERE run_id=%s AND operation_id=%s",
                (row["run_id"], operation_id),
            ).fetchone()
            assert result is not None
            return result

    @staticmethod
    def _run_token_usage(conn: Connection, run_id: UUID) -> dict[str, int]:
        usage = conn.execute(
            "SELECT coalesce(sum((usage->>'input_tokens')::bigint),0) AS input_tokens,"
            "coalesce(sum((usage->>'output_tokens')::bigint),0) AS output_tokens "
            "FROM app.usage_calls WHERE run_id=%s AND status='known'",
            (run_id,),
        ).fetchone()
        assert usage is not None
        return {key: int(value) for key, value in usage.items()}

    def run_token_usage(self, task_id: str) -> dict[str, int]:
        with self.engine.transaction() as conn:
            return self._run_token_usage(conn, self._row(conn, task_id)["run_id"])

    def dispatch_operation(
        self, task_id: str, epoch: int, operation_id: str, *, intent: Row | None = None,
    ) -> Row:
        with self.engine.transaction() as conn:
            row = self._guard(conn, task_id, epoch)
            self._unsealed(row)
            op = conn.execute(
                "SELECT * FROM app.harness_operations WHERE run_id=%s AND operation_id=%s "
                "FOR UPDATE", (row["run_id"], operation_id),
            ).fetchone()
            if not op or op["state"] != "prepared":
                raise DomainError("RECOVERY_BLOCKED", "Operation is not safely dispatchable", 409)
            if op["budget_kind"] == "model":
                if row["managed_workspace_id"] is not None:
                    self._managed_model_authority(conn, row)
                self._group_known(conn, row)
                if row["request"].get("product_session_id"):
                    self._check_product_model_dispatch(conn, row, op)
            result = conn.execute(
                "UPDATE app.harness_operations SET state='dispatched',intent=%s,"
                "execution_epoch=%s,updated_at=now() WHERE reservation_id=%s RETURNING *",
                (Jsonb(intent if intent is not None else op["intent"]), epoch,
                 op["reservation_id"]),
            ).fetchone()
            assert result is not None
            return result

    def _check_product_model_dispatch(self, conn: Connection, row: Row, op: Row) -> None:
        from mgar_v3.harness.delegation import EXPERT_BUDGET, EXPERT_OPERATION
        from mgar_v3.harness.product_models import product_request_input_bound

        request = row["request"]
        limit = request.get("product_expert_max_tokens", 0)
        if EXPERT_OPERATION in op["operation_id"]:
            if not (
                limit and request.get("product_expert_enabled")
                and self.engine.settings.harness_experts_enabled
            ):
                raise DomainError("HARNESS_EXPERT_DISABLED", "Expert consultation is disabled", 403)
            count = conn.execute(
                "SELECT count(*) AS n FROM app.harness_operations WHERE run_id=%s "
                "AND budget_kind='model' AND operation_id LIKE %s "
                "AND state IN ('known','dispatched','unknown')",
                (row["run_id"], f"%{EXPERT_OPERATION}%"),
            ).fetchone()
            if count and count["n"] >= EXPERT_BUDGET.max_calls_per_revision:
                raise DomainError(
                    "HARNESS_EXPERT_CALL_LIMIT", "Expert consultation limit reached", 429,
                )
        if not limit:
            return
        # The Run row is locked by _guard. Do not overcommit while a request is in flight.
        if conn.execute(
            "SELECT 1 FROM app.harness_operations WHERE run_id=%s AND budget_kind='model' "
            "AND state='dispatched' LIMIT 1", (row["run_id"],),
        ).fetchone():
            raise DomainError("RECOVERY_BLOCKED", "A Run model call is still in flight", 409)
        pending = ModelRequest.model_validate(op["intent"])
        estimate = product_request_input_bound(pending) + pending.max_output_tokens
        known = sum(self._run_token_usage(conn, row["run_id"]).values())
        if known + estimate > limit:
            raise DomainError("HARNESS_TOKEN_BUDGET", "Run has insufficient token budget", 429)

    def known_operation(
        self, task_id: str, epoch: int, operation_id: str, *,
        result_hash: str | None = None, receipt: Row | None = None,
    ) -> Row:
        size = self._artifact_size(result_hash)
        with self.engine.transaction() as conn:
            row = self._guard(conn, task_id, epoch)
            return self._known(conn, row, operation_id, result_hash, size, receipt)

    def _known(
        self, conn: Connection, row: Row, operation_id: str, result_hash: str | None,
        size: int | None, receipt: Row | None,
    ) -> Row:
        op = conn.execute(
            "SELECT * FROM app.harness_operations WHERE run_id=%s AND operation_id=%s "
            "FOR UPDATE", (row["run_id"], operation_id),
        ).fetchone()
        if op is None or op["budget_kind"] == "model":
            raise DomainError("HARNESS_OPERATION_DENIED", "No reconcilable operation", 409)
        if op["state"] == "known":
            if op["result_hash"] != result_hash or op["receipt"] != (receipt or {}):
                raise DomainError("SETTLEMENT_CONFLICT", "Operation receipt changed", 409)
            return op
        self._register(conn, result_hash, size)
        result = conn.execute(
            "UPDATE app.harness_operations SET state='known',result_hash=%s,receipt=%s,"
            "reason='',actual_units=reserved_units,updated_at=now() "
            "WHERE reservation_id=%s RETURNING *",
            (result_hash, Jsonb(receipt or {}), op["reservation_id"]),
        ).fetchone()
        assert result is not None
        return result

    def reconcile_operation(
        self, task_id: str, operation_id: str, *,
        result_hash: str | None = None, receipt: Row | None = None,
    ) -> Row:
        size = self._artifact_size(result_hash)
        with self.engine.transaction() as conn:
            row = self._row(conn, task_id)
            if row["status"] != "paused":
                raise DomainError("NOT_RESUMABLE", "Reconciliation requires a paused task", 409)
            result = self._known(conn, row, operation_id, result_hash, size, receipt)
            self.engine._event(conn, row["run_id"], "harness_operation_reconciled",
                               operation_id=operation_id)
            return result

    def unknown_operation(self, task_id: str, epoch: int, operation_id: str, reason: str) -> Row:
        with self.engine.transaction() as conn:
            row = self._guard(conn, task_id, epoch)
            operation = conn.execute(
                "UPDATE app.harness_operations SET state='unknown',reason=%s,updated_at=now() "
                "WHERE run_id=%s AND operation_id=%s AND state NOT IN ('known','released') "
                "RETURNING *",
                (reason[:500], row["run_id"], operation_id),
            ).fetchone()
            if operation is None:
                operation = conn.execute(
                    "SELECT * FROM app.harness_operations WHERE run_id=%s AND operation_id=%s",
                    (row["run_id"], operation_id),
                ).fetchone()
            if operation is None:
                raise DomainError("CALL_NOT_FOUND", "Operation was not found", 404)
            if operation["state"] == "unknown" and operation["model_call_id"] is not None:
                self.engine.settle_call(operation["model_call_id"], None)
            return operation

    def model_result(self, task_id: str, epoch: int, operation_id: str) -> ModelResponse | None:
        with self.engine.transaction() as conn:
            row = self._guard(conn, task_id, epoch)
            self._unsealed(row)
            call = conn.execute(
                "SELECT * FROM app.usage_calls WHERE run_id=%s AND step_key=%s",
                (row["run_id"], operation_id),
            ).fetchone()
            if call is None:
                return None
            if call["status"] == "reserved":
                operation = conn.execute(
                    "SELECT state FROM app.harness_operations WHERE model_call_id=%s",
                    (call["call_id"],),
                ).fetchone()
                if operation and operation["state"] == "prepared":
                    return None
                raise DomainError("USAGE_UNKNOWN", "Sent model request cannot be resent", 409)
            if call["status"] != "known":
                raise DomainError("USAGE_UNKNOWN", "Unknown model request cannot be resent", 409)
            if call["result"] is None:
                raise DomainError("MODEL_RESULT_UNAVAILABLE", "Known call has no reusable result")
            return ModelResponse.model_validate(call["result"])

    def save_model_result(
        self, task_id: str, epoch: int, operation_id: str, response: ModelResponse,
        *, provider_ms: int | None = None,
    ) -> None:
        with self.engine.transaction() as conn:
            row = self._guard(conn, task_id, epoch)
            op = conn.execute(
                "SELECT * FROM app.harness_operations WHERE run_id=%s AND operation_id=%s "
                "FOR UPDATE", (row["run_id"], operation_id),
            ).fetchone()
            if op is None or op["model_call_id"] is None:
                raise DomainError("CALL_NOT_FOUND", "Model reservation was not found", 404)
            if op["state"] == "known":
                existing = conn.execute(
                    "SELECT result FROM app.usage_calls WHERE call_id=%s", (op["model_call_id"],)
                ).fetchone()
                if existing is None or existing["result"] != response.model_dump(mode="json"):
                    raise DomainError("SETTLEMENT_CONFLICT", "Known model response changed", 409)
                return
            self.engine.settle_call(
                op["model_call_id"], response.usage.cost_micros if response.usage.known else None,
                response.usage.model_dump(mode="json"), response if response.usage.known else None,
            )
            known = conn.execute(
                "SELECT status FROM app.usage_calls WHERE call_id=%s", (op["model_call_id"],)
            ).fetchone()
            state = "known" if known and known["status"] == "known" else "unknown"
            if state == "unknown":
                conn.execute(
                    "UPDATE app.usage_calls SET usage=%s WHERE call_id=%s",
                    (Jsonb(response.usage.model_dump(mode="json")), op["model_call_id"]),
                )
            conn.execute(
                "UPDATE app.harness_operations SET state=%s,actual_units=%s,updated_at=now() "
                "WHERE reservation_id=%s",
                (state, op["reserved_units"] if state == "known" else 0, op["reservation_id"]),
            )
            if provider_ms is not None:
                self.engine._event(
                    conn, row["run_id"], "harness_provider_completed",
                    operation_id=operation_id, status=response.finish_reason,
                    code=response.error_code, duration_ms=provider_ms,
                )

    def seal_evaluation(self, task_id: str, epoch: int, tree_hash: str) -> Row:
        """Fence model/tool activity before mounting any independent judge."""
        with self.engine.transaction() as conn:
            row = self._guard(conn, task_id, epoch)
            if row["evaluation_profile"] is None:
                raise DomainError("EVALUATION_PROFILE_DENIED", "Evaluation task required", 403)
            if row["tree_hash"] != tree_hash:
                raise DomainError("WORKSPACE_TREE_STALE", "Judge tree differs from the ledger", 409)
            if row["sealed_tree_hash"] is not None:
                if row["sealed_tree_hash"] != tree_hash:
                    raise DomainError("WORKSPACE_TREE_STALE", "Sealed tree changed", 409)
                return row
            self._resolved(conn, row["run_id"])
            conn.execute(
                "UPDATE app.harness_tasks SET sealed_tree_hash=%s,sealed_at=now(),updated_at=now() "
                "WHERE task_id=%s", (tree_hash, task_id),
            )
            self.engine._event(
                conn, row["run_id"], "harness_evaluation_sealed", tree_hash=tree_hash
            )
            return self._row(conn, task_id)

    def finish(
        self, task_id: str, epoch: int, status: str, result_hash: str, expected_tree: str,
    ) -> Row:
        size = self._artifact_size(result_hash)
        # Read CAS outside the Engine transaction. Evaluation completion must
        # carry the independent verdict for this exact sealed candidate tree.
        completion: Any = None
        if status == "completed":
            try:
                raw_completion = self.artifacts.read(result_hash)
                completion = json.loads(raw_completion)
            except (ValueError, UnicodeDecodeError):
                pass
            else:
                if isinstance(completion, dict) and "__harness_result_cas__" in completion:
                    selected = self.get(task_id)
                    completion = load_result(
                        self.artifacts, raw_completion, task_id, str(selected["run_id"])
                    )
        with self.engine.transaction() as conn:
            row = self._guard(conn, task_id, epoch)
            if row["tree_hash"] != expected_tree:
                raise DomainError("WORKSPACE_TREE_STALE", "Final tree differs from the ledger", 409)
            if status == "completed":
                if (
                    row["evaluation_profile"] is not None
                    and row["sealed_tree_hash"] != expected_tree
                ):
                    raise DomainError(
                        "EVALUATION_NOT_SEALED", "Independent judge seal required", 409
                    )
                if row["evaluation_profile"] is not None:
                    gate = (
                        completion.get("independent_gate") if isinstance(completion, dict) else None
                    )
                    if (
                        not isinstance(completion, dict)
                        or completion.get("task_id") != task_id
                        or completion.get("run_id") != str(row["run_id"])
                        or completion.get("evaluation_profile") != row["evaluation_profile"]
                        or completion.get("status") != "completed"
                        or completion.get("candidate_status") != "completed"
                        or completion.get("tree_after") != expected_tree
                        or not isinstance(gate, dict)
                        or gate.get("status") != "PASS"
                        or gate.get("tree_hash") != expected_tree
                    ):
                        raise DomainError(
                            "EVALUATION_JUDGE_REQUIRED",
                            "Independent judge receipt must pass for this sealed tree",
                            409,
                        )
                if any(
                    message.get("kind") in {"instruction", "answer"}
                    and (
                        message.get("status") == "received"
                        or (
                            message.get("status") == "applied"
                            and message.get("applied_revision", 0)
                            > row["checkpoint_inbox_revision"]
                        )
                    )
                    for message in row["inbox"].get("messages", [])
                ):
                    raise DomainError(
                        "HARNESS_INBOX_PENDING",
                        "Pending user input must be checkpointed before completion",
                        409,
                    )
                self._resolved(conn, row["run_id"])
            self._register(conn, result_hash, size)
            run_status = (
                "succeeded" if status == "completed" else "cancelled" if status == "cancelled"
                else "paused" if status in {"paused", "unknown", "blocked", "incomplete"}
                else "failed"
            )
            conn.execute(
                "UPDATE app.runs SET status=%s,outcome=%s,report_hash=%s WHERE run_id=%s",
                (run_status, status, result_hash, row["run_id"]),
            )
            conn.execute(
                "UPDATE app.harness_tasks SET public_status=%s,result_hash=%s,updated_at=now() "
                "WHERE task_id=%s", (status, result_hash, task_id),
            )
            if run_status in {"succeeded", "failed", "cancelled"}:
                self.engine._end_ticket(conn, row, status)
            self.engine._event(conn, row["run_id"], "harness_finished", status=status,
                               tree_hash=expected_tree, result_hash=result_hash)
            return self._row(conn, task_id)
