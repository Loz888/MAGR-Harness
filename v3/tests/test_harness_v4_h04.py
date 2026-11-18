from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from mgar_v3.domain import DomainError
from mgar_v3.harness.broker import (
    CapabilityAuthority,
    IsolatedExecutionBroker,
    issue_lease,
)
from mgar_v3.harness.contracts import (
    CancellationToken,
    CapabilityGrant,
    ExecutionRequest,
    HarnessToolCall,
    TaskBinding,
    ToolContext,
    VerificationRequirement,
    WorkspaceEdit,
    sha256_text,
)

ROOT = Path(__file__).resolve().parents[1]
LOCK = ROOT / ".local/r03/sandbox-lock.json"


def _workspace(tmp_path: Path) -> Path:
    root = tmp_path / "workspace"
    root.mkdir(parents=True, exist_ok=True)
    (root / "app.py").write_text("print('ok')\n", encoding="utf-8")
    for path in [root, *root.rglob("*")]:
        path.chmod(0o777 if path.is_dir() else 0o666)
    ancestor = root.parent
    while ancestor != Path("/tmp") and ancestor.is_dir():
        ancestor.chmod(0o755)
        ancestor = ancestor.parent
    return root


def _lease(mode: str = "modify_test", **overrides: object):
    values: dict[str, object] = {
        "mode": mode,
        "actor": "owner",
        "workspace_id": "ws-h04",
        "repository": "example/project",
        "execution_epoch": 3,
        "read_paths": ("",),
        "write_paths": ("",) if mode == "modify_test" else (),
        "allowed_commands": ("id", "sh", "python3", "touch", "sleep", "cat"),
        "timeout_seconds": 30,
        "approved_by": "owner" if mode != "investigate" else "",
        "approval_ref": "approval:test" if mode != "investigate" else "",
    }
    values.update(overrides)
    return issue_lease(**values)  # type: ignore[arg-type]


def _grant(
    *,
    execute: bool = True,
    write: tuple[str, ...] = ("",),
    commands: tuple[str, ...] = ("id", "sh", "python3", "touch", "sleep", "cat"),
) -> CapabilityGrant:
    return CapabilityGrant(
        actor="owner",
        workspace_id="ws-h04",
        repository="example/project",
        read_paths=("",),
        write_paths=write,
        execute=execute,
        allowed_commands=commands,
    )


def _request(
    argv: tuple[str, ...],
    *,
    cwd: str = ".",
    timeout: float = 20,
    epoch: int = 3,
) -> ExecutionRequest:
    return ExecutionRequest(
        operation_id="h04-op",
        purpose="command",
        argv=argv,
        cwd=cwd,
        timeout_seconds=timeout,
        execution_epoch=epoch,
    )


def _broker(root: Path, lease, **kwargs: object) -> IsolatedExecutionBroker:
    if "budget_check" not in kwargs:
        kwargs["budget_check"] = lambda: True
    return IsolatedExecutionBroker(root, lease, **kwargs)  # type: ignore[arg-type]


def test_lease_presets_fail_closed() -> None:
    investigate = _lease("investigate")
    assert investigate.write_paths == ()
    assert investigate.external_write is False
    with pytest.raises(DomainError):
        issue_lease(
            mode="modify_test",
            actor="owner",
            workspace_id="ws",
            repository="example/project",
            execution_epoch=1,
            write_paths=("",),
            allowed_commands=("sh",),
        )
    with pytest.raises(DomainError):
        issue_lease(
            mode="investigate",
            actor="owner",
            workspace_id="ws",
            repository="example/project",
            execution_epoch=1,
            write_paths=("src",),
            allowed_commands=("sh",),
        )
    publish = _lease("publish")
    assert publish.external_write is True


def test_readonly_grant_cannot_execute(tmp_path: Path) -> None:
    broker = _broker(_workspace(tmp_path), _lease())
    with pytest.raises(DomainError) as error:
        broker.execute(_request(("id",)), _grant(execute=False), CancellationToken())
    assert error.value.code == "HARNESS_EXECUTE_DENIED"


def test_stale_epoch_scope_command_and_budget_are_denied(tmp_path: Path) -> None:
    broker = _broker(_workspace(tmp_path), _lease())
    with pytest.raises(DomainError) as epoch_error:
        broker.execute(_request(("id",), epoch=2), _grant(), CancellationToken())
    assert epoch_error.value.code == "HARNESS_EPOCH_MISMATCH"
    with pytest.raises(DomainError) as scope_error:
        broker.execute(_request(("id",), cwd="../outside"), _grant(), CancellationToken())
    assert scope_error.value.code in {"HARNESS_PATH_DENIED", "HARNESS_READ_DENIED"}
    with pytest.raises(DomainError) as command_error:
        broker.execute(_request(("curl", "https://example.invalid")), _grant(), CancellationToken())
    assert command_error.value.code == "HARNESS_COMMAND_DENIED"
    blocked = _broker(_workspace(tmp_path), _lease(), budget_check=lambda: False)
    with pytest.raises(DomainError) as budget_error:
        blocked.execute(_request(("id",)), _grant(), CancellationToken())
    assert budget_error.value.code == "HARNESS_BUDGET_EXHAUSTED"


