"""Real product-only writable native links retain effective scope and inode guards."""

from __future__ import annotations

import os
from pathlib import Path
from uuid import uuid4

import pytest

from mgar_v3.domain import DomainError
from mgar_v3.harness.broker import IsolatedExecutionBroker, issue_lease
from mgar_v3.harness.contracts import (
    CancellationToken,
    CapabilityGrant,
    ExecutionRequest,
    WorkspaceEdit,
)
from mgar_v3.harness.workspace import WorkspaceRegistry, _scan

LOCK = Path(__file__).resolve().parents[1] / ".local/r03/sandbox-lock.json"


def _fixture(tmp_path, *, write=("",), grant_write=None, flag=True, protected=None):
    source = tmp_path / "source"
    (source / "real").mkdir(parents=True)
    (source / "real/value.txt").write_text("original content")
    (source / "writable").mkdir()
    (source / "writable/keep.txt").write_text("preserve nonempty scope directory")
    (source / "file_link").symlink_to("real/value.txt")
    (source / "dir_link").symlink_to("real", target_is_directory=True)
    registry = WorkspaceRegistry(tmp_path / "registry")
    registration = registry.register_local(source, actor="operator")
    workspace = registry.create_task_workspace(registration, "native-links", source_root=source)
    for folder, _directories, files in os.walk(workspace.root, followlinks=False):
        Path(folder).chmod(0o777)
        for name in files:
            path = Path(folder) / name
            if not path.is_symlink():
                path.chmod(0o666)
    ancestor = workspace.root.parent
    while ancestor != Path("/tmp"):
        ancestor.chmod(0o755)
        ancestor = ancestor.parent
    lease = issue_lease(
        mode="modify_test" if write else "investigate", actor="operator",
        workspace_id=registration.workspace_id, repository=registration.repository,
        execution_epoch=1, read_paths=("",), write_paths=write, allowed_commands=("python3",),
        timeout_seconds=30, max_calls=20,
        approved_by="operator" if write else "", approval_ref="product-links" if write else "",
        protected_paths=protected or (".git", ".mgar", ".env"),
    )
    grant = CapabilityGrant(
        actor=lease.actor, workspace_id=lease.workspace_id, repository=lease.repository,
        read_paths=("",), write_paths=write if grant_write is None else grant_write,
        execute=True, allowed_commands=lease.allowed_commands, max_seconds=30,
    )
    broker = IsolatedExecutionBroker(
        workspace.root, lease, lock_path=LOCK, allow_internal_write_symlinks=flag,
    )
    return source, workspace, broker, grant


def _execute(broker, grant, script):
    return broker.execute(ExecutionRequest(
        operation_id="links-" + uuid4().hex[:12], purpose="command",
        argv=("python3", "-B", "-c", script), cwd=".", timeout_seconds=30,
        execution_epoch=1,
    ), grant, CancellationToken())


def test_real_product_native_file_and_directory_links_only_change_the_isolated_copy(tmp_path):
    source, workspace, broker, grant = _fixture(tmp_path)
    before = _scan(source)
    receipt = _execute(broker, grant, (
        "from pathlib import Path\n"
        "assert Path('file_link').is_symlink() and Path('dir_link').is_symlink()\n"
        "assert Path('file_link').read_text()=='original content'\n"
        "Path('file_link').write_text('isolated changed')\n"
        "assert Path('dir_link/value.txt').read_text()=='isolated changed'\n"
        "Path('dir_link/new.txt').write_text('created through native link')\n"
        "print('NATIVE_LINKS_OK')\n"
    ))
    assert receipt.status == "passed", receipt
    assert "NATIVE_LINKS_OK" in receipt.stdout
    assert (workspace.root / "real/value.txt").read_text() == "isolated changed"
    assert (workspace.root / "real/new.txt").read_text() == "created through native link"
    assert os.readlink(workspace.root / "file_link") == "real/value.txt"
    assert os.readlink(workspace.root / "dir_link") == "real"
    assert _scan(source) == before
    assert not (source / "real/new.txt").exists()
    assert not broker._mount_fds


def test_default_broker_still_rejects_writable_native_links(tmp_path):
    _, _, broker, grant = _fixture(tmp_path, flag=False)
    with pytest.raises(DomainError) as rejected:
        _execute(broker, grant, "raise AssertionError('must not execute')")
    assert rejected.value.code == "HARNESS_PATH_DENIED"


def test_product_flag_preserves_existing_readonly_internal_link_behavior(tmp_path):
    source, _, broker, grant = _fixture(tmp_path, write=())
    before = _scan(source)
    receipt = _execute(broker, grant, (
        "from pathlib import Path\n"
        "assert Path('file_link').read_text()=='original content'\n"
        "assert Path('dir_link/value.txt').read_text()=='original content'\n"
    ))
    assert receipt.status == "passed", receipt
    assert _scan(source) == before


