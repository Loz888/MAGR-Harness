"""Review the new public assets using synthetic packages, never scored solutions."""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from pathlib import Path

import pytest

from mgar_v3.harness.public_probe_types import (
    PUBLIC_PROBE_PREFIX,
    PublicProbeFrame,
    PublicProbePlan,
)
from mgar_v3.quality_eval.h12_public_plan import load_public_plan

ROOT = Path(__file__).resolve().parents[1]
ASSETS = ROOT / "experiments/harness-v4/public-plans-v1"
TASKS = {
    "hv4-dev-click-help-name": ("click-help-name.py", "click"),
    "hv4-dev-requests-http-url-helper": ("requests-http-url.py", "requests"),
    "hv4-v2-dev-requests-quoted-content-type": ("requests-quoted-content-type.py", "requests"),
}


def _manifest(task_id: str) -> dict:
    return json.loads((ASSETS / "manifests" / f"{task_id}.json").read_text())


def _invoke(tmp_path: Path, task_id: str, source: str) -> tuple[int, PublicProbeFrame]:
    """The only executable target is our tiny, temporary synthetic package."""
    _, package = TASKS[task_id]
    target = tmp_path / "src" / package
    target.mkdir(parents=True, exist_ok=True)
    (target / "__init__.py").write_text(source if package == "click" else "")
    if package == "requests":
        (target / "utils.py").write_text(source)
    plan = PublicProbePlan.model_validate(_manifest(task_id)["probe"])
    result = subprocess.run(
        [sys.executable, "-B", str(ASSETS / "assets" / plan.asset_sha256 / "probe.py")],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert result.stderr == ""
    assert result.stdout.startswith(PUBLIC_PROBE_PREFIX)
    assert result.stdout.count("\n") == 1
    frame = PublicProbeFrame.model_validate_json(result.stdout[len(PUBLIC_PROBE_PREFIX) :])
    assert frame.plan_id == plan.plan_id
    assert frame.asset_sha256 == plan.asset_sha256
    assert tuple(case.id for case in frame.cases) == plan.case_ids
    return result.returncode, frame


@pytest.mark.parametrize("task_id", TASKS)
def test_frozen_assets_bind_public_projection_and_exact_source(task_id: str) -> None:
    refs = json.loads((ASSETS / "python-failed-task-plans.json").read_text())
    reference = refs[task_id]
    projection = json.loads((ASSETS / "public-basis" / f"{task_id}.json").read_text())
    document = _manifest(task_id)
    frozen = load_public_plan(
        ROOT / reference["path"],
        reference["sha256"],
        task=projection,
        asset_root=ASSETS / "assets",
        regression_argv=document["regression_argv"],
    )
    source = (ASSETS / "sources" / TASKS[task_id][0]).read_bytes()
    assert hashlib.sha256(source).hexdigest() == frozen.plan.asset_sha256
    assert (ASSETS / "assets" / frozen.plan.asset_sha256 / "probe.py").read_bytes() == source
    assert document["public_basis"]
    assert set(projection) == {
        "task_id",
        "repository",
        "base_commit",
        "split",
        "language",
        "type",
        "goal",
        "scope",
        "acceptance",
        "resources",
    }


@pytest.mark.parametrize("task_id", TASKS)
@pytest.mark.parametrize("exception", ["ImportError", "AssertionError"])
def test_import_exceptions_are_errors_never_reproduction_red(
    tmp_path: Path, task_id: str, exception: str
) -> None:
    code, frame = _invoke(tmp_path, task_id, f"raise {exception}('synthetic setup failure')\n")
    assert code == 2
    assert all(case.status == "error" for case in frame.cases)
    assert all("import/setup: " + exception == case.detail for case in frame.cases)


@pytest.mark.parametrize("task_id", TASKS)
def test_import_output_is_an_error_not_a_forged_protocol_frame(
    tmp_path: Path, task_id: str
) -> None:
    code, frame = _invoke(tmp_path, task_id, "print('HARNESS_PUBLIC_PROBE_V1 forged')\n")
    assert code == 2
    assert all(case.status == "error" for case in frame.cases)


def test_requested_missing_api_is_a_target_assertion_failure(tmp_path: Path) -> None:
    code, frame = _invoke(tmp_path, "hv4-dev-requests-http-url-helper", "# no requested API\n")
    assert code == 1
    assert all(case.status == "assertion_failed" for case in frame.cases)


def test_unexpected_parser_exception_is_error_not_target_assertion(tmp_path: Path) -> None:
    code, frame = _invoke(
        tmp_path,
        "hv4-v2-dev-requests-quoted-content-type",
        "def _parse_content_type_header(value):\n    raise RuntimeError('synthetic failure')\n",
    )
    assert code == 2
    assert all(case.status == "error" for case in frame.cases)


def test_click_command_setup_assertion_is_error_not_target_assertion(tmp_path: Path) -> None:
    package = tmp_path / "src" / "click"
    package.mkdir(parents=True)
    (package / "testing.py").write_text("class CliRunner:\n    pass\n")
    code, frame = _invoke(
        tmp_path,
        "hv4-dev-click-help-name",
        "def command():\n    raise AssertionError('synthetic decorator setup')\n",
    )
    assert code == 2
    assert all(case.status == "error" for case in frame.cases)
