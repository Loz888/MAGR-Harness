"""Developer-controlled H12 evaluation; never registered as a project tool.

Catalog/judge data stays in the evaluator. Only a task's safe projection goes
to the coding service, and independent results never become model feedback.
All arms use the same persistent Engine experiment budget.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import tempfile
import time
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, Literal
from uuid import UUID

from mgar_v3.domain import DomainError, digest
from mgar_v3.harness.contracts import EvaluationProfile, EvaluationResources
from mgar_v3.harness.lifecycle_plan import public_routes
from mgar_v3.harness.lifecycle_types import LifecyclePolicy
from mgar_v3.harness.output_protocol import OutputPolicy
from mgar_v3.harness.public_feedback import PublicFeedbackPolicy
from mgar_v3.harness.public_probe_types import PublicProbePlan
from mgar_v3.harness.regression_types import RegressionPlan
from mgar_v3.harness.result_artifacts import load_result
from mgar_v3.harness.semantic_types import BaselineAssessment, SemanticRevision
from mgar_v3.harness.service import CodingTaskRequest, HarnessCodingService
from mgar_v3.harness.tool_errors import ToolErrorPolicy
from mgar_v3.quality_eval import h12, h12_judge
from mgar_v3.quality_eval.h12_independent_runner import IndependentRegressionRunner
from mgar_v3.quality_eval.h12_live import FLASH, MODEL_OBSERVATION, RecordedFlashModel
from mgar_v3.quality_eval.h12_public_plan import (
    FrozenPublicPlan,
    load_public_plan,
    validate_public_original,
)
from mgar_v3.quality_eval.h12_regression_gate import assess_evaluation, evaluate_final
from mgar_v3.quality_eval.h12_regression_metrics import regression_metrics
from mgar_v3.quality_eval.h12_regression_plan import regression_plan as build_regression_plan
from mgar_v3.quality_eval.h12_regression_scoring import load_report, unverified_metrics
from mgar_v3.quality_eval.harness_v4 import CatalogError, load_tasks, repo_root

ARMS = ("legacy-v35", "candidate")
_DEFAULT_OUTPUT_POLICY = OutputPolicy()
_DEFAULT_TOOL_ERROR_POLICY = ToolErrorPolicy()
_DEFAULT_LIFECYCLE_POLICY = LifecyclePolicy()


def private_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2)
        stream.flush()
        os.fsync(stream.fileno())


DEV_ENVIRONMENT_V3 = "h12-dev-environment-v3"
DEV_ENVIRONMENT_V4 = "h12-dev-environment-v4"
HOLDOUT_INFRA_V4 = "h12-holdout-infrastructure-v4"
DEV_REVISIONS = (DEV_ENVIRONMENT_V3, DEV_ENVIRONMENT_V4)
REPAIRED_REVISIONS = (*DEV_REVISIONS, HOLDOUT_INFRA_V4)


def profile_for(
    task: Mapping[str, Any],
    experiment_id: str,
    arm: str,
    *,
    environment_revision: str | None = None,
    public_probe: PublicProbePlan | None = None,
    acceptance_protocol: Literal["legacy", "contract-v1"] = "contract-v1",
    output_policy: OutputPolicy | None = _DEFAULT_OUTPUT_POLICY,
    tool_error_policy: ToolErrorPolicy | None = _DEFAULT_TOOL_ERROR_POLICY,
    lifecycle_policy: LifecyclePolicy | None = _DEFAULT_LIFECYCLE_POLICY,
    independent_regression_plan: RegressionPlan | None | Literal["public-v1"] = "public-v1",
) -> EvaluationProfile:
    if public_probe is not None and task.get("language") not in {"python", "go"}:
        raise CatalogError("The public probe protocol requires a supported Python or Go project")
    if environment_revision not in {None, *REPAIRED_REVISIONS}:
        raise CatalogError("Unsupported trusted environment revision")
    if environment_revision in DEV_REVISIONS and task.get("split") != "dev":
        raise CatalogError("The exposed historical holdout is quarantined from dev repairs")
    if environment_revision == HOLDOUT_INFRA_V4 and task.get("split") != "holdout":
        raise CatalogError("Holdout infrastructure revision requires holdout input")
    safe = h12.project_task(task)
    commands = safe["public_commands"]
    if task["type"] == "run_app":
        commands = h12.public_commands(
            {
                "environment": {"verify": []},
                "acceptance": task.get("acceptance", {}),
            }
        )
    argv = list(commands[0]) if commands else []
    if argv and argv[0] in {"python", "python3"}:
        argv[0] = "python3"
        if "-B" not in argv:
            argv.insert(1, "-B")
    if argv[:2] == ["go", "test"] or argv[:1] == ["npx"]:
        argv = h12_judge._normalize(argv)
    if (
        environment_revision in REPAIRED_REVISIONS
        and task["repository"] == "sindresorhus/got"
        and argv == ["npm", "test"]
    ):
        argv += (
            ["--", "--config=/opt/h12/ava-config.mjs"]
            if environment_revision in {DEV_ENVIRONMENT_V4, HOLDOUT_INFRA_V4}
            else ["--", "--no-cache"]
        )
    # The original public tests are a frozen evaluator input. Grant only the
    # implementation part of the frozen scope; never hidden or original tests.
    scope = tuple(
        path
        for path in safe["scope"]
        if not set(Path(path).parts) & {"test", "tests", "verification"}
    )
    service: tuple[str, ...] = ()
    service_mode: Literal["http", "oneshot"] = "http"
    port, service_path = 8000, "/"
    application_languages = {"javascript", "typescript"}
    if environment_revision in REPAIRED_REVISIONS:
        application_languages.add("python")
    if task["type"] == "run_app" and task["language"] in application_languages:
        goal = str(safe["goal"])
        one_shot = bool(
            re.search(
                r"\b(?:ephemeral|selfcheck|one-shot|cli|command.line)\b"
                if environment_revision in REPAIRED_REVISIONS
                else r"\b(?:ephemeral|selfcheck|one-shot)\b",
                goal,
                re.IGNORECASE,
            )
            and re.search(r"\bexits?\b", goal, re.IGNORECASE)
        )
        if one_shot:
            goal_commands = h12.public_commands(
                {
                    "environment": task.get("environment", {}),
                    "acceptance": {"regression": []},
                }
            )
            executable = (
                goal_commands
                if environment_revision is None
                else [
                    command
                    for command in goal_commands
                    if not any(
                        value in {"pytest", "unittest", "test", "tsc", "mocha", "ava"}
                        for value in command
                    )
                ]
            )
            if executable and (environment_revision is None or len(executable) == 1):
                launch = list(executable[0])
                if launch[0] in {"python", "python3"}:
                    launch[0] = "python3"
                    if "-B" not in launch:
                        launch.insert(1, "-B")
                service = tuple(h12_judge._normalize(launch))
                service_mode = "oneshot"
        else:
            suffixes = (".py",) if task["language"] == "python" else (".js", ".mjs")
            entries = [path for path in scope if path.endswith(suffixes)]
            entry = (
                entries[0]
                if entries and (environment_revision is None or len(entries) == 1)
                else ""
            )
            port_match = re.search(
                r"\b(?:default(?:\s+port)?|port)\s*[:=]?\s*([0-9]{2,5})\b",
                goal,
                re.IGNORECASE,
            )
            route = re.search(r"\bGET\s+[`'\"]?(/[A-Za-z0-9_/-]*)", goal)
            if entry and port_match and route and 1 <= int(port_match[1]) <= 65535:
                service = (
                    ("python3", "-B", entry) if task["language"] == "python" else ("node", entry)
                )
                port, service_path = int(port_match[1]), route[1]
    if environment_revision in {DEV_ENVIRONMENT_V4, HOLDOUT_INFRA_V4} and task["type"] == "run_app":
        from mgar_v3.quality_eval.h12_execution_plan import application_plan

        plan = application_plan(task, safe)
        service, service_mode, port, service_path = plan
    independent = (independent_regression_plan
                   if isinstance(independent_regression_plan, RegressionPlan) else None)
    if independent_regression_plan == "public-v1" and acceptance_protocol == "contract-v1":
        independent = build_regression_plan(
            h12_judge.public_regression_commands(task, environment_revision)
            if task.get("acceptance", {}).get("regression") else [])
    return EvaluationProfile(
        experiment_id=experiment_id,
        task_id=safe["task_id"],
        split=safe["split"],
        arm=arm,
        task_kind=safe["type"],
        objective=h12.public_objective(task),
        output_policy=output_policy if acceptance_protocol == "contract-v1" else None,
        tool_error_policy=tool_error_policy if acceptance_protocol == "contract-v1" else None,
        lifecycle_policy=lifecycle_policy if acceptance_protocol == "contract-v1" else None,
        independent_regression_plan=independent,
        lifecycle_routes=public_routes(h12.public_objective(task))
        if lifecycle_policy is not None and acceptance_protocol == "contract-v1"
        and task["type"] == "run_app" and service_mode == "http" else (),
        acceptance_protocol=acceptance_protocol,
        public_requirements=h12.public_requirements(task)
        if acceptance_protocol == "contract-v1" else (),
        acceptance_baseline_commit=safe.get("base_commit")
        if acceptance_protocol == "contract-v1" else None,
        resources=EvaluationResources(**safe["resources"], max_output_tokens=8192),
        model=FLASH.model_dump(mode="json"),
        read_paths=("",),
        write_paths=scope,
        public_test_argv=tuple(argv),
        public_probe=public_probe,
        verification_protocol="public-probe-v1" if public_probe is not None else "legacy",
        service_argv=service,
        service_mode=service_mode,
        service_port=port,
        service_path=service_path,
        sandbox_offline_network="connect-timeout-v1"
        if environment_revision in {DEV_ENVIRONMENT_V4, HOLDOUT_INFRA_V4}
        and task["repository"] == "psf/requests"
        else "got-network-v1"
        if environment_revision in {DEV_ENVIRONMENT_V4, HOLDOUT_INFRA_V4}
        and task["repository"] == "sindresorhus/got"
        else "none",
        sandbox_nofile=1024 if environment_revision in REPAIRED_REVISIONS else 128,
        sandbox_log_bytes=1048576 if environment_revision in REPAIRED_REVISIONS else 65536,
        sandbox_memory_mb=2048
        if environment_revision in {DEV_ENVIRONMENT_V4, HOLDOUT_INFRA_V4}
        and task["repository"] == "sindresorhus/got"
        else 1024
        if environment_revision in REPAIRED_REVISIONS and task["repository"] == "sindresorhus/got"
        else 512
        if environment_revision in {DEV_ENVIRONMENT_V4, HOLDOUT_INFRA_V4}
        and task["language"] == "go"
        else 256,
    )


class EvaluationRunner:
    def __init__(
        self,
        engine: Any,
        *,
        experiment_id: str,
        evidence: Path,
        image_locks: Mapping[str, str],
        api_key: str | None,
        root: Path | None = None,
        execute_live: bool = False,
        registry_path: Path | None = None,
        registry_sha256: str | None = None,
        environment_revision: str | None = None,
        public_plan_refs: Mapping[str, tuple[Path, str]] | None = None,
        public_probe_root: Path | None = None,
        public_readiness_ref: tuple[Path, str] | None = None,
        full_dev_readiness_ref: tuple[Path, str] | None = None,
        semantic_revisions: Mapping[str, SemanticRevision] | None = None,
        public_feedback_policy: PublicFeedbackPolicy | None = None,
    ) -> None:
        self.engine, self.store = engine, engine.harness
        self.root = root or repo_root()
        self.experiment_id, self.evidence = experiment_id, evidence
        self.image_locks, self.api_key = dict(image_locks), api_key
        self.execute_live = execute_live
        if environment_revision not in {None, *DEV_REVISIONS}:
            raise CatalogError("Unsupported trusted environment revision")
        if execute_live and environment_revision not in DEV_REVISIONS:
            raise DomainError(
                "H12_ENVIRONMENT_REVISION_REQUIRED",
                "New paid execution requires the explicit dev environment revision",
                403,
            )
        self.environment_revision = environment_revision
        if public_plan_refs is not None and (
            environment_revision != DEV_ENVIRONMENT_V4 or public_probe_root is None
        ):
            raise CatalogError("Public plans require an explicit dev-v4 candidate and asset root")
        self.public_plan_refs = dict(public_plan_refs) if public_plan_refs is not None else None
        if self.public_plan_refs == {} or (
            public_readiness_ref is not None and public_plan_refs is None
        ):
            raise CatalogError("Public readiness needs a nonempty explicit public plan subset")
        self.public_probe_root = public_probe_root
        self.public_readiness_ref = public_readiness_ref
        self.full_dev_readiness_ref = full_dev_readiness_ref
        self.semantic_revisions = dict(semantic_revisions or {})
        self.public_feedback_policy = public_feedback_policy
        if semantic_revisions is not None and not self.semantic_revisions:
            raise CatalogError("Semantic revisions require a nonempty explicit task subset")
        self._dev_readiness_passed = False
        self.registry_path, self.registry_sha256 = registry_path, registry_sha256
        self.evidence.mkdir(parents=True, exist_ok=True)

    def _tasks(self, split: str) -> list[dict[str, Any]]:
        return load_tasks(split, self.root)

    def _revision_for(self, task: Mapping[str, Any]) -> str | None:
        return self.environment_revision

    def _profile_for(self, task: Mapping[str, Any], arm: str) -> EvaluationProfile:
        profile = profile_for(
            task, self.experiment_id, arm,
            environment_revision=self._revision_for(task),
        )
        frozen = self._public_plan_for(task, profile.public_test_argv)
        profile = profile.model_copy(update={
            "public_probe": frozen.plan, "verification_protocol": "public-probe-v1",
        }) if frozen else profile
        return self._bind_regression_metadata(task, profile)

    def _bind_regression_metadata(
        self, task: Mapping[str, Any], profile: EvaluationProfile,
    ) -> EvaluationProfile:
        profile = self._bind_semantic_revision(task, profile)
        plan = profile.independent_regression_plan
        if plan is None or not any(s.framework == "unconfigured" for s in plan.commands):
            return self._bind_public_feedback(profile)
        source = self._source(task)
        if source is None:
            return self._bind_public_feedback(profile)
        package: Mapping[str, Any] | None = None
        commit = str(task.get("base_commit", ""))
        if h12._git(source, "rev-parse", "--verify", commit + "^{commit}") == commit:
            raw = h12._git(source, "show", commit + ":package.json")
        else:
            path = source / "package.json"
            raw = path.read_text() if path.is_file() and not path.is_symlink() else ""
        try:
            value = json.loads(raw or "")
            if isinstance(value, dict):
                package = value
        except ValueError:
            pass
        return self._bind_public_feedback(profile.model_copy(update={
            "independent_regression_plan": build_regression_plan(
            h12_judge.public_regression_commands(task, self._revision_for(task)), package=package,
        )}))

    def _bind_public_feedback(self, profile: EvaluationProfile) -> EvaluationProfile:
        policy = getattr(self, "public_feedback_policy", None)
        if policy is None:
            return profile
        if profile.baseline_policy is None:
            raise CatalogError("R07 requires a new R06-reviewed benchmark revision")
        values = profile.model_dump(mode="json")
        values["public_feedback_policy"] = policy.model_dump(mode="json")
        return EvaluationProfile.model_validate(values)

    def _bind_semantic_revision(
        self,
        task: Mapping[str, Any],
        profile: EvaluationProfile,
    ) -> EvaluationProfile:
        revision = getattr(self, "semantic_revisions", {}).get(str(task["task_id"]))
        if revision is None:
            return profile
        if revision.policy.benchmark_revision == profile.acceptance_revision:
            raise CatalogError("R06 semantics require a distinct new benchmark revision")
        declared = h12_judge.public_regression_commands(task, self._revision_for(task))
        if [list(c.public_argv) for c in revision.policy.behavior_plan.commands] != declared:
            raise CatalogError(
                "R06 behavior scope differs from the publicly declared full regression")
        values = profile.model_dump(mode="json")
        values.update(
            baseline_policy=revision.policy.model_dump(mode="json"),
            independent_regression_plan=revision.policy.behavior_plan.model_dump(mode="json"),
            acceptance_revision=revision.policy.benchmark_revision,
            structural_probe=revision.structural_probe.model_dump(mode="json")
            if revision.structural_probe
            else None,
            judge_traceability_sha256=revision.traceability_sha256,
            judge_registry_sha256=revision.registry_sha256,
        )
        return EvaluationProfile.model_validate(values)

    def _semantic_options(
        self,
        task: Mapping[str, Any],
        profile: EvaluationProfile,
    ) -> dict[str, Any]:
        if profile.baseline_policy is None:
            return {}
        revision = getattr(self, "semantic_revisions", {}).get(str(task["task_id"]))
        if (
            revision is None
            or revision.policy != profile.baseline_policy
            or (revision.traceability_sha256 != profile.judge_traceability_sha256)
            or revision.registry_sha256 != profile.judge_registry_sha256
        ):
            raise CatalogError("Frozen R06 review binding is missing or changed")
        return {
            "baseline_policy": profile.baseline_policy,
            "traceability_ref": (revision.traceability_path, revision.traceability_sha256),
            "evaluation_artifacts": self.engine.artifacts,
            "experiment_id": self.experiment_id,
            "public_probe_root": self.public_probe_root,
        }

    def _review_semantic_judge(
        self,
        task: Mapping[str, Any],
        profile: EvaluationProfile,
        case: Path,
    ) -> None:
        if profile.baseline_policy is None:
            return
        from mgar_v3.quality_eval.h12_judge_review import read_manifest, review_judge

        options = self._semantic_options(task, profile)
        manifest = read_manifest(*options["traceability_ref"])
        # Private staging is never registered as a project or included in worker context.
        with tempfile.TemporaryDirectory(
            prefix="magr-r06-private-review-", dir="/tmp"
        ) as directory:
            bundle = h12_judge.prepare_judge(
                task, Path(directory) / "private",
                registry_path=self._registry_for(task)[0],
                registry_sha256=self._registry_for(task)[1],
            )
            if bundle["baseline_oracle"]:
                raise CatalogError(
                    "R06 requires a revised Judge with separate public baseline behavior")
            sources = {item["target"]: Path(item["file"]).read_bytes() for item in bundle["files"]}
            proof = review_judge(
                profile.baseline_policy, manifest, sources, task_id=str(task["task_id"]),
                commands=bundle["commands"])
        path = case / "r06-review-preflight.json"
        if not path.exists():
            private_json(path, {
                **proof, "judge_traceability_sha256": options["traceability_ref"][1]})

    def _validate_semantic_freeze(self, tasks: Sequence[Mapping[str, Any]]) -> None:
        if not getattr(self, "semantic_revisions", {}):
            return
        for task in tasks:
            profile = self._profile_for(task, "candidate")
            if profile.baseline_policy is not None:
                self._review_semantic_judge(
                    task, profile, self.evidence / "r06-freeze-review" / str(task["task_id"]))
                if profile.structural_probe is not None:
                    from mgar_v3.harness.public_probe import read_asset

                    if self.public_probe_root is None:
                        raise CatalogError("R06 structural public asset root is unavailable")
                    read_asset(profile.structural_probe, self.public_probe_root)

    def _public_plan_for(
        self, task: Mapping[str, Any], regression_argv: Sequence[str],
    ) -> FrozenPublicPlan | None:
        if self.public_plan_refs is None:
            return None
        reference = self.public_plan_refs.get(str(task["task_id"]))
        if reference is None:
            return None  # Explicitly unlisted tasks retain their frozen legacy protocol.
        if self.public_probe_root is None:
            raise CatalogError("An explicit frozen public behavior plan is missing")
        return load_public_plan(
            reference[0], reference[1], task=task, asset_root=self.public_probe_root,
            regression_argv=regression_argv,
        )

    def _public_plan_metadata(self, tasks: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
        if self.public_plan_refs is None:
            return {}
        if not self.public_plan_refs or not set(self.public_plan_refs).issubset(
            {str(task["task_id"]) for task in tasks if task.get("split") == "dev"}
        ):
            raise CatalogError("Public plans must name a nonempty subset of development tasks")
        result = {}
        for task in tasks:
            profile = profile_for(task, self.experiment_id, "candidate",
                                  environment_revision=self._revision_for(task))
            frozen = self._public_plan_for(task, profile.public_test_argv)
            if frozen is None:
                continue
            source = self._source(task)
            if source is None:
                raise CatalogError("Public plan original source is unavailable")
            original = self._materialize(task, source)
            try:
                validate_public_original(frozen, original)
                result[str(task["task_id"])] = frozen.freeze_metadata()
            finally:
                shutil.rmtree(original.parent)
        return result

    def prepare_public_readiness(self, evidence: Path) -> dict[str, Any]:
        """Execute only the explicit public dev subset, without model or private Judge.

        A ready subset does not establish complete twelve-task readiness or
        authorize paid dispatch. This method does not initialize an experiment.
        """
        from mgar_v3.quality_eval.h12_public_readiness import (
            execute_public_original,
            write_evidence_json,
        )

        if self.public_plan_refs is None:
            raise CatalogError("No explicit public development plans were supplied")
        tasks = self._tasks("dev")
        metadata = self._public_plan_metadata(tasks)
        source_before = self._candidate_fingerprint()
        evidence.mkdir(parents=True, exist_ok=False)
        results: dict[str, dict[str, Any]] = {}
        for task in tasks:
            task_id = str(task["task_id"])
            if task_id not in metadata:
                continue
            profile = self._profile_for(task, "candidate")
            frozen = self._public_plan_for(task, profile.public_test_argv)
            assert frozen is not None
            source = self._source(task)
            lock = self._lock_for(task)
            if source is None or lock is None:
                results[task_id] = {
                    "status": "NOT_RUN", "blockers": ["PUBLIC_SOURCE_OR_IMAGE_MISSING"],
                }
                continue
            workspace = self._materialize(task, source)
            name = hashlib.sha256(task_id.encode()).hexdigest()
            try:
                result = execute_public_original(
                    frozen, workspace, profile, Path(lock), evidence / name,
                )
                results[task_id] = {"directory": name, "result": result}
            except (DomainError, CatalogError, OSError) as exc:
                results[task_id] = {
                    "directory": name, "status": "NOT_RUN",
                    "blockers": [
                        exc.code if isinstance(exc, DomainError)
                        else "PUBLIC_READINESS_EXECUTION_FAILED"
                    ],
                }
            finally:
                shutil.rmtree(workspace.parent)
        source_after = self._candidate_fingerprint()
        ready = source_before == source_after and all(
            item.get("result", {}).get("status") == "READY" for item in results.values()
        )
        report = {
            "schema": "h12-public-subset-readiness-v1",
            "status": "PUBLIC_SUBSET_READY" if ready else "NOT_RUN",
            "zero_model": True, "readiness_only": True,
            "paid_model_calls": 0, "cost_micros": 0,
            "quality_status": "NOT_RUN", "full_dev_status": "NOT_RUN",
            "paid_dispatch_allowed": False,
            "blockers": ["FULL_TWELVE_DEV_PUBLIC_READINESS_NOT_ESTABLISHED"],
            "candidate_source_sha256_before": source_before,
            "candidate_source_sha256_after": source_after,
            "environment_revision": self.environment_revision,
            "environment_manifest_sha256": self._environment_manifest_hash(),
            "public_plans": metadata, "task_ids": sorted(metadata),
            "legacy_task_ids": sorted(
                str(task["task_id"]) for task in tasks if str(task["task_id"]) not in metadata
            ),
            "tasks": results,
        }
        write_evidence_json(evidence / "report.json", report)
        return report

    def validate_public_readiness(self) -> dict[str, Any]:
        """Validate an explicitly pinned engineering report; grant no budget."""
        from mgar_v3.quality_eval.h12_public_readiness import (
            read_evidence_json,
            validate_public_original_readiness,
        )

        if self.public_readiness_ref is None or self.public_plan_refs is None:
            raise CatalogError("An explicit public subset readiness report is required")
        path, expected = self.public_readiness_ref
        report = read_evidence_json(path, expected)
        tasks = self._tasks("dev")
        metadata = self._public_plan_metadata(tasks)
        source = self._candidate_fingerprint()
        if (
            report.get("schema") != "h12-public-subset-readiness-v1"
            or report.get("status") != "PUBLIC_SUBSET_READY"
            or report.get("zero_model") is not True or report.get("readiness_only") is not True
            or report.get("paid_model_calls") != 0 or report.get("cost_micros") != 0
            or report.get("quality_status") != "NOT_RUN"
            or report.get("full_dev_status") != "NOT_RUN"
            or report.get("paid_dispatch_allowed") is not False
            or report.get("candidate_source_sha256_before") != source
            or report.get("candidate_source_sha256_after") != source
            or report.get("environment_revision") != self.environment_revision
            or report.get("environment_manifest_sha256") != self._environment_manifest_hash()
            or report.get("public_plans") != metadata
            or report.get("task_ids") != sorted(metadata)
            or report.get("legacy_task_ids") != sorted(
                str(task["task_id"]) for task in tasks if str(task["task_id"]) not in metadata
            )
            or not isinstance(report.get("tasks"), dict) or set(report["tasks"]) != set(metadata)
        ):
            raise CatalogError("Public subset readiness belongs to another source, plan or scope")
        for task in tasks:
            task_id = str(task["task_id"])
            if task_id not in metadata:
                continue
            profile = self._profile_for(task, "candidate")
            frozen = self._public_plan_for(task, profile.public_test_argv)
            assert frozen is not None
            entry = report["tasks"][task_id]
            directory = hashlib.sha256(task_id.encode()).hexdigest()
            lock = self._lock_for(task)
            if entry.get("directory") != directory or lock is None:
                raise CatalogError("Public readiness execution evidence or image is missing")
            validate_public_original_readiness(
                entry.get("result", {}), path.parent / directory,
                frozen=frozen, profile=profile, image_lock=Path(lock),
            )
        return report

    def _full_dev_bindings(self) -> dict[str, Any]:
        return self._readiness_bindings("dev")

    def _readiness_bindings(self, split: str) -> dict[str, Any]:
        from mgar_v3.quality_eval.h12_full_dev_readiness import task_binding

        tasks = self._tasks(split)
        task_ids = [str(task["task_id"]) for task in tasks]
        if len(task_ids) != 12 or len(set(task_ids)) != 12 or any(
            task.get("split") != split for task in tasks
        ):
            raise CatalogError("Complete readiness needs exactly twelve distinct dev tasks")
        if split == "dev":
            self._public_plan_metadata(tasks)  # Reject unknown or missing explicit references.
        result = {}
        for task in tasks:
            profile = self._profile_for(task, "candidate")
            source, lock = self._source(task), self._lock_for(task)
            if source is None or lock is None:
                raise CatalogError("Full public readiness requires every pinned source and image")
            original = self._materialize(task, source)
            try:
                public = self._public_plan_for(task, profile.public_test_argv)
                revision = self._revision_for(task)
                binding = task_binding(
                    task, profile, original, Path(lock), revision, public,
                    public_root=self.root, split=split,
                )
                binding["environment_manifest_sha256"] = (
                    hashlib.sha256((self.root / "experiments/harness-v4" /
                                    f"{revision}.json").read_bytes()).hexdigest()
                    if revision is not None else None
                )
                result[str(task["task_id"])] = binding
            finally:
                shutil.rmtree(original.parent)
        return result

    def prepare_full_dev_readiness(self, evidence: Path) -> dict[str, Any]:
        """Run all twelve original public prerequisites with no model or Judge."""
        return self._prepare_split_readiness("dev", evidence)

    def _prepare_split_readiness(self, split: str, evidence: Path) -> dict[str, Any]:
        from mgar_v3.quality_eval.h12_full_dev_readiness import (
            HOLDOUT_SCHEMA,
            SCHEMA,
            execute_task,
        )
        from mgar_v3.quality_eval.h12_public_readiness import write_evidence_json

        before = self._candidate_fingerprint()
        bindings = self._readiness_bindings(split)
        evidence.mkdir(parents=True, exist_ok=False)
        results: dict[str, dict[str, Any]] = {}
        for task in self._tasks(split):
            task_id = str(task["task_id"])
            directory = hashlib.sha256(task_id.encode()).hexdigest()
            source = self._source(task)
            assert source is not None
            original = self._materialize(task, source)
            profile = self._profile_for(task, "candidate")
            try:
                if bindings[task_id]["original_tree"] != self._tree_hash_for(original):
                    raise CatalogError("Pinned original source changed during preparation")
                public = self._public_plan_for(task, profile.public_test_argv)
                result = execute_task(original, binding=bindings[task_id], public=public,
                                      evidence=evidence / directory)
            except (DomainError, CatalogError, OSError) as exc:
                result = {"status": "NOT_RUN", "blockers": [
                    exc.code if isinstance(exc, DomainError) else "PUBLIC_PREPARATION_FAILED"
                ]}
            finally:
                shutil.rmtree(original.parent)
            results[task_id] = {"directory": directory, "result": result}
        after = self._candidate_fingerprint()
        count = sum(row["result"]["status"] == "READY" for row in results.values())
        report = {
            "schema": SCHEMA if split == "dev" else HOLDOUT_SCHEMA,
            "status": f"FULL_{split.upper()}_READY"
            if count == 12 and before == after else "NOT_RUN",
            "ready_count": count, "task_ids": sorted(bindings), "bindings": bindings,
            "candidate_source_sha256_before": before, "candidate_source_sha256_after": after,
            "zero_model": True, "readiness_only": True, "quality_status": "NOT_RUN",
            "paid_model_calls": 0, "cost_micros": 0, "tasks": results,
        }
        write_evidence_json(evidence / "report.json", report)
        return report

    @staticmethod
    def _tree_hash_for(path: Path) -> str:
        from mgar_v3.harness.workspace import _scan, _tree_hash

        return _tree_hash(_scan(path)[0])

    def validate_full_dev_readiness(self) -> dict[str, Any]:
        from mgar_v3.quality_eval.h12_full_dev_readiness import (
            validate_full_report,
        )
        from mgar_v3.quality_eval.h12_public_readiness import read_evidence_json

        if self.full_dev_readiness_ref is None:
            raise CatalogError("An explicitly pinned full development readiness report is required")
        path, expected = self.full_dev_readiness_ref
        report = read_evidence_json(path, expected)
        bindings = self._full_dev_bindings()
        public = {}
        for task in self._tasks("dev"):
            profile = self._profile_for(task, "candidate")
            frozen = self._public_plan_for(task, profile.public_test_argv)
            if frozen is not None:
                public[str(task["task_id"])] = frozen
        validate_full_report(report, path.parent, bindings=bindings,
                             candidate_source=self._candidate_fingerprint(), public_plans=public)
        return report

    def _full_dev_readiness_metadata(self) -> dict[str, str] | None:
        if self.full_dev_readiness_ref is None:
            return None
        return {"path": str(self.full_dev_readiness_ref[0].resolve()),
                "sha256": self.full_dev_readiness_ref[1]}

    def _registry_for(self, task: Mapping[str, Any]) -> tuple[Path | None, str | None]:
        revision = getattr(self, "semantic_revisions", {}).get(str(task["task_id"]))
        if revision is not None:
            return revision.registry_path, revision.registry_sha256
        if task["split"] == "holdout":
            return self.registry_path, self.registry_sha256
        return None, None

    def _lock_for(self, task: Mapping[str, Any]) -> str | None:
        return self.image_locks.get(task["repository"] + "@" + task["base_commit"])

    def _log_limit_for(self, task: Mapping[str, Any]) -> int:
        return 1048576 if self.environment_revision in DEV_REVISIONS else 524288

    def _after_materialize(self, task: Mapping[str, Any], target: Path) -> Path:
        return target

    def initialize(self) -> None:
        frozen = h12.frozen_hashes(self.root)
        if frozen["status"] != "PASS":
            raise DomainError("H12_FROZEN_INPUT_CHANGED", "Frozen H00 bytes changed", 409)
        authorization_path = self.root / "experiments/harness-v4/h12-authorization-v1.json"
        authorization = json.loads(authorization_path.read_text())
        if authorization.get("max_cost_micros") != 12_000_000 or not authorization.get(
            "allow_paid"
        ):
            raise DomainError(
                "H12_AUTHORIZATION_REQUIRED", "Explicit H12 approval is required", 403
            )
        # This method runs in the isolated evaluator. Do not print this config:
        # safe profiles include each held-out goal, scoped to its own Run only.
        tasks_for_freeze = [
            task
            for split in (
                ("dev",) if self.environment_revision in DEV_REVISIONS else ("dev", "holdout")
            )
            for task in self._tasks(split)
        ]
        profiles = [
            self._profile_for(task, arm).model_dump(mode="json")
            for task in tasks_for_freeze
            for arm in ARMS
        ]
        self._validate_semantic_freeze(tasks_for_freeze)
        model_path = self.root / MODEL_OBSERVATION
        config = {
            "schema": "harness-h12-experiment-v1",
            "profiles": profiles,
            "frozen_catalog": frozen,
            "model_observation_sha256": hashlib.sha256(model_path.read_bytes()).hexdigest(),
            "arms": list(ARMS),
            "independent_registry_sha256": self.registry_sha256,
            "image_locks": {
                key: {"path": value, "sha256": hashlib.sha256(Path(value).read_bytes()).hexdigest()}
                for key, value in self.image_locks.items()
            },
            "candidate_source": self._candidate_fingerprint(),
            "environment_revision": self.environment_revision,
        }
        if self.environment_revision == DEV_ENVIRONMENT_V4:
            config["environment_manifest_sha256"] = self._environment_manifest_hash()
        if self.public_plan_refs is not None:
            assert self.public_probe_root is not None
            config["public_plans"] = self._public_plan_metadata(tasks_for_freeze)
            config["public_probe_root"] = str(self.public_probe_root.resolve())
            if self.public_readiness_ref is not None:
                self.validate_public_readiness()
                config["public_readiness"] = {
                    "path": str(self.public_readiness_ref[0].resolve()),
                    "sha256": self.public_readiness_ref[1],
                }
        if self.full_dev_readiness_ref is not None:
            self.validate_full_dev_readiness()
            config["full_dev_readiness"] = self._full_dev_readiness_metadata()
        approval = {
            "approved_by": self.engine.settings.local_actor,
            "allow_paid": True,
            "max_cost_micros": 12_000_000,
            "max_calls": 1536,
            "source_authorization_sha256": hashlib.sha256(
                authorization_path.read_bytes()
            ).hexdigest(),
        }
        self.store.create_experiment(self.experiment_id, config, approval)

    def _environment_manifest_hash(self) -> str:
        path = self.root / f"experiments/harness-v4/{self.environment_revision}.json"
        return hashlib.sha256(path.read_bytes()).hexdigest()

    def _candidate_fingerprint(self) -> str:
        # Same public frozen source method as the registered engineering gates.
        from importlib.util import module_from_spec, spec_from_file_location

        spec = spec_from_file_location("h12_verify_fingerprint", self.root / "scripts/verify.py")
        assert spec is not None and spec.loader is not None
        module = module_from_spec(spec)
        spec.loader.exec_module(module)
        return str(module.source_fingerprint(self.root))

    def _source(self, task: Mapping[str, Any]) -> Path | None:
        source = h12._source(
            task, self.root, {}, h12._public_v35_metadata(self.root, task["split"])
        )
        return Path(source["path"]) if source.get("available") is True else None

    def _materialize(self, task: Mapping[str, Any], source: Path) -> Path:
        directory = Path(tempfile.mkdtemp(prefix="magr-h12-source-", dir="/tmp"))
        target = directory / "project"
        try:
            return self._after_materialize(task, self._materialize_into(task, source, target))
        except BaseException:
            shutil.rmtree(directory, ignore_errors=True)
            raise

    def _materialize_into(self, task: Mapping[str, Any], source: Path, target: Path) -> Path:
        commit = task["base_commit"]
        if h12._git(source, "rev-parse", "--verify", commit + "^{commit}") == commit:
            # A pinned object may coexist with an unrelated or dirty HEAD. A
            # new repository receives only that object, with no inherited
            # hooks, filters, credentials, local configuration or worktree.
            environment = {
                "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
                "GIT_CONFIG_NOSYSTEM": "1",
                "GIT_CONFIG_GLOBAL": "/dev/null",
                "GIT_TERMINAL_PROMPT": "0",
                "GIT_LFS_SKIP_SMUDGE": "1",
                "GIT_OPTIONAL_LOCKS": "0",
            }
            commands = [
                ["git", "init", "-q", str(target)],
                [
                    "git",
                    "-C",
                    str(target),
                    "-c",
                    "core.hooksPath=/dev/null",
                    "fetch",
                    "--no-tags",
                    "--depth=1",
                    str(source),
                    commit,
                ],
                [
                    "git",
                    "-C",
                    str(target),
                    "-c",
                    "core.hooksPath=/dev/null",
                    "checkout",
                    "--detach",
                    commit,
                ],
                [
                    "git",
                    "-C",
                    str(target),
                    "remote",
                    "add",
                    "origin",
                    "https://github.com/" + task["repository"] + ".git",
                ],
            ]
            for command in commands:
                result = subprocess.run(
                    command, env=environment, capture_output=True, timeout=120, check=False
                )
                if result.returncode:
                    raise DomainError(
                        "H12_PIN_MATERIALIZATION_FAILED", "Pinned checkout failed", 409
                    )
            if h12._git(target, "rev-parse", "HEAD") != commit:
                raise DomainError("H12_PIN_MATERIALIZATION_FAILED", "Pinned HEAD differs", 409)
        else:
            before = h12._snapshot_digest(source)
            shutil.copytree(source, target, symlinks=True)
            if h12._snapshot_digest(target) != before or h12._snapshot_digest(source) != before:
                raise DomainError(
                    "H12_SOURCE_CHANGED", "Frozen snapshot changed while copying", 409
                )
        # Only public V35 tests, never gold or hidden implementation sources.
        reference = task.get("judge", {}).get("reference_task")
        if reference:
            split = task["split"]
            rows = h12._public_v35_metadata(self.root, split)
            record = next(
                (
                    row
                    for row in rows
                    if row["task_id"] == reference
                    and row["repository"] == task["repository"]
                    and row["base_commit"] == task["base_commit"]
                ),
                None,
            )
            if record is None:
                if any(
                    row["task_id"] == reference and row["repository"] == task["repository"]
                    for row in rows
                ):
                    raise DomainError(
                        "H12_PUBLIC_TEST_SOURCE_BINDING_CONFLICT",
                        "Frozen public reference is bound to a different source commit",
                        409,
                    )
                raise DomainError(
                    "H12_PUBLIC_TEST_UNAVAILABLE", "Public fixture is unavailable", 409
                )
            frozen_files = h12._public_v35_hashes(self.root, split)
            seen: set[str] = set()
            for name, path in zip(record["public_targets"], record["public_files"], strict=True):
                public_file = (self.root / "experiments/v35-harness" / path).resolve()
                relative = Path(name)
                if (
                    not public_file.is_relative_to(
                        self.root / "experiments/v35-harness" / split / "public"
                    )
                    or (self.root / "experiments/v35-harness" / path).is_symlink()
                    or not name
                    or relative.is_absolute()
                    or ".." in relative.parts
                    or "\\" in name
                    or name in seen
                    or not (target / name).resolve().is_relative_to(target.resolve())
                ):
                    raise DomainError("H12_PRIVATE_INPUT_DENIED", "Public test scope differs", 403)
                seen.add(name)
                raw = public_file.read_bytes()
                if hashlib.sha256(raw).hexdigest() != frozen_files.get(path):
                    raise DomainError("H12_PUBLIC_TEST_CHANGED", "Public test hash differs", 409)
                destination = target / name
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes(raw)
        for path in (target.parent, target, *target.rglob("*")):
            if not path.is_symlink():
                path.chmod(path.stat().st_mode | (0o755 if path.is_dir() else 0o644))
        return target

    def _not_run(self, task: Mapping[str, Any], arm: str, reason: str) -> dict[str, Any]:
        return {
            "task_id": task["task_id"],
            "split": task["split"],
            "type": task["type"],
            "arm": arm,
            "status": "NOT_RUN",
            "reason": reason,
            "candidate_status": "NOT_RUN",
            "raw_runtime_status": "NOT_RUN",
            "judge_status": "NOT_RUN",
            "usage_known": True,
            "cost_micros": 0,
            "reserved_micros": 0,
            "wall_seconds": 0,
            "attempts": [],
            **unverified_metrics("Independent tests not executed: " + reason),
        }

    def sample_usage(self, profile: EvaluationProfile) -> dict[str, Any] | None:
        """Read the selected sample's Engine facts, never allocate shared costs."""
        rows = [
            row
            for row in self.store.tasks()
            if row.get("experiment_id") == self.experiment_id
            and row.get("evaluation_profile") == profile.model_dump(mode="json")
        ]
        if not rows:
            return None
        if len(rows) != 1:
            raise DomainError("H12_DUPLICATE_SAMPLE", "Sample identity is ambiguous", 409)
        row = rows[0]
        with self.engine.transaction() as conn:
            calls = conn.execute(
                "SELECT u.*,o.state AS operation_state FROM app.usage_calls u "
                "LEFT JOIN app.harness_operations o ON o.model_call_id=u.call_id "
                "WHERE u.run_id=%s ORDER BY u.step_key",
                (row["run_id"],),
            ).fetchall()
            tools = conn.execute(
                "SELECT state,reserved_units,actual_units FROM app.harness_operations "
                "WHERE run_id=%s AND budget_kind='tool'",
                (row["run_id"],),
            ).fetchall()
        observations = []
        for call in calls:
            usage = call["usage"] or {}
            observations.append(
                {
                    "call_id": str(call["call_id"]),
                    "operation_id": call["step_key"],
                    "status": call["status"],
                    "operation_state": call["operation_state"],
                    "reserved_micros": int(call["reserved_micros"]),
                    "settled_micros": int(call["settled_micros"] or 0),
                    "input_tokens": int(usage.get("input_tokens", usage.get("prompt_tokens", 0))),
                    "output_tokens": int(
                        usage.get("output_tokens", usage.get("completion_tokens", 0))
                    ),
                }
            )
        unknown = sum(
            call["status"] == "unknown"
            or call["status"] == "reserved"
            and call["operation_state"] in {"dispatched", "unknown"}
            for call in observations
        )
        receipt = {
            "runtime_task_id": row["task_id"],
            "run_id": str(row["run_id"]),
            "engine_status": row["status"],
            "execution_epoch": row["epoch"],
            "tree_hash": row["tree_hash"],
            "result_hash": row["result_hash"],
            "model_calls": sum(
                call["status"] in {"known", "unknown"}
                or call["operation_state"] in {"dispatched", "unknown"}
                for call in observations
            ),
            "model_reservations": len(observations),
            "unknown_calls": unknown,
            "tool_calls": sum(
                int(
                    op["actual_units"]
                    if op["state"] == "known"
                    else op["reserved_units"]
                    if op["state"] != "released"
                    else 0
                )
                for op in tools
            ),
            "settled_cost_micros": sum(
                call["settled_micros"] for call in observations if call["status"] == "known"
            ),
            "reserved_micros": sum(
                call["reserved_micros"] for call in observations if call["status"] != "known"
            ),
            "input_tokens": sum(call["input_tokens"] for call in observations),
            "output_tokens": sum(call["output_tokens"] for call in observations),
            "peak_context_tokens": max(
                (call["input_tokens"] + call["output_tokens"] for call in observations), default=0
            ),
            "has_execution": row["status"] != "queued"
            or any(
                call["status"] in {"known", "unknown"}
                or call["operation_state"] in {"dispatched", "unknown"}
                for call in observations
            )
            or any(op["state"] in {"known", "dispatched", "unknown"} for op in tools),
            "calls": observations,
        }
        return receipt

    @staticmethod
    def _usage_fields(authority: Mapping[str, Any]) -> dict[str, Any]:
        unknown = bool(authority["unknown_calls"])
        return {
            key: authority[key]
            for key in (
                "run_id",
                "model_calls",
                "tool_calls",
                "input_tokens",
                "output_tokens",
                "peak_context_tokens",
                "reserved_micros",
                "settled_cost_micros",
            )
        } | {
            "usage_known": not unknown,
            "cost_micros": None if unknown else authority["settled_cost_micros"],
        }

    def fence_exception(self, authority: Mapping[str, Any], reason: str) -> dict[str, Any]:
        """Fence exactly this sample through Engine; never cancel or refund it."""
        task_id = str(authority["runtime_task_id"])
        row = self.store.get(task_id)
        if str(row["run_id"]) != authority["run_id"]:
            raise DomainError("H12_SAMPLE_CHANGED", "Exception Run identity differs", 409)
        for call in authority["calls"]:
            if call["status"] == "reserved" and call["operation_state"] in {
                "dispatched",
                "unknown",
            }:
                self.engine.settle_call(UUID(call["call_id"]), None)
        if row["status"] in {"running", "queued"}:
            row = self.store.pause(task_id)
        payload = {
            "schema": "h12-engine-exception-v1",
            "run_id": authority["run_id"],
            "task_id": task_id,
            "reason": reason,
            "outcome": "incomplete",
            "usage": dict(authority),
            "engine_status": row["status"],
            "execution_epoch": row["epoch"],
        }
        raw = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode()
        content_hash = self.engine.artifacts.put(raw)
        with self.engine.transaction() as conn:
            self.store._register(conn, content_hash, len(raw))
            self.engine._event(
                conn,
                UUID(authority["run_id"]),
                "h12_evaluation_exception",
                reason_code=reason,
                result_hash=content_hash,
                execution_epoch=row["epoch"],
            )
        return {
            "engine_status": row["status"],
            "execution_epoch": row["epoch"],
            "exception_receipt_hash": content_hash,
        }

    def _exception_score(
        self,
        task: Mapping[str, Any],
        profile: EvaluationProfile,
        reason: str,
        case: Path,
    ) -> dict[str, Any]:
        record = self._not_run(task, profile.arm, reason)
        authority = self.sample_usage(profile)
        if authority is None:
            return record
        record.update(self._usage_fields(authority))
        record.update(
            status="UNKNOWN"
            if authority["unknown_calls"]
            else "FAIL"
            if authority["has_execution"]
            else "NOT_RUN",
            raw_runtime_status="unknown"
            if authority["unknown_calls"]
            else "incomplete"
            if authority["has_execution"]
            else "NOT_RUN",
            final_tree=authority["tree_hash"],
            engine_usage=authority,
        )
        if authority["has_execution"] or authority["unknown_calls"]:
            record.update(self.fence_exception(authority, reason))
        private_json(case / f"runtime-exception-{time.time_ns()}.json", record)
        return record

    def evaluate_task(self, task: Mapping[str, Any], arm: str) -> dict[str, Any]:
        begun = time.monotonic()
        if arm == "candidate" and self.execute_live:
            self._live_policy(str(task.get("split", "")))
            self._require_dev_readiness()
        profile = self._profile_for(task, arm)
        case = self.evidence / arm / task["split"] / task["task_id"]
        record_path = case / "score.json"
        if record_path.exists():
            return dict(json.loads(record_path.read_text()))
        source = self._source(task)
        lock = self._lock_for(task)
        if arm == "legacy-v35":
            record = self._not_run(task, arm, "LEGACY_SAME_MODEL_BUDGET_ADAPTER_UNAVAILABLE")
        elif source is None:
            record = self._not_run(task, arm, "PINNED_SOURCE_UNAVAILABLE")
        elif lock is None or not Path(lock).is_file():
            record = self._not_run(task, arm, "FROZEN_PROJECT_ENVIRONMENT_UNAVAILABLE")
        elif not self.execute_live or not self.api_key:
            record = self._not_run(task, arm, "EXPLICIT_LIVE_EXECUTION_UNAVAILABLE")
        elif self.store.get_experiment(self.experiment_id)["usage"]["unknown_calls"]:
            record = self._exception_score(
                task,
                profile,
                "UNKNOWN_CALL_RECONCILIATION_REQUIRED",
                case,
            )
        else:
            case.mkdir(parents=True, exist_ok=True, mode=0o700)
            try:
                record = self._execute(task, profile, source, Path(lock), case)
            except Exception as exc:
                record = self._exception_score(
                    task,
                    profile,
                    getattr(exc, "code", type(exc).__name__),
                    case,
                )
        record["experiment_id"] = self.experiment_id
        record["wall_seconds"] = time.monotonic() - begun
        if not record_path.exists():
            private_json(record_path, record)
        return record

    def _execute(
        self,
        task: Mapping[str, Any],
        profile: EvaluationProfile,
        source: Path,
        image_lock: Path,
        case: Path,
    ) -> dict[str, Any]:
        original = self._materialize(task, source)
        frozen_public = self._public_plan_for(task, profile.public_test_argv)
        if frozen_public is not None:
            validate_public_original(frozen_public, original)
            if profile.public_probe != frozen_public.plan:
                raise CatalogError("Public plan differs from the frozen evaluation profile")
        tests = h12_judge.test_hashes(original)
        self._review_semantic_judge(task, profile, case)
        semantic_options = self._semantic_options(task, profile)
        environment_path = case / "environment-private/judge-result.json"
        environment = (
            json.loads(environment_path.read_text())
            if environment_path.is_file()
            else h12_judge.verify(
                original,
                task,
                image_lock,
                case / "environment-private",
                phase="environment",
                nofile_limit=profile.sandbox_nofile,
                memory_mb=profile.sandbox_memory_mb,
                environment_revision=self._revision_for(task),
                offline_network=profile.sandbox_offline_network,
                log_bytes=self._log_limit_for(task),
                registry_path=self._registry_for(task)[0],
                registry_sha256=self._registry_for(task)[1],
                **semantic_options,
            )
        )
        if (
            environment["status"] != "PASS"
            or environment.get("tree_hash") != h12_judge.tree_hash(original)
            or environment.get("judge_sha256") != task["judge_sha256"]
        ):
            return {
                **self._not_run(task, profile.arm, "PUBLIC_ENVIRONMENT_PRECONDITION_UNAVAILABLE"),
                "environment_reason": environment.get("reason"),
                "environment_evidence": str(environment_path),
            }
        independent = None
        independent_baseline = None
        baseline_assessment_ref = None
        if profile.independent_regression_plan is not None:
            independent = IndependentRegressionRunner(
                self.engine.artifacts, image_lock, repository=str(task["repository"]),
                task_id=str(task["task_id"]), experiment_id=self.experiment_id,
                memory_mb=profile.sandbox_memory_mb, nofile=profile.sandbox_nofile,
                log_bytes=profile.sandbox_log_bytes,
                offline_network=profile.sandbox_offline_network,
                public_probe_root=self.public_probe_root,
            )
            if profile.baseline_policy is not None:
                from mgar_v3.quality_eval.h12_baseline_policy import assess_baseline

                baseline_assessment_ref = environment.get("baseline_assessment_ref")
                if not baseline_assessment_ref:
                    return self._not_run(task, profile.arm, "R06_BASELINE_ASSESSMENT_MISSING")
                assessment = BaselineAssessment.model_validate_json(
                    self.engine.artifacts.read(baseline_assessment_ref))
                recomputed = assess_baseline(
                    self.engine.artifacts, profile.baseline_policy, task_id=str(task["task_id"]),
                    experiment_id=self.experiment_id, tree=h12_judge.tree_hash(original),
                    behavior_ref=assessment.behavior_ref, target_ref=assessment.target_ref,
                )
                if recomputed != assessment or assessment.status != "PASS":
                    return self._not_run(task, profile.arm, "R06_BASELINE_ASSESSMENT_UNVERIFIED")
                independent_baseline = assessment.behavior_ref
                measured, _ = load_report(
                    self.engine.artifacts, independent_baseline,
                    profile.independent_regression_plan,
                    task_id=str(task["task_id"]), experiment_id=self.experiment_id,
                    phase="baseline", tree=h12_judge.tree_hash(original),
                )
            else:
                independent_baseline, measured = independent.execute(
                    original, profile.independent_regression_plan,
                    case / "independent-public/baseline", phase="baseline",
                    expected_tree=h12_judge.tree_hash(original), frozen_tests=tests,
                )
            baseline_complete = False
            try:
                _, baseline_receipts = load_report(
                    self.engine.artifacts, independent_baseline,
                    profile.independent_regression_plan,
                    task_id=str(task["task_id"]), experiment_id=self.experiment_id,
                    phase="baseline", tree=h12_judge.tree_hash(original),
                )
                baseline_complete = all(r.result.complete for r in baseline_receipts)
            except (DomainError, ValueError, KeyError, TypeError):
                pass
            if measured.status not in {"PASS", "FAIL"} or not baseline_complete:
                return {**self._not_run(task, profile.arm, "INDEPENDENT_BASELINE_UNVERIFIED"),
                        **unverified_metrics(measured.detail),
                        "independent_baseline_ref": independent_baseline}
        baseline = None
        if task["type"] in {"bugfix", "refactor"} and profile.baseline_policy is None:
            baseline_path = case / "baseline-private/judge-result.json"
            baseline = (
                json.loads(baseline_path.read_text())
                if baseline_path.is_file()
                else h12_judge.verify(
                    original,
                    task,
                    image_lock,
                    case / "baseline-private",
                    phase="baseline",
                    nofile_limit=profile.sandbox_nofile,
                    memory_mb=profile.sandbox_memory_mb,
                    environment_revision=self._revision_for(task),
                    offline_network=profile.sandbox_offline_network,
                    log_bytes=self._log_limit_for(task),
                    registry_path=self._registry_for(task)[0],
                    registry_sha256=self._registry_for(task)[1],
                    **semantic_options,
                )
            )
            if (
                baseline["status"] != "PASS"
                or baseline.get("tree_hash") != h12_judge.tree_hash(original)
                or baseline.get("judge_sha256") != task["judge_sha256"]
            ):
                return self._not_run(task, profile.arm, "PINNED_BASELINE_PRECONDITION_UNAVAILABLE")

        def final_gate(workspace: Any, candidate: dict[str, Any]) -> dict[str, Any]:
            row = self.store.tasks()
            current = next(item for item in row if item["task_id"] == workspace.manifest.task_id)
            def judge() -> dict[str, Any]:
                return h12_judge.verify(
                    workspace.root, task, image_lock, case / "judge-private",
                    expected_tree=candidate["tree_after"],
                    sealed=current.get("sealed_tree_hash") == candidate["tree_after"],
                    frozen_tests=tests,
                    baseline_evidence=case / "baseline-private" if baseline else None,
                    nofile_limit=profile.sandbox_nofile, memory_mb=profile.sandbox_memory_mb,
                    environment_revision=self._revision_for(task),
                    offline_network=profile.sandbox_offline_network,
                    log_bytes=self._log_limit_for(task), registry_path=self._registry_for(task)[0],
                    registry_sha256=self._registry_for(task)[1],
                    **semantic_options,
                )
            if independent is None:
                return judge()
            return evaluate_final(
                independent, profile, workspace.root, case / "independent-public",
                baseline_ref=independent_baseline, baseline_tree=candidate["tree_before"],
                final_tree=candidate["tree_after"], frozen_tests=tests,
                execution_task_id=current["task_id"], run_id=str(current["run_id"]),
                execution_epoch=current["execution_epoch"],
                sealed=current.get("sealed_tree_hash") == candidate["tree_after"],
                judge_sha256=str(task["judge_sha256"]),
                judge_root=case / "judge-private", judge=judge,
                baseline_assessment_ref=baseline_assessment_ref,
            )

        self._live_policy(str(task.get("split", "")))
        self._require_dev_readiness()
        public_options: dict[str, Any] = (
            {"public_probe_root": self.public_probe_root} if frozen_public is not None else {}
        )
        service = HarnessCodingService(
            self.engine.settings.harness_root,
            actor=self.engine.settings.local_actor,
            image_lock=image_lock,
            engine=self.engine,
            evaluation_profile=profile,
            final_gate=final_gate,
            live_api_key=self.api_key,
            allow_paid=True,
            model_factory=lambda request: RecordedFlashModel(
                api_key=self.api_key or "", evidence=case / "provider",
                artifacts=self.engine.artifacts,
            ),
            **public_options,
            **self._service_options(task, case),
        )
        # Reconstruct a selected existing Run, never turn a retry into a new sample.
        existing = [
            row
            for row in self.store.tasks()
            if row.get("experiment_id") == self.experiment_id
            and row.get("evaluation_profile") == profile.model_dump(mode="json")
        ]
        try:
            if existing:
                if len(existing) != 1:
                    raise DomainError("H12_DUPLICATE_SAMPLE", "First sample is ambiguous", 409)
                row = existing[0]
                if row["result_hash"]:
                    result = service.get_task(row["task_id"])
                elif row.get("sealed_tree_hash"):
                    return self._exception_score(
                        task,
                        profile,
                        "SEALED_RESULT_REQUIRES_RECONCILIATION",
                        case,
                    )
                else:
                    self.engine.recover()
                    result = service.recover_task(row["task_id"])
            else:
                project = service.register_project(str(original))
                result = service.create_task(
                    CodingTaskRequest(
                        project_id=project["project_id"],
                        objective=profile.objective,
                        task_kind=profile.task_kind,
                        mode="live",
                        approved=True,
                        test_command=shlex.join(profile.public_test_argv),
                    )
                )
        except Exception as exc:
            return self._exception_score(
                task,
                profile,
                getattr(exc, "code", type(exc).__name__),
                case,
            )
        if not (case / "runtime-result.json").exists():
            private_json(case / "runtime-result.json", result)
        return self._score_payload(task, profile, result)

    def _service_options(self, task: Mapping[str, Any], case: Path) -> dict[str, Any]:
        return {}

    def _score_payload(
        self, task: Mapping[str, Any], profile: EvaluationProfile, result: Mapping[str, Any]
    ) -> dict[str, Any]:
        gate = result.get("independent_gate") or {}
        completed = result.get("status") == "completed"
        group = self.store.get_experiment(self.experiment_id)
        authority = self.sample_usage(profile)
        unknown = bool(authority["unknown_calls"] if authority else group["usage"]["unknown_calls"])
        status = (
            "UNKNOWN"
            if unknown
            else "PASS"
            if completed and gate.get("status") == "PASS"
            else "FAIL"
        )
        score = {
            "task_id": task["task_id"],
            "split": task["split"],
            "type": task["type"],
            "arm": profile.arm,
            "status": status,
            "reason": result.get("reason"),
            "candidate_status": result.get("candidate_status"),
            "raw_runtime_status": result.get("status"),
            "judge_status": gate.get("status", "NOT_RUN"),
            "final_tree": result.get("tree_after"),
            "judge_tree": gate.get("tree_hash"),
            "original_public_commands": h12.public_commands(task),
            "executed_public_command": list(profile.public_test_argv),
            "usage_known": not unknown,
            "cost_micros": None if unknown else result.get("cost_micros", 0),
            "reserved_micros": group["reserved_micros"] if unknown else 0,
            "model_calls": result.get("model_calls", 0),
            "tool_calls": result.get("tool_calls", 0),
            **{
                key: result[key]
                for key in (
                    "input_tokens",
                    "output_tokens",
                    "peak_context_tokens",
                    "estimated_peak_input_tokens",
                    "repair_rounds",
                    "command_seconds",
                    "no_progress_rounds",
                    "compaction_count",
                )
                if key in result
            },
            "run_id": result.get("run_id"),
            "attempts": result.get("attempts", []),
            "wall_seconds": 0,
            "unauthorized_actions": 0,
            "unknown_blind_retries": 0,
            **{"model_phase_" + key: value for key, value in regression_metrics(
                result, profile.public_test_argv,
            ).items()},
        }
        score.update(unverified_metrics())
        try:
            anchored = self._anchored_independent_result(profile, result)
            reference = anchored["independent_gate"]["independent_evidence_ref"]
            independent_status, independent_metrics = assess_evaluation(
                self.engine.artifacts, reference, profile, task_id=str(task["task_id"]),
                judge_sha256=str(task["judge_sha256"]), result=anchored,
            )
            score.update(independent_metrics,
                judge_status=independent_metrics["independent_judge_status"],
                judge_tree=independent_metrics["independent_judge_tree"])
            if independent_status != "PASS" and score["status"] == "PASS":
                score.update(status="FAIL", reason="INDEPENDENT_REQUIRED_CHECKS_NOT_PASSED")
        except (DomainError, ValueError, KeyError, TypeError, AttributeError) as exc:
            score.update(unverified_metrics(getattr(
                exc, "code", "Independent Engine/CAS binding unavailable")))
        if (not score["regression_evidence_known"]
                or score.get("independent_public_regression_status") != "PASS"
        ) and score["status"] == "PASS":
            score.update(status="FAIL", reason="INDEPENDENT_REGRESSION_UNVERIFIED_OR_FAILED")
        if profile.acceptance_protocol == "contract-v1":
            from mgar_v3.harness.acceptance import score_binding

            try:
                binding = score_binding(result, profile, task)
                score.update(acceptance_integrity_status="PASS",
                             acceptance_binding=binding.model_dump(mode="json"))
            except DomainError as exc:
                score.update(acceptance_integrity_status="FAIL",
                             acceptance_integrity_reason=exc.code)
                if score["status"] == "PASS":
                    score.update(status="FAIL", reason=exc.code)
        else:
            score.update(acceptance_integrity_status="NOT_RUN")
        if profile.output_policy is not None:
            from mgar_v3.harness.output_protocol import validate_output_result

            try:
                validate_output_result(result, profile)
                score.update(output_integrity_status="PASS")
            except DomainError as exc:
                score.update(output_integrity_status="FAIL", output_integrity_reason=exc.code)
                if score["status"] == "PASS":
                    score.update(status="FAIL", reason=exc.code)
        else:
            score.update(output_integrity_status="NOT_RUN")
        if profile.lifecycle_policy is not None:
            from mgar_v3.harness.lifecycle_validation import validate_lifecycle_result

            try:
                validate_lifecycle_result(result, profile)
                score.update(lifecycle_integrity_status="PASS")
            except DomainError as exc:
                score.update(lifecycle_integrity_status="FAIL", lifecycle_integrity_reason=exc.code)
                if score["status"] == "PASS":
                    score.update(status="FAIL", reason=exc.code)
        else:
            score.update(lifecycle_integrity_status="NOT_RUN")
        if authority is not None:
            score.update(self._usage_fields(authority), engine_usage=authority)
        return score

    def _anchored_independent_result(
        self, profile: EvaluationProfile, result: Mapping[str, Any],
    ) -> dict[str, Any]:
        """Only the existing Engine-selected result can select scoring CAS refs."""
        if profile.independent_regression_plan is None:
            raise ValueError("No frozen independent regression plan")
        row = self.store.get(str(result.get("task_id", "")))
        if (row.get("experiment_id") != self.experiment_id or not row.get("result_hash")
                or row.get("evaluation_profile") != profile.model_dump(mode="json")
                or row.get("sealed_tree_hash") != result.get("tree_after")):
            raise ValueError("Scoring source is not the original sealed Engine result")
        anchored = load_result(
            self.engine.artifacts, self.engine.artifacts.read(row["result_hash"]),
            row["task_id"], str(row["run_id"]),
        )
        for key in ("task_id", "run_id", "execution_epoch", "tree_before", "tree_after",
                    "status", "candidate_status"):
            if anchored.get(key) != result.get(key):
                raise ValueError("Runtime result differs from Engine authority")
        expected = (anchored.get("independent_gate") or {}).get("independent_evidence_ref")
        actual = (result.get("independent_gate") or {}).get("independent_evidence_ref")
        if not expected or expected != actual:
            raise ValueError("Unselected independent CAS reference")
        if digest(anchored.get("independent_gate")) != digest(result.get("independent_gate")):
            raise ValueError("Caller changed the original Engine-selected gate projection")
        return anchored

    def append_regression_audit(
        self, task: Mapping[str, Any], profile: EvaluationProfile, result: Mapping[str, Any],
        *, original_score: Path, output: Path,
    ) -> dict[str, Any]:
        """Recompute into a new audit; never execute/resume or overwrite a score."""
        if output.exists() or output.resolve().is_relative_to(self.evidence.resolve()):
            raise ValueError("Regression audit needs a new directory outside historical evidence")
        before = original_score.read_bytes()
        old = json.loads(before)
        if old.get("task_id") != task["task_id"]:
            raise ValueError("Historical score belongs to another task")
        measured = self._score_payload(task, profile, result)
        record = {
            "schema": "h12-additive-regression-audit-v1", "original_score": str(original_score),
            "original_score_sha256": hashlib.sha256(before).hexdigest(),
            "original_status": old.get("status"), "recomputed": measured,
            "new_paid_calls": 0, "resumed": False,
            "scorer_source_sha256": self._candidate_fingerprint(),
        }
        if original_score.read_bytes() != before:
            raise DomainError("H12_ARCHIVE_CHANGED", "Historical score changed during audit", 409)
        private_json(output / "regression-audit.json", record)
        return record

    def correct_archived_scores(
        self,
        *,
        split: str,
        arm: str,
        output: Path,
        fence_run_ids: Sequence[str] = (),
    ) -> dict[str, Any]:
        """Append an accounting-only correction; never execute/resume a sample.

        The executed source is taken from the original immutable experiment,
        while this scorer's source is recorded separately. Execution still
        uses evaluate()'s original fingerprint checks; this audit never calls it.
        Only explicitly listed, exact, known-usage Runs may be fenced here.
        """
        if split not in {"dev", "holdout"} or arm not in ARMS:
            raise ValueError("Correction split or arm is invalid")
        config = self.store.get_experiment(self.experiment_id)["config"]
        if config["frozen_catalog"] != h12.frozen_hashes(self.root):
            raise DomainError("H12_FROZEN_INPUT_CHANGED", "Original catalog differs", 409)
        if output.exists():
            raise ValueError("Corrections require a new immutable output directory")
        selected = self._tasks(split)
        originals = []
        corrections = []
        allowed_fences = set(fence_run_ids)
        seen_fences: set[str] = set()
        for task in selected:
            originals_for_task = [p for p in config["profiles"]
                                  if p["task_id"] == task["task_id"]
                                  and p["split"] == split and p["arm"] == arm]
            if len(originals_for_task) != 1:
                raise DomainError("H12_CORRECTION_PROFILE_CHANGED",
                                  "Original profile identity differs", 409)
            original_profile = EvaluationProfile.model_validate(originals_for_task[0])
            profile = profile_for(
                task, self.experiment_id, arm,
                acceptance_protocol=original_profile.acceptance_protocol,
                output_policy=original_profile.output_policy,
                tool_error_policy=original_profile.tool_error_policy,
                lifecycle_policy=original_profile.lifecycle_policy,
                independent_regression_plan=original_profile.independent_regression_plan,
            )
            if original_profile.baseline_policy is not None:
                # Accounting-only replay uses the original frozen revision;
                # it must not silently fall back to the legacy baseline policy.
                values = profile.model_dump(mode="json")
                original_values = original_profile.model_dump(mode="json")
                for key in ("baseline_policy", "structural_probe", "judge_traceability_sha256",
                            "judge_registry_sha256", "acceptance_revision",
                            "public_feedback_policy"):
                    if key in original_values:
                        values[key] = original_values[key]
                profile = EvaluationProfile.model_validate(values)
            if profile.model_dump(mode="json") not in config["profiles"]:
                raise DomainError("H12_CORRECTION_PROFILE_CHANGED", "Original profile differs", 409)
            original_path = self.evidence / arm / split / task["task_id"] / "score.json"
            raw = original_path.read_bytes()
            original = json.loads(raw)
            if original["task_id"] != task["task_id"] or original["split"] != split:
                raise ValueError("Archived score identity differs")
            score = dict(original)
            authority = self.sample_usage(profile)
            if authority is not None:
                score.update(self._usage_fields(authority), engine_usage=authority)
                if authority["unknown_calls"]:
                    score.update(status="UNKNOWN", raw_runtime_status="unknown")
                elif authority["has_execution"] and original["status"] == "NOT_RUN":
                    score.update(status="FAIL", raw_runtime_status="incomplete")
                if authority["run_id"] in allowed_fences:
                    if authority["unknown_calls"] or authority["reserved_micros"]:
                        raise DomainError(
                            "H12_CORRECTION_USAGE_UNRESOLVED", "Usage is not settled", 409
                        )
                    score.update(
                        self.fence_exception(
                            authority, str(original.get("reason", "H12_SCORE_CORRECTION"))
                        )
                    )
                    seen_fences.add(authority["run_id"])
            originals.append(
                {
                    "task_id": task["task_id"],
                    "path": str(original_path),
                    "sha256": hashlib.sha256(raw).hexdigest(),
                    "status": original["status"],
                }
            )
            corrections.append(score)
        if seen_fences != allowed_fences:
            raise ValueError("An explicitly selected correction Run was not found")
        report = {
            "schema": "h12-accounting-correction-v1",
            "experiment_id": self.experiment_id,
            "split": split,
            "arm": arm,
            "classification": "accounting_only_no_execution",
            "executed_source_sha256": config["candidate_source"],
            "scoring_source_sha256": self._candidate_fingerprint(),
            "new_paid_calls": 0,
            "originals": originals,
            "summary": h12.score_records(corrections, split, expected_tasks=selected),
        }
        for score in corrections:
            private_json(output / score["task_id"] / "score-correction.json", score)
        private_json(output / "summary-correction.json", report)
        return report

    def _live_policy(self, split: str) -> None:
        if not self.execute_live:
            raise DomainError("H12_LIVE_EXECUTION_REQUIRED", "Paid dispatch is not enabled", 403)
        if self.environment_revision not in DEV_REVISIONS:
            raise DomainError(
                "H12_ENVIRONMENT_REVISION_REQUIRED",
                "New paid execution requires the explicit dev environment revision",
                403,
            )
        if split != "dev":
            raise DomainError(
                "H12_HISTORICAL_HOLDOUT_QUARANTINED",
                "Dev repairs require a future independent holdout protocol",
                409,
            )

    def _require_dev_readiness(self) -> None:
        self._live_policy("dev")
        if self.full_dev_readiness_ref is not None:
            self.validate_full_dev_readiness()
            self._validate_frozen_execution()
            self._dev_readiness_passed = True
            return
        if self.public_plan_refs is not None:
            if self.public_readiness_ref is not None:
                self.validate_public_readiness()
                raise DomainError(
                    "H12_FULL_DEV_READINESS_REQUIRED",
                    "The explicit public subset is ready; all twelve development prerequisites "
                    "still need complete public readiness before paid dispatch",
                    403,
                )
            raise DomainError(
                "H12_PUBLIC_PLAN_READINESS_REQUIRED",
                "New public plans require independently executed reproduction and regression "
                "readiness before any paid dispatch; asset binding alone is not readiness",
                403,
            )
        if self._dev_readiness_passed:
            self._validate_frozen_execution()
            return
        # A direct single-task invocation has the same gate as a whole split.
        from mgar_v3.quality_eval.h12_readiness import execution_readiness

        readiness_options: dict[str, Any] = (
            {"environment_revision": self.environment_revision}
            if self.environment_revision == DEV_ENVIRONMENT_V4 else {}
        )
        readiness = execution_readiness(
            self.image_locks,
            self.evidence / ("readiness-" + str(time.time_ns())),
            root=self.root,
            **readiness_options,
        )
        if readiness["status"] != "READY":
            raise DomainError(
                "H12_DEV_EXECUTION_NOT_READY",
                "All twelve dev prerequisites must execute before model dispatch",
                409,
            )
        config = self._validate_frozen_execution()
        if (
            readiness["candidate_source_sha256_after"] != config["candidate_source"]
            or readiness["candidate_source_sha256_after"] != self._candidate_fingerprint()
        ):
            raise DomainError(
                "H12_FROZEN_EXECUTION_CHANGED", "Readiness belongs to another source candidate", 409
            )
        self._dev_readiness_passed = True

    def _validate_frozen_execution(self) -> dict[str, Any]:
        config = self.store.get_experiment(self.experiment_id)["config"]
        observation = self.root / MODEL_OBSERVATION
        current_locks = {
            key: {"path": value, "sha256": hashlib.sha256(Path(value).read_bytes()).hexdigest()}
            for key, value in self.image_locks.items()
        }
        if (
            h12.frozen_hashes(self.root)["status"] != "PASS"
            or config["candidate_source"] != self._candidate_fingerprint()
            or config["image_locks"] != current_locks
            or config.get("environment_revision") != self.environment_revision
            or self.environment_revision == DEV_ENVIRONMENT_V4
            and config.get("environment_manifest_sha256") != self._environment_manifest_hash()
            or config["independent_registry_sha256"] != self.registry_sha256
            or config["model_observation_sha256"]
            != hashlib.sha256(observation.read_bytes()).hexdigest()
            or self.registry_path is not None
            and (
                hashlib.sha256(self.registry_path.read_bytes()).hexdigest() != self.registry_sha256
            )
        ):
            raise DomainError(
                "H12_FROZEN_EXECUTION_CHANGED",
                "Source, model, environment or private registry differs from the frozen experiment",
                409,
            )
        if self.public_plan_refs is not None or "public_plans" in config:
            if (
                config.get("public_plans") != self._public_plan_metadata(self._tasks("dev"))
                or self.public_probe_root is None
                or config.get("public_probe_root") != str(self.public_probe_root.resolve())
            ):
                raise DomainError("H12_PUBLIC_PLAN_CHANGED", "Frozen public plans differ", 409)
            current_readiness = (
                {"path": str(self.public_readiness_ref[0].resolve()),
                 "sha256": self.public_readiness_ref[1]}
                if self.public_readiness_ref is not None else None
            )
            if config.get("public_readiness") != current_readiness:
                raise DomainError(
                    "H12_PUBLIC_READINESS_CHANGED", "Frozen public readiness differs", 409,
                )
            if self.public_readiness_ref is not None:
                self.validate_public_readiness()
        if self.full_dev_readiness_ref is not None or "full_dev_readiness" in config:
            if config.get("full_dev_readiness") != self._full_dev_readiness_metadata():
                raise DomainError(
                    "H12_FULL_DEV_READINESS_CHANGED", "Frozen full readiness differs", 409,
                )
            self.validate_full_dev_readiness()
        return dict(config)

    def _development_only(self) -> bool:
        return self.environment_revision in DEV_REVISIONS

    def evaluate(self, *, split: str, arm: str, task_id: str | None = None) -> dict[str, Any]:
        if arm not in ARMS or split not in {"dev", "holdout"}:
            raise ValueError("H12 split or arm is invalid")
        if self.execute_live:
            self._live_policy(split)
        if self._development_only() and split != "dev":
            raise DomainError(
                "H12_HISTORICAL_HOLDOUT_QUARANTINED",
                "Dev repairs require a future independent holdout protocol",
                409,
            )
        self._validate_frozen_execution()
        if self.environment_revision in DEV_REVISIONS and arm == "candidate" and self.execute_live:
            # No single-task smoke may spend a model call while another dev
            # prerequisite is still incapable of execution.  Recheck the full
            # immutable matrix for each explicit invocation; no stale READY.
            self._dev_readiness_passed = False
            self._require_dev_readiness()
        selected = self._tasks(split)
        if task_id:
            selected = [task for task in selected if task["task_id"] == task_id]
            if not selected:
                raise ValueError("Frozen task id was not found")
        records = [self.evaluate_task(task, arm) for task in selected]
        summary = h12.score_records(records, split, expected_tasks=self._tasks(split))
        group = self.store.get_experiment(self.experiment_id)
        report = {
            "schema": "harness-h12-run-v1",
            "experiment_id": self.experiment_id,
            "arm": arm,
            "split": split,
            "summary": summary,
            "shared_budget": {
                key: group[key]
                for key in (
                    "limit_micros",
                    "reserved_micros",
                    "settled_micros",
                    "used_calls",
                    "call_limit",
                )
            },
            "usage": group["usage"],
            "paid_calls": group["used_calls"],
            "source_sha256": self._candidate_fingerprint(),
        }
        summary_path = self.evidence / f"summary-{arm}-{split}.json"
        if summary_path.exists():
            # Incremental/resumed invocations retain every earlier aggregate.
            summary_path = summary_path.with_name(f"summary-{arm}-{split}-{time.time_ns()}.json")
        private_json(summary_path, report)
        return report
