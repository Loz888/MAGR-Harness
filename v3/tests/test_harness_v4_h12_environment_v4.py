"""New environment authority stays explicit and public execution plans fail closed."""

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from mgar_v3.domain import DomainError
from mgar_v3.quality_eval import h12
from mgar_v3.quality_eval import h12_readiness as readiness
from mgar_v3.quality_eval.h12_execution_plan import application_plan
from mgar_v3.quality_eval.h12_runner import (
    DEV_ENVIRONMENT_V3,
    DEV_ENVIRONMENT_V4,
    HOLDOUT_INFRA_V4,
    EvaluationRunner,
    profile_for,
)
from mgar_v3.quality_eval.harness_v4 import CatalogError


def test_only_explicit_new_revision_grants_requests_timeout_namespace() -> None:
    task = next(t for t in h12.load_frozen_tasks("dev") if t["repository"] == "psf/requests")
    old = profile_for(task, "old", "candidate", environment_revision=DEV_ENVIRONMENT_V3)
    assert "sandbox_offline_network" not in old.model_dump(mode="json")
    new = profile_for(task, "new", "candidate", environment_revision=DEV_ENVIRONMENT_V4)
    assert new.sandbox_offline_network == "connect-timeout-v1"
    assert new.resources == old.resources
    with pytest.raises(CatalogError, match="quarantined"):
        profile_for(
            {**task, "split": "holdout"},
            "bad",
            "candidate",
            environment_revision=DEV_ENVIRONMENT_V4,
        )


@pytest.mark.parametrize("paid", [False, True])
def test_holdout_infrastructure_cannot_create_evaluation_runner(tmp_path: Path, paid: bool) -> None:
    with pytest.raises(CatalogError):
        EvaluationRunner(
            SimpleNamespace(harness=None),
            experiment_id="blocked",
            evidence=tmp_path / "eval",
            image_locks={},
            api_key=None,
            execute_live=paid,
            environment_revision=HOLDOUT_INFRA_V4,
        )


@pytest.mark.parametrize("command", ["python app.py --selfcheck", "python3 -B app.py --selfcheck"])
def test_public_oneshot_command_builds_exact_scoped_plan(command: str) -> None:
    task = {"language": "python", "environment": {"verify": [command]}}
    safe = {"goal": "Run the one-shot CLI selfcheck and exit 0.", "scope": ["app.py"]}
    assert application_plan(task, safe)[:2] == (
        ("python3", "-B", "app.py", "--selfcheck"),
        "oneshot",
    )


@pytest.mark.parametrize(
    "commands",
    [
        ["python other.py"],
        ["python -c 'print(1)'"],
        ["python ../app.py"],
        ["python app.py", "python other.py"],
        ["python tests/check.py"],
        ["python app.py && python other.py"],
    ],
)
def test_unscoped_ambiguous_or_shell_plan_is_unavailable(commands: list[str]) -> None:
    task = {"language": "python", "environment": {"verify": commands}}
    safe = {"goal": "Run the CLI and exit 0.", "scope": ["app.py", "other.py", "tests/check.py"]}
    # The first case deliberately has only app.py in the authorized scope.
    if commands == ["python other.py"]:
        safe["scope"] = ["app.py"]
    assert application_plan(task, safe)[0] == ()


def test_materialization_exception_cleans_only_its_owned_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    owned = tmp_path / "owned"
    owned.mkdir()
    sentinel = tmp_path / "other-owner"
    sentinel.mkdir()
    runner = EvaluationRunner(
        SimpleNamespace(harness=None),
        experiment_id="test",
        evidence=tmp_path / "evidence",
        image_locks={},
        api_key=None,
    )
    monkeypatch.setattr("mgar_v3.quality_eval.h12_runner.tempfile.mkdtemp", lambda **_: str(owned))

    def fail(*args: object) -> Path:
        (owned / "partial").write_text("partial")
        raise CatalogError("Frozen public asset mismatch")

    monkeypatch.setattr(runner, "_materialize_into", fail)
    with pytest.raises(CatalogError):
        runner._materialize({}, tmp_path)
    assert not owned.exists()
    assert sentinel.is_dir()


