"""Independent verification events and authoritative GA01 publication guard."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import DecimalException
from typing import TYPE_CHECKING, Any
from uuid import UUID, uuid4

from psycopg.types.json import Jsonb

from mgar_v3.domain import DomainError, digest
from mgar_v3.general_agent.contracts import (
    CriterionEvidence,
    OutcomeContract,
    OutcomeVerificationReceipt,
    TaskSnapshot,
)
from mgar_v3.general_agent.verification_checks import check_action, check_content
from mgar_v3.general_agent.verification_contracts import (
    CheckResult,
    CodingDiff,
    CodingProof,
    CodingRun,
    EvidenceObservation,
    ExternalAction,
    ResultDraft,
    SourceStep,
    VerificationPlan,
    VerificationReport,
    VerificationStep,
)
from mgar_v3.harness.tenancy import require_membership

if TYPE_CHECKING:
    from mgar_v3.coordination.engine import Connection, Engine, Row

_DOMAIN_CHECKS = {
    "coding": {
        "coding_diff", "coding_test", "coding_build", "coding_inspect", "citation", "document",
    },
    "research": {"citation", "browser"},
    "data": {"numeric"},
    "office": {"document", "email", "calendar", "browser"},
}


def _invalid(message: str) -> DomainError:
    return DomainError("GENERAL_VERIFICATION_UNVERIFIED", message, 409)


def _sha(value: Any) -> str:
    return digest(value.model_dump(mode="json"))


class GeneralVerificationStore:
    def __init__(self, engine: Engine) -> None:
        self.engine = engine

    def _task(self, conn: Connection, task_id: UUID, revision: int, version: int) -> TaskSnapshot:
        task = self.engine.general_tasks.lock_current(conn, task_id)
        if (task.revision, task.version, task.state) != (revision, version, "verifying"):
            raise _invalid("Verification is outside the current task phase or revision.")
        if task.membership_id is not None:
            require_membership(conn, task.require_project(), str(task.membership_id), write=True)
        return task

    @staticmethod
    def _validate_plan(task: TaskSnapshot, plan: VerificationPlan) -> None:
        if task.plan_version != plan.plan_version or {
            item.id: item for item in plan.expected_outputs
        } != {item.id: item for item in task.spec.deliverables}:
            raise _invalid("Verification plan is outside the declared output and plan scope.")
        for step in plan.verification_steps:
            if isinstance(step, SourceStep) and step.source not in task.spec.source_refs:
                raise _invalid("Verification source and version are outside the declared scope.")
        for output in plan.expected_outputs:
            kinds = {step.kind for step in plan.verification_steps if step.output_id == output.id}
            if output.kind in {"table", "chart"}:
                required = {"numeric"}
            elif output.kind == "patch":
                required = {"coding_diff", "coding_test"}
            elif output.kind == "test_receipt":
                required = {"coding_test"}
            elif output.kind == "service_receipt":
                if kinds & {"coding_build", "coding_service"}:
                    continue
                required = {"coding_service"}
            else:
                assert task.spec.domain is not None
                allowed = _DOMAIN_CHECKS[task.spec.domain]
                if not kinds & allowed:
                    raise _invalid("Output has no domain-specific mechanical check.")
                continue
            if not required <= kinds:
                raise _invalid("Output is missing required domain checks.")
        trees = {
            step.tree_sha256
            for step in plan.verification_steps
            if isinstance(step, (CodingDiff, CodingRun, CodingProof))
        }
        if len(trees) > 1:
            raise _invalid("Coding checks must cover the same resulting file tree.")

    def install(self, plan: VerificationPlan, expected_version: int) -> UUID:
        """Install an independent, service-authored verification contract."""
        with self.engine.transaction() as conn:
            task = self._task(conn, plan.task_id, plan.task_revision, expected_version)
            self._validate_plan(task, plan)
            identity = _sha(plan)
            verification = conn.execute(
                "SELECT verification_id,plan_sha256 FROM app.general_verification_plans "
                "WHERE task_id=%s AND task_revision=%s AND task_version=%s",
                (task.task_id, task.revision, task.version),
            ).fetchone()
            if verification is not None:
                if verification["plan_sha256"] != identity:
                    raise _invalid("Verification plan already pinned for this version.")
                return UUID(str(verification["verification_id"]))
            key = uuid4()
            conn.execute(
                "INSERT INTO app.general_verification_plans "
                "(verification_id,task_id,task_revision,task_version,"
                "plan_version,plan_sha256,plan) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s)",
                (
                    key,
                    task.task_id,
                    task.revision,
                    task.version,
                    plan.plan_version,
                    identity,
                    Jsonb(plan.model_dump(mode="json")),
                ),
            )
            return key

    def _current(
        self,
        conn: Connection,
        verification_id: UUID,
    ) -> tuple[TaskSnapshot, VerificationPlan, str]:
        row = conn.execute(
            "SELECT * FROM app.general_verification_plans WHERE verification_id=%s",
            (verification_id,),
        ).fetchone()
        if row is None:
            raise _invalid("Verification plan was not found.")
        task = self._task(conn, row["task_id"], row["task_revision"], row["task_version"])
        plan = VerificationPlan.model_validate(row["plan"])
        if row["plan_sha256"] != _sha(plan) or task.plan_version != plan.plan_version:
            raise _invalid("Verification plan binding changed.")
        return task, plan, row["plan_sha256"]

    @staticmethod
    def _latest(conn: Connection, key: UUID) -> dict[str, tuple[int, EvidenceObservation]]:
        rows = conn.execute(
            "SELECT DISTINCT ON (step_id) step_id,evidence_version,observation "
            "FROM app.general_verification_evidence WHERE verification_id=%s "
            "ORDER BY step_id,evidence_version DESC",
            (key,),
        ).fetchall()
        return {
            row["step_id"]: (
                row["evidence_version"],
                EvidenceObservation.model_validate(
                    row["observation"],
                ),
            )
            for row in rows
        }

    def capture(self, key: UUID, step_id: str, observation: EvidenceObservation) -> int:
        """Trusted collection port. Caller supplies the collector's authenticated identity."""
        raw = self.engine.artifacts.read(observation.content_sha256)
        if observation.observed_at > datetime.now(UTC):
            raise _invalid("Evidence has a future observation timestamp.")
        with self.engine.transaction() as conn:
            _, plan, _ = self._current(conn, key)
            step = next((item for item in plan.verification_steps if item.id == step_id), None)
            if step is None or isinstance(step, (ExternalAction, CodingProof)):
                raise _invalid("Evidence step is absent or bound to the action journal.")
            if isinstance(step, SourceStep) and (
                observation.source != step.source
                or observation.content_sha256 != step.source_sha256
            ):
                raise _invalid("Evidence differs from the locked source version and content.")
            if not isinstance(step, SourceStep) and observation.source is not None:
                raise _invalid("Evidence source is outside the step definition.")
            conn.execute(
                "INSERT INTO app.artifacts(content_hash,byte_count) VALUES (%s,%s) "
                "ON CONFLICT DO NOTHING",
                (observation.content_sha256, len(raw)),
            )
            row = conn.execute(
                "INSERT INTO app.general_verification_evidence "
                "(verification_id,step_id,observation) "
                "VALUES (%s,%s,%s) RETURNING evidence_version",
                (key, step_id, Jsonb(observation.model_dump(mode="json"))),
            ).fetchone()
            assert row is not None
            return int(row["evidence_version"])

    def _step(
        self,
        task: TaskSnapshot,
        step: VerificationStep,
        item: tuple[int, EvidenceObservation] | None,
        output: bytes | None,
        verifier: str,
        now: datetime,
    ) -> tuple[CheckResult, datetime | None]:
        version = item[0] if item is not None else None
        content_hash = item[1].content_sha256 if item is not None else None
        valid_until = None
        passed = False
        reason = "artifact_missing" if output is None else "evidence_missing"
        try:
            if output is not None and isinstance(step, CodingProof):
                from mgar_v3.general_agent.coding_executor import check_coding_proof

                reason = "coding_proof_not_verified"
                proof = check_coding_proof(self.engine, task, step, output, verifier)
                if proof is not None:
                    observed_at, content_hash = proof
                    valid_until = observed_at + timedelta(seconds=step.max_age_seconds)
                    passed = observed_at <= now <= valid_until
            elif output is not None and isinstance(step, ExternalAction):
                action = self.engine.general_actions.get(step.action_id)
                reason = "action_not_confirmed"
                if action.receipt is not None:
                    content_hash = action.receipt_sha256
                    passed = verifier != action.intent.scope.actor and check_action(
                        task, step, action, output, now
                    )
                    valid_until = action.receipt.recorded_at + timedelta(
                        seconds=step.max_age_seconds
                    )
            elif output is not None and item is not None:
                observation = item[1]
                valid_until = observation.observed_at + timedelta(seconds=step.max_age_seconds)
                if observation.collector == verifier:
                    reason = "independent_verifier_required"
                elif not observation.observed_at <= now <= valid_until:
                    reason = "evidence_stale"
                else:
                    reason = "check_failed"
                    source = self.engine.artifacts.read(observation.content_sha256)
                    passed = check_content(task, step, source, output, verifier)
                    if isinstance(step, CodingRun) and any(
                        deliver.id == step.output_id and deliver.kind == "test_receipt"
                        for deliver in task.spec.deliverables
                    ):
                        passed = passed and source == output
        except (ValueError, DecimalException, DomainError, RecursionError):
            reason = "evidence_invalid"
        return (
            CheckResult(
                step_id=step.id,
                status="verified" if passed else "unverified",
                reason=None if passed else reason,
                evidence_version=version,
                evidence_sha256=content_hash,
            ),
            valid_until if passed else None,
        )

    def _graph_outputs(
        self,
        conn: Connection,
        task: TaskSnapshot,
        proposed: dict[str, str],
    ) -> bool:
        row = conn.execute(
            "SELECT snapshot FROM app.general_task_graphs WHERE task_id=%s AND task_revision=%s",
            (task.task_id, task.revision),
        ).fetchone()
        if row is None:
            return True
        snapshot = row["snapshot"]
        produced = {
            key: value
            for progress in snapshot["progress"].values()
            if progress["state"] == "succeeded"
            for key, value in progress["outputs"].items()
        }
        return snapshot["plan_version"] == task.plan_version and all(
            produced.get(key) == value for key, value in proposed.items()
        )

    @staticmethod
    def _versions(items: dict[str, tuple[int, EvidenceObservation]]) -> dict[str, int]:
        return {key: pair[0] for key, pair in items.items()}

    def verify(self, key: UUID, result: ResultDraft, *, verifier: str) -> VerificationReport:
        """Verify outside the Engine transaction, then fence every input at publication."""
        with self.engine.transaction() as conn:
            task, plan, plan_hash = self._current(conn, key)
            items = self._latest(conn, key)
        outputs = {item.deliverable_id: item.sha256 for item in result.outputs}
        if set(outputs) != {item.id for item in plan.expected_outputs}:
            raise _invalid("Result output IDs differ from the pinned plan.")
        content: dict[str, bytes | None] = {}
        for output_id, artifact in outputs.items():
            try:
                content[output_id] = self.engine.artifacts.read(artifact)
            except DomainError:
                content[output_id] = None
        now = datetime.now(UTC)
        checks = []
        expiries = []
        for step in plan.verification_steps:
            checked, valid = self._step(
                task, step, items.get(step.id), content[step.output_id], verifier, now
            )
            checks.append(checked)
            if valid is not None:
                expiries.append(valid)
        verified = all(check.status == "verified" for check in checks)
        binding = {
            "verification_id": str(key),
            "plan_sha256": plan_hash,
            "checks": [check.model_dump(mode="json") for check in checks],
            "result": result.model_dump(mode="json"),
        }
        outcome = self._outcome(task, plan, result, binding) if verified else None
        report = VerificationReport(
            report_id=uuid4(),
            verification_id=key,
            verifier=verifier,
            checked_at=now,
            valid_until=min(expiries) if verified else None,
            plan_sha256=plan_hash,
            result_sha256=digest(binding),
            status="verified" if verified else "unverified",
            check_results=tuple(checks),
            outcome=outcome,
        )
        with self.engine.transaction() as conn:
            self._current(conn, key)
            if self._versions(self._latest(conn, key)) != self._versions(items):
                raise _invalid("Evidence changed during verification.")
            if not self._graph_outputs(conn, task, outputs):
                raise _invalid("Result outputs differ from the current graph receipts.")
            conn.execute(
                "INSERT INTO app.general_verification_reports "
                "(report_id,verification_id,outcome_sha256,result_sha256,evidence_versions,report) "
                "VALUES (%s,%s,%s,%s,%s,%s)",
                (
                    report.report_id,
                    key,
                    _sha(outcome) if outcome else None,
                    report.result_sha256,
                    Jsonb(self._versions(items)),
                    Jsonb(report.model_dump(mode="json")),
                ),
            )
        return report

    def _outcome(
        self,
        task: TaskSnapshot,
        plan: VerificationPlan,
        result: ResultDraft,
        binding: dict[str, Any],
    ) -> OutcomeContract:
        import json

        manifest = self.engine.artifacts.put(
            json.dumps(
                binding,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
            ).encode()
        )
        receipts = []
        for item in task.spec.success_criteria:
            receipt = OutcomeVerificationReceipt(
                status="verified",
                task_id=task.task_id,
                task_revision=task.revision,
                plan_version=plan.plan_version,
                verification_version=task.version,
                criterion_id=item.id,
                criterion_sha256=_sha(item),
                deliverables=tuple(
                    output
                    for output in result.outputs
                    if output.deliverable_id in item.deliverable_ids
                ),
                evidence_sha256=(manifest,),
            )
            receipts.append(
                CriterionEvidence(
                    criterion_id=item.id,
                    receipt_sha256=self.engine.artifacts.put(receipt.model_dump_json().encode()),
                )
            )
        return OutcomeContract(
            task_revision=task.revision,
            plan_version=plan.plan_version,
            deliverables=result.outputs,
            evidence=tuple(receipts),
            summary=result.summary,
        )

    @staticmethod
    def _report(row: Row) -> VerificationReport:
        return VerificationReport.model_validate(
            {**row["report"], "confirmed_by": row["confirmed_by"]}
        )

    def _stored_report(self, conn: Connection, report_id: UUID) -> Row:
        row = conn.execute(
            "SELECT * FROM app.general_verification_reports WHERE report_id=%s",
            (report_id,),
        ).fetchone()
        if row is None:
            raise _invalid("Verification report was not found.")
        return row

    def get(self, report_id: UUID) -> VerificationReport:
        with self.engine.transaction() as conn:
            return self._report(self._stored_report(conn, report_id))

    def _eligible(self, row: Row, report: VerificationReport, versions: dict[str, int]) -> bool:
        expiry = report.valid_until
        return (
            report.status == "verified"
            and report.outcome is not None
            and row["outcome_sha256"] == _sha(report.outcome)
            and row["result_sha256"] == report.result_sha256
            and expiry is not None
            and expiry > datetime.now(UTC)
            and row["evidence_versions"] == versions
        )

    def confirm(self, report_id: UUID, result_sha256: str, *, reviewer: str) -> VerificationReport:
        """Trusted human review port with an exact result digest."""
        with self.engine.transaction() as conn:
            row = self._stored_report(conn, report_id)
            report = self._report(row)
            self._current(conn, report.verification_id)
            versions = self._versions(self._latest(conn, report.verification_id))
            if (
                not self._eligible(row, report, versions)
                or report.result_sha256 != result_sha256
                or (report.confirmed_by is not None and report.confirmed_by != reviewer)
            ):
                raise _invalid("Review differs from the current verified result.")
            if report.confirmed_by is None:
                conn.execute(
                    "UPDATE app.general_verification_reports "
                    "SET confirmed_by=%s,confirmed_at=now() "
                    "WHERE report_id=%s",
                    (reviewer, report_id),
                )
            return self._report(self._stored_report(conn, report_id))

    def publish(self, report_id: UUID) -> TaskSnapshot:
        with self.engine.transaction() as conn:
            row = self._stored_report(conn, report_id)
            report = self._report(row)
            task, _, _ = self._current(conn, report.verification_id)
        if report.outcome is None:
            raise _invalid("Outcome has unverified checks.")
        return self.engine.general_tasks.finish(
            task.task_id, task.revision, task.version, report.outcome
        )

    def require_attestation(
        self,
        conn: Connection,
        task: TaskSnapshot,
        outcome: OutcomeContract,
    ) -> None:
        row = conn.execute(
            "SELECT "
            "EXISTS (SELECT 1 FROM app.general_task_graphs WHERE task_id=%(task)s "
            "AND task_revision=%(revision)s) OR "
            "EXISTS (SELECT 1 FROM app.general_action_intents WHERE task_id=%(task)s "
            "AND task_revision=%(revision)s) OR "
            "EXISTS (SELECT 1 FROM app.general_verification_plans WHERE task_id=%(task)s "
            "AND task_revision=%(revision)s) AS managed",
            {"task": task.task_id, "revision": task.revision},
        ).fetchone()
        if row is None or not row["managed"]:
            return
        if task.membership_id is not None:
            require_membership(conn, task.require_project(), str(task.membership_id), write=True)
        if conn.execute(
            "SELECT 1 FROM app.general_action_intents i JOIN app.general_action_outbox o "
            "USING (action_id) WHERE i.task_id=%s AND i.task_revision=%s "
            "AND o.status IN ('dispatching','unknown') LIMIT 1",
            (task.task_id, task.revision),
        ).fetchone():
            raise _invalid("An action still requires reconciliation.")
        matches = conn.execute(
            "SELECT r.*,p.plan,p.plan_sha256 FROM app.general_verification_plans p "
            "JOIN app.general_verification_reports r USING (verification_id) "
            "WHERE p.task_id=%s AND p.task_revision=%s AND p.task_version=%s AND p.plan_version=%s "
            "AND r.outcome_sha256=%s",
            (task.task_id, task.revision, task.version, task.plan_version, _sha(outcome)),
        ).fetchall()
        for saved in matches:
            report = self._report(saved)
            plan = VerificationPlan.model_validate(saved["plan"])
            versions = self._versions(self._latest(conn, report.verification_id))
            if (
                report.plan_sha256 == saved["plan_sha256"] == _sha(plan)
                and self._eligible(saved, report, versions)
                and (not plan.require_confirmation or report.confirmed_by is not None)
            ):
                return
        raise _invalid("The current result lacks an independent and confirmed attestation.")
