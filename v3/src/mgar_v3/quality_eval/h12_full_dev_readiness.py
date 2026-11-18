"""Complete, explicitly pinned public preparation for twelve development tasks.

No model, private Judge, held-out catalog, Engine connection, or budget is
created here. These facts establish execution prerequisites, never quality.
"""

from __future__ import annotations

import hashlib
import json
import shlex
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any, cast
from uuid import uuid4

from mgar_v3.domain import DomainError
from mgar_v3.harness.broker import (
    CapabilityLease,
    IsolatedExecutionBroker,
    expected_environment_hash,
    issue_lease,
)
from mgar_v3.harness.commands import CommandSession
from mgar_v3.harness.contracts import (
    CapabilityGrant,
    EvaluationProfile,
    ExecutionReceipt,
    TaskBinding,
    VerificationRequirement,
    canonical_hash,
)
from mgar_v3.harness.project_commands import parse_tests
from mgar_v3.harness.public_probe import normalize_probe_receipt
from mgar_v3.harness.workspace import _scan, _tree_hash
from mgar_v3.quality_eval import h12
from mgar_v3.quality_eval.h12_judge import public_regression_commands
from mgar_v3.quality_eval.h12_public_plan import FrozenPublicPlan, validate_public_original
from mgar_v3.quality_eval.h12_public_readiness import (
    _observe,
    read_evidence,
    read_evidence_json,
    write_evidence_json,
)
from mgar_v3.quality_eval.h12_public_regression import (
    declared_assertion_failure,
    missing_public_go_target,
    public_test_argv,
)
from mgar_v3.quality_eval.harness_v4 import CatalogError

SCHEMA = "h12-full-dev-public-readiness-v1"
HOLDOUT_SCHEMA = "h12-full-holdout-public-readiness-v1"
_DEPENDENCIES = (
    "pyproject.toml",
    "setup.cfg",
    "setup.py",
    "requirements.txt",
    "package.json",
    "package-lock.json",
    "go.mod",
    "go.sum",
)


def normalized_profile(profile: EvaluationProfile) -> dict[str, Any]:
    # Experiment/arm names do not change a command or its resource authority.
    return profile.model_dump(mode="json", exclude={"experiment_id", "arm"})


def command_plan(
    task: Mapping[str, Any],
    profile: EvaluationProfile,
    revision: str | None,
) -> list[dict[str, Any]]:
    from mgar_v3.quality_eval.h12_runner import profile_for

    language = task["language"]
    runtime = (
        ("python3", "--version")
        if language == "python"
        else (("go", "version") if language == "go" else ("node", "--version"))
    )
    commands = [{"role": "runtime", "kind": "runtime", "argv": list(runtime)}]
    if profile.public_probe is not None and language != "python":
        commands.append(
            {"role": "probe_runtime", "kind": "probe_runtime", "argv": ["python3", "--version"]}
        )
    # Prepare exactly the commands exposed by the frozen runtime profile, plus
    # the original declared regression. A replaced environment target must not
    # silently reappear as an additional readiness prerequisite.
    fixed_commands = [profile.public_test_argv]
    if profile.public_build_argv:
        fixed_commands.append(profile.public_build_argv)
    public = h12.public_commands({**task, "environment": {"verify": []}})
    for argv in public:
        isolated = {
            **task,
            "environment": {"verify": []},
            "acceptance": {"regression": [shlex.join(argv)]},
        }
        fixed_commands.append(
            profile_for(
                isolated, "full-public-readiness", "candidate", environment_revision=revision
            ).public_test_argv
        )
    seen: set[tuple[str, ...]] = set()
    for fixed in fixed_commands:
        fixed = public_test_argv(task, fixed)
        if not fixed or fixed in seen:
            continue
        seen.add(fixed)
        build = any(Path(part).name == "tsc" for part in fixed[:2])
        commands.append(
            {
                "role": f"public_{len(seen):02d}",
                "kind": "build" if build else "tests",
                "argv": list(fixed),
            }
        )
    # The independent verifier may bootstrap an interpreter differently from
    # the model's fixed command. Exercise that exact public path too, without
    # loading hidden assets, so public readiness cannot miss its config errors.
    for verifier_argv in public_regression_commands(task, revision):
        fixed = tuple(verifier_argv)
        if fixed in seen:
            continue
        seen.add(fixed)
        build = any(Path(part).name == "tsc" for part in fixed[:2])
        commands.append({
            "role": f"verifier_public_{len(seen):02d}",
            "kind": "build" if build else "tests", "argv": list(fixed),
        })
    if not seen or not any(c["kind"] == "tests" for c in commands):
        raise CatalogError("Complete public readiness needs an original regression test command")
    if profile.public_test_argv not in seen:
        raise CatalogError("Frozen profile test command is absent from complete public preparation")
    if profile.public_probe is not None:
        commands.append(
            {"role": "public_probe", "kind": "probe", "argv": list(profile.public_probe.argv)}
        )
    if len(commands) > profile.resources.max_tool_calls:
        raise CatalogError("Public preparation exceeds the frozen task tool-call bound")
    return commands


