"""Developer-only, zero-model execution readiness for the public H12 dev split.

Every process uses the same Broker and CommandSession as evaluation.  A built
image or an available source object alone never establishes execution readiness.
The exposed historical holdout is deliberately outside this repair workflow.
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
from collections.abc import Mapping
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

from mgar_v3.domain import DomainError
from mgar_v3.harness.broker import IsolatedExecutionBroker, issue_lease
from mgar_v3.harness.commands import CommandSession
from mgar_v3.harness.contracts import CapabilityGrant
from mgar_v3.harness.project_commands import ProjectKind, parse_tests
from mgar_v3.quality_eval import h12, h12_judge
from mgar_v3.quality_eval.h12_runner import (
    DEV_ENVIRONMENT_V3,
    DEV_ENVIRONMENT_V4,
    DEV_REVISIONS,
    HOLDOUT_INFRA_V4,
    EvaluationRunner,
    private_json,
    profile_for,
)
from mgar_v3.quality_eval.harness_v4 import CatalogError, repo_root


def command_counts(result: Mapping[str, Any], language: str) -> list[dict[str, Any]]:
    """Count the actual complete public output; never infer counts from exit 0."""
    kind: ProjectKind = "python" if language == "python" else "go" if language == "go" else "node"
    rows = []
    for receipt in result.get("receipts", ()):
        path = Path(receipt["private_log"])
        raw = path.read_bytes()
        if hashlib.sha256(raw).hexdigest() != receipt["log_sha256"]:
            raise CatalogError("Readiness log identity changed")
        status, ran, failed, skipped, detail = parse_tests(
            kind, raw.decode("utf-8"), receipt.get("exit_code")
        )
        rows.append(
            {
                "command_id": receipt.get("handle_id"),
                "status": status,
                "executed": ran,
                "failed": failed,
                "skipped": skipped,
                "detail": detail,
                "complete": receipt["complete"],
                "log_sha256": receipt["log_sha256"],
                "private_log": str(path),
            }
        )
    return rows


def declared_reference_assets(
    workspace: Path, task: Mapping[str, Any], source: Mapping[str, Any]
) -> dict[str, str]:
    """Bind expected red assertions to frozen public assets and declarations."""
    baseline = task.get("acceptance", {}).get("baseline", ())
    if (
        task.get("split") != "dev"
        or task.get("language") != "python"
        or task.get("judge", {}).get("kind") != "v35_reference"
        or not task.get("judge", {}).get("reference_task")
        or not baseline
        or not all(isinstance(line, str) for line in baseline)
        or not any(
            re.search(
                r"\bfails?\b|\bfailures?\b|\babsent\b|\bmissing\b|\bdoes not exist\b",
                line,
                re.IGNORECASE,
            )
            for line in baseline
        )
    ):
        return {}
    assets = {}
    for item in source.get("public_files", ()):
        target, expected = item.get("target"), item.get("sha256")
        if (
            not h12._safe_path(target)
            or Path(target).parts[0] != "tests"
            or not target.endswith(".py")
            or not isinstance(expected, str)
            or not re.fullmatch(r"[a-f0-9]{64}", expected)
            or target in assets
        ):
            raise CatalogError("Frozen public reference inventory is invalid")
        path = workspace / target
        if (
            path.is_symlink()
            or not path.is_file()
            or not path.resolve().is_relative_to(workspace.resolve())
            or hashlib.sha256(path.read_bytes()).hexdigest() != expected
        ):
            raise CatalogError("Frozen public reference asset changed")
        assets[target] = expected
    return assets


def reference_failures_reproduced(result: Mapping[str, Any], assets: Mapping[str, str]) -> bool:
    """Only executed failures from declared reference files establish the red baseline."""
    if (
        not assets
        or result.get("status") != "PASS"
        or result.get("baseline_test_status") != "FAIL"
        or not result.get("receipts")
    ):
        return False
    failed_paths: set[str] = set()
    for receipt in result["receipts"]:
        raw = Path(receipt["private_log"]).read_bytes()
        if hashlib.sha256(raw).hexdigest() != receipt["log_sha256"]:
            return False
        output = raw.decode("utf-8")
        parsed, ran, failed, _, _ = parse_tests("python", output, receipt.get("exit_code"))
        if (
            not receipt.get("complete")
            or receipt.get("state") not in {"exited", "failed"}
            or receipt.get("exit_code") not in {0, 1}
            or parsed not in {"passed", "failed"}
            or ran <= 0
            or h12_judge._INFRA.search(output)
            or h12_judge._RESOURCE.search(output)
            or re.search(r"^ERROR\s", output, re.MULTILINE)
        ):
            return False
        paths = set(re.findall(r"^FAILED\s+([^\s:]+)(?:::|\s)", output, re.MULTILINE))
        if failed and (not paths or receipt["state"] != "failed" or receipt["exit_code"] != 1):
            return False
        if not failed and (receipt["state"] != "exited" or receipt["exit_code"] != 0):
            return False
        failed_paths.update(paths)
    return bool(failed_paths) and failed_paths <= set(assets)


def reproduce_public_reference_baseline(
    workspace: Path,
    task: Mapping[str, Any],
    source: Mapping[str, Any],
    image_lock: Path,
    evidence: Path,
    *,
    memory_mb: int,
    observed: Mapping[str, Any],
    environment_revision: str = DEV_ENVIRONMENT_V3,
) -> dict[str, Any]:
    """Replay the original public judge commands without changing task type or tests."""
    assets = declared_reference_assets(workspace, task, source)
    if not assets:
        return {"status": "NOT_RUN", "reason": "DECLARED_PUBLIC_REFERENCE_REQUIRED"}
    if not reference_failures_reproduced(observed, assets):
        return {"status": "NOT_RUN", "reason": "UNRELATED_PUBLIC_REGRESSION_FAILURE"}
    captured = h12_judge.tree_hash(workspace)
    result = h12_judge.verify(
        workspace,
        task,
        image_lock,
        evidence,
        phase="environment",
        expected_tree=captured,
        nofile_limit=1024,
        log_bytes=1048576,
        memory_mb=memory_mb,
        environment_revision=environment_revision,
    )
    matched = (
        result.get("tree_hash") == captured
        and result.get("judge_sha256") == task["judge_sha256"]
        and h12_judge.tree_hash(workspace) == captured
        and declared_reference_assets(workspace, task, source) == assets
        and reference_failures_reproduced(result, assets)
    )
    proof = {
        "status": "PASS" if matched else "NOT_RUN",
        "reason": "DECLARED_PUBLIC_REFERENCE_BASELINE_REPRODUCED"
        if matched
        else "PUBLIC_REFERENCE_BASELINE_NOT_REPRODUCED",
        "phase": "declared_public_reference_baseline",
        "task_id": task["task_id"],
        "judge_sha256": task["judge_sha256"],
        "tree_hash": captured,
        "declared_baseline": task["acceptance"]["baseline"],
        "public_assets": assets,
        "judge_replay": result,
        "paid_model_calls": 0,
        "cost_micros": 0,
    }
    private_json(evidence / "reference-baseline-proof.json", proof)
    return proof


def runtime_probe(
    workspace: Path,
    task: Mapping[str, Any],
    image_lock: Path,
    evidence: Path,
    *,
    environment_revision: str | None = None,
) -> dict[str, Any]:
    """Observe exact toolchain versions inside the actual offline sandbox."""
    language = task["language"]
    argv = (
        ["python3", "--version"]
        if language == "python"
        else (["go", "version"] if language == "go" else ["node", "--version"])
    )
    lock = json.loads(image_lock.read_text())
    key = (
        "python_version"
        if language == "python"
        else "go_version"
        if language == "go"
        else "node_version"
    )
    expected = lock.get(key)
    evidence.mkdir(parents=True, exist_ok=False, mode=0o700)
    lease = issue_lease(
        mode="investigate",
        actor="h12-readiness",
        workspace_id="readiness-" + uuid4().hex,
        repository=task["repository"],
        execution_epoch=1,
        read_paths=("",),
        write_paths=(),
        allowed_commands=(argv[0],),
        timeout_seconds=60,
        nofile_limit=1024,
    )
    grant = CapabilityGrant(
        actor=lease.actor,
        workspace_id=lease.workspace_id,
        repository=lease.repository,
        read_paths=("",),
        write_paths=(),
        execute=True,
        allowed_commands=(argv[0],),
        max_seconds=60,
    )
    session = CommandSession(IsolatedExecutionBroker(workspace, lease, lock_path=image_lock))
    try:
        receipt, output = h12_judge._run(session, grant, argv, 60, evidence, 0)
    finally:
        session.close()
    observed = output.strip()
    runtime = str(task["environment"]["runtime"])
    family_matches = (
        observed.startswith("Python " + runtime.removeprefix("python-") + ".")
        if runtime.startswith("python-")
        else observed.startswith("v" + runtime.removeprefix("node-") + ".")
        if runtime.startswith("node-")
        else observed.startswith("go version go" + runtime.removeprefix("go-") + ".")
        if runtime.startswith("go-")
        else False
    )
    explicit_toolchain = bool(
        environment_revision in {DEV_ENVIRONMENT_V4, HOLDOUT_INFRA_V4}
        and task.get("repository") == "expressjs/express"
        and language == "javascript"
        and runtime == "node-20"
        and observed == "v18.10.0"
    )
    if environment_revision == HOLDOUT_INFRA_V4 and language == "go" and runtime == "go-1.22":
        module = workspace / "go.mod"
        explicit_toolchain = bool(
            observed.startswith("go version go1.23.12 ")
            and module.is_file()
            and not module.is_symlink()
            and re.search(r"(?m)^go\s+1\.23\.0\s*$", module.read_text())
        )
    matched = bool(expected) and observed == expected and (family_matches or explicit_toolchain)
    return {
        "status": "PASS"
        if receipt["exit_code"] == 0 and receipt["complete"] and matched
        else "NOT_RUN",
        "declared_runtime": runtime,
        "declared_runtime_matches": family_matches,
        "explicit_toolchain_revision": environment_revision if explicit_toolchain else None,
        "locked_version": expected,
        "observed_version": observed,
        "argv": argv,
        "receipt": receipt,
        "image_id": lock.get("image_id"),
        "image_lock_sha256": hashlib.sha256(image_lock.read_bytes()).hexdigest(),
    }


def execution_readiness(
    image_locks: Mapping[str, str],
    evidence: Path,
    *,
    root: Path | None = None,
    environment_revision: str = DEV_ENVIRONMENT_V3,
    draft_proposal: Path | None = None,
) -> dict[str, Any]:
    """Run all twelve dev prerequisites before any new candidate model call.

    Each task runs its original public regression in a separate immutable copy.
    Bugfix/refactor prerequisites are also executed, retaining red original
    assertions and baseline-oracle receipts separately from green regressions.
    This entry point neither instantiates a provider nor initializes an Engine.
    """
    if environment_revision not in DEV_REVISIONS:
        raise CatalogError("Only trusted dev environment revisions can run this gate")
    base = root or repo_root()
    evidence.mkdir(parents=True, exist_ok=False, mode=0o700)
    metadata = h12.preflight("dev", base)
    tasks = h12.load_frozen_tasks("dev", base)
    draft = None
    draft_hash = None
    draft_registry: dict[str, Any] = {}
    draft_inputs: dict[Path, str] = {}
    if draft_proposal is not None:
        from mgar_v3.quality_eval.harness_v4 import load_jsonl, validate_task

        draft_hash = hashlib.sha256(draft_proposal.read_bytes()).hexdigest()
        draft = json.loads(draft_proposal.read_text())
        catalog = draft_proposal.parent / "dev.jsonl"
        if (draft.get("revision") != "h12-benchmark-v2"
                or draft.get("status") != "DRAFT_NOT_FROZEN"
                or hashlib.sha256(catalog.read_bytes()).hexdigest()
                != draft.get("dev_catalog_sha256")
                or environment_revision != DEV_ENVIRONMENT_V4):
            raise CatalogError("Draft benchmark identity differs")
        revised = load_jsonl(catalog)
        old_by_id = {task["task_id"]: task for task in tasks}
        if len(revised) != 12 or len({task["task_id"] for task in revised}) != 12:
            raise CatalogError("Draft requires twelve distinct dev tasks")
        replacement = next((t for t in revised if t["task_id"] == draft["dev_replacement"]), None)
        if replacement is None or replacement.get("supersedes_invalid_task") not in old_by_id:
            raise CatalogError("Draft replacement provenance missing")
        for task in revised:
            if validate_task(task, "dev"):
                raise CatalogError("Invalid draft task")
            if task is not replacement and task != old_by_id.get(task["task_id"]):
                raise CatalogError("Unreviewed change to another dev task")
        previous = old_by_id[replacement["supersedes_invalid_task"]]
        if {t["task_id"] for t in revised} != (set(old_by_id) - {previous["task_id"]}) | {
            replacement["task_id"]
        }:
            raise CatalogError("Draft replacement must preserve the other eleven tasks")
        for key in ("base_commit", "repository", "type", "composition", "resources", "scope"):
            if replacement[key] != previous[key]:
                raise CatalogError("Draft replacement changes its source or task limits")
        for metadata_row in metadata["tasks"]:
            if metadata_row["task_id"] == previous["task_id"]:
                metadata_row["task_id"] = replacement["task_id"]
                metadata_row["judge_sha256"] = replacement["judge_sha256"]
        registry = draft_proposal.parent / "judges/dev/manifest.json"
        if hashlib.sha256(registry.read_bytes()).hexdigest() != draft["dev_registry_sha256"]:
            raise CatalogError("Draft registry differs from proposal")
        draft_registry = {"registry_path": registry,
                          "registry_sha256": draft["dev_registry_sha256"]}
        draft_inputs = {
            catalog: draft["dev_catalog_sha256"], registry: draft["dev_registry_sha256"],
            draft_proposal.parent / "got-public-amendments.json": draft["public_amendments_sha256"],
        }
        tasks = revised
    runner = EvaluationRunner(
        SimpleNamespace(harness=None),
        experiment_id="zero-model-readiness-v3",
        evidence=evidence / "materialization",
        image_locks=image_locks,
        api_key=None,
        root=base,
        environment_revision=environment_revision,
    )
    source_before = runner._candidate_fingerprint()
    metadata_by_id = {row["task_id"]: row for row in metadata["tasks"]}
    rows = []
    for task in tasks:
        registry_options = draft_registry if draft and task["task_id"] == draft[
            "dev_replacement"] else {}
        row: dict[str, Any] = {
            "task_id": task["task_id"],
            "type": task["type"],
            "status": "NOT_RUN",
            "blockers": [],
            "paid_model_calls": 0,
            "cost_micros": 0,
        }
        case = evidence / task["task_id"]
        case.mkdir(mode=0o700)
        original = None
        try:
            source_row = metadata_by_id[task["task_id"]]
            row["source"] = source_row["source"]
            row["blockers"].extend(source_row["blockers"])
            lock_name = image_locks.get(task["repository"] + "@" + task["base_commit"])
            if lock_name is None or not Path(lock_name).is_file():
                row["blockers"].append("FROZEN_PROJECT_ENVIRONMENT_UNAVAILABLE")
            profile = profile_for(
                task,
                "zero-model-readiness-v3",
                "candidate",
                environment_revision=environment_revision,
            )
            row["execution_plan"] = {
                "original_public_commands": h12.public_commands(task),
                "public_test_argv": list(profile.public_test_argv),
                "service_argv": list(profile.service_argv),
                "service_mode": profile.service_mode,
                "service_port": profile.service_port,
                "service_path": profile.service_path,
                "service_execution": "AWAITING_IMPLEMENTATION"
                if task["type"] == "run_app"
                else "NOT_APPLICABLE",
                "sandbox_nofile": profile.sandbox_nofile,
                "sandbox_log_bytes": profile.sandbox_log_bytes,
                "sandbox_memory_mb": profile.sandbox_memory_mb,
            }
            if not profile.public_test_argv:
                row["blockers"].append("PUBLIC_REGRESSION_PLAN_UNAVAILABLE")
            if task["type"] == "run_app" and not profile.service_argv:
                row["blockers"].append("PUBLIC_APPLICATION_PLAN_UNAVAILABLE")
            if not row["blockers"]:
                assert lock_name is not None
                image_lock = Path(lock_name)
                original = runner._materialize(task, Path(source_row["source"]["path"]))
                if draft is not None and draft_proposal is not None and task[
                    "repository"] == "sindresorhus/got":
                    from mgar_v3.quality_eval.h12_benchmark import apply_public_amendments

                    amendment = draft_proposal.parent / "got-public-amendments.json"
                    row["benchmark_amendment"] = apply_public_amendments(
                        original, amendment, draft["public_amendments_sha256"],
                        repository=task["repository"], commit=task["base_commit"],
                    )
                row["tree_hash"] = h12_judge.tree_hash(original)
                row["public_assets"] = h12_judge.test_hashes(original)
                row["dependency_inputs"] = {
                    name: hashlib.sha256((original / name).read_bytes()).hexdigest()
                    for name in (
                        "pyproject.toml",
                        "setup.cfg",
                        "setup.py",
                        "requirements.txt",
                        "package.json",
                        "package-lock.json",
                        "go.mod",
                        "go.sum",
                    )
                    if (original / name).is_file() and not (original / name).is_symlink()
                }
                runtime = runtime_probe(
                    original,
                    task,
                    image_lock,
                    case / "runtime",
                    environment_revision=environment_revision,
                )
                row["runtime"] = runtime
                if runtime["status"] != "PASS":
                    row["blockers"].append("FIXED_TOOLCHAIN_UNAVAILABLE")
                environment = h12_judge.verify(
                    original,
                    task,
                    image_lock,
                    case / "public-environment",
                    phase="environment",
                    nofile_limit=1024,
                    log_bytes=1048576,
                    memory_mb=profile.sandbox_memory_mb,
                    offline_network=profile.sandbox_offline_network,
                    environment_revision=environment_revision,
                    **registry_options,
                )
                row["public_environment"] = environment
                row["public_counts"] = command_counts(environment, task["language"])
                if environment["status"] != "PASS":
                    row["blockers"].append(
                        environment.get("reason", "PUBLIC_ENVIRONMENT_UNAVAILABLE")
                    )
                elif environment.get("baseline_test_status") == "FAIL":
                    reference = reproduce_public_reference_baseline(
                        original,
                        task,
                        source_row["source"],
                        image_lock,
                        case / "public-reference-baseline",
                        memory_mb=profile.sandbox_memory_mb,
                        observed=environment,
                        environment_revision=environment_revision,
                    )
                    row["public_reference_baseline"] = reference
                    if reference["status"] != "PASS":
                        row["blockers"].append("PUBLIC_REGRESSION_BASELINE_RED")
                if task["type"] in {"bugfix", "refactor"} and not row["blockers"]:
                    baseline = h12_judge.verify(
                        original,
                        task,
                        image_lock,
                        case / "baseline",
                        phase="baseline",
                        nofile_limit=1024,
                        log_bytes=1048576,
                        memory_mb=profile.sandbox_memory_mb,
                        offline_network=profile.sandbox_offline_network,
                        environment_revision=environment_revision,
                        **registry_options,
                    )
                    row["baseline"] = baseline
                    if baseline["status"] != "PASS":
                        row["blockers"].append(baseline.get("reason", "BASELINE_UNAVAILABLE"))
                if h12_judge.tree_hash(original) != row["tree_hash"]:
                    row["blockers"].append("TREE_CHANGED_DURING_READINESS")
            row["status"] = "READY" if not row["blockers"] else "NOT_RUN"
        except (CatalogError, DomainError, OSError, ValueError, KeyError) as error:
            row["blockers"].append(getattr(error, "code", type(error).__name__))
        finally:
            if original is not None:
                shutil.rmtree(original.parent)
        private_json(case / "readiness.json", row)
        rows.append(row)
    ready = sum(row["status"] == "READY" for row in rows)
    source_after = runner._candidate_fingerprint()
    report = {
        "schema": "harness-h12-dev-execution-readiness-v3",
        "split": "dev",
        "status": "READY" if ready == 12 and source_before == source_after else "NOT_RUN",
        "denominator": 12,
        "ready_count": ready,
        "not_run_count": 12 - ready,
        "tasks": rows,
        "frozen": metadata["frozen"],
        "paid_model_calls": 0,
        "cost_micros": 0,
        "candidate_source_sha256_before": source_before,
        "candidate_source_sha256_after": source_after,
        "source_stable": source_before == source_after,
        "blockers": []
        if source_before == source_after
        else ["HARNESS_SOURCE_CHANGED_DURING_READINESS"],
        "environment_manifest_sha256": hashlib.sha256(
            (base / f"experiments/harness-v4/{environment_revision}.json").read_bytes()
        ).hexdigest(),
        "image_map_sha256": hashlib.sha256(
            json.dumps(dict(image_locks), sort_keys=True).encode()
        ).hexdigest(),
        "scope": "original public dev prerequisites and actual offline Broker commands",
        "holdout": "NOT_ACCESSED_EXPOSED_HISTORICAL_SPLIT_QUARANTINED",
        "environment_revision": environment_revision,
        "environment_amendments": [
            {
                "field": "sandbox_nofile",
                "original": 128,
                "candidate": 1024,
                "reason": "Original offline Node ESM dependency graph exceeds 128 descriptors; "
                "no network/UID/CPU/PID/write-scope amendment",
            },
            {
                "field": "sandbox_log_bytes",
                "original": 65536,
                "candidate": 1048576,
                "reason": "Keep complete public logs; overflow remains explicit and cannot pass",
            },
            {
                "field": "sandbox_memory_mb",
                "scope": "sindresorhus/got",
                "original": 256,
                "candidate": 1024,
                "reason": "768MB XO heap exhausted; 1024MB executes XO/tsc",
            },
            {
                "field": "Got public command",
                "original": ["npm", "test"],
                "candidate": ["npm", "test", "--", "--no-cache"],
                "reason": "Official AVA cache uses TMPDIR=/scratch; "
                "original test bytes/selection retained; CI selects original 200ms "
                "timing margin branch instead of 100ms",
            },
        ],
    }
    if environment_revision == DEV_ENVIRONMENT_V4:
        report["schema"] = "harness-h12-dev-execution-readiness-v4"
        report["environment_amendments"] = json.loads(
            (base / f"experiments/harness-v4/{environment_revision}.json").read_text()
        )["changes"]
    if draft_proposal is not None:
        stable = (hashlib.sha256(draft_proposal.read_bytes()).hexdigest() == draft_hash
                  and all(hashlib.sha256(path.read_bytes()).hexdigest() == expected
                          for path, expected in draft_inputs.items()))
        report["schema"] = "harness-h12-draft-benchmark-readiness-v2"
        report["draft_proposal_sha256"] = draft_hash
        report["draft_inputs_stable"] = stable
        report["paid_dispatch_allowed"] = False
        report["scope"] = "new draft dev benchmark prerequisites; not frozen or a quality score"
        report["status"] = "DRAFT_READY" if stable and report["status"] == "READY" else "NOT_RUN"
    private_json(evidence / f"readiness-{environment_revision.rsplit('-', 1)[1]}.json", report)
    return report
