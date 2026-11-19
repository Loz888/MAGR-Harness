"""H12 bounded tool progress and PG-selected chunked recovery regressions."""

from __future__ import annotations

import json

import pytest
from test_h11_loop import Crash, MemoryContinuation, fresh_loop
from test_harness_v4_h07 import (
    BUGGY,
    TEST_CALC,
    ScriptedModel,
    _grant,
    _loop,
    _run,
    _seed,
    _stop,
    _task,
    _tool,
)

from mgar_v3.domain import DomainError
from mgar_v3.evidence.artifacts import ArtifactStore
from mgar_v3.harness.adapters import HarnessToolRegistry
from mgar_v3.harness.contracts import HarnessToolResult, ToolSpec
from mgar_v3.harness.persistence import _checkpoint_bytes, _checkpoint_payload
from mgar_v3.harness.service import CodingTaskRequest, HarnessCodingService


def test_repeated_real_failed_commands_stop_after_corrective_feedback(tmp_path):
    model = ScriptedModel([_run("-m", "unittest", "-q") for _ in range(16)])
    loop, _, _ = _loop(_seed(tmp_path, {"calc.py": BUGGY, "test_calc.py": TEST_CALC}), model)
    task = _task("bugfix")
    result = loop.run(task, _grant()[0], loop.plan(task))
    assert (result.status, result.reason) == ("blocked", "no_progress")
    assert result.model_calls == 3
    assert len(result.receipts) == 3
    assert all(receipt.tests_failed == 1 for receipt in result.receipts)
    assert any(
        "This tool batch made no new progress." in m.content
        and "Repeated unchanged batches stop at the frozen bound." in m.content
        for m in model.requests[-1].messages
    )
    assert [attempt.progress for attempt in result.attempts] == [True, False, False]


class PageRegistry(HarnessToolRegistry):
    identical_text = False

    def specs(self):
        return super().specs() + (
            ToolSpec(
                name="artifact_page",
                description="Read a validated page",
                capability="read",
            ),
        )

    def invoke(self, call, context, cancellation):
        if call.name != "artifact_page":
            return super().invoke(call, context, cancellation)
        offset = call.arguments["offset"]
        data = {
            "artifact_hash": "a" * 64,
            "path": "calc.py",
            "offset": offset,
            "source_tree": context.workspace_tree,
            "text": "same repeated source" if self.identical_text else f"source page {offset}",
        }
        return HarnessToolResult(
            call_id=call.call_id,
            status="ok",
            content=json.dumps(data),
            data=data,
        )


@pytest.mark.parametrize("repeat", [False, True])
@pytest.mark.parametrize("identical_text", [False, True])
def test_distinct_pages_of_one_artifact_are_progress_but_repeated_pages_are_bounded(
    tmp_path, monkeypatch, repeat, identical_text
):
    monkeypatch.setattr(PageRegistry, 'identical_text', identical_text)
    steps = [
        _tool("artifact_page", {"path": "calc.py", "offset": n})
        for n in ((0, 1, 2, 2, 2) if repeat else (0, 1, 2))
    ]
    model = ScriptedModel(steps + [_stop("calc.py contains source")])
    loop, _, _ = _loop(
        _seed(tmp_path, {"calc.py": BUGGY}),
        model,
        execute=False,
        write_paths=(),
        mode="investigate",
        registry=PageRegistry,
    )
    task = _task("inspect")
    result = loop.run(
        task, _grant(execute=False, write_paths=(), mode="investigate")[0], loop.plan(task)
    )
    if repeat:
        assert (result.status, result.reason, result.model_calls) == ("blocked", "no_progress", 5)
        assert [attempt.progress for attempt in result.attempts] == [True, True, True, False, False]
    else:
        assert result.status == "completed", result.reason
        assert result.model_calls == 4
        assert all(attempt.progress for attempt in result.attempts)


