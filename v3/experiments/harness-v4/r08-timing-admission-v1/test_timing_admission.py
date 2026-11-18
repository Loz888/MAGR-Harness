from datetime import UTC, datetime, timedelta

import pytest
import timing_admission as admission

from mgar_v3.domain import DomainError
from mgar_v3.quality_eval.h12_r08 import reference
from mgar_v3.quality_eval.h12_runner import private_json


def test_independent_clocks_bound_the_original_measurement():
    start = datetime(2026, 1, 1, tzinfo=UTC)
    end = start + timedelta(seconds=10)
    first = start + timedelta(seconds=2)
    last = end - timedelta(seconds=1)
    for measured in (7, 8.25, 10):
        assert admission.checked_elapsed(measured, start, end, first, last) == measured


@pytest.mark.parametrize("measured", [0, -1, True, None, float("nan"), float("inf"), 6.99, 10.01])
def test_invalid_or_out_of_bounds_record_is_rejected(measured):
    start = datetime(2026, 1, 1, tzinfo=UTC)
    first, last, end = (start + timedelta(seconds=i) for i in (1, 8, 10))
    with pytest.raises(DomainError):
        admission.checked_elapsed(measured, start, end, first, last)


@pytest.mark.parametrize("change", ["naive", "early", "late", "reversed"])
def test_invalid_independent_clock_evidence_is_rejected(change):
    start = datetime(2026, 1, 1, tzinfo=UTC)
    end = start + timedelta(seconds=10)
    first = start + timedelta(seconds=1)
    last = end - timedelta(seconds=1)
    if change == "naive":
        first = first.replace(tzinfo=None)
    elif change == "early":
        first = start - timedelta(seconds=1)
    elif change == "late":
        last = end + timedelta(seconds=1)
    else:
        first, last = last, first
    with pytest.raises(DomainError):
        admission.checked_elapsed(9, start, end, first, last)


@pytest.mark.parametrize(
    "change",
    [
        "none",
        "different_reviewer",
        "different_candidate",
        "changed_file",
        "fail",
        "other_fields",
        "past_holdout",
        "past_refusal",
    ],
)
def test_independent_attestation_binds_existing_evidence(tmp_path, monkeypatch, change):
    def save(name, value):
        path = tmp_path / name
        private_json(path, value)
        return reference(tmp_path, path)

    evaluator = tmp_path / "evaluator.py"
    evaluator.write_text("# fixture")
    monkeypatch.setattr(admission, "__file__", str(evaluator))
    candidate = {"sampling": "first", "thresholds": {"dev": 10}, "source_sha256": "a" * 64}
    candidate_ref = save("candidate.json", candidate)
    refusal = save(
        "refusal.json",
        {
            "status": "BLOCKED_AT_FROZEN_BARRIER",
            "candidate": candidate_ref,
            "holdout_model_calls": 0,
            "original_barrier_emitted": False,
        },
    )
    proposal = save(
        "proposal.json",
        {
            "schema": "h12-r08-timing-proposal-v1",
            "experiment_id": "fixture",
            "candidate": candidate_ref,
            **candidate,
            "evaluator": reference(tmp_path, evaluator),
            "holdout_at_proposal": 0,
            "original_refusal": refusal,
            "reviewer_id": "independent-fixture",
        },
    )
    review_data = {
        "schema": "h12-r08-independent-timing-review-v1",
        "status": "APPROVED",
        "reviewer": "independent-fixture",
        "proposal_sha256": proposal["sha256"],
        "candidate_sha256": candidate_ref["sha256"],
        "experiment_id": "fixture",
        "model_calls_for_review": 0,
        "holdout_samples_at_review": 0,
        "verified_rows": 12,
        "other_scoring_differences": 0,
        "preserved_original_refusal": True,
    }
    changes = {
        "different_reviewer": {"reviewer": "unbound-fixture"},
        "different_candidate": {"candidate_sha256": "0" * 64},
        "fail": {"status": "REJECTED"},
        "other_fields": {"other_scoring_differences": 1},
        "past_holdout": {"holdout_samples_at_review": 1},
        "past_refusal": {"preserved_original_refusal": False},
    }
    review = save("review.json", review_data | changes.get(change, {}))
    runner = object.__new__(admission.ReviewedTimingAdmission)
    runner.root, runner.candidate, runner.candidate_ref = tmp_path, candidate, candidate_ref
    runner.experiment_id = "fixture"
    runner.amendment_ref = save(
        "amendment.json",
        {
            "schema": "h12-r08-reviewed-timing-admission-v1",
            "proposal": proposal,
            "independent_review": review,
        },
    )
    if change == "changed_file":
        evaluator.write_text("# replacement")
    if change == "none":
        _, reviewed = runner.reviewed_inputs()
        assert reviewed == review_data
    else:
        with pytest.raises(DomainError):
            runner.reviewed_inputs()


@pytest.mark.parametrize("split,arm", [("dev", "candidate"), ("holdout", "legacy-v35")])
def test_timing_amendment_refuses_development_resampling_and_other_arms(split, arm):
    runner = object.__new__(admission.ReviewedTimingAdmission)
    with pytest.raises(DomainError):
        runner.evaluate_task({"split": split}, arm)
