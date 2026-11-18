"""Offline public-probe protocol and actual, task-owned Docker boundaries."""

from __future__ import annotations

import json
import os
from pathlib import Path
from uuid import uuid4

import pytest
from pydantic import ValidationError

from mgar_v3.domain import DomainError, digest
from mgar_v3.harness.broker import CapabilityAuthority, issue_lease
from mgar_v3.harness.commands import CommandSession
from mgar_v3.harness.contracts import (
    CancellationToken,
    CapabilityGrant,
    ExecutionProvenance,
    ExecutionReceipt,
    ExecutionRequest,
    TaskBinding,
    VerificationRequirement,
    WorkspaceEdit,
    sha256_text,
)
from mgar_v3.harness.public_probe import normalize_probe_receipt, read_asset
from mgar_v3.harness.public_probe_types import PUBLIC_PROBE_PREFIX, PublicProbePlan
from mgar_v3.harness.service import WorkspaceBoundBroker
from mgar_v3.harness.workspace import WorkspaceRegistry

ROOT = Path(__file__).resolve().parents[1]
LOCK = ROOT / ".local/r03/sandbox-lock.json"
PLAN = PublicProbePlan(plan_id="toy-add", asset_sha256="a" * 64, case_ids=("sum", "zero"))


def task(**changes):
    values = dict(
        task_id="public-probe-test",
        thread_id=uuid4(),
        run_id=uuid4(),
        execution_epoch=3,
        actor="owner",
        repository="local/source",
        base_commit="0" * 40,
        workspace_id="probe-workspace",
        acceptance_revision="public-probe-v1",
        objective="Correct arithmetic and verify its public behavior.",
        requirements=(VerificationRequirement(requirement_id="probe"),),
    )
    return TaskBinding(**{**values, **changes})


def output(plan=PLAN, statuses=("passed", "passed")):
    return (
        PUBLIC_PROBE_PREFIX
        + json.dumps(
            {
                "plan_id": plan.plan_id,
                "asset_sha256": plan.asset_sha256,
                "cases": [
                    {"id": name, "status": status}
                    for name, status in zip(plan.case_ids, statuses, strict=True)
                ],
            }
        )
        + "\n"
    )


def raw_receipt(stdout=None, **changes):
    text = output() if stdout is None else stdout
    provenance = ExecutionProvenance(
        operation_id="public-probe-test:probe",
        lease_id="probe-lease",
        lease_hash="1" * 64,
        actor="owner",
        workspace_id="probe-workspace",
        repository="local/source",
        execution_epoch=3,
        read_paths=("",),
        write_paths=("calc.py",),
        protected_paths=(".git", ".mgar", ".env"),
        grant_hash="2" * 64,
    )
    values = dict(
        receipt_id=sha256_text(text),
        operation_id=provenance.operation_id,
        purpose="test",
        status="passed",
        argv=PLAN.argv,
        cwd=".",
        exit_code=0,
        stdout=text,
        stderr="",
        duration_seconds=0.1,
        tree_hash="b" * 64,
        log_sha256=sha256_text(text),
        environment_hash="c" * 64,
        provenance=provenance,
    )
    return ExecutionReceipt(**{**values, **changes})


def normalize(raw, **changes):
    args = dict(task=task(), expected_tree="b" * 64, log_complete=True, log_limit_bytes=65536)
    return normalize_probe_receipt(PLAN, raw, **{**args, **changes})


def test_exact_public_result_retains_raw_and_provenance_and_binds_derived_identity():
    raw = raw_receipt()
    green = normalize(raw)
    assert green.outcome == "green" and green.execution.tests_run == 2
    assert green.source_receipt_id == raw.receipt_id != green.execution.receipt_id
    assert green.execution.provenance == raw.provenance
    metadata = green.execution.model_dump(mode="json")["public_probe"]
    assert metadata["plan_hash"] == PLAN.plan_hash
    assert metadata["source_receipt_id"] == raw.receipt_id
    red = normalize(
        raw_receipt(output(statuses=("assertion_failed", "passed")), status="failed", exit_code=1)
    )
    assert red.outcome == "red" and red.execution.tests_failed == 1
    assert raw.tests_run == 0 and raw.status == "passed"


