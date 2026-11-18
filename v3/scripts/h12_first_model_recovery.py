"""Explicit, separately frozen diagnostics for primary cases with no model Run."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from h12_eval import STATE, load_settings

from mgar_v3.coordination.engine import Engine
from mgar_v3.evidence.artifacts import ArtifactStore
from mgar_v3.quality_eval.h12_first_model_recovery import FirstModelRecoveryRunner
from mgar_v3.settings import Settings

ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--settings", type=Path, default=STATE / "settings.json")
    parser.add_argument("--recovery-freeze", type=Path, required=True)
    parser.add_argument("--recovery-freeze-sha256", required=True)
    parser.add_argument("--experiment-id", required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--initialize-experiment", action="store_true")
    parser.add_argument("--execute-live", action="store_true")
    parser.add_argument("--task-id")
    args = parser.parse_args()
    if args.initialize_experiment and (args.execute_live or args.task_id):
        parser.error("Initialize alone; live execution requires a separate explicit selected case")
    if not args.initialize_experiment and (not args.execute_live or not args.task_id):
        parser.error("Choose initialization or one explicit live recovery case")
    runner_args: dict[str, Any] = {
        "experiment_id": args.experiment_id,
        "evidence": args.out.resolve(),
        "recovery_path": args.recovery_freeze.resolve(),
        "recovery_sha256": args.recovery_freeze_sha256,
        "root": ROOT,
    }
    try:
        if (
            hashlib.sha256(args.recovery_freeze.read_bytes()).hexdigest()
            != args.recovery_freeze_sha256
        ):
            raise ValueError("Recovery authority bytes changed")
        prepared = FirstModelRecoveryRunner(
            SimpleNamespace(harness=None),
            api_key=None,
            **runner_args,
        )
        prepared._require_public_development_frozen()
        settings = load_settings(args.settings.resolve())  # Existing bound shared database only.
        key = Settings().deepseek_api_key if args.execute_live else None
        engine = Engine(settings, ArtifactStore(settings.artifact_root))
        try:
            runner = FirstModelRecoveryRunner(
                engine,
                api_key=key.get_secret_value() if key else None,
                execute_live=args.execute_live,
                **runner_args,
            )
            if args.initialize_experiment:
                runner.initialize()
                print(
                    json.dumps(
                        {
                            "status": "RECOVERY_FROZEN",
                            "paid_calls": 0,
                            "quality_status": "DIAGNOSTIC_ONLY",
                        }
                    )
                )
                return 0
            report = runner.evaluate(split="holdout", arm="candidate", task_id=args.task_id)
            print(
                json.dumps(
                    {
                        "status": "DIAGNOSTIC_RECORDED",
                        "quality_status": "DIAGNOSTIC_ONLY",
                        "summary": report["summary"],
                        "shared_budget": report["shared_budget"],
                    }
                )
            )
            return 0
        finally:
            engine.close()
    except Exception as exc:
        # Do not expose provider credentials, DSNs, private requests or raw errors.
        print(
            json.dumps(
                {
                    "status": "BLOCKED",
                    "error_type": type(exc).__name__,
                    "error_code": getattr(exc, "code", "H12_RECOVERY_CONTROLLER_ERROR"),
                }
            )
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
