"""Immutable, grant-checked delivery of project guidance to a product Run."""

from __future__ import annotations

import json
from collections.abc import Sequence
from pathlib import PurePosixPath
from typing import Any

from pydantic import Field, ValidationError

from mgar_v3.domain import DomainError, Model
from mgar_v3.harness.broker import CapabilityAuthority
from mgar_v3.harness.contracts import (
    CapabilityGrant,
    HarnessToolResult,
    ModelMessage,
    ModelRequest,
    TaskBinding,
    ToolContext,
    canonical_hash,
    sha256_text,
)
from mgar_v3.harness.project_context import (
    _PREFIX,
    CONTEXT_VERSION,
    _safe_path,
    authorize_project_sources,
    load_edit_guidance,
)
from mgar_v3.harness.recovery_io import RecoveryIO
from mgar_v3.harness.workspace import TaskWorkspace

GUIDANCE_MESSAGE = "harness_project_guidance"
GUIDANCE_REQUIRED = "PROJECT_GUIDANCE_REQUIRED"
GUIDANCE_OPERATION = "project_guidance"
_MAX_RUN_GUIDANCE_BYTES = 65536


class FrozenGuidance(Model):
    context_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    text: str = Field(max_length=32768)

    def payload(self) -> dict[str, Any]:
        try:
            if (
                not self.text.startswith(_PREFIX)
                or canonical_hash({"version": CONTEXT_VERSION, "text": self.text})
                != self.context_hash
                or len(self.text.encode()) > 32768
            ):
                raise ValueError("frozen guidance hash")
            data = json.loads(self.text[len(_PREFIX) :])
            if data["version"] != CONTEXT_VERSION:
                raise ValueError("frozen guidance version")
            for key in ("rule_index", "skills", "sources"):
                for item in data[key]:
                    if _safe_path(item["path"]) is None:
                        raise ValueError("frozen guidance path")
            for source in data["sources"]:
                text = source["content"]
                if (
                    len(text.encode()) != source["included_bytes"]
                    or source["total_bytes"] < source["included_bytes"]
                    or (not source["truncated"] and sha256_text(text) != source["sha256"])
                ):
                    raise ValueError("frozen guidance source")
            return dict(data)
        except (KeyError, TypeError, ValueError, AttributeError) as exc:
            raise DomainError(
                "PROJECT_GUIDANCE_INVALID", "Frozen project guidance differs", 409
            ) from exc


class GuidanceDeferral(Model):
    capsule: FrozenGuidance | None = None
    pending_paths: tuple[str, ...] = Field(min_length=1)

    def result(self, call_id: str) -> HarnessToolResult:
        return HarnessToolResult(
            call_id=call_id,
            status="error",
            code=GUIDANCE_REQUIRED,
            content=(
                "No edits were applied. The next model request carries the missing scoped "
                "project rules. Reconsider the edits and issue a new call."
            ),
            data={"guidance_paths": list(self.pending_paths), "executed": False},
        )


