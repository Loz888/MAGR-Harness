"""Phase 3 H12 studies using the ordinary CodingLoop, Engine, Broker and local fixtures."""

from __future__ import annotations

import json
import os
from pathlib import Path
from time import perf_counter

import pytest
from test_harness_v4_h10 import ScriptedModel, _call, _stop
from test_harness_v4_h11_service import _InjectedCrash, _service

from mgar_v3.coordination.engine import Engine
from mgar_v3.evidence.artifacts import ArtifactStore
from mgar_v3.harness.contracts import ModelResponse, ModelUsage, sha256_text
from mgar_v3.harness.service import CodingTaskRequest

SPEC = Path(__file__).resolve().parent
PATCH_CASES = list(map(json.loads, (SPEC / "cases.jsonl").read_text().splitlines()))
ARMS = ("full_file", "chunked", "structured_plan", "verified_resume")


def save(key, value):
    dest = os.environ.get("MAGR_PHASE3_EVIDENCE")
    if dest:
        (Path(dest) / (key + ".json")).write_text(
            json.dumps(value, ensure_ascii=False, default=str, sort_keys=True) + "\n"
        )


def proposal(files, whole=None):
    def reply(request):
        edits = []
        for old, new, content in files:
            edit = {"path": "source.py", "expected_sha256": sha256_text(content)}
            if whole is None:
                edit["replacements"] = [
                    {"old_text": a, "new_text": b, "old_sha256": sha256_text(a)}
                    for a, b in zip(old, new, strict=True)
                ]
            else:
                edit["content"] = whole
            edits.append(edit)
        result = _call("apply_edits", {"expected_tree": request.workspace_tree, "edits": edits})
        return result.model_copy(
            update={"usage": ModelUsage(input_tokens=148, output_tokens=82, cost_micros=3)}
        )

    return reply


