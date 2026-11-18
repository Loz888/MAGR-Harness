"""Public reusable boundary assets executed in original pinned isolation."""

from pathlib import Path

import pytest
from test_h12_r04_runtime import runtime
from test_h12_r06_baseline import LOCK
from test_harness_v4_h06 import _grant

from mgar_v3.evidence.artifacts import ArtifactStore
from mgar_v3.harness.contracts import CancellationToken
from mgar_v3.harness.lifecycle_types import HTTPExpectation
from mgar_v3.quality_eval import h12_judge
from mgar_v3.quality_eval.h12_independent_runner import IndependentRegressionRunner
from mgar_v3.quality_eval.h12_regression_plan import regression_plan

ASSETS = Path(__file__).parent / "fixtures/h12_r07"
PY_ARGS = """import argparse
def parse(values):
 p=argparse.ArgumentParser();p.add_argument('--debug',action='store_true')
 namespace,rest=p.parse_known_args(values)
 return namespace.debug,rest
"""
JS_SPLIT = """exports.split = function(text) {
  const values = []; let start = 0, quote = null, escaped = false;
  for(let i = 0; i < text.length; i++) {
    const c = text[i];
    if(escaped) { escaped = false; continue; }
    if(c === "\\\\" && quote) { escaped = true; continue; }
    if(quote) { if(c === quote) quote = null; }
    else if(c === '"' || c === "'") quote = c;
    else if(c === ';') { values.push(text.slice(start, i)); start = i + 1; }
  }
  values.push(text.slice(start)); return values;
};
"""


@pytest.mark.parametrize(
    "case", ["unknown_argv", "quoted_delimiter", "module_import", "light_import"]
)
@pytest.mark.parametrize("good", [False, True], ids=["negative", "positive"])
def test_public_cross_project_language_boundary_is_executed(tmp_path, case, good):
    source = tmp_path / "project"
    source.mkdir()
    files = {}
    argv = ()
    if case == "unknown_argv":
        code = (
            PY_ARGS
            if good
            else PY_ARGS.replace(
                "namespace,rest=p.parse_known_args(values)",
                "namespace=p.parse_args(values);rest=[]",
            )
        )
        files = {
            "program.py": code,
            "test_boundary.py": (ASSETS / "unknown_argv_public.py").read_text(),
        }
        argv = ("python3", "-B", "-m", "unittest", "test_boundary")
    elif case == "quoted_delimiter":
        code = JS_SPLIT if good else "exports.split = text => text.split(';');\n"
        files = {
            "split.cjs": code,
            "test_boundary.cjs": (ASSETS / "quoted_delimiter_public.cjs").read_text(),
        }
        argv = ("node", "--test", "test_boundary.cjs")
    elif case == "module_import":
        code = "def main():return 0\nif __name__=='__main__':raise SystemExit(main())\n"
        if not good:
            code += "raise RuntimeError('module entry executed while importing')\n"
        files = {
            "package/__init__.py": "from .core import format_result\n",
            "package/core.py": "def format_result(v):return 'value='+str(v)\n",
            "package/__main__.py": code,
            "test_boundary.py": (ASSETS / "module_import_public.py").read_text(),
        }
        argv = ("python3", "-B", "-m", "unittest", "test_boundary")
    else:
        code = "package stable\nfunc LegacyName() int {return 7}\n"
        if not good:
            code = code.replace("LegacyName", "Replacement")
        files = {
            "values.go": code,
            "go.mod": "module example/r07stable\n\ngo 1.22\n",
            "values_test.go": (ASSETS / "light_import_public_test.go").read_text(),
        }
        argv = ("go", "test", "-buildvcs=false", "./...")
    for name, data in files.items():
        path = source / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(data)
    runner = IndependentRegressionRunner(
        ArtifactStore(tmp_path / "cas"),
        LOCK,
        repository="public/r07-" + case,
        task_id="r07-" + case,
        experiment_id="r07-boundary-matrix",
    )
    _, report = runner.execute(
        source,
        regression_plan([argv]),
        tmp_path / "evidence",
        phase="baseline",
        expected_tree=h12_judge.tree_hash(source),
        frozen_tests=h12_judge.test_hashes(source),
    )
    assert report.status == ("PASS" if good else "BLOCKED" if case == "light_import" else "FAIL"), (
        report
    )
    if not good and case == "light_import":
        import json

        native = json.loads(runner.artifacts.read(report.receipt_refs[0]))["result"]
        assert native["tests_run"] is None and native["tests_failed"] is None


@pytest.mark.parametrize("case", ["readiness", "cleanup"])
@pytest.mark.parametrize("good", [False, True], ids=["negative", "positive"])
def test_public_node_readiness_and_cleanup_boundaries_use_real_processes(tmp_path, case, good):
    code = (ASSETS / "server_public.cjs").read_text()
    if not good and case == "readiness":
        code = code.replace("'READY port='", "'BOOTING port='")
    if not good and case == "cleanup":
        code = code.replace("() => server.close(() => process.exit(0))", "() => {}")
    runner, session, broker = runtime(tmp_path, {"server.cjs": code})
    tree = broker.workspace_tree_hash()
    try:
        handle, evidence = runner.application(
            ("node", "server.cjs"),
            _grant(),
            "r07-public-" + case,
            tree,
            CancellationToken(),
            mode="http",
            port=0,
            routes=(HTTPExpectation(body="ok"),),
            cwd=".",
            timeout=3,
        )
        cleanup = evidence.cleanup or runner.cleanup(
            handle.handle_id, handle.operation_id, tree, port=evidence.ready.port or 0
        )
        assert evidence.ready.ready == (good or case == "cleanup"), evidence
        assert cleanup.graceful == (good or case == "readiness"), cleanup
        assert cleanup.port_released and cleanup.processes_exited and cleanup.container_removed
        assert not cleanup.orphan_pids and broker.workspace_tree_hash() == tree
    finally:
        session.close()
