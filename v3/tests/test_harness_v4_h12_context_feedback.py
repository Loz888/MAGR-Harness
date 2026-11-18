"""Recoverable page arguments preserve current scope and durable provenance."""

from __future__ import annotations

import base64
import json

import pytest
from test_harness_v4_h10_tools import _call, _fixture

from mgar_v3.domain import DomainError
from mgar_v3.harness.broker import issue_lease
from mgar_v3.harness.context_tools import ScopedContextTools
from mgar_v3.harness.contracts import (
    CapabilityGrant,
    TaskBinding,
    ToolContext,
    VerificationRequirement,
)
from mgar_v3.harness.recovery_io import RecoveryIO
from mgar_v3.harness.service import CodingTaskRequest, HarnessCodingService


def test_compact_reference_scope_preserves_old_revisions_and_legacy_restore(tmp_path, monkeypatch):
    from test_harness_v4_h10 import _context_fixture

    from mgar_v3.harness.long_context import WorkingState

    context, task, state = _context_fixture(tmp_path)
    artifact = context.artifacts.put(b"original bytes")
    current = {
        "task_id": state.task_id, "workspace_id": state.workspace_id,
        "acceptance_revision": state.acceptance_revision, "path": "source.py",
        "tree": state.tree, "artifact_hash": artifact, "kind": "source",
    }
    old = {**current, "acceptance_revision": "previous-acceptance"}
    state = state.model_copy(update={"artifact_refs": (current, old),
                                     "artifact_hashes": (artifact,)})
    projected = state.context_payload()["artifact_refs"]
    assert "task_id" not in projected[0] and "workspace_id" not in projected[0]
    assert "acceptance_revision" not in projected[0]
    assert projected[1]["acceptance_revision"] == "previous-acceptance"
    assert all(ref["artifact_hash"] == artifact for ref in projected)
    original = WorkingState.context_payload
    with monkeypatch.context() as patch:
        patch.setattr(WorkingState, "context_payload",
                      lambda self, **kw: original(self, legacy_scope=True))
        context.prepare(task, (), (), state)
    restored = context.load(context.state_path, state)
    assert restored.working_state == state
    context.prepare(task, (), (), state)
    assert context.load(context.state_path, state).working_state == state


@pytest.mark.parametrize(
    "extra",
    [
        {"cursor": 1130},
        {"cursor": "0"},
        {"cursor": "offset:20000"},
        {"cursor": ""},
        {"offset": 20000},
        {"unexpected": "ignored, never executed"},
    ],
)
def test_bad_page_parameters_offer_bounded_authorized_correction(tmp_path, extra):
    tools, context, _, _ = _fixture(tmp_path)
    text = "汉字🙂original\n" * 100
    ref = tools.archive(text, context, path="code.py")
    call = _call(path="code.py", artifact_hash=ref["artifact_hash"], **extra)
    with pytest.raises(DomainError) as rejected:
        tools.page(call, context)
    assert rejected.value.code in {"CONTEXT_CURSOR_INVALID", "HARNESS_TOOL_ARGUMENT"}
    correction = tools.recover_page(call, context)
    assert correction["page"]["offset"] == 0
    assert len(correction["page"]["text"].encode()) <= 128
    assert correction["retry_arguments"] == {
        "path": "code.py",
        "artifact_hash": ref["artifact_hash"],
    }
    following = tools.page(_call(**correction["next_arguments"]), context)
    assert following.data["offset"] == correction["page"]["end_offset"]
    assert text.startswith(correction["page"]["text"] + following.data["text"])


def test_cursor_bound_to_another_hash_stays_denied_even_with_other_argument_errors(tmp_path):
    tools, context, _, _ = _fixture(tmp_path)
    first = tools.archive("first" * 100, context, path="code.py")
    second = tools.archive("second" * 100, context, path="code.py")
    page = tools.page(_call(path="code.py", artifact_hash=second["artifact_hash"]), context)
    call = _call(
        path="code.py",
        artifact_hash=first["artifact_hash"],
        cursor=page.data["continuation"],
        offset=20000,
    )
    with pytest.raises(DomainError) as error:
        tools.recover_page(call, context)
    assert error.value.code == "CONTEXT_ARTIFACT_SCOPE_DENIED"


