"""Mechanical verification and independent publication on real PostgreSQL."""

import hashlib
import json
import subprocess
import sys
from datetime import UTC, datetime, timedelta

import pytest
from test_general_agent_ga01 import create, outcome_for, spec
from test_general_agent_ga02 import allowed, capability, claim, graph, node, receipt, start
from test_general_agent_ga04 import Transport, approve, bound_receipt, prepare

from mgar_v3.domain import DomainError
from mgar_v3.effects.dispatcher import GeneralActionDispatcher
from mgar_v3.general_agent import verification_store
from mgar_v3.general_agent.actions import ActionTarget
from mgar_v3.general_agent.capabilities import CapabilityRegistry
from mgar_v3.general_agent.contracts import Deliverable, OutcomeArtifact, SourceRef, TaskPlan
from mgar_v3.general_agent.verification_contracts import (
    BrowserState,
    Citation,
    CodingDiff,
    CodingRun,
    EvidenceObservation,
    ExternalAction,
    Numeric,
    ResultDraft,
    VerificationPlan,
)
from mgar_v3.harness.contracts import ExecutionProvenance, ExecutionReceipt, WorkspaceDiff

RUN_TREE = "2" * 64
BASE_TREE = "1" * 64
ENVIRONMENT = "3" * 64
RESEARCH = "https://docs.example.test/v1"
EXCERPT = "The stable SDK supports continuation."


def ready_for_review(engine, value):
    store = engine.general_tasks
    task = create(store, value)
    planned = store.save_plan(
        task.task_id, 1, 1, TaskPlan(task_revision=1, steps=("Produce result",))
    )
    running = store.transition(task.task_id, 1, planned.version, "running")
    return store.transition(task.task_id, 1, running.version, "verifying")


def pin(engine, task, *steps, **changes):
    model = VerificationPlan(
        task_id=task.task_id,
        task_revision=task.revision,
        plan_version=task.plan_version,
        expected_outputs=changes.pop("expected_outputs", task.spec.deliverables),
        verification_steps=steps,
        **changes,
    )
    store = engine.general_verification
    return store, store.install(model, task.version)


def put(engine, payload):
    if not isinstance(payload, bytes):
        payload = json.dumps(payload).encode()
    return engine.artifacts.put(payload)


def draft(hash_value, summary="Reviewed according to the current verification plan."):
    return ResultDraft(
        summary=summary, outputs=(OutcomeArtifact(deliverable_id="result", sha256=hash_value),)
    )


def capture(store, key, step, content_hash, *, source=None, collector="collector", when=None):
    return store.capture(
        key,
        step,
        EvidenceObservation(
            collector=collector,
            observed_at=when or datetime.now(UTC),
            content_sha256=content_hash,
            source=source,
        ),
    )


def research(engine, *, managed=False):
    src = SourceRef(kind="web", reference=RESEARCH, version="v1")
    value = allowed().model_copy(update={"source_refs": (src,)})
    report_hash = put(engine, f"{EXCERPT}\nSource: {RESEARCH}\n".encode())
    original = put(engine, f"Published source\n{EXCERPT}\n".encode())
    if managed:
        descriptor = capability()
        graphs = engine.general_graphs
        graphs.registry = CapabilityRegistry([descriptor])
        task, state, access = start(
            engine,
            graph(node("report", outputs=("result",))),
            value,
            descriptor,
        )
        run = claim(graphs, state, "report", access)
        graphs.settle(receipt(engine, run, outputs={"result": report_hash}))
        task = engine.general_tasks.get(task.task_id)
    else:
        task = ready_for_review(engine, value)
    step = Citation(
        id="citation",
        kind="citation",
        output_id="result",
        source=src,
        excerpt=EXCERPT,
        source_sha256=original,
        max_age_seconds=120,
    )
    return task, step, report_hash


