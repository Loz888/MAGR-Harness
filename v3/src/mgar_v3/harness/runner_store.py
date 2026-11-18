"""Device admissions and delivery leases under the existing Engine connection."""

from __future__ import annotations

import hashlib
import json
import re
import secrets
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Literal
from uuid import UUID, uuid4

from psycopg.types.json import Jsonb

from mgar_v3.coordination.engine import Connection, Row
from mgar_v3.domain import DomainError, digest
from mgar_v3.harness.contracts import BudgetLimits, EventDraft
from mgar_v3.harness.project_commands import parse_tests
from mgar_v3.harness.runner_contract import (
    CONTACT_SECONDS,
    LEASE_SECONDS,
    Claim,
    Finish,
    Pairing,
    Start,
    Ticket,
    WorkspaceSpec,
)

if TYPE_CHECKING:
    from mgar_v3.coordination.engine import Engine

_PAIR_SECONDS = 300
_JOB_SECONDS = 1800
_SECRET = re.compile(r"^[A-Za-z0-9_-]{43}$")
_ACTIVE_PHASES = ("claimed", "running")
_FINISHED_RUNS = {"succeeded", "failed", "cancelled"}
_JOB_QUERY = (
    "SELECT j.*,r.run_id,r.status AS run_status,r.reason_code,r.execution_epoch,h.public_status,"
    "h.deadline_at,h.tree_hash,h.manifest_hash,h.request,h.result_hash,h.created_at,"
    "d.name AS device_name,d.online,d.revoked_at,w.name AS workspace_name,"
    "w.project_kind,w.plan_hash FROM app.harness_runner_jobs j "
    "JOIN app.harness_tasks h ON h.task_id=j.task_id JOIN app.runs r USING(run_id) "
    "JOIN app.harness_runner_devices d USING(device_id) "
    "JOIN app.harness_runner_workspaces w ON w.workspace_id=j.workspace_id "
)
_CLAIM_QUERY = (
    "SELECT c.*,h.run_id,h.deadline_at,h.tree_hash,h.manifest_hash,r.status AS run_status,"
    "r.execution_epoch AS run_epoch,r.reason_code,d.revoked_at,j.workspace_id,"
    "w.name AS workspace_name,w.project_kind,w.plan_hash "
    "FROM app.harness_runner_claims c JOIN app.harness_runner_jobs j USING(task_id,device_id) "
    "JOIN app.harness_tasks h USING(task_id) JOIN app.runs r USING(run_id) "
    "JOIN app.harness_runner_devices d USING(device_id) "
    "JOIN app.harness_runner_workspaces w ON w.workspace_id=j.workspace_id "
)


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _fenced() -> DomainError:
    return DomainError("RUNNER_FENCED", "The device or execution ticket is no longer current", 409)


