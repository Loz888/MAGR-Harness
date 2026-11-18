"""Explicit diagnostic first samples for previously unstarted, now-seen cases.

This is a separate protocol. QualityRunner keeps its development-score barrier;
recovery results never replace a primary score or become independent holdout data.
"""

from __future__ import annotations

import fcntl
import hashlib
import os
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from mgar_v3.domain import DomainError
from mgar_v3.quality_eval import h12
from mgar_v3.quality_eval.h12_quality_runner import QualityRunner
from mgar_v3.quality_eval.h12_v2_freeze import _public_asset, _public_document
from mgar_v3.quality_eval.harness_v4 import CatalogError, repo_root

SCHEMA = "h12-known-benchmark-first-model-recovery-v1"


def load_recovery_primary(
    root: Path,
    document: Mapping[str, Any],
    tasks: list[dict[str, Any]],
) -> dict[str, Any]:
    if (
        document.get("schema") != SCHEMA
        or document.get("status") != "USER_REQUESTED"
        or document.get("quality_status") != "DIAGNOSTIC_ONLY"
        or document.get("allow_model_resample") is not False
        or document.get("allow_score_replacement") is not False
        or document.get("allow_unknown_retry") is not False
        or document.get("allow_publication") is not False
        or document.get("shared_budget_micros") != 12_000_000
        or document.get("shared_call_limit") != 1536
        or document.get("thinking") != {"type": "enabled"}
        or document.get("reasoning_effort") != "max"
    ):
        raise CatalogError("Recovery needs explicit bounded first-model diagnostic authority")
    reference = document["primary_report"]
    primary = _public_document(root, _public_asset(root, reference), reference["sha256"])
    dev = primary.get("summaries", {}).get("dev", {})
    if (
        primary.get("schema") != "h12-complete-thinking-matrix-report-v1"
        or primary.get("matrix_complete") is not True
        or primary.get("ledger_matches_case_totals") is not True
        or primary.get("holdout") != "QUARANTINED_AFTER_THIS_REPORT"
        or dev.get("pass_count", 0) + dev.get("fail_count", 0) != 12
        or dev.get("not_run_count") != 0
        or dev.get("unknown_count") != 0
        or primary.get("budget_after", {}).get("unknown_calls") != 0
        or primary.get("budget_after", {}).get("budget", {}).get("reserved_micros") != 0
    ):
        raise CatalogError("Recovery cannot reuse incomplete or unaccounted primary evidence")
    current = {(t["split"], t["task_id"]): t for t in tasks}
    if len(current) != 24 or len(primary.get("case_records", [])) != 24:
        raise CatalogError("Recovery requires the complete original 12+12 inventory")
    records = {}
    eligible = []
    for reference in primary["case_records"]:
        case = _public_document(root, _public_asset(root, reference), reference["sha256"])
        score = case["score"]
        key = (score["split"], score["task_id"])
        task = current.get(key)
        if (
            task is None
            or key in records
            or case["task"] != h12.project_task(task)
            or case["judge_specification"]["sha256"] != task["judge_sha256"]
            or score.get("experiment_id") != primary["experiment_id"]
            or score.get("arm") != "candidate"
            or score.get("usage_known") is not True
            or score.get("reserved_micros") != 0
            or score.get("status") not in {"PASS", "FAIL", "NOT_RUN"}
        ):
            raise CatalogError("Original task, Judge, sample or usage identity changed")
        records[key] = case
        if score["status"] == "NOT_RUN":
            if (
                score["split"] != "holdout"
                or score.get("run_id")
                or score.get("model_calls", 0) != 0
                or score.get("cost_micros") != 0
                or score.get("settled_cost_micros", 0) != 0
            ):
                raise CatalogError("Only an original case with no Run or model send can recover")
            eligible.append(score["task_id"])
    if (
        not eligible
        or set(records) != set(current)
        or document.get("eligible_task_ids") != sorted(eligible)
        or document.get("max_first_model_samples") != len(eligible)
    ):
        raise CatalogError("Recovery allowlist differs from original zero-call cases")
    return {"primary": primary, "records": records, "eligible_task_ids": sorted(eligible)}


