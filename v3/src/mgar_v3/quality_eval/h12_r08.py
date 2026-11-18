"""New-candidate R08 input closure and zero-model preparation.

All business execution is inherited from EvaluationRunner/Engine. This module
only binds explicitly reviewed new inputs; it neither resets the shared budget
nor modifies any historical freeze. Private contents are never printed.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import time
from collections import Counter
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from mgar_v3.domain import DomainError
from mgar_v3.harness.contracts import EvaluationProfile, canonical_hash
from mgar_v3.harness.public_feedback import PublicFeedbackPolicy
from mgar_v3.harness.public_probe import read_asset
from mgar_v3.harness.public_probe_types import PublicProbePlan
from mgar_v3.harness.result_artifacts import load_result
from mgar_v3.harness.semantic_types import BaselineAssessment, SemanticRevision
from mgar_v3.quality_eval import h12, h12_judge
from mgar_v3.quality_eval.h12_public_plan import (
    PUBLIC_ROLES,
    FrozenPublicPlan,
    validate_public_original,
)
from mgar_v3.quality_eval.h12_runner import (
    DEV_ENVIRONMENT_V4,
    HOLDOUT_INFRA_V4,
    EvaluationRunner,
    private_json,
    profile_for,
)
from mgar_v3.quality_eval.harness_v4 import CatalogError, load_jsonl, load_tasks, validate_task

BUNDLE_SCHEMA = "h12-r08-bundle-v1"
CANDIDATE_SCHEMA = "h12-r08-candidate-v1"
COMPOSITION = {"bugfix": 4, "feature": 3, "refactor": 2, "run_app": 2, "large_multi": 1}
THRESHOLDS = {"dev": 10, "holdout": 9, "denominator_each": 12}


def _error(message: str) -> DomainError:
    return DomainError("H12_R08_FROZEN_INPUT_REQUIRED", message, 409)


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def reference(root: Path, path: Path) -> dict[str, str]:
    return {"path": str(path.absolute().relative_to(root.absolute())), "sha256": sha(path)}


def asset(root: Path, ref: Mapping[str, str]) -> Path:
    if set(ref) != {"path", "sha256"}:
        raise _error("An asset needs exactly its relative path and SHA256")
    name = ref["path"]
    path = root / name
    if (
        not name
        or Path(name).is_absolute()
        or ".." in Path(name).parts
        or "\\" in name
        or any(p.is_symlink() for p in (path, *path.parents))
        or not path.resolve().is_relative_to(root.resolve())
        or not path.is_file()
        or sha(path) != ref["sha256"]
    ):
        raise _error("Frozen asset hash or root binding differs")
    return path


def document(root: Path, ref: Mapping[str, str]) -> dict[str, Any]:
    def pairs(rows: list[tuple[str, Any]]) -> dict[str, Any]:
        value: dict[str, Any] = {}
        for k, v in rows:
            if k in value:
                raise _error("Duplicate identity in a frozen document")
            value[k] = v
        return value

    value = json.loads(asset(root, ref).read_bytes(), object_pairs_hook=pairs)
    if not isinstance(value, dict):
        raise _error("Frozen document must be an object")
    return value


def semantic_revision(root: Path, ref: Mapping[str, str]) -> SemanticRevision:
    values = document(root, ref)
    for key in ("traceability_path", "registry_path"):
        p = Path(values[key])
        relative = str(p.relative_to(root)) if p.is_absolute() else str(p)
        values[key] = asset(
            root, {"path": relative, "sha256": values[key.removesuffix("_path") + "_sha256"]}
        )
    return SemanticRevision.model_validate(values)


def load_bundle(root: Path, ref: Mapping[str, str], split: str) -> dict[str, Any]:
    bundle = document(root, ref)
    if bundle.get("schema") != BUNDLE_SCHEMA or bundle.get("split") != split:
        raise _error("New split bundle schema/identity differs")
    provenance = document(root, bundle["provenance"])
    tasks = load_jsonl(asset(root, bundle["catalog"]))
    if split == "holdout":
        if (
            provenance.get("schema") != "h12-r08-provenance-v1"
            or provenance.get("split") != "holdout"
            or provenance.get("independent_curator") is not True
            or provenance.get("historical_holdout_quarantined") is not True
            or provenance.get("private_answers_exposed_to_developer") is not False
            or provenance.get("private_answers_exposed_to_worker") is not False
            or provenance.get("evaluated_model_used_for_curation") is not False
            or provenance.get("independent_coding_agents_used") is not True
        ):
            raise _error("Independent provenance and unexposed held-out answers are required")
        review = document(root, provenance["semantic_dedup_review"])
        if (
            review.get("status") != "PASS"
            or review.get("case_count") != 12
            or review.get("catalog_sha256") != bundle["catalog"]["sha256"]
        ):
            raise _error("Independent semantic deduplication review is not bound")
        old = [t for s in ("dev", "holdout") for t in load_tasks(s, root)]
        old.extend(t for s in ("dev", "holdout") for t in h12._public_v35_metadata(root, s))
        anchor = root / ".local/h12/benchmark-v2/freeze-manifest-d8ee974.json"
        if anchor.exists():
            historical = json.loads(anchor.read_bytes())["holdout_catalog"]
            old.extend(load_jsonl(asset(root, historical)))
        old_ids = {str(t.get("task_id")) for t in old}
        old_goals = {str(t.get("goal")) for t in old if t.get("goal")}
        if any(t["task_id"] in old_ids or t["goal"] in old_goals for t in tasks):
            raise _error("A historical task cannot be renamed into the new independent split")
    if (
        len(tasks) != 12
        or len({t["task_id"] for t in tasks}) != 12
        or Counter(t["composition"] for t in tasks) != Counter(COMPOSITION)
        or len({t["repository"] for t in tasks}) < 4
        or not {"python", "go"}.issubset({t["language"] for t in tasks})
        or not {"javascript", "typescript"}.intersection(t["language"] for t in tasks)
        or any(validate_task(t, split) for t in tasks)
    ):
        raise _error("New split must preserve the complete H00 composition and resource scope")
    entries = bundle["per_task"]
    if set(entries) != {t["task_id"] for t in tasks}:
        raise _error("Each task needs exactly one explicit plan")
    locks = {key: asset(root, item) for key, item in bundle["image_locks"].items()}
    probe_root = root / bundle["public_probe_root"]
    if (
        not probe_root.is_dir()
        or not probe_root.resolve().is_relative_to(root.resolve())
        or any(p.is_symlink() for p in (probe_root, *probe_root.parents))
    ):
        raise _error("Public probe directory must be scoped without symlinks")
    revisions, plans = {}, {}
    for task in tasks:
        name = task["task_id"]
        row = entries[name]
        if task["repository"] + "@" + task["base_commit"] not in locks:
            raise _error("Every pinned source needs a frozen image")
        revision = semantic_revision(root, row["semantic_revision"])
        expected_revision = (
            "h12-r08-20261117-v1" if split == "dev" else "h12-r08-independent-20261117-v1"
        )
        if revision.policy.benchmark_revision != expected_revision:
            raise _error("R08 requires a distinct explicit semantic revision")
        revisions[name] = revision
        h12_judge._implementation(
            task, root, registry_path=revision.registry_path,
            registry_sha256=revision.registry_sha256)
        p = document(root, row["public_plan"])
        probe = PublicProbePlan.model_validate(p["probe"])
        read_asset(probe, probe_root)
        if revision.structural_probe is not None:
            read_asset(revision.structural_probe, probe_root)
        if (
            p.get("schema") != "h12-public-plan-v1"
            or p.get("status") != "FROZEN"
            or p.get("roles") != PUBLIC_ROLES
            or p.get("task_id") != name
            or p.get("repository") != task["repository"]
            or p.get("base_commit") != task["base_commit"]
            or p.get("public_goal_sha256") != hashlib.sha256(task["goal"].encode()).hexdigest()
            or p.get("resources_sha256") != canonical_hash(task["resources"])
            or p.get("regression_argv") != row["public_test_argv"]
            or p.get("original_tree_sha256") != row["original_tree_sha256"]
        ):
            raise _error("Public plan belongs to another task/tree/command")
        plans[name] = FrozenPublicPlan(
            plan=probe,
            manifest_path=asset(root, row["public_plan"]),
            manifest_sha256=row["public_plan"]["sha256"],
            asset_root=probe_root,
            task_id=name,
            repository=task["repository"],
            base_commit=task["base_commit"],
            public_goal_sha256=p["public_goal_sha256"],
            resources_sha256=p["resources_sha256"],
            original_tree_sha256=p["original_tree_sha256"],
            regression_argv=tuple(p["regression_argv"]),
            public_basis=tuple((v["path"], v["sha256"]) for v in p["public_basis"]),
        )
        for overlay in row.get("public_assets", []):
            asset(root, overlay["source"])
            if not h12._safe_path(overlay["target"]):
                raise _error("Public overlay path is not canonical")
    return {
        "document": bundle,
        "tasks": tasks,
        "revisions": revisions,
        "plans": plans,
        "locks": locks,
        "probe_root": probe_root,
    }


class R08Runner(EvaluationRunner):
    """New input binding around the same Engine/Run/budget/permission path."""

    def __init__(
        self,
        engine: Any,
        *,
        experiment_id: str,
        evidence: Path,
        candidate: tuple[Path, str],
        api_key: str | None,
        execute_live: bool = False,
        root: Path,
    ) -> None:
        self.candidate_ref = {"path": str(candidate[0].relative_to(root)), "sha256": candidate[1]}
        self.candidate = document(root, self.candidate_ref)
        if (
            self.candidate.get("schema") != CANDIDATE_SCHEMA
            or self.candidate.get("phase") not in {"PREPARATION", "FROZEN"}
            or self.candidate.get("thresholds") != THRESHOLDS
            or self.candidate.get("shared_budget_micros") != 12000000
            or self.candidate.get("sampling") != "first-sample-no-selection-v1"
            or self.candidate.get("concurrency") != 1
        ):
            raise _error("New candidate protocol is incomplete")
        self.bundles = {
            split: load_bundle(root, self.candidate[split + "_bundle"], split)
            for split in ("dev", "holdout")
            if self.candidate.get(split + "_bundle")
        }
        if "dev" not in self.bundles:
            raise _error("R08 preparation requires development input")
        ids = [t["task_id"] for b in self.bundles.values() for t in b["tasks"]]
        if len(set(ids)) != len(ids):
            raise _error("Split identities overlap")
        allowlist = self.candidate.get("development_dispatch_allowlist")
        if allowlist is not None and (
            not isinstance(allowlist, list) or not allowlist
            or any(not isinstance(name, str) for name in allowlist)
            or len(set(allowlist)) != len(allowlist)
            or not set(allowlist).issubset({t["task_id"] for t in self.bundles["dev"]["tasks"]})
        ):
            raise _error("Targeted dispatch must select existing unique development tasks")
        super().__init__(
            engine,
            experiment_id=experiment_id,
            evidence=evidence,
            image_locks={},
            api_key=api_key,
            root=root,
            execute_live=False,
            semantic_revisions={
                name: r for b in self.bundles.values() for name, r in b["revisions"].items()
            },
            public_feedback_policy=PublicFeedbackPolicy(),
        )
        # Reuse the existing complete-matrix storage protocol. The new benchmark
        # identity is separately frozen, not substituted for historical v2 data.
        self.environment_revision = "h12-benchmark-v2"
        self.execute_live = execute_live
        if execute_live:
            self._require_dev_readiness()

    def _tasks(self, split: str) -> list[dict[str, Any]]:
        return list(self.bundles[split]["tasks"]) if split in self.bundles else []

    def evaluate_task(self, task: Mapping[str, Any], arm: str) -> dict[str, Any]:
        allowlist = self.candidate.get("development_dispatch_allowlist")
        if task["split"] == "dev" and allowlist is not None and task["task_id"] not in allowlist:
            raise DomainError(
                "H12_R08_TASK_NOT_AUTHORIZED", "Task is outside this frozen targeted scope", 403
            )
        return super().evaluate_task(task, arm)

    def _revision_for(self, task: Mapping[str, Any]) -> str:
        return DEV_ENVIRONMENT_V4 if task["split"] == "dev" else HOLDOUT_INFRA_V4

    def _profile_for(self, task: Mapping[str, Any], arm: str) -> EvaluationProfile:
        bundle = self.bundles[task["split"]]
        self.public_probe_root = bundle["probe_root"]
        entry = bundle["document"]["per_task"][task["task_id"]]
        profile = profile_for(
            task, self.experiment_id, arm, environment_revision=self._revision_for(task)
        )
        values = profile.model_dump(mode="json")
        values.update(
            public_test_argv=entry["public_test_argv"],
            public_build_argv=entry.get("public_build_argv", []),
            public_probe=bundle["plans"][task["task_id"]].plan.model_dump(mode="json"),
            verification_protocol="public-probe-v1",
            sandbox_scratch_mb=256,
        )
        for key in (
            "service_argv",
            "service_mode",
            "service_port",
            "service_path",
            "service_timeout_seconds",
            "lifecycle_routes",
        ):
            if key in entry:
                values[key] = entry[key]
        return self._bind_regression_metadata(task, EvaluationProfile.model_validate(values))

    def _lock_for(self, task: Mapping[str, Any]) -> str:
        return str(
            self.bundles[task["split"]]["locks"][task["repository"] + "@" + task["base_commit"]]
        )

    def _public_plan_for(
        self, task: Mapping[str, Any], regression_argv: Sequence[str]
    ) -> FrozenPublicPlan:
        plan: FrozenPublicPlan = self.bundles[task["split"]]["plans"][task["task_id"]]
        if plan.regression_argv != tuple(regression_argv):
            raise _error("Public target argv changed")
        return plan

    def _public_plan_metadata(self, tasks: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
        return {
            str(t["task_id"]): self.bundles[t["split"]]["plans"][t["task_id"]].freeze_metadata()
            for t in tasks
        }

    def _after_materialize(self, task: Mapping[str, Any], target: Path) -> Path:
        bundle = self.bundles[task["split"]]["document"]
        for ref in bundle.get("public_amendments", []):
            from mgar_v3.quality_eval.h12_benchmark import apply_public_amendments

            amendment = document(self.root, ref)
            if (amendment["repository"], amendment["base_commit"]) == (
                task["repository"],
                task["base_commit"],
            ):
                apply_public_amendments(
                    target,
                    asset(self.root, ref),
                    ref["sha256"],
                    repository=task["repository"],
                    commit=task["base_commit"],
                )
        entry = bundle["per_task"][task["task_id"]]
        for overlay in entry.get("public_assets", []):
            path = target / overlay["target"]
            if (
                path.is_symlink()
                or any(p.is_symlink() for p in path.parents)
                or not path.resolve().is_relative_to(target.resolve())
            ):
                raise _error("Frozen public overlay conflicts with pinned source")
            if path.exists() and (
                not overlay.get("replace_sha256")
                or not path.is_file()
                or sha(path) != overlay["replace_sha256"]
                or not set(Path(overlay["target"]).parts) & {"tests", "test"}
            ):
                raise _error("Public test correction needs its exact original hash")
            if not path.exists() and overlay.get("replace_sha256"):
                raise _error("A public replacement cannot silently become a new file")
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(asset(self.root, overlay["source"]).read_bytes())
        return target

    def _log_limit_for(self, task: Mapping[str, Any]) -> int:
        return 1048576

    def initialize(self) -> None:
        self._require_dev_readiness()
        frozen = h12.frozen_hashes(self.root)
        if frozen["status"] != "PASS":
            raise _error("Original H00 bytes differ")
        tasks = [t for split in ("dev", "holdout") for t in self._tasks(split)]
        self._validate_semantic_freeze(tasks)
        profiles = [
            self._profile_for(t, arm).model_dump(mode="json")
            for t in tasks
            for arm in ("candidate", "legacy-v35")
        ]
        config = {
            "schema": "harness-h12-experiment-v1",
            "environment_revision": "h12-benchmark-v2",
            "benchmark_revision": "h12-r08-20261117-v1",
            "profiles": profiles,
            "frozen_catalog": frozen,
            "candidate_source": self._candidate_fingerprint(),
            "candidate_manifest": self.candidate_ref,
            "r08_protocol": self.candidate,
            "arms": ["candidate", "legacy-v35"],
            "model_observation_sha256": sha(self.root / "experiments/harness-v4/h12-model-v2.json"),
        }
        approval = {
            "approved_by": self.engine.settings.local_actor,
            "allow_paid": True,
            "max_cost_micros": 12000000,
            "max_calls": 1536,
            "source_authorization_sha256": sha(
                self.root / "experiments/harness-v4/h12-authorization-v1.json"
            ),
            "r08_authorization": self.candidate["authorization"],
        }
        self.store.create_experiment(self.experiment_id, config, approval)

    def _require_dev_readiness(self) -> None:
        if self.candidate.get("phase") != "FROZEN" or set(self.bundles) != {"dev", "holdout"}:
            raise DomainError("H12_R08_FULL_READINESS_REQUIRED", "Preparation grants no calls", 403)
        if (
            document(self.root, self.candidate_ref) != self.candidate
            or self._candidate_fingerprint() != self.candidate["source_sha256"]
        ):
            raise _error("Source or candidate bytes drifted")
        if h12.frozen_hashes(self.root)["status"] != "PASS":
            raise _error("Original H00 history changed")
        fixed_inputs = {
            "model": "experiments/harness-v4/h12-model-v2.json",
            "authorization": "experiments/harness-v4/h12-authorization-v1.json",
            "scoring": "experiments/harness-v4/scoring-v1.json",
            "dev_environment": "experiments/harness-v4/h12-dev-environment-v4.json",
            "holdout_environment": "experiments/harness-v4/h12-holdout-infrastructure-v4.json",
            "dependencies": "uv.lock", "verification_registry": "config/verification.json",
        }
        for key, path in fixed_inputs.items():
            ref = self.candidate["protocol_inputs"][key]
            if ref["path"] != path:
                raise _error("Frozen execution protocol was substituted")
            asset(self.root, ref)
        history = document(self.root, self.candidate["historical_evidence"])
        if len(history["scores"]) != 128:
            raise _error("The full original score archive is required")
        for origin, expected in history["scores"].items():
            historical_path = Path(origin)
            if (not historical_path.is_absolute()
                    or not historical_path.is_relative_to(Path.home().resolve())
                    or historical_path.name != "score.json"
                    or not historical_path.is_file() or sha(historical_path) != expected):
                raise _error("A historical score changed")
        authorization = document(self.root, self.candidate["authorization"])
        if (
            authorization.get("status") != "USER_REQUESTED"
            or authorization.get("shared_budget_micros") != 12000000
            or authorization.get("max_calls") != 1536
            or any(authorization.get(k) is not False for k in ("publication", "push", "h13"))
        ):
            raise _error("Bounded explicit R08 authorization is required")
        from mgar_v3.quality_eval.h12_r08_engineering import validate_engineering

        validate_engineering(self.root, self.candidate)
        for name, ref in self.candidate["dependencies"].items():
            if (
                name not in {f"R{i:02d}" for i in range(8)}
                or document(self.root, ref).get("status") != "PASS"
            ):
                raise _error("R00-R07 engineering dependencies are required")
        if set(self.candidate["dependencies"]) != {f"R{i:02d}" for i in range(8)}:
            raise _error("R00-R07 dependency set is incomplete")
        profiles = [
            self._profile_for(task, arm).model_dump(mode="json", exclude={"experiment_id"})
            for split in ("dev", "holdout")
            for task in self._tasks(split)
            for arm in ("candidate", "legacy-v35")
        ]
        if canonical_hash(profiles) != self.candidate["profiles_sha256"]:
            raise _error("Frozen model, output, task or runtime profile changed")
        for split, bundle in self.bundles.items():
            if load_bundle(self.root, self.candidate[split + "_bundle"], split) != bundle:
                raise _error("Frozen bundle closure drifted")
            ready = document(self.root, self.candidate["readiness"][split])
            if (
                ready.get("status") != "PASS"
                or ready.get("ready_count") != 12
                or ready.get("source_sha256") != self.candidate["source_sha256"]
                or ready.get("source_sha256_after") != self.candidate["source_sha256"]
                or ready.get("bundle") != self.candidate[split + "_bundle"]
            ):
                raise _error("Complete source-bound public readiness is required")
            if set(ready["tasks"]) != {t["task_id"] for t in bundle["tasks"]}:
                raise _error("Exactly twelve original readiness receipts are required")
            if ready.get("schema") == "h12-r08-retained-readiness-v1":
                from mgar_v3.quality_eval.h12_r08_retest import validate_retained_readiness

                validate_retained_readiness(self.root, self.candidate, ready, split, bundle)
            for task_id, row in ready["tasks"].items():
                result = document(self.root, row["result"])
                if result["status"] != "PASS" or result.get("baseline_method") != (
                    "r06-public-task-type-policy"
                ):
                    raise _error("Readiness must reference real public type-baseline receipts")
                from mgar_v3.quality_eval.h12_baseline_policy import assess_baseline

                policy = bundle["revisions"][task_id].policy
                assessment = BaselineAssessment.model_validate_json(
                    self.engine.artifacts.read(result["baseline_assessment_ref"])
                )
                if (
                    result.get("task_id") != task_id
                    or assessment.tree_hash
                    != bundle["document"]["per_task"][task_id]["original_tree_sha256"]
                    or assessment
                    != assess_baseline(
                        self.engine.artifacts,
                        policy,
                        task_id=task_id,
                        experiment_id=ready["experiment_id"],
                        tree=assessment.tree_hash,
                        behavior_ref=assessment.behavior_ref,
                        target_ref=assessment.target_ref,
                    )
                    or assessment.status != "PASS"
                ):
                    raise _error("Original public readiness is not independently reproducible")
            from mgar_v3.quality_eval.h12_r08_controls import validate_controls

            validate_controls(self.root, bundle)

    def _live_policy(self, split: str, *, active_reservation: bool = False) -> None:
        if not self.execute_live or not self.api_key:
            raise DomainError("H12_LIVE_EXECUTION_REQUIRED", "Paid execution not enabled", 403)
        self._require_dev_readiness()
        group = self.store.get_experiment(self.experiment_id)
        if group["config"].get("candidate_manifest") != self.candidate_ref:
            raise _error("Original Engine experiment selected another frozen candidate")
        if group["usage"]["unknown_calls"]:
            raise DomainError("H12_UNKNOWN_USAGE", "Shared usage requires reconciliation", 409)
        if split == "holdout":
            self.validate_barrier(active_reservation=active_reservation)

    def _service_options(self, task: Mapping[str, Any], case: Path) -> dict[str, Any]:
        def before_model(request: Any) -> None:
            try:
                self._live_policy(str(task["split"]), active_reservation=True)
                private_json(case / "admission" / (
                    hashlib.sha256(request.operation_id.encode()).hexdigest()[:20]
                    + "-" + str(time.time_ns()) + ".json"), {
                        "status": "FREEZE_REVALIDATED_BEFORE_RESERVATION",
                        "recorded_at": datetime.now(UTC).isoformat(),
                        "candidate": self.candidate_ref,
                        "source_sha256": self.candidate["source_sha256"],
                        "experiment_id": self.experiment_id,
                        "task_id": task["task_id"], "runtime_task_id": request.task.task_id,
                        "run_id": str(request.task.run_id),
                        "execution_epoch": request.task.execution_epoch,
                        "operation_id": request.operation_id, "physical_send_proven": False,
                    })
            except DomainError:
                raise
            except Exception as exc:
                raise _error("Frozen evidence revalidation failed before reservation") from exc

        return {"before_model_request": before_model}

    def _execute(
        self,
        task: Mapping[str, Any],
        profile: EvaluationProfile,
        source: Path,
        image_lock: Path,
        case: Path,
    ) -> dict[str, Any]:
        # The original service construction, leases, CAS, budget and final seal
        # remain in the parent. Only the separately bound public asset root varies.
        self.public_probe_root = self.bundles[task["split"]]["probe_root"]
        return super()._execute(task, profile, source, image_lock, case)

    def prepare_readiness(
        self,
        split: str,
        output: Path,
        *,
        only: Sequence[str] | None = None,
    ) -> dict[str, Any]:
        before = self._candidate_fingerprint()
        output.mkdir(parents=True, exist_ok=False)
        results: dict[str, Any] = {}
        self.public_probe_root = self.bundles[split]["probe_root"]
        tasks = self._tasks(split)
        if only is not None:
            if not only or not set(only).issubset(t["task_id"] for t in tasks):
                raise _error("Readiness subset must name declared tasks")
            tasks = [t for t in tasks if t["task_id"] in only]
        for task in tasks:
            name = task["task_id"]
            case = output / name
            source = self._source(task)
            if source is None:
                results[name] = {"status": "BLOCKED", "reason": "PINNED_SOURCE_UNAVAILABLE"}
                continue
            original = self._materialize(task, source)
            try:
                profile = self._profile_for(task, "candidate")
                validate_public_original(
                    self._public_plan_for(task, profile.public_test_argv), original
                )
                self._review_semantic_judge(task, profile, case)
                result = h12_judge.verify(
                    original,
                    task,
                    Path(self._lock_for(task)),
                    case / "public-baseline",
                    phase="environment",
                    nofile_limit=profile.sandbox_nofile,
                    memory_mb=profile.sandbox_memory_mb,
                    log_bytes=profile.sandbox_log_bytes,
                    offline_network=profile.sandbox_offline_network,
                    environment_revision=self._revision_for(task),
                    **self._semantic_options(task, profile),
                )
                path = case / "public-baseline/judge-result.json"
                results[name] = {"status": result["status"], "result": reference(self.root, path)}
            except (DomainError, CatalogError, OSError, ValueError) as exc:
                results[name] = {
                    "status": "BLOCKED",
                    "reason": getattr(exc, "code", type(exc).__name__),
                }
            finally:
                shutil.rmtree(original.parent)
        after = self._candidate_fingerprint()
        count = sum(row["status"] == "PASS" for row in results.values())
        report = {
            "schema": "h12-r08-readiness-v1",
            "split": split,
            "status": "PASS" if count == 12 and before == after else "BLOCKED",
            "ready_count": count,
            "source_sha256": before,
            "source_sha256_after": after,
            "bundle": self.candidate[split + "_bundle"],
            "tasks": results,
            "new_paid_calls": 0,
            "experiment_id": self.experiment_id,
        }
        private_json(output / "report.json", report)
        return report

    def audit_development(self, *, allow_reserved: bool = False) -> dict[str, Any]:
        """Recompute persisted first samples from original PG-selected CAS results."""
        scores, passed = [], 0
        for task in self._tasks("dev"):
            path = self.evidence / "candidate/dev" / task["task_id"] / "score.json"
            if not path.is_file():
                raise DomainError("H12_R08_DEV_BARRIER_REQUIRED", "A dev sample is missing", 403)
            score = json.loads(path.read_bytes())
            profile = self._profile_for(task, "candidate")
            authority = self.sample_usage(profile)
            if authority is None or not authority["result_hash"]:
                raise _error("Every development sample needs an original terminal Engine result")
            result = load_result(
                self.engine.artifacts,
                self.engine.artifacts.read(authority["result_hash"]),
                authority["runtime_task_id"],
                authority["run_id"],
            )
            original = self._score_payload(task, profile, result)
            if any(score.get(k) != v for k, v in original.items()):
                raise _error("A stored first-sample score differs from original Engine evidence")
            if (
                not score.get("usage_known")
                or score.get("reserved_micros")
                or score.get("status") == "UNKNOWN"
            ):
                raise _error("All development usage must be reconciled before held-out admission")
            passed += score["status"] == "PASS"
            scores.append(reference(self.root, path))
        group = self.store.get_experiment(self.experiment_id)
        if group["usage"]["unknown_calls"] or group["reserved_micros"] and not allow_reserved:
            raise _error("Shared ledger is not fully settled")
        return {
            "candidate_sha256": self.candidate_ref["sha256"],
            "experiment_id": self.experiment_id,
            "status": "PASS" if passed >= 10 else "FAIL",
            "dev_pass": passed,
            "denominator": 12,
            "scores": scores,
            "source_sha256": self._candidate_fingerprint(),
            "ledger": {k: group[k] for k in ("settled_micros", "used_calls", "reserved_micros")},
        }

    def write_barrier(self) -> dict[str, Any]:
        self._require_dev_readiness()
        barrier = self.audit_development()
        private_json(self.evidence / "development-holdout-barrier.json", barrier)
        return barrier

    def validate_barrier(self, *, active_reservation: bool = False) -> dict[str, Any]:
        path = self.evidence / "development-holdout-barrier.json"
        if not path.is_file():
            raise DomainError("H12_R08_DEV_BARRIER_REQUIRED", "Development barrier missing", 403)
        barrier: dict[str, Any] = json.loads(path.read_bytes())
        if (
            barrier.get("candidate_sha256") != self.candidate_ref["sha256"]
            or barrier.get("experiment_id") != self.experiment_id
            or barrier.get("status") != "PASS"
            or barrier.get("dev_pass", 0) < 10
        ):
            raise _error("Development quality barrier does not belong to this candidate")
        for item in barrier["scores"]:
            document(self.root, item)
        fresh = self.audit_development(allow_reserved=active_reservation)
        if any(
            barrier.get(k) != fresh[k]
            for k in (
                "candidate_sha256",
                "experiment_id",
                "status",
                "dev_pass",
                "denominator",
                "scores",
                "source_sha256",
            )
        ):
            raise _error("Development barrier differs from authoritative original results")
        usage = self.store.get_experiment(self.experiment_id)["usage"]
        if usage["unknown_calls"]:
            raise DomainError("H12_UNKNOWN_USAGE", "Usage must be reconciled first", 409)
        return barrier
