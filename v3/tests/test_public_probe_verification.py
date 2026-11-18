"""Synthetic verifier boundary regressions; no provider, PG or process calls."""

from __future__ import annotations

import copy
import json
from dataclasses import dataclass

import pytest
from pydantic import ValidationError
from test_harness_public_probe import PLAN, output, raw_receipt
from test_harness_public_probe import task as base_task

from mgar_v3.domain import digest
from mgar_v3.harness.adapters import LocalVerifier
from mgar_v3.harness.coding_loop import CodingLoop
from mgar_v3.harness.contracts import (
    EvaluationProfile,
    ExecutionReceipt,
    TaskBinding,
    VerificationRequest,
    VerificationRequirement,
    WorkspaceSnapshot,
    execution_matches,
    sha256_text,
)
from mgar_v3.harness.public_probe import normalize_probe_receipt
from mgar_v3.harness.public_probe_types import PUBLIC_PROBE_PREFIX

SUITE_ARGV = ("python3", "-B", "-m", "unittest", "discover", "-v")


def requirements():
    return (
        VerificationRequirement(
            requirement_id="suite", acceptance_role="regression", command_argv=SUITE_ARGV
        ),
        VerificationRequirement(
            requirement_id="behavior",
            acceptance_role="behavior",
            command_argv=PLAN.argv,
            public_probe_plan_hash=PLAN.plan_hash,
            min_tests=len(PLAN.case_ids),
        ),
    )


def binding(**changes):
    return base_task(requirements=requirements(), **changes)


@dataclass
class Fixture:
    task: TaskBinding
    green: ExecutionReceipt
    red: ExecutionReceipt


@pytest.fixture
def facts():
    task = binding()

    def derive(raw):
        return normalize_probe_receipt(
            PLAN, raw, task=task, expected_tree="b" * 64, log_complete=True, log_limit_bytes=65536
        ).execution

    return Fixture(
        task=task,
        green=derive(raw_receipt()),
        red=derive(
            raw_receipt(
                output(statuses=("assertion_failed", "passed")), status="failed", exit_code=1
            )
        ),
    )


def matches(facts, receipt):
    return execution_matches(facts.task, facts.task.requirements[1], receipt)


def change_proof(receipt, **updates):
    return receipt.model_copy(update={"public_probe": {**receipt.public_probe, **updates}})


def rewrite_frame(receipt, edit, *, synchronize_cases=True):
    value = json.loads(receipt.stdout[len(PUBLIC_PROBE_PREFIX) :])
    edit(value)
    text = PUBLIC_PROBE_PREFIX + json.dumps(value) + "\n"
    proof = copy.deepcopy(receipt.public_probe)
    if synchronize_cases:
        # Model defaults are part of normalized case evidence, including detail="".
        proof["cases"] = [{"detail": "", **case} for case in value["cases"]]
    return receipt.model_copy(
        update={"stdout": text, "log_sha256": sha256_text(text), "public_probe": proof}
    )


def test_normalizer_embeds_exact_frozen_plan_and_raw_generating_identity(facts):
    proof = facts.green.public_probe
    assert proof["plan"] == PLAN.model_dump(mode="json")
    assert digest(proof["plan"]) == proof["plan_hash"] == PLAN.plan_hash
    assert proof["execution_epoch"] == facts.green.provenance.execution_epoch
    assert matches(facts, facts.green) and matches(facts, facts.red)
    restored = facts.task.model_copy(update={"execution_epoch": facts.task.execution_epoch + 1})
    assert execution_matches(restored, restored.requirements[1], facts.green)


@pytest.mark.parametrize(
    "updates",
    [
        {"status": "passed", "exit_code": 0, "tests_failed": 0},
        {"exit_code": 0},
        {"status": "passed"},
        {"tests_failed": 0},
        {"tests_run": 99},
        {"purpose": "command"},
    ],
)
def test_red_metadata_cannot_turn_a_pass_or_inconsistent_receipt_into_reproduction(facts, updates):
    forged = facts.red.model_copy(update=updates)
    assert not matches(facts, forged)


@pytest.mark.parametrize(
    "updates",
    [
        {"status": "failed", "exit_code": 1, "tests_failed": 1},
        {"exit_code": 1},
        {"tests_failed": 1},
        {"tests_run": 1},
        {"log_sha256": "0" * 64},
        {"provenance": None},
    ],
)
def test_green_metadata_cannot_hide_failures_missing_cases_or_provenance(facts, updates):
    assert not matches(facts, facts.green.model_copy(update=updates))


@pytest.mark.parametrize(
    "updates",
    [
        {"plan_hash": "0" * 64},
        {"task_id": "other-task"},
        {"tree_hash": "0" * 64},
        {"execution_epoch": 99},
        {"outcome": "red"},
        {"reason_code": "collection_error"},
        {"source_receipt_id": "not-an-artifact"},
        {"cases": []},
        {"plan": None},
    ],
)
def test_proof_identity_and_plan_labels_are_not_sufficient_authority(facts, updates):
    assert not matches(facts, change_proof(facts.green, **updates))


@pytest.mark.parametrize("mutation", ["plan_id", "case_id", "asset", "skipped", "xfailed"])
def test_exact_frozen_plan_and_case_set_survive_self_consistent_output_relabeling(facts, mutation):
    def edit(frame):
        if mutation == "plan_id":
            frame["plan_id"] = "different-plan-at-same-asset"
        elif mutation == "case_id":
            frame["cases"][0]["id"] = "different-case-at-same-count"
        elif mutation == "asset":
            frame["asset_sha256"] = "d" * 64
        else:
            frame["cases"][0]["status"] = mutation

    assert not matches(facts, rewrite_frame(facts.green, edit))


