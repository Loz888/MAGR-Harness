"""Product commands and preview use the real H04/H06 isolated runtime."""

from __future__ import annotations

import ensurepip
import hashlib
import json
import shutil
import subprocess
import zipfile
from pathlib import Path
from uuid import uuid4

import pytest

from mgar_v3.domain import DomainError
from mgar_v3.harness.broker import issue_lease
from mgar_v3.harness.commands import CommandSession, ReadinessProbe
from mgar_v3.harness.contracts import (
    CancellationToken,
    CapabilityGrant,
    HarnessToolCall,
    TaskBinding,
    ToolContext,
    VerificationRequirement,
    WorkspaceEdit,
)
from mgar_v3.harness.product_preparation import prepare_product_environment
from mgar_v3.harness.product_runtime import (
    ProductCodingToolRegistry,
    ProductCommand,
    ProductPreview,
    discover_product_plan,
    product_readiness,
    validate_product_image_lock,
)
from mgar_v3.harness.service import WorkspaceBoundBroker
from mgar_v3.harness.workspace import WorkspaceRegistry

ROOT = Path(__file__).resolve().parents[1]
LOCK = ROOT / ".local/r03/sandbox-lock.json"


def _runtime(tmp_path, files=None):
    files = {"index.html": "<h1>Preview 汉字</h1>", "test_calc.py": (
        "import unittest\nclass CalcTest(unittest.TestCase):\n"
        " def test_sum(self): self.assertEqual(2+3,5)\n"
    )} if files is None else files
    original = tmp_path / "source"
    original.mkdir()
    for relative, body in files.items():
        target = original / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(body)
    registry = WorkspaceRegistry(tmp_path / "registry")
    registration = registry.register_local(original, actor="owner")
    workspace = registry.create_task_workspace(registration, "product-test", source_root=original)
    ancestor = workspace.root
    while ancestor != Path("/tmp"):
        ancestor.chmod(0o755)
        ancestor = ancestor.parent
    plan = discover_product_plan(files, image_lock=str(LOCK))
    task = TaskBinding(
        task_id="product-test", thread_id=uuid4(), run_id=uuid4(), execution_epoch=1,
        actor="owner", workspace_id=registration.workspace_id,
        repository=registration.repository, base_commit=registration.base_commit,
        objective="Check and preview the project", task_kind="feature", acceptance_revision="v1",
        requirements=(VerificationRequirement(requirement_id="suite", kind="test"),),
    )
    lease = issue_lease(
        mode="modify_test", actor=task.actor, workspace_id=task.workspace_id,
        repository=task.repository, execution_epoch=1, read_paths=("",), write_paths=("",),
        allowed_commands=plan.programs, timeout_seconds=30, max_calls=100,
        approved_by="owner", approval_ref="product-test-authorization",
    )
    grant = CapabilityGrant(
        actor=task.actor, workspace_id=task.workspace_id, repository=task.repository,
        read_paths=("",), write_paths=("",), execute=True, allowed_commands=plan.programs,
        max_seconds=30,
    )
    broker = WorkspaceBoundBroker(workspace, lease, lock_path=LOCK)
    session = CommandSession(broker)
    tools = ProductCodingToolRegistry(
        workspace, broker, session, plan.test_argv, kind="feature", runtime_plan=plan
    )
    context = ToolContext(
        task=task, grant=grant, operation_id="command-" + uuid4().hex,
        workspace_tree=workspace.snapshot().tree_hash,
    )
    return workspace, tools, context, plan


def test_discovery_uses_package_scripts_and_package_manager():
    plan = discover_product_plan({
        "package.json": json.dumps({"scripts": {"test": "vitest run", "lint": "eslint .",
                                                "dev": "vite --port 4321"}}),
        "pnpm-lock.yaml": "lockfileVersion: 9",
    })
    assert plan.test_argv == ("pnpm", "run", "test")
    assert plan.commands["lint"].purpose == "build"
    assert "pnpm" in plan.programs and "node" in plan.programs
    assert plan.readiness.port == 4321


def test_discovery_does_not_invent_npm_test_for_a_project_without_tests():
    plan = discover_product_plan({"package.json": '{"scripts":{"build":"vite build"}}'})
    assert not plan.test_argv and "tests" not in plan.commands
    assert plan.commands["build"].argv == ("npm", "run", "build")