def test_graph_completion_requires_durable_independent_attestation_and_exact_outputs(engine):
    task, step, result_hash = research(engine, managed=True)
    with pytest.raises(DomainError) as forged:
        engine.general_tasks.finish(task.task_id, 1, task.version, outcome_for(engine, task))
    assert forged.value.code == "GENERAL_VERIFICATION_UNVERIFIED"
    store, key = pin(engine, task, step)
    reported = draft(result_hash)
    missing = store.verify(key, reported, verifier="verifier")
    assert missing.status == "unverified" and missing.check_results[0].reason == "evidence_missing"
    with pytest.raises(DomainError):
        store.publish(missing.report_id)

    version = capture(store, key, step.id, step.source_sha256, source=step.source)
    swapped = put(engine, f"Replacement result\n{EXCERPT}\n{RESEARCH}".encode())
    with pytest.raises(DomainError, match="current graph receipts"):
        store.verify(key, draft(swapped), verifier="verifier")
    result = store.verify(key, reported, verifier="verifier")
    assert result.status == "verified" and result.check_results[0].evidence_version == version
    assert engine.general_tasks.get(task.task_id).state == "verifying"
    with pytest.raises(DomainError):
        engine.general_tasks.finish(
            task.task_id,
            1,
            task.version,
            result.outcome.model_copy(update={"summary": "Unverified substituted summary"}),
        )
    assert store.publish(result.report_id).state == "completed"


def test_source_version_freshness_and_verifier_independence(engine):
    task, step, result_hash = research(engine)
    store, key = pin(engine, task, step)
    with pytest.raises(DomainError, match="locked source"):
        capture(
            store,
            key,
            step.id,
            step.source_sha256,
            source=step.source.model_copy(update={"version": "another-version"}),
        )
    capture(
        store,
        key,
        step.id,
        step.source_sha256,
        source=step.source,
        when=datetime.now(UTC) - timedelta(minutes=3),
    )
    assert store.verify(key, draft(result_hash), verifier="verifier").check_results[0].reason == (
        "evidence_stale"
    )
    capture(store, key, step.id, step.source_sha256, source=step.source, collector="verifier")
    assert store.verify(key, draft(result_hash), verifier="verifier").check_results[0].reason == (
        "independent_verifier_required"
    )
    truncated = put(engine, f"Different quote.\nSource: {RESEARCH}".encode())
    negative = store.verify(key, draft(truncated), verifier="other-verifier")
    assert negative.status == "unverified" and negative.check_results[0].reason == "check_failed"


def test_manual_confirmation_binds_evidence_version_and_summary(engine):
    task, step, result_hash = research(engine)
    store, key = pin(engine, task, step, require_confirmation=True)
    capture(store, key, step.id, step.source_sha256, source=step.source)
    first = store.verify(key, draft(result_hash), verifier="verifier")
    with pytest.raises(DomainError, match="confirmed attestation"):
        store.publish(first.report_id)
    with pytest.raises(DomainError):
        store.confirm(first.report_id, "a" * 64, reviewer="human")
    capture(store, key, step.id, step.source_sha256, source=step.source)
    with pytest.raises(DomainError, match="current verified result"):
        store.confirm(first.report_id, first.result_sha256, reviewer="human")
    new = store.verify(
        key, draft(result_hash, "Updated text is part of the review."), verifier="verifier"
    )
    assert new.result_sha256 != first.result_sha256
    assert store.confirm(new.report_id, new.result_sha256, reviewer="human").confirmed_by == "human"
    assert store.publish(new.report_id).state == "completed"


def test_expired_report_cannot_publish_and_revision_fences_existing_plan(engine, monkeypatch):
    task, step, result_hash = research(engine)
    store, key = pin(engine, task, step)
    capture(store, key, step.id, step.source_sha256, source=step.source)
    report = store.verify(key, draft(result_hash), verifier="verifier")

    class Expired(datetime):
        @classmethod
        def now(cls, tz=None):
            return report.valid_until + timedelta(seconds=1)

    monkeypatch.setattr(verification_store, "datetime", Expired)
    with pytest.raises(DomainError, match="confirmed attestation"):
        store.publish(report.report_id)
    engine.general_tasks.revise(task.task_id, 1, task.version, task.spec)
    with pytest.raises(DomainError, match="revision"):
        store.verify(key, draft(result_hash), verifier="verifier")