@pytest.mark.parametrize(
    "change",
    [
        {"argv": ("python3", "-c", "print('not an approved probe')")},
        {"argv": (*PLAN.argv, "--extra")},
        {"purpose": "command"},
        {"tree_hash": "d" * 64},
        {"provenance": None},
        {"status": "timeout", "exit_code": 124},
        {"status": "error", "exit_code": 2},
        {"status": "failed", "exit_code": 1},
        {"status": "passed", "exit_code": 1},
        {"log_sha256": "0" * 64},
    ],
)
def test_mismatched_command_tree_execution_and_hash_never_verify(change):
    assert normalize(raw_receipt(**change)).outcome == "invalid"


@pytest.mark.parametrize(
    "text",
    [
        "",
        "collection error\n",
        output() + output(),
        "untrusted prefix\n" + output(),
        output() + "extra stdout\n",
        output().rstrip("\n"),
        output().replace("\n", "\r\n"),
        output().replace('"sum"', '"other-case"'),
        output().replace('"zero"', '"sum"'),
        output().replace('"plan_id": "toy-add"', '"plan_id": "other"'),
        output().replace(
            '"asset_sha256": "' + "a" * 64 + '"', '"asset_sha256": "' + "d" * 64 + '"'
        ),
        output().replace('"plan_id": "toy-add"', '"plan_id":"toy-add","plan_id":"toy-add"'),
    ],
)
def test_exactly_one_frame_exact_cases_and_identity_are_required(text):
    assert normalize(raw_receipt(text)).outcome == "invalid"


@pytest.mark.parametrize(
    "states",
    [
        ("error", "passed"),
        ("skipped", "passed"),
        ("xfailed", "passed"),
        ("error", "assertion_failed"),
        ("skipped", "assertion_failed"),
        ("xfailed", "assertion_failed"),
    ],
)
def test_errors_skip_and_expected_failure_cannot_substitute_for_red_or_green(states):
    raw = raw_receipt(output(statuses=states), status="failed", exit_code=1)
    assert normalize(raw).outcome == "invalid"


def test_log_completeness_and_generating_identity_are_explicit():
    raw = raw_receipt()
    assert normalize(raw, log_complete=False).reason_code == "PUBLIC_PROBE_LOG_INCOMPLETE"
    assert normalize(raw, log_limit_bytes=len(raw.stdout.encode())).outcome == "invalid"
    assert normalize(raw, task=task(actor="other")).outcome == "invalid"
    assert normalize(raw, task=task(workspace_id="other")).outcome == "invalid"
    assert normalize(raw, task=task(repository="other/repo")).outcome == "invalid"
    assert normalize(raw, task=task(execution_epoch=2)).outcome == "invalid"
    restored = normalize(raw, task=task(execution_epoch=4))
    assert restored.outcome == "green" and restored.execution_epoch == 3
    assert restored.execution.provenance.execution_epoch == 3
    stderr = raw_receipt(stderr="warning\n", log_sha256=sha256_text(raw.stdout + "warning\n"))
    assert normalize(stderr).outcome == "invalid"


def test_plan_rejects_empty_duplicate_and_unsafe_ids_or_arbitrary_argv():
    for changes in (
        {"case_ids": ()},
        {"case_ids": ("sum", "sum")},
        {"case_ids": ("../case",)},
        {"runner": "shell"},
        {"argv": ["sh", "-c", "x"]},
    ):
        with pytest.raises(ValidationError):
            PublicProbePlan.model_validate({**PLAN.model_dump(mode="json"), **changes})


def store_asset(root: Path, content: bytes):
    sha = sha256_text(content.decode())
    directory = root / sha
    directory.mkdir(parents=True)
    path = directory / "probe.py"
    path.write_bytes(content)
    return PublicProbePlan(plan_id="toy-add", asset_sha256=sha, case_ids=("sum",)), path


