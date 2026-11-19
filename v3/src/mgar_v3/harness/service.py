"""H08 web coding service.

This is the only place that joins the H05 isolated task workspace, the H07
coding loop and the H06 container runtime for the versioned web API.  It does
not add an execution path of its own: every command still goes through
``IsolatedExecutionBroker`` (lease, epoch, command allowlist and budgets) and
``CommandSession``.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import threading
import time
from collections.abc import Callable, Sequence
from contextlib import nullcontext
from dataclasses import asdict, dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, cast
from uuid import UUID, uuid4

from pydantic import ValidationError

from mgar_v3.domain import DomainError, digest
from mgar_v3.harness.acceptance import AcceptanceScope
from mgar_v3.harness.adapters import (
    InMemoryBudgetLedger,
    LocalVerifier,
)
from mgar_v3.harness.broker import (
    CapabilityAuthority,
    CapabilityLease,
    IsolatedExecutionBroker,
    issue_lease,
)
from mgar_v3.harness.coding_loop import (
    CodingLoop,
    CodingResult,
    SafePoint,
    ServicePlan,
    ServiceReceipt,
)
from mgar_v3.harness.commands import CommandSession, ReadinessProbe
from mgar_v3.harness.context_tools import (
    ARTIFACT_TOOLS,
    READ_LINES_SPEC,
    READ_SYMBOL_SPEC,
    ScopedContextTools,
    artifact_trailer,
)
from mgar_v3.harness.contracts import (
    BudgetLimits,
    CancellationToken,
    CapabilityGrant,
    EvaluationProfile,
    EventDraft,
    ExecutionReceipt,
    ExecutionRequest,
    HarnessToolCall,
    HarnessToolResult,
    ModelAdapter,
    ModelRequest,
    ModelResponse,
    ModelUsage,
    TaskBinding,
    ToolContext,
    ToolSpec,
    VerificationRequirement,
    WorkspaceChange,
    WorkspaceDiff,
    WorkspaceEdit,
    WorkspaceSnapshot,
    canonical_hash,
    is_workspace_artifact,
    sha256_text,
)
from mgar_v3.harness.delegation import CONSULT, RepositoryConsultation
from mgar_v3.harness.entrypoints import EntryPointChecker
from mgar_v3.harness.events import STREAM_VERSION, PersistentEventSink
from mgar_v3.harness.inbox import TaskInbox
from mgar_v3.harness.incremental import output_bytes, protocol
from mgar_v3.harness.ledger import PersistentBudgetLedger
from mgar_v3.harness.lifecycle import LifecycleRunner, sealed
from mgar_v3.harness.lifecycle_plan import public_routes
from mgar_v3.harness.lifecycle_types import HTTPExpectation, LifecyclePolicy, ProcessPortReceipt
from mgar_v3.harness.long_context import LongContextManager
from mgar_v3.harness.model import (
    DEEPSEEK_CHAT,
    DeepSeekChatAdapter,
    ModelCapabilities,
    _price_usage,
)
from mgar_v3.harness.output_protocol import OutputPolicy
from mgar_v3.harness.persistence import (
    DurableContinuation,
    DurableModel,
    EngineEvents,
    EngineInbox,
    _checkpoint_payload,
)
from mgar_v3.harness.project_commands import (
    detect_project,
    infer_test_runner,
    parse_tests,
    test_project_kind,
)
from mgar_v3.harness.project_guidance import (
    GUIDANCE_OPERATION,
    GUIDANCE_REQUIRED,
    GuidanceDeferral,
    GuidanceDelivery,
)
from mgar_v3.harness.public_feedback import observe_public_command, render_public_feedback
from mgar_v3.harness.public_probe import normalize_probe_receipt, read_asset
from mgar_v3.harness.public_probe_types import PublicProbePlan
from mgar_v3.harness.recovery_io import RecoveryIO, workspace_lock
from mgar_v3.harness.regression_types import RegressionCommand, RegressionPlan
from mgar_v3.harness.result_artifacts import dump_result, load_result
from mgar_v3.harness.runner_store import RunnerStore
from mgar_v3.harness.test_results import parse_independent_test
from mgar_v3.harness.tool_errors import ToolErrorPolicy, effective_roots
from mgar_v3.harness.workspace import (
    Registration,
    TaskWorkspace,
    WorkspaceRegistry,
    WorkspaceToolRegistry,
    _open_read_target,
    _safe_target,
    _scan,
    _tree_hash,
)

if TYPE_CHECKING:
    from mgar_v3.general_agent.coding_executor_contract import CodingDispatch

DETERMINISTIC_MODEL_ID = "local-deterministic-h08"
DETERMINISTIC_PRICE_VERSION = "local-deterministic-v1"
READ_FILE_MARKER = "\n\n<<mgar-read-file "
TASK_ID_PATTERN = re.compile(r"h(?:08|15)-[0-9a-f]{12}")


def _strip_read_trailer(text: str) -> str:
    marker = text.find(READ_FILE_MARKER)
    return text[:marker] if marker >= 0 else text


def _file_digest_trailer(content: str, tree: str) -> str:
    """Tell a real model the pre-image digest it cannot compute itself."""
    return (
        f"{READ_FILE_MARKER}sha256={sha256_text(content)} tree={tree}>>\n"
        "Use sha256 above as expected_sha256 and tree as expected_tree for apply_edits.\n"
    )


TaskKind = Literal["inspect", "bugfix", "feature", "refactor", "run_app"]
_WRITABLE: frozenset[str] = frozenset({"bugfix", "feature", "refactor", "run_app"})
_TERMINAL: frozenset[str] = frozenset(
    {
        "completed",
        "blocked",
        "denied",
        "failed",
        "cancelled",
        "incomplete",
        "budget_exhausted",
        "unknown",
    }
)

DEFAULT_MAX_SECONDS = 600
DEFAULT_OUTPUT_TOKENS = 2048
DEFAULT_RESERVATION_MICROS = 2000
LIVE_MAX_COST_MICROS = 250_000


class WorkspaceBoundBroker(IsolatedExecutionBroker):
    """Bind broker receipts to the H05 workspace tree.

    ``IsolatedExecutionBroker`` hashes the mounted directory on its own (it
    includes files the H05 workspace deliberately excludes and decodes binary
    content lossily).  ``LocalVerifier`` compares receipt trees with
    ``TaskWorkspace.snapshot().tree_hash``, so this adapter overrides only the
    tree digest.  Authorization, container creation, lease/epoch checks, output
    limits and budgets stay in the parent implementation.
    """

    def __init__(
        self,
        workspace: TaskWorkspace,
        lease: CapabilityLease | CapabilityAuthority,
        **kwargs: Any,
    ) -> None:
        super().__init__(workspace.root, lease, **kwargs)
        self.workspace = workspace
        self.recovery_io: RecoveryIO | None = None
        self.context_tools: ScopedContextTools | None = None
        self._task_binding: TaskBinding
        self._recovered_receipts: set[str] = set()

    def is_recovered_receipt(self, receipt: ExecutionReceipt) -> bool:
        return receipt.receipt_id in self._recovered_receipts

    def workspace_tree_hash(self) -> str:
        return self.workspace.snapshot().tree_hash

    def execute(
        self,
        request: ExecutionRequest,
        grant: CapabilityGrant,
        cancellation: CancellationToken,
    ) -> ExecutionReceipt:
        if self.recovery_io is not None:
            io = self.recovery_io
            old = io.store.operation(io.task_id, request.operation_id)
            value = request.model_dump(mode="json", exclude={"execution_epoch"})

            def execute() -> ExecutionReceipt:
                receipt = self._execute_bound(request, grant, cancellation)
                if self.context_tools is not None:
                    self.context_tools.archive_command_log(
                        receipt.stdout + receipt.stderr,
                        ToolContext(
                            task=self._task_binding,
                            grant=grant,
                            operation_id=request.operation_id,
                            workspace_tree=receipt.tree_hash,
                        ),
                        provenance=receipt.provenance,
                        complete=False,
                    )
                return receipt

            receipt = io.invoke(
                request.operation_id,
                "baseline",
                value,
                execute,
                encode=lambda item: item.model_dump(mode="json"),
                decode=ExecutionReceipt.model_validate,
            )
            if old is not None and old["state"] == "known":
                self._recovered_receipts.add(receipt.receipt_id)
            return receipt
        return self._execute_bound(request, grant, cancellation)

    def _execute_bound(
        self,
        request: ExecutionRequest,
        grant: CapabilityGrant,
        cancellation: CancellationToken,
    ) -> ExecutionReceipt:
        receipt = super().execute(request, grant, cancellation)
        complete = (len((receipt.stdout + receipt.stderr).encode())
                    < self.authority.peek().max_output_bytes)
        if self.recovery_io is not None:
            operation = self.recovery_io.store.operation(
                self.recovery_io.task_id, self.recovery_io.command_operation_id(request),
            )
            if operation and operation["state"] == "known":
                original = self.recovery_io._verified_receipt(operation)
                complete = not (original.get("dropped_bytes", 0)
                                or original.get("handle", {}).get("log_truncated", True))
        receipt = _parsed_model_test_receipt(receipt, complete=complete)
        bound = self.workspace.snapshot().tree_hash
        payload = receipt.model_dump(mode="json", exclude={"receipt_id", "tree_hash"})
        payload["tree_hash"] = bound
        return receipt.model_copy(
            update={"tree_hash": bound, "receipt_id": canonical_hash(payload)}
        )


class CodingWorkspace:
    """Adapt ``TaskWorkspace`` to the coding loop's workspace protocol.

    ``TaskWorkspace.diff(path)`` returns a textual patch for the baseline while
    the loop needs a structured ``WorkspaceDiff``.  The baseline of a fresh
    task workspace *is* the task checkpoint, so the structured diff is derived
    from the same status scan and the current snapshot.
    """

    def __init__(self, inner: TaskWorkspace) -> None:
        self.inner = inner

    def snapshot(self) -> WorkspaceSnapshot:
        return self.inner.snapshot()

    def read(self, path: str) -> str:
        return self.inner.read(path)

    def list_dir(self, path: str) -> tuple[str, ...]:
        return self.inner.list_dir(path)

    def apply(self, edits: Sequence[WorkspaceEdit], *, expected_tree: str) -> WorkspaceSnapshot:
        return self.inner.apply(edits, expected_tree=expected_tree)

    def diff(self, tree_before: str) -> WorkspaceDiff:
        baseline = self.inner._baseline  # frozen path -> sha256 map of the checkpoint
        changes: list[WorkspaceChange] = []
        for item in self.inner.status():
            path = item["path"]
            before = baseline.get(path)
            if item["status"] == "deleted":
                after_sha256 = sha256_text("")
            else:
                try:
                    after_sha256 = sha256_text(self.inner.read(path))
                except UnicodeDecodeError:
                    continue
            changes.append(
                WorkspaceChange(
                    path=path,
                    change="added" if before is None else "modified",
                    # ``before`` is already the recorded digest; hashing it again
                    # would report a hash of a hash and break independent checks.
                    before_sha256=before,
                    after_sha256=after_sha256,
                )
            )
        current, _ = _scan(self.inner.root, self.inner.limits)
        return WorkspaceDiff(
            workspace_id=self.inner.registration.workspace_id,
            tree_before=tree_before,
            tree_after=_tree_hash(current),
            changes=tuple(changes),
            patch=self.inner.diff(),
        )


class MeteredModel:
    """Count model calls and generated bytes without touching the request."""

    def __init__(self, inner: Any, *, live: bool) -> None:
        self.inner = inner
        self.live = live
        self.calls = 0
        self.input_bytes = 0
        self.output_bytes = 0
        self.cost_micros = 0

    def complete(self, request: ModelRequest, cancellation: CancellationToken) -> ModelResponse:
        response = cast(ModelResponse, self.inner.complete(request, cancellation))
        self.calls += 1
        self.input_bytes += sum(len(message.content.encode()) for message in request.messages)
        self.output_bytes += len(response.text.encode())
        for call in response.tool_calls:
            arguments = json.dumps(call.arguments, sort_keys=True, default=str)
            self.output_bytes += len(call.name.encode()) + len(arguments.encode())
        self.cost_micros += response.usage.cost_micros
        return response

    def set_output_observer(self, observer: Any) -> None:
        setter = getattr(self.inner, "set_output_observer", None)
        if callable(setter):
            setter(observer)


class DeterministicCodingModel:
    """Local, network-free model adapter used by the phase-one web flow.

    It is a real tool-using state machine (read, incrementally patch, test) but
    it is *not* a language model.  The task request must therefore carry the
    target path and the before/after snippets explicitly.
    """

    def __init__(
        self,
        *,
        task_kind: str,
        target_path: str,
        old_text: str,
        new_text: str,
        test_argv: tuple[str, ...],
        occurrence: int | None = None,
    ) -> None:
        self.task_kind = task_kind
        self.target_path = target_path
        self.old_text = old_text
        self.new_text = new_text
        self.test_argv = test_argv
        self.occurrence = occurrence

    def complete(self, request: ModelRequest, cancellation: CancellationToken) -> ModelResponse:
        cancellation.ensure_active()
        tool_messages = [message for message in request.messages if message.role == "tool"]
        if not tool_messages:
            return self._tool(request, "read_file", {"path": self.target_path})
        if self.task_kind == "inspect":
            return self._text(
                f"Inspected {self.target_path}. The file contains "
                f"{len(tool_messages[-1].content)} characters of evidence."
            )
        if not any(message.name == "apply_edits" for message in tool_messages):
            read = next(
                (message for message in reversed(tool_messages) if message.name == "read_file"),
                None,
            )
            if read is None:
                raise DomainError(
                    "HARNESS_DETERMINISTIC_STATE",
                    "The deterministic adapter needs a read_file result first",
                    409,
                )
            if self.old_text not in _strip_read_trailer(read.content):
                raise DomainError(
                    "HARNESS_DETERMINISTIC_MISS",
                    f"The requested pre-image was not found in {self.target_path}",
                    409,
                )
            replacement: dict[str, Any] = {
                "old_sha256": sha256_text(self.old_text),
                "old_text": self.old_text,
                "new_text": self.new_text,
            }
            if self.occurrence is not None:
                replacement["occurrence"] = self.occurrence
            return self._tool(
                request,
                "apply_edits",
                {
                    "edits": [
                        {
                            "path": self.target_path,
                            "expected_sha256": sha256_text(_strip_read_trailer(read.content)),
                            "replacements": [replacement],
                        }
                    ],
                    "expected_tree": request.workspace_tree,
                },
            )
        if not any(message.name == "run_tests" for message in tool_messages):
            return self._tool(request, "run_tests", {"argv": list(self.test_argv)})
        return self._text(
            f"Updated {self.target_path} with an incremental replacement and ran the tests."
        )

    def _tool(self, request: ModelRequest, name: str, arguments: dict[str, Any]) -> ModelResponse:
        return ModelResponse(
            tool_calls=(
                HarnessToolCall(
                    call_id=f"{request.operation_id}:{name}",
                    name=name,
                    arguments=arguments,
                ),
            ),
            finish_reason="tool_calls",
            usage=ModelUsage(),
            provider="local",
            model_id=DETERMINISTIC_MODEL_ID,
            price_version=DETERMINISTIC_PRICE_VERSION,
        )

    def _text(self, text: str) -> ModelResponse:
        return ModelResponse(
            text=text,
            usage=ModelUsage(),
            provider="local",
            model_id=DETERMINISTIC_MODEL_ID,
            price_version=DETERMINISTIC_PRICE_VERSION,
        )


RUN_TESTS_SPEC = ToolSpec(
    name="run_tests",
    description=(
        "Run the trusted test command for this task inside the restricted "
        "container. Use command_id='tests'; the trusted argv is fixed by the lease."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "command_id": {"type": "string", "enum": ["tests"]},
            "argv": {
                "type": "array",
                "items": {"type": "string", "minLength": 1},
                "minItems": 1,
            },
        },
        "oneOf": [{"required": ["command_id"]}, {"required": ["argv"]}],
        "additionalProperties": False,
    },
    capability="execute",
)


ASK_USER_SPEC = ToolSpec(
    name="ask_user",
    description=(
        "Ask the operator a concrete question when information or a capability "
        "is missing. The task keeps its progress and waits for the answer."
    ),
    input_schema={
        "type": "object",
        "properties": {"question": {"type": "string", "minLength": 1}},
        "required": ["question"],
        "additionalProperties": False,
    },
    capability="read",
)


class CodingToolRegistry:
    """H05 workspace tools plus a lease-bound ``run_tests`` through H06."""

    def __init__(
        self,
        workspace: TaskWorkspace,
        broker: WorkspaceBoundBroker,
        session: CommandSession,
        test_argv: tuple[str, ...],
        *,
        kind: str,
        build_argv: tuple[str, ...] = (),
        recovery_io: RecoveryIO | None = None,
        command_timeout_seconds: float = 120.0,
        feedback_plan: RegressionPlan | None = None,
    ) -> None:
        self.workspace = workspace
        self.broker = broker
        self.session = session
        self.test_argv = test_argv
        self.build_argv = build_argv
        self.kind = kind
        self.public_probe = broker.public_probe
        self.public_probe_root = broker.public_probe_root
        self.recovery_io = recovery_io
        self.command_timeout_seconds = command_timeout_seconds
        self.feedback_plan = feedback_plan
        self.guidance: GuidanceDelivery | None = None
        self.expert: RepositoryConsultation | None = None
        self.workspace_tools = WorkspaceToolRegistry(workspace)
        self.context_tools = ScopedContextTools(workspace, broker.authority)
        self._active_commands: set[str] = set()

    def active_commands(self) -> tuple[str, ...]:
        return tuple(sorted(self._active_commands))

    def resolve_command(self, call: HarnessToolCall) -> tuple[str, ...] | None:
        return _resolve_test_command(
            call.arguments, self.test_argv, self.public_probe, self.build_argv, self.feedback_plan
        )

    def public_commands(self) -> dict[str, tuple[str, ...]]:
        commands = {"build": self.build_argv, "tests": self.test_argv,
                    "public_probe": self.public_probe.argv if self.public_probe else ()}
        commands = {k:v for k,v in commands.items() if v}
        if self.feedback_plan is not None:
            for index, item in enumerate(self.feedback_plan.commands, 1):
                if item.argv not in commands.values():
                    commands[f"regression_{index}"] = item.argv
        return commands

    def _feedback_command(self, argv: tuple[str, ...]) -> RegressionCommand | None:
        return next((c for c in self.feedback_plan.commands if c.argv == argv), None
                    ) if self.feedback_plan else None

    def _authorize_read(self, context: ToolContext, path: str) -> None:
        self.context_tools._check(context, (path,))

    def _command_correction(self, call: HarnessToolCall) -> HarnessToolResult:
        probe_hint = (
            ' Public behavior check: use {"command_id":"public_probe"}.'
            if self.public_probe is not None
            else ""
        )
        if self.build_argv:
            probe_hint += ' Fixed build/check: use {"command_id":"build"}.'
        return HarnessToolResult(
            call_id=call.call_id,
            status="error",
            code="HARNESS_COMMAND_DENIED",
            content=(
                "Command was not executed. Use run_tests with "
                '{"command_id":"tests"}. Arbitrary diagnostic commands are unavailable. '
                "Use read_file or search_files to inspect source." + probe_hint
            ),
            data={
                "correctable": True,
                "executed": False,
                "retry_arguments": {"command_id": "tests"},
                "allowed_argv": list(self.test_argv),
                **({"command_ids": list(self.public_commands())} if self.feedback_plan else {}),
            },
        )

    def invoke_interaction(
        self,
        call: HarnessToolCall,
        context: ToolContext,
        cancellation: CancellationToken,
        callback: Callable[[], HarnessToolResult],
    ) -> HarnessToolResult:
        cancellation.ensure_active()
        if self.recovery_io is None:
            return callback()
        return self.recovery_io.invoke(
            context.operation_id,
            "tool",
            call.model_dump(mode="json"),
            callback,
            encode=lambda item: item.model_dump(mode="json"),
            decode=HarnessToolResult.model_validate,
        )

    def error_scope(self, context: ToolContext) -> dict[str, Any]:
        """Hints are an intersection, never a replacement for Broker authorization."""
        try:
            lease = self.broker.authority.peek()
        except DomainError:
            return {}
        if (lease.actor != context.task.actor or lease.workspace_id != context.task.workspace_id
                or lease.repository != context.task.repository.lower()
                or lease.execution_epoch != context.task.execution_epoch):
            return {}
        grant = context.grant
        command_ids = []
        if grant.execute and lease.mode != "investigate":
            for name, argv in (
                ("tests", self.test_argv), ("build", self.build_argv),
                ("public_probe", self.public_probe.argv if self.public_probe else ()),
            ):
                if argv and grant.can_execute(argv) and argv[0] in lease.allowed_commands:
                    command_ids.append(name)
        return {
            "read_roots": effective_roots(
                grant.read_paths, lease.read_paths, lease.protected_paths
            ),
            "write_roots": effective_roots(
                grant.write_paths, lease.write_paths, lease.protected_paths
            ) if lease.mode != "investigate" else (),
            "command_ids": tuple(command_ids),
        }

    def invoke_guarded(
        self,
        call: HarnessToolCall,
        context: ToolContext,
        cancellation: CancellationToken,
        check: Callable[[], HarnessToolResult | None],
        transform: Callable[[HarnessToolResult], HarnessToolResult],
        interaction: Callable[[], HarnessToolResult] | None = None,
        *,
        dispatch: Callable[[], HarnessToolResult] | None = None,
        validate_known: Callable[[HarnessToolResult], None] | None = None,
    ) -> HarnessToolResult:
        """Journal even a preflight denial with the original operation and CAS.

        The callback runs after the existing epoch/lease check. Unknown mutation
        results retain the original RecoveryIO reconciliation rules.
        """
        def invoke() -> HarnessToolResult:
            cancellation.ensure_active()
            try:
                self.broker.authority.check_scope(
                    actor=context.task.actor, workspace_id=context.task.workspace_id,
                    repository=context.task.repository,
                    execution_epoch=context.task.execution_epoch, paths=(), write=False,
                )
            except DomainError as exc:
                return transform(HarnessToolResult(
                    call_id=call.call_id, status="denied", code=exc.code, content=str(exc),
                ))
            denial = check()
            if denial is not None:
                return transform(denial)
            handler = interaction or dispatch
            result = handler() if handler is not None else self._invoke(call, context, cancellation)
            return transform(result)

        if self.recovery_io is None:
            return invoke()

        def verify(result: HarnessToolResult) -> None:
            self._verify_known(result, cancellation, context)
            if validate_known is not None:
                validate_known(result)

        result = self.recovery_io.invoke(
            context.operation_id, "tool", call.model_dump(mode="json"), invoke,
            encode=lambda item: item.model_dump(mode="json"),
            decode=HarnessToolResult.model_validate,
            read_only=call.name not in {"apply_edits", "run_tests", "run_command", "ask_user"},
            validate_known=verify,
        )
        return self._restored_feedback(result, call, context)

    def _verify_known(
        self, result: HarnessToolResult, cancellation: CancellationToken, context: ToolContext,
    ) -> None:
        cancellation.ensure_active()
        self.broker.authority.peek()
        if self.expert is not None:
            self.expert.verify_known(result, context)
        if self.guidance is not None and result.code == GUIDANCE_REQUIRED:
            self.guidance.authorize(*result.data["guidance_paths"])

    def _restored_feedback(
        self, result: HarnessToolResult, call: HarnessToolCall, context: ToolContext,
    ) -> HarnessToolResult:
        raw = result.data.get("receipt")
        if (self.feedback_plan is None or not isinstance(raw, dict)
                or raw.get("public_feedback") is not None):
            return result
        assert self.recovery_io is not None
        self.context_tools.restore_known(context, self.recovery_io)
        receipt = ExecutionReceipt.model_validate(raw)
        reference = next((r for r in self.context_tools.references()
                          if r["kind"] == "log" and r["artifact_hash"] == receipt.log_sha256), None)
        if reference is None:
            raise DomainError("PUBLIC_FEEDBACK_LOG_MISSING", "Verified public log unavailable", 409)
        feedback = observe_public_command(
            receipt, context, command_id=str(call.arguments.get("command_id", "tests")),
            artifact_hash=reference["artifact_hash"], complete=reference["complete"],
            command=self._feedback_command(receipt.argv),
        )
        return result.model_copy(update={
            "content":(render_public_feedback(feedback) + result.content
                       + artifact_trailer(reference)),
            "data":{**result.data, "artifact":reference,
                    "receipt":receipt.model_copy(update={"public_feedback":feedback}).model_dump(
                        mode="json")},
        })

    def specs(self) -> tuple[ToolSpec, ...]:
        command = shlex.join(self.test_argv)
        try:
            scopes = self.broker.authority.peek()
            scope_hint = (
                " Read="
                + repr(scopes.read_paths)
                + "; write="
                + repr(scopes.write_paths)
                + "; protected="
                + repr(scopes.protected_paths)
                + ". Root=''; no symlink writes."
            )
        except DomainError as exc:
            # Tool metadata must remain available while producing a blocked
            # final result. Actual invocation always checks current authority.
            scope_hint = f" Authority unavailable ({exc.code}); operations are blocked."
        workspace_specs = tuple(
            item.model_copy(update={"description": (item.description + scope_hint)})
            for item in self.workspace_tools.specs()
        )
        spec = RUN_TESTS_SPEC.model_copy(
            update={
                "description": (
                    "Run the trusted test command for this task inside the restricted "
                    f"container. Use command_id='tests'. Fixed argv: {command}. "
                    "Other commands are unavailable; use search_files for file diagnostics."
                )
            }
        )
        extra: tuple[ToolSpec, ...] = ()
        if self.public_probe is not None:
            schema = json.loads(json.dumps(spec.input_schema))
            schema["properties"]["command_id"]["enum"] = ["tests", "public_probe"]
            spec = spec.model_copy(
                update={
                    "input_schema": schema,
                    "description": (
                        spec.description
                        + " Public behavior command_id='public_probe': "
                        + shlex.join(self.public_probe.argv)
                        + "; plan hash="
                        + self.public_probe.plan_hash
                    ),
                }
            )
            extra = (
                ToolSpec(
                    name="read_public_probe",
                    capability="read",
                    description=("Read the frozen public behavior check directly with this tool. "
                                 "Its command mount is outside the source workspace."),
                    input_schema={
                        "type": "object",
                        "properties": {
                            "offset": {"type": "integer", "minimum": 0},
                            "limit": {"type": "integer", "minimum": 1, "maximum": 16000},
                        },
                        "additionalProperties": False,
                    },
                ),
            )
        if self.build_argv:
            schema = json.loads(json.dumps(spec.input_schema))
            schema["properties"]["command_id"]["enum"].append("build")
            spec = spec.model_copy(update={
                "input_schema": schema,
                "description": spec.description + " Fixed compile/check command_id='build': "
                + shlex.join(self.build_argv) + ". Build success is separate from test success.",
            })
        if self.feedback_plan is not None:
            schema = json.loads(json.dumps(spec.input_schema))
            schema["properties"]["command_id"]["enum"] = list(self.public_commands())
            spec = spec.model_copy(update={"input_schema": schema, "description":
                "Fixed public checks (only these IDs/argv are available): "
                + json.dumps(self.public_commands())
                + ". Checks run after edits. Inspect real failures, fix the code, or ask_user "
                "to clarify ambiguous public requirements. Private Judge results are unavailable."})
        return (*workspace_specs, spec, ASK_USER_SPEC, READ_LINES_SPEC, READ_SYMBOL_SPEC,
                *ARTIFACT_TOOLS, *extra, *((CONSULT,) if self.expert is not None else ()))

    def invoke(
        self,
        call: HarnessToolCall,
        context: ToolContext,
        cancellation: CancellationToken,
    ) -> HarnessToolResult:
        if self.recovery_io is not None:
            result = self.recovery_io.invoke(
                context.operation_id,
                "tool",
                call.model_dump(mode="json"),
                lambda: self._invoke(call, context, cancellation),
                encode=lambda item: item.model_dump(mode="json"),
                decode=HarnessToolResult.model_validate,
                read_only=call.name not in {"apply_edits", "run_tests", "run_command", "ask_user"},
                validate_known=lambda result: self._verify_known(result, cancellation, context),
            )
            return self._restored_feedback(result, call, context)
        return self._invoke(call, context, cancellation)

    def _invoke(
        self,
        call: HarnessToolCall,
        context: ToolContext,
        cancellation: CancellationToken,
    ) -> HarnessToolResult:
        if call.name != "run_tests":
            try:
                cancellation.ensure_active()
                if call.name == CONSULT.name:
                    if self.expert is None:
                        raise DomainError(
                            "HARNESS_EXPERT_DISABLED", "Expert consultation is disabled", 403,
                        )
                    return self.expert.invoke(call, context, cancellation)
                if call.name == "read_public_probe":
                    self.broker.authority.check_scope(
                        actor=context.task.actor,
                        workspace_id=context.task.workspace_id,
                        repository=context.task.repository,
                        execution_epoch=context.task.execution_epoch,
                        paths=(),
                        write=False,
                    )
                    if self.public_probe is None or self.public_probe_root is None:
                        raise DomainError(
                            "PUBLIC_PROBE_REQUIRED", "No public probe was frozen", 403
                        )
                    if set(call.arguments) - {"offset", "limit"}:
                        raise DomainError(
                            "HARNESS_TOOL_ARGUMENT", "Only offset/limit are supported", 422
                        )
                    offset, limit = (
                        call.arguments.get("offset", 0),
                        call.arguments.get("limit", 8000),
                    )
                    if (
                        type(offset) is not int
                        or type(limit) is not int
                        or offset < 0
                        or not 1 <= limit <= 16000
                    ):
                        raise DomainError("HARNESS_TOOL_ARGUMENT", "Invalid public probe page", 422)
                    try:
                        text = read_asset(self.public_probe, self.public_probe_root).decode("utf-8")
                    except UnicodeDecodeError as exc:
                        raise DomainError(
                            "PUBLIC_PROBE_ASSET_ENCODING", "Public probe must be UTF-8", 409
                        ) from exc
                    if offset > len(text):
                        raise DomainError(
                            "HARNESS_TOOL_ARGUMENT", "Public probe offset exceeds source", 422
                        )
                    probe_page = text[offset : offset + limit]
                    self.broker.authority.peek()
                    return HarnessToolResult(
                        call_id=call.call_id,
                        status="ok",
                        content=probe_page,
                        data={
                            "plan_hash": self.public_probe.plan_hash,
                            "asset_sha256": self.public_probe.asset_sha256,
                            "total_characters": len(text),
                            "next_offset": offset + len(probe_page)
                            if offset + len(probe_page) < len(text)
                            else None,
                        },
                    )
                if call.name == "apply_edits" and self.recovery_io is not None:
                    raw_edits = call.arguments.get("edits")
                    expected_tree = call.arguments.get("expected_tree")
                    if not isinstance(raw_edits, list) or not isinstance(expected_tree, str):
                        raise DomainError("HARNESS_TOOL_ARGUMENT", "Invalid edit arguments", 422)
                    try:
                        edits = [WorkspaceEdit.model_validate(item) for item in raw_edits]
                    except ValidationError as exc:
                        raise DomainError(
                            "HARNESS_TOOL_ARGUMENT", "Invalid workspace edit", 422
                        ) from exc
                    for edit in edits:
                        if not context.grant.can_write(edit.path):
                            raise DomainError("HARNESS_WRITE_DENIED", "Write outside grant", 403)
                    with self.recovery_io.locked():
                        targets = tuple(
                            self.workspace.resolve_edit_path(edit.path) for edit in edits
                        )
                        self.broker.authority.check_scope(
                            actor=context.task.actor, workspace_id=context.task.workspace_id,
                            repository=context.task.repository,
                            execution_epoch=context.task.execution_epoch, paths=targets, write=True,
                        )
                        if any(not context.grant.can_write(path) for path in targets):
                            raise DomainError(
                                "HARNESS_WRITE_DENIED", "Canonical write path denied", 403
                            )
                        if self.guidance is not None:
                            deferred = self.guidance.guard_edit(context, targets)
                            if deferred is not None:
                                return deferred.result(call.call_id)
                        snapshot = self.recovery_io.edit(
                            edits,
                            expected_tree=expected_tree,
                            operation_id=context.operation_id + ":edit",
                        )
                    return self._edit_result(call, edits, snapshot)
                if call.name in {spec.name for spec in ARTIFACT_TOOLS}:
                    if self.recovery_io is not None:
                        self.context_tools.restore_known(context, self.recovery_io)
                    return self.context_tools.page(call, context)
                if call.name == "search_files":
                    return self.context_tools.search(call, context)
                if call.name == "list_dir":
                    return self.context_tools.list_dir(call, context)
                if call.name == "read_lines":
                    return self.context_tools.read_lines(call, context)
                if call.name == "read_symbol":
                    return self.context_tools.read_symbol(call, context)
                result = self.workspace_tools.invoke(
                    call,
                    context,
                    cancellation,
                    authorize=lambda path: self._authorize_read(context, path),
                )
                if call.name == "read_file" and result.status == "ok":
                    path = call.arguments.get("path")
                    if not isinstance(path, str):
                        raise DomainError("HARNESS_TOOL_ARGUMENT", "Read needs path", 422)
                    reference = self.context_tools.archive(result.content, context, path=path)
                    result = result.model_copy(
                        update={
                            "content": result.content
                            + _file_digest_trailer(
                                result.content, self.workspace.snapshot().tree_hash
                            )
                            + artifact_trailer(reference),
                            "data": {**result.data, "artifact": reference},
                        }
                    )
                return result
            except DomainError as exc:
                if call.name in {spec.name for spec in ARTIFACT_TOOLS} and exc.code in {
                    "CONTEXT_CURSOR_INVALID",
                    "HARNESS_TOOL_ARGUMENT",
                }:
                    try:
                        correction = self.context_tools.recover_page(call, context)
                    except DomainError as recovery_error:
                        exc = recovery_error
                    else:
                        return HarnessToolResult(
                            call_id=call.call_id,
                            status="error",
                            code=exc.code,
                            content=json.dumps(correction, ensure_ascii=False),
                            data={"correctable": True, **correction},
                        )
                return HarnessToolResult(
                    call_id=call.call_id,
                    status="denied" if exc.status in {403, 429} else "error",
                    code=exc.code,
                    content=str(exc),
                )
        try:
            cancellation.ensure_active()
            self.broker.authority.check_scope(
                actor=context.task.actor,
                workspace_id=context.task.workspace_id,
                repository=context.task.repository,
                execution_epoch=context.task.execution_epoch,
                paths=(),
                write=False,
            )
            argv = self.resolve_command(call)
            if argv is None:
                return self._command_correction(call)
            is_build = bool(self.build_argv and call.arguments == {"command_id": "build"})
            purpose: Literal["build", "test"] = "build" if is_build else "test"
            started = time.monotonic()
            request = ExecutionRequest(
                operation_id=context.operation_id,
                purpose=purpose,
                argv=argv,
                cwd=".",
                timeout_seconds=self.command_timeout_seconds,
                execution_epoch=context.task.execution_epoch,
            )
            handle = self.session.run(request, context.grant, cancellation, purpose=purpose)
            self._active_commands.add(handle.handle_id)
            try:
                final = self.session.wait(
                    handle.handle_id, timeout=self.command_timeout_seconds + 30
                )
                provenance = self.session.assert_authority(handle.handle_id)
                page = self.session.read_log(handle.handle_id, offset=0)
                chunks = [page.data]
                while not page.eof:
                    following = self.session.read_log(handle.handle_id, offset=page.next_offset)
                    if following.next_offset <= page.next_offset:
                        raise DomainError("HARNESS_LOG_INCOMPLETE", "Log did not advance", 409)
                    page = following
                    chunks.append(page.data)
                output = "".join(chunks)
                self.session.assert_authority(handle.handle_id)
            finally:
                self._active_commands.discard(handle.handle_id)
            log_complete = not (page.truncated or page.dropped_bytes)
            log_reference = self.context_tools.archive_command_log(
                output,
                context,
                provenance=provenance,
                complete=log_complete,
                total_bytes=page.total_bytes,
            )
            is_probe = (
                not is_build and self.public_probe is not None and argv == self.public_probe.argv
            )
            status, run, failed, skipped, detail = (
                _build_status(final.exit_code)
                if is_build
                else ("passed" if final.exit_code == 0 else "failed", 0, 0, 0, "Public probe")
                if is_probe
                else _feedback_test_status(self.feedback_plan, self.kind, output,
                                           final.exit_code, argv=argv, complete=log_complete)
            )
            if not log_complete:
                status, detail = "unverified", "Command log is incomplete; no verified pass"
            receipt = ExecutionReceipt(
                receipt_id=canonical_hash(
                    {
                        "handle": final.handle_id,
                        "argv": list(argv),
                        "status": status,
                        "exit": final.exit_code,
                        "tree": final.tree_hash,
                        "log": sha256_text(output),
                        "provenance": provenance.model_dump(mode="json"),
                    }
                ),
                operation_id=context.operation_id,
                purpose=purpose,
                status=cast(
                    Any,
                    (
                        _execution_status(final.state, final.exit_code)
                        if status == "passed" or final.state in {"timed_out", "cancelled"}
                        else "failed"
                        if status == "failed"
                        else "error"
                    ),
                ),
                argv=argv,
                cwd=".",
                exit_code=final.exit_code if final.exit_code is not None else -1,
                stdout=output,
                stderr="",
                duration_seconds=time.monotonic() - started,
                tree_hash=final.tree_hash,
                log_sha256=sha256_text(output),
                environment_hash=final.environment_hash,
                tests_run=run,
                tests_failed=failed,
                provenance=provenance,
            )
            raw_receipt = receipt
            probe_result = None
            if is_probe:
                assert self.public_probe is not None
                probe_result = normalize_probe_receipt(
                    self.public_probe,
                    raw_receipt,
                    task=context.task,
                    expected_tree=context.workspace_tree,
                    log_complete=log_complete,
                    log_limit_bytes=self.broker.authority.peek().max_output_bytes,
                )
                receipt = probe_result.execution
                status = (
                    "passed"
                    if probe_result.outcome == "green"
                    else "failed"
                    if probe_result.outcome == "red"
                    else "unverified"
                )
                run, failed, skipped = receipt.tests_run, receipt.tests_failed, 0
                detail = (
                    "Public probe "
                    + probe_result.outcome
                    + ": "
                    + (
                        probe_result.reason_code
                        or json.dumps([case.model_dump() for case in probe_result.cases])
                    )
                )
            ok = final.state == "exited" and status == "passed"
            content = _test_feedback(detail, output) + artifact_trailer(log_reference)
            if self.feedback_plan is not None:
                feedback = observe_public_command(
                    receipt, context, command_id=str(call.arguments.get("command_id", "tests")),
                    artifact_hash=log_reference["artifact_hash"], complete=log_complete,
                    command=self._feedback_command(receipt.argv),
                )
                receipt = receipt.model_copy(update={"public_feedback": feedback})
                content = render_public_feedback(feedback) + content
            result = HarnessToolResult(
                call_id=call.call_id,
                status="ok" if ok else "error",
                content=content,
                data={
                    "receipt": receipt.model_dump(mode="json"),
                    **(
                        {
                            **_public_probe_source_data(raw_receipt, {"artifact": log_reference}),
                            "public_probe": probe_result.model_dump(
                                mode="json", exclude={"execution"}
                            ),
                        }
                        if probe_result is not None
                        else {}
                    ),
                    "tests_run": run,
                    "tests_failed": failed,
                    "skipped": skipped,
                    "detail": detail,
                    "test_status": status,
                    "test_runner": "build" if is_build else infer_test_runner(argv),
                    **({"command_kind": "build"} if is_build else {}),
                    "truncated": not log_complete,
                    "dropped_bytes": page.dropped_bytes,
                    "total_log_bytes": page.total_bytes,
                    "artifact": log_reference,
                },
            )
            self.session.assert_authority(handle.handle_id)
            return result
        except DomainError as exc:
            return HarnessToolResult(
                call_id=call.call_id,
                status="denied" if exc.status in {403, 429} else "error",
                code=exc.code,
                content=str(exc),
            )

    def _edit_result(
        self,
        call: HarnessToolCall,
        edits: Sequence[WorkspaceEdit],
        snapshot: WorkspaceSnapshot,
    ) -> HarnessToolResult:
        from mgar_v3.harness.code_diagnostics import edit_diagnostics

        diagnostics = edit_diagnostics(snapshot.files, (edit.path for edit in edits))
        return HarnessToolResult(
            call_id=call.call_id,
            status="ok",
            content="edits applied" + (
                "\nAdvisory static diagnostics (not test results):\n"
                + json.dumps(diagnostics, ensure_ascii=False) if diagnostics else ""
            ),
            data={
                **({"diagnostics": diagnostics} if diagnostics else {}),
                "tree_hash": snapshot.tree_hash,
                "changes": self.workspace.status(),
                "protocol": protocol(edits),
                "output_bytes": output_bytes(edits),
            },
        )


@dataclass(frozen=True)
class CodingTaskRequest:
    project_id: str
    objective: str
    task_kind: str = "bugfix"
    test_command: str = ""
    target_path: str = ""
    old_text: str = ""
    new_text: str = ""
    occurrence: int | None = None
    approved: bool = False
    mode: str = "deterministic"
    # Product metadata is constructed by the authenticated orchestration service,
    # never accepted by the historical task/fixture API. No credentials live here.
    product_session_id: str = ""
    product_expert_enabled: bool = False
    product_expert_max_tokens: int = 0
    product_managed: bool = False
    product_membership_id: str = ""
    product_turn_id: int = 0
    product_model_revision: int = 0
    product_model_capabilities: dict[str, Any] = field(default_factory=dict)
    product_context_policy: dict[str, Any] = field(default_factory=dict)
    product_authorization_revision: int = 0
    product_previous_task_id: str = ""
    product_plan: dict[str, Any] = field(default_factory=dict)
    product_limits: dict[str, Any] = field(default_factory=dict)
    product_context: str = ""
    product_guidance: dict[str, Any] = field(default_factory=dict)
    product_plugin_bindings: dict[str, str] = field(default_factory=dict)
    product_knowledge_scopes: dict[str, str] = field(default_factory=dict)
    product_mcp_endpoint_hash: str = ""
    product_intent: str = ""


class TaskControl:
    """Cooperative pause/cancel control shared by the API and the worker."""

    def __init__(self, token: CancellationToken | None = None) -> None:
        self.token = token or CancellationToken()
        self._condition = threading.Condition()
        self.pause_requested = False
        self.pause_requested_at = ""
        self.cancel_requested_at = ""
        self.cancel_reason = ""
        self.on_paused: Callable[[], None] | None = None
        self.on_resumed: Callable[[], None] | None = None
        self.on_cancel_observed: Callable[[], None] | None = None

    @property
    def cancel_requested(self) -> bool:
        with self._condition:
            return bool(self.cancel_requested_at)

    def wait(self, timeout: float) -> None:
        with self._condition:
            self._condition.wait(timeout)

    def wake(self) -> None:
        with self._condition:
            self._condition.notify_all()

    def request_pause(self) -> str:
        with self._condition:
            if not self.pause_requested:
                self.pause_requested = True
                self.pause_requested_at = datetime.now(UTC).isoformat()
            self._condition.notify_all()
            return self.pause_requested_at

    def request_resume(self) -> None:
        with self._condition:
            self.pause_requested = False
            self._condition.notify_all()

    def request_cancel(self, reason: str = "") -> str:
        with self._condition:
            if not self.cancel_requested_at:
                self.cancel_requested_at = datetime.now(UTC).isoformat()
                self.cancel_reason = reason
            self.token.cancel()
            self._condition.notify_all()
            return self.cancel_requested_at

    def checkpoint(self, stage: str, revision: int) -> SafePoint:
        del stage, revision
        with self._condition:
            if self.cancel_requested_at:
                if self.on_cancel_observed is not None:
                    self.on_cancel_observed()
                self.token.ensure_active()
            if self.pause_requested:
                if self.on_paused is not None:
                    self.on_paused()
                while self.pause_requested and not self.cancel_requested_at:
                    self._condition.wait(0.2)
                if self.cancel_requested_at:
                    if self.on_cancel_observed is not None:
                        self.on_cancel_observed()
                    self.token.ensure_active()
                if self.on_resumed is not None:
                    self.on_resumed()
        self.token.ensure_active()
        return SafePoint()


@dataclass
class _TaskRuntime:
    task_id: str
    workspace: TaskWorkspace
    registration: Registration
    request: CodingTaskRequest
    control: TaskControl
    inbox: Any
    events: Any
    task: TaskBinding | None = None
    lease: CapabilityLease | None = None
    thread: threading.Thread | None = None
    started_at: str = ""
    finished_at: str = ""
    error: str = ""
    cancel_event_emitted: bool = False
    pause_event_emitted: bool = False
    paused_event: bool = False
    cancel_observed_event: bool = False
    durable_row: dict[str, Any] | None = None
    delivered_instructions: set[str] = field(default_factory=set)


class _TaskInteraction:
    """``ask_user`` implementation backed by the persistent inbox."""

    def __init__(self, service: HarnessCodingService, runtime: _TaskRuntime) -> None:
        self.service = service
        self.runtime = runtime

    def ask(self, *, question: str, task: TaskBinding, revision: int) -> str:
        del task
        question_id = f"q-{uuid4().hex[:12]}"
        self.runtime.inbox.add_question(question_id=question_id, text=question)
        self.service._emit(
            self.runtime,
            "ask_user",
            revision,
            {
                "question_id": question_id,
                "question": question,
                "status": "waiting",
                "message_id": question_id,
            },
        )
        self.service._status(
            self.runtime,
            status="waiting_input",
            question_id=question_id,
            question=question,
        )
        answer = self.runtime.inbox.wait_for_answer(question_id, self.runtime.control)
        if answer is None:
            raise DomainError(
                "HARNESS_ASK_TIMEOUT",
                "The operator did not answer the question in time",
                409,
            )
        record = self.runtime.inbox.mark_applied(str(answer["message_id"]), revision)
        self.runtime.inbox.mark_applied(question_id, revision)
        self.service._emit(
            self.runtime,
            "ask_user_answered",
            revision,
            {
                "question_id": question_id,
                "message_id": str(answer["message_id"]),
                "status": "applied",
                "revision": revision,
            },
        )
        self.service._status(
            self.runtime,
            status="running",
            question_id="",
            question="",
        )
        return str(record["text"] if record is not None else answer["text"])


class SessionApplication:
    """ApplicationPort over the existing owned H06 command lifecycle."""

    def __init__(
        self,
        session: CommandSession,
        task: TaskBinding,
        budgets: Any,
        recovery_io: RecoveryIO | None,
        cwd: str = ".",
        policy: LifecyclePolicy | None = None,
        artifacts: Any = None,
    ) -> None:
        self.session, self.task, self.budgets, self.io = session, task, budgets, recovery_io
        self.cwd = cwd
        self.lifecycle_policy = policy
        self.artifacts = artifacts

    def _invoke(
        self, operation_id: str, request: dict[str, Any], callback: Callable[[], ServiceReceipt]
    ) -> ServiceReceipt:
        reservation = self.budgets.reserve(self.task.task_id, operation_id, "tool")
        try:
            receipt = (
                self.io.invoke(
                    operation_id,
                    "application",
                    request,
                    callback,
                    encode=lambda value: value.model_dump(mode="json"),
                    decode=ServiceReceipt.model_validate,
                )
                if self.io is not None
                else callback()
            )
        except BaseException:
            self.budgets.mark_unknown(
                reservation.reservation_id, "Application outcome requires reconciliation"
            )
            raise
        self.budgets.settle(reservation.reservation_id, actual_micros=0, actual_units=1)
        return receipt

    def start(
        self,
        plan: ServicePlan,
        tree: str,
        grant: CapabilityGrant,
        cancellation: CancellationToken,
    ) -> ServiceReceipt:
        cancellation.ensure_active()
        if self.lifecycle_policy is not None:
            return self._controlled_start(plan, tree, grant, cancellation)
        operation_id = f"{self.task.task_id[:40]}:app:{tree[:16]}:start"

        def start() -> ServiceReceipt:
            request = ExecutionRequest(
                operation_id=operation_id,
                purpose="command",
                argv=plan.argv,
                cwd=self.cwd,
                timeout_seconds=min(
                    float(grant.max_seconds),
                    1800,
                    plan.timeout_seconds if plan.mode == "oneshot" else 1800,
                ),
                execution_epoch=self.task.execution_epoch,
            )
            if plan.mode == "oneshot":
                handle = self.session.run(request, grant, CancellationToken(), purpose="service")
                finished = self.session.wait(handle.handle_id)
                # H06 finalizes logs and stops the owned container before
                # returning a terminal handle. Exit zero is lifecycle evidence;
                # task behavior still requires the separate independent judge.
                ready = (
                    finished.state == "exited"
                    and finished.exit_code == 0
                    and not finished.log_truncated
                    and bool(finished.log_sha256)
                )
                return ServiceReceipt(
                    receipt_id=canonical_hash({"operation": operation_id, "tree": tree}),
                    argv=plan.argv,
                    tree_hash=tree,
                    started=True,
                    ready=ready,
                    stopped=finished.state != "running",
                    command_handle_id=finished.handle_id,
                    probe_detail=(
                        "H06 one-shot command exited zero with complete logs"
                        if ready
                        else f"H06 one-shot command ended as {finished.state}; "
                        f"exit={finished.exit_code}, log_truncated={finished.log_truncated}"
                    ),
                    cleanup_detail="Owned one-shot command finalized through H06",
                    log_sha256=finished.log_sha256,
                )
            # Stopping an owned service must not cancel the entire coding task.
            launch = self.session.start_service(
                request,
                grant,
                CancellationToken(),
                ReadinessProbe(
                    kind="http",
                    port=plan.port,
                    path=plan.path,
                    timeout_seconds=plan.timeout_seconds,
                ),
            )
            return ServiceReceipt(
                receipt_id=canonical_hash({"operation": operation_id, "tree": tree}),
                argv=plan.argv,
                tree_hash=tree,
                started=True,
                ready=True,
                stopped=False,
                address=launch.address,
                command_handle_id=launch.handle.handle_id,
                probe_detail="H06 HTTP readiness succeeded inside the owned container",
            )

        return self._invoke(
            operation_id, {"plan": plan.model_dump(mode="json"), "tree": tree}, start
        )

    def stop(self, receipt: ServiceReceipt) -> ServiceReceipt:
        if self.lifecycle_policy is not None and receipt.lifecycle is not None:
            return self._controlled_stop(receipt)
        operation_id = f"{self.task.task_id[:40]}:app:{receipt.tree_hash[:16]}:stop"

        def stop() -> ServiceReceipt:
            if receipt.stopped:
                return receipt.model_copy(update={
                    "receipt_id": canonical_hash(
                        {"operation": operation_id, "start": receipt.receipt_id}
                    ),
                    "cleanup_detail": "Owned command already finalized through H06",
                })
            try:
                handle = self.session.cancel(receipt.command_handle_id)
            except DomainError as exc:
                if self.io is None or exc.code != "HARNESS_COMMAND_NOT_FOUND":
                    raise
                # Service restart reconciliation already stopped the exact
                # original command. Never launch it again to manufacture a stop.
                start_id = f"{self.task.task_id[:40]}:app:{receipt.tree_hash[:16]}:start:process"
                known = self.io.store.operation(self.task.task_id, start_id)
                if known is None or known["state"] != "known":
                    raise DomainError(
                        "RECOVERY_BLOCKED", "Service stop is not reconciled", 409
                    ) from exc
                handle = None
            return receipt.model_copy(
                update={
                    "receipt_id": canonical_hash(
                        {"operation": operation_id, "start": receipt.receipt_id}
                    ),
                    "stopped": True,
                    "cleanup_detail": "Owned command stopped and cleaned through H06",
                    "log_sha256": handle.log_sha256 if handle is not None else receipt.log_sha256,
                }
            )

        return self._invoke(operation_id, {"start_receipt": receipt.model_dump(mode="json")}, stop)

    def _controlled_start(
        self, plan: ServicePlan, tree: str, grant: CapabilityGrant, token: CancellationToken,
    ) -> ServiceReceipt:
        assert self.lifecycle_policy is not None
        policy = self.lifecycle_policy
        operation_id = (f"{self.task.task_id[:36]}:app:{tree[:12]}:"
                        f"{(plan.acceptance_hash or '')[:8]}:start")

        def start() -> ServiceReceipt:
            runner = LifecycleRunner(self.session, self.task, policy, self.artifacts)
            handle, evidence = runner.application(
                plan.argv, grant, operation_id, tree, token, mode=plan.mode, port=plan.port,
                routes=plan.routes or (HTTPExpectation(path=plan.path),),
                cwd=self.cwd, timeout=plan.timeout_seconds,
            )
            # Complete the verification cycle inside the already reserved
            # start operation. Cleanup cannot depend on buying another tool
            # slot or on the model producing another response.
            cleanup = evidence.cleanup
            if cleanup is None:
                cleanup = runner.cleanup(
                    handle.handle_id, operation_id, tree, port=evidence.ready.port or 0,
                    expect_running=evidence.ready.ready and plan.mode == "http",
                )
                evidence = evidence.model_copy(update={"cleanup": cleanup})
            ready_values = evidence.ready.model_dump(exclude={"receipt_id"})
            ready_values["acceptance_hash"] = plan.acceptance_hash
            ready = sealed(type(evidence.ready), ready_values)
            finished = self.session.poll(handle.handle_id)
            evidence = evidence.model_copy(update={
                "ready": ready, "raw_log_hash": finished.log_sha256,
            })
            return ServiceReceipt(
                receipt_id=digest({"operation": operation_id, "ready": ready.receipt_id,
                                   "cleanup": cleanup.receipt_id if cleanup else None}),
                argv=plan.argv, tree_hash=tree, started=True, ready=ready.ready,
                stopped=bool(cleanup and cleanup.processes_exited is True
                             and cleanup.port_released is True and cleanup.container_removed),
                process_id=ready.process_id, command_handle_id=handle.handle_id,
                address=f"{ready.host}:{ready.port}" if ready.port else "",
                probe_detail=ready.detail, cleanup_detail=cleanup.detail if cleanup else "",
                log_sha256=finished.log_sha256, lifecycle=evidence,
            )

        return self._invoke(operation_id, {"plan": plan.model_dump(mode="json"), "tree": tree,
                                           "policy": self.lifecycle_policy.model_dump(mode="json")},
                            start)

    def _controlled_stop(self, receipt: ServiceReceipt) -> ServiceReceipt:
        assert receipt.lifecycle is not None and self.lifecycle_policy is not None
        policy = self.lifecycle_policy
        evidence = receipt.lifecycle
        operation_id = evidence.ready.command_id.removesuffix(":start") + ":stop"

        def stop() -> ServiceReceipt:
            cleanup = evidence.cleanup
            if cleanup is None:
                try:
                    runner = LifecycleRunner(self.session, self.task, policy, self.artifacts)
                    cleanup = runner.cleanup(
                        receipt.command_handle_id, evidence.ready.command_id, receipt.tree_hash,
                        port=evidence.ready.port or 0,
                        expect_running=evidence.mode == "http" and evidence.ready.ready,
                    )
                except DomainError as exc:
                    if self.io is None or exc.code != "HARNESS_COMMAND_NOT_FOUND":
                        raise
                    original = self.io.store.operation(
                        self.task.task_id, evidence.ready.command_id + ":process",
                    )
                    if original is None or original["state"] != "known":
                        raise DomainError(
                            "RECOVERY_BLOCKED", "Service requires reconciliation", 409
                        ) from exc
                    # Recovery's forced stop cannot manufacture a graceful
                    # shutdown or a missing process/port observation.
                    cleanup = ProcessPortReceipt(
                        receipt_id=digest({"recovered": operation_id}),
                        observed_at=datetime.now(UTC).isoformat(),
                        command_id=evidence.ready.command_id, tree_hash=receipt.tree_hash,
                        signal="RECOVERY", graceful=False, forced=True,
                        processes_exited=None, port_released=None, container_removed=False,
                        detail="Recovered stop lacks graceful process/port observations",
                    )
            try:
                log_hash = self.session.poll(receipt.command_handle_id).log_sha256
            except DomainError:
                log_hash = receipt.log_sha256
            return receipt.model_copy(update={
                "receipt_id": digest({"operation": operation_id, "cleanup": cleanup.receipt_id}),
                "stopped": (cleanup.processes_exited is True and cleanup.port_released is True
                            and cleanup.container_removed),
                "cleanup_detail": cleanup.detail,
                "lifecycle": evidence.model_copy(update={"cleanup": cleanup,
                                                         "raw_log_hash": log_hash}),
                "log_sha256": log_hash,
            })

        return self._invoke(operation_id, {"start_receipt": receipt.model_dump(mode="json")}, stop)


class HarnessCodingService:
    """Register local projects and run one H08 coding task per request."""

    def __init__(
        self,
        root: Path,
        *,
        actor: str,
        image_lock: Path,
        live_model_id: str = DEEPSEEK_CHAT.model_id,
        live_api_key: str | None = None,
        allow_paid: bool = False,
        engine: Any = None,
        model_factory: Callable[[CodingTaskRequest], Any] | None = None,
        fault_hook: Callable[[str, dict[str, Any]], None] | None = None,
        evaluation_profile: EvaluationProfile | None = None,
        public_probe_root: Path | None = None,
        final_gate: Callable[[TaskWorkspace, dict[str, Any]], dict[str, Any]] | None = None,
        before_model_request: Callable[[ModelRequest], None] | None = None,
    ) -> None:
        self.root = Path(root).resolve()
        self.runs = self.root / "runs"
        self.runs.mkdir(parents=True, exist_ok=True)
        self.registry_root = self.root / "registry"
        self.registry = WorkspaceRegistry(self.registry_root)
        self.projects_file = self.root / "projects.json"
        self.actor = actor
        self.image_lock = Path(image_lock)
        self._live_model_id = live_model_id
        self._live_api_key = live_api_key
        self._allow_paid = allow_paid
        self.engine = engine
        self.store = engine.harness if engine is not None else None
        self.runners = RunnerStore(engine) if engine is not None else None
        self.model_factory = model_factory
        self.before_model_request = before_model_request
        self.product_model_resolver: (
            Callable[[CodingTaskRequest], tuple[str, ModelCapabilities]] | None
        ) = None
        self.product_extension_factory: Callable[[Any, Any], Any] | None = None
        self.product_adapter_factory: Callable[
            [CodingTaskRequest, str, ModelCapabilities], ModelAdapter | None
        ] | None = None
        self.fault_hook = fault_hook or (lambda point, payload: None)
        if evaluation_profile is not None and engine is None:
            raise DomainError(
                "EVALUATION_ENGINE_REQUIRED", "Evaluation needs Engine authority", 409
            )
        if evaluation_profile is not None and (
            evaluation_profile.baseline_policy is not None
            or evaluation_profile.public_feedback_policy is not None
        ):
            # A caller's model_copy must not bypass the public-policy/review binding.
            evaluation_profile = EvaluationProfile.model_validate(
                evaluation_profile.model_dump(mode="json"))
        self.evaluation_profile = evaluation_profile
        self.public_probe_root = public_probe_root
        if evaluation_profile is not None:
            if evaluation_profile.verification_protocol == "public-probe-v1":
                if evaluation_profile.public_probe is None or public_probe_root is None:
                    raise DomainError(
                        "PUBLIC_PROBE_REQUIRED",
                        "Freeze the public plan and asset before model dispatch",
                        409,
                    )
                read_asset(evaluation_profile.public_probe, public_probe_root)
            elif evaluation_profile.public_probe is not None or public_probe_root is not None:
                raise DomainError(
                    "PUBLIC_PROBE_PROTOCOL_REQUIRED",
                    "Public assets need the explicit public-probe-v1 protocol",
                    409,
                )
        self.final_gate = final_gate
        self.model_capabilities = (
            ModelCapabilities.model_validate(evaluation_profile.model)
            if evaluation_profile is not None
            else DEEPSEEK_CHAT
        )
        if evaluation_profile is not None:
            self._live_model_id = self.model_capabilities.model_id
        if evaluation_profile is not None and (
            self.model_capabilities.provider != "deepseek"
            or evaluation_profile.resources.max_output_tokens
            > self.model_capabilities.max_output_tokens
        ):
            raise DomainError("EVALUATION_MODEL_DENIED", "Unsupported frozen model capability", 409)
        self._lock = threading.Lock()
        self._runtime_lock = threading.Lock()
        self._general_launch_lock = threading.Lock()
        self._runtimes: dict[str, _TaskRuntime] = {}

    # -- projects -------------------------------------------------------

    def list_projects(self) -> list[dict[str, Any]]:
        projects = self._projects()
        return [
            {
                "project_id": project_id,
                "path": entry.get("path", ""),
                "repository": entry.get("repository", ""),
                "base_commit": entry.get("base_commit", ""),
                "registered_at": entry.get("registered_at", ""),
                "execution_site": self._execution_site(entry.get("path", "")),
            }
            for project_id, entry in sorted(projects.items())
        ]

    def _execution_site(self, path: str) -> dict[str, str]:
        if path and Path(path).is_relative_to(self.root / "github-sources"):
            return {"kind": "server_sandbox", "name": "服务器隔离环境"}
        return {"kind": "harness_host", "name": "Harness 所在主机"}

    def register_project(self, path: str) -> dict[str, Any]:
        registration = self.registry.register_local(path, actor=self.actor)
        source_path = str(Path(path).resolve())
        with self._lock:
            projects = self._projects()
            projects[registration.workspace_id] = {
                "path": source_path,
                "repository": registration.repository,
                "base_commit": registration.base_commit,
                "registered_at": registration.registered_at,
            }
            self._write_json(self.projects_file, projects)
        return {
            "project_id": registration.workspace_id,
            "path": source_path,
            "repository": registration.repository,
            "base_commit": registration.base_commit,
            "execution_site": self._execution_site(source_path),
        }

    def live_model_status(self) -> dict[str, Any]:
        return {
            "available": bool(self._allow_paid and self._live_api_key),
            "allow_paid": self._allow_paid,
            "credential_present": bool(self._live_api_key),
            "model_id": self._live_model_id,
        }

    # -- tasks ----------------------------------------------------------

    def create_task(self, request: CodingTaskRequest) -> dict[str, Any]:
        runtime = self._prepare(request)
        try:
            return self._run(runtime)
        finally:
            self._sweep(runtime.workspace)

    def submit_task(self, request: CodingTaskRequest) -> dict[str, Any]:
        runtime = self._prepare(request)
        return self._submit_runtime(runtime)

    def _submit_runtime(self, runtime: _TaskRuntime) -> dict[str, Any]:
        thread = threading.Thread(
            target=self._background,
            args=(runtime,),
            name=f"harness-{runtime.task_id}",
            daemon=True,
        )
        runtime.thread = thread
        thread.start()
        return self._project_status(runtime.task_id)

    def submit_general_task(
        self, request: CodingTaskRequest, dispatch: CodingDispatch
    ) -> dict[str, Any]:
        """Submit a graph-owned task through the original durable Harness run."""
        if (
            self.store is None or self.evaluation_profile is not None
            or request.mode != "deterministic" or request.product_session_id
            or digest(asdict(request)) != dispatch.request_sha256
        ):
            raise DomainError("GENERAL_CODING_SCOPE", "Invalid Coding hand-off.", 403)
        task = dispatch.require_current(self.engine)
        if (
            task.project_id != request.project_id
            or task.spec.coding_policy != request.task_kind
            or task.spec.goal != request.objective
            or bool(dispatch.service_plan) != (request.task_kind == "run_app")
        ):
            raise DomainError("GENERAL_CODING_SCOPE", "Coding request differs from graph.", 403)
        task_id = dispatch.harness_task_id
        with self._general_launch_lock:
            try:
                row = self.store.get(task_id)
            except DomainError as exc:
                if exc.code != "HARNESS_TASK_NOT_FOUND":
                    raise
                dispatch.require_resources(self.engine)
                runtime = self._prepare(request, general_dispatch=dispatch)
                return self._submit_runtime(runtime)
            saved = row["request"].get("general_dispatch") or {}
            incoming = dispatch.model_dump(mode="json")
            if any(saved.get(key) != incoming[key] for key in (
                "claim", "access", "request_sha256", "service_plan",
            )):
                raise DomainError("GENERAL_CODING_SCOPE", "Coding submission binding differs.", 409)
            if row["status"] != "queued" or task_id in self._runtimes:
                return self.get_task(task_id)
            # A controller may stop after persisting admission and before launching.
            registration = Registration.model_validate(row["registration"])
            workspace = self.registry.open_task_workspace(registration, task_id)
            if workspace.manifest.model_dump(mode="json") != row["request"]["workspace_manifest"]:
                raise DomainError("RECOVERY_MANIFEST_CONFLICT", "Queued workspace changed.", 409)
            runtime = _TaskRuntime(
                task_id=task_id, workspace=workspace, registration=registration, request=request,
                control=TaskControl(), durable_row=row, inbox=EngineInbox(self.store, task_id),
                events=EngineEvents(self.store, task_id, 0),
            )
            self._register_runtime(runtime)
            return self._submit_runtime(runtime)

    def enqueue_product_task(self, request: CodingTaskRequest) -> dict[str, Any]:
        if self.store is None or not request.product_managed or not request.product_session_id:
            raise DomainError("HARNESS_QUEUE_UNAVAILABLE", "Managed dispatch is unavailable", 409)
        registration = self.registry.load(request.project_id)
        self.store.check_managed_admission(
            registration.model_dump(mode="json"), asdict(request),
        )
        runtime = self._prepare(request, queue_only=True)
        return self._project_status(runtime.task_id)

    def run_scheduled(self, task_id: str, stopping: threading.Event) -> None:
        assert self.store is not None
        row = self.store.get(task_id)
        if stopping.is_set():
            return
        try:
            if row["status"] == "paused" and row["resume_requested_at"]:
                self.recover_task(task_id, stopping=stopping)
                return
            if row["status"] != "queued" or row["managed_workspace_id"] is None:
                return
            request = CodingTaskRequest(**{
                key: value for key, value in row["request"].items()
                if key in CodingTaskRequest.__dataclass_fields__
            })
            registration = Registration.model_validate(row["registration"])
            with self.store.workspace_lock(row):
                workspace = self.registry.open_task_workspace(registration, task_id)
                frozen_manifest = row["request"]["workspace_manifest"]
                if (workspace.manifest.model_dump(mode="json") != frozen_manifest
                        or workspace.snapshot().tree_hash != row["tree_hash"]):
                    raise DomainError("RECOVERY_TREE_CONFLICT", "Queued workspace changed", 409)
                if stopping.is_set():
                    return
                row = self.store.begin(task_id)
            runtime = _TaskRuntime(
                task_id=task_id, workspace=workspace, registration=registration, request=request,
                control=TaskControl(), durable_row=row,
                inbox=EngineInbox(self.store, task_id, epoch=row["execution_epoch"]),
                events=EngineEvents(self.store, task_id, row["execution_epoch"]),
            )
            self._register_runtime(runtime)
            if stopping.is_set():
                self.store.pause(task_id)
                return
            self._background(runtime)
        except DomainError as exc:
            if exc.code not in {"HARNESS_CAPACITY_BUSY", "NOT_RUNNABLE", "FENCED", "STALE_EPOCH"}:
                self.store.dispatch_failed(task_id, exc.code)
        except Exception:
            self.store.dispatch_failed(task_id, "HARNESS_DISPATCH_FAILED")
        finally:
            with self._runtime_lock:
                self._runtimes.pop(task_id, None)

    def suspend_scheduled(self, task_id: str) -> None:
        assert self.store is not None
        if self.store.get(task_id)["status"] == "running":
            self.store.pause(task_id)
            runtime = self._runtimes.get(task_id)
            if runtime is not None:
                runtime.control.request_cancel("Controller stopping")

    def _prepare(
        self, request: CodingTaskRequest, *, queue_only: bool = False,
        general_dispatch: CodingDispatch | None = None,
    ) -> _TaskRuntime:
        kind = request.task_kind
        profile = self.evaluation_profile
        if kind not in {"inspect", "bugfix", "feature", "refactor"} and not (
            (profile is not None or request.product_session_id or general_dispatch is not None)
            and kind == "run_app"
        ):
            raise DomainError("HARNESS_TASK_KIND", "Unknown task kind", 422)
        objective = request.objective.strip()
        if not objective:
            raise DomainError("HARNESS_OBJECTIVE_REQUIRED", "An objective is required", 422)
        if profile is not None and (
            kind != profile.task_kind
            or objective != profile.objective.strip()
            or request.mode != "live"
            or request.old_text
            or request.new_text
        ):
            raise DomainError("EVALUATION_SCOPE_DENIED", "Task differs from frozen profile", 403)
        if profile is not None and kind == "run_app" and not profile.service_argv:
            raise DomainError(
                "EVALUATION_SERVICE_PLAN_REQUIRED", "Frozen start command is missing", 409
            )
        if kind in _WRITABLE and not request.approved:
            raise DomainError(
                "HARNESS_APPROVAL_REQUIRED",
                "Writable tasks need an explicit approval",
                403,
            )
        product_model = (
            self.product_model_resolver(request)
            if request.product_session_id and self.product_model_resolver else None
        )
        if request.product_session_id and product_model is None:
            raise DomainError("HARNESS_PRODUCT_UNAVAILABLE", "Product authority is missing", 409)
        if request.mode == "live" and not (
            product_model or (self._allow_paid and self._live_api_key)
        ):
            raise DomainError(
                "HARNESS_LIVE_MODEL_UNAVAILABLE",
                "A paid model call needs an approved credential and budget",
                409,
            )
        entry = self._projects().get(request.project_id)
        if entry is None:
            raise DomainError("HARNESS_PROJECT_UNKNOWN", "Register the project first", 404)
        registration = self.registry.load(request.project_id)
        task_id = (
            general_dispatch.harness_task_id if general_dispatch else f"h08-{uuid4().hex[:12]}"
        )
        try:
            workspace = self.registry.create_task_workspace(
                registration, task_id, source_root=Path(str(entry["path"])),
            )
        except DomainError as exc:
            if general_dispatch is None or exc.code != "WORKSPACE_CONFLICT":
                raise
            workspace = self.registry.open_task_workspace(registration, task_id)
            if workspace.snapshot().tree_hash != workspace.manifest.tree_hash:
                raise DomainError(
                    "RECOVERY_TREE_CONFLICT", "Coding workspace changed.", 409
                ) from exc
        if request.product_previous_task_id:
            self._inherit_product_tree(workspace, request)
        service_plan = general_dispatch.service_plan if general_dispatch is not None else None
        test_argv = self._test_command(workspace, request, kind) if service_plan is None and (
            self.store is not None or request.product_session_id
        ) else ()
        grant = self._grant(registration, kind, test_argv, request)
        if general_dispatch is not None:
            grant = grant.model_copy(update={
                "write_paths": (request.target_path,) if grant.write_paths else (),
                "max_seconds": general_dispatch.limits.max_seconds,
                **({"allowed_commands": (_program(service_plan.argv),), "execute": True}
                   if service_plan else {}),
            })
        if request.product_session_id:
            from mgar_v3.harness.project_context import (
                authorize_project_sources,
                load_workspace_context,
            )

            authority = CapabilityAuthority(lease=issue_lease(
                mode="investigate", actor=self.actor, workspace_id=registration.workspace_id,
                repository=registration.repository, execution_epoch=1,
                read_paths=grant.read_paths, allowed_commands=(),
            ), budget_check=lambda: True)

            def authorize(path: str) -> None:
                authorize_project_sources(
                    workspace, grant, authority, task_id=task_id, actor=self.actor,
                    epoch=1, paths=(path,),
                )

            initial_paths = tuple(path for path in (
                request.target_path, request.product_plan.get("root", "."),
            ) if path not in {"", "."})
            with workspace_lock(workspace.root):
                context = load_workspace_context(
                    workspace, request.objective, authorize, paths=initial_paths,
                )
            assert self.product_model_resolver is not None
            self.product_model_resolver(request)
            request = replace(request, product_guidance={**context.public(), "text": context.text})
            if request.approved and kind in _WRITABLE:
                self._prepare_product_permissions(workspace)
        directory = self.runs / task_id
        directory.mkdir(parents=True, exist_ok=True)
        runtime = _TaskRuntime(
            task_id=task_id,
            workspace=workspace,
            registration=registration,
            request=request,
            control=TaskControl(),
            inbox=TaskInbox(directory / "inbox.json"),
            events=PersistentEventSink(directory / "events.jsonl"),
        )
        if self.store is not None:
            limits = (
                general_dispatch.limits
                if general_dispatch is not None
                else profile.resources.budget_limits()
                if profile
                else BudgetLimits.model_validate(request.product_limits)
                if request.product_session_id else BudgetLimits(
                    max_model_calls=12,
                    max_tool_calls=48,
                    max_cost_micros=LIVE_MAX_COST_MICROS if request.mode == "live" else 1_000_000,
                    max_seconds=DEFAULT_MAX_SECONDS,
                )
            )
            runtime.durable_row = self.store.create(
                task_id,
                registration,
                {
                    **asdict(request),
                    "workspace_root": str(workspace.root),
                    "source_root": str(entry["path"]),
                    "trusted_test_argv": list(test_argv),
                    **({"trusted_build_argv": list(profile.public_build_argv)}
                       if profile is not None and profile.public_build_argv else {}),
                    "workspace_manifest": workspace.manifest.model_dump(mode="json"),
                    **({"general_dispatch": general_dispatch.model_dump(mode="json")}
                       if general_dispatch else {}),
                },
                grant.model_dump(mode="json"),
                limits.model_dump(mode="json"),
                **({"evaluation_profile": profile} if profile is not None else {}),
            )
            runtime.inbox = EngineInbox(self.store, task_id)
            runtime.events = EngineEvents(self.store, task_id, 0)
        if queue_only:
            return runtime
        self._register_runtime(runtime)
        self._status(
            runtime,
            status="queued",
            created_at=datetime.now(UTC).isoformat(),
            objective=objective,
            task_kind=kind,
            live_model=request.mode == "live",
            approval_ref=f"web-approval-{task_id}" if kind in _WRITABLE else "",
            inbox_revision=0,
        )
        return runtime

    def _register_runtime(self, runtime: _TaskRuntime) -> None:
        control = runtime.control
        control.on_paused = lambda: self._control_event(runtime, "paused")
        control.on_resumed = lambda: self._control_event(runtime, "resumed")
        control.on_cancel_observed = lambda: self._control_event(runtime, "cancel_observed")
        with self._runtime_lock:
            self._runtimes[runtime.task_id] = runtime

    def recover_task(
        self, task_id: str, *, background: bool = False, stopping: threading.Event | None = None,
    ) -> dict[str, Any]:
        if self.store is None:
            raise DomainError("HARNESS_RECOVERY_UNAVAILABLE", "Engine is required", 409)
        if not TASK_ID_PATTERN.fullmatch(task_id):
            raise DomainError("HARNESS_TASK_ID_INVALID", "Invalid task id", 400)
        if self.runners is not None and task_id.startswith("h15-"):
            return self.runners.control(task_id, "resume")
        row = self.store.get(task_id)
        frozen_profile = row.get("evaluation_profile")
        if frozen_profile is not None and (
            self.evaluation_profile is None
            or self.evaluation_profile.model_dump(mode="json") != frozen_profile
        ):
            raise DomainError("EVALUATION_SCOPE_DENIED", "Recovery profile differs", 403)
        if row["actor"] != self.actor:
            raise DomainError("HARNESS_SCOPE_DENIED", "Task actor differs", 403)
        if row["result_hash"] and row["status"] in {"succeeded", "failed", "cancelled"}:
            return load_result(
                self.engine.artifacts,
                self.engine.artifacts.read(row["result_hash"]),
                task_id,
                str(row["run_id"]),
            )
        registration = Registration.model_validate(row["registration"])
        request = CodingTaskRequest(
            **{
                key: value
                for key, value in row["request"].items()
                if key in CodingTaskRequest.__dataclass_fields__
            }
        )
        if request.product_session_id:
            assert self.product_model_resolver is not None
            self.product_model_resolver(request)
        with workspace_lock(Path(row["request"]["workspace_root"])):
            selected: dict[str, Any] = {}
            if row["checkpoint_hash"]:
                selected = _checkpoint_payload(
                    self.engine.artifacts,
                    self.engine.artifacts.read(row["checkpoint_hash"]),
                    task_id,
                    str(row["run_id"]),
                )
            pending_request = selected.get("request") or {}
            model_operation = str(pending_request.get("operation_id", ""))
            active_tool_prefix = (
                model_operation.replace(":model:", ":tool:") + ":"
                if ":model:" in model_operation
                else ""
            )
            workspace = self.registry.open_task_workspace(
                registration,
                task_id,
                allow_unreconciled=True,
            )
            original_manifest = dict(row["request"]["workspace_manifest"])
            actual_manifest = workspace.manifest.model_dump(mode="json")
            for key in ("tree_hash", "manifest_hash"):
                original_manifest.pop(key)
                actual_manifest.pop(key)
            if original_manifest != actual_manifest:
                raise DomainError(
                    "RECOVERY_MANIFEST_CONFLICT", "Immutable workspace manifest changed", 409
                )
            io = RecoveryIO(
                self.store,
                task_id,
                row["execution_epoch"],
                workspace,
                artifacts=self.engine.artifacts,
                run_id=str(row["run_id"]),
                fault=lambda point: self.fault_hook(point, {"task_id": task_id}),
            )
            # Recovery inspects only the journalled owner identity. It never
            # launches an old command with a fresh lease.
            recovery_lease = issue_lease(
                mode="investigate",
                actor=self.actor,
                workspace_id=registration.workspace_id,
                repository=registration.repository,
                execution_epoch=row["execution_epoch"],
                read_paths=("",),
                write_paths=(),
                allowed_commands=(),
            )
            recovery_broker = WorkspaceBoundBroker(
                workspace,
                recovery_lease,
                lock_path=self.image_lock,
            )
            reconciliation_errors: list[DomainError] = []
            for operation in self.store.operations(task_id):
                if operation["kind"] == "command" and operation["state"] in {
                    "known",
                    "unknown",
                    "dispatched",
                }:
                    try:
                        io.reconcile_command_locked(recovery_broker, operation["operation_id"])
                    except DomainError as exc:
                        reconciliation_errors.append(exc)
            for operation in self.store.operations(task_id):
                if operation["kind"] == "edit" and operation["state"] in {"unknown", "dispatched"}:
                    if not active_tool_prefix or not operation["operation_id"].startswith(
                        active_tool_prefix
                    ):
                        raise DomainError(
                            "RECOVERY_TREE_CONFLICT", "Edit is outside selected checkpoint", 409
                        )
                    if request.product_session_id:
                        assert self.product_model_resolver is not None
                        self.product_model_resolver(request)
                    io.reconcile_edit_locked(operation["operation_id"])
            product_command_tree: str | None = None
            for operation in self.store.operations(task_id):
                if operation["kind"] not in {"baseline", "tool"} or operation["state"] not in {
                    "unknown",
                    "dispatched",
                }:
                    continue
                process_id = operation["operation_id"] + ":process"
                process = self.store.operation(task_id, process_id)
                if process is None or process["state"] != "known":
                    continue
                receipt = io.recovered_execution_receipt(
                    process_id,
                    pre_command_tree=bool(
                        request.product_session_id and operation["kind"] == "tool"
                    ),
                )
                origin = receipt.provenance
                grant = CapabilityGrant.model_validate(row["grant_scope"])
                if origin is None or origin.grant_hash != canonical_hash(
                    grant.model_dump(mode="json")
                ):
                    raise DomainError(
                        "RECOVERY_SCOPE_DENIED", "Original command grant differs", 403
                    )
                if (
                    origin.actor != self.actor
                    or origin.workspace_id != registration.workspace_id
                    or origin.repository != registration.repository
                ):
                    raise DomainError("RECOVERY_SCOPE_DENIED", "Command owner differs", 403)
                if request.product_session_id and operation["kind"] == "tool":
                    from mgar_v3.harness.product_runtime import (
                        ProductRuntimePlan,
                        recovered_product_command,
                    )

                    assert self.product_model_resolver is not None
                    self.product_model_resolver(request)
                    call = HarnessToolCall.model_validate(operation["intent"])
                    process_receipt = io._verified_receipt(process)
                    preview = recovered_product_command(
                        ProductRuntimePlan.model_validate(request.product_plan), call, receipt,
                        process_receipt, operation_id=operation["operation_id"],
                        task_kind=request.task_kind,
                    )
                    saved_receipt = preview.model_dump(mode="json")
                    self.store.reconcile_operation(
                        task_id, operation["operation_id"],
                        result_hash=io._receipt_hash(saved_receipt), receipt=saved_receipt,
                    )
                    if (
                        active_tool_prefix
                        and operation["operation_id"].startswith(active_tool_prefix)
                        and receipt.tree_hash == row["tree_hash"]
                        and "reconciled_interruption" not in process_receipt
                    ):
                        product_command_tree = process_receipt["tree_hash"]
                    continue
                probe_plan = (
                    self.evaluation_profile.public_probe if self.evaluation_profile else None
                )
                build_argv = (
                    self.evaluation_profile.public_build_argv if self.evaluation_profile else ()
                )
                if tuple(row["request"].get("trusted_build_argv", ())) != build_argv:
                    raise DomainError(
                        "RECOVERY_SCOPE_DENIED", "Frozen build command differs", 403
                    )
                allowed_argv = [tuple(row["request"]["trusted_test_argv"])]
                if build_argv:
                    allowed_argv.append(build_argv)
                if probe_plan is not None:
                    allowed_argv.append(probe_plan.argv)
                feedback_plan = (
                    self.evaluation_profile.independent_regression_plan
                    if self.evaluation_profile and self.evaluation_profile.public_feedback_policy
                    else None
                )
                if feedback_plan:
                    allowed_argv.extend(command.argv for command in feedback_plan.commands)
                if (
                    receipt.argv not in allowed_argv
                    or receipt.purpose != ("build" if receipt.argv == build_argv else "test")
                ):
                    raise DomainError(
                        "RECOVERY_SCOPE_DENIED", "Original command scope differs", 403
                    )
                if operation["kind"] == "baseline":
                    process_receipt = io._verified_receipt(process)
                    complete = not (process_receipt.get("dropped_bytes", 0)
                                    or process_receipt.get("handle", {}).get("log_truncated", True))
                    saved_receipt = _parsed_model_test_receipt(
                        receipt, complete=complete,
                    ).model_dump(mode="json")
                else:
                    call = HarnessToolCall.model_validate(operation["intent"])
                    if (
                        call.name != "run_tests"
                        or _resolve_test_command(
                            call.arguments, tuple(row["request"]["trusted_test_argv"]),
                            probe_plan, build_argv, feedback_plan,
                        )
                        != receipt.argv
                        or (call.arguments == {"command_id": "build"})
                        != (receipt.purpose == "build")
                    ):
                        raise DomainError("RECOVERY_SCOPE_DENIED", "Tool and command differ", 403)
                    process_receipt = io._verified_receipt(process)
                    complete = not (
                        process_receipt.get("handle", {}).get("log_truncated")
                        or process_receipt.get("dropped_bytes", 0)
                    )
                    raw_receipt = receipt
                    extra: dict[str, Any] = {}
                    if receipt.purpose == "build":
                        test_status, run, failed, skipped, detail = _build_status(receipt.exit_code)
                        if not complete or receipt.status != "passed" and test_status == "passed":
                            test_status, detail = (
                                "unverified", "Build log or execution is incomplete"
                            )
                        receipt = receipt.model_copy(update={
                            "tests_run": 0, "tests_failed": 0,
                            "status": receipt.status if test_status in {"passed", "failed"}
                            else "error",
                        })
                        extra = {"command_kind": "build", "test_runner": "build"}
                    elif probe_plan is not None and receipt.argv == probe_plan.argv:
                        assert self.evaluation_profile is not None
                        task_binding = TaskBinding(
                            task_id=task_id,
                            thread_id=row["thread_id"],
                            run_id=row["run_id"],
                            execution_epoch=row["execution_epoch"],
                            actor=self.actor,
                            repository=registration.repository,
                            base_commit=registration.base_commit,
                            workspace_id=registration.workspace_id,
                            acceptance_revision=self.evaluation_profile.acceptance_revision,
                            objective=request.objective.strip(),
                            task_kind=cast(Any, request.task_kind),
                            requirements=self._requirements(request.task_kind, allowed_argv[0]),
                        )
                        probe_result = normalize_probe_receipt(
                            probe_plan,
                            raw_receipt,
                            task=task_binding,
                            expected_tree=raw_receipt.tree_hash,
                            log_complete=complete,
                            log_limit_bytes=self.evaluation_profile.sandbox_log_bytes,
                        )
                        receipt = probe_result.execution
                        test_status = (
                            "passed"
                            if probe_result.outcome == "green"
                            else "failed"
                            if probe_result.outcome == "red"
                            else "unverified"
                        )
                        run, failed, skipped = receipt.tests_run, receipt.tests_failed, 0
                        detail = (
                            "Public probe "
                            + probe_result.outcome
                            + ": "
                            + (
                                probe_result.reason_code
                                or json.dumps([case.model_dump() for case in probe_result.cases])
                            )
                        )
                        extra = {
                            **_public_probe_source_data(raw_receipt, {
                                "process_operation_id": process_id,
                                "raw_log_hash": process_receipt["raw_log_hash"],
                            }),
                            "public_probe": probe_result.model_dump(
                                mode="json", exclude={"execution"}
                            ),
                        }
                    else:
                        test_status, run, failed, skipped, detail = _feedback_test_status(
                            feedback_plan, request.task_kind,
                            receipt.stdout,
                            receipt.exit_code,
                            argv=receipt.argv,
                            complete=complete,
                        )
                        if not complete:
                            test_status = "unverified"
                            detail = "Test log is incomplete; no verified pass"
                        receipt = receipt.model_copy(
                            update={
                                "tests_run": run,
                                "tests_failed": failed,
                                "status": receipt.status
                                if test_status == "passed"
                                else ("failed" if test_status == "failed" else "error"),
                            }
                        )
                    saved_receipt = HarnessToolResult(
                        call_id=call.call_id,
                        status="ok" if test_status == "passed" else "error",
                        content=_test_feedback(detail, receipt.stdout),
                        data={
                            "receipt": receipt.model_dump(mode="json"),
                            **extra,
                            "tests_run": run,
                            "tests_failed": failed,
                            "skipped": skipped,
                            "detail": detail,
                            "test_status": test_status,
                            "truncated": not complete,
                            "recovered": True,
                            **({"total_log_bytes":process_receipt.get("handle", {}).get(
                                    "log_bytes"),
                                "dropped_bytes":process_receipt.get("dropped_bytes", 0)}
                               if feedback_plan else {}),
                        },
                    ).model_dump(mode="json")
                content_hash = io._receipt_hash(saved_receipt)
                self.store.reconcile_operation(
                    task_id,
                    operation["operation_id"],
                    result_hash=content_hash,
                    receipt=saved_receipt,
                )
            # Complete only an outer patch receipt with a proven edit or deferral.
            for operation in self.store.operations(task_id):
                if operation["kind"] != "tool" or operation["state"] not in {
                    "unknown",
                    "dispatched",
                }:
                    continue
                call = HarnessToolCall.model_validate(operation["intent"])
                inner = self.store.operation(task_id, operation["operation_id"] + ":edit")
                deferred = self.store.operation(task_id, operation["operation_id"] + ":guidance")
                if (
                    call.name == "apply_edits" and inner is None and deferred
                    and deferred["kind"] == GUIDANCE_OPERATION and deferred["state"] == "known"
                    and active_tool_prefix
                    and operation["operation_id"].startswith(active_tool_prefix)
                ):
                    value = GuidanceDeferral.model_validate(io._verified_receipt(deferred))
                    grant = CapabilityGrant.model_validate(row["grant_scope"])
                    if any(not grant.can_read(path) for path in value.pending_paths):
                        raise DomainError(
                            "HARNESS_READ_DENIED", "Saved guidance scope differs", 403
                        )
                    deferred_result = value.result(call.call_id).model_dump(mode="json")
                    self.store.reconcile_operation(
                        task_id, operation["operation_id"],
                        result_hash=io._receipt_hash(deferred_result), receipt=deferred_result,
                    )
                    continue
                if call.name == "apply_edits" and inner and inner["state"] == "known":
                    edits = [WorkspaceEdit.model_validate(item) for item in call.arguments["edits"]]
                    snapshot = WorkspaceSnapshot.model_validate(inner["receipt"]["snapshot"])
                    result = HarnessToolResult(
                        call_id=call.call_id,
                        status="ok",
                        content="edits applied",
                        data={
                            "tree_hash": snapshot.tree_hash,
                            "changes": workspace.status(),
                            "protocol": protocol(edits),
                            "output_bytes": output_bytes(edits),
                        },
                    )
                    edit_receipt = result.model_dump(mode="json")
                    content_hash = self.engine.artifacts.put(
                        json.dumps(edit_receipt, sort_keys=True).encode()
                    )
                    self.store.reconcile_operation(
                        task_id,
                        operation["operation_id"],
                        result_hash=content_hash,
                        receipt=edit_receipt,
                    )
            if reconciliation_errors:
                raise reconciliation_errors[0]
            if stopping is not None and stopping.is_set():
                return self._project_status(task_id)
            row = self.store.claim(task_id)
            if workspace.snapshot().tree_hash != row["tree_hash"]:
                # Reconciled edits carry the only authorized tree transition.
                known_edits = [
                    op
                    for op in self.store.operations(task_id)
                    if op["kind"] == "edit"
                    and op["state"] == "known"
                    and active_tool_prefix
                    and op["operation_id"].startswith(active_tool_prefix)
                ]
                proven_tree = (
                    known_edits[-1]["receipt"]["tree_hash"]
                    if known_edits else product_command_tree
                )
                if (
                    proven_tree != workspace.snapshot().tree_hash
                ):
                    self.store.pause(task_id)
                    raise DomainError(
                        "RECOVERY_TREE_CONFLICT", "Workspace differs from recovery evidence", 409
                    )
                self.store.record_tree(
                    task_id, row["execution_epoch"], workspace.snapshot().tree_hash
                )
                row = self.store.get(task_id)
        runtime = _TaskRuntime(
            task_id=task_id,
            workspace=workspace,
            registration=registration,
            request=request,
            control=TaskControl(),
            inbox=EngineInbox(self.store, task_id, epoch=row["execution_epoch"]),
            events=EngineEvents(self.store, task_id, row["execution_epoch"]),
            durable_row=row,
        )
        self._register_runtime(runtime)
        if stopping is not None and stopping.is_set():
            self.store.pause(task_id)
            return self._project_status(task_id)
        if background:
            thread = threading.Thread(target=self._background, args=(runtime,), daemon=True)
            runtime.thread = thread
            thread.start()
            return self._project_status(task_id)
        try:
            return self._run(runtime)
        finally:
            self._sweep(workspace)

    def _grant(
        self,
        registration: Registration,
        kind: str,
        test_argv: tuple[str, ...],
        request: CodingTaskRequest | None = None,
    ) -> CapabilityGrant:
        profile = self.evaluation_profile
        commands: tuple[str, ...] = (_program(test_argv),) if test_argv else ()
        if request is not None and request.product_session_id:
            commands = tuple(
                dict.fromkeys(
                    _program(tuple(command["argv"]))
                    for command in request.product_plan.get("commands", {}).values()
                    if command.get("argv")
                )
            )
            if request.product_plan.get("start_argv"):
                commands = tuple(
                    dict.fromkeys(
                        (
                            *commands,
                            _program(tuple(request.product_plan["start_argv"])),
                        )
                    )
                )
        if profile is not None and profile.public_build_argv:
            commands = tuple(dict.fromkeys((*commands, _program(profile.public_build_argv))))
        if profile is not None and profile.public_probe is not None:
            commands = tuple(dict.fromkeys((*commands, _program(profile.public_probe.argv))))
        if profile is not None and profile.service_argv:
            commands = tuple(dict.fromkeys((*commands, _program(profile.service_argv))))
        return CapabilityGrant(
            actor=self.actor,
            workspace_id=registration.workspace_id,
            repository=registration.repository,
            read_paths=profile.read_paths if profile else ("",),
            write_paths=profile.write_paths
            if profile and kind in _WRITABLE
            else ("",)
            if kind in _WRITABLE
            else (),
            execute=kind in _WRITABLE and bool(commands),
            network=False,
            allowed_commands=commands if kind in _WRITABLE else (),
            max_seconds=profile.resources.deadline_seconds if profile else 120,
        )

    def _inherit_product_tree(self, workspace: TaskWorkspace, request: CodingTaskRequest) -> None:
        """Carry a terminal turn forward without rewriting its Run or source tree."""
        if self.store is None:
            raise DomainError("HARNESS_RECOVERY_UNAVAILABLE", "Engine is required", 409)
        old = self.store.get(request.product_previous_task_id)
        if (
            old["workspace_id"] != workspace.registration.workspace_id
            or old["request"].get("product_session_id") != request.product_session_id
            or old["public_status"] not in _TERMINAL
        ):
            raise DomainError("HARNESS_CONTINUATION_SCOPE", "Previous turn differs", 409)
        previous = self.registry.open_task_workspace(
            workspace.registration, request.product_previous_task_id,
            allow_unreconciled=True,
        )
        with workspace_lock(previous.root):
            snapshot, current = previous.snapshot(), workspace.snapshot()
            if snapshot.tree_hash != old["tree_hash"]:
                raise DomainError("HARNESS_CONTINUATION_CONFLICT",
                                  "The previous terminal tree changed", 409)
            baseline = previous._load_baseline()
            edits: list[WorkspaceEdit] = []
            deleted: list[str] = []
            for change in previous.status():
                path = change["path"]
                before, desired = current.files.get(path), snapshot.files.get(path)
                if before == desired:
                    continue  # The user already applied this exact earlier change.
                actual = sha256_text(before) if before is not None else None
                if actual != baseline.get(path):
                    raise DomainError(
                        "HARNESS_CONTINUATION_CONFLICT",
                        f"Original project changed at {path}; review before continuing", 409,
                    )
                if change["status"] == "deleted":
                    deleted.append(path)
                elif desired is None:
                    raise DomainError("HARNESS_CONTINUATION_UNSUPPORTED",
                                      "Binary or symlink changes need explicit review", 409)
                else:
                    edits.append(WorkspaceEdit(path=path, create=before is None,
                                               content=desired, expected_sha256=actual))
            if edits:
                workspace.apply(edits, expected_tree=current.tree_hash)
            for path in deleted:
                _safe_target(workspace.root, path).unlink()
            if deleted:
                workspace.manifest = workspace.manifest.model_copy(update={
                    "tree_hash": workspace.snapshot().tree_hash,
                })
                workspace.manifest = workspace.manifest.model_copy(update={
                    "manifest_hash": workspace.manifest.computed_hash(),
                })
                workspace._persist_manifest()

    @staticmethod
    def _prepare_product_permissions(workspace: TaskWorkspace) -> None:
        """Make only the authorized isolated copy writable by H04's non-root uid."""
        for folder, dirs, files in os.walk(workspace.root, followlinks=False):
            parent = Path(folder)
            dirs[:] = [
                name for name in dirs
                if not is_workspace_artifact(
                    (parent / name).relative_to(workspace.root).as_posix()
                ) and name.lower() != ".env" and not (parent / name).is_symlink()
            ]
            for path in (parent, *(parent / name for name in files)):
                relative = path.relative_to(workspace.root).as_posix()
                if (path.is_symlink() or is_workspace_artifact(relative)
                        or ".env" in relative.lower().split("/")):
                    continue
                with _open_read_target(workspace.root, relative) as (descriptor, _):
                    mode = os.fstat(descriptor).st_mode
                    os.fchmod(descriptor, mode | (0o777 if path.is_dir() else 0o666))

    def _background(self, runtime: _TaskRuntime) -> None:
        try:
            self._run(runtime)
        except DomainError as exc:
            if exc.code not in {"FENCED", "STALE_EPOCH", "RUN_NOT_ACTIVE"}:
                self._persist_failure(runtime, exc.code, str(exc), exc.status)
        except Exception as exc:  # pragma: no cover - defensive fail-closed
            self._persist_failure(
                runtime,
                "HARNESS_INTERNAL",
                f"{type(exc).__name__}: {exc}",
                500,
            )
        finally:
            self._sweep(runtime.workspace)

    def command_logs(self, task_id: str) -> list[dict[str, Any]]:
        if self.store is None:
            return []
        result = []
        for operation in self.store.operations(task_id):
            receipt = operation["receipt"]
            if operation["kind"] != "command" or operation["state"] != "known" or not receipt:
                continue
            content_hash = receipt.get("raw_log_hash")
            if not isinstance(content_hash, str) or not re.fullmatch(r"[0-9a-f]{64}", content_hash):
                continue
            result.append({
                "operation_id": operation["operation_id"],
                "sha256": content_hash,
                "truncated": bool(receipt.get("dropped_bytes")
                                  or receipt.get("handle", {}).get("log_truncated")),
                "url": f"/api/v3/harness/tasks/{task_id}/logs/{content_hash}",
            })
        return result

    def command_log(self, task_id: str, content_hash: str) -> bytes:
        if self.engine is None or not any(
            item["sha256"] == content_hash for item in self.command_logs(task_id)
        ):
            raise DomainError("HARNESS_LOG_UNKNOWN", "命令日志不存在。", 404)
        return cast(bytes, self.engine.artifacts.read(content_hash))

    def get_task(self, task_id: str) -> dict[str, Any]:
        if not TASK_ID_PATTERN.fullmatch(task_id):
            raise DomainError("HARNESS_TASK_UNKNOWN", "Task result is unavailable", 404)
        if self.store is not None:
            row = self.store.get(task_id)
            if row["result_hash"]:
                return load_result(
                    self.engine.artifacts, self.engine.artifacts.read(row["result_hash"]),
                    task_id, str(row["run_id"]),
                )
            # Project the same snapshot used for the result lookup. A later
            # snapshot could mark the request completed without its result.
            return self._project_status(task_id, row=row)
        path = self.runs / task_id / "result.json"
        if path.is_file():
            return cast(dict[str, Any], json.loads(path.read_text(encoding="utf-8")))
        status_path = self.runs / task_id / "status.json"
        if status_path.is_file():
            return self._project_status(task_id)
        raise DomainError("HARNESS_TASK_UNKNOWN", "Task result is unavailable", 404)

    def event_page(
        self,
        task_id: str,
        after: int = 0,
        limit: int = 200,
    ) -> dict[str, Any]:
        if not TASK_ID_PATTERN.fullmatch(task_id):
            raise DomainError("HARNESS_TASK_ID_INVALID", "Invalid task id", 400)
        if not self._task_exists(task_id):
            raise DomainError("HARNESS_TASK_UNKNOWN", "Task is unavailable", 404)
        sink = self._event_sink(task_id)
        # Terminal status is saved after the last event. Observe status before
        # looking for pending events so a concurrent finish can't hide them.
        status = self._current_status(task_id)
        records, next_cursor, has_more = sink.page(after_cursor=after, limit=limit)
        return {
            "stream_version": STREAM_VERSION,
            "task_id": task_id,
            "events": records,
            "next_cursor": next_cursor,
            "has_more": has_more,
            "status": status,
            "terminal": status in _TERMINAL,
        }

    def task_messages(self, task_id: str) -> dict[str, Any]:
        if not TASK_ID_PATTERN.fullmatch(task_id):
            raise DomainError("HARNESS_TASK_ID_INVALID", "Invalid task id", 400)
        if not self._task_exists(task_id):
            raise DomainError("HARNESS_TASK_UNKNOWN", "Task is unavailable", 404)
        inbox = self._inbox(task_id)
        return {
            "task_id": task_id,
            "revision": inbox.revision,
            "messages": inbox.messages(),
        }

    def post_message(
        self,
        task_id: str,
        *,
        kind: str,
        text: str,
        message_id: str = "",
        reply_to: str = "",
        client_id: str = "",
    ) -> dict[str, Any]:
        if not TASK_ID_PATTERN.fullmatch(task_id):
            raise DomainError("HARNESS_TASK_ID_INVALID", "Invalid task id", 400)
        if not self._task_exists(task_id):
            raise DomainError("HARNESS_TASK_UNKNOWN", "Task is unavailable", 404)
        if task_id.startswith("h15-"):
            raise DomainError(
                "RUNNER_FIXED_PLAN", "Runner verification uses a fixed local plan", 409,
            )
        status = self._current_status(task_id)
        if status in _TERMINAL:
            raise DomainError(
                "HARNESS_TASK_FINISHED",
                "The task already finished and cannot accept messages",
                409,
            )
        runtime = self._runtimes.get(task_id)
        if self.store is not None and runtime is None:
            record, duplicate = self._inbox(task_id).post(
                message_id=message_id or uuid4().hex,
                kind=cast(Any, kind),
                text=text,
                reply_to=reply_to,
                client_id=client_id,
            )
            return {
                "task_id": task_id,
                "message": record,
                "duplicate": duplicate,
                "events_cursor": self._event_sink(task_id).last_cursor,
            }
        runtime = self._active_runtime(task_id)
        record, duplicate = runtime.inbox.post(
            message_id=message_id or uuid4().hex,
            kind=cast(Any, kind),
            text=text,
            reply_to=reply_to,
            client_id=client_id,
        )
        if not duplicate:
            self._emit(
                runtime,
                "inbox_received",
                runtime.inbox.revision,
                {
                    "message_id": record["message_id"],
                    "kind": record["kind"],
                    "text": record["text"],
                    "reply_to": record["reply_to"],
                    "status": record["status"],
                },
            )
            self._status(
                runtime,
                inbox_revision=runtime.inbox.revision,
                last_message_id=record["message_id"],
            )
            runtime.control.wake()
        return {
            "task_id": task_id,
            "message": record,
            "duplicate": duplicate,
            "events_cursor": runtime.events.last_cursor,
        }

    def cancel_task(self, task_id: str, reason: str = "") -> dict[str, Any]:
        if not TASK_ID_PATTERN.fullmatch(task_id):
            raise DomainError("HARNESS_TASK_ID_INVALID", "Invalid task id", 400)
        if not self._task_exists(task_id):
            raise DomainError("HARNESS_TASK_UNKNOWN", "Task is unavailable", 404)
        if self.runners is not None and task_id.startswith("h15-"):
            return self.runners.control(task_id, "cancel")
        if self.store is not None:
            runtime = self._runtimes.get(task_id)
            if runtime is not None:
                runtime.control.request_cancel(reason)
            row = self.store.get(task_id)
            errors: list[str] = []
            with self.store.workspace_lock(row):
                if row["status"] not in {"succeeded", "failed", "cancelled"}:
                    row = self.store.pause(task_id)
                    registration = Registration.model_validate(row["registration"])
                    workspace = self.registry.open_task_workspace(
                        registration,
                        task_id,
                        allow_unreconciled=True,
                    )
                    io = RecoveryIO(
                        self.store,
                        task_id,
                        row["execution_epoch"],
                        workspace,
                        artifacts=self.engine.artifacts,
                        run_id=str(row["run_id"]),
                    )
                    lease = issue_lease(
                        mode="investigate",
                        actor=self.actor,
                        workspace_id=registration.workspace_id,
                        repository=registration.repository,
                        execution_epoch=row["execution_epoch"],
                        read_paths=("",),
                        write_paths=(),
                        allowed_commands=(),
                    )
                    broker = WorkspaceBoundBroker(workspace, lease, lock_path=self.image_lock)
                    for operation in self.store.operations(task_id):
                        if operation["kind"] == "command" and operation["state"] in {
                            "known",
                            "unknown",
                            "dispatched",
                        }:
                            try:
                                io.reconcile_command_locked(broker, operation["operation_id"])
                            except DomainError as exc:
                                errors.append(exc.code)
                row = self.store.cancel(task_id)
            return {
                "task_id": task_id,
                "status": self._task_public_status(row),
                "execution_epoch": row["execution_epoch"],
                "refund": "not_promised",
                "reconciliation_errors": errors,
            }
        status = self._current_status(task_id)
        if status in _TERMINAL:
            return {
                "task_id": task_id,
                "status": status,
                "cancel_receipt": {
                    "requested_at": "",
                    "observed_at": "",
                    "status": "too_late",
                    "refund": "not_promised",
                },
                "refund": "not_promised",
            }
        runtime = self._active_runtime(task_id)
        requested_at = runtime.control.request_cancel(reason)
        if not runtime.cancel_event_emitted:
            runtime.cancel_event_emitted = True
            self._emit(
                runtime,
                "cancel_requested",
                runtime.inbox.revision,
                {
                    "requested_at": requested_at,
                    "reason": reason,
                    "refund": "not_promised",
                    "note": (
                        "The UI receipt only confirms delivery; the actual stop "
                        "is judged by the runner/provider receipt."
                    ),
                },
            )
            self._status(
                runtime,
                status="cancelling",
                cancel_requested_at=requested_at,
                cancel_reason=reason,
            )
        return {
            "task_id": task_id,
            "status": "cancelling",
            "requested_at": requested_at,
            "events_cursor": runtime.events.last_cursor,
            "refund": "not_promised",
            "note": "Actual stop is judged by the runner/provider receipt.",
        }

    def pause_task(self, task_id: str) -> dict[str, Any]:
        if not TASK_ID_PATTERN.fullmatch(task_id):
            raise DomainError("HARNESS_TASK_ID_INVALID", "Invalid task id", 400)
        if not self._task_exists(task_id):
            raise DomainError("HARNESS_TASK_UNKNOWN", "Task is unavailable", 404)
        if self.runners is not None and task_id.startswith("h15-"):
            return self.runners.control(task_id, "pause")
        if self.store is not None and task_id not in self._runtimes:
            row = self.store.pause(task_id)
            return {"task_id": task_id, "status": row["public_status"]}
        status = self._current_status(task_id)
        if status in _TERMINAL:
            raise DomainError("HARNESS_TASK_FINISHED", "The task already finished", 409)
        runtime = self._active_runtime(task_id)
        requested_at = runtime.control.request_pause()
        if not runtime.pause_event_emitted:
            runtime.pause_event_emitted = True
            self._emit(
                runtime,
                "pause_requested",
                runtime.inbox.revision,
                {"requested_at": requested_at},
            )
            self._status(runtime, status="pausing", pause_requested_at=requested_at)
        return {
            "task_id": task_id,
            "status": "pausing",
            "requested_at": requested_at,
            "events_cursor": runtime.events.last_cursor,
        }

    def resume_task(self, task_id: str) -> dict[str, Any]:
        if not TASK_ID_PATTERN.fullmatch(task_id):
            raise DomainError("HARNESS_TASK_ID_INVALID", "Invalid task id", 400)
        if not self._task_exists(task_id):
            raise DomainError("HARNESS_TASK_UNKNOWN", "Task is unavailable", 404)
        if self.store is not None and task_id not in self._runtimes:
            if self.store.get(task_id)["managed_workspace_id"] is not None:
                self.store.schedule_resume(task_id)
                return {"task_id": task_id, "status": "queued"}
            return self.recover_task(task_id, background=True)
        status = self._current_status(task_id)
        if status in _TERMINAL:
            raise DomainError("HARNESS_TASK_FINISHED", "The task already finished", 409)
        runtime = self._active_runtime(task_id)
        runtime.control.request_resume()
        self._emit(
            runtime,
            "resume_requested",
            runtime.inbox.revision,
            {"resumed_at": datetime.now(UTC).isoformat()},
        )
        self._status(runtime, status="running", pause_requested_at="")
        return {
            "task_id": task_id,
            "status": "running",
            "events_cursor": runtime.events.last_cursor,
        }

    def join_task(self, task_id: str, timeout: float = 10.0) -> dict[str, Any]:
        runtime = self._runtimes.get(task_id)
        if runtime is not None and runtime.thread is not None:
            runtime.thread.join(timeout)
        return self.get_task(task_id)

    def _active_runtime(self, task_id: str) -> _TaskRuntime:
        if not TASK_ID_PATTERN.fullmatch(task_id):
            raise DomainError("HARNESS_TASK_ID_INVALID", "Invalid task id", 400)
        runtime = self._runtimes.get(task_id)
        if runtime is None:
            if self._task_exists(task_id):
                raise DomainError(
                    "HARNESS_TASK_ORPHANED",
                    "This task is not active in the current process",
                    409,
                )
            raise DomainError("HARNESS_TASK_UNKNOWN", "Task is unavailable", 404)
        return runtime

    def _task_exists(self, task_id: str) -> bool:
        if self.store is not None:
            try:
                self.store.get(task_id)
                return True
            except DomainError:
                return False
        directory = self.runs / task_id
        return (
            (directory / "result.json").is_file()
            or (directory / "status.json").is_file()
            or (directory / "events.jsonl").is_file()
        )

    def _event_sink(self, task_id: str) -> Any:
        runtime = self._runtimes.get(task_id)
        if runtime is not None:
            return runtime.events
        if self.store is not None:
            row = self.store.get(task_id)
            return cast(Any, EngineEvents(self.store, task_id, row["execution_epoch"]))
        return PersistentEventSink(self.runs / task_id / "events.jsonl")

    def _inbox(self, task_id: str) -> Any:
        runtime = self._runtimes.get(task_id)
        if runtime is not None:
            return runtime.inbox
        if self.store is not None:
            return EngineInbox(self.store, task_id)
        return TaskInbox(self.runs / task_id / "inbox.json")

    @staticmethod
    def _task_public_status(row: dict[str, Any]) -> str:
        # Ask lives in the original PG inbox. status.json is only a cache and
        # cannot override epoch, cancellation, paused or terminal ledger facts.
        status = str(row["public_status"])
        if status not in _TERMINAL:
            run_status = str(row["status"])
            if run_status in {"failed", "cancelled", "paused"}:
                return run_status
            if run_status == "succeeded":
                return "incomplete"
        if status == "running" and any(
            message.get("kind") == "question" and message.get("status") == "waiting"
            for message in row["inbox"].get("messages", [])
        ):
            return "waiting_input"
        return status

    def _current_status(self, task_id: str) -> str:
        if self.store is not None:
            return self._task_public_status(self.store.get(task_id))
        result_path = self.runs / task_id / "result.json"
        if result_path.is_file():
            try:
                payload = json.loads(result_path.read_text(encoding="utf-8"))
                return str(payload.get("status", "completed"))
            except (OSError, ValueError):
                return "unknown"
        status_path = self.runs / task_id / "status.json"
        if status_path.is_file():
            try:
                payload = json.loads(status_path.read_text(encoding="utf-8"))
                return str(payload.get("status", "unknown"))
            except (OSError, ValueError):
                return "unknown"
        return "unknown"

    def _project_status(
        self, task_id: str, *, row: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        status_path = self.runs / task_id / "status.json"
        payload: dict[str, Any] = {}
        if status_path.is_file():
            try:
                payload = json.loads(status_path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                payload = {}
        if self.store is not None:
            if row is None:
                row = self.store.get(task_id)
            status = self._task_public_status(row)
            payload.update(
                status=status,
                run_id=str(row["run_id"]),
                execution_epoch=row["execution_epoch"],
                recovery_required=row["status"] == "paused",
            )
            site = row["request"].get("execution_site")
            if site is not None:
                payload["execution_site"] = site
            if (site is not None or status in _TERMINAL) and row["reason_code"]:
                payload["reason"] = row["reason_code"]
            # The durable inbox can precede status.json. Project the question
            # from the same PG snapshot as waiting_input, never a missing or
            # stale cache ID that prevents the caller from answering.
            payload.update(question_id="", question="")
            if payload["status"] == "waiting_input":
                pending = next((
                    message for message in row["inbox"].get("messages", [])
                    if message.get("kind") == "question"
                    and message.get("status") == "waiting"
                ), None)
                if pending is not None:
                    payload.update(question_id=pending["message_id"], question=pending["text"])
        runtime = self._runtimes.get(task_id)
        inbox = self._inbox(task_id)
        sink = self._event_sink(task_id)
        payload.update(
            {
                "task_id": task_id,
                "stream_version": STREAM_VERSION,
                "events_cursor": sink.last_cursor,
                "progress": sink.summary(),
                "messages": inbox.messages(),
                "inbox_revision": inbox.revision,
                "events_url": f"/api/v3/harness/tasks/{task_id}/events",
                "messages_url": f"/api/v3/harness/tasks/{task_id}/messages",
                "cancel_requested_at": (runtime.control.cancel_requested_at if runtime else ""),
                "pause_requested": (runtime.control.pause_requested if runtime else False),
                "background": bool(runtime and runtime.thread is not None),
            }
        )
        return payload

    def _emit(
        self,
        runtime: _TaskRuntime,
        kind: str,
        revision: int,
        payload: dict[str, Any],
    ) -> Any:
        row = runtime.durable_row
        run_id = (
            runtime.task.run_id
            if runtime.task is not None
            else (row["run_id"] if row else UUID(int=0))
        )
        epoch = (
            runtime.task.execution_epoch if runtime.task else (row["execution_epoch"] if row else 1)
        )
        return runtime.events.emit(
            EventDraft(
                run_id=run_id,
                operation_id=f"{runtime.task_id}:{kind}",
                execution_epoch=epoch,
                revision=max(0, int(revision)),
                kind=kind,
                payload=dict(payload),
            )
        )

    def _status(self, runtime: _TaskRuntime, **fields: Any) -> None:
        path = self.runs / runtime.task_id / "status.json"
        guard = workspace_lock(runtime.workspace.root) if self.store is not None else nullcontext()
        with guard, self._lock:
            if self.store is not None and runtime.task is not None:
                self.store.guard(runtime.task_id, runtime.task.execution_epoch)
            payload: dict[str, Any] = {}
            if path.is_file():
                try:
                    payload = json.loads(path.read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    payload = {}
            payload.update(fields)
            payload["task_id"] = runtime.task_id
            payload["updated_at"] = datetime.now(UTC).isoformat()
            payload["stream_version"] = STREAM_VERSION
            payload["events_cursor"] = runtime.events.last_cursor
            self._write_json(path, payload)

    def _control_event(self, runtime: _TaskRuntime, kind: str) -> None:
        if kind == "paused":
            if runtime.paused_event:
                return
            runtime.paused_event = True
            if self.store is not None and runtime.task is not None:
                self.store.record_live_pause(
                    runtime.task_id, runtime.task.execution_epoch, paused=True
                )
            self._emit(
                runtime,
                "paused",
                runtime.inbox.revision,
                {
                    "requested_at": runtime.control.pause_requested_at,
                    "observed_at": datetime.now(UTC).isoformat(),
                },
            )
            self._status(runtime, status="paused")
            return
        if kind == "resumed":
            runtime.paused_event = False
            runtime.pause_event_emitted = False
            if self.store is not None and runtime.task is not None:
                self.store.record_live_pause(
                    runtime.task_id, runtime.task.execution_epoch, paused=False
                )
            self._emit(
                runtime,
                "resumed",
                runtime.inbox.revision,
                {"observed_at": datetime.now(UTC).isoformat()},
            )
            self._status(runtime, status="running", pause_requested_at="")
            return
        if kind == "cancel_observed":
            if runtime.cancel_observed_event:
                return
            runtime.cancel_observed_event = True
            self._emit(
                runtime,
                "cancel_observed",
                runtime.inbox.revision,
                {
                    "requested_at": runtime.control.cancel_requested_at,
                    "observed_at": datetime.now(UTC).isoformat(),
                    "refund": "not_promised",
                },
            )
            self._status(
                runtime,
                status="cancelling",
                cancel_observed_at=datetime.now(UTC).isoformat(),
            )

    def _checkpoint(self, runtime: _TaskRuntime) -> Callable[[str, int], SafePoint]:
        def checkpoint(stage: str, revision: int) -> SafePoint:
            runtime.control.checkpoint(stage, revision)
            objective = (
                runtime.task.objective
                if runtime.task is not None
                else runtime.request.objective.strip()
            )
            instructions: list[str] = []
            current = revision
            for message in runtime.inbox.pending_instructions():
                if message["message_id"] in runtime.delivered_instructions:
                    continue
                current += 1
                record = runtime.inbox.mark_applied(message["message_id"], current)
                runtime.delivered_instructions.add(message["message_id"])
                text = str(record["text"] if record is not None else message["text"])
                instructions.append(text)
                objective = f"{objective}\n\nAdditional instruction: {text}"
                self._emit(
                    runtime,
                    "instruction_applied",
                    current,
                    {
                        "message_id": message["message_id"],
                        "text": text,
                        "revision": current,
                        "stage": stage,
                    },
                )
            if instructions and runtime.task is not None:
                runtime.task = runtime.task.model_copy(update={"objective": objective})
            return SafePoint(
                instructions=tuple(instructions),
                objective=objective,
                revision=runtime.inbox.revision,
            )

        return checkpoint

    def list_tasks(self) -> list[dict[str, Any]]:
        if self.store is not None:
            return [
                {
                    "task_id": row["task_id"],
                    "status": row["public_status"],
                    "task_kind": row["request"].get("task_kind", ""),
                    "objective": row["request"].get("objective", ""),
                    "run_id": str(row["run_id"]),
                    "execution_epoch": row["execution_epoch"],
                }
                for row in self.store.tasks()
                if row["actor"] == self.actor
            ][-50:][::-1]
        results: list[dict[str, Any]] = []
        for directory in sorted(self.runs.iterdir()) if self.runs.is_dir() else []:
            if not directory.is_dir():
                continue
            path = directory / "result.json"
            if not path.is_file():
                path = directory / "status.json"
            if not path.is_file():
                continue
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            results.append(
                {
                    "task_id": payload.get("task_id", directory.name),
                    "status": payload.get("status", ""),
                    "task_kind": payload.get("task_kind", ""),
                    "objective": payload.get("objective", ""),
                    "summary": payload.get("summary", ""),
                    "created_at": payload.get("created_at", ""),
                    "live_model": payload.get("live_model", False),
                }
            )
        return list(reversed(results[-50:]))

    def patch_path(self, task_id: str) -> Path:
        result = self.get_task(task_id)
        candidate = Path(str(result.get("patch_path", "")))
        if not candidate.is_file():
            raise DomainError("HARNESS_PATCH_UNAVAILABLE", "No patch was produced", 404)
        return candidate

    # -- run ------------------------------------------------------------

    def _run(self, runtime: _TaskRuntime) -> dict[str, Any]:
        task_id = runtime.task_id
        workspace = runtime.workspace
        registration = runtime.registration
        request = runtime.request
        kind = request.task_kind
        profile = self.evaluation_profile
        row = runtime.durable_row
        general_dispatch = None
        if row is not None and "general_dispatch" in row["request"]:
            from mgar_v3.general_agent.coding_executor_contract import CodingDispatch

            general_dispatch = CodingDispatch.model_validate(row["request"]["general_dispatch"])
        general_service = general_dispatch.service_plan if general_dispatch else None
        max_seconds = (
            general_dispatch.limits.max_seconds
            if general_dispatch
            else profile.resources.deadline_seconds
            if profile
            else request.product_limits.get("max_seconds", DEFAULT_MAX_SECONDS)
        )
        max_output = profile.resources.max_output_tokens if profile else DEFAULT_OUTPUT_TOKENS
        capabilities = self.model_capabilities
        live_key = self._live_api_key
        if request.product_session_id:
            if self.product_model_resolver is None:
                raise DomainError(
                    "HARNESS_PRODUCT_UNAVAILABLE", "Product authority is missing", 409
                )
            live_key, capabilities = self.product_model_resolver(request)
            max_output = min(max_output, capabilities.max_output_tokens)
        from mgar_v3.context.policy import ContextPolicy

        context_policy = (
            ContextPolicy.model_validate(request.product_context_policy)
            if request.product_session_id
            else None
        )
        if context_policy is not None:
            # Adapter capabilities are the configured effective window; the
            # original API declaration remains frozen in the product request.
            capabilities = capabilities.model_copy(
                update={
                    "context_tokens": context_policy.window(
                        capabilities.context_tokens, max_output
                    ),
                }
            )
        context_tokens = min(
            profile.resources.max_context_tokens - max_output
            if profile
            else (
                context_policy.input_limit(capabilities.context_tokens, max_output) // 3
                if context_policy is not None
                else 32000
            ),
            capabilities.context_tokens - max_output - 1024,
            262144,
        )
        if self.store is not None:
            if row is None or row["status"] == "queued":
                row = self.store.begin(task_id)
            runtime.durable_row = row
            runtime.events = EngineEvents(self.store, task_id, row["execution_epoch"])
            runtime.inbox = EngineInbox(self.store, task_id, epoch=row["execution_epoch"])
        epoch = int(row["execution_epoch"]) if row is not None else 1
        self._status(
            runtime,
            status="running",
            started_at=datetime.now(UTC).isoformat(),
        )
        live = request.mode == "live"
        if live and not (request.product_session_id and live_key or self._allow_paid and live_key):
            raise DomainError(
                "HARNESS_LIVE_MODEL_UNAVAILABLE",
                "A paid model call needs an approved credential and budget",
                409,
            )
        test_argv = (
            tuple(row["request"]["trusted_test_argv"])
            if row
            else self._test_command(
                workspace,
                request,
                kind,
            )
        )
        requirements = self._requirements(kind, test_argv)
        if request.product_session_id and not test_argv and kind not in {"inspect", "run_app"}:
            build = request.product_plan.get("commands", {}).get("build", {}).get("argv", ())
            if not build:
                raise DomainError(
                    "HARNESS_VERIFICATION_REQUIRED",
                    "No trusted test or build command was discovered",
                    409,
                )
            requirements = (
                VerificationRequirement(
                    requirement_id="build",
                    kind="build",
                    command_contains=tuple(build),
                ),
            )
        approved = kind in _WRITABLE
        grant = (
            CapabilityGrant.model_validate(row["grant_scope"])
            if row
            else self._grant(registration, kind, test_argv, request)
        )
        lease = issue_lease(
            mode="modify_test" if approved else "investigate",
            actor=self.actor,
            workspace_id=registration.workspace_id,
            repository=registration.repository,
            execution_epoch=epoch,
            read_paths=grant.read_paths,
            write_paths=grant.write_paths,
            allowed_commands=grant.allowed_commands,
            timeout_seconds=min(max_seconds, 600) if profile or general_dispatch else 120,
            max_output_bytes=profile.sandbox_log_bytes
            if profile
            else 1_048_576
            if request.product_session_id
            else 65_536,
            nofile_limit=profile.sandbox_nofile
            if profile
            else 1024
            if request.product_session_id
            else 128,
            memory_mb=profile.sandbox_memory_mb
            if profile
            else 1024
            if request.product_session_id
            else 256,
            scratch_mb=profile.sandbox_scratch_mb if profile else 64,
            offline_network=profile.sandbox_offline_network if profile else "none",
            max_calls=max(200, profile.resources.max_tool_calls + 32) if profile else 200,
            approved_by=self.actor if approved else "",
            approval_ref=f"web-approval-{task_id}" if approved else "",
        )
        task = TaskBinding(
            task_id=task_id,
            thread_id=row["thread_id"] if row else UUID(int=0),
            run_id=row["run_id"] if row else UUID(int=0),
            execution_epoch=epoch,
            actor=self.actor,
            repository=registration.repository,
            base_commit=registration.base_commit,
            workspace_id=registration.workspace_id,
            acceptance_revision=profile.acceptance_revision if profile else "web-v1",
            objective=request.objective.strip(),
            task_kind=cast(Any, kind),
            requirements=requirements,
            public_requirements=profile.public_requirements if profile else (),
            acceptance_baseline_commit=profile.acceptance_baseline_commit if profile else None,
            acceptance_scope=AcceptanceScope(
                workspace_id=registration.workspace_id, repository=registration.repository,
                read_paths=grant.read_paths, write_paths=grant.write_paths,
                execute=grant.execute, network=grant.network,
                allowed_commands=grant.allowed_commands,
            ),
        )
        runtime.task = task
        runtime.lease = lease
        events = runtime.events
        self._emit(
            runtime,
            "approval_recorded",
            0,
            {
                "approval_ref": lease.approval_ref,
                "approved_by": lease.approved_by,
                "mode": lease.mode,
                "task_kind": kind,
                "write_paths": list(lease.write_paths),
            },
        )
        live = request.mode == "live"
        model: Any
        registered_model: ModelAdapter | None = None
        if live:
            assert live_key is not None
            adapter: Any = DeepSeekChatAdapter
            if request.product_session_id:
                from mgar_v3.harness.product_models import ProductDeepSeekChatAdapter

                adapter = ProductDeepSeekChatAdapter
            model = adapter(
                api_key=live_key,
                capabilities=capabilities,
                max_attempts=1 if profile or request.product_expert_max_tokens else 2,
                artifacts=self.engine.artifacts if self.engine is not None else None,
            )
            if request.product_session_id and self.product_adapter_factory is not None:
                registered_model = self.product_adapter_factory(request, live_key, capabilities)
                if registered_model is not None:
                    model = registered_model
            model_id = capabilities.model_id
            price_version = capabilities.price.version
            max_cost = (
                profile.resources.max_cost_micros
                if profile
                else request.product_limits.get("max_cost_micros", LIVE_MAX_COST_MICROS)
            )
            input_bound = (
                min(capabilities.context_tokens, profile.resources.max_context_tokens)
                if profile
                else 0
            )
            reservation = (
                (
                    (
                        input_bound * capabilities.price.input_micros_per_million
                        + max_output * capabilities.price.output_micros_per_million
                        + 999999
                    )
                    // 1000000
                )
                if profile
                else 50_000
            )
        else:
            if kind in _WRITABLE and general_service is None:
                if not request.target_path or not request.old_text:
                    raise DomainError(
                        "HARNESS_DETERMINISTIC_HINT_REQUIRED",
                        "The deterministic adapter needs target_path and old_text",
                        422,
                    )
            model = DeterministicCodingModel(
                task_kind="inspect" if general_service is not None else kind,
                target_path=request.target_path,
                old_text=request.old_text,
                new_text=request.new_text,
                test_argv=test_argv,
                occurrence=request.occurrence,
            )
            model_id = DETERMINISTIC_MODEL_ID
            price_version = DETERMINISTIC_PRICE_VERSION
            max_cost = 1_000_000
            reservation = 0 if general_dispatch else DEFAULT_RESERVATION_MICROS
        metered = MeteredModel(model, live=live)
        if self.model_factory is not None:
            metered = MeteredModel(self.model_factory(request), live=live)
        guarded_model: Any = metered
        if request.product_session_id:
            resolver = self.product_model_resolver

            class ProductModelGuard:
                def set_output_observer(self, observer: Any) -> None:
                    metered.set_output_observer(observer)

                def complete(
                    self, model_request: ModelRequest, cancellation: CancellationToken
                ) -> ModelResponse:
                    assert resolver is not None
                    if not request.product_managed:
                        resolver(request)
                    # Managed requests are admitted while their prepared
                    # operation moves to dispatched on the Engine connection.
                    from mgar_v3.harness.product_models import product_request_reservation

                    if model_request.reservation_micros < product_request_reservation(
                        model_request, capabilities,
                    ):
                        raise DomainError(
                            "MODEL_RESERVATION_REQUIRED", "Model reservation is insufficient", 403,
                        )
                    result = metered.complete(model_request, cancellation)
                    if registered_model is None or not result.usage.known:
                        return result
                    usage = result.usage
                    priced = _price_usage(
                        usage.input_tokens, usage.output_tokens, model_request, capabilities,
                    )
                    metered.cost_micros += priced.cost_micros - usage.cost_micros
                    return result.model_copy(update={
                        "provider": capabilities.provider, "model_id": capabilities.model_id,
                        "price_version": capabilities.price.version,
                        "usage": priced.model_copy(update={
                            "reasoning_tokens": usage.reasoning_tokens,
                        }),
                    })

            guarded_model = ProductModelGuard()
        authority: CapabilityLease | CapabilityAuthority = lease
        io: RecoveryIO | None = None
        if self.store is not None:
            io = RecoveryIO(
                self.store,
                task_id,
                epoch,
                workspace,
                artifacts=self.engine.artifacts,
                run_id=str(task.run_id),
                fault=lambda point: self.fault_hook(point, {"task_id": task_id}),
            )

            def current_lease() -> CapabilityLease:
                assert self.store is not None
                self.store.guard(task_id, epoch)
                if request.product_session_id:
                    assert self.product_model_resolver is not None
                    self.product_model_resolver(request)
                return lease

            authority = CapabilityAuthority(lease_source=current_lease, budget_check=lambda: True)
        broker = WorkspaceBoundBroker(
            workspace,
            authority,
            lock_path=Path(request.product_plan.get("image_lock") or self.image_lock)
            if request.product_session_id
            else self.image_lock,
            allow_internal_write_symlinks=bool(request.product_session_id),
            public_probe=profile.public_probe if profile else None,
            public_probe_root=self.public_probe_root,
        )
        broker.recovery_io = io
        broker._task_binding = task
        session = (
            CommandSession(broker, recovery_io=io) if io is not None else CommandSession(broker)
        )
        registry: Any = CodingToolRegistry
        product_kwargs: dict[str, Any] = {}
        if request.product_session_id:
            from mgar_v3.harness.product_runtime import (
                ProductCodingToolRegistry,
                ProductRuntimePlan,
            )

            registry = ProductCodingToolRegistry
            product_kwargs["runtime_plan"] = ProductRuntimePlan.model_validate(request.product_plan)
        tools = registry(
            workspace,
            broker,
            session,
            test_argv,
            kind=kind,
            build_argv=profile.public_build_argv if profile else (),
            recovery_io=io,
            command_timeout_seconds=min(max_seconds, 1800) if profile else 120,
            **product_kwargs,
            **({"feedback_plan": profile.independent_regression_plan}
               if profile and profile.public_feedback_policy else {}),
        )
        guidance = None
        if io is not None and request.product_session_id and request.product_guidance:
            guidance = GuidanceDelivery(
                workspace, io, broker.authority, task, grant, request.product_guidance,
            )
            tools.guidance = guidance
        broker.context_tools = tools.context_tools
        if io is not None:
            tools.context_tools.restore_known(
                ToolContext(
                    task=task,
                    grant=grant,
                    operation_id=f"{task_id}:restore-context",
                    workspace_tree=workspace.snapshot().tree_hash,
                ),
                io,
            )
        budgets: Any = InMemoryBudgetLedger(
            BudgetLimits(
                max_model_calls=12,
                max_tool_calls=48,
                max_cost_micros=max_cost,
                max_seconds=DEFAULT_MAX_SECONDS,
            )
        )
        from mgar_v3.harness.product_models import product_request_reservation

        model_adapter: Any = guarded_model
        continuation = None
        if self.store is not None:
            budgets = PersistentBudgetLedger(self.store, task_id, epoch)
            model_adapter = DurableModel(guarded_model, self.store, task_id, epoch, self.fault_hook)
            if request.product_session_id and request.product_expert_enabled:
                assert io is not None and self.engine is not None
                settings = self.engine.settings
                tools.expert = RepositoryConsultation(
                    tools.context_tools, io, budgets, model_adapter,
                    lambda model_request: product_request_reservation(model_request, capabilities),
                    model_id=model_id, price_version=price_version,
                    enabled=lambda: settings.harness_experts_enabled,
                )
            continuation = DurableContinuation(
                self.store, task_id, epoch, workspace, self.fault_hook
            )
            assert row is not None
            if not row["tree_hash"]:
                self.store.record_tree(
                    task_id,
                    epoch,
                    workspace.snapshot().tree_hash,
                    manifest_hash=workspace.manifest.manifest_hash,
                )
        saved = continuation.load() if continuation is not None else None
        lifecycle_policy = profile.lifecycle_policy if profile else LifecyclePolicy()
        if saved is not None and profile is None:
            frozen_lifecycle = saved["plan"].get("lifecycle_policy")
            lifecycle_policy = (
                LifecyclePolicy.model_validate(frozen_lifecycle) if frozen_lifecycle else None
            )
        app_cwd = request.product_plan.get("root", ".") if request.product_session_id else "."
        start_argv = (
            general_service.argv if general_service else
            profile.service_argv if profile else tuple(request.product_plan.get("start_argv", ()))
        )
        application = (
            SessionApplication(
                session,
                task,
                budgets,
                io,
                cwd=request.product_plan.get("root", ".") if request.product_session_id else ".",
                policy=lifecycle_policy,
                artifacts=self.engine.artifacts if self.engine else tools.context_tools.artifacts,
            )
            if kind == "run_app"
            else None
        )
        loop = CodingLoop(
            model=model_adapter,
            tools=tools,
            workspace=CodingWorkspace(workspace),
            broker=broker,
            context=LongContextManager(
                artifacts=tools.context_tools.artifacts,
                state_path=self.runs / task_id / "context.json",
                max_tokens=context_tokens,
                policy=context_policy,
            ),
            verifier=LocalVerifier(),
            events=events,
            budgets=budgets,
            interaction=_TaskInteraction(self, runtime),
            model_id=model_id,
            price_version=price_version,
            max_output_tokens=max_output,
            output_policy=profile.output_policy if profile else OutputPolicy(),
            tool_error_policy=profile.tool_error_policy if profile else ToolErrorPolicy(),
            public_feedback_policy=profile.public_feedback_policy if profile else None,
            lifecycle_policy=lifecycle_policy,
            entrypoints=EntryPointChecker(
                workspace, session, task, budgets, io, lifecycle_policy,
                test_argv or start_argv, cwd=app_cwd, service_argv=start_argv,
            ) if lifecycle_policy is not None and kind != "inspect" else None,
            output_artifacts=self.engine.artifacts if self.engine is not None
            else tools.context_tools.artifacts,
            reservation_micros=reservation,
            max_seconds=max_seconds,
            max_repair_rounds=profile.resources.max_repair_rounds if profile else None,
            max_context_tokens=profile.resources.max_context_tokens if profile else None,
            command_timeout_seconds=min(max_seconds, 1800) if profile or general_dispatch else 120,
            continuation=continuation,
            application=application,
            project_context=request.product_context,
            project_guidance=guidance,
            baseline_cwd=request.product_plan.get("root", ".")
            if request.product_session_id
            else ".",
            model_quote=(
                lambda model_request: product_request_reservation(model_request, capabilities)
            )
            if request.product_session_id
            else None,
            before_model_request=self.before_model_request,
        )
        if saved is not None:
            recovered = TaskBinding.model_validate(saved["state"]["task"])
            expected_goal = runtime.request.objective.strip()
            expected_public = list(task.public_requirements)
            for message in runtime.inbox.messages():
                if message.get("kind") == "instruction" and message.get("status") == "applied":
                    expected_goal += "\n\nAdditional instruction: " + message["text"]
                    if message["text"] not in expected_public:
                        expected_public.append(message["text"])
            immutable = {"execution_epoch", "objective", "public_requirements"}
            if (
                recovered.model_dump(exclude=immutable) != task.model_dump(exclude=immutable)
                or recovered.objective != expected_goal
                or recovered.public_requirements != tuple(expected_public)
            ):
                raise DomainError(
                    "ACCEPTANCE_RECOVERY_MISMATCH",
                    "Checkpoint differs from accepted task/inbox facts; reconcile before execution",
                    409,
                )
            task = recovered.model_copy(update={"execution_epoch": epoch})
            runtime.task = task
        from mgar_v3.harness.coding_loop import TaskPlan

        extensions = None
        try:
            if request.product_session_id and self.product_extension_factory is not None:
                extensions = self.product_extension_factory(runtime, tools)
                loop.tools = extensions
            tool_specs = tuple(loop.tools.specs())
            plan = (
                TaskPlan.model_validate(saved["plan"])
                if saved
                else loop.plan(
                    task,
                    baseline_command=test_argv if kind in _WRITABLE else (),
                    service_plan=(
                        general_service
                        if general_service is not None
                        else ServicePlan(
                            argv=profile.service_argv,
                            mode=profile.service_mode,
                            port=profile.service_port,
                            path=profile.service_path,
                            timeout_seconds=profile.service_timeout_seconds,
                            routes=profile.lifecycle_routes,
                        )
                        if profile and profile.service_argv
                        else ServicePlan(
                            argv=tuple(request.product_plan["start_argv"]),
                            port=request.product_plan["readiness"]["port"],
                            path=request.product_plan["readiness"].get("path", "/"),
                            routes=public_routes(task.objective),
                        )
                        if request.product_session_id
                        and request.product_plan.get("start_argv")
                        and request.product_plan.get("readiness")
                        else None
                    ),
                )
            )
            result = loop.run(
                task,
                grant,
                plan,
                cancellation=runtime.control.token,
                checkpoint=self._checkpoint(runtime),
            )
        finally:
            try:
                if extensions is not None and hasattr(extensions, "close"):
                    extensions.close()
            finally:
                session.close()
        runtime.finished_at = datetime.now(UTC).isoformat()
        candidate_status = result.status
        independent_gate: dict[str, Any] | None = None
        if io is not None:
            assert self.store is not None
            with io.locked():
                if workspace.snapshot().tree_hash != result.tree_after:
                    raise DomainError("HARNESS_TERMINAL_TREE_UNSTABLE", "Final tree changed", 409)
                self.store.record_tree(
                    task_id,
                    epoch,
                    result.tree_after,
                    manifest_hash=workspace.manifest.manifest_hash,
                )
                if profile is not None:
                    self.store.seal_evaluation(task_id, epoch, result.tree_after)
                    if self.final_gate is None:
                        independent_gate = {"status": "NOT_RUN", "reason": "No independent judge"}
                    else:
                        try:
                            independent_gate = self.final_gate(
                                workspace,
                                result.model_dump(mode="json"),
                            )
                        except Exception as exc:
                            independent_gate = {
                                "status": "UNKNOWN",
                                "error_type": type(exc).__name__,
                            }
                    if not isinstance(independent_gate, dict) or independent_gate.get(
                        "status"
                    ) not in {
                        "PASS",
                        "FAIL",
                        "NOT_RUN",
                        "UNKNOWN",
                    }:
                        independent_gate = {
                            "status": "UNKNOWN",
                            "error_type": "InvalidJudgeReceipt",
                        }
                    if (
                        independent_gate.get("status") == "PASS"
                        and independent_gate.get("tree_hash") != result.tree_after
                    ):
                        independent_gate = {
                            **independent_gate,
                            "status": "FAIL",
                            "reason": "JUDGE_TREE_MISMATCH",
                        }
                    if independent_gate.get("status") != "PASS" and result.status == "completed":
                        result = result.model_copy(
                            update={
                                "status": "blocked",
                                "reason": "independent_judge_not_passed",
                                "summary": "Candidate stopped; independent judge did not pass",
                            }
                        )
                payload = self._persist(runtime, result, metered, tool_specs)
                if profile is not None:
                    payload.update(
                        candidate_status=candidate_status,
                        independent_gate=independent_gate,
                        evaluation_profile=profile.model_dump(mode="json"),
                    )
                snapshot = budgets.snapshot(task_id)
                payload.update(
                    model_calls=snapshot.model_calls,
                    tool_calls=snapshot.tool_calls,
                    cost_micros=snapshot.cost_micros,
                    run_id=str(task.run_id),
                    execution_epoch=epoch,
                )
                content_hash = self.engine.artifacts.put(
                    dump_result(self.engine.artifacts, payload, task_id, str(task.run_id))
                )
                self.fault_hook("result_before_commit", {"content_hash": content_hash})
                if workspace.snapshot().tree_hash != result.tree_after:
                    raise DomainError(
                        "HARNESS_TERMINAL_TREE_UNSTABLE",
                        "Tree changed before result commit",
                        409,
                    )
                self.store.finish(task_id, epoch, result.status, content_hash, result.tree_after)
                self.fault_hook("result_after_commit", {"content_hash": content_hash})
                return payload
        return self._persist(runtime, result, metered, tool_specs)

    def _persist(
        self,
        runtime: _TaskRuntime,
        result: CodingResult,
        metered: MeteredModel,
        tool_specs: tuple[ToolSpec, ...],
    ) -> dict[str, Any]:
        task_id = runtime.task_id
        workspace = runtime.workspace
        request = runtime.request
        directory = self.runs / task_id
        directory.mkdir(parents=True, exist_ok=True)
        patch_file = directory / "change.patch"
        patch_file.write_text(result.diff.patch, encoding="utf-8")
        receipts = [receipt.model_dump(mode="json") for receipt in result.receipts]
        tests = _test_summary(result)
        model_id = (DETERMINISTIC_MODEL_ID if request.mode != "live" else
                    request.product_model_capabilities.get("model_id", self._live_model_id))
        if request.product_session_id and (
            request.product_plan.get("verification_scope") == "build_only"
        ):
            tests = {"status": "not_run", "run": 0, "failed": 0, "skipped": 0,
                     "detail": "仅执行构建/内容检查，未配置行为测试；不代表行为测试通过。"}
        payload: dict[str, Any] = {
            "task_id": task_id,
            "status": result.status,
            "reason": result.reason,
            "summary": result.summary,
            "objective": result.objective,
            "task_kind": result.task_kind,
            "intent": request.product_intent or result.task_kind,
            "verification_scope": request.product_plan.get("verification_scope", "tests"),
            "created_at": datetime.now(UTC).isoformat(),
            "tree_before": result.tree_before,
            "tree_after": result.tree_after,
            "changes": [change.model_dump(mode="json") for change in result.diff.changes],
            "diff": result.diff.patch,
            "patch_path": str(patch_file),
            "patch_url": f"/api/v3/harness/tasks/{task_id}/patch",
            "checklist": result.checklist.model_dump(mode="json"),
            "attempts": [attempt.model_dump(mode="json") for attempt in result.attempts],
            "receipts": receipts,
            "service_receipts": [r.model_dump(mode="json") for r in result.service_receipts],
            "commands": [
                {
                    "receipt_id": receipt.receipt_id,
                    "argv": list(receipt.argv),
                    "status": receipt.status,
                    "exit_code": receipt.exit_code,
                    "duration_seconds": receipt.duration_seconds,
                }
                for receipt in result.receipts
            ],
            "tests": tests,
            "verification": result.verification.model_dump(mode="json")
            if result.verification is not None
            else None,
            "model_calls": metered.calls,
            "tool_calls": result.tool_calls,
            "input_bytes": metered.input_bytes,
            "output_bytes": metered.output_bytes,
            "cost_micros": metered.cost_micros,
            "input_tokens": result.input_tokens,
            "output_tokens": result.output_tokens,
            "output_receipts": [r.model_dump(mode="json") for r in result.output_receipts],
            "output_continuations": result.output_continuations,
            "tool_error_receipts": [r.model_dump(mode="json") for r in result.tool_error_receipts],
            "tool_correction_receipts": [
                r.model_dump(mode="json") for r in result.tool_correction_receipts
            ],
            "tool_error_policy": result.tool_error_policy.model_dump(mode="json")
            if result.tool_error_policy else None,
            "lifecycle_policy": result.lifecycle_policy.model_dump(mode="json")
            if result.lifecycle_policy else None,
            "entrypoint_batches": [b.model_dump(mode="json") for b in result.entrypoint_batches],
            "output_policy": result.output_policy.model_dump(mode="json")
            if result.output_policy else None,
            "peak_context_tokens": result.peak_context_tokens,
            "estimated_peak_input_tokens": result.estimated_peak_input_tokens,
            "repair_rounds": result.repair_rounds,
            "command_seconds": sum(receipt.duration_seconds for receipt in result.receipts),
            "no_progress_rounds": sum(
                not attempt.progress
                for attempt in result.attempts
                if attempt.reason in {"acceptance_unmet", "no_progress", "tool_calls"}
            ),
            "compaction_count": max(
                (metric["compaction_count"] for metric in result.context_metrics), default=0
            ),
            "protocol": f"{request.mode}:{model_id}",
            "model_id": model_id,
            "live_model": request.mode == "live",
            "approval_ref": f"web-approval-{task_id}",
            "workspace_path": str(workspace.root),
            "workspace_files": len(workspace.snapshot().files),
            "tool_specs": [spec.model_dump(mode="json") for spec in tool_specs],
            "context_state": result.context_state.model_dump(mode="json")
            if result.context_state is not None
            else None,
            "context_ref": result.context_ref,
            "context_metrics": list(result.context_metrics),
            "checkpoint": result.checkpoint.model_dump(mode="json", exclude={"context_state"})
            if result.checkpoint is not None else None,
        }
        if self.store is not None:
            assert runtime.task is not None
            if request.product_session_id and request.product_expert_max_tokens:
                payload.update(self.store.run_token_usage(task_id))
            usage = PersistentBudgetLedger(
                self.store, task_id, runtime.task.execution_epoch,
            ).resource_snapshot(task_id)
            reserved = int(usage["reserved_micros"])
            settled = int(usage["used"]["cost_micros"]) - reserved
            payload.update(
                model_calls=int(usage["used"]["model_calls"]), cost_micros=settled,
                tool_calls=int(usage["used"]["tool_calls"]),
                settled_cost_micros=settled, reserved_micros=reserved,
                usage_known=usage["unknown_calls"] == 0,
                usage_ledger={
                    "source": "original_engine_task_budget",
                    "model_calls_used_or_reserved": int(usage["used"]["model_calls"]),
                    "tool_calls_used_or_reserved": int(usage["used"]["tool_calls"]),
                    "settled_cost_micros": settled, "reserved_micros": reserved,
                    "unknown_calls": int(usage["unknown_calls"]),
                },
                metered_process={"model_calls": metered.calls,
                                 "model_cost_micros": metered.cost_micros},
            )
        if result.acceptance_contract is not None:
            contract = result.acceptance_contract
            if result.acceptance_task is None:
                raise DomainError("ACCEPTANCE_CONTRACT_MISSING",
                                  "Missing accepted task snapshot", 409)
            contract.assert_task(result.acceptance_task)
            artifacts: dict[str, Any] = {
                "verification_report": result.verification.verification_id
                if result.verification is not None else None,
                "patch": sha256_text(result.diff.patch),
                "test_receipts": [r.receipt_id for r in result.receipts],
                "process_receipts": [r.receipt_id for r in result.service_receipts],
                "evidence_references": list(dict.fromkeys(
                    [item for node in result.graph.nodes for item in node.evidence]
                    + list(result.context_state.evidence_ids if result.context_state else ())
                )),
            }
            payload.update(
                acceptance_contract=contract.model_dump(mode="json"),
                acceptance_binding=result.acceptance_binding.model_dump(mode="json")
                if result.acceptance_binding is not None else None,
                acceptance_receipts=[r.model_dump(mode="json") for r in result.acceptance_receipts],
                acceptance_task=result.acceptance_task.model_dump(mode="json"),
                acceptance_artifacts={k: artifacts[k] for k in contract.required_artifacts},
            )
            if result.status == "completed" and any(
                not artifacts.get(name) for name in contract.required_artifacts
            ):
                raise DomainError("ACCEPTANCE_ARTIFACT_MISSING",
                                  "Required acceptance artifacts were not produced", 409)
        cancel_requested_at = runtime.control.cancel_requested_at
        cancel_observed_at = (
            runtime.finished_at if cancel_requested_at and result.status == "cancelled" else ""
        )
        cancel_receipt = {
            "requested_at": cancel_requested_at,
            "observed_at": cancel_observed_at,
            "status": (
                "observed" if cancel_observed_at else ("too_late" if cancel_requested_at else "")
            ),
            "last_runner_receipt": receipts[-1] if receipts else None,
            "provider_receipt": {
                "scope": "current_process_meter",
                "model_calls": metered.calls,
                "cost_micros": metered.cost_micros,
                "live_model": request.mode == "live",
            },
            "refund": "not_promised",
        }
        payload.update(
            {
                "stream_version": STREAM_VERSION,
                "started_at": runtime.started_at,
                "finished_at": runtime.finished_at,
                "background": runtime.thread is not None,
                "events_url": f"/api/v3/harness/tasks/{task_id}/events",
                "messages_url": f"/api/v3/harness/tasks/{task_id}/messages",
                "cancel_receipt": cancel_receipt,
                "inbox": runtime.inbox.messages(),
                "inbox_revision": runtime.inbox.revision,
                "patch_sha256": sha256_text(result.diff.patch),
            }
        )
        self._emit(
            runtime,
            "artifact_ready",
            runtime.inbox.revision,
            {
                "status": result.status,
                "patch_url": payload["patch_url"],
                "patch_sha256": payload["patch_sha256"],
                "diff_bytes": len(result.diff.patch.encode("utf-8")),
                "changes": len(result.diff.changes),
            },
        )
        payload["events_cursor"] = runtime.events.last_cursor
        payload["progress"] = runtime.events.summary()
        self._write_json(directory / "result.json", payload)
        self._status(
            runtime,
            status=result.status,
            finished_at=runtime.finished_at,
            cancel_receipt=cancel_receipt,
            inbox_revision=runtime.inbox.revision,
        )
        return payload

    def _persist_failure(
        self,
        runtime: _TaskRuntime,
        code: str,
        message: str,
        http_status: int,
    ) -> dict[str, Any]:
        if self.store is None:
            return self._persist_failure_payload(runtime, code, message, http_status)
        with workspace_lock(runtime.workspace.root):
            row = self.store.get(runtime.task_id)
            epoch = runtime.task.execution_epoch if runtime.task else row["execution_epoch"]
            self.store.guard(runtime.task_id, epoch)
            payload = self._persist_failure_payload(runtime, code, message, http_status)
            content_hash = self.engine.artifacts.put(json.dumps(payload, sort_keys=True).encode())
            self.store.finish(
                runtime.task_id, epoch, payload["status"], content_hash, row["tree_hash"]
            )
            return payload

    def _persist_failure_payload(
        self,
        runtime: _TaskRuntime,
        code: str,
        message: str,
        http_status: int,
    ) -> dict[str, Any]:
        runtime.finished_at = datetime.now(UTC).isoformat()
        final_status = (
            "cancelled"
            if code == "HARNESS_CANCELLED"
            else ("failed" if http_status >= 500 else "blocked")
        )
        cancel_requested_at = runtime.control.cancel_requested_at
        cancel_receipt = {
            "requested_at": cancel_requested_at,
            "observed_at": runtime.finished_at
            if cancel_requested_at and final_status == "cancelled"
            else "",
            "status": (
                "observed"
                if cancel_requested_at and final_status == "cancelled"
                else ("too_late" if cancel_requested_at else "")
            ),
            "last_runner_receipt": None,
            "provider_receipt": None,
            "refund": "not_promised",
        }
        self._emit(
            runtime,
            "run_failed",
            runtime.inbox.revision,
            {"code": code, "message": message, "status": final_status},
        )
        payload: dict[str, Any] = {
            "task_id": runtime.task_id,
            "status": final_status,
            "reason": code,
            "summary": message,
            "objective": (
                runtime.task.objective
                if runtime.task is not None
                else runtime.request.objective.strip()
            ),
            "task_kind": runtime.request.task_kind,
            "created_at": runtime.started_at or datetime.now(UTC).isoformat(),
            "started_at": runtime.started_at,
            "finished_at": runtime.finished_at,
            "stream_version": STREAM_VERSION,
            "events_url": f"/api/v3/harness/tasks/{runtime.task_id}/events",
            "messages_url": f"/api/v3/harness/tasks/{runtime.task_id}/messages",
            "cancel_receipt": cancel_receipt,
            "inbox": runtime.inbox.messages(),
            "inbox_revision": runtime.inbox.revision,
            "progress": runtime.events.summary(),
            "events_cursor": runtime.events.last_cursor,
            "background": runtime.thread is not None,
        }
        self._write_json(self.runs / runtime.task_id / "result.json", payload)
        self._status(
            runtime,
            status=final_status,
            error=message,
            finished_at=runtime.finished_at,
            cancel_receipt=cancel_receipt,
            inbox_revision=runtime.inbox.revision,
        )
        return payload

    def _test_command(
        self, workspace: TaskWorkspace, request: CodingTaskRequest, kind: str
    ) -> tuple[str, ...]:
        if request.product_session_id:
            return tuple(request.product_plan.get("commands", {}).get("tests", {}).get("argv", ()))
        if self.evaluation_profile is not None:
            expected = self.evaluation_profile.public_test_argv
            if (
                request.test_command.strip()
                and tuple(shlex.split(request.test_command)) != expected
            ):
                raise DomainError(
                    "EVALUATION_SCOPE_DENIED", "Test command differs from profile", 403
                )
            return expected
        if request.test_command.strip():
            argv = tuple(shlex.split(request.test_command.strip()))
            if not argv:
                raise DomainError("HARNESS_TEST_COMMAND_REQUIRED", "Test command is empty", 422)
            return _no_bytecode(argv)
        if kind not in _WRITABLE:
            return ()
        profile = detect_project(workspace.snapshot().files)
        if not profile.test_argv:
            raise DomainError(
                "HARNESS_TEST_COMMAND_REQUIRED",
                "This project needs an explicit test command",
                422,
            )
        return _no_bytecode(tuple(profile.test_argv))

    def _requirements(
        self, kind: str, test_argv: tuple[str, ...]
    ) -> tuple[VerificationRequirement, ...]:
        profile = self.evaluation_profile
        additional = (
            tuple(VerificationRequirement(
                requirement_id=f"regression_{index}", kind="test", command_argv=command.argv,
                acceptance_role="regression", min_tests=1,
            ) for index, command in enumerate(profile.independent_regression_plan.commands, 1)
                if command.argv not in {test_argv, profile.public_build_argv,
                    profile.public_probe.argv if profile.public_probe else ()})
            if profile and profile.public_feedback_policy and profile.independent_regression_plan
            else ()
        )
        build = (
            (VerificationRequirement(
                requirement_id="build", kind="build", command_argv=profile.public_build_argv,
                acceptance_role="regression"
                if profile.verification_protocol == "public-probe-v1" else "legacy",
            ),)
            if profile is not None and profile.public_build_argv else ()
        )
        if profile is not None and profile.verification_protocol == "public-probe-v1":
            plan = profile.public_probe
            if plan is None or not test_argv:
                raise DomainError(
                    "PUBLIC_PROBE_REQUIRED", "Behavior and regression plans are required", 409
                )
            return (
                VerificationRequirement(
                    requirement_id="suite",
                    kind="test",
                    min_tests=1,
                    command_argv=test_argv,
                    acceptance_role="regression",
                ),
                VerificationRequirement(
                    requirement_id="public_probe",
                    kind="test",
                    command_argv=plan.argv,
                    min_tests=len(plan.case_ids),
                    acceptance_role="behavior",
                    public_probe_plan_hash=plan.plan_hash,
                ),
            ) + build + additional
        if kind == "run_app" and not test_argv:
            return build
        if kind not in _WRITABLE or not test_argv:
            return (
                VerificationRequirement(
                    requirement_id="evidence", kind="test", command_contains=()
                ),
            )
        return (
            VerificationRequirement(
                requirement_id="suite",
                kind="test",
                command_contains=(_program(test_argv),),
                command_argv=(test_argv
                              if build or profile and profile.public_feedback_policy else ()),
                min_tests=1,
            ),
        ) + build + additional

    def _sweep(self, workspace: TaskWorkspace) -> None:
        """Remove host-side metadata the container may have created.

        Container-created bytecode is suppressed with ``-B``; this only
        provides a stable tree for the persisted evidence.
        """

        if self.store is not None:
            # Durable commands use -B; a fenced worker cannot clean files
            # belonging to a newer epoch, even when they are ignored caches.
            return
        for pattern in ("**/__pycache__",):
            for path in workspace.root.glob(pattern):
                if not path.is_dir():
                    continue
                for child in path.iterdir():
                    try:
                        child.unlink()
                    except OSError:
                        pass
                try:
                    path.rmdir()
                except OSError:
                    pass

    # -- storage --------------------------------------------------------

    def _projects(self) -> dict[str, dict[str, Any]]:
        if not self.projects_file.is_file():
            return {}
        try:
            payload = json.loads(self.projects_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        if not isinstance(payload, dict):
            return {}
        return {
            str(key): cast(dict[str, Any], value)
            for key, value in payload.items()
            if isinstance(value, dict)
        }

    @staticmethod
    def _write_json(path: Path, payload: Any) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(path.suffix + ".tmp")
        temporary.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
        temporary.replace(path)


def _argv(arguments: dict[str, Any]) -> tuple[str, ...]:
    raw = arguments.get("argv")
    if (
        not isinstance(raw, list)
        or not raw
        or not all(isinstance(item, str) and item for item in raw)
    ):
        raise DomainError("HARNESS_TOOL_ARGUMENT", "argv must be a non-empty string list", 422)
    return tuple(cast(list[str], raw))


def _resolve_test_command(
    arguments: dict[str, Any],
    trusted: tuple[str, ...],
    public_probe: PublicProbePlan | None = None,
    build_argv: tuple[str, ...] = (),
    feedback_plan: RegressionPlan | None = None,
) -> tuple[str, ...] | None:
    if feedback_plan is not None:
        for index, command in enumerate(feedback_plan.commands, 1):
            if arguments == {"command_id": f"regression_{index}"}:
                return command.argv
    if build_argv and arguments == {"command_id": "build"}:
        return build_argv
    if (
        public_probe is not None
        and set(arguments) == {"command_id"}
        and arguments["command_id"] == "public_probe"
    ):
        return public_probe.argv
    if set(arguments) == {"command_id"} and arguments["command_id"] == "tests":
        return trusted
    if set(arguments) == {"argv"}:
        try:
            argv = _argv(arguments)
        except DomainError:
            return None
        if argv == trusted:
            return trusted
    return None


def _program(argv: tuple[str, ...]) -> str:
    return argv[0].rsplit("/", 1)[-1] if argv else ""


def _no_bytecode(argv: tuple[str, ...]) -> tuple[str, ...]:
    if _program(argv) == "python3" and "-B" not in argv:
        return (argv[0], "-B", *argv[1:])
    return argv


def _execution_status(state: str, exit_code: int | None) -> str:
    if state == "timed_out":
        return "timeout"
    if state == "cancelled":
        return "denied"
    if state in {"exited", "failed"}:
        return "passed" if exit_code == 0 else "failed"
    return "error"


def _test_status(
    kind: str, output: str, exit_code: int | None, *, argv: tuple[str, ...] = (),
) -> tuple[str, int, int, int, str]:
    if exit_code is None:
        return "error", 0, 0, 0, "test container disappeared"
    return parse_tests(test_project_kind(argv, fallback=kind), output, exit_code)


def _feedback_test_status(
    plan: RegressionPlan | None, kind: str, output: str, exit_code: int | None,
    *, argv: tuple[str, ...], complete: bool,
) -> tuple[str, int, int, int, str]:
    command = next((c for c in plan.commands if c.argv == argv), None) if plan else None
    if command is None:
        return _test_status(kind, output, exit_code, argv=argv)
    native = parse_independent_test(command, output, exit_code, complete=complete)
    status = ("passed" if native.status == "PASS" else
              "failed" if native.status == "FAIL" else "unverified")
    return (status, native.tests_run or 0, native.tests_failed or 0,
            native.tests_skipped or 0, native.detail)


def _parsed_model_test_receipt(raw: ExecutionReceipt, *, complete: bool) -> ExecutionReceipt:
    """Populate real model-phase baseline counts; these never attest independent coverage."""
    if not complete or raw.purpose != "test" or raw.public_probe is not None:
        return raw
    state, ran, failed, _, _ = _test_status(
        "unknown", raw.stdout + raw.stderr, raw.exit_code, argv=raw.argv,
    )
    if state not in {"passed", "failed", "no_tests"}:
        return raw
    values = raw.model_dump(mode="json", exclude={"receipt_id"})
    values.update(tests_run=ran, tests_failed=failed)
    return raw.model_copy(update={"tests_run": ran, "tests_failed": failed,
                                  "receipt_id": canonical_hash(values)})


def _build_status(exit_code: int | None) -> tuple[str, int, int, int, str]:
    if exit_code is None:
        return "error", 0, 0, 0, "Build container disappeared; no tests were run"
    return (
        "passed" if exit_code == 0 else "failed", 0, 0, 0,
        "Build passed; no tests were run" if exit_code == 0 else "Build failed; no tests were run",
    )


def _test_feedback(detail: str, output: str) -> str:
    """Keep the summary and useful failure context while paging the raw log."""
    if len(output) <= 7000:
        preview = output
    else:
        # Pytest warnings can push the actual assertion out of both ends.
        # Prioritize the failure section; the original log stays in CAS.
        failure = re.search(
            r"^=+ FAILURES =+$(.*?)(?=^=+ (?:warnings summary|short test summary)|\Z)",
            output, re.M | re.S,
        )
        excerpt = failure.group(1) if failure else ""
        if len(excerpt) > 3500:
            excerpt = excerpt[:2000] + "\n[failure excerpt omitted]\n" + excerpt[-1400:]
        preview = (output[:1000] + "\nFailure context:\n" + excerpt
                   + "\n[log preview omitted; use artifact_page]\n" + output[-2000:])
    return f"{detail}\n{preview}"


def _public_probe_source_data(
    raw: ExecutionReceipt, output_ref: dict[str, Any],
) -> dict[str, Any]:
    """Retain raw identity without duplicating stdout inside the tool CAS object."""
    return {
        "raw_receipt_metadata": raw.model_dump(mode="json", exclude={"stdout", "stderr"}),
        "raw_output_ref": {
            **output_ref,
            "log_sha256": raw.log_sha256,
            "stdout_bytes": len(raw.stdout.encode()),
            "stderr_bytes": len(raw.stderr.encode()),
        },
    }


def _test_summary(result: CodingResult) -> dict[str, Any]:
    for receipt in reversed(result.receipts):
        if receipt.purpose == "build":
            continue
        public = receipt.public_feedback
        if public is not None:
            valid = (public.receipt_id == receipt.receipt_id and public.argv == receipt.argv
                     and public.tree_hash == receipt.tree_hash
                     and public.tree_hash == result.tree_after
                     and public.log_sha256 == receipt.log_sha256)
            return {"status":public.outcome if valid else "unverified",
                    "run":public.tests_run if valid else None,
                    "failed":public.tests_failed if valid else None,
                    "skipped":public.tests_skipped if valid else None,
                    "detail":"Scope-bound public check feedback",
                    "receipt_id":receipt.receipt_id}
        if receipt.public_probe is not None:
            outcome = receipt.public_probe.get("outcome")
            status = (
                "passed" if (outcome == "green" and receipt.status == "passed"
                             and receipt.exit_code == 0 and receipt.tests_run > 0
                             and receipt.tests_failed == 0)
                else "failed" if (outcome == "red" and receipt.status == "failed"
                                  and receipt.exit_code == 1 and receipt.tests_failed > 0)
                else "unverified"
            )
            run, failed, skipped = receipt.tests_run, receipt.tests_failed, 0
            detail = "Public probe " + str(outcome) + ": " + str(
                receipt.public_probe.get("reason_code", "")
            )
        else:
            status, run, failed, skipped, detail = _test_status(
                result.task_kind, receipt.stdout, receipt.exit_code, argv=receipt.argv,
            )
        if status == "passed" and receipt.status != "passed":
            status = "unverified"
            detail = "Execution receipt does not verify a complete passing run"
        return {
            "status": status,
            "run": run,
            "failed": failed,
            "skipped": skipped,
            "detail": detail,
            "receipt_id": receipt.receipt_id,
        }
    return {"status": "not_run", "run": 0, "failed": 0, "skipped": 0, "detail": ""}