def test_evidence_changed_during_check_cannot_publish_old_version(engine, monkeypatch):
    task, step, result_hash = research(engine)
    store, key = pin(engine, task, step)
    capture(store, key, step.id, step.source_sha256, source=step.source)
    original = verification_store.check_content

    def updated(*args):
        capture(store, key, step.id, step.source_sha256, source=step.source)
        return original(*args)

    with monkeypatch.context() as fault:
        fault.setattr(verification_store, "check_content", updated)
        with pytest.raises(DomainError, match="Evidence changed"):
            store.verify(key, draft(result_hash), verifier="verifier")
    with engine.transaction() as conn:
        assert (
            conn.execute(
                "SELECT count(*) AS total FROM app.general_verification_reports"
            ).fetchone()["total"]
            == 0
        )


@pytest.mark.parametrize("bad", ["sample", "formula", "value", "field"])
def test_data_recalculates_typed_fields_formula_and_sample_count(engine, bad):
    src = SourceRef(kind="dataset", reference="march-orders", version="2026-03")
    value = spec("data").model_copy(update={"source_refs": (src,)})
    task = ready_for_review(engine, value)
    data = put(engine, [{"amount": 2}, {"amount": 10}])
    step = Numeric(
        id="check-data",
        kind="numeric",
        output_id="result",
        source=src,
        source_sha256=data,
        field="amount",
        formula="mean",
    )
    store, key = pin(engine, task, step)
    capture(store, key, step.id, data, source=src)
    expected = {"sample_count": 2, "value": 6, "formula": "mean", "field": "amount"}
    name, wrong = {
        "sample": ("sample_count", 1),
        "formula": ("formula", "sum"),
        "value": ("value", 12),
        "field": ("field", "currency"),
    }[bad]
    invalid = store.verify(key, draft(put(engine, {**expected, name: wrong})), verifier="verifier")
    assert invalid.status == "unverified"
    good = store.verify(key, draft(put(engine, expected)), verifier="verifier")
    assert good.status == "verified"
    assert store.publish(good.report_id).state == "completed"


@pytest.mark.parametrize("channel, operation", [("email", "send"), ("calendar", "create")])
def test_external_completion_requires_real_journal_receipt_and_reconciliation(
    engine, channel, operation
):
    _, task, lease, intent = prepare(engine)
    journal = engine.general_actions
    if channel != "email":
        scope = lease.scope.model_copy(
            update={
                "target": ActionTarget(kind="calendar", reference="fixture-account/event-1"),
                "operation": operation,
            }
        )
        lease = journal.issue_lease(scope, lease.access, lease.plan_version)
        intent = intent.model_copy(update={"scope": scope})
    approve(journal, journal.submit(intent, lease.lease_id, lease.epoch), lease)
    transport = Transport(loses_receipt=True)
    dispatcher = GeneralActionDispatcher(engine, transport)
    assert dispatcher.tick() == intent.action_id
    assert journal.get(intent.action_id).status == "unknown"
    task = engine.general_tasks.transition(task.task_id, 1, task.version, "verifying")
    step = ExternalAction(
        id="provider",
        kind=channel,
        output_id="result",
        action_id=intent.action_id,
        operation=operation,
    )
    store, key = pin(engine, task, step)
    forged = put(engine, bound_receipt(intent, "invented-id").model_dump_json().encode())
    assert store.verify(key, draft(forged), verifier="verifier").status == "unverified"
    assert dispatcher.reconcile(intent.action_id)
    action = journal.get(intent.action_id)
    proof = put(engine, action.receipt.model_dump_json().encode())
    assert store.verify(key, draft(proof), verifier="owner").status == "unverified"
    assert store.verify(key, draft(forged), verifier="verifier").status == "unverified"
    checked = store.verify(key, draft(proof), verifier="verifier")
    assert checked.status == "verified"
    assert checked.check_results[0].evidence_sha256 == action.receipt_sha256
    assert store.publish(checked.report_id).state == "completed" and transport.sent == 1


