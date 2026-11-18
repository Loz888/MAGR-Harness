"""Real-PG H12 shared $12 authority; every catalog/profile here is synthetic."""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor

import psycopg
import pytest

from mgar_v3.coordination.engine import Engine
from mgar_v3.domain import DomainError
from mgar_v3.evidence.artifacts import ArtifactStore
from mgar_v3.harness.contracts import (
    EvaluationProfile,
    EvaluationResources,
    ModelResponse,
    ModelUsage,
)
from mgar_v3.harness.ledger import PersistentBudgetLedger
from mgar_v3.harness.model import DEEPSEEK_CHAT


def _freeze(engine, experiment_id="h12-test", *, max_calls=20):
    resources = EvaluationResources(
        max_model_calls=max_calls, max_tool_calls=120, max_cost_micros=6_000_000,
        deadline_seconds=600, max_context_tokens=8192, max_repair_rounds=8,
    )
    profiles = [EvaluationProfile(
        experiment_id=experiment_id, task_id=f"synthetic-{split}-{index}", split=split,
        arm=arm, task_kind="inspect", objective="Inspect synthetic fixture",
        resources=resources, model=DEEPSEEK_CHAT.model_dump(mode="json"),
    ) for split in ("dev", "holdout") for index in range(12) for arm in ("baseline", "new")]
    config = {"schema": "harness-h12-experiment-v1",
              "profiles": [profile.model_dump(mode="json") for profile in profiles]}
    authorization = {"approved_by": "owner", "allow_paid": True, "max_cost_micros": 12_000_000}
    row = engine.harness.create_experiment(experiment_id, config, authorization)
    return profiles, row, config, authorization


def _task(engine, tmp_path, profile, task_id):
    workspace = tmp_path / task_id
    workspace.mkdir()
    registration = {"workspace_id": task_id, "repository": "local/h12", "base_commit": ""}
    grant = {"actor": "owner", "workspace_id": task_id, "repository": "local/h12",
             "read_paths": list(profile.read_paths), "write_paths": list(profile.write_paths)}
    grant.update(execute=False, network=False, allowed_commands=[],
                 max_seconds=profile.resources.deadline_seconds)
    request = {"project_id": task_id, "task_kind": profile.task_kind,
               "objective": profile.objective,
               "mode": "live", "workspace_root": str(workspace)}
    row = engine.harness.create(task_id, registration, request, grant,
                                profile.resources.budget_limits().model_dump(mode="json"),
                                evaluation_profile=profile)
    row = engine.harness.begin(task_id)
    return row, PersistentBudgetLedger(engine.harness, task_id, row["epoch"])


def _known(engine, row, operation_id, cost, *, input_tokens=0, output_tokens=0):
    engine.harness.save_model_result(row["task_id"], row["epoch"], operation_id, ModelResponse(
        text="controlled", usage=ModelUsage(cost_micros=cost, input_tokens=input_tokens,
                                             output_tokens=output_tokens),
    ))


def test_all_splits_arms_and_new_experiments_share_one_existing_money_budget(engine, tmp_path):
    profiles, experiment, _, _ = _freeze(engine)
    first, ledger = _task(engine, tmp_path, profiles[0], "first")
    ledger.reserve("first", "one", "model", reserved_micros=6_000_000)
    _known(engine, first, "one", 6_000_000)
    profiles2, experiment2, _, _ = _freeze(engine, "another-candidate")
    second, ledger2 = _task(engine, tmp_path, profiles2[-1], "second")
    ledger2.reserve("second", "two", "model", reserved_micros=6_000_000)
    assert first["profile_hash"] == second["profile_hash"] == experiment["profile_hash"]
    assert experiment2["profile_hash"] == experiment["profile_hash"]
    _, ledger3 = _task(engine, tmp_path, profiles[1], "third")
    with pytest.raises(DomainError, match="budget exhausted"):
        ledger3.reserve("third", "over-cap", "model", reserved_micros=1)
    totals = engine.harness.get_experiment("h12-test")
    assert totals["settled_micros"] == totals["reserved_micros"] == 6_000_000
    assert totals["used_calls"] == 2 and ledger3.snapshot("third").cost_micros == 0


