from __future__ import annotations

import hashlib
import json
import shutil
import stat
import subprocess
import sys
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Literal

import pytest
from harness_docker_ownership import DockerOwnership

from mgar_v3.domain import DomainError
from mgar_v3.harness.broker import IsolatedExecutionBroker, issue_lease
from mgar_v3.harness.commands import CommandSession, ReadinessProbe
from mgar_v3.harness.contracts import (
    CancellationToken,
    CapabilityGrant,
    ExecutionRequest,
    TextReplacement,
    WorkspaceEdit,
    sha256_text,
)
from mgar_v3.harness.project_commands import (
    detect_project,
    parse_tests,
    prepare_dependencies,
    project_files,
    run_build,
    run_tests,
    start_application,
)
from mgar_v3.harness.workspace import WorkspaceRegistry

ROOT = Path(__file__).resolve().parents[1]
LOCK = ROOT / ".local/r03/sandbox-lock.json"
COMMANDS = ("python3", "sh", "sleep", "cat", "mkdir", "touch", "ls", "go", "node", "npm")


def _workspace(tmp_path: Path, files: dict[str, str] | None = None) -> Path:
    root = tmp_path / "workspace"
    root.mkdir(parents=True, exist_ok=True)
    for relative, content in (files or {"app.py": "print('ok')\n"}).items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    for path in [root, *root.rglob("*")]:
        path.chmod(0o777 if path.is_dir() else 0o666)
    ancestor = root.parent
    while ancestor != Path("/tmp") and ancestor.is_dir():
        ancestor.chmod(0o755)
        ancestor = ancestor.parent
    return root


def _lease(
    *,
    read: tuple[str, ...] = ("",),
    write: tuple[str, ...] = ("",),
    commands: tuple[str, ...] = COMMANDS,
    timeout: int = 30,
    max_output_bytes: int = 65_536,
    memory_mb: int = 256,
    pids: int = 64,
    scratch_mb: int = 64,
    epoch: int = 3,
    ttl_seconds: int = 3600,
):
    return issue_lease(
        mode="modify_test",
        actor="owner",
        workspace_id="ws-h06",
        repository="example/project",
        execution_epoch=epoch,
        read_paths=read,
        write_paths=write,
        allowed_commands=commands,
        timeout_seconds=timeout,
        max_output_bytes=max_output_bytes,
        memory_mb=memory_mb,
        pids=pids,
        scratch_mb=scratch_mb,
        ttl_seconds=ttl_seconds,
        approved_by="owner",
        approval_ref="approval:h06",
    )


def _grant(
    *,
    read: tuple[str, ...] = ("",),
    write: tuple[str, ...] = ("",),
    commands: tuple[str, ...] = COMMANDS,
    execute: bool = True,
) -> CapabilityGrant:
    return CapabilityGrant(
        actor="owner",
        workspace_id="ws-h06",
        repository="example/project",
        read_paths=read,
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
    purpose: Literal["test", "build", "command"] = "command",
) -> ExecutionRequest:
    return ExecutionRequest(
        operation_id="h06-" + str(time.time_ns()),
        purpose=purpose,
        argv=argv,
        cwd=cwd,
        timeout_seconds=timeout,
        execution_epoch=epoch,
    )


def _broker(root: Path, lease=None) -> IsolatedExecutionBroker:
    return IsolatedExecutionBroker(root, lease or _lease())


@pytest.fixture
def owned_containers(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    record_property,
) -> Iterator[DockerOwnership]:
    owner = DockerOwnership(tmp_path)
    make_broker = _broker

    def tracked(root: Path, lease=None) -> IsolatedExecutionBroker:
        broker = make_broker(root, lease)
        owner.bind(broker, monkeypatch)
        return broker

    monkeypatch.setattr(sys.modules[__name__], "_broker", tracked)
    yield owner
    if not owner.checked:
        owner.assert_clean()
    record_property(
        "owned_container_ids", json.dumps([item.container_id for item in owner.created])
    )
    record_property("external_container_changes", json.dumps(owner.external_changes))


def test_run_poll_and_logs_record_the_real_command(
    tmp_path: Path,
    owned_containers: DockerOwnership,
) -> None:
    root = _workspace(tmp_path)
    broker = _broker(root)
    session = CommandSession(broker)
    try:
        handle = session.run(
            _request(("python3", "-c", "print('hello h06')")), _grant(), CancellationToken()
        )
        final = session.wait(handle.handle_id, timeout=20)
        assert final.state == "exited"
        assert final.exit_code == 0
        assert final.argv == ("python3", "-c", "print('hello h06')")
        assert final.cwd == "."
        assert final.tree_hash == broker.workspace_tree_hash()
        assert final.environment_hash
        page = session.read_log(handle.handle_id)
        assert "hello h06" in page.data
        assert page.eof is True
        assert session.poll(handle.handle_id).state == "exited"
    finally:
        session.close()
    owned_containers.assert_clean()