def _file_hash(workspace: Path, name: str) -> str:
    path = workspace / name
    if (
        not h12._safe_path(name)
        or not path.is_file()
        or any(
            part.is_symlink()
            for part in (path, *path.parents)
            if part == workspace or part.is_relative_to(workspace)
        )
    ):
        raise CatalogError("Public readiness input is not a safe original regular file")
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _declared_reference_assets(
    workspace: Path,
    task: Mapping[str, Any],
    public_root: Path | None,
) -> dict[str, str]:
    reference = task.get("judge", {}).get("reference_task")
    if not reference or public_root is None:
        return {}
    if task.get("split") != "dev":
        raise CatalogError("Declared public inventory is development-only")
    rows = [
        row
        for row in h12._public_v35_metadata(public_root, "dev")
        if (
            row.get("task_id") == reference
            and row.get("repository") == task["repository"]
            and row.get("base_commit") == task["base_commit"]
        )
    ]
    if len(rows) != 1:
        raise CatalogError("Frozen public reference identity is missing or ambiguous")
    row = rows[0]
    targets, files = row.get("public_targets", ()), row.get("public_files", ())
    if len(targets) != len(files):
        raise CatalogError("Frozen public reference inventory has mismatched entries")
    frozen = h12._public_v35_hashes(public_root, "dev")
    inventory = {}
    for target, source in zip(targets, files, strict=True):
        if (
            not h12._safe_path(target)
            or not h12._safe_path(source)
            or Path(source).parts[:2] != ("dev", "public")
            or Path(target).parts[0] not in {"tests", "test"}
            or not target.endswith(".py")
            or target in inventory
            or not isinstance(frozen.get(source), str)
        ):
            raise CatalogError("Frozen public reference paths or hash are invalid")
        expected = frozen[source]
        if (
            _file_hash(public_root / "experiments/v35-harness", source) != expected
            or _file_hash(workspace, target) != expected
        ):
            raise CatalogError("Declared public test changed from its frozen inventory")
        inventory[target] = expected
    return inventory


def task_binding(
    task: Mapping[str, Any],
    profile: EvaluationProfile,
    workspace: Path,
    image_lock: Path,
    revision: str | None,
    public: FrozenPublicPlan | None,
    *,
    public_root: Path | None = None,
    split: str = "dev",
) -> dict[str, Any]:
    if split not in {"dev", "holdout"} or task.get("split") != split or profile.split != split:
        raise CatalogError("Full public preparation split differs from the explicit controller")
    if public is not None:
        validate_public_original(public, workspace)
    lock_raw = image_lock.read_bytes()
    lock = json.loads(lock_raw)
    tests = _declared_reference_assets(workspace, task, public_root)
    for asset in task.get("public_assets", ()):
        if _file_hash(workspace, asset["target"]) != asset["sha256"]:
            raise CatalogError("Independent public test asset changed")
        tests[asset["target"]] = asset["sha256"]
    for command in h12.public_commands(task):
        for name in command[1:]:
            if (
                h12._safe_path(name)
                and Path(name).suffix == ".py"
                and (workspace / name).is_file()
                and set(Path(name).parts) & {"tests", "test"}
            ):
                tests[name] = _file_hash(workspace, name)
    if public is not None:
        tests.update(
            {
                name: expected
                for name, expected in public.public_basis
                if Path(name).suffix == ".py" and set(Path(name).parts) & {"tests", "test"}
            }
        )
    service = {
        "argv": list(profile.service_argv),
        "mode": profile.service_mode,
        "port": profile.service_port,
        "path": profile.service_path,
        "execution": "AWAITING_IMPLEMENTATION"
        if profile.task_kind == "run_app"
        else "NOT_APPLICABLE",
    }
    if profile.task_kind == "run_app" and not profile.service_argv:
        raise CatalogError("Public application startup plan is unavailable")
    return {
        "task": h12.project_task(task),
        "declared_runtime": task["environment"]["runtime"],
        "profile": normalized_profile(profile),
        "protocol": profile.verification_protocol,
        "image": {
            "path": str(image_lock.resolve()),
            "sha256": hashlib.sha256(lock_raw).hexdigest(),
            "image_id": lock["image_id"],
            "versions": {
                key: lock.get(key) for key in ("python_version", "node_version", "go_version")
            },
        },
        "environment_revision": revision,
        "commands": command_plan(task, profile, revision),
        "original_tree": _tree_hash(_scan(workspace)[0]),
        "dependency_inputs": {
            name: _file_hash(workspace, name)
            for name in _DEPENDENCIES
            if (workspace / name).is_file()
        },
        "public_test_assets": tests,
        "public_plan": public.freeze_metadata() if public else None,
        "service_plan": service,
    }