@pytest.mark.parametrize("case", PATCH_CASES, ids=[c["case_id"] for c in PATCH_CASES])
@pytest.mark.parametrize("arm", ARMS)
def test_patch_transport(case, arm, engine, settings, tmp_path):
    source = tmp_path / "project"
    source.mkdir()
    names = ["width", "height", "margin"]
    old = [f"{name} = -{v}" for name, v in zip(names, case["positions"], strict=True)]
    new = [expr.replace("= -", "= ") for expr in old]
    original = "\n".join(
        [*old, *[f"# filler {i:03} 測定" for i in range(case["padding_lines"])], ""]
    )
    desired = original
    for a, b in zip(old, new, strict=True):
        desired = desired.replace(a, b)
    (source / "source.py").write_text(original)
    test = "import unittest\nimport source\nclass Exact(unittest.TestCase):\n" + "".join(
        f"    def test_{name}(self): self.assertEqual(source.{name}, {v})\n"
        for name, v in zip(names, case["positions"], strict=True)
    )
    (source / "test_source.py").write_text(test)
    length = ModelResponse(
        finish_reason="length",
        text='{"name":"apply_edits","arguments":{"edits":[{"path":"source.py","content":"incomplete',
        usage=ModelUsage(input_tokens=148, output_tokens=256, cost_micros=5),
    )
    large = len(desired.encode()) > 1536
    cut = (arm == "full_file" and large) or arm == "verified_resume"
    pieces = []
    interim = original
    for a, b in zip(old, new, strict=True):
        pieces.append(proposal([([a], [b], interim)]))
        interim = interim.replace(a, b)
    if arm == "full_file":
        # Model cap observed through a controlled finish signal. No complete replacement
        # is returned in this arm once it reaches that cap.
        steps = [length] * 3 if large else [proposal([(old, new, original)], whole=desired)]
    elif arm == "structured_plan":
        steps = [proposal([(old, new, original)])]
    else:
        steps = pieces if arm == "chunked" else [pieces[0], length, *pieces[1:]]
    steps = [_call("read_file", {"path": "source.py"}), *steps]
    if not (arm == "full_file" and large):
        steps += [
            _call("run_tests", {"command_id": "tests"}),
            _stop("Changed source.py and ran tests"),
        ]
    model = ScriptedModel(steps)

    def interrupt(point, _payload):
        if (
            arm == "verified_resume"
            and point == "model_after_settlement"
            and len(model.requests) == 4
        ):
            assert model.requests[-1].output_continuation is not None
            raise _InjectedCrash("settled complete chunk before dispatch")

    service = _service(engine, settings, model, interrupt)
    project = service.register_project(str(source))
    request = CodingTaskRequest(
        project_id=project["project_id"],
        objective="Correct source.py values",
        task_kind="bugfix",
        target_path="source.py",
        approved=True,
        old_text=old[0],
        new_text=new[0],
        test_command="python3 -B -m unittest -v",
    )
    before = perf_counter()
    resumed = False
    owner = engine
    task_before = None
    try:
        if arm == "verified_resume":
            with pytest.raises(_InjectedCrash):
                service.create_task(request)
            task_before = engine.harness.tasks()[0]
            engine.close()
            owner = Engine(settings, ArtifactStore(settings.artifact_root))
            owner.recover()
            service = _service(owner, settings, model)
            result = service.recover_task(task_before["task_id"])
            resumed = owner.harness.get(task_before["task_id"])["run_id"] == task_before["run_id"]
        else:
            result = service.create_task(request)
        task = owner.harness.get(result["task_id"])
        operations = owner.harness.operations(result["task_id"])
        writes = [op for op in operations if op["kind"] == "edit"]
        capture = []
        for receipt in result["output_receipts"]:
            blob = owner.artifacts.read(receipt["artifact_hash"])
            assert sha256_text(blob.decode()) == receipt["artifact_hash"]
            capture.append({"receipt": receipt, "original_cas_json": json.loads(blob)})
        complete = result["status"] == "completed" and result["tests"]["status"] == "passed"
        correct = (
            complete
            and result["tests"]["run"] == 3
            and all(item["path"] == "source.py" for item in result["changes"])
        )
        incomplete_writes = int(cut and arm == "full_file" and bool(writes))
        key = case["case_id"] + "-" + arm
        save(
            key,
            dict(
                case_id=case["case_id"],
                arm=arm,
                task_id=result["task_id"],
                run_id=task["run_id"],
                success=correct,
                terminal=result["status"],
                reason=result["reason"],
                injected_truncation=cut,
                truncated=sum(x["receipt"]["finish_reason"] == "length" for x in capture),
                apply_receipts=len(writes),
                partial_writes=incomplete_writes,
                continuations=result["output_continuations"],
                recovered_original_run=resumed,
                model_calls=result["model_calls"],
                fixture_input_tokens=result["input_tokens"],
                fixture_output_tokens=result["output_tokens"],
                fixture_cost_micros=result["cost_micros"],
                reserved_micros=result["reserved_micros"],
                wall_seconds=perf_counter() - before,
                tree_before=result["tree_before"],
                tree_after=result["tree_after"],
                diff=result["diff"],
                test_receipts=result["receipts"],
                captures=capture,
            ),
        )
        assert (source / "test_source.py").read_text() == test and (
            source / "source.py"
        ).read_text() == original
        expected_cuts = 3 if arm == "full_file" and large else int(cut)
        assert sum(x["receipt"]["finish_reason"] == "length" for x in capture) == expected_cuts
        assert (
            len({r.operation_id for r in model.requests})
            == len(model.requests)
            == result["model_calls"]
        )
        if arm == "full_file" and large:
            assert not complete and not writes and result["tree_before"] == result["tree_after"]
        else:
            assert correct
            assert len(writes) == (3 if arm in {"chunked", "verified_resume"} else 1)
        if arm == "verified_resume":
            assert resumed and result["output_continuations"] == 1
    finally:
        if owner is not engine:
            owner.close()


PREFLIGHT_CASES = list(map(json.loads, (SPEC / "preflight-cases.jsonl").read_text().splitlines()))


@pytest.mark.parametrize("case", PREFLIGHT_CASES, ids=[c["case_id"] for c in PREFLIGHT_CASES])
def test_preflight_paired_floor(case, tmp_path):
    from runpy import run_path

    evaluation = run_path(str(SPEC / "preflight.py"))
    inputs, verify = evaluation["inputs"], evaluation["verify"]
    root = tmp_path / "baseline"
    inputs(root, case["fault"], repaired=False)
    gate = verify(root, case["fault"])
    ground_truth = case["expected"]
    recovered = gate
    if gate["status"] == "BLOCKED":
        repaired = tmp_path / "repaired"
        inputs(repaired, case["fault"], repaired=True)
        recovered = verify(repaired, case["fault"], repaired=True)
        assert (root / "check_suite.py").read_bytes() == (repaired / "check_suite.py").read_bytes()
    save(
        case["case_id"],
        {
            "case_id": case["case_id"],
            "ground_truth": ground_truth,
            "no_gate": {
                "scheduled_model_slots": 1,
                "actual_model_calls": 0,
                "counterfactual": True,
            },
            "gate": gate,
            "gate_plus_hint": {
                "schedule_allowed": recovered["status"] == "READY",
                "outcome": recovered,
            },
            "public_suite_sha": sha256_text((root / "check_suite.py").read_text()),
        },
    )
    assert gate["category"] == ground_truth, gate
    assert recovered["status"] == "READY", recovered