def test_container_bytecode_caches_do_not_change_tree_hash(
    tmp_path: Path,
    owned_containers: DockerOwnership,
) -> None:
    root = _workspace(tmp_path, {"calc.py": "VALUE = 7\n"})
    broker = _broker(root)
    session = CommandSession(broker)
    before = broker.workspace_tree_hash()
    try:
        handle = session.run(
            _request(("python3", "-c", "import calc; print(calc.VALUE)")),
            _grant(),
            CancellationToken(),
        )
        final = session.wait(handle.handle_id, timeout=20)
        assert final.state == "exited"
        assert final.exit_code == 0
        assert final.tree_hash == before
    finally:
        session.close()
    # The container really did write bytecode; the hash must ignore it.
    assert (root / "__pycache__").is_dir()
    assert list((root / "__pycache__").glob("calc.*.pyc"))
    assert broker.workspace_tree_hash() == before
    owned_containers.assert_clean()


def test_runtime_caches_are_ignored_but_real_edits_still_trigger_patch_stale(
    tmp_path: Path,
) -> None:
    source = _workspace(tmp_path, {"calc.py": "VALUE = 7\n"})
    registry = WorkspaceRegistry(tmp_path / "registry")
    registration = registry.register_local(source, actor="owner")
    tree = registry.create_task_workspace(registration, "task-cache")
    before = tree.snapshot().tree_hash

    (tree.root / "__pycache__").mkdir()
    (tree.root / "__pycache__" / "calc.cpython-312.pyc").write_bytes(b"\x00\x01")
    (tree.root / ".pytest_cache" / "v" / "cache").mkdir(parents=True)
    (tree.root / ".pytest_cache" / "v" / "cache" / "nodeids").write_text(
        "[]", encoding="utf-8"
    )
    (tree.root / ".coverage").write_text("coverage", encoding="utf-8")
    assert tree.snapshot().tree_hash == before

    # A real external edit must still be visible and must still trip PATCH_STALE.
    (tree.root / "calc.py").write_text("VALUE = 8\n", encoding="utf-8")
    edited = tree.snapshot().tree_hash
    assert edited != before
    with pytest.raises(DomainError) as stale:
        tree.apply(
            [
                WorkspaceEdit(
                    path="calc.py",
                    expected_sha256=sha256_text("VALUE = 7\n"),
                    replacements=[
                        TextReplacement(
                            old_sha256=sha256_text("VALUE = 7"),
                            old_text="VALUE = 7",
                            new_text="VALUE = 9",
                        )
                    ],
                )
            ],
            expected_tree=edited,
        )
    assert stale.value.code == "PATCH_STALE"


def test_workspace_copy_preserves_coverage_configuration_and_report_fixtures(
    tmp_path: Path,
) -> None:
    retained = {
        "calc.py": "VALUE = 7\n",
        ".coveragerc": "[run]\nbranch = True\n",
        ".coverage.toml": "[tool.coverage.run]\nbranch = true\n",
        ".coverage-config": "branch = true\n",
        "nested/.coveragerc": "[report]\nfail_under = 90\n",
        "coverage.xml": "<fixture value='1'/>\n",
        "tests/fixtures/coverage.xml": "<fixture value='2'/>\n",
        "htmlcov/index.html": "<p>project fixture</p>\n",
        "pyproject.toml": "[tool.coverage.run]\nbranch = true\n",
        "setup.cfg": "[coverage:run]\nbranch = True\n",
        "tox.ini": "[tox]\nenvlist = py312\n",
        "pytest.ini": "[pytest]\naddopts = -ra\n",
        ".ruff.toml": "line-length = 100\n",
        "package.json": '{"scripts":{"test":"node --test"}}\n',
    }
    source = _workspace(tmp_path, retained)
    registry = WorkspaceRegistry(tmp_path / "registry")
    registration = registry.register_local(source, actor="owner")
    tree = registry.create_task_workspace(registration, "task-config-copy")
    assert tree.snapshot().files == retained
    for path, text in retained.items():
        assert (tree.root / path).read_text() == text
    assert tree.status() == []
    broker = _broker(tree.root)
    assert broker.workspace_tree_hash() == tree.snapshot().tree_hash

    # Both ordinary reports and same-named fixture data must remain versioned.
    for path in retained:
        before = tree.snapshot().tree_hash
        original = retained[path]
        (tree.root / path).write_text(original + "# changed\n")
        assert tree.snapshot().tree_hash != before, path
        assert broker.workspace_tree_hash() == tree.snapshot().tree_hash, path
        assert path in {change["path"] for change in tree.status()}
        assert f"a/{path}" in tree.diff()
        (tree.root / path).write_text(original)
    assert tree.status() == []