class GuidanceDelivery:
    def __init__(
        self,
        workspace: TaskWorkspace,
        io: RecoveryIO,
        authority: CapabilityAuthority,
        task: TaskBinding,
        grant: CapabilityGrant,
        initial: dict[str, Any],
    ) -> None:
        self.workspace, self.io, self.authority = workspace, io, authority
        self.task, self.grant = task, grant
        self.capsules: dict[str, FrozenGuidance] = {}
        self.visible: set[str] = set()
        self._rules: dict[str, tuple[str, bool]] = {}
        with io.locked():
            authority.peek()
            try:
                if "text" in initial:
                    self._remember(
                        FrozenGuidance(context_hash=initial["context_hash"], text=initial["text"])
                    )
                for operation in io.store.operations(task.task_id):
                    if operation["kind"] == GUIDANCE_OPERATION and operation["state"] == "known":
                        decision = GuidanceDeferral.model_validate(io._verified_receipt(operation))
                        self._validate_deferral(decision)
                        if decision.capsule is not None:
                            self._remember(decision.capsule)
            except ValidationError as exc:
                raise DomainError(
                    "PROJECT_GUIDANCE_INVALID", "Saved project guidance is invalid", 409
                ) from exc

    def authorize(self, *paths: str) -> None:
        authorize_project_sources(
            self.workspace,
            self.grant,
            self.authority,
            task_id=self.task.task_id,
            actor=self.task.actor,
            epoch=self.task.execution_epoch,
            paths=paths,
        )

    def _remember(self, capsule: FrozenGuidance) -> None:
        self._check_capacity(capsule)
        data = capsule.payload()
        paths = {item["path"] for key in ("rule_index", "skills", "sources") for item in data[key]}
        self.authorize(*sorted(paths))
        for item in data["sources"]:
            if item["kind"] == "instruction":
                existing = self._rules.get(item["path"])
                if existing is not None and existing != (capsule.context_hash, item["truncated"]):
                    raise DomainError("PROJECT_GUIDANCE_CHANGED", "A frozen rule was replaced", 409)
                self._rules[item["path"]] = (capsule.context_hash, item["truncated"])
        self.capsules[capsule.context_hash] = capsule

    def _check_capacity(self, capsule: FrozenGuidance) -> None:
        if capsule.context_hash not in self.capsules and (
            sum(len(item.text.encode()) for item in self.capsules.values())
            + len(capsule.text.encode()) > _MAX_RUN_GUIDANCE_BYTES
        ):
            raise DomainError("PROJECT_GUIDANCE_LIMIT", "Run guidance exceeds its limit", 413)

    def messages(self) -> tuple[ModelMessage, ...]:
        return tuple(
            ModelMessage(role="user", name=GUIDANCE_MESSAGE, content=item.text)
            for item in self.capsules.values()
        )

    def verify_request(self, request: ModelRequest) -> None:
        """Observe the exact outgoing request, including an unchanged saved request."""
        with self.io.locked():
            self.authority.peek()
            if (
                request.task.run_id != self.task.run_id
                or request.task.execution_epoch != self.task.execution_epoch
                or request.task.workspace_id != self.task.workspace_id
            ):
                raise DomainError("PROJECT_GUIDANCE_SCOPE", "Model request scope differs", 403)
            visible: set[str] = set()
            for message in request.messages:
                if message.name != GUIDANCE_MESSAGE:
                    continue
                key = canonical_hash({"version": CONTEXT_VERSION, "text": message.content})
                item = self.capsules.get(key)
                if (
                    item is None
                    or item.text != message.content
                    or message.role != "user"
                    or message.tool_calls
                    or message.tool_call_id
                    or key in visible
                ):
                    raise DomainError("PROJECT_GUIDANCE_INVALID", "Model guidance differs", 409)
                self._remember(item)  # Recheck paths against the present grant and epoch.
                visible.add(key)
            self.visible = visible

    def _validate_deferral(self, decision: GuidanceDeferral) -> None:
        self.authorize(*decision.pending_paths)
        if decision.capsule is not None:
            data = decision.capsule.payload()
            self.authorize(*(item["path"] for item in data["sources"]))

    def guard_edit(self, context: ToolContext, targets: Sequence[str]) -> GuidanceDeferral | None:
        """Run under the workspace lock, before any inner mutation is journalled."""
        candidates = {
            f"{parent}/AGENTS.md" if str(parent) != "." else "AGENTS.md"
            for path in targets
            for parent in PurePosixPath(path).parents
        }
        # Do not reopen sources already pinned by this Run.
        newly_seen = load_edit_guidance(
            self.workspace,
            self.authorize,
            targets=targets,
            known_paths=set(self._rules),
        )
        if newly_seen.omitted_sources:
            raise DomainError("PROJECT_GUIDANCE_LIMIT", "Applicable rules exceed their limit", 413)
        if any(truncated for path, (_, truncated) in self._rules.items() if path in candidates):
            raise DomainError("PROJECT_GUIDANCE_LIMIT", "An applicable rule was truncated", 413)
        if any(source.truncated for source in newly_seen.sources):
            raise DomainError("PROJECT_GUIDANCE_LIMIT", "An applicable rule exceeds its limit", 413)
        pending = {path for path, (key, _) in self._rules.items() if key not in self.visible}
        pending.update(source.path for source in newly_seen.sources)
        if not pending:
            return None
        capsule = (
            FrozenGuidance(context_hash=newly_seen.context_hash, text=newly_seen.text)
            if newly_seen.sources
            else None
        )
        if capsule is not None:
            self._check_capacity(capsule)
        proposed = GuidanceDeferral(capsule=capsule, pending_paths=tuple(sorted(pending)))
        recovered = self.io.invoke(
            context.operation_id + ":guidance",
            GUIDANCE_OPERATION,
            {"targets": list(targets), "model_operation": context.operation_id},
            lambda: proposed,
            encode=lambda decision: decision.model_dump(mode="json"),
            decode=GuidanceDeferral.model_validate,
            validate_known=self._validate_deferral,
            read_only=True,
        )
        if recovered.capsule is not None:
            self._remember(recovered.capsule)
        return recovered