def _runtime_matches(output: str, binding: Mapping[str, Any], *, probe: bool) -> bool:
    observed = output.strip()
    language = "python" if probe else binding["task"]["language"]
    key = (
        "python_version"
        if language == "python"
        else ("go_version" if language == "go" else "node_version")
    )
    if not observed or observed != binding["image"]["versions"].get(key):
        return False
    if probe:
        return observed.startswith("Python ")
    runtime = binding["declared_runtime"]
    family = (
        observed.startswith("Python " + runtime.removeprefix("python-") + ".")
        if runtime.startswith("python-")
        else observed.startswith("go version go" + runtime.removeprefix("go-") + ".")
        if runtime.startswith("go-")
        else observed.startswith("v" + runtime.removeprefix("node-") + ".")
        if runtime.startswith("node-")
        else False
    )
    explicit = (
        binding["environment_revision"] in {
            "h12-dev-environment-v4", "h12-holdout-infrastructure-v4",
        }
        and binding["task"]["repository"] == "expressjs/express"
        and runtime == "node-20"
        and observed == "v18.10.0"
    )
    return family or explicit


def _task_facts(
    result: Mapping[str, Any],
    evidence: Path,
    *,
    binding: Mapping[str, Any],
    public: FrozenPublicPlan | None,
) -> dict[str, Any]:
    try:
        profile = EvaluationProfile.model_validate(
            {**binding["profile"], "experiment_id": "full-public-readiness", "arm": "candidate"}
        )
        task = TaskBinding.model_validate(result["task_binding"])
        lease = CapabilityLease.model_validate(result["lease"])
        grant = CapabilityGrant.model_validate(result["grant"])
        expected_commands = tuple(sorted({Path(c["argv"][0]).name for c in binding["commands"]}))
        if (
            result["binding_hash"] != canonical_hash(binding)
            or result["original_tree_before"] != binding["original_tree"]
            or result["original_tree_after"] != binding["original_tree"]
            or result["workspace_read_only"] is not True
            or task.task_id != profile.task_id
            or task.task_kind != profile.task_kind
            or task.objective != profile.objective
            or task.execution_epoch != 1
            or task.repository != binding["task"]["repository"].lower()
            or task.base_commit != binding["task"]["base_commit"]
            or lease.lease_hash != lease.computed_hash()
            or lease.mode != "investigate"
            or lease.actor != task.actor
            or lease.repository != task.repository
            or lease.workspace_id != task.workspace_id
            or lease.execution_epoch != 1
            or lease.write_paths
            or lease.network_hosts
            or lease.credentials
            or lease.external_write
            or lease.read_paths != ("",)
            or lease.allowed_commands != expected_commands
            or lease.max_calls != profile.resources.max_tool_calls
            or lease.max_output_bytes != profile.sandbox_log_bytes
            or lease.memory_mb != profile.sandbox_memory_mb
            or lease.nofile_limit != profile.sandbox_nofile
            or lease.offline_network != profile.sandbox_offline_network
            or lease.timeout_seconds != min(600, profile.resources.deadline_seconds)
            or lease.cpus != 1.0
            or lease.pids != 64
            or lease.scratch_mb != profile.sandbox_scratch_mb
            or lease.protected_paths != (".git", ".mgar", ".env")
            or grant.actor != task.actor
            or grant.workspace_id != task.workspace_id
            or grant.repository != task.repository
            or grant.write_paths
            or grant.network
            or grant.read_paths != ("",)
            or not grant.execute
            or grant.allowed_commands != expected_commands
            or grant.max_seconds != lease.timeout_seconds
            or set(result["commands"]) != {c["role"] for c in binding["commands"]}
            or not 0 <= result["elapsed_seconds"] <= profile.resources.deadline_seconds
        ):
            raise CatalogError("Full public readiness task authority or source differs")
        blockers = []
        facts: dict[str, dict[str, Any]] = {}
        operation_ids = []
        durations = []
        public_red = False
        for command in binding["commands"]:
            role, kind = command["role"], command["kind"]
            record = result["commands"][role]
            if set(record) != {
                "receipt_sha256",
                "log_sha256",
                "log_complete",
                "total_bytes",
                "truncated",
                "dropped_bytes",
                "terminal_state",
                "operation_id",
            }:
                raise CatalogError("Unexpected complete-readiness command metadata")
            raw = ExecutionReceipt.model_validate(
                read_evidence_json(
                    evidence / f"{role}-receipt.json",
                    record["receipt_sha256"],
                )
            )
            output = read_evidence(
                evidence / f"{role}.log", record["log_sha256"], limit=profile.sandbox_log_bytes
            )
            provenance = raw.provenance
            if (
                raw.argv != tuple(command["argv"])
                or raw.purpose != "test"
                or raw.cwd != "."
                or raw.operation_id != record["operation_id"]
                or raw.tree_hash != binding["original_tree"]
                or raw.public_probe is not None
                or raw.stdout + raw.stderr != output.decode()
                or raw.log_sha256 != record["log_sha256"]
                or raw.environment_hash != expected_environment_hash(lease)
                or not 0 <= raw.duration_seconds <= profile.resources.deadline_seconds
                or record["log_complete"] is not True
                or record["truncated"]
                or record["dropped_bytes"] != 0
                or record["total_bytes"] != len(output)
                or len(output) >= profile.sandbox_log_bytes
                or record["terminal_state"] not in {"exited", "failed"}
                or raw.status not in {"passed", "failed"}
                or (raw.status == "passed") != (raw.exit_code == 0)
                or provenance is None
                or provenance.operation_id != raw.operation_id
                or provenance.actor != task.actor
                or provenance.workspace_id != task.workspace_id
                or provenance.repository != task.repository
                or provenance.execution_epoch != 1
                or provenance.lease_id != lease.lease_id
                or provenance.lease_hash != lease.lease_hash
                or provenance.grant_hash != canonical_hash(grant.model_dump(mode="json"))
                or provenance.read_paths != ("",)
                or provenance.write_paths
                or provenance.protected_paths != lease.protected_paths
            ):
                raise CatalogError("Complete public readiness command, log or epoch differs")
            operation_ids.append(raw.operation_id)
            durations.append(raw.duration_seconds)
            if kind in {"runtime", "probe_runtime"}:
                passed = raw.status == "passed" and _runtime_matches(
                    raw.stdout,
                    binding,
                    probe=kind == "probe_runtime",
                )
                facts[role] = {"status": "passed" if passed else "unverified"}
            elif kind == "build":
                passed = raw.status == "passed"
                facts[role] = {"status": "passed" if passed else "failed"}
            elif kind == "probe":
                if public is None or result.get("probe_mount") != {
                    "asset_sha256": public.plan.asset_sha256,
                    "container_path": public.plan.container_path,
                    "single_file_read_only": True,
                }:
                    raise CatalogError("Complete readiness public probe mount differs")
                normalized = normalize_probe_receipt(
                    public.plan,
                    raw,
                    task=task,
                    expected_tree=binding["original_tree"],
                    log_complete=True,
                    log_limit_bytes=profile.sandbox_log_bytes,
                )
                passed = normalized.outcome in (
                    {"red"}
                    if profile.task_kind == "bugfix"
                    else {"red", "green"}
                    if profile.task_kind == "feature"
                    else {"green"}
                )
                facts[role] = {
                    "status": "passed" if passed else "unverified",
                    "outcome": normalized.outcome,
                    "reason": normalized.reason_code,
                }
            else:
                language = binding["task"]["language"]
                parser = "python" if language == "python" else "go" if language == "go" else "node"
                status, ran, failed, skipped, detail = parse_tests(
                    cast(Any, parser), raw.stdout, raw.exit_code
                )
                green = raw.status == "passed" and status == "passed" and ran > skipped
                red = (
                    status == "failed"
                    and raw.status == "failed"
                    and raw.exit_code == 1
                    and (
                        declared_assertion_failure(
                            raw.stdout, failed, tuple(binding["public_test_assets"]),
                            language=language,
                            missing_export_scopes=profile.write_paths
                            if profile.task_kind == "feature" and profile.baseline_policy is None
                            else (),
                        )
                    )
                )
                missing_target = (
                    language == "go" and profile.task_kind == "feature"
                    and raw.status == "failed" and raw.exit_code == 1
                    and missing_public_go_target(raw.stdout, tuple(binding["public_test_assets"]))
                )
                if profile.baseline_policy is not None:
                    from mgar_v3.quality_eval.h12_baseline_policy import declared_missing_target

                    missing_target = (
                        language == "go" and raw.status == "failed" and raw.exit_code == 1
                        and declared_missing_target(raw.stdout, profile.baseline_policy)
                    )
                passed = (
                    green or profile.task_kind in {"bugfix", "feature"} and red or missing_target
                )
                public_red = public_red or red
                facts[role] = {
                    "status": status,
                    "tests_run": ran,
                    "tests_failed": failed,
                    "tests_skipped": skipped,
                    "detail": detail,
                    "declared_public_red": red,
                }
                if missing_target:
                    facts[role]["compiler_baseline"] = "FROZEN_PUBLIC_TARGET_NOT_IMPLEMENTED"
                    if profile.baseline_policy is not None:
                        facts[role].update(tests_run=None, tests_failed=None, tests_skipped=None)
            if not passed:
                blockers.append("PUBLIC_COMMAND_NOT_READY:" + role)
        if len(operation_ids) != len(set(operation_ids)):
            raise CatalogError("Public preparation requires distinct command executions")
        if sum(durations) > result["elapsed_seconds"] + 0.001:
            raise CatalogError("Public command times exceed the recorded frozen deadline")
        if profile.task_kind == "bugfix" and public is None and not public_red:
            blockers.append("LEGACY_BUGFIX_REPRODUCTION_REQUIRED")
        return {
            "status": "READY" if not blockers else "NOT_RUN",
            "blockers": blockers,
            "command_facts": facts,
            "tool_calls": len(operation_ids),
            "service_execution": binding["service_plan"]["execution"],
            "quality_status": "NOT_RUN",
        }
    except (DomainError, KeyError, TypeError, ValueError, OSError) as exc:
        raise CatalogError("Complete public readiness evidence is invalid") from exc