def test_recovery_preserves_tool_stagnation_streak(tmp_path):
    class CrashBeforeThirdRequest(MemoryContinuation):
        def save(self, payload):
            super().save(payload)
            if payload["phase"] == "model_request" and payload["state"]["model_calls"] == 2:
                raise Crash()

    model = ScriptedModel([_run("-m", "unittest", "-q") for _ in range(16)])
    loop, _, _ = _loop(_seed(tmp_path, {"calc.py": BUGGY, "test_calc.py": TEST_CALC}), model)
    continuation = CrashBeforeThirdRequest()
    loop.continuation = continuation
    task, grant = _task("bugfix"), _grant()[0]
    with pytest.raises(Crash):
        loop.run(task, grant, loop.plan(task))
    assert continuation.value["state"]["tool_streak"] == 1
    # Load through a fresh adapter, without the one-shot fault injector.
    recovered = MemoryContinuation()
    recovered.value = continuation.value
    resumed = fresh_loop(loop, recovered)
    result = resumed.run(task, grant, resumed.plan(task))
    assert (result.reason, result.model_calls, model.calls) == ("no_progress", 3, 3)


def large_payload():
    return {
        "schema_version": "harness-loop-continuation-v1",
        "expected_tree": "a" * 64,
        "phase": "model_request",
        "state": {
            "baseline_files": {"source.py": "汉字 source\n" * 100_000},
            "receipts": [{"stdout": "raw test log\n" * 60_000}],
            "instruction_revision": 0,
        },
        "history": [{"role": "tool", "content": "history\n" * 90_000}],
        "request": {"operation_id": "task-one:model:4", "messages": []},
    }


def test_checkpoint_larger_than_cas_cap_round_trips_via_bounded_manifest(tmp_path):
    artifacts = ArtifactStore(tmp_path / "cas")
    payload = large_payload()
    inline = json.dumps(
        {"task_id": "task-one", "run_id": "run-one", "continuation": payload}
    ).encode()
    assert len(inline) > artifacts.max_bytes
    with pytest.raises(DomainError) as error:
        artifacts.put(inline)
    assert error.value.code == "ARTIFACT_TOO_LARGE"
    raw = _checkpoint_bytes(artifacts, "task-one", "run-one", payload)
    assert len(raw) < 16_000
    manifest_hash = artifacts.put(raw)
    assert (
        _checkpoint_payload(artifacts, artifacts.read(manifest_hash), "task-one", "run-one")
        == payload
    )
    manifest = json.loads(raw)
    baseline = manifest["sections"]["state.baseline_files"]
    payload["state"]["receipts"].append({"stdout": "additional receipt"})
    updated = json.loads(_checkpoint_bytes(artifacts, "task-one", "run-one", payload))
    assert updated["sections"]["state.baseline_files"] == baseline
    assert max(p.stat().st_size for p in (tmp_path / "cas").glob("*/*")) <= artifacts.max_bytes


@pytest.mark.parametrize(
    "corruption",
    [
        "scope",
        "section_scope",
        "section_name",
        "version",
        "type",
        "hash",
        "size",
        "chunks",
        "missing",
        "hint",
        "bytes",
    ],
)
def test_checkpoint_references_fail_closed(tmp_path, corruption):
    artifacts = ArtifactStore(tmp_path / "cas")
    manifest = json.loads(_checkpoint_bytes(artifacts, "task-one", "run-one", large_payload()))
    section = manifest["sections"]["state.baseline_files"]
    if corruption == "scope":
        manifest["run_id"] = "another-run"
    elif corruption == "section_scope":
        section["task_id"] = "another-task"
    elif corruption == "section_name":
        section["name"] = "history"
    elif corruption == "version":
        manifest["schema_version"] = "harness-checkpoint-v99"
    elif corruption == "type":
        section["json_type"] = "array"
    elif corruption == "hash":
        section["sha256"] = "b" * 64
    elif corruption == "size":
        section["bytes"] += 1
    elif corruption == "chunks":
        section["chunks"] = section["chunks"] * 128
    elif corruption == "missing":
        artifacts.path(section["chunks"][0]).unlink()
    elif corruption == "hint":
        manifest["continuation"]["request"]["operation_id"] = "task-one:model:5"
    elif corruption == "bytes":
        artifacts.path(section["chunks"][0]).write_bytes(b"tampered bytes")
    with pytest.raises(DomainError):
        _checkpoint_payload(artifacts, json.dumps(manifest).encode(), "task-one", "run-one")