def test_task_model_call_limit_is_independent_of_shared_authorization_limit(engine, tmp_path):
    profiles, _, _, _ = _freeze(engine, max_calls=1)
    first, ledger = _task(engine, tmp_path, profiles[0], "first")
    reservation = ledger.reserve("first", "one", "model", reserved_micros=0)
    _known(engine, first, "one", 0)
    assert ledger.reserve("first", "one", "model", reserved_micros=0).reservation_id == (
        reservation.reservation_id
    )
    with pytest.raises(DomainError, match="Per-task"):
        ledger.reserve("first", "two", "model", reserved_micros=0)
    _, second = _task(engine, tmp_path, profiles[-1], "second")
    second.reserve("second", "one", "model", reserved_micros=0)
    assert engine.harness.get_experiment("h12-test")["used_calls"] == 2


def test_shared_budget_survives_engine_reconstruction_without_reset(engine, settings, tmp_path):
    profiles, _, _, _ = _freeze(engine)
    row, ledger = _task(engine, tmp_path, profiles[0], "first")
    ledger.reserve("first", "one", "model", reserved_micros=6_000_000)
    _known(engine, row, "one", 6_000_000)
    engine.close()
    replacement = Engine(settings, ArtifactStore(settings.artifact_root))
    try:
        replacement.recover()
        _, experiment, _, _ = _freeze(replacement)
        assert experiment["settled_micros"] == 6_000_000 and experiment["used_calls"] == 1
    finally:
        replacement.close()


def test_dispatched_unknown_survives_restart_and_blocks_new_task(engine, settings, tmp_path):
    profiles, _, _, _ = _freeze(engine)
    row, ledger = _task(engine, tmp_path, profiles[0], "first")
    ledger.reserve("first", "sent", "model", reserved_micros=6_000_000)
    engine.harness.start_operation("first", row["epoch"], "sent", "model", "a" * 64)
    engine.harness.dispatch_operation("first", row["epoch"], "sent")
    engine.close()
    replacement = Engine(settings, ArtifactStore(settings.artifact_root))
    try:
        replacement.recover()
        profiles2, _, _, _ = _freeze(replacement, "retry-matrix")
        _, retry = _task(replacement, tmp_path, profiles2[-1], "retry")
        with pytest.raises(DomainError, match="Shared paid outcome"):
            retry.reserve("retry", "new-provider-request", "model", reserved_micros=1)
        group = replacement.harness.get_experiment("retry-matrix")
        assert group["reserved_micros"] == 6_000_000 and group["used_calls"] == 1
        assert group["usage"]["unknown_calls"] == 1
    finally:
        replacement.close()


def test_unknown_holds_money_and_blocks_other_arms_even_prepared_dispatch(engine, tmp_path):
    profiles, _, _, _ = _freeze(engine)
    first, ledger = _task(engine, tmp_path, profiles[0], "first")
    second, ledger2 = _task(engine, tmp_path, profiles[-1], "second")
    reservation = ledger.reserve("first", "unknown", "model", reserved_micros=1_000_000)
    ledger2.reserve("second", "prepared", "model", reserved_micros=1_000_000)
    engine.harness.start_operation("second", second["epoch"], "prepared", "model", "a" * 64)
    ledger.mark_unknown(reservation.reservation_id, "lost receipt")
    with pytest.raises(DomainError, match="Shared paid outcome"):
        ledger2.reserve("second", "another", "model", reserved_micros=1)
    with pytest.raises(DomainError, match="Shared paid outcome"):
        engine.harness.dispatch_operation("second", second["epoch"], "prepared")
    totals = engine.harness.get_experiment("h12-test")
    assert totals["reserved_micros"] == 2_000_000 and totals["usage"]["unknown_calls"] == 1
    assert first["run_id"] != second["run_id"]


