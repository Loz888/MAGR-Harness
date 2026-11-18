"""Read historical public logs as diagnostics; no upgrade to R05 proof authority."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from mgar_v3.harness.project_commands import ProjectKind, parse_tests
from mgar_v3.harness.test_failures import failed_test_ids


def _legacy_log_diagnostics(
    case: Path,
    task: Mapping[str, Any],
    result: Mapping[str, Any],
) -> dict[str, Any]:
    """The model's narrow goal test cannot attest to the whole public suite."""
    unknown = {
        "new_regression_failures": None,
        "regression_evidence_known": False,
        "regression_evidence_status": "independent_public_unverified",
        "regression_evidence_scope": "independent_full_public",
    }
    final_path = case / "judge-private/judge-result.json"
    if not final_path.is_file():
        return unknown if (result.get("independent_gate") or {}).get("status") else {}
    final_tree, original_tree = result.get("tree_after"), result.get("tree_before")
    try:
        final = json.loads(final_path.read_bytes())
        if (
            final.get("task_id") != task["task_id"]
            or final.get("phase") != "final"
            or final.get("judge_sha256") != task["judge_sha256"]
            or final.get("tree_hash") != final_tree
        ):
            return unknown
        # The private verifier writes full public regressions on their own
        # source-only copy so private probes do not alter discovery/lint.
        public_root = (case / "judge-private/public-regressions").resolve()
        public = [
            row
            for row in final.get("receipts", [])
            if Path(row.get("private_log", "")).resolve().is_relative_to(public_root)
        ]
        if not public:
            return {
                **unknown,
                "independent_public_regression_status": "NOT_RUN",
            }
        baseline_path = case / "environment-private/judge-result.json"
        baseline = json.loads(baseline_path.read_bytes())
        if (
            baseline.get("task_id") != task["task_id"]
            or baseline.get("judge_sha256") != task["judge_sha256"]
            or baseline.get("phase") != "environment"
            or baseline.get("tree_hash") != original_tree
        ):
            return unknown
        kind: ProjectKind = (
            "python"
            if task["language"] == "python"
            else "go"
            if task["language"] == "go"
            else "node"
        )

        def observe(row: dict[str, Any], root: Path, tree: Any) -> dict[str, Any] | None:
            log = Path(row.get("private_log", "")).resolve()
            if (
                not log.is_relative_to(root.resolve())
                or not log.is_file()
                or row.get("tree_hash") != tree
                or row.get("cwd") != "."
                or row.get("complete") is not True
                or row.get("log_truncated") is not False
                or row.get("state") not in {"exited", "failed"}
                or row.get("exit_code") not in {0, 1}
            ):
                return None
            raw = log.read_bytes()
            if hashlib.sha256(raw).hexdigest() != row.get("log_sha256") or len(raw) != row.get(
                "log_bytes"
            ):
                return None
            output = raw.decode("utf-8", errors="replace")
            state, ran, failed, skipped, _ = parse_tests(kind, output, row["exit_code"])
            if (
                state not in {"passed", "failed"}
                or ran <= 0
                or skipped >= ran
                or failed > ran
                or row["exit_code"] != (0 if state == "passed" else 1)
            ):
                return None
            ids = failed_test_ids(row["argv"], output, failed) if failed else ()
            if ids is None or len(set(ids)) != failed:
                return None
            return {
                "failed": failed,
                "ids": set(ids),
                "ran": ran,
                "receipt": row["operation_id"],
                "log_sha256": row["log_sha256"],
            }

        before_ids: set[str] = set()
        after_ids: set[str] = set()
        introduced: set[str] = set()
        proofs = []
        baseline_verified = True
        for receipt in public:
            after = observe(receipt, public_root, final_tree)
            matches = [
                row
                for row in baseline.get("receipts", [])
                if row.get("argv") == receipt.get("argv")
            ]
            if after is None or len(matches) > 1:
                return unknown
            before = (
                observe(matches[0], case / "environment-private", original_tree)
                if matches
                else None
            )
            if before is None:
                # A new public Go API may intentionally stop compilation on
                # the original tree. A fully green final suite still proves
                # zero remaining/new failures, as in runtime metrics.
                baseline_verified = False
                if after["failed"]:
                    return {
                        **unknown,
                        "regression_final_failures": sorted(after["ids"]),
                        "independent_public_regression_status": "FAIL",
                    }
            elif before["failed"] and after["failed"] and before["ran"] != after["ran"]:
                return unknown
            if before is not None:
                before_ids.update(before["ids"])
            after_ids.update(after["ids"])
            introduced.update(after["ids"] - (before["ids"] if before else set()))
            proofs.append(
                {
                    "argv": receipt["argv"],
                    "baseline": {k: v for k, v in before.items() if k != "ids"} if before else None,
                    "final": {k: v for k, v in after.items() if k != "ids"},
                }
            )
        return {
            "new_regression_failures": len(introduced),
            "regression_evidence_known": True,
            "regression_evidence_status": "measured",
            "regression_evidence_scope": "independent_full_public",
            "regression_baseline_verified": baseline_verified,
            "regression_final_tree": final_tree,
            "regression_baseline_failures": sorted(before_ids),
            "regression_final_failures": sorted(after_ids),
            "regression_introduced_failures": sorted(introduced),
            "independent_public_regression_status": "FAIL" if after_ids else "PASS",
            "independent_public_regression_receipts": proofs,
        }
    except (KeyError, TypeError, ValueError, OSError):
        return unknown


def independent_regression_metrics(
    case: Path, task: Mapping[str, Any], result: Mapping[str, Any],
) -> dict[str, Any]:
    """Old reports lack complete frozen IDs and an original Engine/CAS anchor.

    Historical files remain unchanged. Verified old failure names are useful
    diagnostics, but summary-only logs cannot establish a zero/new-failure count.
    """
    from mgar_v3.quality_eval.h12_regression_scoring import unverified_metrics

    legacy = _legacy_log_diagnostics(case, task, result)
    value = unverified_metrics("Legacy receipts do not attest R05 complete public scope")
    if legacy.get("regression_evidence_known"):
        value.update(independent_public_regression_status="BLOCKED",
                     legacy_diagnostic_only=True,
                     legacy_observed_final_failures=legacy.get("regression_final_failures"))
    return value