def test_asset_is_single_regular_hash_bound_file_and_all_path_components_nofollow(tmp_path):
    root = tmp_path / "controller"
    plan, asset = store_asset(root, b"print('public asset')\n")
    assert read_asset(plan, root) == asset.read_bytes()
    asset.write_text("print('changed')\n")
    with pytest.raises(DomainError) as error:
        read_asset(plan, root)
    assert error.value.code == "PUBLIC_PROBE_ASSET_HASH"
    other = tmp_path / "other.py"
    other.write_text("print('public asset')\n")
    asset.unlink()
    asset.symlink_to(other)
    with pytest.raises(DomainError):
        read_asset(plan, root)
    asset.unlink()
    asset.write_bytes(other.read_bytes())
    link = tmp_path / "root-alias"
    link.symlink_to(root, target_is_directory=True)
    with pytest.raises(DomainError):
        read_asset(plan, link)
    asset.unlink()
    asset.mkdir()
    with pytest.raises(DomainError):
        read_asset(plan, root)
    asset.rmdir()
    os.mkfifo(asset)
    with pytest.raises(DomainError):
        read_asset(plan, root)
    asset.unlink()
    asset.parent.rmdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "probe.py").write_bytes(other.read_bytes())
    asset.parent.symlink_to(outside, target_is_directory=True)
    with pytest.raises(DomainError):
        read_asset(plan, root)


SCRIPT = b"""import contextlib,hashlib,io,json,sys
from pathlib import Path
stream=io.StringIO()
status="passed"
try:
    try:
        with open(__file__,"a") as target: target.write("unexpected write")
        raise RuntimeError("controller asset was writable")
    except OSError:
        pass
    assert len(list(Path(__file__).parent.iterdir())) == 1
    sys.path.insert(0,str(Path.cwd()))
    with contextlib.redirect_stdout(stream),contextlib.redirect_stderr(stream):
        from calc import add
        assert add(2,3)==5
except AssertionError:
    status="assertion_failed"
except Exception:
    status="error"
if stream.getvalue(): status="error"
result={"plan_id":"toy-add",
        "asset_sha256":hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "cases":[{"id":"sum", "status":status}]}
print("HARNESS_PUBLIC_PROBE_V1 "+json.dumps(result))
sys.exit(0 if status=="passed" else 1 if status=="assertion_failed" else 2)
"""


def test_read_public_asset_rechecks_bytes_before_exposing_them(tmp_path, monkeypatch):
    plan, asset = store_asset(tmp_path / "controller", b"print('frozen public asset')\n")
    original, reads = os.read, 0

    def change_after_hash(descriptor, size):
        nonlocal reads
        reads += 1
        if reads == 2:
            asset.write_text("print('unapproved changed bytes')\n")
        return original(descriptor, size)

    monkeypatch.setattr(os, "read", change_after_hash)
    with pytest.raises(DomainError) as error:
        read_asset(plan, asset.parent.parent)
    assert error.value.code == "PUBLIC_PROBE_ASSET_CHANGED"


@pytest.fixture
def real_probe(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "calc.py").write_text("def add(a,b):\n    return a-b\n")
    registry = WorkspaceRegistry(tmp_path / "registry")
    registration = registry.register_local(source, actor="owner")
    workspace = registry.create_task_workspace(
        registration, "public-probe-test", source_root=source
    )
    for path in (workspace.root, *workspace.root.parents):
        if path == Path("/tmp"):
            break
        path.chmod(0o755)
    (workspace.root / "calc.py").chmod(0o644)
    plan, asset = store_asset(tmp_path / "controller", SCRIPT)
    (asset.parent / "not-mounted.txt").write_text("Only probe.py may enter the sandbox.")
    lease = issue_lease(
        mode="modify_test",
        actor="owner",
        workspace_id=registration.workspace_id,
        repository=registration.repository,
        execution_epoch=3,
        read_paths=("",),
        write_paths=("calc.py",),
        allowed_commands=("python3",),
        timeout_seconds=10,
        approved_by="owner",
        approval_ref="controlled-public-probe",
    )
    authority = CapabilityAuthority(lease, budget_check=lambda: True)
    broker = WorkspaceBoundBroker(
        workspace,
        authority,
        lock_path=LOCK,
        public_probe=plan,
        public_probe_root=tmp_path / "controller",
    )
    grant = CapabilityGrant(
        actor="owner",
        workspace_id=registration.workspace_id,
        repository=registration.repository,
        read_paths=("",),
        write_paths=("calc.py",),
        execute=True,
        allowed_commands=("python3",),
    )
    session = CommandSession(broker, page_bytes=8192, max_buffer_bytes=65536)
    yield dict(
        workspace=workspace,
        plan=plan,
        asset=asset,
        broker=broker,
        grant=grant,
        session=session,
        task=task(workspace_id=registration.workspace_id, repository=registration.repository),
    )
    session.close()


