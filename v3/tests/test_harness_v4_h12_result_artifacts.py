"""Final results and recovery receipts stay below the unchanged CAS cap."""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest
from test_harness_v4_h12_runtime import freeze

from mgar_v3.domain import DomainError
from mgar_v3.evidence.artifacts import ArtifactStore
from mgar_v3.harness.contracts import BudgetLimits, CapabilityGrant
from mgar_v3.harness.recovery_io import RecoveryIO
from mgar_v3.harness.result_artifacts import dump_result, load_result
from mgar_v3.harness.workspace import WorkspaceRegistry

KEY = "__harness_result_cas__"


def _payload():
    return {
        "task_id": "task",
        "run_id": "run",
        "evaluation_profile": "profile-hash",
        "status": "completed",
        "candidate_status": "completed",
        "tree_after": "tree-hash",
        "independent_gate": {"status": "PASS", "tree_hash": "tree-hash"},
        "receipts": [
            {"stdout": ("original log <>& 中文\n" * 80_000), "index": i} for i in range(3)
        ],
    }


def _encoded(artifacts, payload=None):
    original = payload if payload is not None else _payload()
    return original, dump_result(artifacts, original, "task", "run")


def _replace_manifest(artifacts, index, manifest):
    raw = json.dumps(manifest, sort_keys=True, ensure_ascii=False).encode()
    index[KEY]["manifest_sha256"] = artifacts.put(raw)
    return json.dumps(index, ensure_ascii=False).encode()


def test_multi_megabyte_unicode_logs_roundtrip_below_unchanged_cap(tmp_path: Path) -> None:
    artifacts = ArtifactStore(tmp_path / "cas")
    payload, raw = _encoded(artifacts)
    assert len(json.dumps(payload, ensure_ascii=False).encode()) > 3_000_000
    assert len(raw) < 64_000
    assert artifacts.max_bytes == 2_000_000
    index = json.loads(raw)
    for field in (
        "task_id",
        "run_id",
        "evaluation_profile",
        "status",
        "candidate_status",
        "tree_after",
        "independent_gate",
    ):
        assert index[field] == payload[field]
    outer_hash = artifacts.put(raw)
    reopened = ArtifactStore(artifacts.root)
    assert load_result(reopened, reopened.read(outer_hash), "task", "run") == payload
    assert max(path.stat().st_size for path in artifacts.root.glob("*/*")) < 2_000_000


def test_small_unicode_and_legacy_inline_remain_compatible(tmp_path: Path) -> None:
    artifacts = ArtifactStore(tmp_path / "cas")
    payload = {"stdout": "中文", "bytes": [True, None, 12, 1.5]}
    raw = dump_result(artifacts, payload, "task", "run")
    assert KEY not in json.loads(raw)
    assert b"\\u" not in raw
    assert load_result(artifacts, raw, "task", "run") == payload
    legacy = json.dumps({"task_id": "task", "run_id": "run", "output": "中文"}).encode()
    assert load_result(artifacts, legacy, "task", "run")["output"] == "中文"
    assert not list(artifacts.root.glob("*/*"))


@pytest.mark.parametrize(
    "payload",
    [
        {"task_id": "application-owned-value", "run_id": {"arbitrary": "data"}},
        {KEY: {"arbitrary": "application data"}},
        {},
    ],
)
def test_arbitrary_payload_fields_do_not_override_envelope_scope(tmp_path: Path, payload) -> None:
    artifacts = ArtifactStore(tmp_path / "cas")
    raw = dump_result(artifacts, payload, "task", "run")
    assert load_result(artifacts, raw, "task", "run") == payload
    if payload:
        assert json.loads(raw)[KEY]["task_id"] == "task"
        with pytest.raises(DomainError) as error:
            load_result(artifacts, raw, "another-task", "run")
        assert error.value.code == "RESULT_SCOPE_DENIED"


@pytest.mark.parametrize("task,run", [("other", "run"), ("task", "other")])
def test_cross_run_indexes_are_rejected(tmp_path: Path, task: str, run: str) -> None:
    artifacts = ArtifactStore(tmp_path / "cas")
    _, raw = _encoded(artifacts)
    with pytest.raises(DomainError) as error:
        load_result(artifacts, raw, task, run)
    assert error.value.code == "RESULT_SCOPE_DENIED"