def test_single_nested_app_is_detected_and_ambiguous_apps_are_not_guessed():
    plan = discover_product_plan({"frontend/package.json": '{"scripts":{"test":"vitest"}}'})
    assert plan.root == "frontend" and plan.commands["tests"].cwd == "frontend"
    plan = discover_product_plan({"a/package.json": "{}", "b/package.json": "{}"})
    assert plan.kind == "unknown" and "tests" not in plan.commands
    assert plan.verification_scope == "build_only"


def test_image_lock_requires_a_full_immutable_digest(tmp_path):
    invalid = tmp_path / "image.json"
    invalid.write_text('{"image_id":"sha256:short"}')
    with pytest.raises(DomainError, match="full SHA256"):
        validate_product_image_lock(invalid)


def test_real_zero_model_readiness_reports_missing_image_as_environment(tmp_path):
    root = tmp_path / "source"
    root.mkdir()
    (root / "test_calc.py").write_text("import unittest\n")
    result = product_readiness(root, tmp_path / "missing.json")
    assert result["status"] == "waiting_environment"
    assert result["blockers"][0]["code"] == "SANDBOX_NOT_PREPARED"


def test_real_zero_model_readiness_checks_python_runtime_and_missing_distribution(tmp_path):
    root = tmp_path / "source"
    root.mkdir()
    (root / "requirements.txt").write_text("harness-impossible-distribution-a724d69==1.0\n")
    result = product_readiness(root, LOCK)
    assert result["status"] == "waiting_environment", result
    assert any(row["check"] == "runtime" and row["exit_code"] == 0
               for row in result["observations"])
    assert any(row["code"] == "PRODUCT_DEPENDENCIES_MISSING" for row in result["blockers"])


def test_unknown_command_is_correctable_and_does_not_run_code(tmp_path):
    workspace, tools, context, _ = _runtime(tmp_path)
    try:
        result = tools.invoke(HarnessToolCall(
            call_id="bad", name="run_command",
            arguments={"argv": ["python3", "-c", "open('escaped','w').write('x')"]},
        ), context, CancellationToken())
        assert result.status == "error" and result.data["executed"] is False
        assert "tests" in result.data["allowed_command_ids"]
        assert not (workspace.root / "escaped").exists()
    finally:
        tools.session.close()


def test_python_registry_tool_metadata_is_bounded_despite_long_build_argv(tmp_path):
    _, tools, _, plan = _runtime(tmp_path)
    try:
        assert len(" ".join(plan.commands["build"].argv)) > 500
        specs = tools.specs()
        assert all(len(spec.description) <= 1000 for spec in specs)
        spec = next(spec for spec in specs if spec.name == "run_command")
        assert set(spec.input_schema["properties"]["command_id"]["enum"]) == set(plan.commands)
    finally:
        tools.session.close()


def test_empty_project_bootstrap_requires_an_actual_deliverable_then_validates_without_fake_tests(
    tmp_path,
):
    workspace, tools, context, plan = _runtime(tmp_path, {})
    try:
        assert plan.verification_scope == "build_only" and not plan.test_argv
        assert not any(spec.name == "run_tests" for spec in tools.specs())
        assert "python_tests" in plan.commands
        empty = tools.invoke(HarnessToolCall(
            call_id="empty", name="run_command", arguments={"command_id": "build"}
        ), context, CancellationToken())
        assert empty.status == "error" and empty.data["receipt"]["tests_run"] == 0
        assert "No deliverable files" in empty.content
        created_tree = workspace.apply([
            WorkspaceEdit(path="index.html", create=True,
                          content="<!doctype html><h1>Fresh app</h1>")
        ], expected_tree=workspace.snapshot().tree_hash)
        assert "Fresh app" in workspace.diff()
        current = created_tree.tree_hash
        created = tools.invoke(HarnessToolCall(
            call_id="created", name="run_command", arguments={"command_id": "build"}
        ), context.model_copy(update={"operation_id": "created", "workspace_tree": current}),
            CancellationToken())
        assert created.status == "ok", created
        assert created.data["receipt"]["tests_run"] == 0
        assert "Build-only verification: 1 deliverables" in created.content
        assert "behavior tests not run" in created.content
    finally:
        tools.session.close()


def test_bootstrap_readme_and_static_projects_have_real_runtime_preflight(tmp_path):
    project = tmp_path / "empty"
    project.mkdir()
    for name in (None, "README.md", "index.html"):
        if name:
            (project / name).write_text("<h1>hello</h1>" if name.endswith("html") else "hello")
        result = product_readiness(project, LOCK)
        assert result["status"] == "ready", result
        assert result["verification_scope"] == "build_only"
        assert result["plan"]["commands"]["build"]["purpose"] == "build"