def test_well_encoded_out_of_range_offset_is_corrected_without_honoring_it(tmp_path):
    tools, context, _, _ = _fixture(tmp_path)
    ref = tools.archive("source" * 100, context, path="code.py")
    token = (
        base64.urlsafe_b64encode(
            json.dumps(
                {
                    "hash": ref["artifact_hash"],
                    "offset": 20000,
                }
            ).encode()
        )
        .decode()
        .rstrip("=")
    )
    correction = tools.recover_page(
        _call(
            path="code.py",
            artifact_hash=ref["artifact_hash"],
            cursor=token,
        ),
        context,
    )
    assert correction["page"]["offset"] == 0


@pytest.mark.parametrize(
    "failure", ["unissued", "wrong_path", "other_task", "new_grant", "revoked"]
)
def test_page_correction_never_grants_scope(tmp_path, failure):
    tools, context, authority, _ = _fixture(tmp_path)
    ref = tools.archive("source" * 100, context, path="code.py")
    arguments = {"path": "code.py", "artifact_hash": ref["artifact_hash"], "cursor": "0"}
    if failure == "unissued":
        arguments["artifact_hash"] = tools.artifacts.put(b"unissued bytes")
    elif failure == "wrong_path":
        arguments["path"] = "private.py"
    elif failure == "other_task":
        context = context.model_copy(
            update={"task": context.task.model_copy(update={"task_id": "other"})}
        )
    elif failure == "new_grant":
        context = context.model_copy(
            update={"grant": context.grant.model_copy(update={"read_paths": ("code.py",)})}
        )
    elif failure == "revoked":
        authority.revoke()
    with pytest.raises(DomainError) as error:
        tools.recover_page(_call(**arguments), context)
    assert error.value.status == 403


def test_alias_and_canonical_target_both_require_grant_and_lease(tmp_path):
    tools, context, authority, lease = _fixture(tmp_path)
    alias = tools.workspace.root / "alias.py"
    alias.symlink_to("code.py")
    context = context.model_copy(update={"workspace_tree": tools.workspace.snapshot().tree_hash})
    ref = tools.archive("source" * 100, context, path="alias.py")
    assert (
        tools.page(_call(path="alias.py", artifact_hash=ref["artifact_hash"]), context).status
        == "ok"
    )
    narrowed = context.model_copy(
        update={"grant": context.grant.model_copy(update={"read_paths": ("alias.py",)})}
    )
    with pytest.raises(DomainError):
        tools._check(narrowed, ("alias.py",))
    authority.replace(
        issue_lease(
            mode="investigate",
            actor=lease.actor,
            workspace_id=lease.workspace_id,
            repository=lease.repository,
            execution_epoch=lease.execution_epoch,
            allowed_commands=(),
            read_paths=("alias.py",),
        )
    )
    with pytest.raises(DomainError):
        tools._check(context, ("alias.py",))


def test_file_search_checks_canonical_descriptor_and_revocation_before_read(tmp_path, monkeypatch):
    tools, context, authority, _ = _fixture(tmp_path)
    root = tools.workspace.root
    (root / "alias.py").symlink_to("code.py")
    context = context.model_copy(update={"workspace_tree": tools.workspace.snapshot().tree_hash})
    result = tools.search(_call("search_files", path="alias.py", query="needle"), context)
    assert result.data["matches"][0]["path"] == "code.py"
    original = tools.workspace.read

    def revoke_before_descriptor(path, *, authorize=None):
        authority.revoke()
        return original(path, authorize=authorize)

    monkeypatch.setattr(tools.workspace, "read", revoke_before_descriptor)
    with pytest.raises(DomainError) as error:
        tools.search(_call("search_files", path="alias.py", query="value"), context)
    assert error.value.code == "HARNESS_LEASE_REVOKED"