def test_browser_and_document_minimum_require_actual_final_content(engine):
    browser = SourceRef(kind="web", reference="https://office.example.test/receipt", version="v1")
    document = SourceRef(kind="document", reference="meeting-notes", version="v7")
    task = ready_for_review(
        engine, spec("office").model_copy(update={"source_refs": (browser, document)})
    )
    view = put(engine, {"url": browser.reference, "text": "Final state confirmed"})
    note = put(engine, b"Agenda starts at 11")
    checked = BrowserState(
        id="page",
        kind="browser",
        output_id="result",
        source=browser,
        source_sha256=view,
        expected_text="Final state confirmed",
    )
    citation = Citation(
        id="notes",
        kind="document",
        output_id="result",
        source=document,
        source_sha256=note,
        excerpt="Agenda starts at 11",
    )
    store, key = pin(engine, task, checked, citation)
    capture(store, key, "page", view, source=browser)
    capture(store, key, "notes", note, source=document)
    invalid = draft(put(engine, b"Reported wrongly without the final state or original notes"))
    assert store.verify(key, invalid, verifier="verifier").status == "unverified"
    valid = put(engine, b"Final state confirmed\nAgenda starts at 11\nSource meeting-notes")
    report = store.verify(key, draft(valid), verifier="verifier")
    assert report.status == "verified" and store.publish(report.report_id).state == "completed"


def test_coding_patch_uses_diff_and_receipts_on_current_tree(engine, tmp_path):
    task = ready_for_review(engine, spec("coding"))
    patch = "--- add.py\n+++ add.py\n@@ -1 +1 @@\n-result = 4\n+result = 5\n"
    (tmp_path / "test_add.py").write_text(
        "import unittest\nclass Addition(unittest.TestCase):\n"
        "    def test_sum(self):\n        self.assertEqual(2 + 3, 5)\n"
    )
    runs = (
        ("test", (sys.executable, "-m", "unittest", "test_add.py")),
        ("build", (sys.executable, "-m", "compileall", "-q", "test_add.py")),
    )
    steps = [
        CodingDiff(
            id="diff",
            kind="coding_diff",
            output_id="result",
            before_sha256=BASE_TREE,
            tree_sha256=RUN_TREE,
        ),
        *[
            CodingRun(
                id=kind,
                kind="coding_" + kind,
                output_id="result",
                tree_sha256=RUN_TREE,
                environment_sha256=ENVIRONMENT,
                argv=argv,
            )
            for kind, argv in runs
        ],
    ]
    store, key = pin(engine, task, *steps)
    diff = WorkspaceDiff.model_validate(
        {
            "workspace_id": task.project_id,
            "tree_before": BASE_TREE,
            "tree_after": RUN_TREE,
            "patch": patch,
            "changes": [
                {
                    "path": "add.py",
                    "change": "modified",
                    "before_sha256": BASE_TREE,
                    "after_sha256": RUN_TREE,
                }
            ],
        }
    )
    capture(store, key, "diff", put(engine, diff.model_dump_json().encode()))
    for kind, argv in runs:
        result = subprocess.run(argv, cwd=tmp_path, text=True, capture_output=True, check=True)
        if kind == "test":
            assert "Ran 1 test" in result.stderr
        stdout = result.stdout + result.stderr
        proof = ExecutionReceipt(
            receipt_id=hashlib.sha256(stdout.encode()).hexdigest(),
            operation_id=kind,
            purpose=kind,
            status="passed",
            argv=argv,
            cwd=".",
            exit_code=result.returncode,
            stdout=result.stdout,
            stderr=result.stderr,
            duration_seconds=0,
            tree_hash=RUN_TREE,
            log_sha256=hashlib.sha256(stdout.encode()).hexdigest(),
            environment_hash=ENVIRONMENT,
            tests_run=int(kind == "test"),
            provenance=ExecutionProvenance(
                operation_id=kind,
                lease_id="fixture",
                lease_hash=ENVIRONMENT,
                actor="test-runner",
                workspace_id=task.project_id,
                repository="fixture/project",
                execution_epoch=1,
                read_paths=("test_add.py",),
                write_paths=(),
                protected_paths=(),
                grant_hash=ENVIRONMENT,
            ),
        )
        if kind == "test":
            wrong_tree = proof.model_copy(update={"tree_hash": BASE_TREE})
            capture(store, key, kind, put(engine, wrong_tree.model_dump_json().encode()))
            blocked = store.verify(key, draft(put(engine, patch.encode())), verifier="verifier")
            assert blocked.status == "unverified"
        capture(store, key, kind, put(engine, proof.model_dump_json().encode()))
    final = store.verify(key, draft(put(engine, patch.encode())), verifier="verifier")
    assert final.status == "verified"
    assert store.publish(final.report_id).state == "completed"