@pytest.mark.parametrize(
    "field,changed",
    [
        ("status", "failed"),
        ("candidate_status", "failed"),
        ("evaluation_profile", "different"),
        ("tree_after", "another-tree"),
        ("independent_gate", {"status": "PASS", "tree_hash": "other"}),
        ("task_id", "different"),
        ("run_id", "different"),
    ],
)
def test_projection_tampering_is_rejected(tmp_path: Path, field: str, changed) -> None:
    artifacts = ArtifactStore(tmp_path / "cas")
    _, raw = _encoded(artifacts)
    index = json.loads(raw)
    index[field] = changed
    with pytest.raises(DomainError) as error:
        load_result(artifacts, json.dumps(index).encode(), "task", "run")
    assert error.value.code == "RESULT_INVALID"


def test_projection_type_does_not_use_python_boolean_numeric_equality(tmp_path: Path) -> None:
    artifacts = ArtifactStore(tmp_path / "cas")
    _, raw = _encoded(artifacts, {"status": 1, "stdout": "a" * 600_000})
    index = json.loads(raw)
    index["status"] = True
    with pytest.raises(DomainError):
        load_result(artifacts, json.dumps(index).encode(), "task", "run")


@pytest.mark.parametrize("layer", ["manifest", "checkpoint", "section"])
def test_every_manifest_layer_is_task_and_run_bound(tmp_path: Path, layer: str) -> None:
    artifacts = ArtifactStore(tmp_path / "cas")
    _, raw = _encoded(artifacts)
    index = json.loads(raw)
    manifest = json.loads(artifacts.read(index[KEY]["manifest_sha256"]))
    target = manifest if layer == "manifest" else manifest["checkpoint"]
    if layer == "section":
        target = target["sections"]["result"]
    target["run_id"] = "other-run"
    with pytest.raises(DomainError) as error:
        load_result(artifacts, _replace_manifest(artifacts, index, manifest), "task", "run")
    assert error.value.code in {"RESULT_SCOPE_DENIED", "CHECKPOINT_SCOPE_DENIED"}


@pytest.mark.parametrize("change", ["type", "name", "bytes", "chunks", "payload_hash"])
def test_changed_type_size_chunk_or_payload_binding_is_rejected(
    tmp_path: Path, change: str
) -> None:
    artifacts = ArtifactStore(tmp_path / "cas")
    _, raw = _encoded(artifacts)
    index = json.loads(raw)
    manifest = json.loads(artifacts.read(index[KEY]["manifest_sha256"]))
    section = manifest["checkpoint"]["sections"]["result"]
    if change == "type":
        section["json_type"] = "array"
    elif change == "name":
        section["name"] = "another"
    elif change == "bytes":
        section["bytes"] += 1
    elif change == "chunks":
        section["chunks"].reverse()
    else:
        index[KEY]["payload_sha256"] = "0" * 64
    with pytest.raises(DomainError):
        load_result(artifacts, _replace_manifest(artifacts, index, manifest), "task", "run")


@pytest.mark.parametrize("layer", ["manifest", "chunk"])
def test_missing_and_corrupt_cas_objects_fail_closed(tmp_path: Path, layer: str) -> None:
    artifacts = ArtifactStore(tmp_path / "cas")
    _, raw = _encoded(artifacts)
    index = json.loads(raw)
    content_hash = index[KEY]["manifest_sha256"]
    if layer == "chunk":
        manifest = json.loads(artifacts.read(content_hash))
        content_hash = manifest["checkpoint"]["sections"]["result"]["chunks"][0]
    original = artifacts.read(content_hash)
    artifacts.path(content_hash).unlink()
    with pytest.raises(DomainError) as missing:
        load_result(artifacts, raw, "task", "run")
    assert missing.value.code == "ARTIFACT_MISSING"
    artifacts.path(content_hash).write_bytes(original + b"corrupt")
    with pytest.raises(DomainError) as corrupt:
        load_result(artifacts, raw, "task", "run")
    assert corrupt.value.code == "ARTIFACT_CORRUPTED"