class RunnerStore:
    def __init__(self, engine: Engine) -> None:
        self.engine = engine
        self.actor = engine.settings.local_actor

    def _event(
        self,
        conn: Connection,
        kind: str,
        device_id: UUID | None = None,
        task_id: str | None = None,
        **payload: object,
    ) -> None:
        conn.execute(
            "INSERT INTO app.harness_runner_events(device_id,task_id,kind,payload) "
            "VALUES(%s,%s,%s,%s)",
            (device_id, task_id, kind, Jsonb(payload)),
        )

    def issue_pairing(self) -> Row:
        code = secrets.token_urlsafe(32)
        expires = datetime.now(UTC) + timedelta(seconds=_PAIR_SECONDS)
        with self.engine.transaction() as conn:
            conn.execute(
                "INSERT INTO app.harness_runner_pairings(code_hash,actor,expires_at) "
                "VALUES(%s,%s,%s)",
                (_hash(code), self.actor, expires),
            )
            self._event(conn, "pairing_issued")
        return {"code": code, "expires_at": expires}

    def pair(self, body: Pairing) -> Row:
        with self.engine.transaction() as conn:
            key = _hash(body.code)
            pairing = conn.execute(
                "SELECT * FROM app.harness_runner_pairings WHERE code_hash=%s AND actor=%s "
                "AND expires_at>clock_timestamp() FOR UPDATE",
                (key, self.actor),
            ).fetchone()
            if pairing is None:
                raise DomainError(
                    "RUNNER_PAIRING_INVALID", "Pairing code is expired or invalid", 403
                )
            credential = _hash(body.credential)
            if pairing["device_id"] is not None:
                replay = conn.execute(
                    "SELECT 1 FROM app.harness_runner_devices WHERE device_id=%s "
                    "AND credential_hash=%s AND name=%s AND revoked_at IS NULL",
                    (pairing["device_id"], credential, body.name),
                ).fetchone()
                if pairing["device_id"] != body.device_id or replay is None:
                    raise DomainError(
                        "RUNNER_PAIRING_USED", "The pairing code was already used", 409
                    )
            else:
                inserted = conn.execute(
                    "INSERT INTO app.harness_runner_devices(device_id,actor,name,credential_hash) "
                    "VALUES(%s,%s,%s,%s) ON CONFLICT DO NOTHING RETURNING device_id",
                    (body.device_id, self.actor, body.name, credential),
                ).fetchone()
                if inserted is None:
                    raise DomainError(
                        "RUNNER_DEVICE_CONFLICT", "The device identity is in use", 409
                    )
                conn.execute(
                    "UPDATE app.harness_runner_pairings SET device_id=%s WHERE code_hash=%s",
                    (body.device_id, key),
                )
                self._event(conn, "device_paired", body.device_id)
        return {"device_id": body.device_id, "name": body.name}

    def _device(self, conn: Connection, secret: str) -> Row:
        row = None
        if _SECRET.fullmatch(secret):
            row = conn.execute(
                "SELECT * FROM app.harness_runner_devices WHERE credential_hash=%s "
                "AND actor=%s AND revoked_at IS NULL",
                (_hash(secret), self.actor),
            ).fetchone()
        if row is None:
            raise DomainError(
                "RUNNER_DEVICE_AUTH", "A current Runner device credential is required", 401
            )
        return row

    def _touch(self, conn: Connection, device: Row, now: datetime) -> None:
        if (
            not device["online"]
            or (now - device["last_seen_at"]).total_seconds() >= CONTACT_SECONDS
        ):
            if device["online"]:
                self._event(conn, "device_offline", device["device_id"])
            self._event(conn, "device_reconnected", device["device_id"])
        conn.execute(
            "UPDATE app.harness_runner_devices SET last_seen_at=%s,online=true WHERE device_id=%s",
            (now, device["device_id"]),
        )

    def devices(self) -> list[Row]:
        with self.engine.transaction() as conn:
            self._sweep(conn, datetime.now(UTC))
            return conn.execute(
                "SELECT device_id,name,online,last_seen_at,revoked_at,created_at "
                "FROM app.harness_runner_devices WHERE actor=%s ORDER BY created_at,device_id",
                (self.actor,),
            ).fetchall()

    def revoke(self, device_id: UUID) -> Row:
        with self.engine.transaction() as conn:
            device = conn.execute(
                "SELECT * FROM app.harness_runner_devices WHERE device_id=%s AND actor=%s",
                (device_id, self.actor),
            ).fetchone()
            if device is None:
                raise DomainError("RUNNER_DEVICE_UNKNOWN", "Runner device was not found", 404)
            if device["revoked_at"] is None:
                conn.execute(
                    "UPDATE app.harness_runner_devices SET revoked_at=now(),online=false "
                    "WHERE device_id=%s",
                    (device_id,),
                )
                self._event(conn, "device_revoked", device_id)
                for row in conn.execute(
                    _JOB_QUERY
                    + "WHERE j.device_id=%s AND r.status NOT IN ('succeeded','failed','cancelled')",
                    (device_id,),
                ).fetchall():
                    self._stop(conn, row, cancel=True, reason="RUNNER_DEVICE_REVOKED")
        return {"device_id": device_id, "revoked": True}

    def attach(self, secret: str, spec: WorkspaceSpec) -> Row:
        with self.engine.transaction() as conn:
            device = self._device(conn, secret)
            inserted = conn.execute(
                "INSERT INTO app.harness_runner_workspaces"
                "(workspace_id,device_id,name,project_kind,plan_hash) "
                "VALUES(%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING RETURNING workspace_id",
                (
                    spec.workspace_id,
                    device["device_id"],
                    spec.name,
                    spec.project_kind,
                    spec.plan_hash,
                ),
            ).fetchone()
            if inserted is None:
                existing = conn.execute(
                    "SELECT 1 FROM app.harness_runner_workspaces WHERE workspace_id=%s "
                    "AND device_id=%s AND name=%s AND project_kind=%s AND plan_hash=%s",
                    (
                        spec.workspace_id,
                        device["device_id"],
                        spec.name,
                        spec.project_kind,
                        spec.plan_hash,
                    ),
                ).fetchone()
                if existing is None:
                    raise DomainError("RUNNER_WORKSPACE_CONFLICT", "Workspace binding differs", 409)
            else:
                self._event(
                    conn,
                    "workspace_attached",
                    device["device_id"],
                    workspace_id=str(spec.workspace_id),
                )
            self._touch(conn, device, datetime.now(UTC))
        return {"workspace_id": spec.workspace_id, "device_id": device["device_id"]}

    def workspaces(self) -> list[Row]:
        with self.engine.transaction() as conn:
            self._sweep(conn, datetime.now(UTC))
            return conn.execute(
                "SELECT w.workspace_id,w.name,w.project_kind,d.device_id,d.name AS device_name,"
                "d.online FROM app.harness_runner_workspaces w "
                "JOIN app.harness_runner_devices d USING(device_id) "
                "WHERE d.actor=%s AND d.revoked_at IS NULL ORDER BY w.created_at,w.workspace_id",
                (self.actor,),
            ).fetchall()

    def enqueue(self, workspace_id: UUID, client_id: UUID) -> Row:
        with self.engine.transaction() as conn:
            replay = conn.execute(
                "SELECT task_id,workspace_id FROM app.harness_runner_jobs WHERE client_id=%s",
                (client_id,),
            ).fetchone()
            if replay is not None:
                if replay["workspace_id"] != workspace_id:
                    raise DomainError(
                        "IDEMPOTENCY_CONFLICT", "Job key is bound to another workspace", 409
                    )
                return self._public(self._job(conn, replay["task_id"]))
            source = conn.execute(
                "SELECT w.*,d.name AS device_name FROM app.harness_runner_workspaces w "
                "JOIN app.harness_runner_devices d USING(device_id) "
                "WHERE w.workspace_id=%s AND d.actor=%s AND d.revoked_at IS NULL",
                (workspace_id, self.actor),
            ).fetchone()
            if source is None:
                raise DomainError(
                    "RUNNER_WORKSPACE_UNKNOWN", "Runner workspace is unavailable", 404
                )
            task_id = "h15-" + uuid4().hex[:12]
            repository = "runner/" + workspace_id.hex
            identity = {
                "workspace_id": str(workspace_id),
                "repository": repository,
                "plan_hash": source["plan_hash"],
                "device_id": str(source["device_id"]),
            }
            site = {
                "kind": "paired_runner",
                "name": source["device_name"],
                "device_id": str(source["device_id"]),
            }
            self.engine.harness.create(
                task_id,
                identity,
                {
                    "project_id": str(workspace_id),
                    "objective": "Run the device's fixed project tests",
                    "execution_site": site,
                },
                {
                    "actor": self.actor,
                    "workspace_id": str(workspace_id),
                    "repository": repository,
                    "read_paths": [""],
                    "write_paths": [],
                    "network": False,
                },
                BudgetLimits(
                    max_cost_micros=0, max_model_calls=1, max_tool_calls=1, max_seconds=_JOB_SECONDS
                ).model_dump(mode="json"),
            )
            conn.execute(
                "INSERT INTO app.harness_runner_jobs(task_id,device_id,workspace_id,client_id) "
                "VALUES(%s,%s,%s,%s)",
                (task_id, source["device_id"], workspace_id, client_id),
            )
            self._event(conn, "job_queued", source["device_id"], task_id)
            return self._public(self._job(conn, task_id))

    def _job(self, conn: Connection, task_id: str) -> Row:
        row = conn.execute(
            _JOB_QUERY + "WHERE j.task_id=%s AND d.actor=%s",
            (task_id, self.actor),
        ).fetchone()
        if row is None:
            raise DomainError("RUNNER_JOB_UNKNOWN", "Runner job was not found", 404)
        return row

    @staticmethod
    def _public(row: Row) -> Row:
        return {
            "task_id": row["task_id"],
            "run_id": row["run_id"],
            "workspace_id": row["workspace_id"],
            "workspace_name": row["workspace_name"],
            "execution_site": row["request"]["execution_site"],
            "execution_epoch": row["execution_epoch"],
            "status": row["public_status"],
            "reason": row["reason_code"],
            "waiting_for_device": row["dispatchable"],
            "device_online": bool(row["online"]),
            "result_hash": row["result_hash"],
            "created_at": row["created_at"],
        }

    def jobs(self) -> list[Row]:
        with self.engine.transaction() as conn:
            self._sweep(conn, datetime.now(UTC))
            rows = conn.execute(
                _JOB_QUERY + "WHERE d.actor=%s ORDER BY h.created_at DESC,j.task_id LIMIT 100",
                (self.actor,),
            ).fetchall()
        return [self._public(row) for row in rows]

    def job(self, task_id: str) -> Row:
        with self.engine.transaction() as conn:
            self._sweep(conn, datetime.now(UTC))
            return self._public(self._job(conn, task_id))

    def events(self, after: int) -> list[Row]:
        with self.engine.transaction() as conn:
            self._sweep(conn, datetime.now(UTC))
            return conn.execute(
                "SELECT event_id,device_id,task_id,kind,payload,created_at "
                "FROM app.harness_runner_events WHERE event_id>%s ORDER BY event_id LIMIT 100",
                (after,),
            ).fetchall()

    def _claim_view(self, row: Row, now: datetime) -> Claim:
        return Claim.model_validate(
            {
                "task_id": row["task_id"],
                "run_id": row["run_id"],
                "execution_epoch": row["execution_epoch"],
                "phase": row["phase"],
                "lease_ms": int((row["expires_at"] - now).total_seconds() * 1000),
                "device_id": row["device_id"],
                "workspace": {
                    "workspace_id": row["workspace_id"],
                    "name": row["workspace_name"],
                    "plan_hash": row["plan_hash"],
                    "project_kind": row["project_kind"],
                },
            }
        )

    def claim(self, secret: str, ticket: str) -> Claim | None:
        with self.engine.transaction() as conn:
            device = self._device(conn, secret)
            now = datetime.now(UTC)
            self._touch(conn, device, now)
            self._sweep(conn, now)
            device_id = device["device_id"]
            key = _hash(ticket)
            seen = conn.execute(
                _CLAIM_QUERY + "WHERE c.device_id=%s AND c.ticket_hash=%s",
                (device_id, key),
            ).fetchone()
            if seen is not None:
                self._event(
                    conn,
                    "claim_replayed",
                    device_id,
                    seen["task_id"],
                    execution_epoch=seen["execution_epoch"],
                )
                if seen["phase"] in _ACTIVE_PHASES and seen["expires_at"] > now + timedelta(
                    milliseconds=1
                ):
                    return self._claim_view(seen, now)
                return None
            active = conn.execute(
                "SELECT task_id FROM app.harness_runner_claims WHERE device_id=%s "
                "AND phase IN ('claimed','running')",
                (device_id,),
            ).fetchone()
            if active is not None:
                self._event(conn, "claim_busy", device_id, active["task_id"])
                return None
            job = conn.execute(
                _JOB_QUERY + "WHERE j.device_id=%s AND j.dispatchable "
                "AND r.status IN ('queued','paused') AND h.deadline_at>%s "
                "ORDER BY h.created_at,j.task_id LIMIT 1",
                (device_id, now + timedelta(milliseconds=1)),
            ).fetchone()
            if job is None:
                return None
            task_id = job["task_id"]
            bound = (
                self.engine.harness.begin(task_id)
                if job["run_status"] == "queued"
                else self.engine.harness.claim(task_id)
            )
            epoch = bound["execution_epoch"]
            expires = min(job["deadline_at"], now + timedelta(seconds=LEASE_SECONDS))
            conn.execute(
                "UPDATE app.harness_runner_jobs SET dispatchable=false WHERE task_id=%s",
                (task_id,),
            )
            conn.execute(
                "INSERT INTO app.harness_runner_claims"
                "(task_id,device_id,execution_epoch,ticket_hash,phase,expires_at) "
                "VALUES(%s,%s,%s,%s,'claimed',%s)",
                (task_id, device_id, epoch, key, expires),
            )
            self._event(conn, "job_claimed", device_id, task_id, execution_epoch=epoch)
            return self._claim_view(
                {**job, "execution_epoch": epoch, "phase": "claimed", "expires_at": expires},
                now,
            )

    def _lease(self, conn: Connection, device: Row, task_id: str, ticket: Ticket) -> Row:
        row = conn.execute(
            _CLAIM_QUERY + "WHERE c.task_id=%s AND c.device_id=%s AND c.execution_epoch=%s "
            "AND c.ticket_hash=%s",
            (task_id, device["device_id"], ticket.execution_epoch, _hash(ticket.ticket)),
        ).fetchone()
        if row is None:
            raise _fenced()
        return row

    def _validate(self, lease: Row, now: datetime) -> None:
        if (
            lease["phase"] not in _ACTIVE_PHASES
            or lease["expires_at"] <= now
            or lease["deadline_at"] <= now + timedelta(milliseconds=1)
            or lease["run_epoch"] != lease["execution_epoch"]
            or lease["run_status"] != "running"
        ):
            raise _fenced()

    def _renew(self, conn: Connection, device: Row, lease: Row, now: datetime) -> Claim:
        expires = min(lease["deadline_at"], now + timedelta(seconds=LEASE_SECONDS))
        conn.execute(
            "UPDATE app.harness_runner_claims SET expires_at=%s "
            "WHERE task_id=%s AND execution_epoch=%s",
            (expires, lease["task_id"], lease["execution_epoch"]),
        )
        self._touch(conn, device, now)
        return self._claim_view({**lease, "expires_at": expires}, now)

    def heartbeat(self, secret: str, task_id: str, ticket: Ticket) -> Claim:
        with self.engine.transaction() as conn:
            device = self._device(conn, secret)
            lease = self._lease(conn, device, task_id, ticket)
            now = datetime.now(UTC)
            self._validate(lease, now)
            return self._renew(conn, device, lease, now)

    def start(self, secret: str, task_id: str, ticket: Start) -> Claim:
        with self.engine.transaction() as conn:
            device = self._device(conn, secret)
            lease = self._lease(conn, device, task_id, ticket)
            now = datetime.now(UTC)
            self._validate(lease, now)
            if lease["phase"] == "claimed":
                self.engine.harness.record_tree(
                    task_id,
                    ticket.execution_epoch,
                    ticket.tree_hash,
                    manifest_hash=ticket.manifest_hash,
                )
                conn.execute(
                    "UPDATE app.harness_runner_claims SET phase='running',started_at=%s "
                    "WHERE task_id=%s AND execution_epoch=%s",
                    (now, task_id, ticket.execution_epoch),
                )
                self._event(
                    conn,
                    "job_started",
                    device["device_id"],
                    task_id,
                    execution_epoch=ticket.execution_epoch,
                )
                self._run_event(lease, "runner_started", tree_hash=ticket.tree_hash)
            elif (lease["tree_hash"], lease["manifest_hash"]) != (
                ticket.tree_hash,
                ticket.manifest_hash,
            ):
                raise DomainError("RUNNER_TREE_MISMATCH", "The dispatched workspace differs", 409)
            return self._renew(conn, device, {**lease, "phase": "running"}, now)

    def _run_event(self, lease: Row, kind: str, **payload: object) -> None:
        self.engine.harness.emit(
            lease["task_id"],
            lease["execution_epoch"],
            EventDraft(
                run_id=lease["run_id"],
                execution_epoch=lease["execution_epoch"],
                operation_id=f"{lease['task_id']}:{lease['execution_epoch']}",
                revision=0,
                kind=kind,
                payload=payload,
            ),
        )

    def finish(self, secret: str, task_id: str, ticket: Finish) -> Row:
        receipt = digest(ticket.model_dump(mode="json", exclude={"ticket"}))
        with self.engine.transaction() as conn:
            device = self._device(conn, secret)
            lease = self._lease(conn, device, task_id, ticket)
            job = self._job(conn, task_id)
            if lease["phase"] == "finished":
                if lease["receipt_hash"] != receipt:
                    raise DomainError("RUNNER_RECEIPT_CONFLICT", "Result was already recorded", 409)
                return self._public(job)
            now = datetime.now(UTC)
            self._validate(lease, now)
            if lease["phase"] == "claimed" and ticket.state == "rejected":
                self._run_event(lease, "runner_setup_failed", reason=ticket.failure)
                assert ticket.failure is not None
                self.engine.fail_run(lease["run_id"], ticket.failure, ticket.execution_epoch)
                conn.execute(
                    "UPDATE app.harness_tasks SET public_status='failed',updated_at=now() "
                    "WHERE task_id=%s",
                    (task_id,),
                )
            elif lease["phase"] == "running" and ticket.tree_hash == lease["tree_hash"]:
                self._store_receipt(lease, job, ticket)
            else:
                raise DomainError("RUNNER_TREE_MISMATCH", "No dispatch matches this receipt", 409)
            conn.execute(
                "UPDATE app.harness_runner_claims SET phase='finished',receipt_hash=%s "
                "WHERE task_id=%s AND execution_epoch=%s",
                (receipt, task_id, ticket.execution_epoch),
            )
            self._event(
                conn,
                "job_finished",
                device["device_id"],
                task_id,
                execution_epoch=ticket.execution_epoch,
            )
            self._touch(conn, device, now)
            return self._public(self._job(conn, task_id))

    def _store_receipt(self, lease: Row, job: Row, ticket: Finish) -> None:
        assert ticket.tree_hash is not None
        if ticket.state == "exited" and not ticket.truncated:
            verdict, run, failed, skipped, detail = parse_tests(
                lease["project_kind"],
                ticket.output,
                ticket.exit_code,
            )
        else:
            verdict, run, failed, skipped, detail = (
                "unverified",
                0,
                0,
                0,
                "The Runner did not return a complete test log",
            )
        status = "completed" if verdict == "passed" else "failed"
        result = {
            "task_id": lease["task_id"],
            "run_id": str(lease["run_id"]),
            "status": status,
            "reason": "" if status == "completed" else "RUNNER_TEST_NOT_PASSED",
            "execution_epoch": ticket.execution_epoch,
            "execution_site": job["request"]["execution_site"],
            "tree_before": ticket.tree_hash,
            "tree_after": ticket.tree_hash,
            "summary": detail,
            "output": ticket.output,
            "log_truncated": ticket.truncated,
            "process_state": ticket.state,
            "exit_code": ticket.exit_code,
            "tests": {"status": verdict, "run": run, "failed": failed, "skipped": skipped},
            "model_calls": 0,
            "settled_cost_micros": 0,
        }
        content_hash = self.engine.artifacts.put(json.dumps(result, ensure_ascii=False).encode())
        self._run_event(lease, "runner_result", status=status, tree_hash=ticket.tree_hash)
        self.engine.harness.finish(
            lease["task_id"], ticket.execution_epoch, status, content_hash, ticket.tree_hash
        )

    def control(self, task_id: str, action: Literal["cancel", "pause", "resume"]) -> Row:
        with self.engine.transaction() as conn:
            self._sweep(conn, datetime.now(UTC))
            row = self._job(conn, task_id)
            if row["run_status"] in _FINISHED_RUNS:
                if action != "cancel":
                    raise DomainError("RUNNER_JOB_FINISHED", "Runner job already finished", 409)
            elif action == "resume":
                started = conn.execute(
                    "SELECT 1 FROM app.harness_runner_claims "
                    "WHERE task_id=%s AND started_at IS NOT NULL",
                    (task_id,),
                ).fetchone()
                if (
                    row["run_status"] != "paused"
                    or row["revoked_at"] is not None
                    or row["deadline_at"] <= datetime.now(UTC)
                    or started is not None
                ):
                    raise DomainError(
                        "RUNNER_REVIEW_REQUIRED",
                        "Only a job that never started can be resumed",
                        409,
                    )
                conn.execute(
                    "UPDATE app.harness_runner_jobs SET dispatchable=true WHERE task_id=%s",
                    (task_id,),
                )
                self._event(conn, "job_resume_requested", row["device_id"], task_id)
            else:
                self._stop(
                    conn,
                    row,
                    cancel=action == "cancel",
                    reason="RUNNER_CANCELLED" if action == "cancel" else "RUNNER_PAUSED",
                )
            return self._public(self._job(conn, task_id))

    def task_for_run(self, run_id: UUID) -> str | None:
        with self.engine.transaction() as conn:
            row = conn.execute(
                "SELECT j.task_id FROM app.harness_runner_jobs j "
                "JOIN app.harness_tasks h USING(task_id) WHERE h.run_id=%s AND h.actor=%s",
                (run_id, self.actor),
            ).fetchone()
        return str(row["task_id"]) if row else None

    def _stop(self, conn: Connection, row: Row, *, cancel: bool, reason: str) -> None:
        task_id = row["task_id"]
        if cancel:
            self.engine.harness.cancel(task_id)
        else:
            self.engine.harness.pause(task_id)
        conn.execute("UPDATE app.runs SET reason_code=%s WHERE run_id=%s", (reason, row["run_id"]))
        conn.execute(
            "UPDATE app.harness_runner_jobs SET dispatchable=false WHERE task_id=%s",
            (task_id,),
        )
        conn.execute(
            "UPDATE app.harness_runner_claims SET phase='fenced' WHERE task_id=%s "
            "AND phase IN ('claimed','running')",
            (task_id,),
        )
        self._event(
            conn,
            "job_cancelled" if cancel else "job_paused",
            row["device_id"],
            task_id,
            reason=reason,
        )

    def sweep(self) -> None:
        with self.engine.transaction() as conn:
            self._sweep(conn, datetime.now(UTC))

    def recover(self) -> None:
        with self.engine.transaction() as conn:
            now = datetime.now(UTC)
            conn.execute(
                "UPDATE app.harness_runner_claims SET expires_at=least(expires_at,%s) "
                "WHERE phase IN ('claimed','running')",
                (now,),
            )
            self._sweep(conn, now)

    def _sweep(self, conn: Connection, now: datetime) -> None:
        lost = conn.execute(
            "UPDATE app.harness_runner_devices SET online=false WHERE online "
            "AND last_seen_at<=%s RETURNING device_id",
            (now - timedelta(seconds=CONTACT_SECONDS),),
        ).fetchall()
        for device in lost:
            self._event(conn, "device_offline", device["device_id"])
        stale = conn.execute(
            _CLAIM_QUERY + "WHERE c.phase IN ('claimed','running') AND "
            "(c.expires_at<=%s OR h.deadline_at<=%s OR r.status<>'running' "
            "OR r.execution_epoch<>c.execution_epoch OR d.revoked_at IS NOT NULL)",
            (now, now),
        ).fetchall()
        for lease in stale:
            task_id = lease["task_id"]
            current = (
                lease["run_status"] == "running" and lease["run_epoch"] == lease["execution_epoch"]
            )
            released = (
                lease["phase"] == "claimed"
                and lease["revoked_at"] is None
                and lease["deadline_at"] > now
                and (current or lease["reason_code"] == "PROCESS_RESTARTED")
            )
            if current:
                self.engine.harness.pause(task_id)
            if current or lease["reason_code"] == "PROCESS_RESTARTED":
                reason = "RUNNER_LEASE_EXPIRED" if released else "RUNNER_RESULT_UNKNOWN"
                conn.execute(
                    "UPDATE app.runs SET reason_code=%s WHERE run_id=%s", (reason, lease["run_id"])
                )
                if not released:
                    conn.execute(
                        "UPDATE app.harness_tasks SET public_status='unknown',updated_at=now() "
                        "WHERE task_id=%s",
                        (task_id,),
                    )
            conn.execute(
                "UPDATE app.harness_runner_claims SET phase=%s "
                "WHERE task_id=%s AND execution_epoch=%s",
                ("released" if released else "fenced", task_id, lease["execution_epoch"]),
            )
            conn.execute(
                "UPDATE app.harness_runner_jobs SET dispatchable=%s WHERE task_id=%s",
                (released, task_id),
            )
            self._event(
                conn,
                "claim_released" if released else "claim_fenced",
                lease["device_id"],
                task_id,
                execution_epoch=lease["execution_epoch"],
                started=lease["started_at"] is not None,
            )
        expired = conn.execute(
            _JOB_QUERY
            + "WHERE j.dispatchable AND r.status IN ('queued','paused') AND h.deadline_at<=%s",
            (now,),
        ).fetchall()
        for row in expired:
            self._stop(conn, row, cancel=True, reason="RUNNER_JOB_EXPIRED")