def test_runtime_cache_shapes_preserve_workspace_and_broker_hashes(tmp_path: Path) -> None:
    source = _workspace(tmp_path, {"calc.py": "VALUE = 7\n"})
    registry = WorkspaceRegistry(tmp_path / "registry")
    registration = registry.register_local(source, actor="owner")
    tree = registry.create_task_workspace(registration, "task-runtime-cache-shapes")
    broker = _broker(tree.root)
    before = tree.snapshot().tree_hash
    runtime_files = (
        "__pycache__/calc.cpython-312.pyc",
        "loose.pyc",
        "loose.pyo",
        ".pytest_cache/v/cache/nodeids",
        ".mypy_cache/3.12/cache.json",
        ".ruff_cache/cache",
        ".venv/lib/cache",
        "node_modules/pkg/index.js",
        ".tox/py312/cache",
        ".harness-tmp/coverage.xml",
        ".coverage",
        ".coverage.my-host.example.123.Xabcdefx",
        "nested/.coverage.localhost.456.nonce",
    )
    for path in runtime_files:
        target = tree.root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"runtime output")
        assert tree.snapshot().tree_hash == before, path
        assert broker.workspace_tree_hash() == before, path
        assert tree.status() == []
        target.write_bytes(b"updated runtime output")
        assert tree.snapshot().tree_hash == before, path
        assert broker.workspace_tree_hash() == before, path


@pytest.mark.parametrize("incremental", [False, True])
def test_configuration_drift_rejects_old_tree_and_file_preimage(
    tmp_path: Path, incremental: bool,
) -> None:
    original = "[run]\nbranch = True\n"
    changed = "[run]\nbranch = False\n"
    source = _workspace(tmp_path, {"calc.py": "VALUE = 7\n", ".coveragerc": original})
    registry = WorkspaceRegistry(tmp_path / "registry")
    registration = registry.register_local(source, actor="owner")
    tree = registry.create_task_workspace(registration, "task-config-stale")
    before = tree.snapshot().tree_hash
    (tree.root / ".coveragerc").write_text(changed)
    current = tree.snapshot().tree_hash
    assert current != before

    # The target file is untouched: only a different configuration file drifted.
    with pytest.raises(DomainError) as stale_tree:
        tree.apply(
            [
                WorkspaceEdit(
                    path="calc.py",
                    expected_sha256=sha256_text("VALUE = 7\n"),
                    content="VALUE = 9\n",
                )
            ],
            expected_tree=before,
        )
    assert stale_tree.value.code == "WORKSPACE_STALE"
    assert (tree.root / "calc.py").read_text() == "VALUE = 7\n"

    if incremental:
        edit = WorkspaceEdit(
            path=".coveragerc",
            expected_sha256=sha256_text(original),
            replacements=[
                TextReplacement(
                    old_sha256=sha256_text("True"), old_text="True", new_text="False",
                )
            ],
        )
    else:
        edit = WorkspaceEdit(
            path=".coveragerc", expected_sha256=sha256_text(original), content=changed,
        )
    with pytest.raises(DomainError) as stale_file:
        tree.apply([edit], expected_tree=current)
    assert stale_file.value.code == ("PATCH_STALE" if incremental else "WORKSPACE_EDIT_STALE")
    assert (tree.root / ".coveragerc").read_text() == changed
    assert tree.snapshot().tree_hash == current


def test_timeout_kills_the_container_and_reports_timed_out(
    tmp_path: Path,
    owned_containers: DockerOwnership,
) -> None:
    root = _workspace(tmp_path)
    session = CommandSession(_broker(root))
    try:
        handle = session.run(
            _request(("python3", "-c", "import time;time.sleep(30)"), timeout=1),
            _grant(),
            CancellationToken(),
        )
        final = session.wait(handle.handle_id, timeout=15)
        assert final.state == "timed_out"
        assert final.exit_code == 124
    finally:
        session.close()
    owned_containers.assert_clean()


def test_cancel_stops_a_running_command(
    tmp_path: Path,
    owned_containers: DockerOwnership,
) -> None:
    root = _workspace(tmp_path)
    session = CommandSession(_broker(root))
    try:
        handle = session.run(
            _request(("python3", "-c", "import time;time.sleep(30)")),
            _grant(),
            CancellationToken(),
        )
        time.sleep(0.5)
        cancelled = session.cancel(handle.handle_id)
        assert cancelled.state == "cancelled"
        assert cancelled.exit_code == 137
    finally:
        session.close()
    owned_containers.assert_clean()


