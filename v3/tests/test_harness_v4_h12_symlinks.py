"""Internal repository links keep their identity without expanding authority."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from mgar_v3.domain import DomainError, digest
from mgar_v3.evidence.artifacts import ArtifactStore
from mgar_v3.harness import workspace as workspace_module
from mgar_v3.harness.broker import CapabilityLease, IsolatedExecutionBroker, issue_lease
from mgar_v3.harness.contracts import ExecutionRequest, WorkspaceEdit, sha256_text
from mgar_v3.harness.workspace import WorkspaceRegistry, _scan, _tree_hash
from mgar_v3.quality_eval import h12_judge as judge
from mgar_v3.quality_eval.h12_judge import test_hashes as frozen_test_hashes


def _source(root: Path) -> Path:
    (root / "tests/certs/expired/ca").mkdir(parents=True)
    (root / "tests/certs/expired/ca/cert.pem").write_text("certificate fixture\n")
    (root / "tests/certs/valid").mkdir()
    (root / "tests/certs/valid/ca").symlink_to("../expired/ca")
    (root / "tests/certs/mtls/client").mkdir(parents=True)
    (root / "tests/certs/mtls/client/ca").symlink_to("../../expired/ca/")
    (root / "src").mkdir()
    (root / "src/code.py").write_text("value = 1\n")
    return root


def _task(tmp_path: Path):
    source = _source(tmp_path / "source")
    registry = WorkspaceRegistry(tmp_path / "registry")
    registration = registry.register_local(source, actor="operator")
    return source, registry, registration, registry.create_task_workspace(registration, "task")


def _lease(**changes):
    return issue_lease(
        mode="investigate",
        actor="operator",
        workspace_id="links",
        repository="example/project",
        execution_epoch=1,
        read_paths=changes.pop("read_paths", ("",)),
        allowed_commands=("python3",),
        **changes,
    )


def test_native_relative_links_survive_copy_tree_hash_and_recovery(tmp_path: Path) -> None:
    source, registry, registration, workspace = _task(tmp_path)
    hashes, denied = _scan(source)
    assert denied == []
    assert hashes["tests/certs/valid/ca"].startswith("symlink:")
    assert workspace.tree_hash() == _tree_hash(hashes)
    assert os.readlink(workspace.root / "tests/certs/mtls/client/ca") == "../../expired/ca/"
    assert workspace.status() == []
    assert workspace.read("tests/certs/valid/ca/cert.pem") == "certificate fixture\n"
    assert workspace.list_dir("tests/certs/valid/ca") == ("cert.pem",)
    assert (
        frozen_test_hashes(workspace.root)["tests/certs/valid/ca"] == hashes["tests/certs/valid/ca"]
    )
    reopened = registry.open_task_workspace(registration, "task")
    reopened.apply(
        [
            WorkspaceEdit(
                path="src/code.py",
                expected_sha256=sha256_text("value = 1\n"),
                content="value = 2\n",
            )
        ],
        expected_tree=reopened.tree_hash(),
    )
    assert os.readlink(reopened.root / "tests/certs/valid/ca") == "../expired/ca"
    assert (
        IsolatedExecutionBroker(
            reopened.root, _lease(), image_id="sha256:" + "1" * 64
        ).workspace_tree_hash()
        == reopened.tree_hash()
    )


def test_file_search_is_bounded_and_rechecks_resolved_authority(tmp_path: Path) -> None:
    _, _, _, workspace = _task(tmp_path)
    seen = []
    matches = workspace.search(
        "certificate", path="tests/certs/valid/ca/cert.pem", limit=1, authorize=seen.append
    )
    assert len(matches) == 1
    assert seen == ["tests/certs/expired/ca/cert.pem"] * 2

    def deny(relative: str) -> None:
        if relative.startswith("tests/certs/expired/"):
            raise DomainError("HARNESS_READ_DENIED", "outside current grant", 403)

    with pytest.raises(DomainError, match="outside current grant"):
        workspace.read("tests/certs/valid/ca/cert.pem", authorize=deny)
    with pytest.raises(DomainError):
        workspace.search("value", path="src/code.py", limit=0)


@pytest.mark.parametrize(
    "raw", ["/etc/passwd", "../../outside", "cycle", ".mgar/manifest.json", ".env", "missing"]
)
def test_external_cyclic_protected_and_dangling_links_fail_closed(tmp_path: Path, raw: str) -> None:
    _, _, _, workspace = _task(tmp_path)
    before = workspace.tree_hash()
    (workspace.root / "cycle").symlink_to(raw)
    current, unsafe = _scan(workspace.root)
    assert "cycle" in unsafe
    assert _tree_hash(current) != before
    with pytest.raises((DomainError, FileNotFoundError)):
        workspace.read("cycle")
    broker = IsolatedExecutionBroker(workspace.root, _lease(), image_id="sha256:" + "1" * 64)
    with pytest.raises(DomainError):
        broker._mount_args(("",), (), (".mgar", ".env", ".git"))


def test_models_cannot_edit_through_internal_links(tmp_path: Path) -> None:
    _, _, _, workspace = _task(tmp_path)
    with pytest.raises(DomainError) as error:
        workspace.apply(
            [
                WorkspaceEdit(
                    path="tests/certs/valid/ca/cert.pem",
                    expected_sha256=sha256_text("certificate fixture\n"),
                    content="changed",
                )
            ],
            expected_tree=workspace.tree_hash(),
        )
    assert error.value.code == "WORKSPACE_SYMLINK_DENIED"
    assert workspace.read("tests/certs/expired/ca/cert.pem") == "certificate fixture\n"


def test_link_resolution_preserves_dotdot_after_intermediate_links(tmp_path: Path) -> None:
    _, _, _, workspace = _task(tmp_path)
    (workspace.root / "nested/deep").mkdir(parents=True)
    (workspace.root / "nested/real.txt").write_text("actual filesystem semantics")
    (workspace.root / "real.txt").write_text("incorrect lexical normalization")
    (workspace.root / "alias").symlink_to("nested/deep")
    (workspace.root / "chain").symlink_to("alias/../real.txt")
    assert workspace.read("chain") == (workspace.root / "chain").read_text()
    assert workspace.read("chain") == "actual filesystem semantics"


def test_link_target_spelling_changes_tree_and_frozen_test_identity(tmp_path: Path) -> None:
    _, registry, registration, workspace = _task(tmp_path)
    before = workspace.tree_hash()
    link = workspace.root / "tests/certs/valid/ca"
    link.unlink()
    link.symlink_to("../expired/ca/")
    assert workspace.tree_hash() != before
    with pytest.raises(DomainError) as error:
        registry.open_task_workspace(registration, "task")
    assert error.value.code == "WORKSPACE_MANIFEST_MISMATCH"


def test_read_descriptor_is_pinned_before_authority_callback(tmp_path: Path) -> None:
    _, _, _, workspace = _task(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "code.py").write_text("OUTSIDE_SECRET")

    def swap(relative: str) -> None:
        assert relative == "src/code.py"
        (workspace.root / "src").rename(workspace.root / "old-src")
        (workspace.root / "src").symlink_to(outside, target_is_directory=True)

    assert workspace.read("src/code.py", authorize=swap) == "value = 1\n"
    with pytest.raises(DomainError):
        workspace.read("src/code.py")


def test_edit_parent_swap_cannot_write_through_external_link(tmp_path: Path) -> None:
    _, _, _, workspace = _task(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "code.py").write_text("OUTSIDE_SENTINEL")
    artifacts = ArtifactStore(tmp_path / "edit-cas")
    intent = workspace.prepare_recovery_edit(
        [
            WorkspaceEdit(
                path="src/code.py",
                expected_sha256=sha256_text("value = 1\n"),
                content="value = 2\n",
            )
        ],
        expected_tree=workspace.tree_hash(),
        artifacts=artifacts,
    )

    def swap(point: str) -> None:
        if point == "edit.before_file_replace:src/code.py":
            (workspace.root / "src").rename(workspace.root / "old-src")
            (workspace.root / "src").symlink_to(outside, target_is_directory=True)

    with pytest.raises(DomainError) as error:
        workspace.apply_recovery_edit(intent, artifacts=artifacts, fault_hook=swap)
    assert error.value.code == "RECOVERY_EDIT_CONFLICT"
    assert (outside / "code.py").read_text() == "OUTSIDE_SENTINEL"


def test_broker_mounts_only_current_read_only_link_targets(tmp_path: Path) -> None:
    _, _, _, workspace = _task(tmp_path)
    broker = IsolatedExecutionBroker(workspace.root, _lease(), image_id="sha256:" + "1" * 64)
    mounts, placeholders = broker._mount_args(("",), ("src",), (".mgar", ".git", ".env"))
    assert mounts and placeholders == []
    with pytest.raises(DomainError):
        broker._mount_args(("tests/certs/valid",), (), ())
    with pytest.raises(DomainError):
        broker._mount_args(("",), ("tests",), ())


def test_broker_pins_scope_inode_against_host_path_swap(tmp_path: Path) -> None:
    _, _, _, workspace = _task(tmp_path)
    broker = IsolatedExecutionBroker(workspace.root, _lease(), image_id="sha256:" + "1" * 64)
    request = ExecutionRequest(
        operation_id="pinned",
        purpose="test",
        argv=("python3", "-V"),
        cwd="src",
        timeout_seconds=1,
        execution_epoch=1,
    )
    args, _ = broker._create_args("test-pinned", request, _lease(), ("src",), ())
    descriptor = broker._mount_fds["test-pinned"][0]
    expected = os.fstat(descriptor)
    assert f"{expected.st_dev}:{expected.st_ino}" in args
    assert "HARNESS_MOUNT_IDENTITY_CHANGED" in args[args.index("-c") + 1]
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "code.py").write_text("OUTSIDE_SECRET")
    (workspace.root / "src").rename(workspace.root / "old-src")
    (workspace.root / "src").symlink_to(outside, target_is_directory=True)
    current = (workspace.root / "src").stat()
    assert (current.st_dev, current.st_ino) != (expected.st_dev, expected.st_ino)
    assert os.fstat(descriptor).st_ino == expected.st_ino
    broker._remove_placeholders("test-pinned")
    with pytest.raises(OSError):
        os.fstat(descriptor)


def test_default_nofile_preserves_old_lease_hash_and_revision_is_signed() -> None:
    lease = _lease()
    payload = lease.model_dump(mode="json", exclude={"lease_hash"})
    assert "nofile_limit" not in payload
    assert lease.lease_hash == digest(payload)
    assert CapabilityLease.model_validate(lease.model_dump()).computed_hash() == lease.lease_hash
    revised = _lease(nofile_limit=1024)
    assert revised.model_dump()["nofile_limit"] == 1024
    assert revised.computed_hash() == revised.lease_hash


def test_real_development_requests_links_keep_exact_source_identity(tmp_path: Path) -> None:
    source = Path(".local/h12/sources/psf/requests/1b40fdd004bfc8ba5301bcf8a6908264e9b6b877")
    if not source.exists():
        pytest.skip("the exact development source cache is unavailable")
    registry = WorkspaceRegistry(tmp_path / "registry")
    registration = registry.register_local(source.resolve(), actor="operator")
    workspace = registry.create_task_workspace(registration, "real-dev-requests")
    assert workspace.tree_hash() == _tree_hash(_scan(source)[0])
    assert _scan(source)[1] == []
    assert os.readlink(workspace.root / "tests/certs/valid/ca") == "../expired/ca"
    assert os.readlink(workspace.root / "tests/certs/mtls/client/ca") == "../../expired/ca/"
    assert workspace.list_dir("tests/certs/valid/ca") == workspace.list_dir(
        "tests/certs/expired/ca"
    )


def test_verifier_copy_keeps_empty_directories_modes_and_link_identity(tmp_path: Path) -> None:
    source = _source(tmp_path / "source")
    (source / "empty-fixture").mkdir()
    (source / "src/code.py").chmod(0o755)
    expected = _scan(source)[0]
    target = tmp_path / "verifier"
    workspace_module._copy_tree_nofollow(source, target, expected)
    assert _scan(target)[0] == expected
    assert (target / "empty-fixture").is_dir()
    assert (target / "src/code.py").stat().st_mode & 0o777 == 0o755
    assert os.readlink(target / "tests/certs/mtls/client/ca") == "../../expired/ca/"


def test_copy_regular_file_swap_before_open_does_not_read_external_bytes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = _source(tmp_path / "source")
    expected = _scan(source)[0]
    outside = tmp_path / "outside-secret"
    outside.write_text("OUTSIDE_SECRET")
    original_open = workspace_module.os.open
    swapped = False

    def swapping_open(path, flags, *args, **kwargs):
        nonlocal swapped
        if (
            not swapped
            and path == "code.py"
            and flags & os.O_NOFOLLOW
            and flags & os.O_ACCMODE == os.O_RDONLY
        ):
            swapped = True
            (source / "src/code.py").unlink()
            (source / "src/code.py").symlink_to(outside)
        return original_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(workspace_module.os, "open", swapping_open)
    destination = tmp_path / "copy"
    with pytest.raises(DomainError):
        workspace_module._copy_tree_nofollow(source, destination, expected)
    assert swapped
    assert not (destination / "src/code.py").exists()


def test_judge_rejects_regular_file_changed_into_external_link_during_copy(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = _source(tmp_path / "source")
    outside = tmp_path / "outside-secret"
    outside.write_text("OUTSIDE_SECRET")
    original_copy = judge._copy_tree_nofollow

    def moved_copy(original, target, expected):
        (original / "src/code.py").unlink()
        (original / "src/code.py").symlink_to(outside)
        return original_copy(original, target, expected)

    monkeypatch.setattr(judge, "_copy_tree_nofollow", moved_copy)
    monkeypatch.setattr(judge, "prepare_judge", lambda *args, **kwargs: {})
    result = judge.verify(source, {}, tmp_path / "lock", tmp_path / "evidence", phase="environment")
    assert result["status"] == "FAIL"
    assert result["reason"] == "TREE_MOVED_DURING_COPY"