def test_publish_and_dependency_phases_are_separate(tmp_path: Path) -> None:
    publish = _broker(_workspace(tmp_path), _lease("publish"))
    with pytest.raises(DomainError) as error:
        publish.execute(_request(("id",)), _grant(), CancellationToken())
    assert error.value.code == "HARNESS_EXTERNAL_WRITE_DENIED"
    broker = _broker(_workspace(tmp_path), _lease())
    with pytest.raises(DomainError) as dependency_error:
        broker.prepare_dependencies({}, _grant(), CancellationToken())
    assert dependency_error.value.code == "HARNESS_DEPENDENCY_SOURCE_DENIED"


@pytest.mark.skipif(not LOCK.is_file(), reason="pinned R03 sandbox is not built")
def test_real_container_runs_nonroot_with_limits_and_no_credentials(tmp_path: Path) -> None:
    broker = _broker(
        _workspace(tmp_path),
        _lease(),
        credentials={"DEEPSEEK_API_KEY": "must-not-appear"},
    )
    result = broker.execute(
        _request(
            (
                "sh",
                "-c",
                "id -u; ulimit -n; printf '%s\\n' \"${DEEPSEEK_API_KEY:-absent}\"; "
                "if [ -e /home/developer ]; then echo host-home-visible; "
                "else echo host-home-hidden; fi",
            )
        ),
        _grant(),
        CancellationToken(),
    )
    assert result.status == "passed"
    assert result.stdout.splitlines()[:3] == ["65532", "128", "absent"]
    assert result.stdout.splitlines()[3] == "host-home-hidden"


@pytest.mark.skipif(not LOCK.is_file(), reason="pinned R03 sandbox is not built")
def test_real_container_has_no_network(tmp_path: Path) -> None:
    broker = _broker(_workspace(tmp_path), _lease())
    result = broker.execute(
        _request(
            (
                "python3",
                "-c",
                "import socket; socket.create_connection(('1.1.1.1', 53), 1)",
            )
        ),
        _grant(),
        CancellationToken(),
    )
    assert result.status == "failed"
    assert result.exit_code != 0


@pytest.mark.skipif(not LOCK.is_file(), reason="pinned R03 sandbox is not built")
def test_investigate_mount_is_readonly_and_modify_writes_workspace(tmp_path: Path) -> None:
    readonly_root = _workspace(tmp_path / "ro")
    investigate = _broker(readonly_root, _lease("investigate"))
    denied = investigate.execute(_request(("touch", "blocked.txt")), _grant(), CancellationToken())
    assert denied.status == "failed"
    assert not (readonly_root / "blocked.txt").exists()

    writable_root = _workspace(tmp_path / "rw")
    modify = _broker(writable_root, _lease())
    allowed = modify.execute(
        _request(("sh", "-c", "echo hello > inside.txt")),
        _grant(),
        CancellationToken(),
    )
    assert allowed.status == "passed"
    assert (writable_root / "inside.txt").read_text(encoding="utf-8") == "hello\n"
    outside = modify.execute(
        _request(("touch", "/etc/mgar-h04-escape")), _grant(), CancellationToken()
    )
    assert outside.status == "failed"
    assert not Path("/etc/mgar-h04-escape").exists()


@pytest.mark.skipif(not LOCK.is_file(), reason="pinned R03 sandbox is not built")
def test_timeout_is_enforced(tmp_path: Path) -> None:
    broker = _broker(_workspace(tmp_path), _lease(timeout_seconds=10))
    result = broker.execute(_request(("sleep", "5"), timeout=1), _grant(), CancellationToken())
    assert result.status == "timeout"
    assert result.exit_code == 124
    assert result.duration_seconds < 5


def test_expired_and_wrong_actor_leases_are_denied(tmp_path: Path) -> None:
    expired = _broker(_workspace(tmp_path), _lease(ttl_seconds=-1))
    with pytest.raises(DomainError) as error:
        expired.execute(_request(("id",)), _grant(), CancellationToken())
    assert error.value.code == "HARNESS_LEASE_EXPIRED"
    broker = _broker(_workspace(tmp_path), _lease())
    wrong = CapabilityGrant(
        actor="intruder",
        workspace_id="ws-h04",
        repository="example/project",
        read_paths=("",),
        write_paths=(),
        execute=True,
    )
    with pytest.raises(DomainError) as actor_error:
        broker.execute(_request(("id",)), wrong, CancellationToken())
    assert actor_error.value.code == "HARNESS_ACTOR_MISMATCH"


def test_old_grant_scopes_and_commands_are_intersected(tmp_path: Path) -> None:
    broker = _broker(_workspace(tmp_path), _lease())
    with pytest.raises(DomainError) as write_error:
        broker.execute(_request(("id",)), _grant(write=()), CancellationToken())
    assert write_error.value.code == "HARNESS_WRITE_DENIED"
    with pytest.raises(DomainError) as command_error:
        broker.execute(_request(("id",)), _grant(commands=()), CancellationToken())
    assert command_error.value.code == "HARNESS_COMMAND_DENIED"