def test_directory_search_authorizes_descendants_before_their_bytes_are_read(tmp_path, monkeypatch):
    tools, context, _, _ = _fixture(tmp_path)
    observed = []
    original = tools.workspace.read

    def observe(path, *, authorize=None):
        def record(canonical):
            assert authorize is not None
            authorize(canonical)
            observed.append(canonical)

        return original(path, authorize=record)

    monkeypatch.setattr(tools.workspace, "read", observe)
    result = tools.search(_call("search_files", path="", query="needle"), context)
    assert [m["path"] for m in result.data["matches"]] == ["code.py"]
    assert observed == ["code.py"]


def pg_context(engine, tmp_path):
    source = tmp_path / "pg-source"
    source.mkdir()
    (source / "code.py").write_text("needle\n" * 200)
    service = HarnessCodingService(
        engine.settings.harness_root,
        actor="owner",
        image_lock=engine.settings.harness_image_lock,
        engine=engine,
    )
    project = service.register_project(str(source))
    runtime = service._prepare(
        CodingTaskRequest(
            project_id=project["project_id"],
            objective="Inspect code.py",
            task_kind="inspect",
            target_path="code.py",
        )
    )
    store = engine.harness
    row = store.begin(runtime.task_id)
    workspace = runtime.workspace
    task = TaskBinding(
        task_id=runtime.task_id,
        thread_id=row["thread_id"],
        run_id=row["run_id"],
        actor="owner",
        repository=workspace.manifest.repository,
        base_commit=workspace.manifest.base_commit,
        workspace_id=workspace.manifest.workspace_id,
        execution_epoch=row["epoch"],
        acceptance_revision="context-recovery-v1",
        objective="Inspect code.py",
        task_kind="inspect",
        requirements=(VerificationRequirement(requirement_id="evidence"),),
    )
    grant = CapabilityGrant.model_validate(row["grant_scope"])
    context = ToolContext(
        task=task,
        grant=grant,
        operation_id=runtime.task_id + ":tool:1:1",
        workspace_tree=workspace.snapshot().tree_hash,
    )
    io = RecoveryIO(
        store,
        runtime.task_id,
        row["epoch"],
        workspace,
        artifacts=engine.artifacts,
        run_id=str(row["run_id"]),
    )
    return service, workspace, context, io