def execute_task(
    workspace: Path,
    *,
    binding: Mapping[str, Any],
    public: FrozenPublicPlan | None,
    evidence: Path,
) -> dict[str, Any]:
    if public is not None:
        validate_public_original(public, workspace)
    profile = EvaluationProfile.model_validate(
        {**binding["profile"], "experiment_id": "full-public-readiness", "arm": "candidate"}
    )
    evidence.mkdir(parents=True, exist_ok=False)
    task = TaskBinding(
        task_id=profile.task_id,
        thread_id=uuid4(),
        run_id=uuid4(),
        execution_epoch=1,
        actor="h12_full_readiness",
        workspace_id="full-" + uuid4().hex,
        repository=binding["task"]["repository"],
        base_commit=binding["task"]["base_commit"],
        objective=profile.objective,
        task_kind=profile.task_kind,
        acceptance_revision="full-public-readiness-v1",
        requirements=(VerificationRequirement(requirement_id="original-public"),),
    )
    timeout = min(600, profile.resources.deadline_seconds)
    allowed = tuple(sorted({Path(c["argv"][0]).name for c in binding["commands"]}))
    lease = issue_lease(
        mode="investigate",
        actor=task.actor,
        workspace_id=task.workspace_id,
        repository=task.repository,
        execution_epoch=1,
        allowed_commands=allowed,
        timeout_seconds=timeout,
        max_output_bytes=profile.sandbox_log_bytes,
        memory_mb=profile.sandbox_memory_mb,
        nofile_limit=profile.sandbox_nofile,
        scratch_mb=profile.sandbox_scratch_mb,
        offline_network=profile.sandbox_offline_network,
        max_calls=profile.resources.max_tool_calls,
        ttl_seconds=profile.resources.deadline_seconds + 60,
    )
    grant = CapabilityGrant(
        actor=task.actor,
        workspace_id=task.workspace_id,
        repository=task.repository,
        execute=True,
        allowed_commands=allowed,
        max_seconds=timeout,
    )
    broker = IsolatedExecutionBroker(
        workspace,
        lease,
        lock_path=Path(binding["image"]["path"]),
        public_probe=public.plan if public else None,
        public_probe_root=public.asset_root if public else None,
    )
    if broker.image_id != binding["image"]["image_id"]:
        raise CatalogError("Full public readiness sandbox image changed")
    result: dict[str, Any] = {
        "binding_hash": canonical_hash(binding),
        "original_tree_before": broker.workspace_tree_hash(),
        "workspace_read_only": True,
        "task_binding": task.model_dump(mode="json"),
        "lease": lease.model_dump(mode="json"),
        "grant": grant.model_dump(mode="json"),
        "commands": {},
    }
    if public:
        result["probe_mount"] = {
            "asset_sha256": public.plan.asset_sha256,
            "container_path": public.plan.container_path,
            "single_file_read_only": True,
        }
    write_evidence_json(evidence / "attempt.json", {**result, "status": "RUNNING"})
    session = CommandSession(broker)
    started = time.monotonic()
    deadline = started + profile.resources.deadline_seconds
    try:
        for command in binding["commands"]:
            role = command["role"]
            observed = _observe(
                session,
                grant,
                tuple(command["argv"]),
                role=role,
                deadline=deadline,
                evidence=evidence,
            )
            receipt = read_evidence_json(
                evidence / f"{role}-receipt.json", observed["receipt_sha256"]
            )
            result["commands"][role] = {**observed, "operation_id": receipt["operation_id"]}
    finally:
        session.close()
    result["original_tree_after"] = broker.workspace_tree_hash()
    result["elapsed_seconds"] = time.monotonic() - started
    try:
        result.update(_task_facts(result, evidence, binding=binding, public=public))
    except CatalogError:
        result.update(status="NOT_RUN", blockers=["PUBLIC_EXECUTION_EVIDENCE_INVALID"])
    write_evidence_json(evidence / "result.json", result)
    return result


