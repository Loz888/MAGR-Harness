"""Frozen paired product experiments with an explicitly controlled model adapter."""

from __future__ import annotations

import json
import os
import random
from dataclasses import replace
from pathlib import Path
from time import perf_counter
from typing import Any

import pytest
from test_harness_product_service import product_env as shared_product_env

from mgar_v3.harness.contracts import HarnessToolCall, ModelResponse, ModelUsage, sha256_text
from mgar_v3.harness.delegation import CONSULT, EXPERT_OPERATION

product_env = shared_product_env
SPEC = Path(__file__).resolve().parent
CASES = {
    row["case_id"]: row for row in map(json.loads, (SPEC / "cases.jsonl").read_text().splitlines())
}
SCHEDULE = list(map(json.loads, (SPEC / "schedule.jsonl").read_text().splitlines()))


def call(request, name, args):
    return ModelResponse(
        finish_reason="tool_calls",
        tool_calls=(
            HarnessToolCall(call_id=f"{request.operation_id}:{name}", name=name, arguments=args),
        ),
    )


def prepare(case, source):
    width = case["operations"]
    datum = case["datum"]
    if case["stratum"] in {"simple", "complex", "research"}:
        files = {
            f"source_{i}.txt": f"Reference for {case['topic']}: [{datum + i}].\n"
            for i in range(width)
        }
        for filename, content in files.items():
            (source / filename).write_text(content)
        return files, {}, f"调查并解释 {case['topic']} 的来源记录与交付数据"
    files = {
        f"unit_{i}.py": f"def result(value):\n    return value - {datum + i}\n"
        for i in range(width)
    }
    edits = {
        name: content.replace("return value -", "return value +") for name, content in files.items()
    }
    for filename, content in files.items():
        (source / filename).write_text(content)
    imports = [f"from unit_{i} import result as result_{i}" for i in range(width)]
    tests = [
        f"    def test_unit_{i}(self): self.assertEqual(result_{i}(7), {7 + datum + i})"
        for i in range(width)
    ]
    (source / "test_suite.py").write_text(
        "\n".join(
            ["import unittest", *imports, "class CalculationTests(unittest.TestCase):", *tests, ""]
        )
    )
    return files, edits, f"修复 {case['topic']} 的计算错误并运行原测试"


class Model:
    def __init__(self, config, paths, old, edits, delegate):
        self.test_args = list(
            config.product_plan.get("commands", {}).get("tests", {}).get("argv", ())
        )
        self.paths, self.old, self.edits = paths, old, edits
        self.delegate = delegate
        self.trace: list[dict[str, Any]] = []
        self.wait = 0.0

    def complete(self, request, cancellation):
        start = perf_counter()
        cancellation.ensure_active()
        child = EXPERT_OPERATION in request.operation_id
        if child:
            assert not request.tools and request.output_continuation is None
            response = ModelResponse(
                text="Read-only advice. Verify the primary source and originals."
            )
        else:
            names = [message.name for message in request.messages if message.role == "tool"]
            count = names.count("read_file")
            if self.delegate and CONSULT.name not in names:
                response = call(
                    request,
                    CONSULT.name,
                    {"paths": self.paths, "question": "Verify which source facts are relevant."},
                )
            elif count < len(self.paths):
                response = call(request, "read_file", {"path": self.paths[count]})
            elif not self.edits:
                text = " ".join(
                    name + " " + self.old[name].split(": ")[1].strip() for name in self.paths
                )
                response = ModelResponse(text=text)
            elif "apply_edits" not in names:
                response = call(
                    request,
                    "apply_edits",
                    {
                        "expected_tree": request.workspace_tree,
                        "edits": [
                            {
                                "path": name,
                                "content": self.edits[name],
                                "expected_sha256": sha256_text(self.old[name]),
                            }
                            for name in self.paths
                        ],
                    },
                )
            elif "run_tests" not in names:
                response = call(request, "run_tests", {"argv": self.test_args})
            else:
                response = ModelResponse(text="Updated and tested " + " ".join(self.paths))
        self.trace.append(
            {
                "operation": request.operation_id,
                "expert": child,
                "finish": response.finish_reason,
                "tools": [x.name for x in response.tool_calls],
                "response": response.model_dump(mode="json"),
            }
        )
        if child:
            self.wait += perf_counter() - start
        return response.model_copy(
            update={"usage": ModelUsage(input_tokens=110, output_tokens=22, cost_micros=3)}
        )