@pytest.mark.skipif(not LOCK.is_file(), reason="pinned R03 sandbox is not built")
def test_container_read_scope_hides_siblings_and_metadata(tmp_path: Path) -> None:
    root = _workspace(tmp_path)
    (root / "allowed").mkdir()
    (root / "allowed" / "ok.txt").write_text("ALLOWED_OK\n", encoding="utf-8")
    (root / "protected").mkdir()
    (root / "protected" / "secret.txt").write_text("SECRET_SIBLING\n", encoding="utf-8")
    (root / ".git").mkdir()
    (root / ".git" / "config").write_text("GIT_CONFIG_SECRET\n", encoding="utf-8")
    (root / ".mgar").mkdir()
    (root / ".mgar" / "manifest.json").write_text("MANIFEST_SECRET\n", encoding="utf-8")
    for path in [root, *root.rglob("*")]:
        path.chmod(0o777 if path.is_dir() else 0o666)
    lease = _lease("investigate", read_paths=("allowed",))
    broker = _broker(root, lease)
    result = broker.execute(
        _request(
            (
                "sh",
                "-c",
                "cat ok.txt; cat ../protected/secret.txt 2>/dev/null; "
                "cat ../.mgar/manifest.json 2>/dev/null; cat ../.git/config 2>/dev/null; true",
            ),
            cwd="allowed",
        ),
        _grant(),
        CancellationToken(),
    )
    assert result.status == "passed"
    assert "ALLOWED_OK" in result.stdout
    assert "SECRET_SIBLING" not in result.stdout
    assert "MANIFEST_SECRET" not in result.stdout
    assert "GIT_CONFIG_SECRET" not in result.stdout


@pytest.mark.skipif(not LOCK.is_file(), reason="pinned R03 sandbox is not built")
def test_container_write_scope_and_protected_metadata(tmp_path: Path) -> None:
    root = _workspace(tmp_path)
    (root / "allowed").mkdir()
    (root / "allowed" / "ok.txt").write_text("ALLOWED_OK\n", encoding="utf-8")
    (root / "protected").mkdir()
    (root / "protected" / "secret.txt").write_text("SECRET_SIBLING\n", encoding="utf-8")
    (root / ".git").mkdir()
    (root / ".git" / "config").write_text("GIT_CONFIG_SECRET\n", encoding="utf-8")
    (root / ".mgar").mkdir()
    (root / ".mgar" / "manifest.json").write_text("MANIFEST_SECRET\n", encoding="utf-8")
    for path in [root, *root.rglob("*")]:
        path.chmod(0o777 if path.is_dir() else 0o666)
    lease = _lease(
        "modify_test",
        read_paths=("",),
        write_paths=("allowed",),
        protected_paths=(".git", ".mgar", "protected"),
    )
    broker = _broker(root, lease)
    result = broker.execute(
        _request(
            (
                "sh",
                "-c",
                "echo changed > allowed/ok.txt; "
                "echo hacked > protected/secret.txt; "
                "echo hacked > .mgar/manifest.json; "
                "echo hacked > .git/config; true",
            )
        ),
        _grant(write=("allowed",)),
        CancellationToken(),
    )
    assert result.status == "passed"
    assert (root / "allowed" / "ok.txt").read_text(encoding="utf-8") == "changed\n"
    assert (root / "protected" / "secret.txt").read_text(encoding="utf-8") == "SECRET_SIBLING\n"
    assert (root / ".mgar" / "manifest.json").read_text(encoding="utf-8") == "MANIFEST_SECRET\n"
    assert (root / ".git" / "config").read_text(encoding="utf-8") == "GIT_CONFIG_SECRET\n"


def test_tool_registry_uses_current_authority_for_read_list_and_edit(tmp_path: Path) -> None:
    from uuid import uuid4

    from mgar_v3.harness.adapters import HarnessToolRegistry, LocalWorkspace

    root = _workspace(tmp_path)
    (root / "allowed").mkdir()
    (root / "allowed" / "ok.txt").write_text("ok\n", encoding="utf-8")
    (root / "secret.txt").write_text("secret\n", encoding="utf-8")
    lease = _lease(
        "modify_test",
        read_paths=("allowed",),
        write_paths=("allowed",),
    )
    authority = CapabilityAuthority(lease, budget_check=lambda: True)
    workspace = LocalWorkspace(root, workspace_id="ws-h04")
    broker = _broker(root, authority)
    registry = HarnessToolRegistry(workspace, broker, authority)
    task = TaskBinding(
        task_id="h04-task",
        thread_id=uuid4(),
        run_id=uuid4(),
        execution_epoch=3,
        actor="owner",
        repository="example/project",
        base_commit="a" * 40,
        workspace_id="ws-h04",
        acceptance_revision="1",
        objective="run tools",
        requirements=(VerificationRequirement(requirement_id="unit"),),
    )
    grant = _grant(write=("allowed",))
    context = ToolContext(
        task=task,
        grant=grant,
        operation_id="h04-op",
        workspace_tree=workspace.snapshot().tree_hash,
    )
    read = registry.invoke(
        HarnessToolCall(call_id="1", name="read_file", arguments={"path": "allowed/ok.txt"}),
        context,
        CancellationToken(),
    )
    assert read.status == "ok"
    denied = registry.invoke(
        HarnessToolCall(call_id="2", name="read_file", arguments={"path": "secret.txt"}),
        context,
        CancellationToken(),
    )
    assert denied.status == "denied"
    listing = registry.invoke(
        HarnessToolCall(call_id="3", name="list_dir", arguments={"path": "allowed"}),
        context,
        CancellationToken(),
    )
    assert listing.status == "ok"
    edit = WorkspaceEdit(
        path="allowed/ok.txt",
        content="changed\n",
        expected_sha256=sha256_text("ok\n"),
    )
    applied = registry.invoke(
        HarnessToolCall(
            call_id="4",
            name="apply_edits",
            arguments={
                "edits": [edit.model_dump(mode="json")],
                "expected_tree": workspace.snapshot().tree_hash,
            },
        ),
        context,
        CancellationToken(),
    )
    assert applied.status == "ok"
    authority.revoke()
    blocked = registry.invoke(
        HarnessToolCall(call_id="5", name="read_file", arguments={"path": "allowed/ok.txt"}),
        context,
        CancellationToken(),
    )
    assert blocked.status == "denied"
    assert blocked.code == "HARNESS_LEASE_REVOKED"