@pytest.mark.parametrize("specification, expected_status", [
    (">=99.0", "not_satisfied"), (">=3.0,<99.0", "satisfied"),
    (">=3.12rc1", "not_verified"),
])
def test_real_preflight_checks_requires_python_and_never_passes_unsupported_constraints(
    tmp_path, specification, expected_status,
):
    root = tmp_path / "python"
    root.mkdir()
    (root / "pyproject.toml").write_text(
        '[project]\nname="version-check"\nrequires-python="' + specification + '"\n'
    )
    result = product_readiness(root, LOCK)
    version = next(item for item in result["observations"] if item["check"] == "python_requires")
    assert version["status"] == expected_status, result
    assert version["actual"] and len(version["actual"]) == 3
    assert result["status"] == (
        "ready" if expected_status == "satisfied" else "waiting_environment"
    )


def test_real_preflight_checks_selected_package_manager_version_without_project_scripts(tmp_path):
    root = tmp_path / "node"
    root.mkdir()
    (root / "package.json").write_text(json.dumps({
        "packageManager": "npm@999.0.0",
        "scripts": {"test": "node -e \"require('fs').writeFileSync('must-not-run','x')\""},
    }))
    result = product_readiness(root, ROOT / ".local/h12/sandbox-lock.json")
    manager = next(item for item in result["observations"] if item["check"] == "package_manager")
    assert manager["program"] == "npm" and manager["expected"] == "999.0.0"
    assert manager["status"] == "not_verified"
    assert result["status"] == "waiting_environment"
    assert not (root / "must-not-run").exists()


@pytest.mark.parametrize("module", [
    "module example.test/new-toolchain\n\ngo 1.99.0\n",
    "module example.test/missing-cache\n\ngo 1.22.0\n\n"
    "require example.invalid/uncached v1.0.0\n",
])
def test_real_preflight_go_toolchain_and_cache_checks_never_download(tmp_path, module):
    root = tmp_path / "go"
    root.mkdir()
    (root / "go.mod").write_text(module)
    result = product_readiness(root, LOCK)
    modules = next(item for item in result["observations"] if item["check"] == "go_modules")
    assert modules["exit_code"] != 0 and modules["toolchain_download"] == "disabled"
    assert modules["network"] == "none"
    assert result["status"] == "waiting_environment"
    assert not (root / "go.sum").exists()


def test_product_command_archives_post_command_tree_without_relabelling_execution_version(tmp_path):
    workspace, tools, context, plan = _runtime(tmp_path)
    workspace.root.chmod(0o777)  # Only this isolated copy grants write permissions to sandbox UID.
    generate = ProductCommand(argv=("python3", "-B", "-c", (
        "from pathlib import Path;Path('generated.txt').write_text('build output')"
    )), purpose="build")
    tools.runtime_plan = plan.model_copy(
        update={"commands": {**plan.commands, "generate": generate}}
    )
    before = workspace.snapshot().tree_hash
    try:
        result = tools.invoke(HarnessToolCall(
            call_id="generate", name="run_command", arguments={"command_id": "generate"}
        ), context.model_copy(update={"operation_id": "generate"}), CancellationToken())
        assert result.status == "ok", result
        after = workspace.snapshot().tree_hash
        assert before != after
        assert result.data["artifact"]["tree"] == after
        assert result.data["receipt"]["tree_hash"] == before
        repeated = tools.invoke(HarnessToolCall(
            call_id="repeat", name="run_command", arguments={"command_id": "generate"}
        ), context.model_copy(update={"operation_id": "repeat", "workspace_tree": after}),
            CancellationToken())
        assert repeated.status == "ok", repeated
        assert repeated.data["receipt"]["tree_hash"] == after
    finally:
        tools.session.close()


def test_real_build_and_test_commands_have_tree_bound_receipts(tmp_path):
    workspace, tools, context, _ = _runtime(tmp_path)
    try:
        for command_id in ("build", "tests"):
            result = tools.invoke(HarnessToolCall(
                call_id=command_id, name="run_command", arguments={"command_id": command_id}
            ), context.model_copy(update={"operation_id": command_id}), CancellationToken())
            assert result.status == "ok", result
            assert result.data["receipt"]["tree_hash"] == workspace.snapshot().tree_hash
            assert result.data["receipt"]["provenance"]["actor"] == "owner"
            assert result.data["artifact"]["complete"]
            if command_id == "tests":
                assert result.data["receipt"]["tests_run"] == 1
    finally:
        tools.session.close()


