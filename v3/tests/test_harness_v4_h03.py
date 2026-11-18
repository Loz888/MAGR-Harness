from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from uuid import uuid4

import pytest

from mgar_v3.domain import DomainError
from mgar_v3.harness.contracts import (
    CancellationToken,
    CapabilityGrant,
    HarnessToolCall,
    TaskBinding,
    ToolContext,
    VerificationRequirement,
    WorkspaceEdit,
    sha256_text,
)
from mgar_v3.harness.workspace import (
    Registration,
    TaskWorkspace,
    WorkspaceRegistry,
    WorkspaceToolRegistry,
)


def _git(repo: Path, *args: str) -> str:
    environment = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    environment.update(
        {
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_AUTHOR_NAME": "H03 Tests",
            "GIT_AUTHOR_EMAIL": "h03@example.invalid",
            "GIT_COMMITTER_NAME": "H03 Tests",
            "GIT_COMMITTER_EMAIL": "h03@example.invalid",
        }
    )
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        env=environment,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


def _repository(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    _git(path, "init", "--quiet")
    (path / "app.py").write_text("def value():\n    return 1\n", encoding="utf-8")
    _git(path, "add", ".")
    _git(path, "commit", "--quiet", "-m", "base")
    return path


def _registry(tmp_path: Path) -> WorkspaceRegistry:
    return WorkspaceRegistry(tmp_path / "registry")


def _workspace(tmp_path: Path, source: Path, task: str = "task-a") -> TaskWorkspace:
    registry = _registry(tmp_path)
    registration = registry.register_local(source, actor="owner")
    return registry.create_task_workspace(registration, task)


def _grant(*, write: tuple[str, ...] = ()) -> CapabilityGrant:
    return CapabilityGrant(
        actor="owner",
        workspace_id="ws",
        repository="example/project",
        read_paths=("",),
        write_paths=write,
        execute=False,
    )


def _task() -> TaskBinding:
    return TaskBinding(
        task_id="h03-task",
        thread_id=uuid4(),
        run_id=uuid4(),
        execution_epoch=1,
        actor="owner",
        repository="example/project",
        base_commit="a" * 40,
        workspace_id="ws",
        acceptance_revision="1",
        objective="Inspect the isolated workspace",
        requirements=(VerificationRequirement(requirement_id="unit"),),
    )


def _context(grant: CapabilityGrant, tree: str) -> ToolContext:
    return ToolContext(
        task=_task(),
        grant=grant,
        operation_id="h03-task:tool:1:1",
        workspace_tree=tree,
    )


def test_relative_registration_is_denied(tmp_path: Path) -> None:
    registry = _registry(tmp_path)
    with pytest.raises(DomainError) as error:
        registry.register_local("relative/project", actor="owner")
    assert error.value.code == "WORKSPACE_REGISTRATION_DENIED"


def test_registration_is_content_addressed_and_reloadable(tmp_path: Path) -> None:
    source = _repository(tmp_path / "project")
    registry = _registry(tmp_path)
    registration = registry.register_local(source, actor="owner")
    loaded = registry.load(registration.workspace_id)
    assert loaded.registration_hash == registration.registration_hash
    assert loaded.repository == "local/project"
    assert len(loaded.base_commit) == 40


def test_two_task_workspaces_do_not_overwrite_each_other(tmp_path: Path) -> None:
    source = _repository(tmp_path / "project")
    registry = _registry(tmp_path)
    registration = registry.register_local(source, actor="owner")
    first = registry.create_task_workspace(registration, "task-a")
    second = registry.create_task_workspace(registration, "task-b")
    assert first.root != second.root
    tree = first.tree_hash()
    preimage = sha256_text(first.read("app.py"))
    first.apply(
        [
            WorkspaceEdit(
                path="app.py", content="def value():\n    return 2\n", expected_sha256=preimage
            )
        ],
        expected_tree=tree,
    )
    assert "return 2" in first.read("app.py")
    assert "return 1" in second.read("app.py")
    assert "return 1" in (source / "app.py").read_text(encoding="utf-8")


def test_dirty_and_untracked_files_are_recorded_and_copied(tmp_path: Path) -> None:
    source = _repository(tmp_path / "project")
    (source / "app.py").write_text("def value():\n    return 99\n", encoding="utf-8")
    (source / "notes.txt").write_text("local note\n", encoding="utf-8")
    workspace = _workspace(tmp_path, source)
    dirty = workspace.manifest.dirty
    assert dirty.tracked_changes == 1
    assert dirty.untracked_files == 1
    assert {item.path for item in dirty.entries} == {"app.py", "notes.txt"}
    assert "return 99" in workspace.read("app.py")
    assert workspace.read("notes.txt") == "local note\n"
    assert "return 99" in (source / "app.py").read_text(encoding="utf-8")


def test_apply_back_requires_exact_preimage_and_preserves_user_change(tmp_path: Path) -> None:
    source = _repository(tmp_path / "project")
    workspace = _workspace(tmp_path, source)
    preimage = sha256_text(workspace.read("app.py"))
    workspace.apply(
        [
            WorkspaceEdit(
                path="app.py", content="def value():\n    return 3\n", expected_sha256=preimage
            )
        ],
        expected_tree=workspace.tree_hash(),
    )
    receipt = workspace.apply_back(source, {"app.py": preimage})
    assert receipt.status == "applied"
    assert "return 3" in (source / "app.py").read_text(encoding="utf-8")

    second = _workspace(tmp_path, source, task="task-b")
    second_preimage = sha256_text(second.read("app.py"))
    second.apply(
        [
            WorkspaceEdit(
                path="app.py",
                content="def value():\n    return 4\n",
                expected_sha256=second_preimage,
            )
        ],
        expected_tree=second.tree_hash(),
    )
    (source / "app.py").write_text("def value():\n    return 77\n", encoding="utf-8")
    conflict = second.apply_back(source, {"app.py": second_preimage})
    assert conflict.status == "conflict"
    assert conflict.conflicts[0].reason == "stale"
    assert "return 77" in (source / "app.py").read_text(encoding="utf-8")


def test_concurrent_workspaces_detect_the_same_preimage_conflict(tmp_path: Path) -> None:
    source = _repository(tmp_path / "project")
    registry = _registry(tmp_path)
    registration = registry.register_local(source, actor="owner")
    first = registry.create_task_workspace(registration, "task-a")
    second = registry.create_task_workspace(registration, "task-b")
    for workspace, value in ((first, 5), (second, 6)):
        preimage = sha256_text(workspace.read("app.py"))
        workspace.apply(
            [
                WorkspaceEdit(
                    path="app.py",
                    content=f"def value():\n    return {value}\n",
                    expected_sha256=preimage,
                )
            ],
            expected_tree=workspace.tree_hash(),
        )
    first_preimage = first.manifest.baseline_hash and sha256_text(
        (source / "app.py").read_text(encoding="utf-8")
    )
    assert first.apply_back(source, {"app.py": first_preimage}).status == "applied"
    conflict = second.apply_back(source, {"app.py": first_preimage})
    assert conflict.status == "conflict"
    assert "return 5" in (source / "app.py").read_text(encoding="utf-8")


def test_apply_back_rejects_an_unregistered_directory(tmp_path: Path) -> None:
    source = _repository(tmp_path / "project")
    other = _repository(tmp_path / "other")
    workspace = _workspace(tmp_path, source)
    with pytest.raises(DomainError) as error:
        workspace.apply_back(other, {})
    assert error.value.code == "WORKSPACE_APPLY_DENIED"


def test_traversal_and_git_metadata_are_denied(tmp_path: Path) -> None:
    source = _repository(tmp_path / "project")
    (tmp_path / "secret.txt").write_text("secret\n", encoding="utf-8")
    workspace = _workspace(tmp_path, source)
    for path in ("../secret.txt", ".git/config", "/etc/passwd"):
        with pytest.raises(DomainError):
            workspace.read(path)


def test_symlink_escape_is_denied(tmp_path: Path) -> None:
    source = _repository(tmp_path / "project")
    (tmp_path / "outside.txt").write_text("outside\n", encoding="utf-8")
    workspace = _workspace(tmp_path, source)
    (workspace.root / "link.txt").symlink_to(tmp_path / "outside.txt")
    with pytest.raises(DomainError) as error:
        workspace.read("link.txt")
    assert error.value.code in {"WORKSPACE_SYMLINK_DENIED", "WORKSPACE_PATH_DENIED"}
    with pytest.raises(DomainError):
        workspace.apply(
            [WorkspaceEdit(path="link.txt", content="x\n", expected_sha256="0" * 64)],
            expected_tree=workspace.tree_hash(),
        )


def test_git_hooks_filters_and_submodules_do_not_execute(tmp_path: Path) -> None:
    source = _repository(tmp_path / "project")
    marker = tmp_path / "hook-marker"
    hooks = source / ".git" / "hooks"
    hooks.mkdir(parents=True, exist_ok=True)
    hook = hooks / "post-checkout"
    hook.write_text(f"#!/bin/sh\ntouch {marker}\n", encoding="utf-8")
    hook.chmod(0o755)
    (source / ".gitattributes").write_text("app.py filter=evil\n", encoding="utf-8")
    _git(source, "config", "filter.evil.smudge", f"sh -c 'touch {marker}; cat'")
    _git(source, "add", ".gitattributes")
    _git(source, "commit", "--quiet", "-m", "attrs")
    submodule_sha = _git(source, "rev-parse", "HEAD")
    _git(source, "update-index", "--add", "--cacheinfo", f"160000,{submodule_sha},vendor/sub")
    (source / "link.txt").symlink_to("app.py")
    _git(source, "add", "link.txt")
    _git(source, "commit", "--quiet", "-m", "submodule and symlink")
    submodule_sha = _git(source, "rev-parse", "HEAD")

    registry = _registry(tmp_path)
    registration = Registration(
        workspace_id="a" * 20,
        kind="github",
        repository="example/project",
        base_commit=submodule_sha,
        registered_by="owner",
        registered_at="2026-11-10T00:00:00Z",
        source_root_sha256="",
        remote=source.as_uri(),
        limits=registry.limits,
        registration_hash="",
    )
    registration = registration.model_copy(
        update={"registration_hash": registration.computed_hash()}
    )
    workspace = registry.create_task_workspace(registration, "task-a")
    assert not marker.exists()
    assert "vendor/sub" in workspace.manifest.excluded
    assert "link.txt" in workspace.manifest.excluded
    assert "return 1" in workspace.read("app.py")


def test_restart_recovery_restores_the_manifest_and_detects_tampering(tmp_path: Path) -> None:
    source = _repository(tmp_path / "project")
    registry = _registry(tmp_path)
    registration = registry.register_local(source, actor="owner")
    workspace = registry.create_task_workspace(registration, "task-a")
    tree = workspace.tree_hash()
    reopened = registry.open_task_workspace(registration, "task-a")
    assert reopened.tree_hash() == tree
    assert reopened.manifest.manifest_hash == workspace.manifest.manifest_hash
    (reopened.root / "app.py").write_text("tampered\n", encoding="utf-8")
    with pytest.raises(DomainError) as error:
        registry.open_task_workspace(registration, "task-a")
    assert error.value.code == "WORKSPACE_MANIFEST_MISMATCH"


def test_tools_read_search_status_diff_and_edit(tmp_path: Path) -> None:
    source = _repository(tmp_path / "project")
    workspace = _workspace(tmp_path, source)
    tools = WorkspaceToolRegistry(workspace)
    names = {spec.name for spec in tools.specs()}
    assert names == {
        "read_file",
        "list_dir",
        "search_files",
        "git_status",
        "git_diff",
        "apply_edits",
    }
    grant = _grant(write=("app.py",))
    context = _context(grant, workspace.tree_hash())
    read = tools.invoke(
        HarnessToolCall(call_id="1", name="read_file", arguments={"path": "app.py"}),
        context,
        CancellationToken(),
    )
    assert read.status == "ok" and "return 1" in read.content
    found = tools.invoke(
        HarnessToolCall(
            call_id="2", name="search_files", arguments={"path": "", "query": "return"}
        ),
        context,
        CancellationToken(),
    )
    assert found.status == "ok" and json.loads(found.content)
    edit = WorkspaceEdit(
        path="app.py",
        content="def value():\n    return 8\n",
        expected_sha256=sha256_text(workspace.read("app.py")),
    )
    applied = tools.invoke(
        HarnessToolCall(
            call_id="3",
            name="apply_edits",
            arguments={
                "edits": [edit.model_dump(mode="json")],
                "expected_tree": workspace.tree_hash(),
            },
        ),
        context,
        CancellationToken(),
    )
    assert applied.status == "ok"
    status = tools.invoke(
        HarnessToolCall(call_id="4", name="git_status", arguments={"path": ""}),
        context,
        CancellationToken(),
    )
    assert "app.py" in status.content
    diff = tools.invoke(
        HarnessToolCall(call_id="5", name="git_diff", arguments={"path": ""}),
        context,
        CancellationToken(),
    )
    assert "+    return 8" in diff.content


def test_authorized_edit_is_persisted_and_reopens(tmp_path: Path) -> None:
    source = _repository(tmp_path / "project")
    registry = _registry(tmp_path)
    registration = registry.register_local(source, actor="owner")
    workspace = registry.create_task_workspace(registration, "edit-and-reopen")
    workspace.apply(
        [
            WorkspaceEdit(
                path="app.py",
                content="def value():\n    return 2\n",
                expected_sha256=sha256_text(workspace.read("app.py")),
            )
        ],
        expected_tree=workspace.tree_hash(),
    )
    reopened = registry.open_task_workspace(registration, "edit-and-reopen")
    assert reopened.read("app.py") == "def value():\n    return 2\n"
    assert reopened.tree_hash() == workspace.tree_hash()
    assert reopened.manifest.manifest_hash == workspace.manifest.manifest_hash


def test_search_rejects_symlink_escape_root_and_nested_links(tmp_path: Path) -> None:
    source = _repository(tmp_path / "project")
    workspace = _workspace(tmp_path, source)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "sentinel.txt").write_text("INDEPENDENT_SENTINEL_NO_SECRET\n", encoding="utf-8")
    (workspace.root / "escape").symlink_to(outside, target_is_directory=True)
    with pytest.raises(DomainError) as error:
        workspace.search("INDEPENDENT_SENTINEL", path="escape")
    assert error.value.code in {"WORKSPACE_SYMLINK_DENIED", "WORKSPACE_PATH_DENIED"}
    (workspace.root / "src").mkdir()
    (workspace.root / "src" / "nested").symlink_to(outside, target_is_directory=True)
    assert workspace.search("INDEPENDENT_SENTINEL") == []


