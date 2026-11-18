"""New structure is a final criterion, while original behavior is a baseline."""

import hashlib
import json
from pathlib import Path

import pytest

from mgar_v3.quality_eval import h12, h12_judge
from mgar_v3.quality_eval.harness_v4 import CatalogError

ROOT = Path(__file__).resolve().parents[1]
LOCK = Path(
    json.loads((ROOT / ".local/h12/public-dev-20261113/image-map.json").read_text())[
        "pallets/click@8c1a0a7abbc1c36f70d1f65f3604acc46c5ce6ab"
    ]
)


def test_public_objective_carries_required_but_not_private_fields():
    task = {
        "goal": "Run local selfcheck",
        "acceptance": {
            "required": ["Print the observed status", "Close the server", "tests/hidden_secret.py"],
            "baseline": ["secret baseline body"],
            "private": "hidden implementation",
        },
    }
    objective = h12.public_objective(task)
    assert "Print the observed status" in objective and "Close the server" in objective
    assert "hidden" not in objective and "secret" not in objective
    with pytest.raises(CatalogError):
        h12.public_objective({"goal": "x", "acceptance": {"required": ["a" * 1025]}})


@pytest.mark.parametrize(
    "variant", ["declared", "foreign", "syntax", "dependency", "changed", "bugfix"]
)
def test_only_declared_hash_bound_missing_go_feature_api_is_red_baseline(tmp_path, variant):
    path = tmp_path / "public_case_test.go"
    path.write_text("// frozen public feature check\n")
    task = {
        "language": "go",
        "type": "feature",
        "public_assets": [
            {"target": path.name, "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
        ],
    }
    output = (
        "./public_case_test.go:3:4: f.Names undefined "
        "(type *FlagSet has no field or method Names)\nFAIL\n"
    )
    if variant == "foreign":
        output = output.replace("public_case_test.go", "implementation.go")
    elif variant == "syntax":
        output = "./public_case_test.go:3:4: syntax error: unexpected token\nFAIL\n"
    elif variant == "dependency":
        output += (
            "go: no required module provides package missing.example/package\n[setup failed]\n"
        )
    elif variant == "changed":
        path.write_text("// changed test\n")
    elif variant == "bugfix":
        task["type"] = "bugfix"
    assert h12_judge._public_go_missing_baseline(task, tmp_path, output, 1) is (
        variant == "declared"
    )


def test_real_refactor_baseline_keeps_original_behavior_but_final_still_requires_structure(
    tmp_path,
    monkeypatch,
):
    workspace = tmp_path / "workspace"
    (workspace / "tests").mkdir(parents=True)
    (workspace / "source.py").write_text("def normalize(value):\n    return value.lower()\n")
    (workspace / "tests/test_behavior.py").write_text(
        "from source import normalize\ndef test_behavior():\n    assert normalize('AbC') == 'abc'\n"
    )
    private = tmp_path / "private-structure.py"
    private.write_text(
        "import ast\nfrom pathlib import Path\ndef test_structure():\n"
        "    tree=ast.parse(Path('source.py').read_text())\n"
        "    assert any(isinstance(n,ast.FunctionDef) and n.name=='helper' for n in tree.body)\n"
    )
    task = {
        "task_id": "synthetic-refactor",
        "split": "dev",
        "type": "refactor",
        "repository": "fixture/project",
        "language": "python",
        "judge_sha256": "a" * 64,
        "environment": {"verify": []},
        "acceptance": {"regression": ["python3 -B -m pytest tests/"]},
    }
    bundle = {
        "baseline_oracle": False,
        "files": [
            {
                "file": str(private),
                "target": "tests/h12_private_structure.py",
                "sha256": hashlib.sha256(private.read_bytes()).hexdigest(),
            }
        ],
        "commands": [["python3", "-B", "-m", "pytest", "tests/h12_private_structure.py"]],
    }
    monkeypatch.setattr(h12_judge, "prepare_judge", lambda *a, **k: bundle)
    before = h12_judge.tree_hash(workspace)
    baseline = h12_judge.verify(workspace, task, LOCK, tmp_path / "baseline", phase="baseline")
    assert baseline["status"] == "PASS"
    assert baseline["baseline_method"] == "frozen_public_refactor_behavior"
    assert h12_judge.tree_hash(workspace) == before
    assert not (workspace / "tests/h12_private_structure.py").exists()
    final = h12_judge.verify(
        workspace,
        task,
        LOCK,
        tmp_path / "final",
        sealed=True,
        expected_tree=before,
        frozen_tests=h12_judge.test_hashes(workspace),
    )
    assert final["status"] == "FAIL" and final["reason"] == "INDEPENDENT_CHECK_FAILED"
    assert h12_judge.tree_hash(workspace) == before
