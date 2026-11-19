"""Narrow modify/test leases keep pytest cache on sandbox scratch, with PG authority."""

from functools import partial
from pathlib import Path

import test_h12_public_probe_service as probes

from mgar_v3.harness.contracts import EvaluationProfile

LOCK = Path(__file__).resolve().parents[1] / ".local/h12/sandbox-lock.json"
REGRESSION = ("python3", "-B", "-m", "pytest", "-q", "test_smoke.py")


def test_real_narrow_scope_service_pytest_cache_completes_red_green(engine, tmp_path, monkeypatch):
    monkeypatch.setattr(probes, "IMAGE_LOCK", LOCK)
    monkeypatch.setattr(
        probes, "EvaluationProfile", partial(EvaluationProfile, sandbox_scratch_mb=256)
    )
    case = probes._case(engine, tmp_path, regression=REGRESSION)
    (case.source / "pytest.ini").write_text("[pytest]\nfilterwarnings = error\n")
    # Assert actual configuration without configuring/repairing the cache in
    # the fixture. Autouse adds no test and applies to the existing Smoke test.
    (case.source / "conftest.py").write_text(
        "import pytest\n@pytest.fixture(autouse=True)\n"
        "def require_scratch_cache(pytestconfig):\n"
        "    assert str(pytestconfig.cache._cachedir) == '/scratch/pytest-cache'\n"
    )
    case.model.steps = probes._successful_steps()
    service = case.service()
    result = service.create_task(case.request(service))
    candidate = probes._candidate(case, result)
    assert candidate["status"] == "completed", candidate
    suites = [r for r in candidate["receipts"] if tuple(r["argv"]) == REGRESSION]
    public = [r for r in candidate["receipts"] if tuple(r["argv"]) == case.plan.argv]
    assert len(suites) == 2
    assert all(
        r["status"] == "passed" and r["exit_code"] == 0 and r["tests_run"] == 1 for r in suites
    )
    assert all(
        "1 passed" in r["stdout"] and "PytestCacheWarning" not in r["stdout"] for r in suites
    )
    assert [r["public_probe"]["outcome"] for r in public] == ["red", "green"]
    row = engine.harness.get(result["task_id"])
    assert row["grant_scope"]["write_paths"] == ["calc.py"]
    assert row["evaluation_profile"]["sandbox_scratch_mb"] == 256
    assert candidate["model_calls"] == 5 and candidate["tool_calls"] == 6
    assert result["tests"]["run"] in {1, 2}
    workspace = Path(row["request"]["workspace_root"])
    assert not (workspace / ".pytest_cache").exists()
    assert not any(workspace.glob("pytest-cache-files-*"))
