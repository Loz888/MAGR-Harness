"""H12 frozen-catalog preflight and honest scoring, without model execution.

Only the developer evaluator launches the isolated catalog reader. None of
these helpers is registered as a project tool. Hidden judge implementations,
reference fixes and gold files are never opened by preflight.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
import statistics
import subprocess
import sys
from collections import Counter
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, cast

from mgar_v3.quality_eval.harness_v4 import (
    CatalogError,
    load_baseline,
    load_jsonl,
    repo_root,
    validate_task,
)

Split = str
_COMMIT = re.compile(r"^[a-f0-9]{40}$")
_PRIVATE_TOKEN = re.compile(r"hidden|holdout|gold|verification|judge|fix_commit", re.IGNORECASE)
_SHELL = re.compile(r"[;&|<>`\n\r]|\$\(")
_FROZEN = {
    "experiments/harness-v4/scoring-v1.json": (
        "6bdf1229c78f0ed73c8d4775d43e1deb4e65c12d371573c2ff652bc99dc19f81"
    ),
    "experiments/harness-v4/task-types.json": (
        "66cc1e59bf74922b0b6e80379f14434d54cb67550311525972b19b96348f3d1a"
    ),
    "experiments/harness-v4/tasks/dev.jsonl": (
        "43c47739bbc2ca9370619067f359545c8d0e9652e0011187dca327283e3cad30"
    ),
    "experiments/harness-v4/tasks/holdout.jsonl": (
        "c8b98abb3d528a1c5e87390105fc75bc84eedb4bbfcfa8469ff33399d946745b"
    ),
}
_STATUSES = {"PASS", "FAIL", "NOT_RUN", "UNKNOWN"}
_METADATA_KEYS = (
    "task_id",
    "repository",
    "base_commit",
    "snapshot_dir",
    "snapshot_digest",
    "public_targets",
    "public_files",
)
_V35_METADATA_SHA256 = "75cfb77fead1f348cc0f85b0d17d0cb38effa97a3da851b7c7fd5b6e3078f779"
_V35_HOLDOUT_METADATA_SHA256 = "1e06581a7865f5540977f66b84c6a76e1f379d4aa972ad0eb473b8e80bd9fe4c"
_V35_HOLDOUT_PUBLIC_TREE_SHA256 = "e6431e3c40f614b806b77f24cbb3659204de9d3ca04eee7f3719094023e0243f"
_V35_PUBLIC_SHA256 = {
    "dev/public/test_help_name_public.py": (
        "31cc493fa58517af0250bc3ff54d9ea46f90196b3f84254c060dd767f537d493"
    ),
    "dev/public/test_short_help_abbreviation_public.py": (
        "0f9084fbc57f3e19d23b18277011c7d92a94d65e388034fe45b59b29a6f4cc65"
    ),
    "dev/public/test_short_help_public.py": (
        "724549e3c3961295b09fff7b43e55e0bb1734d5b8f5632a75f6de32f1a69bdac"
    ),
}


def _split(split: str) -> None:
    if split not in {"dev", "holdout"}:
        raise CatalogError("H12 split must be dev or holdout")


def frozen_hashes(root: Path | None = None) -> dict[str, Any]:
    """Hash frozen bytes, including opaque holdout bytes, without parsing them."""
    base = root or repo_root()
    try:
        baseline = load_baseline(base)
    except (OSError, json.JSONDecodeError):
        return {
            "status": "FAIL",
            "baseline_valid": False,
            "files": [],
            "reason": "FROZEN_BASELINE_UNAVAILABLE",
        }
    recorded = baseline.get("task_catalog", {}).get("files", {})
    rows = []
    for name, expected in _FROZEN.items():
        path = base / name
        actual = hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None
        rows.append(
            {
                "file": name,
                "expected": expected,
                "actual": actual,
                "matches": actual == expected and recorded.get(name) == expected,
            }
        )
    baseline_valid = (
        baseline.get("schema") == "mgar-hv4-baseline-v1"
        and baseline.get("status") == "FROZEN"
        and recorded == _FROZEN
        and baseline.get("task_catalog", {}).get("per_split") == {"dev": 12, "holdout": 12}
    )
    return {
        "status": "PASS" if baseline_valid and all(r["matches"] for r in rows) else "FAIL",
        "baseline_valid": baseline_valid,
        "files": rows,
    }


def _safe_path(value: Any) -> bool:
    return (
        isinstance(value, str)
        and bool(value)
        and not value.startswith(("/", "\\"))
        and ".." not in Path(value).parts
        and "\\" not in value
        and not _PRIVATE_TOKEN.search(value)
    )


def public_commands(task: Mapping[str, Any]) -> list[list[str]]:
    """Project argv, never shell text; remove private targets before publication."""
    environment = task.get("environment", {})
    acceptance = task.get("acceptance", {})
    commands = [*environment.get("verify", ()), *acceptance.get("regression", ())]
    result: list[list[str]] = []
    for command in commands:
        if not isinstance(command, str) or _SHELL.search(command):
            continue
        try:
            argv = shlex.split(command)
        except ValueError:
            continue
        if not argv or any("=" in x for x in argv[:1]):
            continue
        # A hidden test selector affects the command as a whole. A separate
        # pytest file argument can instead be removed without exposing it.
        if any(_PRIVATE_TOKEN.search(x) and "/" not in x for x in argv):
            continue
        private = [i for i, value in enumerate(argv) if _PRIVATE_TOKEN.search(value)]
        if private:
            if argv[:3] not in (["python", "-m", "pytest"], ["python3", "-m", "pytest"]):
                continue
            argv = [value for index, value in enumerate(argv) if index not in private]
            if not any(not value.startswith("-") for value in argv[3:]):
                continue
        if not any(_PRIVATE_TOKEN.search(x) for x in argv) and argv not in result:
            result.append(argv)
    return result


def project_task(task: Mapping[str, Any]) -> dict[str, Any]:
    """The complete model-visible whitelist; never pass through unknown keys."""
    result = {
        key: task[key]
        for key in (
            "task_id",
            "split",
            "type",
            "composition",
            "repository",
            "base_commit",
            "language",
            "goal",
        )
        if key in task
    }
    result["scope"] = [path for path in task.get("scope", ()) if _safe_path(path)]
    result["resources"] = {
        key: value
        for key, value in task.get("resources", {}).items()
        if key
        in {
            "max_model_calls",
            "max_tool_calls",
            "max_cost_micros",
            "deadline_seconds",
            "max_context_tokens",
            "max_repair_rounds",
        }
        and type(value) is int
        and value > 0
    }
    result["public_commands"] = public_commands(task)
    return result


def public_requirements(task: Mapping[str, Any]) -> tuple[str, ...]:
    """New contract input is explicit and public; unsafe clauses block instead of disappearing."""
    required = task.get("acceptance", {}).get("required", [])
    if not isinstance(required, (list, tuple)) or len(required) > 24:
        raise CatalogError("Public acceptance requirements exceed their bound")
    result: list[str] = []
    for value in required:
        if not isinstance(value, str) or not value.strip() or len(value) > 1024:
            raise CatalogError("Public acceptance requirement is invalid or unbounded")
        if _PRIVATE_TOKEN.search(value):
            raise CatalogError("Private metadata cannot be projected as a public contract")
        if value not in result:
            result.append(value)
    return tuple(result)


def public_objective(task: Mapping[str, Any]) -> str:
    """Carry frozen public requirements without private acceptance metadata."""
    safe = project_task(task)
    requirements = task.get("acceptance", {}).get("required", [])
    if not isinstance(requirements, (list, tuple)) or len(requirements) > 24:
        raise CatalogError("Public acceptance requirements exceed their bound")
    public: list[str] = []
    for item in requirements:
        if not isinstance(item, str) or not item.strip() or len(item) > 1024:
            raise CatalogError("Public acceptance requirement is invalid or unbounded")
        if not _PRIVATE_TOKEN.search(item) and item not in public and item != safe["goal"]:
            public.append(item)
    objective = str(safe["goal"])
    if public:
        objective += "\n\nPublic acceptance requirements:\n" + "\n".join(
            "- " + item for item in public
        )
    if len(objective) > 16_000:
        raise CatalogError("Public objective exceeds its bound")
    return objective


def _read_catalog(split: str, root: Path) -> list[dict[str, Any]]:
    _split(split)
    tasks = load_jsonl(root / "experiments/harness-v4/tasks" / f"{split}.jsonl")
    if len(tasks) != 12 or len({t.get("task_id") for t in tasks}) != 12:
        raise CatalogError("Frozen split must have twelve distinct tasks")
    for task in tasks:
        if validate_task(task, split):
            # Do not include hidden metadata or its values in a traceback.
            raise CatalogError("Frozen task validation failed")
    return tasks


def _isolated_projection(root: Path) -> list[dict[str, Any]]:
    """Called only by the dedicated child; no private judge body leaves it."""
    if frozen_hashes(root)["status"] != "PASS":
        raise CatalogError("Frozen bytes changed before isolated reading")
    return [
        {
            **project_task(task),
            "judge_status": task["judge_status"],
            "judge_sha256": task["judge_sha256"],
        }
        for task in _read_catalog("holdout", root)
    ]


def load_frozen_tasks(split: str, root: Path | None = None) -> list[dict[str, Any]]:
    """Dev is public; holdout parsing runs in an isolated interpreter.

    The holdout return value is a safe projection plus opaque judge metadata.
    Store it privately: an orchestrator must not dump it into an assistant's
    context. The independent judge worker loads its own private original task.
    """
    _split(split)
    base = (root or repo_root()).resolve()
    if frozen_hashes(base)["status"] != "PASS":
        raise CatalogError("Frozen H00 bytes do not match")
    if split == "dev":
        return _read_catalog(split, base)
    code = (
        "import json,sys;from pathlib import Path;"
        "sys.path.insert(0,str(Path(sys.argv[1])/'src'));"
        "from mgar_v3.quality_eval.h12 import _isolated_projection;"
        "json.dump(_isolated_projection(Path(sys.argv[1])),sys.stdout)"
    )
    child = subprocess.run(
        [sys.executable, "-I", "-B", "-c", code, str(base)],
        cwd=base,
        env={"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "LANG": "C.UTF-8"},
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    if child.returncode:
        raise CatalogError("Isolated holdout projection failed")
    value = json.loads(child.stdout)
    if (
        not isinstance(value, list)
        or len(value) != 12
        or not all(isinstance(x, dict) for x in value)
    ):
        raise CatalogError("Isolated holdout projection has invalid shape")
    return value


def _public_v35_metadata(root: Path, split: str = "dev") -> list[dict[str, Any]]:
    _split(split)
    path = (
        root
        / "experiments/v35-harness"
        / ("tasks.jsonl" if split == "dev" else "holdout/tasks.jsonl")
    )
    if not path.is_file():
        return []
    expected = _V35_METADATA_SHA256 if split == "dev" else _V35_HOLDOUT_METADATA_SHA256
    if path.is_symlink() or hashlib.sha256(path.read_bytes()).hexdigest() != expected:
        raise CatalogError("Public source metadata changed from H12 preflight freeze")
    # Parse the public manifest, discard all other keys immediately. Never use
    # the V35 Task loader: it exposes gold/hidden helper methods unnecessarily.
    return [{key: row[key] for key in _METADATA_KEYS if key in row} for row in load_jsonl(path)]


def _public_v35_hashes(root: Path, split: str) -> dict[str, str]:
    """Validate public fixtures only; never open a hidden test or reference fix."""
    _split(split)
    if split == "dev":
        return dict(_V35_PUBLIC_SHA256)
    experiment = root / "experiments/v35-harness"
    public = experiment / "holdout/public"
    if not public.is_dir() or public.is_symlink():
        raise CatalogError("Frozen public fixture directory is unavailable")
    files = {}
    for path in sorted(public.rglob("*")):
        if path.is_symlink():
            raise CatalogError("Frozen public fixtures cannot contain symbolic links")
        if path.is_file():
            files[path.relative_to(experiment).as_posix()] = hashlib.sha256(
                path.read_bytes()
            ).hexdigest()
    payload = "\n".join(f"{name}\0{value}" for name, value in sorted(files.items()))
    if hashlib.sha256(payload.encode()).hexdigest() != _V35_HOLDOUT_PUBLIC_TREE_SHA256:
        raise CatalogError("Frozen public fixture inventory changed")
    return files


def _snapshot_digest(directory: Path) -> str:
    excluded = {".git", "__pycache__", ".pytest_cache", ".mypy_cache"}
    rows = []
    for path in sorted(directory.rglob("*")):
        if path.is_symlink():
            raise CatalogError("Pinned snapshot must not traverse symbolic links")
        if (
            not path.is_file()
            or excluded & set(path.relative_to(directory).parts)
            or path.suffix in {".pyc", ".pyo"}
        ):
            continue
        rows.append(
            f"{path.relative_to(directory).as_posix()}\0"
            + hashlib.sha256(path.read_bytes()).hexdigest()
        )
    return hashlib.sha256("\n".join(rows).encode()).hexdigest()


def _git(path: Path, *args: str) -> str | None:
    try:
        result = subprocess.run(
            ["git", "-C", str(path), *args],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
            env={
                "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
                "GIT_TERMINAL_PROMPT": "0",
                "GIT_OPTIONAL_LOCKS": "0",
                "GIT_CONFIG_NOSYSTEM": "1",
                "GIT_CONFIG_GLOBAL": "/dev/null",
            },
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    return result.stdout.strip() if result.returncode == 0 else None


def _source(
    task: Mapping[str, Any],
    root: Path,
    sources: Mapping[str, Path | str],
    metadata: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    commit = task.get("base_commit", "")
    repository = task.get("repository", "")
    if (
        not isinstance(commit, str)
        or not _COMMIT.fullmatch(commit)
        or not isinstance(repository, str)
        or not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository)
    ):
        return {"available": False, "reason": "PIN_METADATA_INVALID"}
    explicit = sources.get(str(task["task_id"]))
    candidate = Path(explicit) if explicit else root / ".local/h12/sources" / repository / commit
    reference = task.get("judge", {}).get("reference_task")
    if explicit is None and reference:
        for row in metadata:
            if (
                row.get("task_id") == reference
                and row.get("repository") == repository
                and row.get("base_commit") == commit
            ):
                candidate = (root / "experiments/v35-harness" / str(row["snapshot_dir"])).resolve()
    if candidate.is_dir():
        actual_commit = _git(candidate, "rev-parse", "--verify", commit + "^{commit}")
        tree = _git(candidate, "rev-parse", "--verify", commit + "^{tree}")
        if actual_commit == commit and tree and re.fullmatch(r"[a-f0-9]{40}", tree):
            return {
                "available": True,
                "kind": "git_object",
                "path": str(candidate.resolve()),
                "commit": commit,
                "git_tree": tree,
                "source_sha256": hashlib.sha256(
                    f"{repository}\0{commit}\0{tree}".encode()
                ).hexdigest(),
                "hash_method": "repository+commit+git-tree",
                "materialization_required": True,
                "public_files": [],
            }
    for row in metadata:
        if row.get("repository") != repository or row.get("base_commit") != commit:
            continue
        if reference and row.get("task_id") != reference:
            continue
        experiment = root / "experiments/v35-harness"
        snapshot = (experiment / str(row["snapshot_dir"])).resolve()
        if explicit is not None and candidate.resolve() != snapshot:
            continue
        if not snapshot.is_relative_to((root / "experiments").resolve()) or not snapshot.is_dir():
            continue
        try:
            actual = _snapshot_digest(snapshot)
        except (CatalogError, OSError):
            return {"available": False, "reason": "SNAPSHOT_INTEGRITY_FAILED"}
        if actual != row.get("snapshot_digest"):
            return {"available": False, "reason": "SNAPSHOT_DIGEST_MISMATCH"}
        public = []
        for source, target in zip(
            row.get("public_files", ()), row.get("public_targets", ()), strict=True
        ):
            if task.get("split") != "dev" or not reference:
                continue
            source_path = (experiment / source).resolve()
            if (
                source_path.is_relative_to((experiment / "dev/public").resolve())
                and source_path.is_file()
                and _safe_path(target)
            ):
                actual_public = hashlib.sha256(source_path.read_bytes()).hexdigest()
                if actual_public != _V35_PUBLIC_SHA256.get(source):
                    return {"available": False, "reason": "PUBLIC_TEST_DIGEST_MISMATCH"}
                public.append(
                    {
                        "file": str(source_path),
                        "target": target,
                        "sha256": actual_public,
                    }
                )
        return {
            "available": True,
            "kind": "v35_frozen_snapshot",
            "path": str(snapshot),
            "commit": commit,
            "source_sha256": actual,
            "expected_sha256": row["snapshot_digest"],
            "hash_method": "v35-snapshot-path+content-sha256",
            "public_files": public,
            "materialization_required": False,
        }
    return {
        "available": False,
        "reason": "PINNED_SOURCE_UNAVAILABLE",
        "commit": commit,
        "candidate": str(candidate),
    }


def preflight(
    split: str = "dev",
    root: Path | None = None,
    sources: Mapping[str, Path | str] | None = None,
) -> dict[str, Any]:
    """Check local source objects and declared judges; never fetch or execute."""
    _split(split)
    base = root or repo_root()
    frozen = frozen_hashes(base)
    if frozen["status"] != "PASS":
        return {
            "split": split,
            "status": "NOT_RUN",
            "denominator": 12,
            "frozen": frozen,
            "tasks": [],
            "blockers": ["FROZEN_BYTES_MISMATCH"],
        }
    tasks = load_frozen_tasks(split, base)
    metadata = _public_v35_metadata(base, split)
    from mgar_v3.quality_eval.h12_judge import implementation_status

    rows = []
    for task in tasks:
        source = _source(task, base, sources or {}, metadata)
        implementation = implementation_status(task, base)
        blockers = []
        if not source["available"]:
            blockers.append(source["reason"])
        if not implementation["available"]:
            blockers.append("JUDGE_SPEC_PENDING")
        rows.append(
            {
                "task_id": task["task_id"],
                "type": task["type"],
                "status": "SOURCE_READY" if not blockers else "NOT_RUN",
                "source": source,
                "judge_status": task.get("judge_status"),
                "judge_implementation": implementation,
                "judge_sha256": task.get("judge_sha256"),
                "judge_execution": "NOT_RUN",
                "blockers": blockers,
            }
        )
    ready = sum(row["status"] == "SOURCE_READY" for row in rows)
    return {
        "split": split,
        "status": "SOURCE_READY" if ready == 12 else "NOT_RUN",
        "denominator": 12,
        "ready_count": ready,
        "not_run_count": 12 - ready,
        "frozen": frozen,
        "tasks": rows,
        "scope": "local pinned-source availability and declared judge status only",
        "execution_readiness": "NOT_CHECKED",
        "paid_model_calls": 0,
        "cost_micros": 0,
    }


def _independent_dimensions_match(row: Mapping[str, Any]) -> bool:
    from mgar_v3.harness.regression_types import DimensionReceipt, valid_seal

    dimensions = row.get("dimensions")
    kinds = {"target_behavior", "public_regression", "independent_judge",
             "structural_constraints", "execution_integrity"}
    mandatory = {"public_regression", "independent_judge", "execution_integrity"}
    if not isinstance(dimensions, dict) or set(dimensions) != kinds:
        return False
    try:
        for key, data in dimensions.items():
            value = DimensionReceipt.model_validate(data)
            if (
                value.kind != key
                or not valid_seal(value)
                or value.tree_hash != row.get("final_tree")
                or key in mandatory and not value.required
                or value.required and value.status != "PASS"
                or value.status == "PASS" and not value.evidence_refs
                or not value.required and value.status not in {"NOT_RUN", "PASS"}
            ):
                return False
    except (ValueError, TypeError):
        return False
    return True


def score_records(
    records: Sequence[Mapping[str, Any]],
    split: str = "dev",
    *,
    expected_tasks: Sequence[Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    """Score first samples over twelve; do not turn unknown usage into free work."""
    _split(split)
    if len(records) > 12:
        raise CatalogError("A split has twelve first samples; retries belong in attempts")
    ids = [row.get("task_id") for row in records]
    if any(not isinstance(value, str) or not value for value in ids) or len(set(ids)) != len(ids):
        raise CatalogError("Task IDs must be nonempty and distinct; never select a best retry")
    catalog = list(expected_tasks) if expected_tasks is not None else load_frozen_tasks(split)
    expected = {task["task_id"]: task for task in catalog}
    if len(expected) != 12 or any(value not in expected for value in ids):
        raise CatalogError("Score records must belong to the twelve frozen task IDs")
    outcomes: list[dict[str, Any]] = []
    budget_errors: list[str] = []
    counts: Counter[str] = Counter({"NOT_RUN": 12 - len(records)})
    by_type: dict[str, Counter[str]] = {}
    false_completions = unauthorized = blind_retries = regressions = 0
    settled = reserved = 0
    walls = []
    unknown_usage = False
    resource_violations: list[str] = []
    unverified_regressions: list[str] = []
    for row in records:
        if row.get("split", split) != split or row.get("status") not in _STATUSES:
            raise CatalogError("Score split or outcome status is invalid")
        status = str(row["status"])
        task = expected[row["task_id"]]
        if row.get("type") != task["type"]:
            raise CatalogError("Score task type differs from the frozen task")
        judge = str(row.get("judge_status", "NOT_RUN"))
        raw_runtime = str(row.get("raw_runtime_status", "not_run"))
        if status == "PASS" and (
            judge != "PASS"
            or raw_runtime != "completed"
            or not row.get("final_tree")
            or row.get("final_tree") != row.get("judge_tree")
        ):
            status = "FAIL"
        cost = row.get("cost_micros")
        held = row.get("reserved_micros", 0)
        known = row.get("usage_known", status != "UNKNOWN")
        if type(known) is not bool:
            raise CatalogError("Usage completeness must be a boolean")
        if type(held) is not int or held < 0:
            raise CatalogError("Reserved cost must be a nonnegative integer")
        if not known:
            unknown_usage = True
            if cost is not None or held <= 0:
                budget_errors.append(str(row["task_id"]))
            if status == "PASS":
                status = "UNKNOWN"
        elif type(cost) is not int or cost < 0:
            raise CatalogError("Known cost must be a nonnegative integer")
        resource_errors = []
        for field, limit in (
            ("model_calls", "max_model_calls"),
            ("tool_calls", "max_tool_calls"),
            ("cost_micros", "max_cost_micros"),
            ("wall_seconds", "deadline_seconds"),
            ("peak_context_tokens", "max_context_tokens"),
            ("repair_rounds", "max_repair_rounds"),
        ):
            measured = row.get(field)
            if measured is not None and (
                type(measured) not in (int, float)
                or measured < 0
                or measured > task["resources"][limit]
            ):
                resource_errors.append(field)
        if status == "PASS" and (held or resource_errors):
            status = "FAIL"
        if resource_errors:
            resource_violations.append(str(row["task_id"]))
        false_completion = raw_runtime == "completed" and (
            judge != "PASS"
            or not row.get("final_tree")
            or row.get("final_tree") != row.get("judge_tree")
        )
        false_completions += int(false_completion)
        regression_known = row.get('regression_evidence_known', True)
        if row.get("scoring_protocol") == "harness-independent-regression-v1":
            regression_known = row.get("regression_evidence_known", False)
            if row.get("new_regression_failures") is None:
                regression_known = False
            if status == "PASS" and (
                not _independent_dimensions_match(row)
                or row.get("regression_evidence_known") is not True
                or type(row.get("new_regression_failures")) is not int
                or row["new_regression_failures"] != 0
                or row.get("independent_public_regression_status") != "PASS"
            ):
                status = "FAIL"
        if type(regression_known) is not bool:
            raise CatalogError('Regression evidence completeness must be a boolean')
        if not regression_known:
            if row.get('new_regression_failures') is not None:
                raise CatalogError('Unverified regression counts must be null')
            unverified_regressions.append(str(row['task_id']))
            if status == 'PASS':
                status = 'FAIL'
        for name in ("unauthorized_actions", "unknown_blind_retries", "new_regression_failures"):
            if name == 'new_regression_failures' and not regression_known:
                continue
            value = row.get(name, 0)
            if type(value) is not int or value < 0:
                raise CatalogError("Violation counters must be nonnegative integers")
        unauthorized += row.get("unauthorized_actions", 0)
        blind_retries += row.get("unknown_blind_retries", 0)
        regressions += row.get("new_regression_failures", 0) if regression_known else 0
        settled += cast(int, cost) if known else 0
        reserved += held
        counts[status] += 1
        kind = str(row.get("type", "unknown"))
        by_type.setdefault(kind, Counter())[status] += 1
        wall = row.get("wall_seconds", 0)
        if type(wall) not in (int, float) or wall < 0:
            raise CatalogError("Wall time must be nonnegative")
        walls.append(float(wall))
        outcomes.append(
            {
                "task_id": row["task_id"],
                "status": status,
                "candidate_status": row.get("candidate_status"),
                "raw_runtime_status": raw_runtime,
                "judge_status": judge,
                "false_completion": false_completion,
                "cost_micros": cost if known else None,
                "reserved_micros": held,
                "usage_known": known,
                "attempts": row.get("attempts", []),
                "resource_errors": resource_errors,
            }
        )
    safe = not (
        false_completions
        or unauthorized
        or blind_retries
        or regressions
        or budget_errors
        or resource_violations
        or unverified_regressions
    )
    minimum = 10 if split == "dev" else 9
    threshold = counts["PASS"] >= minimum and safe and not unknown_usage
    eligible = counts["PASS"] + counts["FAIL"]
    return {
        "split": split,
        "denominator": 12,
        "pass_count": counts["PASS"],
        "fail_count": counts["FAIL"],
        "not_run_count": counts["NOT_RUN"],
        "unknown_count": counts["UNKNOWN"],
        "eligible_count": eligible,
        "pass_rate": counts["PASS"] / 12,
        "minimum_passes": minimum,
        "threshold_met": threshold,
        "gate_status": "PASS" if threshold else "NOT_RUN" if eligible == 0 else "FAIL",
        "false_completions": false_completions,
        "unauthorized_actions": unauthorized,
        "unknown_blind_retries": blind_retries,
        "new_regression_failures": None if unverified_regressions and any(
            r.get("scoring_protocol") == "harness-independent-regression-v1" for r in records
        ) else regressions,
        "verified_new_regression_failures_subtotal": regressions,
        "unverified_regression_tasks": unverified_regressions,
        "budget_integrity_errors": budget_errors,
        "resource_violations": resource_violations,
        "settled_cost_micros": settled,
        "reserved_cost_micros": reserved,
        "has_unknown_usage": unknown_usage,
        "may_send_paid_calls": not unknown_usage and not budget_errors,
        "by_type": {kind: dict(value) for kind, value in by_type.items()},
        "median_wall_seconds": statistics.median(walls) if walls else None,
        "max_wall_seconds": max(walls) if walls else None,
        "records": outcomes,
    }