def test_service_readiness_is_container_local_and_stop_releases_it(
    tmp_path: Path,
    owned_containers: DockerOwnership,
) -> None:
    root = _workspace(tmp_path)
    broker = _broker(root)
    session = CommandSession(broker)
    try:
        handle = session.run(
            _request(("python3", "-m", "http.server", "8765", "--bind", "127.0.0.1"), timeout=30),
            _grant(),
            CancellationToken(),
        )
        readiness = session.wait_ready(
            handle.handle_id, ReadinessProbe(kind="http", port=8765, timeout_seconds=10)
        )
        assert readiness.ready is True, readiness.detail
        assert readiness.address == "127.0.0.1:8765"
        assert session.poll(handle.handle_id).state == "running"
        session.cancel(handle.handle_id)
        time.sleep(0.2)
        stopped = session.read_log(handle.handle_id)
        assert stopped.eof is True
    finally:
        session.close()
    owned_containers.assert_clean()


def test_large_log_is_paged_with_bounded_pages(tmp_path: Path) -> None:
    root = _workspace(tmp_path)
    lease = _lease(max_output_bytes=200_000)
    session = CommandSession(_broker(root, lease), page_bytes=4096)
    try:
        handle = session.run(
            _request(("python3", "-c", "print('x'*190000)")), _grant(), CancellationToken()
        )
        final = session.wait(handle.handle_id, timeout=20)
        assert final.state == "exited"
        offset = 0
        total = 0
        pages = 0
        while True:
            page = session.read_log(handle.handle_id, offset=offset)
            assert len(page.data) <= 4096
            total += len(page.data)
            pages += 1
            offset = page.next_offset
            if page.eof or not page.data:
                break
        assert pages > 10
        assert total == page.total_bytes
        assert total >= 190_000
    finally:
        session.close()


def test_log_buffer_backpressure_drops_only_the_oldest_bytes(tmp_path: Path) -> None:
    root = _workspace(tmp_path)
    lease = _lease(max_output_bytes=200_000)
    session = CommandSession(_broker(root, lease), page_bytes=8192, max_buffer_bytes=8192)
    try:
        handle = session.run(
            _request(("python3", "-c", "print('y'*190000)")), _grant(), CancellationToken()
        )
        session.wait(handle.handle_id, timeout=20)
        page = session.read_log(handle.handle_id, offset=0, limit=8192)
        assert page.dropped_bytes > 0
        assert page.total_bytes == 190_001
        assert page.offset == page.dropped_bytes
        assert len(page.data) == 8192
    finally:
        session.close()


def test_every_launch_is_checked_against_lease_and_grant(tmp_path: Path) -> None:
    root = _workspace(tmp_path)
    broker = _broker(root)
    session = CommandSession(broker)
    try:
        with pytest.raises(DomainError) as denied:
            session.run(
                _request(("sh", "-c", "echo no")),
                _grant(commands=("python3",)),
                CancellationToken(),
            )
        assert denied.value.code == "HARNESS_COMMAND_DENIED"
        with pytest.raises(DomainError) as epoch:
            session.run(
                _request(("python3", "-c", "print(1)"), epoch=9), _grant(), CancellationToken()
            )
        assert epoch.value.code == "HARNESS_EPOCH_MISMATCH"
        with pytest.raises(DomainError) as scope:
            session.run(
                _request(("python3", "-c", "print(1)"), cwd="outside"),
                _grant(read=("src",), write=("src",)),
                CancellationToken(),
            )
        assert scope.value.code == "HARNESS_READ_DENIED"
        with pytest.raises(DomainError) as execute:
            session.run(
                _request(("python3", "-c", "print(1)")),
                _grant(execute=False),
                CancellationToken(),
            )
        assert execute.value.code == "HARNESS_EXECUTE_DENIED"
    finally:
        session.close()


def test_a_handle_cannot_be_polled_by_another_session(tmp_path: Path) -> None:
    root = _workspace(tmp_path)
    broker = _broker(root)
    owner = CommandSession(broker)
    intruder = CommandSession(broker)
    try:
        handle = owner.run(_request(("python3", "-c", "print(1)")), _grant(), CancellationToken())
        with pytest.raises(DomainError) as missing:
            intruder.poll(handle.handle_id)
        assert missing.value.code == "HARNESS_COMMAND_NOT_FOUND"
        owner.wait(handle.handle_id, timeout=20)
    finally:
        intruder.close()
        owner.close()


