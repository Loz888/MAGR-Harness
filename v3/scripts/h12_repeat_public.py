"""Repeat fixed public dev probes; retain every attempt, never score a best run."""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

from mgar_v3.harness.broker import IsolatedExecutionBroker, issue_lease
from mgar_v3.harness.commands import CommandSession
from mgar_v3.harness.contracts import CapabilityGrant
from mgar_v3.harness.project_commands import parse_tests
from mgar_v3.quality_eval import h12, h12_judge
from mgar_v3.quality_eval.h12_runner import EvaluationRunner, private_json


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--suite", choices=("got-retry", "got-crypto", "got-full"), required=True)
    parser.add_argument("--repeats", type=int, default=5)
    parser.add_argument("--image-map", type=Path, required=True)
    parser.add_argument("--evidence", type=Path, required=True)
    parser.add_argument("--public-amendments", type=Path)
    parser.add_argument("--amendments-sha256")
    args = parser.parse_args()
    if not 1 <= args.repeats <= 20:
        parser.error("repeats must be between 1 and 20, fixed before any attempt")
    if bool(args.public_amendments) != bool(args.amendments_sha256):
        parser.error("amendment path and hash must be supplied together")
    args.evidence.mkdir(parents=True, exist_ok=False, mode=0o700)
    task = next(
        t for t in h12.load_frozen_tasks("dev") if t["task_id"] == "hv4-dev-got-header-value"
    )
    image = Path(
        json.loads(args.image_map.read_text())[task["repository"] + "@" + task["base_commit"]]
    )
    runner = EvaluationRunner(
        SimpleNamespace(harness=None),
        experiment_id="public-diagnostic",
        evidence=args.evidence / "materialization",
        image_locks={},
        api_key=None,
    )
    source = runner._source(task)
    if source is None:
        parser.error("the exact frozen dev source is required")
    workspace = runner._materialize(task, source)
    # Match the verifier's source-only copy. XO scans dot directories; mounting
    # a masked .git makes the probe fail before it executes any test.
    shutil.rmtree(workspace / ".git")
    amendment = None
    if args.public_amendments:
        from mgar_v3.quality_eval.h12_benchmark import apply_public_amendments

        try:
            amendment = apply_public_amendments(
                workspace,
                args.public_amendments,
                args.amendments_sha256,
                repository=task["repository"],
                commit=task["base_commit"],
            )
        except BaseException:
            shutil.rmtree(workspace.parent)
            raise
    tree = h12_judge.tree_hash(workspace)
    argv = ["npm", "test", "--", "--config=/opt/h12/ava-config.mjs"]
    if args.suite != "got-full":
        # npm's original script retains XO, tsc and the original TS loader.
        argv += (
            ["test/retry.ts", "--match=respects backoffLimit"]
            if args.suite == "got-retry"
            else ["test/https.ts", "--match=invalid key passphrase"]
        )
    private_json(
        args.evidence / "plan.json",
        {
            "suite": args.suite,
            "repeats": args.repeats,
            "argv": argv,
            "task_id": task["task_id"],
            "tree_hash": tree,
            "image_lock_sha256": hashlib.sha256(image.read_bytes()).hexdigest(),
            "all_attempts_reported": True,
            "paid_calls": 0,
            "benchmark_amendment": amendment,
            "scope": "targeted public diagnostics, not model or whole-benchmark scores",
        },
    )
    rows = []
    try:
        for index in range(args.repeats):
            case = args.evidence / f"attempt-{index + 1:02d}"
            case.mkdir(mode=0o700)
            lease = issue_lease(
                mode="investigate",
                actor="h12-diagnostic",
                workspace_id="diagnostic-" + uuid4().hex,
                repository=task["repository"],
                execution_epoch=1,
                allowed_commands=("npm",),
                timeout_seconds=300,
                max_output_bytes=1048576,
                memory_mb=2048,
                scratch_mb=256,
                nofile_limit=1024,
                offline_network="got-network-v1",
            )
            grant = CapabilityGrant(
                actor=lease.actor,
                workspace_id=lease.workspace_id,
                repository=lease.repository,
                read_paths=("",),
                write_paths=(),
                execute=True,
                allowed_commands=("npm",),
                max_seconds=300,
            )
            session = CommandSession(IsolatedExecutionBroker(workspace, lease, lock_path=image))
            try:
                receipt, output = h12_judge._run(session, grant, argv, 300, case, 0)
                status, executed, failed, skipped, detail = parse_tests(
                    "node", output, receipt["exit_code"]
                )
                row = {
                    "attempt": index + 1,
                    "status": status,
                    "executed": executed,
                    "failed": failed,
                    "skipped": skipped,
                    "detail": detail,
                    "exit_code": receipt["exit_code"],
                    "complete": receipt["complete"],
                    "diagnostics": receipt.get("diagnostics"),
                    "source_stable": h12_judge.tree_hash(workspace) == tree,
                }
                rows.append(row)
                private_json(case / "summary.json", row)
                print(
                    json.dumps({k: row[k] for k in ("attempt", "status", "executed", "failed")}),
                    flush=True,
                )
            finally:
                session.close()
    finally:
        shutil.rmtree(workspace.parent)
        private_json(
            args.evidence / "summary.json",
            {
                "planned": args.repeats,
                "completed": len(rows),
                "attempts": rows,
                "paid_calls": 0,
                "quality_score": "NOT_APPLICABLE",
                "all_passed": len(rows) == args.repeats
                and all(
                    r["status"] == "passed"
                    and r["executed"] > 0
                    and r["complete"]
                    and r["source_stable"]
                    for r in rows
                ),
            },
        )
    return (
        0
        if len(rows) == args.repeats
        and all(
            r["status"] == "passed" and r["executed"] > 0 and r["complete"] and r["source_stable"]
            for r in rows
        )
        else 2
    )


if __name__ == "__main__":
    raise SystemExit(main())