def test_tool_authority_expiry_budget_and_epoch_are_enforced(tmp_path: Path) -> None:
    from uuid import uuid4

    from mgar_v3.harness.adapters import (
        HarnessToolRegistry,
        LocalExecutionBroker,
        LocalWorkspace,
    )

    root = _workspace(tmp_path)
    (root / "allowed").mkdir()
    (root / "allowed" / "ok.txt").write_text("ok\n", encoding="utf-8")
    workspace = LocalWorkspace(root, workspace_id="ws-h04")
    task = TaskBinding(
        task_id="h04-task",
        thread_id=uuid4(),
        run_id=uuid4(),
        execution_epoch=3,
        actor="owner",
        repository="example/project",
        base_commit="a" * 40,
        workspace_id="ws-h04",
        acceptance_revision="1",
        objective="run tools",
        requirements=(VerificationRequirement(requirement_id="unit"),),
    )
    context = ToolContext(
        task=task,
        grant=_grant(write=("allowed",)),
        operation_id="h04-op",
        workspace_tree=workspace.snapshot().tree_hash,
    )
    expired = CapabilityAuthority(
        _lease("modify_test", read_paths=("allowed",), write_paths=("allowed",), ttl_seconds=-1),
        budget_check=lambda: True,
    )
    registry = HarnessToolRegistry(workspace, LocalExecutionBroker(workspace), expired)
    denied = registry.invoke(
        HarnessToolCall(call_id="1", name="read_file", arguments={"path": "allowed/ok.txt"}),
        context,
        CancellationToken(),
    )
    assert denied.code == "HARNESS_LEASE_EXPIRED"
    no_budget = CapabilityAuthority(
        _lease("modify_test", read_paths=("allowed",), write_paths=("allowed",)),
        budget_check=lambda: False,
    )
    registry = HarnessToolRegistry(workspace, LocalExecutionBroker(workspace), no_budget)
    denied = registry.invoke(
        HarnessToolCall(call_id="2", name="read_file", arguments={"path": "allowed/ok.txt"}),
        context,
        CancellationToken(),
    )
    assert denied.code == "HARNESS_BUDGET_EXHAUSTED"
    live = CapabilityAuthority(
        _lease("modify_test", read_paths=("allowed",), write_paths=("allowed",)),
        budget_check=lambda: True,
    )
    stale_task = task.model_copy(update={"execution_epoch": 2})
    stale_context = context.model_copy(update={"task": stale_task})
    registry = HarnessToolRegistry(workspace, LocalExecutionBroker(workspace), live)
    denied = registry.invoke(
        HarnessToolCall(call_id="3", name="read_file", arguments={"path": "allowed/ok.txt"}),
        stale_context,
        CancellationToken(),
    )
    assert denied.code == "HARNESS_EPOCH_MISMATCH"