def test_token_receipts_include_known_and_observed_unknown_usage(engine, tmp_path):
    profiles, _, _, _ = _freeze(engine)
    row, ledger = _task(engine, tmp_path, profiles[0], "first")
    ledger.reserve("first", "known", "model", reserved_micros=100)
    _known(engine, row, "known", 10, input_tokens=17, output_tokens=3)
    ledger.reserve("first", "partial", "model", reserved_micros=100)
    engine.harness.save_model_result("first", row["epoch"], "partial", ModelResponse(
        usage=ModelUsage(known=False, input_tokens=11, output_tokens=2),
    ))
    totals = engine.harness.get_experiment("h12-test")
    assert totals["usage"] == {"input_tokens": 28, "output_tokens": 5, "unknown_calls": 1}
    assert totals["settled_micros"] == 10 and totals["reserved_micros"] == 100


def test_frozen_experiment_and_profile_cannot_be_rebound_by_request_data(engine, tmp_path):
    profiles, _, config, authorization = _freeze(engine)
    with pytest.raises(DomainError, match="Frozen experiment"):
        engine.harness.create_experiment("h12-test", {**config, "candidate": "changed"},
                                         authorization)
    profile = profiles[0].model_copy(update={"objective": "An easier objective"})
    with pytest.raises(DomainError, match="Profile is not frozen"):
        _task(engine, tmp_path, profile, "forged")
    with pytest.raises(DomainError, match="H12 authorization"):
        engine.harness.create_experiment("more-money", config,
                                         {**authorization, "max_cost_micros": 24_000_000})


def test_concurrent_runs_cannot_overreserve_shared_authority(engine, tmp_path):
    profiles, _, _, _ = _freeze(engine)
    ledgers = [_task(engine, tmp_path, profiles[index], f"task-{index}")[1] for index in range(3)]

    def reserve(index):
        try:
            ledgers[index].reserve(f"task-{index}", "one", "model", reserved_micros=6_000_000)
            return "reserved"
        except DomainError as exc:
            return exc.code

    with ThreadPoolExecutor(max_workers=3) as pool:
        outcomes = list(pool.map(reserve, range(3)))
    assert outcomes.count("reserved") == 2 and outcomes.count("BUDGET_EXCEEDED") == 1
    assert engine.harness.get_experiment("h12-test")["reserved_micros"] == 12_000_000


def test_shared_authority_limits_are_immutable_in_postgresql(engine):
    _, experiment, _, _ = _freeze(engine)
    with pytest.raises(psycopg.errors.RaiseException, match="immutable"):
        with engine.transaction() as conn:
            conn.execute("UPDATE app.authorization_budgets SET limit_micros=24000000 "
                         "WHERE profile_hash=%s", (experiment["profile_hash"],))
    assert engine.harness.get_experiment("h12-test")["limit_micros"] == 12_000_000


def test_h12_call_ceiling_does_not_expand_legacy_authorization(engine):
    with pytest.raises(psycopg.errors.CheckViolation):
        with engine.transaction() as conn:
            conn.execute("INSERT INTO app.authorization_budgets(profile_hash,limit_micros,"
                         "call_limit) VALUES ('legacy',1,101)")


def test_raw_profile_dictionary_cannot_grant_shared_budget_authority(engine, tmp_path):
    profiles, _, _, _ = _freeze(engine)
    profile = profiles[0]
    with pytest.raises(DomainError, match="Trusted evaluation profile"):
        engine.harness.create(
            "untrusted", {"workspace_id": "untrusted", "repository": "local/h12"},
            {"project_id": "untrusted", "experiment_id": "h12-test"},
            {"actor": "owner", "workspace_id": "untrusted", "repository": "local/h12"},
            profile.resources.budget_limits().model_dump(mode="json"),
            evaluation_profile=profile.model_dump(mode="json"),
        )


