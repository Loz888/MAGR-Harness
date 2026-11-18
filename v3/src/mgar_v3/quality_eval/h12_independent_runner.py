"""Independent, complete public suites on sealed read-only source copies."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal
from uuid import uuid4

from mgar_v3.domain import DomainError, digest
from mgar_v3.evidence.artifacts import ArtifactStore
from mgar_v3.harness.broker import IsolatedExecutionBroker, issue_lease
from mgar_v3.harness.commands import CommandSession
from mgar_v3.harness.contracts import CancellationToken, CapabilityGrant, ExecutionRequest
from mgar_v3.harness.regression_types import (
    IndependentRegressionReport,
    IndependentTestReceipt,
    RegressionPlan,
    seal,
)
from mgar_v3.harness.test_results import parse_independent_test
from mgar_v3.harness.workspace import _copy_tree_nofollow, _scan, _tree_hash


def _write(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w") as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2)
        stream.flush()
        os.fsync(stream.fileno())


class IndependentRegressionRunner:
    def __init__(
        self,
        artifacts: ArtifactStore,
        image_lock: Path,
        *,
        repository: str,
        task_id: str,
        experiment_id: str,
        memory_mb: int = 256,
        nofile: int = 128,
        log_bytes: int = 1048576,
        offline_network: Literal["none", "connect-timeout-v1", "got-network-v1"] = "none",
        public_probe_root: Path | None = None,
    ) -> None:
        self.artifacts, self.image_lock = artifacts, image_lock
        self.repository, self.task_id, self.experiment_id = repository, task_id, experiment_id
        self.memory_mb, self.nofile, self.log_bytes = memory_mb, nofile, log_bytes
        self.offline_network, self.public_probe_root = offline_network, public_probe_root
        lock = json.loads(image_lock.read_bytes())
        self.image_id = lock["image_id"]
        self.environment_spec_hash = digest(
            {
                "image_lock_sha256": hashlib.sha256(image_lock.read_bytes()).hexdigest(),
                "image_id": self.image_id,
                "memory_mb": memory_mb,
                "nofile": nofile,
                "log_bytes": log_bytes,
                "offline_network": offline_network,
                "scope": "sealed-read-only-source-no-private-overlays",
            }
        )

    def execute(
        self,
        source: Path,
        plan: RegressionPlan,
        evidence: Path,
        *,
        phase: Literal["baseline", "final"],
        expected_tree: str,
        frozen_tests: Mapping[str, str],
        sealed: bool = False,
        execution_task_id: str | None = None,
        run_id: str | None = None,
        execution_epoch: int | None = None,
        role: Literal[
            "public_regression", "target_behavior", "structural_constraints"
        ] = "public_regression",
        cancellation: CancellationToken | None = None,
    ) -> tuple[str, IndependentRegressionReport]:
        from mgar_v3.quality_eval.h12_judge import test_hashes
        from mgar_v3.quality_eval.h12_regression_scoring import load_report

        source = source.resolve()
        token = cancellation or CancellationToken()
        files, links = _scan(source)
        before = _tree_hash(files)
        observed_tests = test_hashes(source)
        test_hash = digest(dict(sorted(frozen_tests.items())))
        receipts: list[str] = []
        detail = "All frozen public suites executed independently"
        status: Literal["PASS", "FAIL", "NOT_RUN", "BLOCKED"] = "PASS"

        def report() -> tuple[str, IndependentRegressionReport]:
            after = _tree_hash(_scan(source)[0])
            value = seal(
                IndependentRegressionReport,
                dict(
                    task_id=self.task_id,
                    experiment_id=self.experiment_id,
                    execution_task_id=execution_task_id,
                    run_id=run_id,
                    execution_epoch=execution_epoch,
                    phase=phase,
                    role=role,
                    status=status,
                    tree_hash=expected_tree,
                    observed_tree_before=before,
                    observed_tree_after=after,
                    public_tests_hash=test_hash,
                    plan_hash=plan.plan_hash,
                    environment_spec_hash=self.environment_spec_hash,
                    image_id=self.image_id,
                    suite_ids=tuple(s.suite_id for s in plan.commands),
                    receipt_refs=tuple(receipts),
                    detail=detail,
                ),
            )
            ref = self.artifacts.put(value.model_dump_json().encode())
            return ref, value

        if phase == "final" and (
            not sealed or not run_id or not execution_task_id or not execution_epoch
        ):
            status, detail = "NOT_RUN", "Final source needs original Engine seal and Run identity"
            return report()
        if (
            before != expected_tree
            or links
            or any(observed_tests.get(p) != h for p, h in frozen_tests.items())
        ):
            status, detail = "FAIL", "Source tree or frozen public tests differ"
            return report()
        if plan.package_scripts:
            try:
                package = json.loads((source / "package.json").read_bytes())
                scripts = package["scripts"]
                if any(scripts.get(k) != v for k, v in plan.package_scripts.items()):
                    raise ValueError("Frozen regression scripts changed")
            except (OSError, ValueError, KeyError, TypeError, AttributeError):
                status, detail = "FAIL", "Existing public verification scripts changed"
                return report()
        try:
            from mgar_v3.quality_eval.h12_expected_failures import validate_expected_failures

            validate_expected_failures(source, plan)
        except (DomainError, OSError, ValueError):
            status, detail = "BLOCKED", "Frozen expected-failure source binding differs"
            return report()
        if not plan.commands or any(s.framework == "unconfigured" for s in plan.commands):
            status, detail = "NOT_RUN", "No complete frozen public command/reporter plan"
            return report()
        path = evidence / "report-ref.json"
        if path.is_file():
            try:
                ref = json.loads(path.read_bytes())["artifact_hash"]
                old, _ = load_report(
                    self.artifacts,
                    ref,
                    plan,
                    task_id=self.task_id,
                    experiment_id=self.experiment_id,
                    phase=phase,
                    tree=expected_tree,
                    role=role,
                    execution_task_id=execution_task_id,
                    run_id=run_id,
                    execution_epoch=execution_epoch,
                    environment_spec_hash=self.environment_spec_hash,
                )
                if old.public_tests_hash != test_hash:
                    raise ValueError("Frozen public tests differ")
                return ref, old
            except (ValueError, KeyError, OSError, DomainError):
                status, detail = "BLOCKED", "Existing independent evidence requires reconciliation"
                return report()
        if (evidence / "dispatch.json").exists() or evidence.exists() and any(evidence.iterdir()):
            status, detail = (
                "BLOCKED",
                "Independent execution was dispatched without a verified final receipt",
            )
            return report()
        _write(
            evidence / "dispatch.json",
            dict(
                plan=plan.model_dump(mode="json"),
                tree_hash=expected_tree,
                phase=phase,
                task_id=self.task_id,
                experiment_id=self.experiment_id,
                run_id=run_id,
                environment_spec_hash=self.environment_spec_hash,
            ),
        )
        copy_root = Path(tempfile.mkdtemp(prefix="magr-h12-regression-", dir="/tmp"))
        try:
            target = copy_root / "workspace"
            _copy_tree_nofollow(source, target, files)
            if (
                _tree_hash(_scan(target)[0]) != expected_tree
                or _tree_hash(_scan(source)[0]) != expected_tree
            ):
                raise DomainError("INDEPENDENT_TREE_DRIFT", "Source moved while copying", 409)
            copy_root.chmod(0o755)
            for path_item in [target, *target.rglob("*")]:
                if not path_item.is_symlink():
                    path_item.chmod(
                        path_item.stat().st_mode | (0o555 if path_item.is_dir() else 0o444)
                    )
            for index, suite in enumerate(plan.commands):
                if token.cancelled:
                    status, detail = (
                        "BLOCKED",
                        "Independent execution cancelled; remaining suites not run",
                    )
                    break
                lease = issue_lease(
                    mode="investigate",
                    actor="h12-regression",
                    workspace_id="reg-" + uuid4().hex,
                    repository=self.repository,
                    execution_epoch=execution_epoch or 1,
                    read_paths=("",),
                    write_paths=(),
                    allowed_commands=(suite.argv[0],),
                    timeout_seconds=min(600, int(plan.timeout_seconds + 1)),
                    max_output_bytes=self.log_bytes,
                    memory_mb=self.memory_mb,
                    nofile_limit=self.nofile,
                    scratch_mb=256,
                    offline_network=self.offline_network,
                )
                grant = CapabilityGrant(
                    actor=lease.actor,
                    workspace_id=lease.workspace_id,
                    repository=self.repository,
                    read_paths=("",),
                    write_paths=(),
                    execute=True,
                    allowed_commands=lease.allowed_commands,
                    max_seconds=min(600, int(plan.timeout_seconds + 1)),
                )
                broker = IsolatedExecutionBroker(
                    target,
                    lease,
                    lock_path=self.image_lock,
                    public_probe=suite.public_probe,
                    public_probe_root=self.public_probe_root if suite.public_probe else None,
                )
                session = CommandSession(broker)
                try:
                    request = ExecutionRequest(
                        operation_id="reg-" + uuid4().hex,
                        purpose="test",
                        argv=suite.argv,
                        cwd=suite.cwd,
                        timeout_seconds=plan.timeout_seconds,
                        execution_epoch=lease.execution_epoch,
                    )
                    handle = session.run(request, grant, token, purpose="test")
                    finished = session.wait(handle.handle_id)
                    chunks = []
                    offset = 0
                    complete = not finished.log_truncated
                    while True:
                        page = session.read_log(handle.handle_id, offset=offset)
                        complete = (
                            complete
                            and page.offset == offset
                            and not page.truncated
                            and not page.dropped_bytes
                        )
                        chunks.append(page.data)
                        if page.eof:
                            break
                        if page.next_offset <= offset:
                            complete = False
                            break
                        offset = page.next_offset
                    raw = "".join(chunks).encode()
                    complete = (
                        complete
                        and len(raw) == finished.log_bytes
                        and hashlib.sha256(raw).hexdigest() == finished.log_sha256
                        and finished.state in {"exited", "failed"}
                    )
                    observed_after = _tree_hash(_scan(target)[0])
                    observation = parse_independent_test(
                        suite, raw.decode(), finished.exit_code, complete=complete
                    )
                    value = seal(
                        IndependentTestReceipt,
                        dict(
                            task_id=self.task_id,
                            experiment_id=self.experiment_id,
                            execution_task_id=execution_task_id,
                            run_id=run_id,
                            execution_epoch=execution_epoch,
                            phase=phase,
                            role=role,
                            suite_id=suite.suite_id,
                            plan_hash=plan.plan_hash,
                            tree_hash=expected_tree,
                            verifier_tree_before=expected_tree,
                            verifier_tree_after=observed_after,
                            public_tests_hash=test_hash,
                            public_argv=suite.public_argv,
                            argv=suite.argv,
                            cwd=suite.cwd,
                            environment_hash=finished.environment_hash,
                            environment_spec_hash=self.environment_spec_hash,
                            image_id=self.image_id,
                            command_id=finished.operation_id,
                            handle_id=finished.handle_id,
                            state=finished.state,
                            exit_code=finished.exit_code,
                            started_at=finished.started_at,
                            finished_at=datetime.now(UTC).isoformat(),
                            log_hash=self.artifacts.put(raw),
                            log_bytes=len(raw),
                            log_complete=complete,
                            log_truncated=finished.log_truncated,
                            result=observation,
                            diagnostics=finished.diagnostics,
                        ),
                    )
                    ref = self.artifacts.put(value.model_dump_json().encode())
                    receipts.append(ref)
                    _write(
                        evidence / f"command-{index}.json",
                        {"artifact_hash": ref, "receipt": value.model_dump(mode="json")},
                    )
                    if observation.status != "PASS":
                        status = (
                            "BLOCKED"
                            if observation.status in {"BLOCKED", "NOT_RUN"} or status == "BLOCKED"
                            else "FAIL"
                        )
                        detail = "One or more independent suites failed or were incomplete"
                    if observed_after != expected_tree:
                        status, detail = "FAIL", "Verifier copy changed during independent tests"
                finally:
                    session.close()
            if _tree_hash(_scan(source)[0]) != expected_tree:
                status, detail = "FAIL", "Sealed source changed during independent tests"
        except (DomainError, OSError, ValueError) as exc:
            status, detail = (
                "BLOCKED",
                "Independent execution unverified: " + getattr(exc, "code", type(exc).__name__),
            )
        finally:
            shutil.rmtree(copy_root)
        ref, report_value = report()
        _write(
            evidence / "report-ref.json",
            {"artifact_hash": ref, "receipt_id": report_value.receipt_id},
        )
        return ref, report_value