@pytest.mark.parametrize("raw", [
    "/etc/passwd", "../../outside-secret", "unsafe", "missing", ".env",
    ".mgar/manifest.json", ".git/config", ".", "real/..",
])
def test_product_flag_rejects_external_cyclic_protected_and_recursive_links(tmp_path, raw):
    source, workspace, broker, grant = _fixture(tmp_path)
    before = _scan(source)
    (workspace.root / "unsafe").symlink_to(raw)
    with pytest.raises(DomainError) as rejected:
        _execute(broker, grant, "raise AssertionError('must not execute')")
    assert rejected.value.code == "HARNESS_PATH_DENIED"
    assert _scan(source) == before
    assert not broker._mount_fds


@pytest.mark.parametrize("protected", [("real",), ("file_link",)])
def test_product_flag_does_not_open_protected_alias_or_canonical_target(tmp_path, protected):
    _, _, broker, grant = _fixture(tmp_path, protected=(".git", ".mgar", ".env", *protected))
    with pytest.raises(DomainError) as rejected:
        _execute(broker, grant, "raise AssertionError('must not execute')")
    assert rejected.value.code == "HARNESS_PATH_DENIED"


def test_writable_alias_cannot_reach_a_readonly_target_after_scope_intersection(tmp_path):
    _, workspace, broker, grant = _fixture(
        tmp_path, write=("writable", "real"), grant_write=("writable",)
    )
    # The root aliases stay read-only here. Remove them to isolate the writable
    # alias check, with a target authorized by the lease but denied by the grant.
    (workspace.root / "file_link").unlink()
    (workspace.root / "dir_link").unlink()
    (workspace.root / "writable/link").symlink_to("../real/value.txt")
    with pytest.raises(DomainError) as rejected:
        _execute(broker, grant, "raise AssertionError('must not execute')")
    assert rejected.value.code == "HARNESS_PATH_DENIED"


def test_readonly_alias_to_writable_target_remains_rejected(tmp_path):
    _, _, broker, grant = _fixture(tmp_path, write=("real",))
    with pytest.raises(DomainError) as rejected:
        _execute(broker, grant, "raise AssertionError('must not execute')")
    assert rejected.value.code == "HARNESS_PATH_DENIED"


@pytest.mark.parametrize("alias", ["file_link", "dir_link"])
def test_real_product_inode_guard_rejects_link_swap_before_start(tmp_path, monkeypatch, alias):
    source, workspace, broker, grant = _fixture(tmp_path)
    before = _scan(source)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "value.txt").write_text("OUTSIDE_SENTINEL")
    raw_target = outside if alias == "dir_link" else outside / "value.txt"
    real_docker = broker._docker
    changed = False

    def swap_before_create(arguments, **kwargs):
        nonlocal changed
        if arguments and arguments[0] == "create" and not changed:
            changed = True
            link = workspace.root / alias
            link.unlink()
            link.symlink_to(raw_target, target_is_directory=alias == "dir_link")
        return real_docker(arguments, **kwargs)

    monkeypatch.setattr(broker, "_docker", swap_before_create)
    receipt = _execute(broker, grant, (
        "from pathlib import Path;Path('command-ran').write_text('MUST_NOT_EXECUTE')"
    ))
    assert changed and receipt.status == "failed" and receipt.exit_code == 126, receipt
    assert not (workspace.root / "command-ran").exists()
    assert (outside / "value.txt").read_text() == "OUTSIDE_SENTINEL"
    assert _scan(source) == before
    assert not broker._mount_fds


def test_incremental_file_edit_api_still_cannot_write_through_native_links(tmp_path):
    _, workspace, _, _ = _fixture(tmp_path)
    with pytest.raises(DomainError) as rejected:
        workspace.apply([
            WorkspaceEdit(path="file_link", expected_sha256="0" * 64, content="DENIED")
        ], expected_tree=workspace.snapshot().tree_hash)
    assert rejected.value.code == "WORKSPACE_SYMLINK_DENIED"


def test_product_start_error_preserves_unknown_outcome_and_closes_inode_pins(tmp_path, monkeypatch):
    _, _, broker, grant = _fixture(tmp_path)
    real_docker = broker._docker
    created = []

    def fail_start(arguments, **kwargs):
        if arguments and arguments[0] == "create":
            created.append(arguments[arguments.index("--name") + 1])
        if arguments and arguments[0] == "start":
            raise DomainError("CONTROLLED_START_UNKNOWN", "Start outcome unknown", 503)
        return real_docker(arguments, **kwargs)

    monkeypatch.setattr(broker, "_docker", fail_start)
    with pytest.raises(DomainError) as outcome:
        broker.launch(ExecutionRequest(
            operation_id="start-fault", argv=("python3", "-B", "-c", "print('not run')"),
            cwd=".", timeout_seconds=30, execution_epoch=1,
        ), grant, CancellationToken())
    assert outcome.value.code == "CONTROLLED_START_UNKNOWN"
    assert len(created) == 1 and not broker._mount_fds
    assert broker.inspect_command(created[0])[0] == "missing"