def test_verification_plan_rejects_unpinned_and_insufficient_checks(engine):
    task, proof, _ = research(engine)
    wrong = proof.model_copy(update={"source": proof.source.model_copy(update={"version": "v2"})})
    with pytest.raises(DomainError, match="source and version"):
        pin(engine, task, wrong)
    unrelated = ExternalAction(
        id="send",
        kind="email",
        output_id="result",
        action_id=task.task_id,
        operation="send",
    )
    with pytest.raises(DomainError, match="domain-specific"):
        pin(engine, task, unrelated)
    bogus = Deliverable(id="result", kind="report", description="Other requirement")
    with pytest.raises(DomainError, match="declared output"):
        pin(engine, task, proof, expected_outputs=(bogus,))


def test_unrelated_attestation_does_not_clear_an_unknown_current_action(engine):
    _, task, lease, intent = prepare(engine)
    journal = engine.general_actions
    approve(journal, journal.submit(intent, lease.lease_id, lease.epoch), lease)
    dispatcher = GeneralActionDispatcher(engine, Transport(loses_receipt=True))
    assert dispatcher.tick() == intent.action_id
    waiting = engine.general_tasks.transition(task.task_id, 1, task.version, "verifying")
    src = waiting.spec.source_refs[0]
    note = put(engine, b"Meeting note")
    step = Citation(
        id="draft",
        kind="document",
        output_id="result",
        source=src,
        source_sha256=note,
        excerpt="Meeting note",
    )
    store, key = pin(engine, waiting, step)
    capture(store, key, step.id, note, source=src)
    result = draft(put(engine, f"Meeting note from {src.reference}".encode()))
    independently_checked = store.verify(key, result, verifier="verifier")
    assert independently_checked.status == "verified"
    with pytest.raises(DomainError, match="requires reconciliation"):
        store.publish(independently_checked.report_id)
    assert dispatcher.reconcile(intent.action_id)
    assert store.publish(independently_checked.report_id).state == "completed"


def test_frozen_verification_report_and_human_confirmation_survive_restart(settings):
    from mgar_v3.coordination.engine import Engine
    from mgar_v3.evidence.artifacts import ArtifactStore

    artifacts = ArtifactStore(settings.artifact_root)
    before = Engine(settings, artifacts)
    try:
        task, step, result_hash = research(before)
        store, key = pin(before, task, step, require_confirmation=True)
        capture(store, key, step.id, step.source_sha256, source=step.source)
        report = store.verify(key, draft(result_hash), verifier="verifier")
        confirmed = store.confirm(report.report_id, report.result_sha256, reviewer="human")
    finally:
        before.close()
    after = Engine(settings, artifacts)
    try:
        assert after.general_verification.get(report.report_id) == confirmed
        result = after.general_verification.publish(report.report_id)
        assert result.outcome == report.outcome and result.state == "completed"
    finally:
        after.close()