@pytest.mark.parametrize("raw", [b"[]", b"null", b"not-json", b'{"bad": NaN}'])
def test_invalid_inline_data_is_rejected(tmp_path: Path, raw: bytes) -> None:
    with pytest.raises(DomainError):
        load_result(ArtifactStore(tmp_path / "cas"), raw, "task", "run")


@pytest.mark.parametrize(
    "payload", [{1: "non-string key"}, {"tuple": (1, 2)}, {"bad": float("nan")}]
)
def test_dump_rejects_values_json_would_coerce_or_cannot_restore(tmp_path: Path, payload) -> None:
    with pytest.raises(DomainError) as error:
        dump_result(ArtifactStore(tmp_path / "cas"), payload, "task", "run")
    assert error.value.code == "RESULT_INVALID"


def test_projection_and_total_size_are_bounded_before_cas_writes(tmp_path: Path) -> None:
    artifacts = ArtifactStore(tmp_path / "cas")
    with pytest.raises(DomainError) as projection:
        dump_result(artifacts, {"independent_gate": {"log": "a" * 600_000}}, "task", "run")
    assert projection.value.code == "RESULT_BOUNDS"
    assert not list(artifacts.root.glob("*/*"))


def test_unknown_index_fields_and_invalid_manifest_schema_are_rejected(tmp_path: Path) -> None:
    artifacts = ArtifactStore(tmp_path / "cas")
    _, raw = _encoded(artifacts)
    index = json.loads(raw)
    changed = copy.deepcopy(index)
    changed["unexpected"] = "value"
    with pytest.raises(DomainError):
        load_result(artifacts, json.dumps(changed).encode(), "task", "run")
    manifest = json.loads(artifacts.read(index[KEY]["manifest_sha256"]))
    manifest["schema_version"] = "untrusted-manifest-v1"
    with pytest.raises(DomainError):
        load_result(artifacts, _replace_manifest(artifacts, index, manifest), "task", "run")


def _real_pg_workspace(engine, tmp_path, *, profile=None):
    source = tmp_path / "source"
    source.mkdir()
    (source / "code.py").write_text("value = 1\n")
    registry = WorkspaceRegistry(tmp_path / "registry")
    registration = registry.register_local(source, actor="owner")
    workspace = registry.create_task_workspace(registration, "result-cas-pg")
    limits = profile.resources.budget_limits() if profile else BudgetLimits()
    grant = CapabilityGrant(
        actor="owner",
        workspace_id=registration.workspace_id,
        repository=registration.repository,
        read_paths=profile.read_paths if profile else ("",),
        write_paths=profile.write_paths if profile else (),
        max_seconds=limits.max_seconds,
    )
    request = {
        "project_id": registration.workspace_id,
        "objective": profile.objective if profile else "read",
        "task_kind": "inspect",
        "mode": "fixture",
        "workspace_root": str(source),
    }
    store = engine.harness
    store.create(
        workspace.manifest.task_id, registration, request, grant, limits, evaluation_profile=profile
    )
    row = store.begin(workspace.manifest.task_id)
    store.record_tree(row["task_id"], row["epoch"], workspace.tree_hash())
    return registry, registration, workspace, store, row


def test_real_pg_large_unicode_known_tool_receipt_restores_without_resending(
    engine,
    tmp_path: Path,
) -> None:
    registry, registration, workspace, store, row = _real_pg_workspace(engine, tmp_path)
    receipt = {"stdout": "汉" * 349_000, "status": "ok", "nested": [True, None, {"value": "原始"}]}
    assert len(json.dumps(receipt).encode()) > 2_000_000
    dispatched = []

    def dispatch():
        dispatched.append("once")
        return receipt

    io = RecoveryIO(store, row["task_id"], row["epoch"], workspace, run_id=str(row["run_id"]))
    first = io.invoke(
        "unicode-tool",
        "tool",
        {"fixture": "unicode"},
        dispatch,
        encode=lambda item: item,
        decode=lambda item: item,
    )
    assert first == receipt
    operation = store.operation(row["task_id"], "unicode-tool")
    assert operation["state"] == "known" and operation["receipt"] == receipt
    assert len(engine.artifacts.read(operation["result_hash"])) < 64_000
    store.pause(row["task_id"])
    engine.recover()
    resumed = store.claim(row["task_id"])
    assert resumed["run_id"] == row["run_id"] and resumed["epoch"] > row["epoch"]
    reopened = registry.open_task_workspace(registration, row["task_id"])
    recovered = RecoveryIO(
        store,
        row["task_id"],
        resumed["epoch"],
        reopened,
        artifacts=ArtifactStore(engine.artifacts.root),
        run_id=str(resumed["run_id"]),
    )
    second = recovered.invoke(
        "unicode-tool",
        "tool",
        {"fixture": "unicode"},
        dispatch,
        encode=lambda item: item,
        decode=lambda item: item,
    )
    assert second == receipt and dispatched == ["once"]
    changed = {**operation, "receipt": {**receipt, "status": "changed"}}
    with pytest.raises(DomainError) as mismatch:
        recovered._verified_receipt(changed)
    assert mismatch.value.code == "RECOVERY_BLOCKED"
    with pytest.raises(DomainError) as wrong_scope:
        load_result(
            engine.artifacts,
            engine.artifacts.read(operation["result_hash"]),
            row["task_id"],
            "another-run",
        )
    assert wrong_scope.value.code == "RESULT_SCOPE_DENIED"


