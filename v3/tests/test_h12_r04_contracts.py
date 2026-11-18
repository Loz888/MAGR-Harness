"""Frozen control/inventory/route contracts; execution facts are tested separately."""

import json

import pytest
from pydantic import ValidationError
from test_harness_v4_h07 import _task

from mgar_v3.domain import DomainError
from mgar_v3.harness.contracts import AcceptanceScope
from mgar_v3.harness.entrypoints import EntryPointChecker
from mgar_v3.harness.lifecycle_plan import public_routes
from mgar_v3.harness.lifecycle_types import EntryPointSpec, LifecyclePolicy, RuntimeControl
from mgar_v3.harness.workspace import WorkspaceRegistry


@pytest.mark.parametrize(
    "path,runtime",
    [
        (".", "python"),
        ("../app.py", "python"),
        ("/etc/passwd", "python"),
        ("app.js", "python"),
        ("app.py", "node"),
    ],
)
def test_entry_loader_paths_are_explicit_relative_files(path, runtime):
    with pytest.raises((DomainError, ValidationError)):
        EntryPointSpec(path=path, runtime=runtime)


def test_runtime_control_shape_and_frozen_asset_binding():
    policy = LifecyclePolicy()
    entry = EntryPointSpec(path="app.py", runtime="python")
    for value in (
        {"mode": "entry"},
        {"mode": "http", "entry": entry},
        {"mode": "oneshot", "entry": entry},
    ):
        with pytest.raises(ValidationError):
            RuntimeControl(**value, assets_hash=policy.control_assets_hash)
    control = RuntimeControl(mode="entry", entry=entry, assets_hash=policy.control_assets_hash)
    assert RuntimeControl.model_validate_json(control.model_dump_json()) == control


@pytest.mark.parametrize(
    "goal,path,status,content_type,body,json_body",
    [
        ('GET / returning "ok".', "/", 200, None, "ok", None),
        (
            'GET `/healthz` returning JSON {"status":"ok"}.',
            "/healthz",
            200,
            "application/json",
            None,
            {"status": "ok"},
        ),
        ('GET /ready returning 204 text/plain "".', "/ready", 204, "text/plain", "", None),
        ('GET / returning `"ok"`.', "/", 200, None, "ok", None),
    ],
)
def test_route_contract_comes_from_explicit_public_goal(
    goal,
    path,
    status,
    content_type,
    body,
    json_body,
):
    routes = public_routes(goal)
    assert len(routes) == 1
    assert routes[0].model_dump() == {
        "path": path,
        "status_code": status,
        "content_type": content_type,
        "body": body,
        "json_body": json_body,
    }
    assert public_routes("Inspect the project") == ()


@pytest.mark.parametrize("variant", ["package", "npm", "src", "modified", "missing"])
def test_real_workspace_inventory_includes_package_scripts_new_and_modified_entries(
    tmp_path, variant
):
    source = tmp_path / "source"
    source.mkdir()
    (source / "old.cjs").write_text("module.exports=7\n")
    (source / "package.json").write_text('{"main":"old.cjs"}')
    registry = WorkspaceRegistry(tmp_path / "registry")
    registration = registry.register_local(source, actor="owner")
    workspace = registry.create_task_workspace(registration, "r04-inventory")
    baseline = dict(workspace.snapshot().files)
    argv = ()
    target = "new.cjs"
    if variant == "package":
        (workspace.root / "package.json").write_text(
            json.dumps(
                {
                    "main": "new.cjs",
                    "exports": {".": {"require": "./new.cjs", "import": "./new.mjs"}},
                }
            )
        )
    elif variant == "npm":
        target = "scripts/selfcheck.mjs"
        (workspace.root / "package.json").write_text(
            json.dumps(
                {
                    "scripts": {"selfcheck": "node scripts/selfcheck.mjs"},
                }
            )
        )
        argv = ("npm", "run", "selfcheck")
    elif variant == "src":
        target = "src/pkg/entry.py"
    elif variant == "modified":
        target = "old.cjs"
    if variant != "missing":
        path = workspace.root / target
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("VALUE=7\n" if target.endswith(".py") else "export const value=7;\n")
    task = _task(
        "bugfix" if variant == "modified" else "feature",
        objective="Module loadable without binding",
    )
    task = task.model_copy(
        update={
            "acceptance_scope": AcceptanceScope(
                repository=task.repository,
                workspace_id=task.workspace_id,
                read_paths=("",),
                write_paths=(target,),
                execute=True,
                allowed_commands=("python3",),
            )
        }
    )
    checker = EntryPointChecker(
        workspace,
        None,
        task,
        None,
        None,
        LifecyclePolicy(),
        ("python3", "-m", "unittest"),
        service_argv=argv,
    )
    specs = checker.inventory(baseline)
    assert target in {spec.path for spec in specs}
    assert all(spec.no_listen_on_import for spec in specs)
    if variant == "package":
        assert "new.mjs" in {spec.path for spec in specs}
    if variant == "src":
        assert next(spec for spec in specs if spec.path == target).import_roots == ("src", "")