@pytest.mark.skipif(not LOCK.is_file(), reason="pinned R03 sandbox is not built")
def test_container_write_scope_cannot_reach_unmounted_siblings(tmp_path: Path) -> None:
    root = _workspace(tmp_path)
    (root / "allowed").mkdir()
    (root / "allowed" / "ok.txt").write_text("ALLOWED_OK\n", encoding="utf-8")
    (root / "protected").mkdir()
    (root / "protected" / "secret.txt").write_text("SECRET_SIBLING\n", encoding="utf-8")
    (root / ".git").mkdir()
    (root / ".git" / "config").write_text("GIT_CONFIG_SECRET\n", encoding="utf-8")
    (root / ".mgar").mkdir()
    (root / ".mgar" / "manifest.json").write_text("MANIFEST_SECRET\n", encoding="utf-8")
    for path in [root, *root.rglob("*")]:
        path.chmod(0o777 if path.is_dir() else 0o666)
    lease = _lease("modify_test", read_paths=("allowed",), write_paths=("allowed",))
    broker = _broker(root, lease)
    result = broker.execute(
        _request(
            (
                "sh",
                "-c",
                "echo changed > ok.txt; "
                "echo hacked > ../protected/secret.txt; "
                "echo hacked > ../.mgar/manifest.json; "
                "echo hacked > ../.git/config; true",
            ),
            cwd="allowed",
        ),
        _grant(write=("allowed",)),
        CancellationToken(),
    )
    assert result.status == "passed"
    assert (root / "allowed" / "ok.txt").read_text(encoding="utf-8") == "changed\n"
    assert (root / "protected" / "secret.txt").read_text(encoding="utf-8") == "SECRET_SIBLING\n"
    assert (root / ".mgar" / "manifest.json").read_text(encoding="utf-8") == "MANIFEST_SECRET\n"
    assert (root / ".git" / "config").read_text(encoding="utf-8") == "GIT_CONFIG_SECRET\n"


@pytest.mark.skipif(not LOCK.is_file(), reason="pinned R03 sandbox is not built")
def test_container_root_write_still_masks_protected_metadata(tmp_path: Path) -> None:
    root = _workspace(tmp_path)
    (root / "allowed").mkdir()
    (root / "allowed" / "ok.txt").write_text("ALLOWED_OK\n", encoding="utf-8")
    (root / "protected").mkdir()
    (root / "protected" / "secret.txt").write_text("SECRET_SIBLING\n", encoding="utf-8")
    (root / ".git").mkdir()
    (root / ".git" / "config").write_text("GIT_CONFIG_SECRET\n", encoding="utf-8")
    (root / ".mgar").mkdir()
    (root / ".mgar" / "manifest.json").write_text("MANIFEST_SECRET\n", encoding="utf-8")
    for path in [root, *root.rglob("*")]:
        path.chmod(0o777 if path.is_dir() else 0o666)
    lease = _lease(
        "modify_test",
        read_paths=("",),
        write_paths=("",),
        protected_paths=(".git", ".mgar", "protected"),
    )
    broker = _broker(root, lease)
    result = broker.execute(
        _request(
            (
                "sh",
                "-c",
                "echo changed > allowed/ok.txt; "
                "echo hacked > protected/secret.txt; "
                "echo hacked > .mgar/manifest.json; "
                "echo hacked > .git/config; true",
            )
        ),
        _grant(write=("",)),
        CancellationToken(),
    )
    assert result.status == "passed"
    assert (root / "allowed" / "ok.txt").read_text(encoding="utf-8") == "changed\n"
    assert (root / "protected" / "secret.txt").read_text(encoding="utf-8") == "SECRET_SIBLING\n"
    assert (root / ".mgar" / "manifest.json").read_text(encoding="utf-8") == "MANIFEST_SECRET\n"
    assert (root / ".git" / "config").read_text(encoding="utf-8") == "GIT_CONFIG_SECRET\n"


@pytest.mark.skipif(not LOCK.is_file(), reason="pinned R03 sandbox is not built")
def test_command_allowlist_is_an_entry_gate_not_process_tree_isolation(tmp_path: Path) -> None:
    root = _workspace(tmp_path)
    lease = _lease(
        "investigate",
        read_paths=("allowed",),
        allowed_commands=("sh",),
    )
    (root / "allowed").mkdir()
    for path in [root, *root.rglob("*")]:
        path.chmod(0o777 if path.is_dir() else 0o666)
    broker = _broker(root, lease)
    result = broker.execute(
        _request(
            (
                "sh",
                "-c",
                "python3 -c 'print(42)'; id -u; "
                "python3 -c \"from pathlib import Path; print(Path('../.mgar').exists())\"",
            ),
            cwd="allowed",
        ),
        _grant(commands=("sh",)),
        CancellationToken(),
    )
    assert result.status == "passed"
    lines = result.stdout.splitlines()
    assert lines[:2] == ["42", "65532"]
    assert lines[2] == "False"


@pytest.mark.skipif(not LOCK.is_file(), reason="pinned R03 sandbox is not built")
def test_no_h04_containers_remain_after_execution(tmp_path: Path) -> None:
    broker = _broker(_workspace(tmp_path), _lease())
    result = broker.execute(_request(("id",)), _grant(), CancellationToken())
    assert result.status == "passed"
    listed = subprocess.check_output(
        ["docker", "ps", "-aq", "--filter", "label=mgar.v4.h04.broker=1"],
        text=True,
    )
    assert listed.strip() == ""


def test_scope_intersection_uses_the_narrower_shared_root() -> None:
    from mgar_v3.harness.broker import _intersect

    assert _intersect(("",), ("allowed/sub",)) == ("allowed/sub",)
    assert _intersect(("allowed",), ("allowed/sub",)) == ("allowed/sub",)
    assert _intersect(("allowed",), ("other",)) == ()
    assert _intersect(("allowed",), ("",)) == ("allowed",)