class FirstModelRecoveryRunner(QualityRunner):
    """A hash-bound, public diagnostic, never a new independent quality gate."""

    def __init__(
        self,
        engine: Any,
        *,
        experiment_id: str,
        evidence: Path,
        recovery_path: Path,
        recovery_sha256: str,
        api_key: str | None,
        execute_live: bool = False,
        root: Path | None = None,
    ) -> None:
        base = root or repo_root()
        self.recovery_path, self.recovery_sha256 = recovery_path, recovery_sha256
        self.recovery_document = _public_document(base, recovery_path, recovery_sha256)
        reference = self.recovery_document["quality_freeze"]
        self._recovery_task_id: str | None = None
        super().__init__(
            engine,
            experiment_id=experiment_id,
            evidence=evidence,
            freeze_path=_public_asset(base, reference),
            freeze_sha256=reference["sha256"],
            api_key=api_key,
            execute_live=execute_live,
            root=base,
        )
        self.recovery = load_recovery_primary(
            base, self.recovery_document, [*self._tasks("dev"), *self._tasks("holdout")]
        )
        if experiment_id == self.recovery["primary"]["experiment_id"]:
            raise CatalogError("Recovery must keep primary experiment and scores unchanged")

    def _config(self) -> dict[str, Any]:
        config = super()._config()
        config["first_model_recovery"] = {
            "schema": SCHEMA,
            "quality_status": "DIAGNOSTIC_ONLY",
            "authorization_sha256": self.recovery_sha256,
            "primary_report": self.recovery_document["primary_report"],
            "eligible_task_ids": self.recovery_document["eligible_task_ids"],
        }
        return config

    def _primary_pg_facts(self) -> None:
        primary = self.recovery["primary"]
        original = self.store.get_experiment(primary["experiment_id"])
        current = self.store.get_experiment(self.experiment_id)
        if (
            original["config"].get("candidate_source") != primary["source_sha256"]
            or original["profile_hash"] != current["profile_hash"]
            or current["limit_micros"] != 12_000_000
            or current["call_limit"] != 1536
            or current["usage"]["unknown_calls"]
        ):
            raise DomainError(
                "H12_RECOVERY_PRIMARY_REQUIRED", "Original shared authority changed", 409
            )
        with self.engine.transaction() as conn:
            for (split, task_id), case in self.recovery["records"].items():
                score = case["score"]
                rows = conn.execute(
                    "SELECT t.run_id,t.tree_hash,r.status FROM app.harness_tasks t "
                    "JOIN app.runs r USING(run_id) WHERE t.experiment_id=%s "
                    "AND t.evaluation_profile->>'split'=%s "
                    "AND t.evaluation_profile->>'task_id'=%s",
                    (primary["experiment_id"], split, task_id),
                ).fetchall()
                if score["status"] == "NOT_RUN":
                    valid = not rows
                else:
                    valid = bool(
                        len(rows) == 1
                        and str(rows[0]["run_id"]) == score.get("run_id")
                        and rows[0]["tree_hash"] == score.get("final_tree")
                        and rows[0]["status"] in {"succeeded", "failed", "cancelled"}
                    )
                    if valid:
                        usage = conn.execute(
                            "SELECT count(*) FILTER(WHERE status='known') AS calls,"
                            "count(*) FILTER(WHERE status IN ('reserved','unknown')) AS unresolved,"
                            "coalesce(sum(settled_micros) FILTER(WHERE status='known'),0) AS cost "
                            "FROM app.usage_calls WHERE run_id=%s",
                            (score["run_id"],),
                        ).fetchone()
                        valid = bool(
                            usage
                            and usage["calls"] == score.get("model_calls")
                            and usage["unresolved"] == 0
                            and usage["cost"] == score.get("settled_cost_micros")
                        )
                if not valid:
                    raise DomainError(
                        "H12_RECOVERY_PRIMARY_REQUIRED",
                        "Original PG case/usage differs from the complete primary",
                        409,
                    )

    def _live_policy(self, split: str) -> None:
        if (
            not self.execute_live
            or split != "holdout"
            or self._recovery_task_id not in self.recovery["eligible_task_ids"]
        ):
            raise DomainError(
                "H12_RECOVERY_CASE_REQUIRED", "Only explicit unstarted cases may run", 403
            )
        current = _public_document(self.root, self.recovery_path, self.recovery_sha256)
        if current != self.recovery_document:
            raise CatalogError("Recovery authority changed")
        # Keep the original source/profile/model/complete-readiness checks.
        # This known-benchmark mode never claims a new independent score.
        self._validate_frozen_execution()
        self._primary_pg_facts()
        with self.engine.transaction() as conn:
            row = conn.execute(
                "SELECT 1 FROM app.harness_tasks WHERE "
                "evaluation_profile->>'task_id'=%s AND evaluation_profile->>'split'='holdout'",
                (self._recovery_task_id,),
            ).fetchone()
        if row:
            raise DomainError(
                "H12_RECOVERY_ALREADY_STARTED",
                "An existing Run requires reconciliation, never another first sample",
                409,
            )

    @contextmanager
    def _first_model_claim(self, task_id: str) -> Iterator[None]:
        # A transient per-case file lock closes the check/create race between
        # threads without holding a PostgreSQL transaction over model I/O.
        # The original PG Run remains the persistent started fact after crash.
        root = self.engine.artifacts.root
        directory = root / "first-model-recovery-locks"
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        if directory.is_symlink() or not directory.resolve().is_relative_to(root.resolve()):
            raise DomainError("H12_RECOVERY_LOCK_DENIED", "Recovery lock root differs", 409)
        identity = self.recovery_document["primary_report"]["sha256"] + ":" + task_id
        name = hashlib.sha256(identity.encode()).hexdigest() + ".lock"
        descriptor = os.open(directory / name, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        with os.fdopen(descriptor, "w") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise DomainError(
                    "H12_RECOVERY_ALREADY_STARTED", "Another worker owns this first sample", 409
                ) from exc
            try:
                yield
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)

    def evaluate_task(self, task: Mapping[str, Any], arm: str) -> dict[str, Any]:
        selected = next(
            (t for t in self._tasks("holdout") if t["task_id"] == task.get("task_id")), None
        )
        if (
            arm != "candidate"
            or selected is None
            or dict(task) != selected
            or task["task_id"] not in self.recovery["eligible_task_ids"]
        ):
            raise DomainError("H12_RECOVERY_CASE_REQUIRED", "Recovery task/profile differs", 403)
        self._recovery_task_id = str(task["task_id"])
        with self._first_model_claim(self._recovery_task_id):
            return super().evaluate_task(task, arm)

    def evaluate(self, *, split: str, arm: str, task_id: str | None = None) -> dict[str, Any]:
        self._recovery_task_id = task_id
        self._live_policy(split)
        return super().evaluate(split=split, arm=arm, task_id=task_id)
