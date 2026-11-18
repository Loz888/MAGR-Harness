"""Private H12 independent judges, executed only through the existing Broker.

The final verifier requires a sealed model session, uses a separate read-only
copy, and never returns hidden output to a model. H00 judge objects stay frozen;
the additional manifest binds their hashes to new implementation file hashes.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
import shutil
import tempfile
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Literal
from uuid import uuid4

from mgar_v3.domain import DomainError, digest
from mgar_v3.evidence.artifacts import ArtifactStore
from mgar_v3.harness.broker import IsolatedExecutionBroker, issue_lease
from mgar_v3.harness.commands import CommandSession
from mgar_v3.harness.contracts import (
    CancellationToken,
    CapabilityGrant,
    ExecutionRequest,
    is_workspace_artifact,
)
from mgar_v3.harness.diagnostics import failure_diagnosis
from mgar_v3.harness.project_commands import ProjectKind, parse_tests
from mgar_v3.harness.semantic_types import TaskBaselinePolicy
from mgar_v3.harness.workspace import _copy_tree_nofollow, _scan, _tree_hash
from mgar_v3.quality_eval.h12_public_regression import declared_assertion_failure, public_test_argv
from mgar_v3.quality_eval.harness_v4 import CatalogError, judge_sha256, repo_root

_MANIFEST_SHA256 = "66c0f0b556d399466ab2a342510244e8e34d617c9cc8bae478abdf1115764930"
_INFRA = re.compile(
    r"No module named|ModuleNotFoundError|Cannot find (?:module|package)|command not found|"
    r"executable file not found|could not determine executable|EACCES|EROFS|"
    r"requires? (?:a different|Python)|No matching distribution|network is unreachable|"
    r"cannot find package|go: downloading|not found in PATH|fixture ['\"][^'\"]+['\"] not found|"
    r"ImportError:|ERROR collecting|(?:collection|collecting).*error",
    re.IGNORECASE,
)
_RESOURCE = re.compile(
    r"\bEMFILE\b|too many open files|resource temporarily unavailable|"
    r"cannot allocate memory|out of memory",
    re.IGNORECASE,
)


def _private_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2)
        stream.write("\n")


def tree_hash(workspace: Path) -> str:
    """Bind regular contents and exact internal link targets like TaskWorkspace."""
    return _tree_hash(_scan(Path(workspace))[0])


def test_hashes(workspace: Path) -> dict[str, str]:
    """Freeze existing tests/configuration before the model can edit anything."""
    hashes = _scan(Path(workspace))[0]
    result = {}
    for path, value in hashes.items():
        name = Path(path).name
        if (
            set(Path(path).parts) & {"tests", "test", "verification"}
            or name.startswith("test_")
            or name.endswith("_test.go")
            or name
            in {
                "conftest.py",
                "pytest.ini",
                ".coveragerc",
                "pyproject.toml",
                "setup.cfg",
                "tox.ini",
                ".mocharc.json",
                ".mocharc.js",
                "ava.config.js",
                "ava.config.mjs",
                "tsconfig.json",
            }
        ):
            result[path] = value
    package = Path(workspace) / "package.json"
    if package.is_file():
        parsed = json.loads(package.read_text())
        scripts = parsed.get("scripts", {})
        result["package.json::scripts.test"] = digest(scripts.get("test"))
        result["package.json::test-configuration"] = digest(
            {
                "pretest": scripts.get("pretest"),
                "posttest": scripts.get("posttest"),
                **{name: parsed.get(name) for name in ("ava", "mocha", "jest")},
            }
        )
    # Additions of configuration/conftest files can disable collection just as
    # edits can. Model-added ordinary tests remain permitted.
    result["::test-configuration-paths"] = digest(
        sorted(
            path
            for path in hashes
            if Path(path).name
            in {
                "conftest.py",
                "pytest.ini",
                ".coveragerc",
                "pyproject.toml",
                "setup.cfg",
                "tox.ini",
                ".mocharc.json",
                ".mocharc.js",
                "ava.config.js",
                "ava.config.mjs",
                "tsconfig.json",
                "package.json",
            }
        )
    )
    return result


test_hashes.__test__ = False  # type: ignore[attr-defined]


def _implementation(
    task: Mapping[str, Any],
    root: Path,
    *,
    registry_path: Path | None = None,
    registry_sha256: str | None = None,
) -> dict[str, Any] | None:
    if (registry_path is None) != (registry_sha256 is None):
        raise CatalogError("Private registry path and expected hash are both required")
    base = registry_path.parent if registry_path else root / "experiments/harness-v4/judges/dev"
    manifest = registry_path or base / "manifest.json"
    expected_manifest = registry_sha256 or _MANIFEST_SHA256
    if not re.fullmatch(r"[a-f0-9]{64}", expected_manifest):
        raise CatalogError("Judge registry hash is not a SHA256")
    if not manifest.is_file():
        if registry_path is not None:
            raise CatalogError("Private judge registry is unavailable")
        return None
    data = manifest.read_bytes()
    if hashlib.sha256(data).hexdigest() != expected_manifest:
        raise CatalogError("Judge implementation manifest changed")
    document = json.loads(data)
    if document.get("schema") != "mgar-h12-judge-implementation-v1":
        raise CatalogError("Judge registry schema is invalid")
    rows = document["tasks"]
    if len({r["task_id"] for r in rows}) != len(rows):
        raise CatalogError("Judge registry task IDs must be unique")
    row = next((r for r in rows if r["task_id"] == task.get("task_id")), None)
    if row is None:
        if registry_path is not None:
            raise CatalogError("Private judge registry has no matching task")
        return None
    if row["original_judge_sha256"] != task.get("judge_sha256"):
        raise CatalogError("Judge implementation belongs to another frozen specification")
    if not row["files"]:
        raise CatalogError("Judge registry needs at least one hash-bound implementation")
    for item in row["files"]:
        path = (base / _relative(item["source"])).resolve()
        _relative(item["target"])
        if not path.is_relative_to(base.resolve()):
            raise CatalogError("Judge registry source escapes its private directory")
        if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != item["sha256"]:
            raise CatalogError("Judge implementation source changed")
    return {**row, "_source_root": str(base.resolve()), "_registry_sha256": expected_manifest}


def implementation_status(task: Mapping[str, Any], root: Path | None = None) -> dict[str, Any]:
    """Opaque preflight metadata; do not read old hidden reference sources."""
    if task.get("judge_status") == "implemented":
        return {"available": True, "kind": "h00_reference", "declared_status": "implemented"}
    row = _implementation(task, root or repo_root())
    return {
        "available": row is not None,
        "kind": "h12_spec_implementation" if row else "pending",
        "manifest_sha256": _MANIFEST_SHA256 if row else None,
        "validated_on_pinned_source": False,
    }


def _relative(value: str) -> str:
    path = Path(value)
    if (
        not value
        or path == Path(".")
        or path.is_absolute()
        or ".." in path.parts
        or "\\" in value
        or is_workspace_artifact(value)
    ):
        raise CatalogError("Judge destination must be a relative path")
    return path.as_posix()


def _normalize(argv: list[str]) -> list[str]:
    if not argv:
        raise CatalogError("Empty judge command")
    if argv[:3] in (["python", "-m", "pytest"], ["python3", "-m", "pytest"]):
        bootstrap = (
            "import sys,runpy;sys.path[:0]=['/workspace/src','/workspace'];"
            "runpy.run_module('pytest',run_name='__main__')"
        )
        # The Broker keeps pytest's cache on /scratch while the source mount
        # stays read-only. Disabling the plugin would invalidate cache_dir and
        # cache fixtures in projects that treat configuration warnings as errors.
        return ["python3", "-B", "-c", bootstrap, *argv[3:]]
    if argv[0] == "npx":
        entrypoints = {
            "mocha": "/node_modules/mocha/bin/mocha.js",
            "tsc": "/node_modules/typescript/bin/tsc",
        }
        if len(argv) < 2 or argv[1] not in entrypoints:
            raise CatalogError("Judge tool has no offline local entry point")
        return ["node", entrypoints[argv[1]], *argv[2:]]
    if argv[:2] == ["go", "test"]:
        if "-v" not in argv:
            argv.insert(2, "-v")
        if not any(value == "-p" or value.startswith("-p=") for value in argv[2:]):
            argv[2:2] = ["-p", "1"]
        return argv
    if argv[0] not in {"python3", "node", "npm", "go"}:
        raise CatalogError("Judge executable is not an allowed verifier tool")
    return ["/" + value if value.startswith("node_modules/") else value for value in argv]


def _environment_commands(
    commands: list[list[str]], repository: str, revision: str | None,
) -> list[list[str]]:
    """Apply the declared AVA sandbox settings to both script and direct entrypoints."""
    if repository != "sindresorhus/got" or revision not in {
        "h12-dev-environment-v3", "h12-dev-environment-v4", "h12-holdout-infrastructure-v4",
    }:
        return commands
    option = ("--no-cache" if revision == "h12-dev-environment-v3"
              else "--config=/opt/h12/ava-config.mjs")
    prefixes = (["node", "/node_modules/ava/entrypoints/cli.mjs"],
                ["node", "--import", "tsx/esm", "/node_modules/ava/entrypoints/cli.mjs"])
    result = []
    for command in commands:
        if command == ["npm", "test"]:
            result.append([*command, "--", option])
            continue
        prefix = next((value for value in prefixes if command[:len(value)] == value), None)
        if prefix is None:
            result.append(command)
            continue
        arguments = command[len(prefix):]
        if option in arguments:
            result.append(command)
            continue
        if any(value in {"--cache", "--config"} or value.startswith("--config=")
               for value in arguments):
            raise CatalogError("Judge AVA configuration conflicts with frozen sandbox settings")
        # Keep every original selector and assertion. The trusted configuration
        # preserves package AVA settings, changing only cache and concurrency.
        result.append([*prefix, option, *arguments])
    return result


def public_regression_commands(
    task: Mapping[str, Any], revision: str | None,
) -> list[list[str]]:
    """The public verifier command path, shared with zero-model preparation.

    This reads only declared public regression arguments, never a Judge bundle.
    """
    from mgar_v3.quality_eval.h12 import public_commands

    commands = [
        list(public_test_argv(task, _normalize(argv))) for argv in public_commands({
            "environment": {"verify": []}, "acceptance": task["acceptance"],
        })
    ]
    return _environment_commands(commands, task["repository"], revision)


def prepare_judge(
    task: Mapping[str, Any],
    destination: Path,
    *,
    root: Path | None = None,
    registry_path: Path | None = None,
    registry_sha256: str | None = None,
) -> dict[str, Any]:
    """Materialize private judges after sealing; never read a gold/reference fix."""
    base = root or repo_root()
    if task.get("judge_sha256") != judge_sha256(task):
        raise CatalogError("Original frozen judge hash changed")
    destination.mkdir(parents=True, exist_ok=True, mode=0o700)
    row = _implementation(task, base, registry_path=registry_path, registry_sha256=registry_sha256)
    selected: list[tuple[Path, str, str]] = []
    commands = []
    oracle = False
    if row is not None:
        for item in row["files"]:
            selected.append(
                (
                    Path(row["_source_root"]) / item["source"],
                    _relative(item["target"]),
                    item["sha256"],
                )
            )
        commands = [_normalize(list(row["command"]))]
        if task["task_id"] == "hv4-dev-got-header-value":
            commands.append(_normalize(["node", "node_modules/typescript/bin/tsc", "--noEmit"]))
        oracle = bool(row["baseline_oracle"])
    elif task.get("judge_status") != "implemented" or task["judge"]["kind"] != "v35_reference":
        raise CatalogError("Judge implementation is pending")
    # Additional registries supplement the original frozen H00 checks. They
    # never replace an existing hash-bound reference test.
    original_checks = [
        item for item in task["judge"].get("hidden_tests", ()) if isinstance(item, dict)
    ]
    if original_checks:
        for item in original_checks:
            if not isinstance(item, dict) or not isinstance(item.get("sha256"), str):
                raise CatalogError("Reference judge is not hash-bound")
            source = (base / item["path"]).resolve()
            if not any(
                source.is_relative_to(
                    (base / f"experiments/v35-harness/{split}/verification").resolve()
                )
                for split in ("dev", "holdout")
            ):
                raise CatalogError("Reference judge source is outside its frozen directory")
            # This is the only runtime read of pre-existing hidden contents.
            selected.append((source, "verification/" + source.name, item["sha256"]))
        for value in task["environment"]["verify"]:
            command = _normalize(shlex.split(value))
            if command not in commands:
                commands.append(command)
    if not selected or not commands:
        raise CatalogError("No hash-bound judge checks are available")
    files = []
    for source, relative, expected in selected:
        raw = source.read_bytes()
        if hashlib.sha256(raw).hexdigest() != expected:
            raise CatalogError("Frozen judge source hash does not match")
        target = destination / relative
        if not target.resolve().is_relative_to(destination.resolve()):
            raise CatalogError("Judge target escapes its private directory")
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(raw)
        files.append({"file": str(target), "target": relative, "sha256": expected})
    regressions = [_normalize(shlex.split(value)) for value in task["acceptance"]["regression"]]
    bundle = {
        "task_id": task["task_id"],
        "judge_sha256": task["judge_sha256"],
        "implementation_manifest_sha256": row["_registry_sha256"] if row else None,
        "files": files,
        "commands": commands,
        "regression_commands": regressions,
        "baseline_oracle": oracle,
        "baseline_required": task["type"] == "bugfix",
        "original_verify_commands": list(task["environment"]["verify"]),
        "original_regression_commands": list(task["acceptance"]["regression"]),
    }
    _private_json(destination / "bundle.json", bundle)
    return bundle


def _logs(session: CommandSession, handle_id: str) -> tuple[str, bool]:
    parts = []
    offset = 0
    complete = True
    while True:
        page = session.read_log(handle_id, offset=offset)
        parts.append(page.data)
        complete = complete and not page.truncated and page.dropped_bytes == 0
        if page.eof:
            return "".join(parts), complete
        if page.next_offset <= offset:
            raise DomainError("JUDGE_LOG_INCOMPLETE", "Verifier log pagination stalled", 409)
        offset = page.next_offset


def _run(
    session: CommandSession,
    grant: CapabilityGrant,
    argv: list[str],
    timeout: float,
    evidence: Path,
    index: int,
    *,
    source_tree: str | None = None,
    verifier_tree: str | None = None,
    phase: str | None = None,
) -> tuple[dict[str, Any], str]:
    request = ExecutionRequest(
        operation_id=f"h12-judge-{uuid4().hex}",
        purpose="test",
        argv=tuple(argv),
        cwd=".",
        timeout_seconds=timeout,
        execution_epoch=1,
    )
    handle = session.run(request, grant, CancellationToken(), purpose="test")
    finished = session.wait(handle.handle_id)
    output, complete = _logs(session, handle.handle_id)
    session.assert_authority(handle.handle_id)
    log = evidence / f"command-{index}.log"
    descriptor = os.open(log, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        stream.write(output)
    receipt = {
        **finished.model_dump(mode="json"),
        "complete": complete,
        "log_sha256": hashlib.sha256(output.encode()).hexdigest(),
        "private_log": str(log),
        "receipt_protocol": "h12-independent-command-v1",
        "source_tree_hash": source_tree,
        "verifier_tree_hash": verifier_tree,
        "image_id": session.broker.image_id,
        "phase": phase,
    }
    receipt["failure_diagnosis"] = failure_diagnosis(receipt, output)
    receipt["receipt_id"] = digest(receipt)
    _private_json(evidence / f"command-{index}.json", receipt)
    return receipt, output


def _positive_checks(language: str, argv: list[str], output: str, exit_code: int) -> bool:
    """Use real counts; a successful compiler is a build, never a test count."""
    # Reviewed private Python assertions can observe a Go/Node project. Select
    # their actual fixed runner before interpreting the project's language.
    python_prefixes = (
        _normalize(["python3", "-m", "pytest"]),
        ["python3", "-m", "pytest"],
        ["python3", "-B", "-m", "pytest"],
    )
    if any(argv[:len(prefix)] == prefix for prefix in python_prefixes):
        language = "python"
    if any("typescript/bin/tsc" in arg for arg in argv):
        return exit_code == 0
    marker = re.search(r"H12_CHECKS_PASSED:([1-9][0-9]*)", output)
    if marker:
        return exit_code == 0
    if language in {"javascript", "typescript"}:
        counts = re.findall(r"\b([1-9][0-9]*)\s+(?:passing|tests? passed)\b", output)
        if counts:
            return exit_code == 0
    kind: ProjectKind = "python" if language == "python" else "go" if language == "go" else "node"
    parsed, ran, _, _, _ = parse_tests(kind, output, exit_code)
    return parsed == "passed" and ran > 0


def _reviewed_observation_checks(
    argv: list[str], output: str, exit_code: int, *, complete: bool,
) -> bool:
    """v2 Judge truth is in real Python test identities, regardless of project language."""
    from mgar_v3.harness.regression_types import RegressionCommand
    from mgar_v3.harness.test_results import parse_independent_test

    suite = RegressionCommand(
        suite_id="reviewed-observation-judge", public_argv=tuple(argv), argv=tuple(argv),
        framework="pytest",
    )
    result = parse_independent_test(suite, output, exit_code, complete=complete)
    return (result.status == "PASS" and result.complete and result.tests_run is not None
            and result.tests_run > 0)


def _baseline_failure(language: str, argv: list[str], output: str) -> bool:
    """A failing process is not evidence that a requested assertion ran."""
    if re.search(r"\b[1-9][0-9]*\s+failed\b", output):
        return True  # pytest / AVA summaries contain executed failures.
    if language == "go":
        return bool(re.search(r"--- FAIL: Test", output))
    if language in {"javascript", "typescript"}:
        return bool(re.search(r"\b[1-9][0-9]*\s+failing\b", output))
    # Hash-bound direct Python probes report an executed probe immediately
    # before the behavior invocation; no general stderr/error exit qualifies.
    return (
        any("test_content_type_parameter.py" in arg for arg in argv)
        and "H12_BASELINE_PROBE:content-type" in output
        and bool(re.search(r"(?:AssertionError|IndexError|KeyError|ValueError):?", output))
    )


def _public_node_baseline_failure(
    task: Mapping[str, Any], argv: list[str], output: str, exit_code: int | None,
) -> bool:
    """An executed frozen Node assertion may be red before a feature exists."""
    allowed = tuple(item["target"] for item in task.get("public_assets", ()))
    if (task.get("language") not in {"javascript", "typescript"} or exit_code != 1
            or len(argv) != 3 or argv[:2] != ["node", "--test"] or argv[2] not in allowed):
        return False
    status, _, failed, _, _ = parse_tests("node", output, exit_code)
    return status == "failed" and declared_assertion_failure(
        output, failed, allowed, language=task["language"],
    )


def _public_go_missing_baseline(
    task: Mapping[str, Any], workspace: Path, output: str, exit_code: int | None,
) -> bool:
    """Only a hash-bound new public feature test may reference a missing API."""
    from mgar_v3.quality_eval.h12_public_regression import missing_public_go_target

    if task.get("language") != "go" or task.get("type") != "feature" or exit_code != 1:
        return False
    paths = []
    for asset in task.get("public_assets", ()):
        if not isinstance(asset, Mapping):
            return False
        name = asset.get("target", "")
        if not isinstance(name, str):
            return False
        path = workspace / name
        if (not name.endswith("_test.go")
                or Path(name).is_absolute() or ".." in Path(name).parts
                or not path.is_file() or path.is_symlink()
                or any(parent.is_symlink() for parent in path.parents
                       if parent == workspace or parent.is_relative_to(workspace))
                or not path.resolve().is_relative_to(workspace.resolve())
                or hashlib.sha256(path.read_bytes()).hexdigest() != asset.get("sha256")):
            return False
        paths.append(name)
    return bool(paths) and missing_public_go_target(output, paths)


def verify(
    workspace: Path,
    task: Mapping[str, Any],
    image_lock: Path,
    evidence: Path,
    *,
    phase: Literal["environment", "regression", "baseline", "final"] = "final",
    expected_tree: str | None = None,
    sealed: bool = False,
    frozen_tests: Mapping[str, str] | None = None,
    baseline_evidence: Path | None = None,
    registry_path: Path | None = None,
    registry_sha256: str | None = None,
    nofile_limit: int = 128,
    log_bytes: int = 524288,
    memory_mb: int = 256,
    environment_revision: str | None = None,
    offline_network: Literal["none", "connect-timeout-v1", "got-network-v1"] = "none",
    baseline_policy: TaskBaselinePolicy | None = None,
    traceability_ref: tuple[Path, str] | None = None,
    evaluation_artifacts: ArtifactStore | None = None,
    experiment_id: str = "r06-standalone-baseline",
    public_probe_root: Path | None = None,
) -> dict[str, Any]:
    """Judge one immutable original tree on a separate read-only verifier copy.

    The caller obtains ``sealed`` from its Engine authority. It must never
    resume model editing or feed this private result/log bundle to the model.
    """
    original = Path(workspace).resolve()
    if phase not in {"environment", "regression", "baseline", "final"}:
        raise CatalogError("Unknown judge phase")
    if environment_revision not in {
        None,
        "h12-dev-environment-v3",
        "h12-dev-environment-v4",
        "h12-holdout-infrastructure-v4",
    }:
        raise CatalogError("Unsupported trusted environment revision")
    if (
        environment_revision in {"h12-dev-environment-v3", "h12-dev-environment-v4"}
        and task.get("split") != "dev"
    ):
        raise CatalogError("Dev environment cannot be applied to historical holdout")
    if environment_revision == "h12-holdout-infrastructure-v4" and task.get("split") != "holdout":
        raise CatalogError("Holdout infrastructure revision requires holdout input")
    if offline_network != "none" and environment_revision not in {
        "h12-dev-environment-v4",
        "h12-holdout-infrastructure-v4",
    }:
        raise CatalogError("Offline network fixtures require an explicit new revision")
    if phase in {"final", "regression"} and not sealed:
        return {"status": "NOT_RUN", "reason": "MODEL_SESSION_NOT_SEALED"}
    if phase in {"final", "regression"} and frozen_tests is None:
        return {"status": "NOT_RUN", "reason": "ORIGINAL_TEST_HASHES_REQUIRED"}
    evidence.mkdir(parents=True, exist_ok=True, mode=0o700)
    captured_files, unsafe_links = _scan(original)
    if unsafe_links:
        return {
            "status": "NOT_RUN" if phase == "environment" else "FAIL",
            "reason": "SOURCE_SYMLINK_DENIED",
        }
    captured = _tree_hash(captured_files)
    if expected_tree is not None and expected_tree != captured:
        return {"status": "FAIL", "reason": "FINAL_TREE_CHANGED", "tree_hash": captured}
    current_tests = test_hashes(original)
    if frozen_tests is not None and any(current_tests.get(p) != h for p, h in frozen_tests.items()):
        return {"status": "FAIL", "reason": "FROZEN_TEST_MODIFIED", "tree_hash": captured}
    if baseline_policy is not None and baseline_policy.task_kind != task["type"]:
        raise CatalogError("R06 baseline policy task type differs")
    if baseline_policy is not None and phase in {"environment", "baseline"}:
        from mgar_v3.quality_eval.h12_baseline_policy import execute_baseline
        from mgar_v3.quality_eval.h12_independent_runner import IndependentRegressionRunner

        artifacts = evaluation_artifacts or ArtifactStore(evidence / "cas")
        independent = IndependentRegressionRunner(
            artifacts, image_lock, repository=task["repository"], task_id=task["task_id"],
            experiment_id=experiment_id, memory_mb=memory_mb, nofile=nofile_limit,
            log_bytes=log_bytes, offline_network=offline_network,
            public_probe_root=public_probe_root,
        )
        assessment_ref, assessment = execute_baseline(
            independent, baseline_policy, original, evidence / "public-baseline",
            tree=captured, frozen_tests=current_tests,
        )
        result = {"status": "PASS" if assessment.status == "PASS" else "NOT_RUN",
                  "execution_status": assessment.status, "reason": assessment.detail,
                  "task_id": task["task_id"], "phase": phase, "tree_hash": captured,
                  "judge_sha256": task["judge_sha256"],
                  "baseline_method": "r06-public-task-type-policy",
                  "baseline_assessment_ref": assessment_ref,
                  "baseline_assessment": assessment.model_dump(mode="json")}
        _private_json(evidence / "judge-result.json", result)
        return result
    receipts = []
    status, reason = "NOT_RUN", "VERIFIER_UNAVAILABLE"
    baseline_test_status = "NOT_RUN"
    copy_root = Path(tempfile.mkdtemp(prefix="magr-h12-verifier-", dir="/tmp"))
    session: CommandSession | None = None
    bundle: dict[str, Any] | None = None
    verifier_tree: str | None = None
    verifier_image: str | None = None
    judge_review: dict[str, Any] | None = None
    reviewed_observation = False
    reference_baseline = (
        phase == "baseline"
        and task["type"] == "bugfix"
        and task.get("judge", {}).get("kind") == "v35_reference"
    )
    try:
        stage = copy_root / "private"
        bundle = prepare_judge(
            task, stage, registry_path=registry_path, registry_sha256=registry_sha256
        )
        if baseline_policy is not None and phase == "final":
            from mgar_v3.quality_eval.h12_judge_review import read_manifest, review_judge

            if traceability_ref is None:
                raise CatalogError("R06 final Judge needs a frozen traceability review")
            manifest = read_manifest(*traceability_ref)
            reviewed_observation = manifest.version == "harness-judge-traceability-v2"
            sources = {item["target"]: Path(item["file"]).read_bytes() for item in bundle["files"]}
            judge_review = review_judge(baseline_policy, manifest, sources, task_id=task["task_id"],
                                       commands=bundle["commands"])
            _private_json(evidence / "traceability-manifest.json", manifest.model_dump(mode="json"))
            # Preserve exact original bytes, including intentional JSON formatting.
            (evidence / "traceability-manifest.json").write_bytes(traceability_ref[0].read_bytes())
            if bundle["baseline_oracle"]:
                raise CatalogError(
                    "R06 requires a revised Judge with a separate public behavior baseline")
        public_refactor_baseline = (
            phase == "baseline" and task["type"] == "refactor"
            and not bundle["baseline_oracle"]
        )
        target = copy_root / "workspace"
        try:
            _copy_tree_nofollow(original, target, captured_files)
        except (DomainError, OSError):
            return {"status": "FAIL", "reason": "TREE_MOVED_DURING_COPY", "tree_hash": captured}
        if tree_hash(original) != captured or tree_hash(target) != captured:
            return {"status": "FAIL", "reason": "TREE_MOVED_DURING_COPY", "tree_hash": captured}
        for item in (
            bundle["files"]
            if phase not in {"environment", "regression"}
            and not reference_baseline and not public_refactor_baseline
            else ()
        ):
            path = target / item["target"]
            if not path.resolve().is_relative_to(target.resolve()):
                raise CatalogError("Judge target resolves outside the verifier copy")
            if path.exists():
                raise CatalogError("Workspace already contains a private judge target")
            path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(item["file"], path)
        commands = list(bundle["commands"])
        if reference_baseline:
            from mgar_v3.quality_eval.h12 import public_commands

            # H00 reference tests may import a symbol introduced by the fix.
            # Reproduce the defect with its frozen public assertion first;
            # every original hidden check remains mandatory at final judging.
            commands = [
                _normalize(argv)
                for argv in public_commands(
                    {
                        "environment": task["environment"],
                        "acceptance": {"regression": []},
                    }
                )
            ]
            if not commands:
                raise CatalogError("A frozen public reference baseline command is required")
        if phase in {"environment", "regression"} or public_refactor_baseline:
            # Check only frozen existing regressions. The environment's goal
            # commands may name a function/file/npm script the model must add.
            commands = public_regression_commands(task, environment_revision)
            if not commands:
                raise CatalogError("No existing public regression command is available")
        if bundle["baseline_oracle"] and phase not in {"environment", "regression"}:
            if phase == "baseline":
                commands = [[*commands[0], "--capture"]]
            elif baseline_evidence is None or not (baseline_evidence / "oracle.json").is_file():
                return {
                    "status": "NOT_RUN",
                    "reason": "PINNED_BASELINE_ORACLE_REQUIRED",
                    "tree_hash": captured,
                }
            else:
                baseline = json.loads((baseline_evidence / "oracle-binding.json").read_text())
                raw = (baseline_evidence / "oracle.json").read_bytes()
                if (
                    baseline["task_id"] != task["task_id"]
                    or baseline["judge_sha256"] != task["judge_sha256"]
                    or hashlib.sha256(raw).hexdigest() != baseline["oracle_sha256"]
                ):
                    raise CatalogError("Baseline oracle binding changed")
                oracle = target / "tests/hidden/h12_baseline_oracle.json"
                oracle.write_bytes(raw)
                commands = [[*commands[0], "--check", "tests/hidden/h12_baseline_oracle.json"]]
        commands = _environment_commands(commands, task["repository"], environment_revision)
        # The copy is not model visible. Make only this unique /tmp hierarchy
        # readable by the fixed container UID; the mount is read-only.
        copy_root.chmod(0o755)
        for path in [target, *target.rglob("*")]:
            if path.is_symlink():
                continue
            path.chmod(path.stat().st_mode | (0o555 if path.is_dir() else 0o444))
        verifier_tree = tree_hash(target)
        allowed = tuple(sorted({command[0] for command in commands}))
        lease = issue_lease(
            mode="investigate",
            actor="h12-verifier",
            workspace_id=f"judge-{uuid4().hex}",
            repository=task["repository"],
            execution_epoch=1,
            read_paths=("",),
            write_paths=(),
            allowed_commands=allowed,
            timeout_seconds=300,
            max_output_bytes=log_bytes,
            memory_mb=memory_mb,
            scratch_mb=256,
            nofile_limit=nofile_limit,
            offline_network=offline_network,
        )
        grant = CapabilityGrant(
            actor=lease.actor,
            workspace_id=lease.workspace_id,
            repository=lease.repository,
            read_paths=("",),
            write_paths=(),
            execute=True,
            allowed_commands=allowed,
            max_seconds=300,
        )
        broker = IsolatedExecutionBroker(target, lease, lock_path=image_lock)
        verifier_image = broker.image_id
        session = CommandSession(broker)
        status, reason = (
            "PASS",
            "PUBLIC_ENVIRONMENT_READY" if phase == "environment" else
            "PUBLIC_BEHAVIOR_BASELINE_VERIFIED" if public_refactor_baseline else
            "INDEPENDENT_JUDGE_PASSED",
        )
        for index, argv in enumerate(commands):
            receipt, output = _run(
                session, grant, argv, 300, evidence, index,
                source_tree=captured, verifier_tree=verifier_tree, phase=phase,
            )
            receipt["failure_diagnosis"] = failure_diagnosis(receipt, output)
            receipts.append(receipt)
            failed = receipt["state"] != "exited" or receipt["exit_code"] != 0
            environment_reason = receipt["failure_diagnosis"]["legacy_environment_reason"]
            if environment_reason:
                status, reason = "NOT_RUN", environment_reason
                break
            if phase in {"environment", "regression"} or public_refactor_baseline:
                # Public goal tests can intentionally fail before a feature
                # exists. Keep that red baseline distinct from dependency or
                # collection failures; final regression still requires green.
                if failed:
                    if not (
                        phase == "environment"
                        and receipt["state"] == "failed"
                        and (
                            _baseline_failure(task["language"], argv, output)
                            or _public_node_baseline_failure(
                                task, argv, output, receipt["exit_code"],
                            )
                            or _public_go_missing_baseline(
                                task, original, output, receipt["exit_code"],
                            )
                        )
                    ):
                        status, reason = (
                            ("NOT_RUN", "PUBLIC_BASELINE_PRECONDITION_UNAVAILABLE")
                            if phase == "environment"
                            else ("FAIL", "PUBLIC_REGRESSION_FAILED")
                        )
                        break
                    baseline_test_status = "FAIL"
                elif not _positive_checks(task["language"], argv, output, receipt["exit_code"]):
                    status, reason = "NOT_RUN", "REGRESSION_TEST_COUNT_UNVERIFIED"
                    break
                elif baseline_test_status != "FAIL":
                    baseline_test_status = "PASS"
                continue
            if phase == "baseline" and task["type"] == "bugfix":
                if re.search(
                    r"ImportError:|ERROR collecting|(?:collection|collecting).*error",
                    output,
                    flags=re.IGNORECASE,
                ):
                    status, reason = "NOT_RUN", "BASELINE_DEPENDENCY_UNAVAILABLE"
                    break
                status = (
                    "PASS"
                    if (
                        failed
                        and receipt["state"] == "failed"
                        and _baseline_failure(task["language"], argv, output)
                    )
                    else "NOT_RUN"
                )
                reason = "BASELINE_REPRODUCED" if status == "PASS" else "BASELINE_NOT_REPRODUCED"
                break
            if failed:
                status, reason = "FAIL", "INDEPENDENT_CHECK_FAILED"
                break
            if phase == "baseline" and bundle["baseline_oracle"]:
                oracle_data = json.loads(output.strip().splitlines()[-1])
                oracle_raw = json.dumps(oracle_data, sort_keys=True).encode()
                descriptor = os.open(
                    evidence / "oracle.json", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600
                )
                with os.fdopen(descriptor, "wb") as stream:
                    stream.write(oracle_raw)
                _private_json(
                    evidence / "oracle-binding.json",
                    {
                        "task_id": task["task_id"],
                        "judge_sha256": task["judge_sha256"],
                        "tree_hash": captured,
                        "oracle_sha256": hashlib.sha256(oracle_raw).hexdigest(),
                    },
                )
            elif not (
                _reviewed_observation_checks(argv, output, receipt["exit_code"],
                                             complete=receipt["complete"])
                if reviewed_observation else
                _positive_checks(task["language"], argv, output, receipt["exit_code"])
            ):
                status, reason = "NOT_RUN", "REGRESSION_TEST_COUNT_UNVERIFIED"
                break
        if tree_hash(original) != captured or tree_hash(target) != verifier_tree:
            status, reason = "FAIL", "TREE_CHANGED_DURING_JUDGING"
    except (CatalogError, OSError, ValueError, DomainError) as error:
        status, reason = "NOT_RUN", getattr(error, "code", "JUDGE_ENVIRONMENT_UNAVAILABLE")
    finally:
        if session is not None:
            session.close()
        shutil.rmtree(copy_root)
    if phase == "final" and status == "PASS":
        # Private probes may be inspected by full-repository lint/test scripts.
        # Run the unchanged public regression on its own sealed source copy.
        public_result = verify(
            original,
            task,
            image_lock,
            evidence / "public-regressions",
            phase="regression",
            expected_tree=captured,
            sealed=sealed,
            frozen_tests=frozen_tests,
            registry_path=registry_path,
            registry_sha256=registry_sha256,
            nofile_limit=nofile_limit,
            log_bytes=log_bytes,
            memory_mb=memory_mb,
            environment_revision=environment_revision,
            offline_network=offline_network,
        )
        receipts.extend(public_result.get("receipts", []))
        if public_result["status"] != "PASS":
            status, reason = public_result["status"], public_result["reason"]
    result = {
        "task_id": task["task_id"],
        "status": status,
        "reason": reason,
        "phase": phase,
        "tree_hash": captured,
        "source_tree_hash": captured,
        "verifier_tree_hash": verifier_tree,
        "image_id": verifier_image,
        "receipt_protocol": "h12-independent-judge-v1",
        "receipts": receipts,
        "judge_sha256": task["judge_sha256"],
        "hashes": {item["target"]: item["sha256"] for item in bundle["files"]} if bundle else {},
        **({"baseline_policy_hash": baseline_policy.policy_hash,
            "judge_registry_sha256": registry_sha256,
            "judge_traceability_sha256": traceability_ref[1] if traceability_ref else None,
            "judge_review": judge_review} if baseline_policy is not None else {}),
        "baseline_reproduced": phase == "baseline" and reason == "BASELINE_REPRODUCED",
        "baseline_method": (
            "frozen_public_reference" if reference_baseline else
            "frozen_public_refactor_behavior" if (
                task["type"] == "refactor" and bundle and not bundle["baseline_oracle"]
            ) else "frozen_private_probe"
        )
        if phase == "baseline"
        else None,
        "baseline_test_status": baseline_test_status if phase == "environment" else None,
    }
    _private_json(evidence / "judge-result.json", result)
    return result