def test_tampered_baseline_is_rejected_and_user_edit_is_preserved(tmp_path: Path) -> None:
    source = _repository(tmp_path / "project")
    registry = _registry(tmp_path)
    registration = registry.register_local(source, actor="owner")
    workspace = registry.create_task_workspace(registration, "baseline-tamper")
    (source / "app.py").write_text("def value():\n    return 999\n", encoding="utf-8")
    baseline_path = workspace.root / ".mgar" / "baseline.json"
    payload = json.loads(baseline_path.read_text(encoding="utf-8"))
    payload["files"]["app.py"] = sha256_text((source / "app.py").read_text(encoding="utf-8"))
    baseline_path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(DomainError) as error:
        registry.open_task_workspace(registration, "baseline-tamper")
    assert error.value.code == "WORKSPACE_MANIFEST_MISMATCH"
    assert "return 999" in (source / "app.py").read_text(encoding="utf-8")
    with pytest.raises(DomainError) as metadata_error:
        workspace.read(".mgar/baseline.json")
    assert metadata_error.value.code == "WORKSPACE_PATH_DENIED"


def test_simultaneous_apply_back_serializes_and_detects_conflict(tmp_path: Path) -> None:
    import threading
    from concurrent.futures import ThreadPoolExecutor

    source = _repository(tmp_path / "project")
    registries = [
        WorkspaceRegistry(tmp_path / "registry-a"),
        WorkspaceRegistry(tmp_path / "registry-b"),
    ]
    workspaces = []
    for index, registry in enumerate(registries):
        registration = registry.register_local(source, actor="owner")
        workspaces.append(registry.create_task_workspace(registration, f"task-{index}"))
    initial = sha256_text(workspaces[0].read("app.py"))
    for workspace, value in zip(workspaces, (2, 3), strict=True):
        workspace.apply(
            [
                WorkspaceEdit(
                    path="app.py",
                    content=f"def value():\n    return {value}\n",
                    expected_sha256=initial,
                )
            ],
            expected_tree=workspace.tree_hash(),
        )
    barrier = threading.Barrier(2)
    original_open = os.open

    def synchronized_open(path: object, flags: int, *args: object, **kwargs: object) -> int:
        if Path(str(path)) == source / "app.py" and flags & os.O_WRONLY:
            barrier.wait(timeout=10)
        return original_open(path, flags, *args, **kwargs)

    os.open = synchronized_open  # type: ignore[assignment]
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            receipts = list(
                pool.map(
                    lambda workspace: workspace.apply_back(source, {"app.py": initial}),
                    workspaces,
                )
            )
    finally:
        os.open = original_open
    statuses = [receipt.status for receipt in receipts]
    assert statuses.count("applied") == 1
    assert statuses.count("conflict") == 1
    assert (source / "app.py").read_text(encoding="utf-8") in {
        "def value():\n    return 2\n",
        "def value():\n    return 3\n",
    }