def test_current_authority_replacement_invalidates_the_old_epoch(tmp_path: Path) -> None:
    old = _lease()
    authority = CapabilityAuthority(old, budget_check=lambda: True)
    broker = _broker(_workspace(tmp_path), authority)
    replacement = _lease(execution_epoch=4)
    authority.replace(replacement)
    assert authority.current().execution_epoch == 4
    with pytest.raises(DomainError) as error:
        broker.execute(_request(("id",), epoch=3), _grant(), CancellationToken())
    assert error.value.code == "HARNESS_EPOCH_MISMATCH"


def test_network_intent_and_missing_budget_are_fail_closed(tmp_path: Path) -> None:
    with pytest.raises(DomainError) as network_error:
        _lease(network_hosts=("example.com",))
    assert network_error.value.code == "HARNESS_LEASE_DENIED"
    broker = _broker(_workspace(tmp_path), _lease(max_calls=1))
    grant = _grant()
    grant = grant.model_copy(update={"network": True})
    with pytest.raises(DomainError) as grant_network:
        broker.execute(_request(("id",)), grant, CancellationToken())
    assert grant_network.value.code == "HARNESS_NETWORK_DENIED"
    bounded = IsolatedExecutionBroker(
        _workspace(tmp_path / "bounded"),
        _lease(max_calls=1),
    )
    first = bounded.execute(_request(("id",)), _grant(), CancellationToken())
    assert first.status == "passed"
    with pytest.raises(DomainError) as budget_error:
        bounded.execute(_request(("id",)), _grant(), CancellationToken())
    assert budget_error.value.code == "HARNESS_BUDGET_EXHAUSTED"


def test_all_builtin_tools_are_denied_without_current_authority(tmp_path: Path) -> None:
    from uuid import uuid4

    from mgar_v3.harness.adapters import (
        HarnessToolRegistry,
        LocalExecutionBroker,
        LocalWorkspace,
    )

    root = _workspace(tmp_path)
    (root / "allowed").mkdir()
    (root / "allowed" / "ok.txt").write_text("ok\n", encoding="utf-8")
    workspace = LocalWorkspace(root, workspace_id="ws-h04")
    task = TaskBinding(
        task_id="h04-task",
        thread_id=uuid4(),
        run_id=uuid4(),
        execution_epoch=3,
        actor="owner",
        repository="example/project",
        base_commit="a" * 40,
        workspace_id="ws-h04",
        acceptance_revision="1",
        objective="run tools",
        requirements=(VerificationRequirement(requirement_id="unit"),),
    )
    context = ToolContext(
        task=task,
        grant=_grant(write=("allowed",)),
        operation_id="h04-op",
        workspace_tree=workspace.snapshot().tree_hash,
    )
    edit = WorkspaceEdit(
        path="allowed/ok.txt",
        content="changed\n",
        expected_sha256=sha256_text("ok\n"),
    )
    calls = {
        "read_file": {"path": "allowed/ok.txt"},
        "list_dir": {"path": "allowed"},
        "apply_edits": {
            "edits": [edit.model_dump(mode="json")],
            "expected_tree": workspace.snapshot().tree_hash,
        },
        "run_tests": {"argv": ["python3", "-c", "print(1)"]},
    }

    def make_authority(
        *, expired: bool = False, budget: bool = True, revoke: bool = False
    ) -> CapabilityAuthority:
        authority = CapabilityAuthority(
            _lease(
                "modify_test",
                read_paths=("allowed",),
                write_paths=("allowed",),
                ttl_seconds=-1 if expired else 3600,
            ),
            budget_check=lambda: budget,
        )
        if revoke:
            authority.revoke()
        return authority

    states = [
        ("expired", make_authority(expired=True), context, "HARNESS_LEASE_EXPIRED"),
        ("revoked", make_authority(revoke=True), context, "HARNESS_LEASE_REVOKED"),
        ("budget", make_authority(budget=False), context, "HARNESS_BUDGET_EXHAUSTED"),
        (
            "epoch",
            make_authority(),
            context.model_copy(update={"task": task.model_copy(update={"execution_epoch": 2})}),
            "HARNESS_EPOCH_MISMATCH",
        ),
    ]
    for tool, arguments in calls.items():
        for label, authority, active_context, expected in states:
            registry = HarnessToolRegistry(
                workspace, LocalExecutionBroker(workspace), authority
            )
            result = registry.invoke(
                HarnessToolCall(
                    call_id=f"{tool}-{label}",
                    name=tool,
                    arguments=arguments,
                ),
                active_context,
                CancellationToken(),
            )
            assert result.code == expected, (tool, label, result)
            assert result.status in {"denied", "error"}, (tool, label, result)


