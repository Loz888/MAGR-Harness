#!/usr/bin/env python3
"""Command line for the scoped GA12 evaluation."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from mgar_v3.quality_eval.ga12_benchmark import BenchmarkError, run, write_freeze
from mgar_v3.quality_eval.ga12_contracts import Arms


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(required=True, dest="command")
    freeze_parser = commands.add_parser("freeze", help="Pin one public or private bundle")
    freeze_parser.add_argument("--bundle", type=Path, required=True)
    freeze_parser.add_argument("--split", choices=("dev", "holdout"), required=True)
    freeze_parser.add_argument("--out", type=Path, required=True)
    run_parser = commands.add_parser("run", help="Evaluate a frozen bundle")
    run_parser.add_argument("--freeze", type=Path, required=True)
    run_parser.add_argument("--bundle", type=Path, required=True)
    run_parser.add_argument("--out", type=Path, required=True)
    run_parser.add_argument(
        "--mode", choices=("mock", "real-provider", "real-external"), default="mock"
    )
    run_parser.add_argument("--arm", choices=Arms, action="append")
    args = parser.parse_args()
    try:
        if args.command == "freeze":
            manifest = write_freeze(args.bundle, args.split, args.out)
            print(manifest.model_dump_json())
            return 0
        report = run(
            args.freeze, args.bundle, args.out, mode=args.mode, arms=tuple(args.arm or Arms)
        )
    except BenchmarkError as exc:
        parser.exit(2, f"{exc}\n")
    except FileExistsError:
        parser.exit(2, "GA12 output already exists. Choose a new path.\n")
    except OSError:
        parser.exit(2, "GA12 failed to read or write a required local file.\n")
    public = {
        "split": report["freeze"]["split"],
        "verdict": report["verdict"],
        "mode": report["mode"],
        "arms": {name: arm["all"] for name, arm in report["arms"].items()},
    }
    print(json.dumps(public, sort_keys=True))
    return 1 if report["verdict"] == "FAIL" else 0


if __name__ == "__main__":
    raise SystemExit(main())
