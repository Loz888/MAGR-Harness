"""A known import failure stays actionable until the worker changes the tree."""

from test_h12_budget_context import working_state
from test_h12_r04_service import feature_model, feature_request, service
from test_harness_v4_h10 import _call, _stop

from mgar_v3.harness.contracts import sha256_text


def test_pg_read_preserves_failed_entry_until_fix_and_final_verification(
    engine, settings, tmp_path,
):
    broken = "module.exports=require('./missing.cjs');\n"
    model = feature_model(broken)
    old_receipts = []

    def entry(request):
        return next(i for i in working_state(request)["checklist"]["items"]
                    if i["kind"] == "entrypoint")

    def inspect(request):
        item = entry(request)
        assert item["status"] == "failed" and "missing.cjs" in item["detail"]
        old_receipts.extend(item["evidence"])
        return _call("read_file", {"path": "marker.py"})

    def repair(request):
        item = entry(request)
        assert item["status"] == "failed" and item["evidence"] == old_receipts
        assert "missing.cjs" in item["detail"]
        return _call("apply_edits", {"expected_tree": request.workspace_tree, "edits": [{
            "path": "examples/entry.cjs", "expected_sha256": sha256_text(broken),
            "content": "module.exports={value:7};\n",
        }]})

    def finish(request):
        item = entry(request)
        assert item["status"] == "pending" and not item["evidence"]
        assert "final answer" in item["detail"]
        return _stop("Fixed the import in examples/entry.cjs and ran the public tests.")

    model.steps.extend([inspect, repair, _call("run_tests", {"command_id": "tests"}), finish])
    coding = service(engine, settings, model, node=True)
    result = coding.create_task(feature_request(coding, tmp_path))
    assert result["status"] == "completed", (result["reason"], result["summary"])
    assert old_receipts and not model.steps
    batches = result["entrypoint_batches"]
    assert [b["status"] for b in batches] == ["FAIL", "PASS"]
    assert batches[0]["tree_hash"] != batches[1]["tree_hash"] == result["tree_after"]
