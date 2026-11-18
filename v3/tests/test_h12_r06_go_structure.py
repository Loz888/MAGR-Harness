"""Go parser in the pinned sandbox: common return helper and preserved CSV behavior."""

import json

import pytest
from test_h12_r06_baseline import LOCK

from mgar_v3.evidence.artifacts import ArtifactStore
from mgar_v3.harness.regression_types import RegressionCommand, RegressionPlan
from mgar_v3.harness.semantic_types import SharedHelperSpec, public_id
from mgar_v3.quality_eval import h12_judge
from mgar_v3.quality_eval.h12_independent_runner import IndependentRegressionRunner
from mgar_v3.quality_eval.h12_regression_plan import regression_plan
from mgar_v3.quality_eval.h12_structural_probe import freeze_structural_probe

GOAL = (
    "sliceValue.String and arrayValue.String delegate their CSV return value to one "
    "shared helper; its name is unrestricted and behavior stays identical."
)
PREFIX = """package values
import("encoding/csv";"strings")
type sliceValue struct{value []string}
type arrayValue struct{value []string}
"""
BODY = ('var b strings.Builder;w:=csv.NewWriter(&b);_ = w.Write(values);w.Flush();'
        'return strings.TrimSuffix(b.String(),"\\n")')
HELPER = "func serializeCSV(values []string)string{" + BODY + "}\n"
ORIGINAL = (
    PREFIX
    + "\n".join(
        "func(v *" + kind + ")String()string{" + BODY.replace("values", "v.value") + "}"
        for kind in ["sliceValue", "arrayValue"]
    )
    + "\n"
)
TEST = """package values
import "testing"
func TestCSVBehavior(t *testing.T){
 s:=&sliceValue{[]string{"a,b","c"}};a:=&arrayValue{[]string{"a,b","c"}}
 if s.String()!=`"a,b",c` || a.String()!=s.String(){t.Fatal("CSV representation changed")}
}
"""


@pytest.mark.parametrize("variant", ["alternate-name", "alias", "unused", "dummy-inline"])
def test_actual_go_shared_writer_uses_real_return_flow_and_behavior(tmp_path, variant):
    source = tmp_path / "project"
    source.mkdir()
    code = ORIGINAL + HELPER
    if variant in {"alternate-name", "alias"}:
        code = PREFIX + HELPER.replace("serializeCSV", "encodeRecord")
        for kind in ["sliceValue", "arrayValue"]:
            expression = (
                "return encodeRecord(v.value)"
                if variant == "alternate-name"
                else "encoded:=encodeRecord(v.value);return encoded"
            )
            code += "func(v *" + kind + ")String()string{" + expression + "}\n"
    elif variant == "dummy-inline":
        code = (
            ORIGINAL.replace(
                'return strings.TrimSuffix(b.String(),"\\n")',
                'return identity(strings.TrimSuffix(b.String(),"\\n"))',
            )
            + HELPER
            + "func identity(s string)string{return s}\n"
        )
    (source / "values.go").write_text(code)
    (source / "values_test.go").write_text(TEST)
    (source / "go.mod").write_text("module example/r06values\n\ngo 1.22\n")
    spec = SharedHelperSpec(
        acceptance_requirement_id=public_id(GOAL),
        language="go",
        path="values.go",
        callers=("sliceValue.String", "arrayValue.String"),
    )
    assets = tmp_path / "assets"
    probe = freeze_structural_probe(spec, assets)
    artifacts = ArtifactStore(tmp_path / "cas")
    runner = IndependentRegressionRunner(
        artifacts,
        LOCK,
        repository="toy/go-structure",
        task_id="r06-go-writer",
        experiment_id="r06-go-writer",
        public_probe_root=assets,
        # Compile the fixed Go parser under the frozen Go project envelope,
        # rather than the generic 256 MiB default used by Python fixtures.
        memory_mb=512,
        nofile=1024,
    )
    tree = h12_judge.tree_hash(source)
    tests = h12_judge.test_hashes(source)
    _, behavior = runner.execute(
        source,
        regression_plan([("go", "test", "-buildvcs=false", "./...")]),
        tmp_path / "behavior",
        phase="baseline",
        expected_tree=tree,
        frozen_tests=tests,
    )
    plan = RegressionPlan(
        commands=(
            RegressionCommand(
                suite_id="structure",
                public_argv=probe.argv,
                argv=probe.argv,
                framework="public-probe",
                public_probe=probe,
            ),
        )
    )
    reference, structure = runner.execute(
        source,
        plan,
        tmp_path / "structure",
        phase="final",
        expected_tree=tree,
        frozen_tests=tests,
        sealed=True,
        role="structural_constraints",
        execution_task_id="r06-go-owned",
        run_id="r06-go-owned-run",
        execution_epoch=1,
    )
    assert behavior.status == "PASS", behavior.model_dump()
    expected = "PASS" if variant in {"alternate-name", "alias"} else "FAIL"
    assert structure.status == expected, (
        structure.model_dump(),
        json.loads(artifacts.read(reference)),
    )
