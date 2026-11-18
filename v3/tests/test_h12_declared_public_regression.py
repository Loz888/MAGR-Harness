"""Original public defect failures are distinct from broken preparation."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
from test_h12_public_readiness import toy

from mgar_v3.harness.workspace import _scan, _tree_hash
from mgar_v3.quality_eval.h12_public_plan import load_public_plan
from mgar_v3.quality_eval.h12_public_readiness import (
    execute_public_original,
    validate_public_original_readiness,
)
from mgar_v3.quality_eval.h12_public_regression import declared_assertion_failure

ROOT = Path(__file__).resolve().parents[1]
LOCK = Path(
    json.loads((ROOT / ".local/h12/public-dev-20261113/image-map.json").read_text())[
        "pallets/click@8c1a0a7abbc1c36f70d1f65f3604acc46c5ce6ab"
    ]
)


@pytest.mark.parametrize(
    "output,failed,allowed,expected",
    [
        (
            "E   assert 2 == 3\nFAILED tests/test_defect.py::test_goal - AssertionError\n",
            1,
            ["tests/test_defect.py"],
            True,
        ),
        (
            "FAILED tests/test_defect.py::test_goal - TypeError: target API raised\n",
            1,
            ["tests/test_defect.py"],
            True,
        ),
        (
            "FAILED tests/test_foreign.py::test_goal - AssertionError\n",
            1,
            ["tests/test_defect.py"],
            False,
        ),
        (
            "FAILED tests/test_defect.py::test_goal - ImportError\n",
            1,
            ["tests/test_defect.py"],
            False,
        ),
        (
            "ERROR collecting tests/test_defect.py\nFAILED tests/test_defect.py::test_goal\n",
            1,
            ["tests/test_defect.py"],
            False,
        ),
        ("FAILED tests/test_defect.py::test_goal\n", 2, ["tests/test_defect.py"], False),
        ("FAILED tests/test_defect.py::test_goal\n", 1, [], False),
    ],
)
def test_only_declared_executed_public_failures_are_accepted(output, failed, allowed, expected):
    assert declared_assertion_failure(output, failed, allowed) is expected


@pytest.mark.parametrize(
    "variant,expected", [("declared", "READY"), ("unlisted", "NOT_RUN"), ("import", "NOT_RUN")]
)
def test_actual_readonly_public_suite_keeps_declared_red_and_refuses_setup_errors(
    tmp_path, variant, expected
):
    task, old, profile, workspace = toy(tmp_path)
    (workspace / "tests").mkdir()
    path = workspace / "tests/test_defect.py"
    path.write_text(
        "import calc\ndef test_goal():\n    assert calc.add(2, 3) == 5\n"
        if variant != "import"
        else "import missing_public_preparation_dependency\n"
    )
    (workspace / "pytest.ini").write_text("[pytest]\nfilterwarnings = error\n")
    argv = ("python3", "-B", "-m", "pytest", "-q", "tests")
    document = json.loads(old.manifest_path.read_text())
    document["original_tree_sha256"] = _tree_hash(_scan(workspace)[0])
    document["regression_argv"] = list(argv)
    if variant != "unlisted":
        document["public_basis"].append(
            {
                "path": "tests/test_defect.py",
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            }
        )
    old.manifest_path.write_text(json.dumps(document))
    frozen = load_public_plan(
        old.manifest_path,
        hashlib.sha256(old.manifest_path.read_bytes()).hexdigest(),
        task=task,
        asset_root=old.asset_root,
        regression_argv=argv,
    )
    profile = profile.model_copy(update={"public_test_argv": argv})
    evidence = tmp_path / "declared-proof"
    result = execute_public_original(frozen, workspace, profile, LOCK, evidence)
    assert result["status"] == expected and result["probe_outcome"] == "red"
    assert not (workspace / ".pytest_cache").exists()
    assert "PytestCacheWarning" not in (evidence / "regression.log").read_text()
    assert result["original_tree_after"] == result["original_tree_before"]
    if expected == "READY":
        assert result["regression_status"] == "failed" and result["regression_failed"] == 1
        validate_public_original_readiness(
            result, evidence, frozen=frozen, profile=profile, image_lock=LOCK
        )
    else:
        assert "ORIGINAL_PUBLIC_REGRESSION_NOT_GREEN" in result["blockers"]