def test_read_only_root_with_writable_subset_is_mountable_and_contained(
    tmp_path: Path,
) -> None:
    root = _workspace(tmp_path, {"app.py": "x\n", "sub/keep.txt": "k\n", "secret.txt": "s\n"})
    broker = _broker(root, _lease(read=("",), write=("sub",)))
    session = CommandSession(broker)
    try:
        writable = session.run(
            _request(("python3", "-c", "open('new.txt','w').write('n')"), cwd="sub"),
            _grant(read=("",), write=("sub",)),
            CancellationToken(),
        )
        assert session.wait(writable.handle_id, timeout=20).exit_code == 0
        assert (root / "sub" / "new.txt").read_text(encoding="utf-8") == "n"
        readonly = session.run(
            _request(("python3", "-c", "open('outside.txt','w').write('n')")),
            _grant(read=("",), write=("sub",)),
            CancellationToken(),
        )
        assert session.wait(readonly.handle_id, timeout=20).exit_code != 0
        assert not (root / "outside.txt").exists()
        for protected in (".mgar", ".git", ".env"):
            assert not (root / protected).exists()
    finally:
        session.close()


def test_python_unittest_project_runs_and_reports_real_counts(tmp_path: Path) -> None:
    root = _workspace(
        tmp_path,
        {
            "app.py": "def add(a, b):\n    return a + b\n",
            "tests/__init__.py": "",
            "tests/test_math.py": (
                "import unittest\n"
                "class MathTest(unittest.TestCase):\n"
                "    def test_add(self):\n"
                "        self.assertEqual(1 + 1, 2)\n"
                "    def test_failure_is_real(self):\n"
                "        self.assertEqual(2 * 2, 4)\n"
            ),
        },
    )
    broker = _broker(root)
    profile = detect_project(project_files(root))
    assert profile.kind == "python"
    assert profile.test_argv[:3] == ("python3", "-m", "unittest")
    before = broker.workspace_tree_hash()
    session = CommandSession(broker)
    try:
        receipt = run_tests(broker, session, profile, _grant(), CancellationToken())
        assert receipt.status == "passed", receipt.detail
        assert receipt.tests_run == 2
        assert receipt.tests_failed == 0
        assert receipt.tree_hash == before
        assert receipt.log_bytes > 0
    finally:
        session.close()


def test_all_skipped_and_missing_tests_are_never_a_pass(tmp_path: Path) -> None:
    root = _workspace(
        tmp_path,
        {
            "tests/__init__.py": "",
            "tests/test_skipped.py": (
                "import unittest\n"
                "class SkipTest(unittest.TestCase):\n"
                "    @unittest.skip('not today')\n"
                "    def test_skip(self):\n"
                "        pass\n"
            ),
        },
    )
    broker = _broker(root)
    profile = detect_project(project_files(root))
    session = CommandSession(broker)
    try:
        skipped = run_tests(broker, session, profile, _grant(), CancellationToken())
        assert skipped.status == "no_tests"
        assert skipped.tests_run == 1
    finally:
        session.close()
    assert parse_tests("python", "Ran 0 tests in 0.000s\n\nOK\n", 0)[0] == "no_tests"
    assert parse_tests("python", "no tests ran in 0.01s\n", 5)[0] == "no_tests"
    assert parse_tests("python", "collected 0 items\n", 0)[0] == "unverified"


def test_pytest_without_the_package_waits_instead_of_faking_install(
    tmp_path: Path,
) -> None:
    root = _workspace(
        tmp_path,
        {"pytest.ini": "[pytest]\n", "tests/test_one.py": "def test_one():\n    assert True\n"},
    )
    broker = _broker(root)
    profile = detect_project(project_files(root))
    assert profile.kind == "python"
    assert "pytest" in profile.test_argv
    session = CommandSession(broker)
    try:
        status = prepare_dependencies(broker, profile, _grant(), CancellationToken())
        assert status.status == "waiting_approval"
        assert "pytest" in status.missing
        assert "unittest" in status.action or "dependency source" in status.action
    finally:
        session.close()


def test_node_project_is_detected_and_missing_runtime_is_actionable(tmp_path: Path) -> None:
    package = {"scripts": {"test": "node --test", "build": "node build.js", "start": "node ."}}
    root = _workspace(tmp_path, {"package.json": json.dumps(package), "package-lock.json": "{}"})
    broker = _broker(root)
    profile = detect_project(project_files(root))
    assert profile.kind == "node"
    assert profile.test_argv[0] == "npm"
    assert profile.build_argv == ("npm", "run", "build", "--silent")
    assert profile.dependency_argv[:2] == ("npm", "ci")
    session = CommandSession(broker)
    try:
        status = prepare_dependencies(broker, profile, _grant(), CancellationToken())
        assert status.status == "waiting_approval"
        assert "node" in status.missing
        assert status.action
    finally:
        session.close()