def test_historical_inline_checkpoint_stays_readable(tmp_path):
    artifacts = ArtifactStore(tmp_path / "cas")
    payload = {"state": {"baseline_files": {"source.py": "original"}}, "request": None}
    raw = json.dumps({"task_id": "task-one", "run_id": "run-one", "continuation": payload}).encode()
    original_hash = artifacts.put(raw)
    assert (
        _checkpoint_payload(artifacts, artifacts.read(original_hash), "task-one", "run-one")
        == payload
    )
    assert artifacts.read(original_hash) == raw


@pytest.mark.parametrize("cut", ["checkpoint_before_commit", "checkpoint_after_commit"])
def test_real_pg_service_recovers_large_baseline_and_known_read_without_extra_model_dispatch(
    engine,
    tmp_path,
    cut,
):
    source = tmp_path / "source"
    source.mkdir()
    (source / "source.py").write_text("value = 1\n")
    for index in range(16):
        (source / f"large{index}.py").write_text("# original baseline\n" * 8_000)
    model = ScriptedModel(
        [
            _tool("read_file", {"path": "source.py"}),
            _stop("source.py contains value = 1"),
        ]
    )
    commits = 0

    def fault(point, payload):
        nonlocal commits
        if point == cut:
            commits += 1
            if commits == 3:
                raise Crash()

    service = HarnessCodingService(
        engine.settings.harness_root,
        actor="owner",
        image_lock=engine.settings.harness_image_lock,
        engine=engine,
        model_factory=lambda _: model,
        fault_hook=fault,
    )
    project = service.register_project(str(source))
    with pytest.raises(Crash):
        service.create_task(
            CodingTaskRequest(
                project_id=project["project_id"],
                objective="Inspect source.py",
                task_kind="inspect",
                target_path="source.py",
            )
        )
    task = engine.harness.tasks()[0]
    old_hash = task["checkpoint_hash"]
    selected = _checkpoint_payload(
        engine.artifacts,
        engine.artifacts.read(old_hash),
        task["task_id"],
        str(task["run_id"]),
    )
    assert sum(len(text) for text in selected["state"]["baseline_files"].values()) > 2_000_000
    assert len(engine.artifacts.read(old_hash)) < 64_000
    assert model.calls == 1
    service.fault_hook = lambda *_: None
    engine.harness.recover()
    result = service.recover_task(task["task_id"])
    assert result["status"] == "completed", result["reason"]
    assert model.calls == 2
    assert result["model_calls"] == 2
    assert engine.artifacts.read(old_hash)

class RecoverPageRegistry(HarnessToolRegistry):
    def specs(self):
        return super().specs() + (
            ToolSpec(name="artifact_page", description="Read a validated page", capability="read"),
        )

    def invoke(self, call, context, cancellation):
        if call.name != "artifact_page":
            return super().invoke(call, context, cancellation)
        page = {
            "artifact_hash": "a" * 64,
            "path": "calc.py",
            "offset": 0,
            "source_tree": context.workspace_tree,
            "text": "validated first page",
        }
        if call.arguments["cursor"] == "issued":
            return HarnessToolResult(
                call_id=call.call_id, status="ok", content=json.dumps(page), data=page
            )
        return HarnessToolResult(
            call_id=call.call_id,
            status="error",
            code="CONTEXT_CURSOR_INVALID",
            content=json.dumps({"page": page, "correctable": True}),
            data={"page": page, "correctable": True},
        )


@pytest.mark.parametrize("first_cursor", ["issued", "1130"])
def test_recovered_first_page_counts_once_without_resetting_stagnation(
    tmp_path, first_cursor
):
    calls = [
        _tool("artifact_page", {"path": "calc.py", "artifact_hash": "a" * 64,
                                "cursor": cursor})
        for cursor in (first_cursor, "1130", "1130")
    ]
    model = ScriptedModel(calls)
    loop, _, _ = _loop(
        _seed(tmp_path, {"calc.py": BUGGY}), model,
        execute=False, write_paths=(), mode="investigate", registry=RecoverPageRegistry,
    )
    task = _task("inspect")
    result = loop.run(
        task, _grant(execute=False, write_paths=(), mode="investigate")[0], loop.plan(task)
    )
    assert (result.status, result.reason, result.model_calls) == (
        "blocked", "no_progress", 3
    )
    assert [attempt.progress for attempt in result.attempts] == [True, False, False]