def test_real_product_command_cannot_bypass_a_revoked_lease(tmp_path):
    _, tools, context, _ = _runtime(tmp_path)
    tools.broker.authority.revoke()
    try:
        result = tools.invoke(HarnessToolCall(
            call_id="revoked", name="run_command", arguments={"command_id": "runtime_info"}
        ), context, CancellationToken())
        assert result.status == "denied"
        assert result.code == "HARNESS_LEASE_REVOKED"
    finally:
        tools.session.close()


def test_real_preview_fetch_and_cleanup_publish_no_host_port(tmp_path):
    _, tools, context, plan = _runtime(tmp_path)
    token = CancellationToken()
    preview = ProductPreview(tools.session, context.grant, token, plan)
    try:
        started = preview.start()
        assert started["host_reachable"] is False
        status, content_type, body = preview.fetch("/index.html")
        assert status == 200 and "html" in content_type
        assert body.decode() == "<h1>Preview 汉字</h1>"
        record = tools.session._record(started["handle_id"])
        inspected = tools.broker._docker([
            "inspect", "--format", "{{json .HostConfig.PortBindings}}", record.launched.name
        ])
        assert json.loads(inspected.stdout) in ({}, None)
        with pytest.raises(DomainError, match="Invalid preview request"):
            preview.fetch("//example.com/secret")
    finally:
        preview.close()
    assert token.cancelled
    assert tools.broker.inspect_command(record.launched.name)[0] == "missing"


def test_preview_respects_revocation(tmp_path):
    _, tools, context, plan = _runtime(tmp_path)
    preview = ProductPreview(tools.session, context.grant, CancellationToken(), plan)
    try:
        preview.start()
        tools.broker.authority.revoke()
        with pytest.raises(DomainError):
            preview.fetch("/")
    finally:
        preview.close()


def test_real_preview_returns_redirect_without_contacting_another_destination(tmp_path):
    _, tools, context, plan = _runtime(tmp_path)
    server = (
        "from http.server import HTTPServer,BaseHTTPRequestHandler\n"
        "class Handler(BaseHTTPRequestHandler):\n"
        " def do_GET(self):\n"
        "  self.send_response(302);self.send_header('Location','http://external.invalid/')\n"
        "  self.end_headers()\n"
        "HTTPServer(('127.0.0.1',8000),Handler).serve_forever()\n"
    )
    plan = plan.model_copy(update={
        "start_argv": ("python3", "-B", "-c", server),
        "readiness": ReadinessProbe(kind="tcp", port=8000),
    })
    preview = ProductPreview(tools.session, context.grant, CancellationToken(), plan)
    try:
        preview.start()
        status, _, body = preview.fetch("/")
        assert status == 302 and body == b""
    finally:
        preview.close()


def test_real_preview_cancellation_stops_the_owned_process(tmp_path):
    _, tools, context, plan = _runtime(tmp_path)
    token = CancellationToken()
    preview = ProductPreview(tools.session, context.grant, token, plan)
    started = preview.start()
    record = tools.session._record(started["handle_id"])
    try:
        token.cancel()
        assert tools.session.poll(started["handle_id"]).state == "cancelled"
        with pytest.raises(DomainError):
            preview.fetch("/")
        assert tools.broker.inspect_command(record.launched.name)[0] == "missing"
    finally:
        preview.close()