def test_real_pg_large_final_index_finish_checks_sealed_independent_gate(
    engine,
    tmp_path: Path,
) -> None:
    frozen = freeze(engine)
    _, _, workspace, store, row = _real_pg_workspace(engine, tmp_path, profile=frozen)
    tree = workspace.tree_hash()
    store.seal_evaluation(row["task_id"], row["epoch"], tree)
    payload = {
        **_payload(),
        "task_id": row["task_id"],
        "run_id": str(row["run_id"]),
        "evaluation_profile": row["evaluation_profile"],
        "tree_after": tree,
        "independent_gate": {"status": "PASS", "tree_hash": tree},
    }
    raw = dump_result(engine.artifacts, payload, row["task_id"], str(row["run_id"]))
    index = json.loads(raw)
    for field in (
        "task_id",
        "run_id",
        "evaluation_profile",
        "status",
        "candidate_status",
        "tree_after",
        "independent_gate",
    ):
        assert index[field] == payload[field]
    invalid = copy.deepcopy(payload)
    invalid["independent_gate"]["tree_hash"] = "0" * 64
    invalid_hash = engine.artifacts.put(
        dump_result(
            engine.artifacts,
            invalid,
            row["task_id"],
            str(row["run_id"]),
        )
    )
    with pytest.raises(DomainError) as rejected:
        store.finish(row["task_id"], row["epoch"], "completed", invalid_hash, tree)
    assert rejected.value.code == "EVALUATION_JUDGE_REQUIRED"
    assert store.get(row["task_id"])["status"] == "running"
    missing_manifest = copy.deepcopy(index)
    missing_manifest[KEY]["manifest_sha256"] = "0" * 64
    missing_manifest_hash = engine.artifacts.put(json.dumps(missing_manifest).encode())
    with pytest.raises(DomainError) as missing:
        store.finish(row["task_id"], row["epoch"], "completed", missing_manifest_hash, tree)
    assert missing.value.code == "ARTIFACT_MISSING"
    assert store.get(row["task_id"])["status"] == "running"
    missing_chunk = copy.deepcopy(index)
    manifest = json.loads(engine.artifacts.read(index[KEY]["manifest_sha256"]))
    manifest["checkpoint"]["sections"]["result"]["chunks"][0] = "0" * 64
    missing_chunk_hash = engine.artifacts.put(
        _replace_manifest(engine.artifacts, missing_chunk, manifest)
    )
    with pytest.raises(DomainError) as missing_section:
        store.finish(row["task_id"], row["epoch"], "completed", missing_chunk_hash, tree)
    assert missing_section.value.code == "ARTIFACT_MISSING"
    assert store.get(row["task_id"])["status"] == "running"
    content_hash = engine.artifacts.put(raw)
    finished = store.finish(row["task_id"], row["epoch"], "completed", content_hash, tree)
    assert finished["status"] == "succeeded" and finished["result_hash"] == content_hash
    assert (
        load_result(
            ArtifactStore(engine.artifacts.root),
            engine.artifacts.read(content_hash),
            row["task_id"],
            str(row["run_id"]),
        )
        == payload
    )