def test_duplicate_keys_extra_frames_and_bare_receipts_are_rejected(facts):
    duplicate_keys = facts.green.stdout.replace(
        '"plan_id": "toy-add"', '"plan_id": "decoy", "plan_id": "toy-add"'
    )
    for text in (
        duplicate_keys,
        facts.green.stdout + facts.green.stdout,
        "noise\n" + facts.green.stdout,
    ):
        candidate = facts.green.model_copy(update={"stdout": text, "log_sha256": sha256_text(text)})
        assert not matches(facts, candidate)
    assert not matches(facts, facts.green.model_copy(update={"public_probe": None}))


def test_local_verifier_cannot_accept_red_labels_on_green_receipt(facts):
    fake = facts.red.model_copy(update={"status": "passed", "exit_code": 0, "tests_failed": 0})
    task = facts.task
    suite = raw_receipt(argv=SUITE_ARGV, tests_run=1)
    verification = LocalVerifier().verify(
        VerificationRequest(
            task=task,
            snapshot=WorkspaceSnapshot(
                workspace_id=task.workspace_id, tree_hash="b" * 64, files={}
            ),
            receipts=(suite, fake),
        )
    )
    assert verification.status != "verified"


@pytest.mark.parametrize(
    "reqs",
    [
        (
            VerificationRequirement(
                requirement_id="suite", acceptance_role="regression", command_argv=SUITE_ARGV
            ),
        ),
        (VerificationRequirement(requirement_id="old"), requirements()[0]),
        (requirements()[1],),
        (requirements()[0], requirements()[1].model_copy(update={"public_probe_plan_hash": None})),
        (
            requirements()[0],
            requirements()[1].model_copy(update={"command_argv": ("python3", "-c", "x")}),
        ),
    ],
)
def test_incomplete_public_roles_cannot_silently_remove_bugfix_reproduction(reqs):
    with pytest.raises(ValidationError):
        base_task(requirements=reqs)


def test_only_public_behavior_requires_red_while_the_original_suite_is_regression():
    loop = object.__new__(CodingLoop)
    bugfix = loop._base_items(binding())
    assert [item.requirement_id for item in bugfix if item.kind == "reproduction"] == ["behavior"]
    feature = loop._base_items(binding(task_kind="feature"))
    assert not any(item.kind == "reproduction" for item in feature)
    legacy = loop._base_items(
        base_task(requirements=(VerificationRequirement(requirement_id="suite"),))
    )
    assert [item.requirement_id for item in legacy if item.kind == "reproduction"] == ["suite"]


def test_default_optional_fields_preserve_legacy_serialization_hash_and_roundtrip():
    old_req = {
        "requirement_id": "suite",
        "kind": "test",
        "command_contains": ["python3"],
        "min_tests": 1,
    }
    req = VerificationRequirement.model_validate(old_req)
    assert req.model_dump(mode="json") == old_req
    explicit = req.model_copy(
        update={"acceptance_role": "legacy", "command_argv": (), "public_probe_plan_hash": None}
    )
    assert digest(explicit.model_dump(mode="json")) == digest(old_req)
    raw = raw_receipt()
    old_raw = raw.model_dump(mode="json")
    old_raw.pop("public_probe", None)
    roundtrip = ExecutionReceipt.model_validate({**old_raw, "public_probe": None})
    assert roundtrip.model_dump(mode="json") == old_raw
    assert digest(roundtrip.model_dump(mode="json")) == digest(old_raw)
    profile = EvaluationProfile(
        experiment_id="legacy",
        task_id="legacy",
        split="dev",
        arm="candidate",
        task_kind="bugfix",
        objective="Legacy controlled task",
        model={},
        resources={
            "max_model_calls": 20,
            "max_tool_calls": 120,
            "max_cost_micros": 1_000_000,
            "deadline_seconds": 600,
            "max_context_tokens": 20000,
            "max_repair_rounds": 8,
            "max_output_tokens": 2048,
        },
    )
    payload = profile.model_dump(mode="json")
    assert "public_probe" not in payload and "verification_protocol" not in payload
    clone = EvaluationProfile.model_validate(
        {**payload, "public_probe": None, "verification_protocol": "legacy"}
    )
    assert clone.model_dump(mode="json") == payload
    assert digest(clone.model_dump(mode="json")) == digest(payload)


def test_public_plan_refactor_keeps_the_required_signature_gate():
    loop = object.__new__(CodingLoop)
    items = loop._base_items(binding(task_kind="refactor"))
    signature = [item for item in items if item.kind == "signature"]
    assert len(signature) == 1 and signature[0].item_id == "signature" and signature[0].required
    assert {item.kind for item in items} >= {"regression", "behavior", "signature"}


def test_public_plan_run_app_keeps_required_readiness_and_cleanup_gates():
    loop = object.__new__(CodingLoop)
    items = loop._base_items(binding(task_kind="run_app"))
    for name in ("readiness", "cleanup"):
        gates = [item for item in items if item.kind == name]
        assert len(gates) == 1 and gates[0].item_id == name and gates[0].required
    assert {item.kind for item in items} >= {"regression", "behavior", "readiness", "cleanup"}


def test_public_plan_inspect_rejects_execution_requirements():
    with pytest.raises(ValidationError, match="Inspection"):
        binding(task_kind="inspect")