def test_local_clean_filter_is_not_executed(tmp_path: Path) -> None:
    source = _repository(tmp_path / "project")
    marker = tmp_path / "clean-filter-ran"
    (source / ".gitattributes").write_text("app.py filter=marker\n", encoding="utf-8")
    _git(
        source,
        "config",
        "filter.marker.clean",
        f'sh -c "echo INDEPENDENT_TEST_ONLY > {marker}; cat"',
    )
    _git(source, "add", ".")
    _git(source, "commit", "--quiet", "-m", "filter attributes")
    marker.unlink(missing_ok=True)
    (source / "app.py").write_text("def value():\n    return 2\n", encoding="utf-8")
    registry = _registry(tmp_path)
    registration = registry.register_local(source, actor="owner")
    assert not marker.exists()
    workspace = registry.create_task_workspace(registration, "task-filter")
    assert not marker.exists()
    assert workspace.read("app.py") == "def value():\n    return 2\n"
    assert [(item.path, item.status) for item in workspace.manifest.dirty.entries] == [
        ("app.py", "modified")
    ]


def test_staged_unstaged_mixed_rename_mode_and_untracked(tmp_path: Path) -> None:
    source = _repository(tmp_path / "project")
    (source / "other.py").write_text("def other():\n    return 1\n", encoding="utf-8")
    (source / "rename_me.py").write_text("renamed = True\n", encoding="utf-8")
    (source / "deleted.py").write_text("gone = True\n", encoding="utf-8")
    (source / "mode.sh").write_text("#!/bin/sh\necho mode\n", encoding="utf-8")
    _git(source, "add", ".")
    _git(source, "commit", "--quiet", "-m", "base extras")
    (source / "app.py").write_text("def value():\n    return 10\n", encoding="utf-8")
    _git(source, "add", "app.py")
    (source / "app.py").write_text("def value():\n    return 11\n", encoding="utf-8")
    (source / "other.py").write_text("def other():\n    return 20\n", encoding="utf-8")
    (source / "new.py").write_text("new = True\n", encoding="utf-8")
    _git(source, "add", "new.py")
    _git(source, "rm", "--quiet", "deleted.py")
    _git(source, "mv", "rename_me.py", "renamed.py")
    (source / "mode.sh").chmod(0o755)
    _git(source, "add", "mode.sh")
    (source / "note.txt").write_text("untracked\n", encoding="utf-8")
    workspace = _workspace(tmp_path, source)
    entries = {(item.path, item.status) for item in workspace.manifest.dirty.entries}
    assert ("app.py", "modified") in entries
    assert ("other.py", "modified") in entries
    assert ("new.py", "added") in entries
    assert ("deleted.py", "deleted") in entries
    assert ("renamed.py", "renamed") in entries
    assert ("mode.sh", "modified") in entries
    assert ("note.txt", "untracked") in entries
    assert "return 11" in workspace.read("app.py")
    assert workspace.read("new.py") == "new = True\n"
    assert workspace.read("renamed.py") == "renamed = True\n"
    assert not (workspace.root / "deleted.py").exists()