def run_real(value, operation):
    session, plan = value["session"], value["plan"]
    request = ExecutionRequest(
        operation_id=operation,
        purpose="test",
        argv=plan.argv,
        execution_epoch=3,
        timeout_seconds=10,
    )
    handle = session.run(request, value["grant"], CancellationToken(), purpose="test")
    final = session.wait(handle.handle_id, timeout=15)
    page = session.read_log(handle.handle_id)
    assert page.eof
    provenance = session.assert_authority(handle.handle_id)
    raw = ExecutionReceipt(
        receipt_id=digest(final.model_dump(mode="json")),
        operation_id=operation,
        purpose="test",
        status="passed" if final.exit_code == 0 else "failed",
        argv=plan.argv,
        cwd=".",
        exit_code=final.exit_code,
        stdout=page.data,
        stderr="",
        duration_seconds=0,
        tree_hash=final.tree_hash,
        log_sha256=sha256_text(page.data),
        environment_hash=final.environment_hash,
        provenance=provenance,
    )
    return normalize_probe_receipt(
        plan,
        raw,
        task=value["task"],
        expected_tree=value["workspace"].snapshot().tree_hash,
        log_complete=not (page.truncated or page.dropped_bytes),
        log_limit_bytes=65536,
    )


def test_actual_broker_original_assertion_red_then_same_readonly_probe_green(real_probe):
    value = real_probe
    red = run_real(value, "public-probe-test:baseline")
    assert red.outcome == "red", red.execution.stdout
    workspace = value["workspace"]
    before = workspace.snapshot()
    workspace.apply(
        (
            WorkspaceEdit(
                path="calc.py",
                expected_sha256=sha256_text(before.files["calc.py"]),
                content="def add(a,b):\n    return a+b\n",
            ),
        ),
        expected_tree=before.tree_hash,
    )
    green = run_real(value, "public-probe-test:candidate")
    assert green.outcome == "green", green.execution.stdout
    assert green.plan_hash == red.plan_hash and green.asset_sha256 == red.asset_sha256
    assert green.tree_hash != red.tree_hash
    assert value["asset"].read_bytes() == SCRIPT


def test_actual_entrypoint_catches_same_inode_asset_mutation_before_execution(
    real_probe, monkeypatch
):
    value = real_probe
    original = value["broker"]._docker

    def swap(args, **kwargs):
        result = original(args, **kwargs)
        if args[0] == "create":
            value["asset"].write_text("raise RuntimeError('must not execute')\n")
        return result

    monkeypatch.setattr(value["broker"], "_docker", swap)
    result = run_real(value, "public-probe-test:asset-race")
    assert result.outcome == "invalid"
    assert "PUBLIC_PROBE_ASSET_CHANGED" in result.execution.stdout
    assert "must not execute" not in result.execution.stdout


def test_only_exact_fixed_argv_can_receive_controller_mount(real_probe):
    value = real_probe
    request = ExecutionRequest(
        operation_id="not-the-probe",
        purpose="test",
        argv=(*value["plan"].argv, "extra"),
        execution_epoch=3,
    )
    lease = value["broker"].authority.peek()
    args, _ = value["broker"]._create_args("controlled-mount-negative", request, lease, ("",), ())
    try:
        assert not any("source=" + str(value["asset"]) in arg for arg in args)
    finally:
        value["broker"]._remove_placeholders("controlled-mount-negative")


def test_controller_root_cannot_be_inside_the_workspace_or_missing_half_of_config(real_probe):
    value = real_probe
    for changes in (
        {"public_probe": value["plan"]},
        {"public_probe_root": value["asset"].parent.parent},
        {"public_probe": value["plan"], "public_probe_root": value["workspace"].root / "assets"},
    ):
        with pytest.raises(DomainError):
            WorkspaceBoundBroker(
                value["workspace"], value["broker"].authority, lock_path=LOCK, **changes
            )


def test_probe_still_requires_original_execute_grant_actor_and_epoch(real_probe):
    value = real_probe
    for grant, epoch in (
        (value["grant"].model_copy(update={"execute": False}), 3),
        (value["grant"].model_copy(update={"actor": "other"}), 3),
        (value["grant"], 2),
    ):
        request = ExecutionRequest(
            operation_id="denied-probe",
            purpose="test",
            argv=value["plan"].argv,
            execution_epoch=epoch,
        )
        with pytest.raises(DomainError):
            value["session"].run(request, grant, CancellationToken(), purpose="test")