def _offline_wheel(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    wheels = tmp_path / "wheels"
    wheels.mkdir()
    pip_wheel, = (Path(ensurepip.__file__).parent / "_bundled").glob("pip-*.whl")
    shutil.copyfile(pip_wheel, wheels / pip_wheel.name)
    wheel = wheels / "harness_offline_fixture-1.0-py3-none-any.whl"
    with zipfile.ZipFile(wheel, "w") as archive:
        archive.writestr("harness_offline_fixture.py", "VALUE = 42\n")
        info = "harness_offline_fixture-1.0.dist-info/"
        archive.writestr(info + "METADATA", "Metadata-Version: 2.1\n"
                          "Name: harness-offline-fixture\nVersion: 1.0\n")
        archive.writestr(info + "WHEEL", "Wheel-Version: 1.0\nGenerator: harness-tests\n"
                          "Root-Is-Purelib: true\nTag: py3-none-any\n")
        archive.writestr(info + "RECORD", "")
    checksum = hashlib.sha256(wheel.read_bytes()).hexdigest()
    (project / "requirements.txt").write_text(
        f"harness-offline-fixture==1.0 --hash=sha256:{checksum}\n"
    )
    (project / "test_value.py").write_text(
        "import unittest\nfrom harness_offline_fixture import VALUE\n"
        "class ValueTest(unittest.TestCase):\n"
        " def test_value(self): self.assertEqual(VALUE,42)\n"
    )
    return project, wheels


def test_real_offline_preparation_installs_wheels_and_derives_an_immutable_image(tmp_path):
    project, wheels = _offline_wheel(tmp_path)
    destination = tmp_path / "prepared.json"
    receipt = None
    try:
        receipt = prepare_product_environment(
            project, LOCK, wheelhouse=wheels, destination_lock=destination
        )
        assert receipt["status"] == "prepared", receipt
        assert receipt["network"] == "none"
        assert receipt["image_id"] != json.loads(LOCK.read_text())["image_id"]
        assert receipt["readiness"]["status"] == "ready", receipt
        lock = json.loads(destination.read_text())
        assert lock["preparation"] == "offline-hash-pinned-wheels"
        assert lock["requirements_sha256"] == hashlib.sha256(
            (project / "requirements.txt").read_bytes()
        ).hexdigest()
        assert set(lock["wheel_hashes"]) == {entry.name for entry in wheels.iterdir()}
        lease = issue_lease(
            mode="investigate", actor="owner", workspace_id="prepared-project",
            repository="local/prepared", execution_epoch=1, read_paths=("",),
            allowed_commands=("python3",), timeout_seconds=30,
        )
        from mgar_v3.harness.broker import IsolatedExecutionBroker
        from mgar_v3.harness.contracts import ExecutionRequest

        broker = IsolatedExecutionBroker(project, lease, lock_path=destination)
        result = broker.execute(ExecutionRequest(
            operation_id="prepared-import", purpose="test",
            argv=("python3", "-B", "-m", "unittest", "discover", "-v"),
            cwd=".", timeout_seconds=30, execution_epoch=1,
        ), CapabilityGrant(
            actor=lease.actor, workspace_id=lease.workspace_id, repository=lease.repository,
            read_paths=("",), execute=True, allowed_commands=lease.allowed_commands,
        ), CancellationToken())
        assert result.exit_code == 0, result
        assert "Ran 1 test" in result.stdout
        with pytest.raises(DomainError, match="cannot be overwritten"):
            prepare_product_environment(project, LOCK, destination_lock=destination)
        assert not list(tmp_path.glob("product-prep-*"))
    finally:
        if receipt is not None:
            subprocess.run(["docker", "image", "rm", receipt["image_id"]],
                           capture_output=True, check=False)


def test_offline_preparation_rejects_project_assets_links_and_unpinned_sources(tmp_path):
    project, wheels = _offline_wheel(tmp_path)
    with pytest.raises(DomainError, match="outside project"):
        prepare_product_environment(project, LOCK, destination_lock=project / "image.json")
    link = tmp_path / "linked-wheels"
    link.symlink_to(wheels, target_is_directory=True)
    with pytest.raises(DomainError, match="Symlink"):
        prepare_product_environment(project, LOCK, wheelhouse=link,
                                    destination_lock=tmp_path / "image.json")
    (project / "requirements.txt").write_text("--index-url https://example.org/\n")
    with pytest.raises(DomainError, match="hashes"):
        prepare_product_environment(project, LOCK, wheelhouse=wheels,
                                    destination_lock=tmp_path / "image.json")


def test_offline_preparation_rejects_modified_wheel_bytes(tmp_path):
    project, wheels = _offline_wheel(tmp_path)
    package = wheels / "harness_offline_fixture-1.0-py3-none-any.whl"
    with zipfile.ZipFile(package, "a") as archive:
        archive.writestr("extra.py", "raise Exception('must never import')")
    with pytest.raises(DomainError, match="Offline installation failed"):
        prepare_product_environment(project, LOCK, wheelhouse=wheels,
                                    destination_lock=tmp_path / "image.json")
    assert not list(tmp_path.glob("product-prep-*"))
    assert not (tmp_path / "image.json").exists()


def test_pinned_image_selection_prepares_without_wheelhouse_or_project_execution(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    (project / "index.html").write_text("<h1>hello</h1>")
    receipt = prepare_product_environment(
        project, LOCK, destination_lock=tmp_path / "selected.json"
    )
    assert receipt["status"] == "ready"
    assert receipt["image_id"] == json.loads(LOCK.read_text())["image_id"]
    assert json.loads((tmp_path / "selected.json").read_text())["preparation"] == (
        "pinned-image-selection"
    )
