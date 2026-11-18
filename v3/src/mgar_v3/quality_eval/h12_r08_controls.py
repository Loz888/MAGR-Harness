"""Verify actual private Judge controls without exporting their contents."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from mgar_v3.domain import DomainError, digest
from mgar_v3.harness.broker import expected_environment_hash, issue_lease
from mgar_v3.harness.contracts import ExecutionProvenance
from mgar_v3.harness.project_commands import parse_tests, test_project_kind
from mgar_v3.harness.regression_types import RegressionCommand
from mgar_v3.harness.semantic_types import SemanticRevision
from mgar_v3.harness.test_results import parse_independent_test


def validate_controls(root: Path, bundle: Mapping[str, Any]) -> None:
    from mgar_v3.quality_eval.h12_r08 import asset, document

    def denied() -> DomainError:
        return DomainError(
            "H12_R08_JUDGE_CONTROLS_REQUIRED",
            "Independent original/positive/negative Judge evidence differs",
            409,
        )

    control = document(root, bundle["document"]["controls"])
    tasks = {t["task_id"]: t for t in bundle["tasks"]}
    if (
        control.get("status") != "PASS"
        or control.get("passed_tasks") != 12
        or set(control.get("tasks", {})) != set(tasks)
    ):
        raise denied()
    for name, task in tasks.items():
        row = control["tasks"][name]
        revision: SemanticRevision = bundle["revisions"][name]
        lock = bundle["locks"][task["repository"] + "@" + task["base_commit"]]
        expected_image = json.loads(lock.read_bytes())["image_id"]
        from mgar_v3.quality_eval.h12_runner import (
            DEV_ENVIRONMENT_V4,
            HOLDOUT_INFRA_V4,
            profile_for,
        )

        profile = profile_for(
            task,
            "r08-control-review",
            "candidate",
            environment_revision=(
                DEV_ENVIRONMENT_V4 if task["split"] == "dev" else HOLDOUT_INFRA_V4
            ),
        )
        # No executable commands; only derive the original Broker's environment projection.
        expected_lease = issue_lease(
            mode="investigate",
            actor="h12-verifier",
            workspace_id="r08-control-review",
            repository=task["repository"],
            execution_epoch=1,
            allowed_commands=(),
            offline_network=profile.sandbox_offline_network,
        )
        expected_env = expected_environment_hash(expected_lease)
        baseline = document(root, row["baseline"])
        if (
            baseline.get("status") != "PASS"
            or baseline.get("task_id") != name
            or baseline.get("tree_hash")
            != bundle["document"]["per_task"][name]["original_tree_sha256"]
            or baseline.get("baseline_method") != "r06-public-task-type-policy"
            or baseline.get("baseline_assessment", {}).get("policy_hash")
            != revision.policy.policy_hash
        ):
            raise denied()
        if not row.get("positive") or not row.get("negative"):
            raise denied()
        for kind in ("positive", "negative"):
            for ref in row[kind]:
                result_path = asset(root, ref)
                result = document(root, ref)
                expected = "PASS" if kind == "positive" else "FAIL"
                if (
                    result.get("status") != expected
                    or result.get("phase") != "final"
                    or result.get("task_id") != name
                    or result.get("judge_sha256") != task["judge_sha256"]
                    or result.get("judge_registry_sha256") != revision.registry_sha256
                    or result.get("judge_traceability_sha256") != revision.traceability_sha256
                    or result.get("baseline_policy_hash") != revision.policy.policy_hash
                    or result.get("image_id") != expected_image
                    or result.get("source_tree_hash") != result.get("tree_hash")
                    or result.get("judge_review", {}).get("status") != "PASS"
                ):
                    raise denied()
                trace = result_path.parent / "traceability-manifest.json"
                if (
                    not trace.is_file()
                    or trace.is_symlink()
                    or hashlib.sha256(trace.read_bytes()).hexdigest()
                    != revision.traceability_sha256
                ):
                    raise denied()
                receipts = result.get("receipts", [])
                private = [r for r in receipts if r.get("phase") == "final"]
                if not private or not receipts:
                    raise denied()
                from mgar_v3.quality_eval.h12_judge_review import read_manifest

                manifest = read_manifest(trace, revision.traceability_sha256)
                if result.get("hashes") != manifest.files or manifest.command_hash != digest(
                    [r["argv"] for r in private]
                ):
                    raise denied()
                failures = 0
                for receipt in receipts:
                    log = Path(receipt["private_log"])
                    if (
                        not log.resolve().is_relative_to(result_path.parent.resolve())
                        or any(p.is_symlink() for p in (log, *log.parents))
                        or not log.is_file()
                        or hashlib.sha256(log.read_bytes()).hexdigest() != receipt["log_sha256"]
                        or receipt.get("receipt_id")
                        != digest({k: v for k, v in receipt.items() if k != "receipt_id"})
                        or receipt.get("complete") is not True
                        or receipt.get("log_truncated") is not False
                        or receipt.get("source_tree_hash") != result["tree_hash"]
                        or receipt.get("image_id") != result["image_id"]
                        or receipt.get("environment_hash") != expected_env
                        or receipt.get("tree_hash") != receipt.get("verifier_tree_hash")
                        or receipt.get("verifier_tree_hash")
                        != (
                            result["verifier_tree_hash"]
                            if receipt.get("phase") == "final"
                            else result["tree_hash"]
                        )
                        or receipt.get("state") not in {"exited", "failed"}
                        or receipt.get("exit_code") not in {0, 1}
                    ):
                        raise denied()
                    provenance = ExecutionProvenance.model_validate(receipt["provenance"])
                    if (
                        provenance.actor != "h12-verifier"
                        or provenance.write_paths
                        or provenance.read_paths != ("",)
                        or provenance.repository != task["repository"].lower()
                        or provenance.operation_id != receipt.get("operation_id")
                        or provenance.execution_epoch != 1
                        or not provenance.lease_id
                        or not provenance.workspace_id
                    ):
                        raise denied()
                    text = log.read_text()
                    if receipt.get("phase") == "final":
                        argv = tuple(receipt["argv"])
                        parsed = parse_independent_test(
                            RegressionCommand(
                                suite_id="private-control",
                                public_argv=argv,
                                argv=argv,
                                framework="pytest",
                            ),
                            text,
                            receipt["exit_code"],
                            complete=True,
                        )
                        if (
                            not parsed.complete
                            or parsed.tests_failed is None
                            or not parsed.tests_run
                        ):
                            raise denied()
                        failures += parsed.tests_failed
                    elif kind == "positive":
                        status, count, _, _, _ = parse_tests(
                            test_project_kind(receipt["argv"], task["language"]),
                            text,
                            receipt["exit_code"],
                        )
                        if status != "passed" or count <= 0:
                            raise denied()
                if (failures > 0) != (kind == "negative"):
                    raise denied()
