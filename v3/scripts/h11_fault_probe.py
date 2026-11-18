"""Private H11 worker for actual process/PG restart acceptance.

The parent passes a mode-0600 fixture document.  This script never reads the
developer environment, calls a paid model, pulls an image, or prints a DSN.
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path
from typing import Any
from uuid import uuid4

import psycopg
from h11_helpers import fixture_settings, make_traversable, private_json, read_json

from mgar_v3.coordination.engine import Engine
from mgar_v3.domain import DomainError
from mgar_v3.evidence.artifacts import ArtifactStore
from mgar_v3.harness.broker import issue_lease
from mgar_v3.harness.contracts import (
    CancellationToken,
    CapabilityGrant,
    ExecutionRequest,
    WorkspaceEdit,
    sha256_text,
)
from mgar_v3.harness.recovery_io import RecoveryIO, workspace_lock
from mgar_v3.harness.service import (
    CodingTaskRequest,
    DeterministicCodingModel,
    HarnessCodingService,
    WorkspaceBoundBroker,
)
from mgar_v3.harness.workspace import Registration, WorkspaceRegistry


class Probe:
    def __init__(self, config_file: Path) -> None:
        self.config = read_json(config_file)
        self.worker_directory = config_file.parent
        self.directory = Path(self.config["directory"])
        self.directory.mkdir(parents=True, exist_ok=True)
        self.settings = fixture_settings(self.config["database_url"], self.directory)
        self.engine = Engine(self.settings, ArtifactStore(self.settings.artifact_root))
        self.task_id = str(self.config.get("task_id", ""))

    def fault(self, point: str, payload: dict[str, Any] | None = None) -> None:
        if point != self.config.get("fault"):
            return
        if self.task_id:
            task = self.engine.harness.get(self.task_id)
        else:
            task = self.engine.harness.tasks()[0]
        marker = {
            "point": point, "task_id": task["task_id"], "run_id": str(task["run_id"]),
            "epoch": task["execution_epoch"], "tree_hash": task["tree_hash"],
            "checkpoint_hash": task["checkpoint_hash"], "payload": payload or {},
        }
        private_json(self.worker_directory / "fault.json", marker)
        # The test either sends a genuine SIGKILL, or restarts its own PG and
        # releases this worker to prove its old Engine connection fails closed.
        while not (self.worker_directory / "release").is_file():
            time.sleep(0.02)

    def model(self, request: CodingTaskRequest) -> Any:
        probe = self
        inner = DeterministicCodingModel(
            task_kind=request.task_kind, target_path=request.target_path,
            old_text=request.old_text, new_text=request.new_text, test_argv=(),
        )

        class RecordedModel:
            def complete(self, model_request: Any, cancellation: CancellationToken) -> Any:
                path = probe.directory / "provider_calls.jsonl"
                descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
                with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                    stream.write(json.dumps({"operation_id": model_request.operation_id}) + "\n")
                    stream.flush()
                    os.fsync(stream.fileno())
                return inner.complete(model_request, cancellation)

        return RecordedModel()

    def service(self) -> dict[str, Any]:
        service = HarnessCodingService(
            self.settings.harness_root, actor="owner", image_lock=self.settings.harness_image_lock,
            engine=self.engine, model_factory=self.model, fault_hook=self.fault,
        )
        if self.config["mode"] == "recover":
            self.engine.recover()
            return service.recover_task(self.task_id)
        source = self.directory / "source"
        source.mkdir()
        (source / "source.py").write_text("value = 1\n", encoding="utf-8")
        project = service.register_project(str(source))
        return service.create_task(CodingTaskRequest(
            project_id=project["project_id"], objective="Inspect source.py", task_kind="inspect",
            target_path="source.py",
        ))

    def cancel(self) -> dict[str, Any]:
        self.engine.recover()
        root = (
            self.settings.harness_root if self.config.get("scenario") == "model"
            else self.directory
        )
        service = HarnessCodingService(
            root, actor="owner", image_lock=self.settings.harness_image_lock, engine=self.engine,
        )
        result = service.cancel_task(self.task_id, "H11 isolated cancellation probe")
        row = self.engine.harness.get(self.task_id)
        operations = self.engine.harness.operations(self.task_id)
        return {
            **result, "run_id": str(row["run_id"]), "epoch": row["epoch"],
            "operations": [{"id": operation["operation_id"], "state": operation["state"],
                            "result_hash": operation["result_hash"]}
                           for operation in operations],
            "receipts": [operation["receipt"] for operation in operations
                         if operation["kind"] == "command" and operation["state"] == "known"],
        }

    def io(self) -> dict[str, Any]:
        store = self.engine.harness
        registry = WorkspaceRegistry(self.directory / "registry")
        if self.config["mode"] == "start":
            source = self.directory / "source"
            source.mkdir()
            (source / "a.py").write_text("value = 1\n", encoding="utf-8")
            (source / "b.py").write_text("other = 1\n", encoding="utf-8")
            registration = registry.register_local(source, actor="owner")
            self.task_id = "h08-" + uuid4().hex[:12]
            workspace = registry.create_task_workspace(registration, self.task_id)
            grant = CapabilityGrant(
                actor="owner", workspace_id=registration.workspace_id,
                repository=registration.repository, read_paths=("",), write_paths=("",),
                execute=True, allowed_commands=("python3",), max_seconds=120,
            )
            store.create(
                self.task_id, registration,
                {"project_id": registration.workspace_id, "objective": "controlled H11 IO",
                 "mode": "deterministic", "workspace_root": str(workspace.root)},
                grant.model_dump(mode="json"),
                {"max_model_calls": 12, "max_tool_calls": 48, "max_cost_micros": 100,
                 "max_seconds": 600},
            )
            row = store.begin(self.task_id)
            store.record_tree(self.task_id, row["epoch"], workspace.snapshot().tree_hash)
        else:
            self.engine.recover()
            row = store.get(self.task_id)
            registration = Registration.model_validate(row["registration"])
            workspace = registry.open_task_workspace(registration, self.task_id,
                                                      allow_unreconciled=True)
            grant = CapabilityGrant.model_validate(row["grant_scope"])
        make_traversable(workspace.root)
        lease = issue_lease(
            mode="modify_test", actor="owner", workspace_id=registration.workspace_id,
            repository=registration.repository, execution_epoch=max(1, row["epoch"]),
            read_paths=("",), write_paths=("",), allowed_commands=("python3",),
            timeout_seconds=120, approved_by="owner", approval_ref="h11-owned-fixture",
        )
        broker = WorkspaceBoundBroker(workspace, lease, lock_path=self.settings.harness_image_lock)
        io = RecoveryIO(
            store, self.task_id, row["epoch"], workspace,
            artifacts=self.engine.artifacts, run_id=str(row["run_id"]), fault=self.fault,
        )
        receipts: list[dict[str, Any]] = []
        if self.config["mode"] == "recover":
            with workspace_lock(workspace.root):
                for op in store.operations(self.task_id):
                    if op["kind"] == "edit" and op["state"] in {"unknown", "dispatched"}:
                        io.reconcile_edit_locked(op["operation_id"])
                    elif op["kind"] == "command":
                        receipts.append(io.reconcile_command_locked(broker, op["operation_id"]))
                row = store.claim(self.task_id)
                store.record_tree(self.task_id, row["epoch"], workspace.snapshot().tree_hash)
        elif self.config["scenario"] == "edit":
            edits = (
                WorkspaceEdit(path="a.py", expected_sha256=sha256_text("value = 1\n"),
                              content="value = 2\n"),
                WorkspaceEdit(path="b.py", expected_sha256=sha256_text("other = 1\n"),
                              content="other = 2\n"),
            )
            snapshot = io.edit(edits, expected_tree=workspace.snapshot().tree_hash,
                               operation_id="edit-1")
            store.record_tree(self.task_id, row["epoch"], snapshot.tree_hash)
        else:
            body = "print('h11-command-output',flush=True)"
            if self.config.get("fault") in {"command.after_create", "command.after_start"}:
                body += ";import time;time.sleep(90)"
            io.execute_command(broker, ExecutionRequest(
                operation_id="command-1", purpose="command", argv=("python3", "-c", body),
                cwd=".", execution_epoch=row["epoch"], timeout_seconds=100,
            ), grant, CancellationToken())
        current = store.get(self.task_id)
        return {
            "task_id": self.task_id, "run_id": str(current["run_id"]),
            "epoch": current["epoch"], "tree_hash": workspace.snapshot().tree_hash,
            "manifest_hash": workspace.manifest.manifest_hash,
            "files": {"a.py": workspace.read("a.py"), "b.py": workspace.read("b.py")},
            "operations": [{"id": op["operation_id"], "state": op["state"],
                            "result_hash": op["result_hash"]}
                           for op in store.operations(self.task_id)],
            "receipts": receipts,
        }

    def run(self) -> None:
        try:
            if self.config["mode"] == "cancel":
                result = self.cancel()
            else:
                result = (
                    self.service() if self.config.get("scenario", "model") == "model" else self.io()
                )
        except DomainError as exc:
            result = {"error": exc.code, "message": str(exc)}
        except psycopg.Error:
            result = {"error": "POSTGRES_CONNECTION_LOST"}
        finally:
            self.engine.close()
        private_json(self.worker_directory / "result.json", result)


if __name__ == "__main__":
    Probe(Path(sys.argv[1])).run()