def test_fsmonitor_script_is_not_executed(tmp_path: Path) -> None:
    source = _repository(tmp_path / "project")
    marker = tmp_path / "fsmonitor-ran"
    script = tmp_path / "fsmonitor.sh"
    script.write_text(f"#!/bin/sh\ntouch {marker}\necho 1\n", encoding="utf-8")
    script.chmod(0o755)
    _git(source, "config", "core.fsmonitor", str(script))
    registry = _registry(tmp_path)
    registration = registry.register_local(source, actor="owner")
    assert not marker.exists()
    registry.create_task_workspace(registration, "task-fsmonitor")
    assert not marker.exists()


def test_staged_delete_with_kept_worktree_file_records_both_facts(tmp_path: Path) -> None:
    source = _repository(tmp_path / "project")
    _git(source, "rm", "--cached", "app.py")
    workspace = _workspace(tmp_path, source)
    entries = [(item.path, item.status) for item in workspace.manifest.dirty.entries]
    assert ("app.py", "deleted") in entries
    assert ("app.py", "untracked") in entries
    assert workspace.manifest.dirty.tracked_changes == 1
    assert workspace.manifest.dirty.untracked_files == 1
    assert workspace.read("app.py") == "def value():\n    return 1\n"


def test_unstaged_delete_is_recorded_as_deleted(tmp_path: Path) -> None:
    source = _repository(tmp_path / "project")
    (source / "app.py").unlink()
    workspace = _workspace(tmp_path, source)
    entries = [(item.path, item.status, item.sha256) for item in workspace.manifest.dirty.entries]
    assert entries == [("app.py", "deleted", None)]
    assert not (workspace.root / "app.py").exists()