def test_builtin_tools_cannot_read_or_edit_protected_metadata(tmp_path: Path) -> None:
    from uuid import uuid4

    from mgar_v3.harness.adapters import (
        HarnessToolRegistry,
        LocalExecutionBroker,
        LocalWorkspace,
    )

    root = _workspace(tmp_path)
    (root / ".mgar").mkdir()
    (root / ".mgar" / "manifest.json").write_text("MANIFEST_SECRET\n", encoding="utf-8")
    (root / ".git").mkdir()
    (root / ".git" / "config").write_text("GIT_CONFIG_SECRET\n", encoding="utf-8")
    workspace = LocalWorkspace(root, workspace_id="ws-h04")
    authority = CapabilityAuthority(
        _lease("modify_test", read_paths=("",), write_paths=("",)),
        budget_check=lambda: True,
    )
    registry = HarnessToolRegistry(workspace, LocalExecutionBroker(workspace), authority)
    task = TaskBinding(
        task_id="h04-task",
        thread_id=uuid4(),
        run_id=uuid4(),
        execution_epoch=3,
        actor="owner",
        repository="example/project",
        base_commit="a" * 40,
        workspace_id="ws-h04",
        acceptance_revision="1",
        objective="run tools",
        requirements=(VerificationRequirement(requirement_id="unit"),),
    )
    context = ToolContext(
        task=task,
        grant=_grant(write=("",)),
        operation_id="h04-op",
        workspace_tree=workspace.snapshot().tree_hash,
    )
    read = registry.invoke(
        HarnessToolCall(
            call_id="1", name="read_file", arguments={"path": ".mgar/manifest.json"}
        ),
        context,
        CancellationToken(),
    )
    assert read.code == "HARNESS_PROTECTED_PATH_DENIED"
    listing = registry.invoke(
        HarnessToolCall(call_id="2", name="list_dir", arguments={"path": ".git"}),
        context,
        CancellationToken(),
    )
    assert listing.code == "HARNESS_PROTECTED_PATH_DENIED"
    edit = WorkspaceEdit(
        path=".git/config",
        content="hacked\n",
        expected_sha256=sha256_text("GIT_CONFIG_SECRET\n"),
    )
    applied = registry.invoke(
        HarnessToolCall(
            call_id="3",
            name="apply_edits",
            arguments={
                "edits": [edit.model_dump(mode="json")],
                "expected_tree": workspace.snapshot().tree_hash,
            },
        ),
        context,
        CancellationToken(),
    )
    assert applied.code == "HARNESS_PROTECTED_PATH_DENIED"
    executed = registry.invoke(
        HarnessToolCall(
            call_id="4",
            name="run_tests",
            arguments={"argv": ["python3", "-c", "print(1)"], "cwd": ".mgar"},
        ),
        context,
        CancellationToken(),
    )
    assert executed.code == "HARNESS_PROTECTED_PATH_DENIED"
    assert (root / ".mgar" / "manifest.json").read_text(encoding="utf-8") == "MANIFEST_SECRET\n"
    assert (root / ".git" / "config").read_text(encoding="utf-8") == "GIT_CONFIG_SECRET\n"


def test_builtin_tools_resolve_symlinks_before_authorization(tmp_path: Path) -> None:
    from uuid import uuid4

    from mgar_v3.harness.adapters import (
        HarnessToolRegistry,
        LocalExecutionBroker,
        LocalWorkspace,
    )

    root = _workspace(tmp_path)
    (root / "allowed").mkdir()
    (root / "allowed" / "ok.txt").write_text("ok\n", encoding="utf-8")
    (root / "protected").mkdir()
    (root / "protected" / "secret.txt").write_text("SECRET\n", encoding="utf-8")
    (root / ".mgar").mkdir()
    (root / ".mgar" / "manifest.json").write_text("MANIFEST_SECRET\n", encoding="utf-8")
    (root / "allowed" / "sibling.txt").symlink_to("../protected/secret.txt")
    (root / "allowed" / "meta.txt").symlink_to("../.mgar/manifest.json")
    (root / "allowed" / "metadata").symlink_to("../.mgar", target_is_directory=True)
    workspace = LocalWorkspace(root, workspace_id="ws-h04")
    authority = CapabilityAuthority(
        _lease("modify_test", read_paths=("allowed",), write_paths=("allowed",)),
        budget_check=lambda: True,
    )
    registry = HarnessToolRegistry(workspace, LocalExecutionBroker(workspace), authority)
    task = TaskBinding(
        task_id="h04-task",
        thread_id=uuid4(),
        run_id=uuid4(),
        execution_epoch=3,
        actor="owner",
        repository="example/project",
        base_commit="a" * 40,
        workspace_id="ws-h04",
        acceptance_revision="1",
        objective="run tools",
        requirements=(VerificationRequirement(requirement_id="unit"),),
    )
    context = ToolContext(
        task=task,
        grant=_grant(write=("allowed",)),
        operation_id="h04-op",
        workspace_tree=workspace.snapshot().tree_hash,
    )
    sibling = registry.invoke(
        HarnessToolCall(
            call_id="1", name="read_file", arguments={"path": "allowed/sibling.txt"}
        ),
        context,
        CancellationToken(),
    )
    assert sibling.code == "HARNESS_READ_DENIED"
    metadata = registry.invoke(
        HarnessToolCall(call_id="2", name="read_file", arguments={"path": "allowed/meta.txt"}),
        context,
        CancellationToken(),
    )
    assert metadata.code == "HARNESS_PROTECTED_PATH_DENIED"
    listing = registry.invoke(
        HarnessToolCall(call_id="3", name="list_dir", arguments={"path": "allowed/metadata"}),
        context,
        CancellationToken(),
    )
    assert listing.code == "HARNESS_PROTECTED_PATH_DENIED"
    edit = WorkspaceEdit(
        path="allowed/meta.txt",
        content="hacked\n",
        expected_sha256=sha256_text("MANIFEST_SECRET\n"),
    )
    applied = registry.invoke(
        HarnessToolCall(
            call_id="4",
            name="apply_edits",
            arguments={
                "edits": [edit.model_dump(mode="json")],
                "expected_tree": workspace.snapshot().tree_hash,
            },
        ),
        context,
        CancellationToken(),
    )
    assert applied.code == "HARNESS_PROTECTED_PATH_DENIED"
    assert (root / ".mgar" / "manifest.json").read_text(encoding="utf-8") == "MANIFEST_SECRET\n"