def test_go_project_builds_and_tests_with_the_real_toolchain(tmp_path: Path) -> None:
    root = _workspace(
        tmp_path,
        {
            "go.mod": "module example/h06\n\ngo 1.27\n",
            "main.go": (
                "package main\n\nfunc Add(a, b int) int { return a + b }\n\nfunc main() {}\n"
            ),
            "main_test.go": (
                'package main\n\nimport "testing"\n\n'
                "func TestAdd(t *testing.T) {\n"
                "    if Add(1, 2) != 3 {\n"
                '        t.Fatal("bad sum")\n'
                "    }\n"
                "}\n"
            ),
        },
    )
    broker = _broker(root, _lease(memory_mb=1024, pids=256, scratch_mb=512))
    profile = detect_project(project_files(root))
    assert profile.kind == "go"
    session = CommandSession(broker)
    try:
        build = run_build(broker, session, profile, _grant(), CancellationToken())
        assert build.status == "passed"
        receipt = run_tests(broker, session, profile, _grant(), CancellationToken())
        assert receipt.status == "passed", receipt.detail
        assert receipt.tests_run == 1
    finally:
        session.close()


def test_python_service_start_readiness_and_stop(
    tmp_path: Path,
    owned_containers: DockerOwnership,
) -> None:
    root = _workspace(tmp_path, {"app.py": "print('ok')\n"})
    broker = _broker(root)
    profile = detect_project(project_files(root)).model_copy(
        update={
            "start_argv": ("python3", "-m", "http.server", "8766", "--bind", "127.0.0.1"),
            "readiness": ReadinessProbe(kind="http", port=8766, timeout_seconds=10),
        }
    )
    session = CommandSession(broker)
    try:
        service, address, host_reachable = start_application(
            broker, session, profile, _grant(), CancellationToken()
        )
        assert service.handle.state == "running"
        assert address == "127.0.0.1:8766"
        assert host_reachable is False
        assert session.cancel(service.handle.handle_id).state == "cancelled"
    finally:
        session.close()
    owned_containers.assert_clean()


def test_close_cleans_every_container_the_session_started(
    tmp_path: Path,
    owned_containers: DockerOwnership,
    monkeypatch: pytest.MonkeyPatch,
    record_property,
) -> None:
    root = _workspace(tmp_path)
    broker = _broker(root)
    session = CommandSession(broker)
    # A distinct real broker owns this witness. Session cleanup must neither
    # stop nor remove it even though its name and labels match the old query.
    neighbor = DockerOwnership(tmp_path)
    peer = IsolatedExecutionBroker(_workspace(tmp_path / "neighbor"), _lease())
    neighbor.bind(peer, monkeypatch)
    witness = peer.launch(_request(("sleep", "30")), _grant(), CancellationToken())
    try:
        first = session.run(
            _request(("python3", "-c", "import time;time.sleep(30)")),
            _grant(),
            CancellationToken(),
        )
        second = session.run(
            _request(("python3", "-c", "import time;time.sleep(30)")),
            _grant(),
            CancellationToken(),
        )
        assert session.poll(first.handle_id).state == "running"
        assert session.poll(second.handle_id).state == "running"
        assert session.close() == 2
        assert len(owned_containers.created) == 2
        owned_containers.assert_clean()
        neighbor.assert_running()
        with pytest.raises(AssertionError, match="not created by this test broker"):
            broker.stop_command(witness.name)
        neighbor.assert_running()
        record_property("neighbor_container_id", neighbor.created[0].container_id)
        record_property("neighbor_running_after_session_close", "true")
    finally:
        session.close()
        neighbor.remove_owned_exact()
        neighbor.assert_clean()
    with pytest.raises(DomainError) as closed:
        session.poll(first.handle_id)
    assert closed.value.code == "HARNESS_SESSION_CLOSED"


_PROBE_SCRIPT = (
    "import pathlib\n"
    "def probe(label, fn):\n"
    "    try:\n"
    "        value = fn()\n"
    "    except Exception as exc:\n"
    "        print(label, 'DENIED', type(exc).__name__)\n"
    "    else:\n"
    "        print(label, 'ALLOWED', repr(value)[:40])\n"
    "probe('env_read', lambda: pathlib.Path('.env').read_text())\n"
    "probe('env_write', lambda: pathlib.Path('.env').write_text('leak'))\n"
    "probe('git_read', lambda: pathlib.Path('.git/config').read_text())\n"
    "probe('git_write', lambda: pathlib.Path('.git/x').write_text('x'))\n"
    "probe('mgar_read', lambda: pathlib.Path('.mgar/manifest.json').read_text())\n"
    "probe('mgar_write', lambda: pathlib.Path('.mgar/y').write_text('y'))\n"
    "probe('root_write', lambda: pathlib.Path('outside.txt').write_text('x'))\n"
)