def test_public_reference_for_other_commit_never_gets_transplanted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "app.py").write_text("print('original')\n")
    runner = EvaluationRunner(
        SimpleNamespace(harness=None),
        experiment_id="binding",
        evidence=tmp_path / "evidence",
        image_locks={},
        api_key=None,
    )
    monkeypatch.setattr(
        h12,
        "_public_v35_metadata",
        lambda *_: [
            {"task_id": "public-ref", "repository": "synthetic/repo", "base_commit": "b" * 40}
        ],
    )
    task = {
        "base_commit": "a" * 40,
        "repository": "synthetic/repo",
        "split": "dev",
        "judge": {"reference_task": "public-ref"},
    }
    with pytest.raises(DomainError) as caught:
        runner._materialize(task, source)
    assert caught.value.code == "H12_PUBLIC_TEST_SOURCE_BINDING_CONFLICT"
    assert (source / "app.py").read_text() == "print('original')\n"


def test_scoped_public_npm_oneshot_is_a_fixed_plan_even_before_implementation() -> None:
    task = {"language": "javascript", "environment": {"verify": ["npm run selfcheck"]}}
    safe = {
        "goal": "Run a selfcheck on an ephemeral port; it exits 0.",
        "scope": ["scripts/selfcheck.mjs", "package.json"],
    }
    assert application_plan(task, safe)[:2] == (("npm", "run", "selfcheck"), "oneshot")
    assert application_plan(task, {**safe, "scope": ["scripts/selfcheck.mjs"]})[0] == ()


def test_go_compile_revision_changes_sandbox_only_and_preserves_old_profile() -> None:
    task = next(t for t in h12.load_frozen_tasks("dev") if t["language"] == "go")
    old = profile_for(task, "same", "candidate", environment_revision=DEV_ENVIRONMENT_V3)
    new = profile_for(task, "same", "candidate", environment_revision=DEV_ENVIRONMENT_V4)
    assert old.sandbox_memory_mb == 256 and new.sandbox_memory_mb == 512
    assert old.resources == new.resources


@pytest.mark.parametrize("route", ["/healthz.json", "/ready?check=1", "`/v1/status.json`"])
def test_http_health_path_is_not_silently_truncated(route: str) -> None:
    task = {"language": "python"}
    safe = {"goal": f"Run on port 4321 serving GET {route}.", "scope": ["app.py"]}
    assert application_plan(task, safe)[3] == route.strip("`")


@pytest.mark.parametrize(
    "repository,language,declared,observed,allowed",
    [
        ("synthetic/python", "python", "python-3.12", "Python 2.7.18", False),
        ("synthetic/node", "javascript", "node-20", "v18.10.0", False),
        ("expressjs/express", "javascript", "node-20", "v18.10.0", True),
        ("expressjs/express", "javascript", "node-20", "v16.17.1", False),
    ],
)
def test_toolchain_revision_only_accepts_the_explicit_compatibility_rule(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    repository: str,
    language: str,
    declared: str,
    observed: str,
    allowed: bool,
) -> None:
    key = "python_version" if language == "python" else "node_version"
    lock = tmp_path / "lock.json"
    lock.write_text(json.dumps({key: observed, "image_id": "sha256:" + "1" * 64}))
    monkeypatch.setattr(readiness, "IsolatedExecutionBroker", lambda *a, **k: object())
    monkeypatch.setattr(readiness, "CommandSession", lambda *a: SimpleNamespace(close=lambda: None))
    monkeypatch.setattr(
        readiness.h12_judge,
        "_run",
        lambda *a: (
            {"exit_code": 0, "complete": True},
            observed + "\n",
        ),
    )
    task = {"repository": repository, "language": language, "environment": {"runtime": declared}}
    result = readiness.runtime_probe(
        tmp_path, task, lock, tmp_path / "probe", environment_revision=DEV_ENVIRONMENT_V4
    )
    assert (result["status"] == "PASS") is allowed
    assert result["declared_runtime_matches"] is False
