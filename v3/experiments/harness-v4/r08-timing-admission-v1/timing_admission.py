"""Reviewed timing amendment for an existing R08 experiment's held-out admission.

The frozen worker, scoring function and PG experiment run unmodified. Only the
development audit supplies an independently bound measurement for the outer CLI
timer. The ordinary R08 audit still compares the entire score, elapsed time included.
"""

from __future__ import annotations

import argparse
import json
import math
from collections.abc import Mapping
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from time import time_ns
from typing import Any

from mgar_v3.coordination.engine import Engine
from mgar_v3.domain import DomainError
from mgar_v3.evidence.artifacts import ArtifactStore
from mgar_v3.harness.contracts import EvaluationProfile
from mgar_v3.quality_eval.h12_r08 import R08Runner, asset, document, reference, sha
from mgar_v3.quality_eval.h12_runner import private_json
from mgar_v3.settings import Settings
from scripts.h12_eval import STATE, load_settings

ROOT = Path(__file__).resolve().parents[3]


def require(condition: bool, message: str) -> None:
    if not condition:
        raise DomainError("H12_R08_TIMING_AMENDMENT_INVALID", message, 409)


def checked_elapsed(
    measured: Any,
    dispatch: datetime,
    finished: datetime,
    first: datetime,
    last: datetime,
) -> float:
    require(
        type(measured) in (int, float) and math.isfinite(measured) and measured > 0,
        "A live case requires a finite positive measured duration",
    )
    require(
        all(t.utcoffset() is not None for t in (dispatch, finished, first, last)),
        "All clock observations must carry time zones",
    )
    require(dispatch <= first <= last <= finished, "Model operations exceed the controller window")
    require(
        (last - first).total_seconds() <= measured <= (finished - dispatch).total_seconds(),
        "Elapsed time lies outside its independent clock bounds",
    )
    return float(measured)


class _DevelopmentClockAudit(R08Runner):
    """A read-only original development replay. It has no model key or dispatch authority."""

    timing_rows: dict[str, dict[str, Any]]

    def _score_payload(
        self,
        task: Mapping[str, Any],
        profile: EvaluationProfile,
        result: Mapping[str, Any],
    ) -> dict[str, Any]:
        canonical = super()._score_payload(task, profile, result)
        name = task["task_id"]
        row = self.timing_rows[name]
        score = document(self.root, row["score"])
        start = document(self.root, row["start"])
        end = document(self.root, row["finish"])
        expected_path = self.evidence / "candidate" / "dev" / name / "score.json"
        require(
            reference(self.root, expected_path) == row["score"],
            "Reviewed score is outside its original first sample",
        )
        require(
            start["candidate"] == self.candidate_ref
            and end["score"] == row["score"]
            and start["split"] == end["split"] == profile.split == "dev"
            and start["index"] == end["index"] == row["index"]
            and start["at"] == end["start"]
            and start["ledger"] == end["before"]
            and start["task_id"] == end["task_id"] == name
            and score["experiment_id"] == profile.experiment_id == self.experiment_id
            and score["run_id"] == result["run_id"] == canonical["run_id"]
            and score["final_tree"] == result["tree_after"] == canonical["final_tree"],
            "Controller, original run, tree or case identity differs",
        )
        require(
            end["result"]["status"] == canonical["status"]
            and end["result"]["model_calls"] == canonical["model_calls"],
            "Original controller verdict or model usage differs from PG",
        )
        with self.engine.transaction() as conn:
            clocks = conn.execute(
                "SELECT min(o.created_at) AS first, max(o.updated_at) AS last, count(*) AS n "
                "FROM app.harness_operations o JOIN app.usage_calls u ON u.call_id=o.model_call_id "
                "AND u.run_id=o.run_id WHERE o.run_id=%s AND o.state='known' AND u.status='known'",
                (canonical["run_id"],),
            ).fetchone()
        require(
            clocks is not None and 0 < clocks["n"] == canonical["model_calls"],
            "Settled original PG clock records are incomplete",
        )
        assert clocks is not None
        elapsed = checked_elapsed(
            score["wall_seconds"],
            datetime.fromisoformat(start["at"]),
            datetime.fromisoformat(end["finish"]),
            clocks["first"],
            clocks["last"],
        )
        return {**canonical, "wall_seconds": elapsed}