def test_builtin_tools_fail_closed_without_realpath_resolver(tmp_path: Path) -> None:
    from uuid import uuid4

    from mgar_v3.harness.adapters import (
        HarnessToolRegistry,
        LocalExecutionBroker,
        LocalWorkspace,
    )

    class NoResolveWorkspace:
        """Delegates every workspace operation but hides the realpath hook."""

        def __init__(self, inner: LocalWorkspace) -> None:
            self._inner = inner

        def snapshot(self):
            return self._inner.snapshot()

        def read(self, path: str) -> str:
            return self._inner.read(path)

        def list_dir(self, path: str) -> tuple[str, ...]:
            return self._inner.list_dir(path)

        def apply(self, edits, *, expected_tree: str):
            return self._inner.apply(edits, expected_tree=expected_tree)

        def diff(self, tree_before: str):
            return self._inner.diff(tree_before)

    root = _workspace(tmp_path)
    (root / "allowed").mkdir()
    (root / "allowed" / "ok.txt").write_text("ok\n", encoding="utf-8")
    (root / "protected").mkdir()
    (root / "protected" / "secret.txt").write_text("SECRET\n", encoding="utf-8")
    (root / "allowed" / "sibling.txt").symlink_to("../protected/secret.txt")
    workspace = LocalWorkspace(root, workspace_id="ws-h04")
    authority = CapabilityAuthority(
        _lease("modify_test", read_paths=("allowed",), write_paths=("allowed",)),
        budget_check=lambda: True,
    )
    registry = HarnessToolRegistry(
        NoResolveWorkspace(workspace), LocalExecutionBroker(workspace), authority
    )
    task = TaskBinding(
        task_id="h04-task",
        thread_id=uuid4(),
        run_id=uuid4(),
        execution_epoch=3,
        actor="owner",
        repository="example/project",
        base_commit="a" * 40,
        workspace_id="ws-h04",
        acceptance_revision="1",
        objective="run tools",
        requirements=(VerificationRequirement(requirement_id="unit"),),
    )
    context = ToolContext(
        task=task,
        grant=_grant(write=("allowed",), commands=("python3",)),
        operation_id="h04-op",
        workspace_tree=workspace.snapshot().tree_hash,
    )
    edit = WorkspaceEdit(
        path="allowed/ok.txt",
        content="changed\n",
        expected_sha256=sha256_text("ok\n"),
    )
    calls = (
        HarnessToolCall(
            call_id="1", name="read_file", arguments={"path": "allowed/sibling.txt"}
        ),
        HarnessToolCall(call_id="2", name="list_dir", arguments={"path": "allowed"}),
        HarnessToolCall(
            call_id="3",
            name="apply_edits",
            arguments={
                "edits": [edit.model_dump(mode="json")],
                "expected_tree": workspace.snapshot().tree_hash,
            },
        ),
        HarnessToolCall(
            call_id="4",
            name="run_tests",
            arguments={"argv": ["python3", "-c", "print(1)"], "cwd": "allowed"},
        ),
    )
    for item in calls:
        result = registry.invoke(item, context, CancellationToken())
        assert result.code == "HARNESS_PATH_DENIED"
    assert (root / "protected" / "secret.txt").read_text(encoding="utf-8") == "SECRET\n"
    assert (root / "allowed" / "ok.txt").read_text(encoding="utf-8") == "ok\n"


def test_container_rejects_symlinked_scope_before_start(tmp_path: Path) -> None:
    root = _workspace(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("OUTSIDE\n", encoding="utf-8")
    (root / "allowed").symlink_to(outside, target_is_directory=True)
    broker = _broker(root, _lease("investigate", read_paths=("allowed",)))
    with pytest.raises(DomainError) as error:
        broker.execute(_request(("id",), cwd="allowed"), _grant(), CancellationToken())
    assert error.value.code == "HARNESS_PATH_DENIED"


def test_sandbox_image_is_pinned() -> None:
    lock = json.loads(LOCK.read_text(encoding="utf-8"))
    assert lock["image_id"].startswith("sha256:")
    assert lock["schema"] == "r03-sandbox-lock-v1"
    with pytest.raises(DomainError):
        IsolatedExecutionBroker(Path("/tmp"), _lease(), lock_path=Path("/missing"))