@pytest.mark.parametrize("trial", SCHEDULE, ids=[f"{x['case_id']}-{x['seed']}" for x in SCHEDULE])
def test_paired_product_trial(product_env, tmp_path, trial, request):
    from runpy import run_path

    env = product_env
    case = CASES[trial["case_id"]]
    source = env.source
    old, edits, objective = prepare(case, source)
    paths = list(old)
    random.Random(trial["seed"]).shuffle(paths)
    env.project_id = env.harness.register_project(str(source))["project_id"]
    grader = run_path(str(SPEC / "grader.py"))["judge"]
    rows = []
    for arm in trial["arms"]:
        delegated = arm == "delegate"
        env.settings.harness_experts_enabled = delegated
        env.save_model()
        env.authorize(calls=32)
        env.harness.submit_task = lambda task: env.harness.create_task(
            replace(task, product_expert_max_tokens=env.settings.harness_expert_token_limit)
        )
        models = []

        def model_factory(task, *, delegated=delegated, models=models):
            model = Model(task, paths, old, edits, delegated)
            models.append(model)
            return model

        env.harness.model_factory = model_factory
        started = perf_counter()
        session = env.product.create(env.project_id, objective, str(delegated))
        elapsed = perf_counter() - started
        data = env.harness.get_task(session["task_id"])
        worktree = env.harness._runtimes[data["task_id"]].workspace.root
        grade = grader(case, data, source, worktree)
        trace = [e for model in models for e in model.trace]
        ledger = env.engine.harness.operations(data["task_id"])
        child = [e for e in trace if e["expert"]]
        consultancy = [
            op for op in ledger if op["kind"] == "tool" and op["intent"].get("name") == CONSULT.name
        ]
        rows.append(
            {
                "case_id": case["case_id"],
                "stratum": case["stratum"],
                "seed": trial["seed"],
                "arm": arm,
                "status": data["status"],
                "reason": data.get("reason"),
                "grade": grade,
                "fixture_input_tokens": data["input_tokens"],
                "fixture_output_tokens": data["output_tokens"],
                "fixture_cost_micros": data["cost_micros"],
                "model_calls": data["model_calls"],
                "parallel_wait_seconds": None,
                "delegated_model_seconds": sum(m.wait for m in models),
                "wall_seconds": elapsed,
                "source_tree": data["tree_before"],
                "tree": data["tree_after"],
                "diff": data["diff"],
                "operations": trace,
                "consultation_receipt": consultancy[0]["receipt"] if consultancy else None,
                "duplicate_calls": len(trace) - len({e["operation"] for e in trace}),
                "edit_count": sum(o["kind"] == "edit" for o in ledger),
                "expert_calls": len(child),
                "overlapping_write_conflicts": 0,
                "verifier_failures": 0 if grade["pass"] else 1,
                "reserved_micros": data["reserved_micros"],
            }
        )
    dest = os.environ.get("MAGR_PHASE3_EVIDENCE")
    if dest:
        f = Path(dest) / f"{case['case_id']}-{trial['seed']}.jsonl"
        f.write_text("".join(json.dumps(x, sort_keys=True, default=str) + "\n" for x in rows))
    assert all(r["grade"]["pass"] for r in rows), rows
    assert all(not r["reserved_micros"] and not r["duplicate_calls"] for r in rows)
    assert all(r["expert_calls"] == int(r["arm"] == "delegate") for r in rows)
    assert rows[0]["source_tree"] == rows[1]["source_tree"]
    assert rows[0]["tree"] == rows[1]["tree"] and rows[0]["diff"] == rows[1]["diff"]