def test_independent_judge_seal_blocks_future_tools_models_and_resume(engine, tmp_path):
    profiles, _, _, _ = _freeze(engine)
    row, ledger = _task(engine, tmp_path, profiles[0], "first")
    engine.harness.record_tree("first", row["epoch"], "a" * 64)
    engine.harness.seal_evaluation("first", row["epoch"], "a" * 64)
    with pytest.raises(DomainError, match="sealed this tree"):
        ledger.reserve("first", "later", "model", reserved_micros=0)
    with pytest.raises(DomainError, match="sealed this tree"):
        engine.harness.start_operation("first", row["epoch"], "later", "tool", "b" * 64)
    engine.harness.pause("first")
    with pytest.raises(DomainError, match="sealed this tree"):
        engine.harness.claim("first")


def test_completed_evaluation_requires_seal_but_final_commit_accepts_it(engine, tmp_path):
    profiles, _, _, _ = _freeze(engine)
    row, _ = _task(engine, tmp_path, profiles[0], "first")
    engine.harness.record_tree("first", row["epoch"], "a" * 64)
    content_hash = engine.artifacts.put(json.dumps({
        "task_id": "first", "run_id": str(row["run_id"]),
        "evaluation_profile": row["evaluation_profile"],
        "status": "completed", "candidate_status": "completed", "tree_after": "a" * 64,
        "independent_gate": {"status": "PASS", "tree_hash": "a" * 64},
    }).encode())
    with pytest.raises(DomainError, match="judge seal required"):
        engine.harness.finish("first", row["epoch"], "completed", content_hash, "a" * 64)
    engine.harness.seal_evaluation("first", row["epoch"], "a" * 64)
    assert engine.harness.finish("first", row["epoch"], "completed", content_hash,
                                 "a" * 64)["status"] == "succeeded"


@pytest.mark.parametrize("verdict,tree", [("FAIL", "a" * 64), ("PASS", "b" * 64), ("PASS", None)])
def test_seal_alone_cannot_commit_an_unverified_evaluation(engine, tmp_path, verdict, tree):
    profiles, _, _, _ = _freeze(engine)
    row, _ = _task(engine, tmp_path, profiles[0], "first")
    engine.harness.record_tree("first", row["epoch"], "a" * 64)
    engine.harness.seal_evaluation("first", row["epoch"], "a" * 64)
    content_hash = engine.artifacts.put(json.dumps({
        "task_id": "first", "run_id": str(row["run_id"]),
        "evaluation_profile": row["evaluation_profile"],
        "status": "completed", "candidate_status": "completed", "tree_after": "a" * 64,
        "independent_gate": {"status": verdict, "tree_hash": tree},
    }).encode())
    with pytest.raises(DomainError, match="Independent judge receipt"):
        engine.harness.finish("first", row["epoch"], "completed", content_hash, "a" * 64)
    assert engine.harness.get("first")["status"] == "running"


def test_v2_revision_reuses_original_budget_authority(engine, tmp_path):
    """A new revision and experiment cannot mint a second H12 budget."""
    profiles, original, config, approval = _freeze(engine, "original-matrix")
    first, ledger = _task(engine, tmp_path, profiles[0], "v1-first")
    ledger.reserve("v1-first", "one", "model", reserved_micros=1_000_000)
    _known(engine, first, "one", 1_000_000)
    new_config = {
        **config,
        "environment_revision": "h12-benchmark-v2",
        "profiles": [
            {**profile, "experiment_id": "v2-matrix"} for profile in config["profiles"]
        ],
    }
    revised = engine.harness.create_experiment("v2-matrix", new_config, approval)
    assert revised["profile_hash"] == original["profile_hash"]
    assert revised["settled_micros"] == 1_000_000
    assert revised["used_calls"] == 1
    with engine.transaction() as conn:
        count = conn.execute(
            "SELECT count(*) AS n FROM app.authorization_budgets WHERE profile_hash=%s",
            (original["profile_hash"],),
        ).fetchone()
    assert count["n"] == 1