def validate_full_report(
    report: Mapping[str, Any],
    evidence: Path,
    *,
    bindings: Mapping[str, Any],
    candidate_source: str,
    public_plans: Mapping[str, FrozenPublicPlan] | None = None,
    split: str = "dev",
) -> None:
    public_plans = public_plans or {}
    expected_ids = sorted(bindings)
    if split not in {"dev", "holdout"} or len(expected_ids) != 12 or any(
        b["task"]["split"] != split or b["profile"]["split"] != split for b in bindings.values()
    ):
        raise CatalogError("Full readiness requires exactly twelve tasks in the selected split")
    if (
        report.get("schema") != (SCHEMA if split == "dev" else HOLDOUT_SCHEMA)
        or report.get("status") != f"FULL_{split.upper()}_READY"
        or report.get("zero_model") is not True
        or report.get("readiness_only") is not True
        or report.get("quality_status") != "NOT_RUN"
        or report.get("paid_model_calls") != 0
        or report.get("cost_micros") != 0
        or report.get("ready_count") != 12
        or report.get("task_ids") != expected_ids
        or report.get("bindings") != dict(bindings)
        or report.get("candidate_source_sha256_before") != candidate_source
        or report.get("candidate_source_sha256_after") != candidate_source
        or not isinstance(report.get("tasks"), dict)
        or set(report["tasks"]) != set(expected_ids)
    ):
        raise CatalogError(
            "Full readiness source, profile, protocol or twelve-task coverage differs"
        )
    operations: list[str] = []
    for task_id in expected_ids:
        entry = report["tasks"][task_id]
        directory = hashlib.sha256(task_id.encode()).hexdigest()
        if entry.get("directory") != directory:
            raise CatalogError("Full readiness evidence directory differs")
        result = entry.get("result", {})
        facts = _task_facts(
            result,
            evidence / directory,
            binding=bindings[task_id],
            public=public_plans.get(task_id),
        )
        if facts["status"] != "READY" or any(result.get(k) != v for k, v in facts.items()):
            raise CatalogError("A required development public prerequisite is not ready")
        operations.extend(c["operation_id"] for c in result["commands"].values())
    if len(operations) != len(set(operations)):
        raise CatalogError("Different tasks cannot reuse the same physical command receipt")