def _manifest(root: Path) -> dict[str, tuple[bool, bool, int, int, int, str]]:
    """Type, owner, mode and content of every entry, without following symlinks."""

    entries: dict[str, tuple[bool, bool, int, int, int, str]] = {}
    for path in [root, *sorted(root.rglob("*"))]:
        info = path.lstat()
        content = ""
        if stat.S_ISREG(info.st_mode):
            content = hashlib.sha256(path.read_bytes()).hexdigest()
        entries[path.relative_to(root).as_posix()] = (
            stat.S_ISDIR(info.st_mode),
            stat.S_ISLNK(info.st_mode),
            info.st_uid,
            info.st_gid,
            stat.S_IMODE(info.st_mode),
            content,
        )
    return entries


def _protected_entries(manifest: dict[str, tuple]) -> dict[str, tuple]:
    return {
        name: value
        for name, value in manifest.items()
        if name == ".env" or name == ".git" or name.startswith(".git/")
        or name == ".mgar" or name.startswith(".mgar/")
    }


def test_read_root_with_narrow_write_starts_with_existing_protected_paths(
    tmp_path: Path,
) -> None:
    root = _workspace(
        tmp_path,
        {
            "app.py": "print('ok')\n",
            "sub/keep.txt": "k\n",
            ".env": "SECRET=1\n",
            ".git/config": "[core]\n",
            ".mgar/manifest.json": "{}",
        },
    )
    broker = _broker(root, _lease(read=("",), write=("sub",)))
    before = _manifest(root)
    session = CommandSession(broker)
    try:
        writable = session.run(
            _request(("python3", "-c", "open('new.txt','w').write('n')"), cwd="sub"),
            _grant(read=("",), write=("sub",)),
            CancellationToken(),
        )
        assert session.wait(writable.handle_id, timeout=20).exit_code == 0
        probe = session.run(
            _request(("python3", "-c", _PROBE_SCRIPT)),
            _grant(read=("",), write=("sub",)),
            CancellationToken(),
        )
        final = session.wait(probe.handle_id, timeout=20)
        assert final.state == "exited"
        page = session.read_log(probe.handle_id)
        output = page.data
        assert "SECRET=1" not in output
        assert "env_write DENIED" in output
        assert "git_read DENIED" in output
        assert "git_write DENIED" in output
        assert "mgar_read DENIED" in output
        assert "mgar_write DENIED" in output
        assert "root_write DENIED" in output
    finally:
        session.close()
    after = _manifest(root)
    assert (root / "sub" / "new.txt").read_text(encoding="utf-8") == "n"
    assert _protected_entries(after) == _protected_entries(before)
    assert {name for name in after if "/" not in name} == {
        name for name in before if "/" not in name
    }


def test_read_root_with_narrow_write_leaves_no_placeholder_for_missing_paths(
    tmp_path: Path,
) -> None:
    root = _workspace(tmp_path, {"app.py": "x\n", "sub/keep.txt": "k\n"})
    broker = _broker(root, _lease(read=("",), write=("sub",)))
    before = _manifest(root)
    session = CommandSession(broker)
    try:
        probe = session.run(
            _request(("python3", "-c", _PROBE_SCRIPT)),
            _grant(read=("",), write=("sub",)),
            CancellationToken(),
        )
        final = session.wait(probe.handle_id, timeout=20)
        assert final.state == "exited"
        output = session.read_log(probe.handle_id).data
        assert "env_read DENIED" in output
        assert "git_read DENIED" in output
        assert "mgar_read DENIED" in output
        assert "env_write DENIED" in output
        assert "git_write DENIED" in output
        assert "mgar_write DENIED" in output
    finally:
        session.close()
    assert _manifest(root) == before
    assert not (root / ".git").exists()
    assert not (root / ".mgar").exists()
    assert not (root / ".env").exists()


def test_writable_root_keeps_protected_paths_denied_and_host_pristine(
    tmp_path: Path,
) -> None:
    root = _workspace(tmp_path, {"app.py": "x\n"})
    broker = _broker(root, _lease(read=("",), write=("",)))
    before = _manifest(root)
    session = CommandSession(broker)
    try:
        probe = session.run(
            _request(("python3", "-c", _PROBE_SCRIPT)), _grant(), CancellationToken()
        )
        final = session.wait(probe.handle_id, timeout=20)
        assert final.state == "exited"
        output = session.read_log(probe.handle_id).data
        assert "env_write DENIED" in output
        assert "git_read DENIED" in output
        assert "git_write DENIED" in output
        assert "mgar_read DENIED" in output
        assert "mgar_write DENIED" in output
        assert "root_write ALLOWED" in output
    finally:
        session.close()
    after = _manifest(root)
    assert (root / "outside.txt").read_text(encoding="utf-8") == "x"
    for protected in (".git", ".mgar", ".env"):
        assert not (root / protected).exists()
    assert _protected_entries(before) == {}
    assert {name for name in after if "/" not in name} == {
        name for name in before if "/" not in name
    } | {"outside.txt"}
    git = shutil.which("git")
    if git:
        initialized = subprocess.run([git, "init", "-q", str(root)], capture_output=True)
        assert initialized.returncode == 0, initialized.stderr.decode("utf-8", "replace")


