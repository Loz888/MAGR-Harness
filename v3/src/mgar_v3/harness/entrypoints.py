"""Independent entry inventory/load checks, backed by existing Engine IO."""

from __future__ import annotations

import json
import re
from pathlib import PurePosixPath
from typing import TYPE_CHECKING, Any

from mgar_v3.domain import DomainError, digest
from mgar_v3.harness.contracts import CancellationToken, CapabilityGrant, TaskBinding
from mgar_v3.harness.lifecycle import LifecycleRunner, sealed
from mgar_v3.harness.lifecycle_types import (
    EntryPointBatch,
    EntryPointReceipt,
    EntryPointSpec,
    LifecyclePolicy,
)

if TYPE_CHECKING:
    from mgar_v3.harness.commands import CommandSession
    from mgar_v3.harness.recovery_io import RecoveryIO
    from mgar_v3.harness.workspace import TaskWorkspace


class EntryPointChecker:
    def __init__(
        self,
        workspace: TaskWorkspace,
        session: CommandSession,
        task: TaskBinding,
        budgets: Any,
        recovery_io: RecoveryIO | None,
        policy: LifecyclePolicy,
        permission_argv: tuple[str, ...],
        *,
        cwd: str = ".",
        service_argv: tuple[str, ...] = (),
    ) -> None:
        self.workspace, self.session, self.task = workspace, session, task
        self.budgets, self.io, self.policy = budgets, recovery_io, policy
        self.permission_argv, self.cwd, self.service_argv = permission_argv, cwd, service_argv

    def inventory(self, baseline: dict[str, str]) -> tuple[EntryPointSpec, ...]:
        current = self.workspace.snapshot()
        selected = set()
        for change in self.workspace.status():
            path = change["path"]
            if change["status"] != "deleted":
                selected.add(path)
        # Frozen exact-file scopes are obligations even if the new file is missing.
        scope = self.task.acceptance_scope
        if scope:
            for path in scope.write_paths or ():
                if path not in baseline:
                    selected.add(path)
        for arg in self.service_argv[1:]:
            if arg.endswith((".py", ".js", ".cjs", ".mjs")):
                selected.add(arg)
        if self.service_argv[:2] == ("npm", "run") and len(self.service_argv) == 3:
            package = json.loads(current.files.get("package.json", "{}"))
            command = package.get("scripts", {}).get(self.service_argv[2], "")
            matched = re.fullmatch(r"node\s+([A-Za-z0-9_./-]+\.(?:js|cjs|mjs))", command)
            if matched:
                selected.add(matched[1])
        old = json.loads(baseline.get("package.json", "{}"))
        package = json.loads(current.files.get("package.json", "{}"))
        for key in ("main", "module", "exports"):
            if old.get(key) == package.get(key):
                continue
            pending = [package.get(key)]
            while pending:
                value = pending.pop()
                if isinstance(value, dict):
                    pending.extend(value.values())
                elif isinstance(value, list):
                    pending.extend(value)
                elif isinstance(value, str):
                    selected.add(value.removeprefix("./"))
        no_bind = bool(
            re.search(
                r"without binding|only when run directly",
                " ".join((self.task.objective, *self.task.public_requirements)),
                re.I,
            )
        )
        targets = []
        for path in sorted(selected):
            parts = PurePosixPath(path).parts
            if set(parts) & {"test", "tests", "verification", "web", "public", "assets"}:
                continue
            if not path.endswith((".py", ".js", ".cjs", ".mjs")):
                continue
            roots = ("src", "") if path.startswith("src/") and path.endswith(".py") else ("",)
            targets.append(
                EntryPointSpec(
                    path=path,
                    runtime="python" if path.endswith(".py") else "node",
                    cwd=self.cwd,
                    import_roots=roots,
                    no_listen_on_import=no_bind,
                )
            )
        if len(targets) > 32:
            raise DomainError("HARNESS_ENTRYPOINT_LIMIT", "More than 32 entry checks required", 429)
        return tuple(targets)

    def check(
        self,
        tree: str,
        grant: CapabilityGrant,
        baseline: dict[str, str],
        acceptance_hash: str,
        token: CancellationToken,
    ) -> EntryPointBatch:
        token.ensure_active()
        snapshot = self.workspace.snapshot()
        if snapshot.tree_hash != tree:
            raise DomainError("HARNESS_ENTRYPOINT_TREE", "Entry inventory tree differs", 409)
        try:
            targets = self.inventory(baseline)
        except (ValueError, AttributeError, TypeError):
            return EntryPointBatch(
                operation_id=f"{self.task.task_id[:36]}:entry-inventory:{tree[:12]}",
                tree_hash=tree,
                status="FAIL",
                acceptance_hash=acceptance_hash,
                inventory_hash=digest({"tree": tree, "invalid_manifest": True}),
                detail="Entry paths or package metadata invalid; no module probe was executed",
            )
        receipts = []
        for index, spec in enumerate(targets):
            operation = (
                f"{self.task.task_id[:36]}:entry:{tree[:12]}:{acceptance_hash[:8]}:{index + 1}"
            )
            if (
                not grant.can_read(spec.path)
                or not self.permission_argv
                or not grant.can_execute(self.permission_argv)
            ):
                raise DomainError(
                    "HARNESS_ENTRYPOINT_DENIED", "Entry check needs original scope", 403
                )
            self.session.broker.authority.check_scope(
                actor=self.task.actor,
                workspace_id=self.task.workspace_id,
                repository=self.task.repository,
                execution_epoch=self.task.execution_epoch,
                paths=(spec.path,),
                write=False,
            )
            reservation = self.budgets.reserve(self.task.task_id, operation, "tool")

            def run(spec: EntryPointSpec = spec, operation: str = operation) -> EntryPointReceipt:
                result = LifecycleRunner(self.session, self.task, self.policy).entry(
                    spec,
                    self.permission_argv,
                    grant,
                    operation,
                    tree,
                    token,
                )
                values = result.model_dump(exclude={"receipt_id"})
                values["acceptance_hash"] = acceptance_hash
                return sealed(EntryPointReceipt, values)

            try:
                receipt = (
                    self.io.invoke(
                        operation,
                        "application",
                        {
                            "entry": spec.model_dump(mode="json"),
                            "tree": tree,
                            "acceptance_hash": acceptance_hash,
                            "policy": self.policy.model_dump(mode="json"),
                        },
                        run,
                        encode=lambda value: value.model_dump(mode="json"),
                        decode=EntryPointReceipt.model_validate,
                    )
                    if self.io is not None
                    else run()
                )
            except BaseException:
                self.budgets.mark_unknown(reservation.reservation_id, "Entry operation interrupted")
                raise
            self.budgets.settle(reservation.reservation_id, actual_micros=0, actual_units=1)
            receipts.append(receipt)
        valid = (
            all(
                r.status == "PASS"
                and r.tree_hash == tree
                and r.acceptance_hash == acceptance_hash
                and r.task_id == self.task.task_id
                and r.run_id == str(self.task.run_id)
                and 1 <= r.execution_epoch <= self.task.execution_epoch
                and r.spec == spec
                and r.loader_hash == self.policy.control_assets_hash
                and r.cleanup is not None
                and r.cleanup.processes_exited is True
                and r.cleanup.port_released is True
                and r.cleanup.container_removed
                and not r.cleanup.orphan_pids
                for r, spec in zip(receipts, targets, strict=True)
            )
            and self.workspace.snapshot().tree_hash == tree
        )
        if not valid:
            detail = (
                "; ".join(
                    f"{r.spec.path}: {r.detail[:500]}" for r in receipts if r.status != "PASS"
                )[:3000]
                or "Final tree changed during entry verification"
            )
        else:
            detail = (
                "All inventoried entries independently loaded"
                if targets
                else "Final-tree inventory found no new or changed executable entry"
            )
        return EntryPointBatch(
            operation_id=f"{self.task.task_id[:36]}:entry-inventory:{tree[:12]}",
            tree_hash=tree,
            targets=targets,
            receipts=tuple(receipts),
            inventory_hash=digest(
                {
                    "tree": tree,
                    "baseline_paths": sorted(baseline),
                    "targets": [s.model_dump(mode="json") for s in targets],
                }
            ),
            acceptance_hash=acceptance_hash,
            status="PASS" if valid else "FAIL",
            detail=detail,
        )