def test_dirty_inventory_never_follows_parent_symlink(tmp_path: Path) -> None:
    import shutil

    source = _repository(tmp_path / "project")
    (source / "nested").mkdir()
    (source / "nested" / "data.txt").write_text("inside\n", encoding="utf-8")
    _git(source, "add", ".")
    _git(source, "commit", "--quiet", "-m", "nested")
    outside = tmp_path / "outside"
    outside.mkdir()
    sentinel = "OUTSIDE_SENTINEL_DO_NOT_READ\n"
    (outside / "data.txt").write_text(sentinel, encoding="utf-8")
    shutil.rmtree(source / "nested")
    (source / "nested").symlink_to(outside, target_is_directory=True)
    workspace = _workspace(tmp_path, source)
    entries = {item.path: item for item in workspace.manifest.dirty.entries}
    assert entries["nested/data.txt"].status == "typechange"
    assert entries["nested/data.txt"].sha256 != sha256_text(sentinel)
    assert entries["nested/data.txt"].size != len(sentinel)
    assert "nested" in workspace.manifest.excluded


def test_diff_never_reads_leaf_or_parent_symlink(tmp_path: Path) -> None:
    import shutil

    source = _repository(tmp_path / "project")
    (source / "pkg").mkdir()
    (source / "pkg" / "data.py").write_text("inside = True\n", encoding="utf-8")
    _git(source, "add", ".")
    _git(source, "commit", "--quiet", "-m", "pkg")
    workspace = _workspace(tmp_path, source)
    outside = tmp_path / "outside"
    outside.mkdir()
    sentinel = "HARMLESS_OUTSIDE_DIFF_SENTINEL\n"
    (outside / "app.py").write_text(sentinel, encoding="utf-8")
    (outside / "data.py").write_text(sentinel, encoding="utf-8")

    (workspace.root / "app.py").unlink()
    (workspace.root / "app.py").symlink_to(outside / "app.py")
    try:
        output = workspace.diff()
    except DomainError:
        output = ""
    assert sentinel.strip() not in output

    shutil.rmtree(workspace.root / "pkg")
    (workspace.root / "pkg").symlink_to(outside, target_is_directory=True)
    try:
        output = workspace.diff("pkg/data.py")
    except DomainError:
        output = ""
    assert sentinel.strip() not in output


