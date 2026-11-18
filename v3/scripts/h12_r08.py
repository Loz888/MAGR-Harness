"""R08 zero-model preparation and explicit first-sample dispatch on the original H12 DB."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from types import SimpleNamespace

from h12_eval import STATE, load_settings

from mgar_v3.coordination.engine import Engine
from mgar_v3.evidence.artifacts import ArtifactStore
from mgar_v3.quality_eval.h12_r08 import R08Runner
from mgar_v3.settings import Settings

ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--candidate", type=Path, required=True)
    parser.add_argument("--candidate-sha256", required=True)
    parser.add_argument("--experiment-id", required=True)
    parser.add_argument("--evidence", type=Path, required=True)
    parser.add_argument("--split", choices=("dev", "holdout"), default="dev")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--prepare-readiness", action="store_true")
    mode.add_argument("--initialize-experiment", action="store_true")
    mode.add_argument("--case-index", type=int, choices=range(1, 13))
    mode.add_argument("--development-barrier", action="store_true")
    mode.add_argument("--audit-development", action="store_true")
    parser.add_argument("--execute-live", action="store_true")
    parser.add_argument("--only-task", action="append")
    args = parser.parse_args()
    if args.execute_live != (args.case_index is not None):
        parser.error("Only an explicit case dispatch may enable live execution")
    if args.only_task and not args.prepare_readiness:
        parser.error("A readiness subset grants no paid execution")
    engine = None
    try:
        # Never initialize or replace the H12 budget identity here.
        binding = json.loads((STATE / "budget-database-binding.json").read_bytes())
        settings = load_settings(Path(binding["settings_file"]))
        artifacts = ArtifactStore(settings.artifact_root)
        if args.prepare_readiness:
            holder = SimpleNamespace(harness=None, artifacts=artifacts)
        else:
            engine = Engine(settings, artifacts)
            holder = engine
        key = None
        if args.execute_live:
            configured = Settings()
            key = (
                configured.deepseek_api_key.get_secret_value()
                if configured.deepseek_api_key is not None
                else None
            )
        runner = R08Runner(
            holder,
            experiment_id=args.experiment_id,
            evidence=args.evidence.resolve(),
            candidate=(args.candidate.resolve(), args.candidate_sha256),
            api_key=key,
            execute_live=args.execute_live,
            root=ROOT,
        )
        if args.prepare_readiness:
            report = runner.prepare_readiness(
                args.split, args.evidence.resolve() / "readiness", only=args.only_task)
            print(
                json.dumps(
                    {
                        "status": report["status"],
                        "ready_count": report["ready_count"],
                        "new_paid_calls": 0,
                        "quality_status": "NOT_RUN",
                    }
                )
            )
            return 0 if report["status"] == "PASS" else 2
        if args.initialize_experiment:
            runner.initialize()
            print(json.dumps({"status": "EXPERIMENT_FROZEN", "new_paid_calls": 0}))
            return 0
        if args.development_barrier or args.audit_development:
            result = (
                runner.write_barrier() if args.development_barrier else runner.audit_development()
            )
            print(
                json.dumps(
                    {
                        "status": result["status"],
                        "dev_pass": result["dev_pass"],
                        "denominator": 12,
                        "new_paid_calls": 0,
                    }
                )
            )
            return 0 if result["status"] == "PASS" else 1
        task = runner._tasks(args.split)[args.case_index - 1]
        score = runner.evaluate_task(task, "candidate")
        print(
            json.dumps(
                {
                    k: score.get(k)
                    for k in (
                        "status",
                        "reason",
                        "model_calls",
                        "tool_calls",
                        "settled_cost_micros",
                        "usage_known",
                        "reserved_micros",
                        "regression_evidence_known",
                        "new_regression_failures",
                        "independent_judge_status",
                    )
                }
            )
        )
        return 0 if score["status"] == "PASS" else 1
    except Exception as exc:
        # Never expose credentials, private task text, provider bodies or answers.
        print(
            json.dumps(
                {
                    "status": "BLOCKED",
                    "error_type": type(exc).__name__,
                    "error_code": getattr(exc, "code", "H12_R08_CONTROLLER_ERROR"),
                }
            )
        )
        return 2
    finally:
        if engine is not None:
            engine.close()


if __name__ == "__main__":
    raise SystemExit(main())
