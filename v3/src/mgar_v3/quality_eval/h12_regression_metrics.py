"""Advisory model-phase counts; never independent full-regression evidence.

The evaluator prefixes these fields with model_phase_. R05 authoritative
metrics are recomputed only from original Engine-selected CAS references.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from mgar_v3.harness.contracts import ExecutionReceipt
from mgar_v3.harness.test_failures import failed_test_ids


def regression_metrics(result: Mapping[str, Any], argv: Sequence[str]) -> dict[str, Any]:
    unknown = {'new_regression_failures': None, 'regression_evidence_known': False,
               'regression_evidence_status': 'missing_or_unverified'}
    if not argv:
        return {'new_regression_failures': 0, 'regression_evidence_known': True,
                'regression_evidence_status': 'not_applicable'}
    raw = result.get('receipts', [])
    if not isinstance(raw, (list, tuple)):
        return unknown

    def latest(tree: Any) -> ExecutionReceipt | None:
        if not isinstance(tree, str) or not tree:
            return None
        for item in reversed(raw):
            if (not isinstance(item, dict) or item.get('tree_hash') != tree
                    or item.get('purpose') != 'test' or item.get('argv') != list(argv)):
                continue
            try:
                receipt = ExecutionReceipt.model_validate(item)
            except ValueError:
                return None
            if (receipt.cwd != '.' or receipt.public_probe is not None
                    or receipt.tests_run <= 0 or receipt.tests_failed > receipt.tests_run
                    or receipt.status not in {'passed', 'failed'}
                    or receipt.exit_code != (0 if receipt.status == 'passed' else 1)
                    or (receipt.status == 'passed' and receipt.tests_failed != 0)
                    or (receipt.status == 'failed' and receipt.tests_failed == 0)):
                return None
            return receipt
        return None

    baseline, final = latest(result.get('tree_before')), latest(result.get('tree_after'))
    if final is None or final.tests_failed and baseline is None:
        return unknown
    # Public bug reproductions legitimately start red. A completely green
    # final command proves zero remaining/new failures even on that baseline.
    # When both are red, require complete failure identities and stable test
    # coverage. Equal counts alone can hide a repaired old failure plus a new one.
    details: dict[str, Any] = {}
    new_failures = final.tests_failed
    if baseline is not None and baseline.tests_failed and final.tests_failed:
        before = failed_test_ids(argv, baseline.stdout + baseline.stderr, baseline.tests_failed)
        after = failed_test_ids(argv, final.stdout + final.stderr, final.tests_failed)
        if before is None or after is None or baseline.tests_run != final.tests_run:
            return unknown
        introduced = sorted(set(after) - set(before))
        new_failures = len(introduced)
        details = {'regression_baseline_failures': list(before),
                   'regression_final_failures': list(after),
                   'regression_introduced_failures': introduced}
    return {
        'new_regression_failures': new_failures,
        'regression_evidence_known': True,
        'regression_evidence_status': 'measured',
        'regression_baseline_receipt': baseline.receipt_id if baseline is not None else None,
        'regression_baseline_verified': baseline is not None,
        'regression_final_receipt': final.receipt_id,
        'regression_final_tree': final.tree_hash,
        **details,
    }