def test_lease_revocation_stops_a_running_command(
    tmp_path: Path,
    owned_containers: DockerOwnership,
) -> None:
    root = _workspace(tmp_path)
    broker = _broker(root)
    session = CommandSession(broker)
    try:
        handle = session.run(
            _request(("python3", "-c", "import time;time.sleep(30)")),
            _grant(),
            CancellationToken(),
        )
        assert session.poll(handle.handle_id).state == "running"
        broker.authority.revoke()
        final = session.wait(handle.handle_id, timeout=15)
        assert final.state == "revoked"
        assert final.exit_code == 137
    finally:
        session.close()
    owned_containers.assert_clean()


def test_lease_expiry_stops_a_running_command(
    tmp_path: Path,
    owned_containers: DockerOwnership,
) -> None:
    root = _workspace(tmp_path)
    broker = _broker(root, _lease(ttl_seconds=2))
    session = CommandSession(broker)
    try:
        handle = session.run(
            _request(("python3", "-c", "import time;time.sleep(30)")),
            _grant(),
            CancellationToken(),
        )
        final = session.wait(handle.handle_id, timeout=20)
        assert final.state == "revoked"
        assert final.exit_code == 137
    finally:
        session.close()
    owned_containers.assert_clean()


def test_epoch_replacement_stops_a_running_command(
    tmp_path: Path,
    owned_containers: DockerOwnership,
) -> None:
    root = _workspace(tmp_path)
    broker = _broker(root, _lease(epoch=3))
    session = CommandSession(broker)
    try:
        handle = session.run(
            _request(("python3", "-c", "import time;time.sleep(30)")),
            _grant(),
            CancellationToken(),
        )
        assert session.poll(handle.handle_id).state == "running"
        broker.authority.replace(_lease(epoch=4))
        final = session.wait(handle.handle_id, timeout=15)
        assert final.state == "revoked"
        assert final.exit_code == 137
    finally:
        session.close()
    owned_containers.assert_clean()


def test_output_clipped_by_the_lease_is_reported_not_silent(tmp_path: Path) -> None:
    root = _workspace(tmp_path)
    lease = _lease(max_output_bytes=2048)
    session = CommandSession(_broker(root, lease), page_bytes=4096)
    try:
        handle = session.run(
            _request(("python3", "-c", "print('z'*20000)")), _grant(), CancellationToken()
        )
        final = session.wait(handle.handle_id, timeout=20)
        page = session.read_log(handle.handle_id, offset=0, limit=4096)
        assert page.truncated is True
        # 20000 printed characters plus the newline; the page only carries the
        # retained prefix, but totals must describe the real output.
        assert page.total_bytes == 20001
        assert len(page.data.encode("utf-8")) == 2048
        assert page.total_bytes > len(page.data.encode("utf-8"))
        assert page.eof is True
        assert final.log_bytes == 20001
        assert final.log_truncated is True
    finally:
        session.close()


def test_exact_length_log_is_not_reported_as_truncated(tmp_path: Path) -> None:
    root = _workspace(tmp_path)
    lease = _lease(max_output_bytes=2048)
    session = CommandSession(_broker(root, lease), page_bytes=4096)
    try:
        handle = session.run(
            _request(
                (
                    "python3",
                    "-c",
                    "import sys; sys.stdout.write('z'*2048)",
                )
            ),
            _grant(),
            CancellationToken(),
        )
        final = session.wait(handle.handle_id, timeout=20)
        page = session.read_log(handle.handle_id, offset=0, limit=4096)
        assert page.total_bytes == 2048
        assert len(page.data.encode("utf-8")) == 2048
        assert page.truncated is False
        assert page.eof is True
        assert final.log_bytes == 2048
        assert final.log_truncated is False
    finally:
        session.close()


def test_pytest_run_waits_for_approval_instead_of_reporting_unverified(
    tmp_path: Path,
) -> None:
    root = _workspace(
        tmp_path,
        {"pytest.ini": "[pytest]\n", "tests/test_one.py": "def test_one():\n    assert True\n"},
    )
    broker = _broker(root)
    profile = detect_project(project_files(root))
    assert profile.kind == "python"
    session = CommandSession(broker)
    try:
        receipt = run_tests(broker, session, profile, _grant(), CancellationToken())
        assert receipt.status == "waiting_approval"
        assert "pytest" in receipt.missing
        assert receipt.tests_run == 0
        assert receipt.action
    finally:
        session.close()
