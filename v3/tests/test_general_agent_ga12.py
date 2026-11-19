"""GA12 frozen benchmark, isolation and score regression tests."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from mgar_v3.quality_eval.ga12_benchmark import BenchmarkError, freeze, run
from mgar_v3.quality_eval.ga12_contracts import Case, Domains, Freeze
from mgar_v3.quality_eval.ga12_fixture import Effect, execute
from mgar_v3.quality_eval.ga12_grader import grade

SUITE = Path(__file__).resolve().parents[1] / "experiments/general-agent/ga12"
DEV = SUITE / "dev.jsonl"
DEV_FREEZE = SUITE / "dev.freeze.json"


def dev_case(identifier: str) -> Case:
    for line in DEV.read_bytes().splitlines():
        case = Case.model_validate_json(line)
        if case.id == identifier:
            return case
    raise KeyError(identifier)


def test_frozen_public_bundle_has_six_domains_and_blind_inputs() -> None:
    manifest = Freeze.model_validate_json(DEV_FREEZE.read_bytes())
    assert freeze(DEV, "dev") == manifest
    assert manifest.counts == {domain: 10 for domain in Domains}
    case = dev_case("dev-cross-domain-01")
    assert "expected" not in case.public_input().model_dump()
    assert not hasattr(case.public_input(), "expected")


def test_evaluation_preserves_baseline_raw_evidence_and_ablation_rows(tmp_path: Path) -> None:
    out = tmp_path / "local-dev"
    report = run(DEV_FREEZE, DEV, out)
    assert report["verdict"] == "PASS"
    assert {arm: metrics["all"]["passed"] for arm, metrics in report["arms"].items()} == {
        "multi_domain": 60,
        "no_planner": 0,
        "no_memory": 57,
        "no_reranker": 54,
        "single_domain": 52,
    }
    baseline = report["arms"]["multi_domain"]
    assert {domain: row["passed"] for domain, row in baseline["domains"].items()} == {
        domain: 10 for domain in Domains
    }
    assert baseline["all"]["usage"]["model_calls"] == 0
    assert baseline["all"]["usage"]["real_external_writes"] == 0
    assert baseline["all"]["policy_violations"] == 0
    assert baseline["all"]["accepted_fixture_effects"] == 6
    assert baseline["all"]["external_effects_correct"] == 60
    assert report["arms"]["no_planner"]["all"]["task_success"] == 52
    assert report["arms"]["no_reranker"]["all"]["evidence_quality"] == 54
    assert report["arms"]["single_domain"]["domains"]["cross-domain"]["passed"] == 2
    raw = [json.loads(line) for line in (out / "raw.jsonl").read_text().splitlines()]
    assert len(raw) == 300
    assert sum(row["passed"] for row in raw) == 223
    assert all(row["latency_ns"] > 0 for row in raw)
    assert {(row["actual"]["reason"]) for row in raw if not row["passed"]} >= {
        "scoped_memory_unavailable",
        "incomplete_workflow",
    }
    with pytest.raises(FileExistsError):
        run(DEV_FREEZE, DEV, out)


@pytest.mark.parametrize("mode", ["real-provider", "real-external"])
def test_live_modes_are_reported_separately_without_execution(tmp_path: Path, mode) -> None:
    report = run(DEV_FREEZE, DEV, tmp_path / mode, mode=mode)
    assert report["verdict"] == "NOT_RUN"
    assert report["arms"] == {}
    assert not (tmp_path / mode / "raw.jsonl").exists()


def test_drift_of_frozen_input_is_rejected_before_any_output(tmp_path: Path) -> None:
    candidate = tmp_path / "candidate.jsonl"
    candidate.write_bytes(DEV.read_bytes().replace(b"Use the project", b"Use a project"))
    assert candidate.read_bytes() != DEV.read_bytes()
    with pytest.raises(BenchmarkError, match="differs from the frozen SHA-256"):
        run(DEV_FREEZE, candidate, tmp_path / "blocked")
    assert not (tmp_path / "blocked").exists()


def test_duplicate_cases_and_public_holdout_input_are_rejected(tmp_path: Path) -> None:
    duplicate = tmp_path / "duplicate.jsonl"
    source = DEV.read_bytes()
    duplicate.write_bytes(source + source.splitlines(keepends=True)[0])
    with pytest.raises(BenchmarkError, match="must be unique"):
        freeze(duplicate, "dev")
    with pytest.raises(BenchmarkError, match="outside the repository"):
        freeze(DEV, "holdout")
    copied_dev = tmp_path / "copied-dev.jsonl"
    copied_dev.write_bytes(source)
    with pytest.raises(BenchmarkError, match="ineligible for holdout"):
        freeze(copied_dev, "holdout")


def test_holdout_manifest_requires_corresponding_author_role() -> None:
    manifest = Freeze.model_validate_json(DEV_FREEZE.read_bytes())
    with pytest.raises(ValidationError, match="independent author"):
        Freeze.model_validate(manifest.model_dump() | {"split": "holdout"})


def test_grader_rejects_replaced_coding_deliverable(tmp_path: Path) -> None:
    case = dev_case("dev-coding-02")
    observation = execute(case.public_input(), "multi_domain", tmp_path)
    assert grade(case, observation, tmp_path).passed
    (tmp_path / "workspace/fixture.txt").write_text("Tampered result", encoding="utf-8")
    rerated = grade(case, observation, tmp_path)
    assert rerated.task_success
    assert not rerated.evidence_quality
    assert not rerated.passed


def test_grader_rejects_missing_source_bytes(tmp_path: Path) -> None:
    case = dev_case("dev-cross-domain-01")
    observation = execute(case.public_input(), "multi_domain", tmp_path)
    assert grade(case, observation, tmp_path).passed
    key = observation.evidence[0].artifact_sha256
    (tmp_path / "cas" / key[:2] / key).unlink()
    assert not grade(case, observation, tmp_path).evidence_quality


def test_policy_violation_and_external_effect_error_are_distinct(tmp_path: Path) -> None:
    case = dev_case("dev-office-04")
    actual = execute(case.public_input(), "multi_domain", tmp_path)
    expected_grade = grade(case, actual, tmp_path)
    assert expected_grade.passed
    assert not expected_grade.policy_violations
    bad_effects = (Effect(action_id="spurious-effect", message_sha256="a" * 64),)
    breach = grade(case, actual.model_copy(update={"effects": bad_effects}), tmp_path)
    assert breach.policy_violations == ("effect_outside_authority",)
    assert not breach.external_effects_correct
    assert breach.task_success


def test_lost_ack_reports_unknown_without_writing_again(tmp_path: Path) -> None:
    case = dev_case("dev-office-03")
    actual = execute(case.public_input(), "multi_domain", tmp_path)
    assert actual.status == "unknown"
    assert len(actual.effects) == 1
    assert actual.evidence == ()
    assert actual.trace.count("fixture_provider_accept") == 1
    assert actual.trace.count("fixture_provider_lookup") == 1
    assert grade(case, actual, tmp_path).passed


def test_partial_grant_preserves_only_authorized_evidence(tmp_path: Path) -> None:
    case = dev_case("dev-cross-domain-04")
    actual = execute(case.public_input(), "multi_domain", tmp_path)
    assert actual.status == "denied"
    assert actual.executed_tools == ("research.read",)
    assert tuple(proof.id for proof in actual.evidence) == ("original",)
    assert grade(case, actual, tmp_path).passed


def test_coding_preserves_exact_line_endings_and_deliverable_bytes(tmp_path: Path) -> None:
    # Mixed line separators, Unicode, and a literal private use code point must
    # round-trip without introducing a second matching hunk.
    case = Case.model_validate(
        dev_case("dev-coding-02").model_dump()
        | {
            "initial_state": {
                "text": "first\r\n𠮷: old\rthird\nprivate: \ue000\n",
                "old": "\r\n𠮷: old\r",
                "new": "\n𠮷: revised\r\n",
                "memory_hint": False,
            },
            "expected": {
                "status": "completed",
                "result": "first\n𠮷: revised\r\nthird\nprivate: \ue000\n",
                "plan": ["coding.patch"],
                "executed_tools": ["coding.patch"],
                "evidence_ids": ["coding-output"],
                "effects": 0,
            },
        }
    )
    actual = execute(case.public_input(), "multi_domain", tmp_path)
    assert grade(case, actual, tmp_path).passed
    assert (tmp_path / "workspace/fixture.txt").read_bytes() == (
        "first\n𠮷: revised\r\nthird\nprivate: \ue000\n".encode()
    )