def test_diff_never_reads_baseline_symlink(tmp_path: Path) -> None:
    import shutil

    source = _repository(tmp_path / "project")
    workspace = _workspace(tmp_path, source)
    (workspace.root / "app.py").write_text("def value():\n    return 9\n", encoding="utf-8")
    outside = tmp_path / "outside-baseline"
    outside.mkdir()
    sentinel = "HARMLESS_OUTSIDE_BASELINE_SENTINEL\n"
    (outside / "app.py").write_text(sentinel, encoding="utf-8")
    baseline = workspace.root / ".mgar" / "baseline-files"
    shutil.rmtree(baseline)
    baseline.symlink_to(outside, target_is_directory=True)
    try:
        output = workspace.diff()
    except DomainError:
        output = ""
    assert sentinel.strip() not in output


def test_manifest_temp_symlink_cannot_redirect_metadata_write(tmp_path: Path) -> None:
    source = _repository(tmp_path / "project")
    registry = _registry(tmp_path)
    registration = registry.register_local(source, actor="owner")
    workspace = registry.create_task_workspace(registration, "task-meta")
    outside = tmp_path / "outside-meta"
    outside.mkdir()
    sentinel = outside / "manifest-target.json"
    sentinel.write_text("HARMLESS_OUTSIDE_META_SENTINEL\n", encoding="utf-8")
    (workspace.root / ".mgar" / "manifest.tmp").symlink_to(sentinel)
    preimage = sha256_text(workspace.read("app.py"))
    workspace.apply(
        [
            WorkspaceEdit(
                path="app.py",
                content="def value():\n    return 12\n",
                expected_sha256=preimage,
            )
        ],
        expected_tree=workspace.tree_hash(),
    )
    assert sentinel.read_text(encoding="utf-8") == "HARMLESS_OUTSIDE_META_SENTINEL\n"
    assert not (workspace.root / ".mgar" / "manifest.json").is_symlink()
    (workspace.root / ".mgar" / "manifest.json").unlink()
    (workspace.root / ".mgar" / "manifest.json").symlink_to(sentinel)
    with pytest.raises(DomainError):
        registry.open_task_workspace(registration, "task-meta")