class ReviewedTimingAdmission(R08Runner):
    amendment_ref: dict[str, str]

    def reviewed_inputs(self) -> tuple[dict[str, Any], dict[str, Any]]:
        amendment = document(self.root, self.amendment_ref)
        proposal = document(self.root, amendment["proposal"])
        review = document(self.root, amendment["independent_review"])
        require(
            amendment.get("schema") == "h12-r08-reviewed-timing-admission-v1"
            and proposal.get("schema") == "h12-r08-timing-proposal-v1"
            and review.get("schema") == "h12-r08-independent-timing-review-v1",
            "Unrecognized timing amendment",
        )
        require(
            proposal["evaluator"] == reference(self.root, Path(__file__).resolve())
            and proposal["candidate"] == self.candidate_ref
            and proposal["experiment_id"] == self.experiment_id
            and proposal["source_sha256"] == self.candidate["source_sha256"]
            and proposal["sampling"] == self.candidate["sampling"]
            and proposal["thresholds"] == self.candidate["thresholds"]
            and proposal["holdout_at_proposal"] == 0,
            "Evaluator, source, experiment or approved protocol differs",
        )
        require(
            review.get("status") == "APPROVED"
            and isinstance(review.get("reviewer"), str)
            and bool(review["reviewer"].strip())
            and review["reviewer"] == proposal.get("reviewer_id")
            and review.get("proposal_sha256") == amendment["proposal"]["sha256"]
            and review.get("candidate_sha256") == self.candidate_ref["sha256"]
            and review.get("experiment_id") == self.experiment_id
            and review.get("model_calls_for_review") == review.get("holdout_samples_at_review") == 0
            and review.get("verified_rows") == 12
            and review.get("other_scoring_differences") == 0
            and review.get("preserved_original_refusal") is True,
            "Independent review does not bind this proposal before held-out exposure",
        )
        refusal = document(self.root, proposal["original_refusal"])
        require(
            refusal["status"] == "BLOCKED_AT_FROZEN_BARRIER"
            and refusal["candidate"] == self.candidate_ref
            and refusal["holdout_model_calls"] == 0
            and not refusal["original_barrier_emitted"],
            "Original refusal must remain immutable",
        )
        return proposal, review

    def audit_development(self, *, allow_reserved: bool = False) -> dict[str, Any]:
        proposal, review = self.reviewed_inputs()
        tasks = [t["task_id"] for t in self._tasks("dev")]
        rows = proposal["development"]
        require(
            len(rows) == len(tasks) == 12
            and [r["task_id"] for r in rows] == tasks
            and [r["index"] for r in rows] == list(range(1, 13)),
            "The complete original development population in frozen order is required",
        )
        replay = _DevelopmentClockAudit(
            self.engine,
            experiment_id=self.experiment_id,
            evidence=self.evidence,
            candidate=(asset(self.root, self.candidate_ref), self.candidate_ref["sha256"]),
            api_key=None,
            execute_live=False,
            root=self.root,
        )
        replay.timing_rows = {row["task_id"]: row for row in rows}
        verdict = replay.audit_development(allow_reserved=allow_reserved)
        require(
            verdict["scores"] == [r["score"] for r in rows]
            and verdict["dev_pass"] == review.get("dev_pass")
            and verdict["denominator"] - verdict["dev_pass"] == review.get("dev_fail"),
            "Original score hashes or recomputed verdict differ from independent review",
        )
        return {**verdict, "timing_amendment": self.amendment_ref}

    def write_barrier(self) -> dict[str, Any]:
        with self.engine.transaction() as conn:
            prior = conn.execute(
                "SELECT count(*) AS n FROM app.harness_tasks "
                "WHERE experiment_id=%s AND evaluation_profile->>'split'='holdout'",
                (self.experiment_id,),
            ).fetchone()
        require(prior is not None and prior["n"] == 0, "A held-out sample predates this amendment")
        return super().write_barrier()

    def validate_barrier(self, *, active_reservation: bool = False) -> dict[str, Any]:
        barrier = super().validate_barrier(active_reservation=active_reservation)
        require(
            barrier.get("timing_amendment") == self.amendment_ref,
            "Barrier belongs to a different timing amendment",
        )
        return barrier

    def evaluate_task(self, task: Mapping[str, Any], arm: str) -> dict[str, Any]:
        require(
            task["split"] == "holdout" and arm == "candidate",
            "The timing amendment admits only original held-out first samples",
        )
        return super().evaluate_task(task, arm)

    def _service_options(self, task: Mapping[str, Any], case: Path) -> dict[str, Any]:
        options = super()._service_options(task, case)
        original_guard = options["before_model_request"]

        def record_admission(request: Any) -> None:
            original_guard(request)
            suffix = sha256(request.operation_id.encode()).hexdigest()[:20]
            private_json(
                case / "timing-admission" / f"{suffix}-{time_ns()}.json",
                {
                    "amendment": self.amendment_ref,
                    "candidate": self.candidate_ref,
                    "barrier": reference(
                        self.root, self.evidence / "development-holdout-barrier.json"
                    ),
                    "run_id": str(request.task.run_id),
                    "operation_id": request.operation_id,
                    "recorded_at": datetime.now(UTC).isoformat(),
                },
            )

        return {**options, "before_model_request": record_admission}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--amendment", type=Path, required=True)
    parser.add_argument("--amendment-sha256", required=True)
    modes = parser.add_mutually_exclusive_group(required=True)
    modes.add_argument("--write-barrier", action="store_true")
    modes.add_argument("--holdout-case", type=int, choices=range(1, 13))
    parser.add_argument("--execute-live", action="store_true")
    args = parser.parse_args()
    if args.execute_live != (args.holdout_case is not None):
        parser.error("Paid dispatch requires an explicit held-out index")
    engine = None
    try:
        amendment = {
            "path": str(args.amendment.resolve().relative_to(ROOT)),
            "sha256": args.amendment_sha256,
        }
        spec = document(ROOT, amendment)
        proposed = document(ROOT, spec["proposal"])
        path = asset(ROOT, proposed["candidate"])
        original = asset(ROOT, proposed["original_refusal"])
        binding = json.loads((STATE / "budget-database-binding.json").read_bytes())
        settings = load_settings(Path(binding["settings_file"]))
        engine = Engine(settings, ArtifactStore(settings.artifact_root))
        key = Settings().deepseek_api_key if args.execute_live else None
        runner = ReviewedTimingAdmission(
            engine,
            experiment_id=proposed["experiment_id"],
            evidence=original.parent / "live",
            root=ROOT,
            candidate=(path, sha(path)),
            api_key=key.get_secret_value() if key else None,
            execute_live=args.execute_live,
        )
        runner.amendment_ref = amendment
        # The original Engine experiment must already own the prospective 24-case candidate.
        require(
            runner.store.get_experiment(runner.experiment_id)["config"]["candidate_manifest"]
            == runner.candidate_ref,
            "Original Engine does not own this frozen candidate",
        )
        if args.write_barrier:
            result = runner.write_barrier()
            print(json.dumps({k: result[k] for k in ("status", "dev_pass", "denominator")}))
        else:
            result = runner.evaluate_task(
                runner._tasks("holdout")[args.holdout_case - 1], "candidate"
            )
            fields = (
                "status",
                "reason",
                "model_calls",
                "tool_calls",
                "settled_cost_micros",
                "usage_known",
                "reserved_micros",
                "independent_judge_status",
                "independent_public_regression_status",
                "new_regression_failures",
            )
            print(json.dumps({k: result.get(k) for k in fields}))
        return 0 if result["status"] == "PASS" else 1
    except Exception as exc:
        print(
            json.dumps(
                {"status": "BLOCKED", "error_code": getattr(exc, "code", type(exc).__name__)}
            )
        )
        return 2
    finally:
        if engine is not None:
            engine.close()


if __name__ == "__main__":
    raise SystemExit(main())