@pytest.mark.parametrize("kind", ["source", "log", "baseline", "reconciled_command"])
def test_real_pg_known_receipt_restores_pagination_after_new_epoch(engine, tmp_path, kind):
    from mgar_v3.harness.broker import CapabilityAuthority
    from mgar_v3.harness.contracts import (
        ExecutionProvenance,
        ExecutionReceipt,
        HarnessToolResult,
        canonical_hash,
        sha256_text,
    )

    _, workspace, context, io = pg_context(engine, tmp_path)
    lease = issue_lease(
        mode="investigate",
        actor=context.task.actor,
        workspace_id=context.task.workspace_id,
        repository=context.task.repository,
        execution_epoch=context.task.execution_epoch,
        allowed_commands=(),
    )
    tools = ScopedContextTools(
        workspace, CapabilityAuthority(lease, budget_check=lambda: True), page_bytes=128
    )
    path = "code.py" if kind == "source" else ""
    text = "needle\n" * 200
    origin = ExecutionProvenance(
        operation_id=context.operation_id,
        lease_id=lease.lease_id,
        lease_hash=lease.lease_hash,
        actor=context.task.actor,
        workspace_id=context.task.workspace_id,
        repository=context.task.repository,
        execution_epoch=context.task.execution_epoch,
        read_paths=lease.read_paths,
        write_paths=lease.write_paths,
        protected_paths=lease.protected_paths,
        grant_hash=canonical_hash(context.grant.model_dump(mode="json")),
    )
    reference = (
        tools.archive(text, context, path=path)
        if kind == "source"
        else tools.archive_command_log(text, context, provenance=origin, complete=False)
    )
    if kind in {"baseline", "reconciled_command"}:
        execution = ExecutionReceipt(
            receipt_id="a" * 64,
            operation_id=context.operation_id,
            purpose="test",
            status="failed",
            argv=("pytest",),
            cwd=".",
            exit_code=1,
            stdout=text,
            stderr="",
            duration_seconds=0.1,
            tree_hash=context.workspace_tree,
            log_sha256=sha256_text(text),
            environment_hash="b" * 64,
            provenance=origin,
        )
        if kind == "baseline":
            io.invoke(
                context.operation_id,
                "baseline",
                {"argv": ["pytest"]},
                lambda: execution,
                encode=lambda r: r.model_dump(mode="json"),
                decode=ExecutionReceipt.model_validate,
            )
        else:
            # Reconciliation can create the outer known receipt before a
            # registry log artifact was issued. Its PG proof selects the bytes.
            call = _call("run_tests", argv=["pytest"])
            io.invoke(
                context.operation_id,
                "tool",
                call.model_dump(mode="json"),
                lambda: HarnessToolResult(
                    call_id=call.call_id,
                    status="error",
                    content="reconciled",
                    data={
                        "receipt": execution.model_dump(mode="json"),
                        "truncated": True,
                        "total_log_bytes": 9999,
                        "dropped_bytes": 10,
                    },
                ),
                encode=lambda r: r.model_dump(mode="json"),
                decode=HarnessToolResult.model_validate,
            )
    else:
        call = (
            _call("read_file", path=path)
            if kind == "source"
            else _call("run_tests", argv=["pytest"])
        )
        io.invoke(
            context.operation_id,
            "tool",
            call.model_dump(mode="json"),
            lambda: HarnessToolResult(
                call_id=call.call_id,
                status="ok",
                content="read",
                data={"artifact": reference},
            ),
            encode=lambda r: r.model_dump(mode="json"),
            decode=HarnessToolResult.model_validate,
            read_only=True,
        )
    original_page = tools.page(_call(path=path, artifact_hash=reference["artifact_hash"]), context)
    io.store.pause(context.task.task_id)
    row = io.store.claim(context.task.task_id)
    recovered_context = context.model_copy(
        update={"task": context.task.model_copy(update={"execution_epoch": row["epoch"]})}
    )
    new_lease = issue_lease(
        mode="investigate",
        actor=context.task.actor,
        workspace_id=context.task.workspace_id,
        repository=context.task.repository,
        execution_epoch=row["epoch"],
        allowed_commands=(),
    )
    recovered = ScopedContextTools(
        workspace, CapabilityAuthority(new_lease, budget_check=lambda: True), page_bytes=128
    )
    recovered_io = RecoveryIO(
        io.store,
        context.task.task_id,
        row["epoch"],
        workspace,
        artifacts=engine.artifacts,
        run_id=str(row["run_id"]),
    )
    assert recovered.restore_known(recovered_context, recovered_io) == 1
    assert recovered.restore_known(recovered_context, recovered_io) == 0
    if kind == "reconciled_command":
        assert recovered.references()[0]["complete"] is False
        assert recovered.references()[0]["total_bytes"] == 9999
    page = recovered.page(
        _call(
            path=path,
            artifact_hash=reference["artifact_hash"],
            cursor=original_page.data["continuation"],
        ),
        recovered_context,
    )
    assert page.data["offset"] == original_page.data["end_offset"]
    narrowed = recovered_context.model_copy(
        update={"grant": recovered_context.grant.model_copy(update={"read_paths": ("code.py",)})}
    )
    with pytest.raises(DomainError):
        recovered.restore_known(narrowed, recovered_io)
    operation = io.store.operations(context.task.task_id)[0]
    engine.artifacts.path(operation["result_hash"]).write_bytes(b"tampered receipt")
    with pytest.raises(DomainError):
        recovered.restore_known(recovered_context, recovered_io)
