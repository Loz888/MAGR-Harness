"""Pure judge-sealing and integrity regressions; no real hidden source is read."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from mgar_v3.quality_eval import h12_judge as judge
from mgar_v3.quality_eval.h12 import load_frozen_tasks
from mgar_v3.quality_eval.harness_v4 import CatalogError, judge_sha256, repo_root

ROOT = repo_root()


def _task(task_id: str = "hv4-dev-requests-http-url-helper") -> dict[str, Any]:
    return next(t for t in load_frozen_tasks("dev", ROOT) if t["task_id"] == task_id)


def _workspace(tmp_path: Path) -> Path:
    root = tmp_path / "model-workspace"
    (root / "tests").mkdir(parents=True)
    (root / "source.py").write_text("value = 1\n")
    (root / "tests/test_original.py").write_text("def test_original():\n    assert True\n")
    return root


def test_nine_additional_implementations_bind_original_frozen_judge_hashes(tmp_path: Path) -> None:
    tasks = [t for t in load_frozen_tasks("dev", ROOT) if t["judge_status"] != "implemented"]
    assert len(tasks) == 9
    for task in tasks:
        bundle = judge.prepare_judge(task, tmp_path / task["task_id"])
        assert bundle["judge_sha256"] == task["judge_sha256"]
        assert bundle["implementation_manifest_sha256"] == judge._MANIFEST_SHA256
        for item in bundle["files"]:
            assert hashlib.sha256(Path(item["file"]).read_bytes()).hexdigest() == item["sha256"]
        assert bundle["commands"]


def test_binding_does_not_accept_a_different_original_specification(tmp_path: Path) -> None:
    task = _task()
    changed = {**task, "judge": {**task["judge"], "pass_rule": "weaker substitute"}}
    changed["judge_sha256"] = judge_sha256(changed)
    with pytest.raises(CatalogError, match="another frozen specification"):
        judge.prepare_judge(changed, tmp_path / "private")


def test_modified_implementation_manifest_is_rejected(tmp_path: Path) -> None:
    directory = tmp_path / "experiments/harness-v4/judges/dev"
    directory.mkdir(parents=True)
    (directory / "manifest.json").write_text('{"tasks": []}')
    with pytest.raises(CatalogError, match="manifest changed"):
        judge.implementation_status(_task(), tmp_path)


def test_old_reference_reader_checks_hash_at_runtime_using_only_synthetic_hidden_file(
    tmp_path: Path,
) -> None:
    source = tmp_path / "experiments/v35-harness/dev/verification/synthetic.py"
    source.parent.mkdir(parents=True)
    source.write_text("# synthetic hidden content, not a real benchmark answer\n")
    task = {
        "task_id": "synthetic-reference",
        "type": "bugfix",
        "judge_status": "implemented",
        "judge": {
            "kind": "v35_reference",
            "version": "synthetic",
            "pass_rule": "synthetic",
            "hidden_tests": [
                {
                    "path": source.relative_to(tmp_path).as_posix(),
                    "sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
                }
            ],
        },
        "environment": {"verify": ["python -m pytest verification/synthetic.py"]},
        "acceptance": {"regression": ["python -m pytest tests/"]},
    }
    task["judge_sha256"] = judge_sha256(task)
    bundle = judge.prepare_judge(task, tmp_path / "private", root=tmp_path)
    assert bundle["files"][0]["target"] == "verification/synthetic.py"
    source.write_text("tampered synthetic content\n")
    with pytest.raises(CatalogError, match="hash does not match"):
        judge.prepare_judge(task, tmp_path / "private-second", root=tmp_path)


def test_final_refuses_unsealed_session_before_creating_or_running_a_judge(tmp_path: Path) -> None:
    result = judge.verify(tmp_path / "absent", _task(), tmp_path / "lock", tmp_path / "evidence")
    assert result == {"status": "NOT_RUN", "reason": "MODEL_SESSION_NOT_SEALED"}
    assert not (tmp_path / "evidence").exists()


def test_final_requires_original_test_hashes(tmp_path: Path) -> None:
    result = judge.verify(
        tmp_path / "absent", _task(), tmp_path / "lock", tmp_path / "evidence", sealed=True
    )
    assert result["reason"] == "ORIGINAL_TEST_HASHES_REQUIRED"


@pytest.mark.parametrize("mutation", ["edit", "delete"])
def test_modifying_or_deleting_an_original_test_never_reaches_execution(
    tmp_path: Path,
    mutation: str,
) -> None:
    workspace = _workspace(tmp_path)
    hashes = judge.test_hashes(workspace)
    original = workspace / "tests/test_original.py"
    if mutation == "edit":
        original.write_text("def test_original():\n    pass\n")
    else:
        original.unlink()
    result = judge.verify(
        workspace,
        _task(),
        tmp_path / "lock",
        tmp_path / "evidence",
        sealed=True,
        frozen_tests=hashes,
    )
    assert result["status"] == "FAIL"
    assert result["reason"] == "FROZEN_TEST_MODIFIED"


def test_changing_npm_test_script_does_not_hide_existing_regressions(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    package = workspace / "package.json"
    package.write_text(json.dumps({"scripts": {"test": "real test", "selfcheck": "old"}}))
    frozen = judge.test_hashes(workspace)
    package.write_text(json.dumps({"scripts": {"test": "echo skipped", "selfcheck": "new"}}))
    result = judge.verify(
        workspace,
        _task(),
        tmp_path / "lock",
        tmp_path / "evidence",
        sealed=True,
        frozen_tests=frozen,
    )
    assert result["reason"] == "FROZEN_TEST_MODIFIED"


def test_expected_tree_guard_rejects_a_different_model_tree(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    original = judge.tree_hash(workspace)
    (workspace / "source.py").write_text("value = 2\n")
    result = judge.verify(
        workspace,
        _task(),
        tmp_path / "lock",
        tmp_path / "evidence",
        sealed=True,
        frozen_tests=judge.test_hashes(workspace),
        expected_tree=original,
    )
    assert result["reason"] == "FINAL_TREE_CHANGED"


def test_refactor_without_private_pinned_oracle_cannot_self_certify(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    result = judge.verify(
        workspace,
        _task("hv4-dev-requests-encoding-refactor"),
        tmp_path / "lock",
        tmp_path / "evidence",
        sealed=True,
        frozen_tests=judge.test_hashes(workspace),
    )
    assert result["status"] == "NOT_RUN"
    assert result["reason"] == "PINNED_BASELINE_ORACLE_REQUIRED"


def _ports(monkeypatch: pytest.MonkeyPatch, effect: Any) -> list[Any]:
    captured = []

    class Broker:
        image_id = "sha256:" + "a" * 64

        def __init__(self, root: Path, lease: Any, **kwargs: Any) -> None:
            self.root = root
            self.lease = lease
            captured.append(self)

    class Session:
        def __init__(self, broker: Any) -> None:
            self.broker = broker

        def close(self) -> int:
            return 0

    def run(
        session: Any, grant: Any, argv: Any, timeout: Any, evidence: Any, index: Any,
        **receipt_binding: Any,
    ) -> Any:
        assert grant.write_paths == ()
        assert session.broker.lease.mode == "investigate"
        return effect(session.broker.root, argv, index)

    monkeypatch.setattr(judge, "IsolatedExecutionBroker", Broker)
    monkeypatch.setattr(judge, "CommandSession", Session)
    monkeypatch.setattr(judge, "_run", run)
    return captured


@pytest.mark.parametrize("which", ["original", "verifier"])
def test_tree_changes_during_readonly_verification_invalidate_successful_receipts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    which: str,
) -> None:
    workspace = _workspace(tmp_path)

    def effect(copy: Path, argv: Any, index: int) -> Any:
        if index == 0:
            (workspace if which == "original" else copy).joinpath("source.py").write_text("drift\n")
        return {
            "state": "exited",
            "exit_code": 0,
            "complete": True,
        }, "1 passed\nH12_CHECKS_PASSED:1"

    captured = _ports(monkeypatch, effect)
    result = judge.verify(
        workspace,
        _task(),
        tmp_path / "lock",
        tmp_path / "evidence",
        sealed=True,
        frozen_tests=judge.test_hashes(workspace),
    )
    assert result["status"] == "FAIL"
    assert result["reason"] == "TREE_CHANGED_DURING_JUDGING"
    assert len(captured) == 1
    assert captured[0].root != workspace
    assert not captured[0].root.exists()


def test_missing_dependencies_are_not_an_independent_pass_or_baseline_reproduction(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = _workspace(tmp_path)
    _ports(
        monkeypatch,
        lambda *args: (
            {"state": "failed", "exit_code": 1, "complete": True},
            "No module named pytest",
        ),
    )
    result = judge.verify(
        workspace,
        _task("hv4-dev-requests-content-type"),
        tmp_path / "lock",
        tmp_path / "evidence",
        phase="baseline",
    )
    assert result["status"] == "NOT_RUN"
    assert result["baseline_reproduced"] is False


@pytest.mark.parametrize("output", ["", "0 passed", "1 skipped", "success"])
def test_zero_exit_without_executed_checks_is_not_a_test_pass(output: str) -> None:
    assert judge._positive_checks("python", ["python3", "test.py"], output, 0) is False


def _external_registry(tmp_path: Path, **changes: Any) -> tuple[dict[str, Any], Path, str]:
    directory = tmp_path / "independent-private-registry"
    directory.mkdir(parents=True)
    source = directory / "probe.py"
    source.write_text("print('H12_CHECKS_PASSED:1')\n")
    task = {**_task(), "task_id": "synthetic-independent-case", "split": "holdout"}
    row = {
        "task_id": task["task_id"],
        "original_judge_sha256": task["judge_sha256"],
        "files": [
            {
                "source": "probe.py",
                "target": "verification/synthetic.py",
                "sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
                **changes,
            }
        ],
        "command": ["python3", "-B", "verification/synthetic.py"],
        "baseline_oracle": False,
    }
    path = directory / "manifest.json"
    path.write_text(json.dumps({"schema": "mgar-h12-judge-implementation-v1", "tasks": [row]}))
    return task, path, hashlib.sha256(path.read_bytes()).hexdigest()


def test_synthetic_external_registry_accepts_only_the_supplied_exact_hash(tmp_path: Path) -> None:
    task, registry, sha = _external_registry(tmp_path)
    bundle = judge.prepare_judge(
        task, tmp_path / "private", registry_path=registry, registry_sha256=sha
    )
    assert bundle["implementation_manifest_sha256"] == sha
    assert bundle["task_id"] == task["task_id"]
    assert Path(bundle["files"][0]["file"]).is_file()
    with pytest.raises(CatalogError, match="manifest changed"):
        judge.prepare_judge(
            task, tmp_path / "private-wrong", registry_path=registry, registry_sha256="0" * 64
        )


@pytest.mark.parametrize(
    "field,path",
    [
        ("source", "../escape.py"),
        ("target", "../escape.py"),
        ("target", "/tmp/escape.py"),
        ("target", "node_modules/private.py"),
    ],
)
def test_external_registry_paths_cannot_escape_or_hide_from_tree_binding(
    tmp_path: Path,
    field: str,
    path: str,
) -> None:
    task, registry, sha = _external_registry(tmp_path, **{field: path})
    with pytest.raises(CatalogError, match="relative path"):
        judge.prepare_judge(task, tmp_path / "private", registry_path=registry, registry_sha256=sha)


def test_external_registry_cannot_follow_source_symlink_outside_its_directory(
    tmp_path: Path,
) -> None:
    task, registry, _ = _external_registry(tmp_path)
    source = registry.parent / "probe.py"
    outside = tmp_path / "outside.py"
    outside.write_bytes(source.read_bytes())
    source.unlink()
    source.symlink_to(outside)
    sha = hashlib.sha256(registry.read_bytes()).hexdigest()
    with pytest.raises(CatalogError, match="source escapes"):
        judge.prepare_judge(task, tmp_path / "private", registry_path=registry, registry_sha256=sha)


def test_external_registry_cannot_follow_target_symlink_to_original_workspace(
    tmp_path: Path,
) -> None:
    task, registry, sha = _external_registry(tmp_path)
    destination = tmp_path / "private"
    outside = tmp_path / "model-workspace"
    destination.mkdir()
    outside.mkdir()
    (destination / "verification").symlink_to(outside, target_is_directory=True)
    with pytest.raises(CatalogError, match="target escapes"):
        judge.prepare_judge(task, destination, registry_path=registry, registry_sha256=sha)
    assert list(outside.iterdir()) == []


def test_source_symlink_refuses_verification_before_hidden_materialization(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    (workspace / "unsafe").symlink_to(outside, target_is_directory=True)
    result = judge.verify(
        workspace,
        _task(),
        tmp_path / "lock",
        tmp_path / "evidence",
        sealed=True,
        frozen_tests=judge.test_hashes(workspace),
    )
    assert result["status"] == "FAIL"
    assert result["reason"] == "SOURCE_SYMLINK_DENIED"
    assert list(outside.iterdir()) == []


def test_environment_checks_existing_public_regressions_without_goal_or_private_commands(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = _workspace(tmp_path)
    commands = []

    def effect(copy: Path, argv: list[str], index: int) -> Any:
        assert not (copy / "tests/hidden/test_is_http_url.py").exists()
        commands.append(argv)
        return {"state": "exited", "exit_code": 0, "complete": True}, "3 passed"

    _ports(monkeypatch, effect)
    result = judge.verify(
        workspace,
        _task(),
        tmp_path / "lock",
        tmp_path / "environment",
        phase="environment",
    )
    assert result["status"] == "PASS"
    assert result["reason"] == "PUBLIC_ENVIRONMENT_READY"
    assert len(commands) == 1 and commands[0][-1] == "tests/"
    assert not any("hidden" in arg for command in commands for arg in command)


@pytest.mark.parametrize(
    "output",
    [
        "No module named pytest_httpbin",
        "ERROR collecting tests/test_base.py; 1 failed",
        "fixture 'httpbin' not found; 1 failed",
    ],
)
def test_environment_dependency_failures_never_count_as_a_reproduced_defect(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    output: str,
) -> None:
    workspace = _workspace(tmp_path)
    _ports(
        monkeypatch,
        lambda *args: (
            {"state": "failed", "exit_code": 1, "complete": True},
            output,
        ),
    )
    result = judge.verify(
        workspace,
        _task("hv4-dev-requests-content-type"),
        tmp_path / "lock",
        tmp_path / "environment",
        phase="environment",
    )
    assert result["status"] == "NOT_RUN"
    assert result["reason"] == "DEPENDENCY_OR_LOG_UNAVAILABLE"


@pytest.mark.parametrize("kind", ["bugfix", "feature"])
def test_environment_retains_executed_original_assertion_failures_without_calling_them_green(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    kind: str,
) -> None:
    workspace = _workspace(tmp_path)
    task = {**_task(), "type": kind}
    _ports(
        monkeypatch,
        lambda *args: (
            {"state": "failed", "exit_code": 1, "complete": True},
            "1 failed, 3 passed",
        ),
    )
    result = judge.verify(
        workspace,
        task,
        tmp_path / "lock",
        tmp_path / "environment",
        phase="environment",
    )
    assert result["status"] == "PASS"
    assert result["baseline_test_status"] == "FAIL"


def test_missing_judge_is_detected_without_starting_any_environment_process(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = _workspace(tmp_path)

    def missing(*args: Any, **kwargs: Any) -> Any:
        raise CatalogError("Independent judge missing")

    monkeypatch.setattr(judge, "prepare_judge", missing)
    monkeypatch.setattr(judge, "IsolatedExecutionBroker", lambda *a, **k: pytest.fail("ran broker"))
    result = judge.verify(
        workspace,
        _task(),
        tmp_path / "lock",
        tmp_path / "environment",
        phase="environment",
    )
    assert result["status"] == "NOT_RUN"


def test_additional_registry_supplements_original_hash_bound_h00_checks(tmp_path: Path) -> None:
    task, registry, _ = _external_registry(tmp_path)
    original = tmp_path / "experiments/v35-harness/holdout/verification/synthetic_original.py"
    original.parent.mkdir(parents=True)
    original.write_text("# synthetic original frozen check\n")
    task["judge"] = {
        **task["judge"],
        "hidden_tests": [
            {
                "path": str(original.relative_to(tmp_path)),
                "sha256": hashlib.sha256(original.read_bytes()).hexdigest(),
            }
        ],
    }
    task["judge_sha256"] = judge_sha256(task)
    task["environment"] = {"verify": ["python -m pytest verification/synthetic_original.py"]}
    document = json.loads(registry.read_text())
    document["tasks"][0]["original_judge_sha256"] = task["judge_sha256"]
    registry.write_text(json.dumps(document))
    bundle = judge.prepare_judge(
        task,
        tmp_path / "private-combined",
        root=tmp_path,
        registry_path=registry,
        registry_sha256=hashlib.sha256(registry.read_bytes()).hexdigest(),
    )
    assert {item["target"] for item in bundle["files"]} == {
        "verification/synthetic.py",
        "verification/synthetic_original.py",
    }
    assert len(bundle["commands"]) == 2


def test_final_public_regression_uses_separate_copy_without_private_test_files(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = _workspace(tmp_path)
    private_roots = []
    public_roots = []

    def effect(copy: Path, argv: list[str], index: int) -> Any:
        if argv[-1] == "tests/":
            public_roots.append(copy)
            assert not (copy / "tests/hidden/test_is_http_url.py").exists()
        else:
            private_roots.append(copy)
            assert (copy / "tests/hidden/test_is_http_url.py").is_file()
        return {"state": "exited", "exit_code": 0, "complete": True}, "1 passed"

    _ports(monkeypatch, effect)
    result = judge.verify(
        workspace,
        _task(),
        tmp_path / "lock",
        tmp_path / "final",
        sealed=True,
        frozen_tests=judge.test_hashes(workspace),
    )
    assert result["status"] == "PASS"
    assert len(private_roots) == len(public_roots) == 1
    assert private_roots[0] != public_roots[0]
    assert all(not path.exists() for path in [*private_roots, *public_roots])


def test_offline_node_entrypoints_and_go_parallelism_have_recorded_normalization() -> None:
    assert judge._normalize(["npx", "mocha", "test/"]) == [
        "node",
        "/node_modules/mocha/bin/mocha.js",
        "test/",
    ]
    assert judge._normalize(["node", "node_modules/ava/entrypoints/cli.mjs"]) == [
        "node",
        "/node_modules/ava/entrypoints/cli.mjs",
    ]
    assert judge._normalize(["go", "test", "./..."]) == [
        "go",
        "test",
        "-p",
        "1",
        "-v",
        "./...",
    ]


@pytest.mark.parametrize("exit_code,summary", [(0, "3 passing"), (1, "4 failing")])
def test_expected_syntax_error_text_is_not_a_missing_environment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    exit_code: int,
    summary: str,
) -> None:
    workspace = _workspace(tmp_path)
    task = _task("hv4-dev-express-healthz")
    _ports(
        monkeypatch,
        lambda *args: (
            {
                "state": "exited" if not exit_code else "failed",
                "exit_code": exit_code,
                "complete": True,
            },
            "Expected SyntaxError: Unexpected token in invalid JSON test\n" + summary,
        ),
    )
    result = judge.verify(
        workspace,
        task,
        tmp_path / "lock",
        tmp_path / "environment",
        phase="environment",
    )
    assert result["status"] == "PASS"
    assert result["baseline_test_status"] == ("FAIL" if exit_code else "PASS")


@pytest.mark.parametrize(
    "message",
    [
        "Error: EMFILE: too many open files",
        "compile: resource temporarily unavailable",
    ],
)
def test_environment_resource_failures_are_explicitly_not_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    message: str,
) -> None:
    workspace = _workspace(tmp_path)
    _ports(
        monkeypatch,
        lambda *args: (
            {"state": "failed", "exit_code": 1, "complete": True},
            message,
        ),
    )
    result = judge.verify(
        workspace,
        _task(),
        tmp_path / "lock",
        tmp_path / "environment",
        phase="environment",
    )
    assert result["status"] == "NOT_RUN"
    assert result["reason"] == "SANDBOX_RESOURCE_UNAVAILABLE"


def test_reference_baseline_runs_real_public_assertion_without_collecting_future_hidden_symbol(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = _workspace(tmp_path)
    (workspace / "tests/test_public.py").write_text(
        "def test_existing_defect():\n    assert 1 == 2\n"
    )
    (workspace / "synthetic_module.py").write_text("existing_symbol = 1\n")
    hidden = tmp_path / "frozen_hidden.py"
    hidden.write_text("from synthetic_module import future_symbol\n")
    task = {
        "task_id": "synthetic-reference-baseline",
        "type": "bugfix",
        "language": "python",
        "repository": "synthetic/project",
        "judge_sha256": "a" * 64,
        "judge": {"kind": "v35_reference"},
        "environment": {
            "verify": [
                "python -m pytest tests/test_public.py verification/test_hidden.py",
            ]
        },
        "acceptance": {"regression": ["python -m pytest tests/test_public.py"]},
    }
    bundle = {
        "files": [
            {
                "file": str(hidden),
                "target": "verification/test_hidden.py",
                "sha256": hashlib.sha256(hidden.read_bytes()).hexdigest(),
            }
        ],
        "commands": [
            judge._normalize(
                [
                    "python",
                    "-m",
                    "pytest",
                    "tests/test_public.py",
                    "verification/test_hidden.py",
                ]
            )
        ],
        "regression_commands": [],
        "baseline_oracle": False,
    }
    monkeypatch.setattr(judge, "prepare_judge", lambda *a, **k: bundle)
    selections = []

    def execute(copy: Path, argv: list[str], index: int) -> Any:
        # The interpreter's bootstrap is followed by the original selectors.
        selectors = argv[argv.index("-c") + 2 :]
        selections.append(selectors)
        result = subprocess.run(
            [sys.executable, "-B", "-m", "pytest", "-p", "no:cacheprovider", *selectors],
            cwd=copy,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
            env={**os.environ, "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1", "PYTHONPATH": str(copy)},
        )
        return {
            "state": "failed" if result.returncode else "exited",
            "exit_code": result.returncode,
            "complete": True,
        }, result.stdout + result.stderr

    _ports(monkeypatch, execute)
    baseline = judge.verify(
        workspace,
        task,
        tmp_path / "lock",
        tmp_path / "baseline",
        phase="baseline",
    )
    assert baseline["status"] == "PASS"
    assert baseline["baseline_reproduced"] is True
    assert baseline["baseline_method"] == "frozen_public_reference"
    assert selections == [["tests/test_public.py"]]
    final = judge.verify(
        workspace,
        task,
        tmp_path / "lock",
        tmp_path / "final-reference",
        sealed=True,
        frozen_tests=judge.test_hashes(workspace),
    )
    assert final["status"] != "PASS"
    assert selections[-1] == ["tests/test_public.py", "verification/test_hidden.py"]
    assert hidden.read_text() == "from synthetic_module import future_symbol\n"
