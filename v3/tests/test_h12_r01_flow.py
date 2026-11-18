"""R01 public projection, inbox replanning, PG recovery and scorer integrity."""

import copy
import json
from types import SimpleNamespace

import pytest
from test_harness_v4_h10 import ScriptedModel, _call, _runtime, _stop
from test_harness_v4_h11_service import _CountingModel, _InjectedCrash, _request, _service
from test_harness_v4_h12_runner import _runner

from mgar_v3.coordination.engine import Engine
from mgar_v3.domain import DomainError
from mgar_v3.evidence.artifacts import ArtifactStore
from mgar_v3.harness.acceptance import AcceptanceContract, score_binding
from mgar_v3.harness.coding_loop import SafePoint
from mgar_v3.harness.service import DEFAULT_OUTPUT_TOKENS
from mgar_v3.quality_eval import h12
from mgar_v3.quality_eval.h12_runner import profile_for


def projected(request):
    body = json.loads(next(m.content for m in reversed(request.messages)
                           if m.name == "h10_context" and m.role == "user"))
    return json.loads(body["constraints"][0])["working_state"]["acceptance_contract"]


def profile_from(result):
    contract = AcceptanceContract.model_validate(result["acceptance_contract"])
    return SimpleNamespace(
        objective=contract.objective, task_kind=contract.task_type,
        acceptance_revision=contract.catalog_revision,
        public_requirements=tuple(c.description for c in contract.required_checks
                                  if c.kind == "public_requirement"),
        read_paths=contract.scope.read_paths, write_paths=contract.scope.write_paths,
    ), {"base_commit": contract.baseline_commit}


def test_pg_result_binds_all_stages_and_scorer_rejects_missing_or_stale_facts(
    engine, settings, tmp_path,
):
    model = _CountingModel()
    service = _service(engine, settings, model)
    result = service.create_task(_request(service, tmp_path))
    assert result["status"] == "completed"
    contract = AcceptanceContract.model_validate(result["acceptance_contract"])
    assert {r["stage"] for r in result["acceptance_receipts"]} == {
        "planner", "context", "worker", "verifier",
    }
    assert all(r["acceptance_hash"] == contract.acceptance_hash
               for r in result["acceptance_receipts"])
    profile, public = profile_from(result)
    scored = score_binding(result, profile, public)
    assert scored.acceptance_hash == contract.acceptance_hash
    assert scored.stage == "scorer"
    assert result["acceptance_artifacts"]["evidence_references"]
    assert all(result["acceptance_artifacts"][k] for k in contract.required_artifacts)
    runner = _runner(tmp_path)
    runner.store.get_experiment = lambda _: {"usage": {"unknown_calls": 0}, "reserved_micros": 0}
    synthetic = {**public, "task_id": result["task_id"], "split": "dev", "type": "inspect"}
    public_profile = profile_for(h12.load_frozen_tasks("dev")[0], "synthetic", "candidate")
    public_profile = public_profile.model_copy(update=vars(profile) | {
        "public_test_argv": (), "acceptance_protocol": "contract-v1",
        "resources": public_profile.resources.model_copy(update={
            "max_output_tokens": DEFAULT_OUTPUT_TOKENS,
        }),
    })
    scored_result = result | {"independent_gate": {"status": "PASS"}}
    # R01 acceptance is valid, but a caller-injected gate lacks R05 independent PG/CAS proof.
    measured = runner._score_payload(synthetic, public_profile, scored_result)
    assert measured["acceptance_integrity_status"] == "PASS"
    assert measured["status"] == "FAIL" and measured["new_regression_failures"] is None
    assert not measured["regression_evidence_known"]
    incomplete = dict(scored_result)
    incomplete.pop("acceptance_contract")
    rejected = runner._score_payload(synthetic, public_profile, incomplete)
    assert rejected["status"] == "FAIL" and rejected["acceptance_integrity_status"] == "FAIL"

    for variant in ("missing", "revision", "hash", "required", "verifier", "artifacts"):
        broken = copy.deepcopy(result)
        if variant == "missing":
            broken.pop("acceptance_contract")
        elif variant == "artifacts":
            broken["acceptance_artifacts"]["evidence_references"] = []
        elif variant == "verifier":
            broken["verification"].pop("acceptance_binding")
        elif variant == "required":
            broken["acceptance_contract"]["required_checks"].pop()
        else:
            broken["acceptance_binding"]["acceptance_" + variant] = (
                999 if variant == "revision" else "a" * 64)
        with pytest.raises(DomainError):
            score_binding(broken, profile, public)
    with engine.transaction() as conn:
        rows = conn.execute("SELECT status FROM app.usage_calls WHERE run_id=%s",
                            (result["run_id"],)).fetchall()
    assert len(model.requests) == len(rows) == 2
    assert all(row["status"] == "known" for row in rows)


