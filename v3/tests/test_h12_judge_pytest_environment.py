"""Exercise verifier phases with a real readonly sandbox and strict pytest options."""

from pathlib import Path

import pytest

from mgar_v3.quality_eval import h12_judge as judge

LOCK = Path(__file__).resolve().parents[1] / ".local/h12/sandbox-lock.json"


@pytest.mark.parametrize(
    "phase,defect,expected",
    [
        ("environment", "", "PASS"),
        ("baseline", "", "PASS"),
        ("regression", "", "PASS"),
        ("final", "", "PASS"),
        ("final", 'assert False, "real behavioral failure"', "FAIL"),
        ("final", 'warnings.warn("real project warning", UserWarning)', "FAIL"),
    ],
)
def test_verifier_preserves_pytest_cache_config_and_behavior(
    tmp_path, monkeypatch, phase, defect, expected
):
    workspace = tmp_path / "project"
    (workspace / "tests").mkdir(parents=True)
    (workspace / "pytest.ini").write_text(
        "[pytest]\nfilterwarnings = error\ncache_dir = .project-cache\n"
    )
    (workspace / "tests/test_cache.py").write_text(
        "import warnings\nfrom pathlib import Path\nimport pytest\n"
        "def test_selected(pytestconfig):\n"
        '    assert pytestconfig.getini("cache_dir") == "/scratch/pytest-cache"\n'
        '    pytestconfig.cache.set("test/value", 17)\n'
        '    assert pytestconfig.cache.get("test/value", None) == 17\n'
        "    with pytest.raises(OSError):\n"
        '        Path("/workspace/forbidden.txt").write_text("no")\n'
        + ("    " + defect + "\n" if defect else "")
        + 'def test_unselected():\n    assert False, "selection must be retained"\n'
    )
    command = ["python3", "-m", "pytest", "tests/", "-k", "selected and not unselected"]
    monkeypatch.setattr(
        judge,
        "prepare_judge",
        lambda *args, **kwargs: {
            "commands": [judge._normalize(command.copy())],
            "files": [],
            "baseline_oracle": False,
        },
    )
    task = {
        "task_id": "synthetic-pytest-options",
        "split": "dev",
        "type": "feature",
        "repository": "synthetic/options",
        "language": "python",
        "judge_sha256": "synthetic",
        "environment": {"verify": []},
        "acceptance": {"regression": ["python3 -m pytest tests/ -k 'selected and not unselected'"]},
    }
    before = judge.tree_hash(workspace)
    evidence = tmp_path / "evidence"
    result = judge.verify(
        workspace,
        task,
        LOCK,
        evidence,
        phase=phase,
        expected_tree=before,
        sealed=True,
        frozen_tests=judge.test_hashes(workspace),
        environment_revision="h12-dev-environment-v4",
    )
    logs = "\n".join(Path(r["private_log"]).read_text() for r in result["receipts"])
    assert result["status"] == expected, (result["reason"], logs)
    assert judge.tree_hash(workspace) == before
    assert not (workspace / ".project-cache").exists()
    assert not (workspace / ".pytest_cache").exists()
    assert not (workspace / "forbidden.txt").exists()
    assert all(r["provenance"]["write_paths"] == [] for r in result["receipts"])
    assert "Unknown config option" not in logs
    if expected == "PASS":
        assert "1 passed" in logs and "1 deselected" in logs
    else:
        assert (
            "1 failed" in logs
            and ("real behavioral failure" if "assert" in defect else "real project warning")
            in logs
        )


@pytest.mark.parametrize("broken_verifier", [False, True])
def test_public_preparation_exercises_the_actual_verifier_command_without_private_assets(
    tmp_path, monkeypatch, broken_verifier
):
    from mgar_v3.quality_eval.h12_full_dev_readiness import execute_task, task_binding
    from mgar_v3.quality_eval.h12_runner import DEV_ENVIRONMENT_V4, profile_for

    workspace = tmp_path / "project"
    (workspace / "tests").mkdir(parents=True)
    (workspace / "pytest.ini").write_text("[pytest]\nfilterwarnings = error\n")
    (workspace / "tests/test_public.py").write_text(
        "def test_public(pytestconfig):\n"
        '    assert pytestconfig.getini("cache_dir") == "/scratch/pytest-cache"\n'
    )
    for parent in (workspace, tmp_path, tmp_path.parent):
        parent.chmod(0o755)
    task = {
        "task_id": "synthetic-verifier-preparation",
        "split": "dev",
        "type": "feature",
        "repository": "synthetic/options",
        "language": "python",
        "base_commit": "a" * 40,
        "goal": "Extend the public API while keeping original behavior.",
        "scope": ["source.py"],
        "resources": {
            "max_model_calls": 20,
            "max_tool_calls": 80,
            "max_cost_micros": 500000,
            "deadline_seconds": 60,
            "max_context_tokens": 32000,
            "max_repair_rounds": 6,
        },
        "environment": {"runtime": "python-3.12", "verify": ["python3 -m pytest tests/"]},
        "acceptance": {"regression": ["python3 -m pytest tests/"]},
    }
    original_normalize = judge._normalize
    if broken_verifier:

        def broken(argv):
            result = original_normalize(argv)
            return [*result, "-p", "no:cacheprovider"]

        monkeypatch.setattr(judge, "_normalize", broken)
    monkeypatch.setattr(
        judge,
        "prepare_judge",
        lambda *args, **kwargs: pytest.fail("Public preparation must not read private judges"),
    )
    profile = profile_for(task, "toy", "candidate", environment_revision=DEV_ENVIRONMENT_V4)
    binding = task_binding(task, profile, workspace, LOCK, DEV_ENVIRONMENT_V4, None)
    verifier_roles = [
        c["role"] for c in binding["commands"] if c["role"].startswith("verifier_public_")
    ]
    assert len(verifier_roles) == 1
    result = execute_task(workspace, binding=binding, public=None, evidence=tmp_path / "proof")
    assert result["command_facts"]["public_01"]["status"] == "passed"
    assert result["status"] == ("NOT_RUN" if broken_verifier else "READY"), result
    if broken_verifier:
        assert result["blockers"] == ["PUBLIC_COMMAND_NOT_READY:" + verifier_roles[0]]
    assert result["original_tree_before"] == result["original_tree_after"]