def test_mid_model_instruction_discards_old_response_and_preserves_required_ids(tmp_path):
    # The first tool must never execute after the owner changes the goal.
    model = ScriptedModel([
        _call("read_file", {"path": "must-not-be-read.py"}),
        _call("read_file", {"path": "source.py"}), _stop("source.py supplies current evidence"),
    ])
    loop, task, grant, session = _runtime(tmp_path, model, max_tokens=32768)
    instruction = "Also retain the actually observed HTTP status in the report."
    delivered = False

    def checkpoint(stage, revision):
        nonlocal delivered
        if stage == "after_model" and not delivered:
            delivered = True
            return SafePoint(
                instructions=(instruction,), revision=1,
                objective=task.objective + "\n\nAdditional instruction: " + instruction,
            )
        return SafePoint()

    try:
        result = loop.run(task, grant, loop.plan(task), checkpoint=checkpoint)
    finally:
        session.close()
    assert result.status == "completed", (result.reason, result.summary)
    first, second = projected(model.requests[0]), projected(model.requests[1])
    assert second["acceptance_revision"] == first["acceptance_revision"] + 1
    assert second["previous_hash"] == first["acceptance_hash"]
    assert {c["id"] for c in first["required_checks"]}.issubset(
        c["id"] for c in second["required_checks"])
    assert instruction in [c["description"] for c in second["required_checks"]]
    assert second["change_reason"] and second["affected_check_ids"]
    assert any(e.kind == "model_discarded_stale" for e in loop.events.events(task.run_id))
    tools = [e for e in loop.events.events(task.run_id) if e.kind == "tool_started"]
    assert len(tools) == 1
    assert all(r.acceptance_hash == second["acceptance_hash"]
               for r in result.acceptance_receipts)


def test_pg_recovery_retains_user_revision_and_reuses_known_reply(engine, settings, tmp_path):
    class OwnerMessageModel(_CountingModel):
        def complete(self, request, cancellation):
            if not self.requests:
                service.post_message(request.task.task_id, kind="instruction",
                                     text="Include source.py line 1 in the evidence.",
                                     message_id="r01-new-goal")
            return super().complete(request, cancellation)

    model = OwnerMessageModel()

    def crash(point, payload):
        if point == "model_after_settlement" and len(model.requests) == 2:
            raise _InjectedCrash("known revised reply before dispatching its tool")

    service = _service(engine, settings, model, crash)
    with pytest.raises(_InjectedCrash):
        service.create_task(_request(service, tmp_path))
    task = engine.harness.tasks()[0]
    operation = model.requests[-1]
    engine.close()
    replacement = Engine(settings, ArtifactStore(settings.artifact_root))
    try:
        replacement.recover()
        resumed = _service(replacement, settings, model).recover_task(task["task_id"])
        assert resumed["status"] == "completed", (resumed["reason"], resumed["summary"])
        contract = AcceptanceContract.model_validate(resumed["acceptance_contract"])
        assert contract.acceptance_revision == 2 and contract.previous_hash
        assert "Include source.py line 1" in contract.objective
        assert model.requests.count(operation) == 1
        assert len(model.requests) == 3
        assert {r["acceptance_hash"] for r in resumed["acceptance_receipts"]} == {
            contract.acceptance_hash,
        }
    finally:
        replacement.close()


def test_public_selfcheck_requirement_reaches_profile_and_actual_worker_projection(tmp_path):
    # Read only the public development task, never private Judge inputs.
    task = next(t for t in h12.load_frozen_tasks("dev")
                if t["task_id"] == "hv4-dev-got-local-roundtrip")
    profile = profile_for(task, "r01-offline-projection", "candidate")
    expected = tuple(task["acceptance"]["required"])
    assert profile.public_requirements == expected
    assert any("status" in text.lower() for text in expected)
    model = ScriptedModel([_call("read_file", {"path": "source.py"}), _stop()])
    loop, bound, grant, session = _runtime(tmp_path, model, max_tokens=32768)
    bound = bound.model_copy(update={"objective": profile.objective,
                                     "public_requirements": profile.public_requirements})
    try:
        result = loop.run(bound, grant, loop.plan(bound))
    finally:
        session.close()
    assert result.status == "completed", result.reason
    for request in model.requests:
        contract = projected(request)
        assert tuple(c["description"] for c in contract["required_checks"]
                     if c["kind"] == "public_requirement") == expected


def test_private_markers_cannot_be_silently_projected_away():
    with pytest.raises(h12.CatalogError):
        h12.public_requirements({"acceptance": {"required": ["Read /private/judge.py"]}})


def test_legacy_profile_serialization_retains_original_hash_inputs():
    task = h12.load_frozen_tasks("dev")[0]
    legacy = profile_for(task, "archived", "candidate", acceptance_protocol="legacy")
    assert not {"acceptance_protocol", "public_requirements", "acceptance_baseline_commit"} & (
        legacy.model_dump(mode="json").keys())
    current = profile_for(task, "new-frozen-candidate", "candidate")
    assert current.acceptance_protocol == "contract-v1"
    assert current.acceptance_baseline_commit == task["base_commit"]
    assert current.public_requirements == tuple(task["acceptance"]["required"])
