"""H10 scoped CAS rereads and tree-bound search caching.

The registry owns the provenance index.  A CAS digest is an identifier, never
a capability: every page checks the original task, workspace, source path and
the current Broker authority.  Restoring an index is deliberately not a lease
or process recovery mechanism (H11).
"""

from __future__ import annotations

import ast
import base64
import json
from collections.abc import Sequence
from copy import deepcopy
from typing import Any, Literal

from pydantic import ValidationError

from mgar_v3.context.compactor import ArtifactPaginator
from mgar_v3.domain import DomainError
from mgar_v3.evidence.artifacts import ArtifactStore
from mgar_v3.harness.broker import CapabilityAuthority, CapabilityLease, _is_protected, _within
from mgar_v3.harness.contracts import (
    ExecutionProvenance,
    ExecutionReceipt,
    HarnessToolCall,
    HarnessToolResult,
    ToolContext,
    ToolSpec,
    canonical_hash,
    normalize_relative_path,
    sha256_text,
)
from mgar_v3.harness.workspace import TaskWorkspace


def _spec(name: str, *, cursor: bool) -> ToolSpec:
    properties: dict[str, Any] = {
        "path": {"type": "string"},
        "artifact_hash": {"type": "string", "pattern": "^[a-f0-9]{64}$"},
    }
    if cursor:
        properties["cursor"] = {"type": "string"}
    return ToolSpec(
        name=name,
        description=(
            "Read a bounded UTF-8 page from a previously returned task artifact. "
            "Copy the exact path and artifact_hash from the issued artifact reference. "
            "Checklist receipt IDs identify verification records and cannot open a page. "
            + ("Follow continuation for the next page." if cursor else "Reread from page one.")
        ),
        input_schema={
            "type": "object",
            "properties": properties,
            "required": ["path", "artifact_hash"],
            "additionalProperties": False,
        },
        capability="read",
    )


ARTIFACT_TOOLS = (_spec("artifact_page", cursor=True), _spec("artifact_reread", cursor=False))
READ_LINES_SPEC = ToolSpec(
    name="read_lines",
    description=(
        "Read current source lines. Send path and start_line for the default 40-line page. "
        "For a longer range, request another page from end_line + 1. "
        "Use start_line as a 1-based line number, not as an artifact cursor. "
        "Use returned file_sha256 as expected_sha256 when editing this file. "
        "The result is bounded and checked against the current read grant and tree."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "path": {"type": "string"},
            "start_line": {"type": "integer", "minimum": 1},
            "max_lines": {"type": "integer", "minimum": 1, "maximum": 60,
                          "description": "Optional page size, 1 to 60. Omit for 40 lines."},
        },
        "required": ["path", "start_line"],
        "additionalProperties": False,
    },
    capability="read",
)
READ_SYMBOL_SPEC = ToolSpec(
    name="read_symbol",
    description=(
        "Read a Python function or class by its name, including decorators, without running it. "
        "Use a qualified name such as Command.parse_args to resolve duplicate method names. "
        "Prefer this to searching then paging a function by line numbers. "
        "Returns up to 200 lines, current file/tree hashes and a continuation for larger symbols."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "path": {"type": "string"}, "symbol": {"type": "string"},
            "max_lines": {"type": "integer", "minimum": 1, "maximum": 200},
        },
        "required": ["path", "symbol"], "additionalProperties": False,
    },
    capability="read",
)


class ScopedContextTools:
    def __init__(
        self,
        workspace: TaskWorkspace,
        authority: CapabilityAuthority,
        *,
        artifacts: ArtifactStore | None = None,
        page_bytes: int = 1024,
    ) -> None:
        self.workspace = workspace
        self.authority = authority
        self.artifacts = artifacts or ArtifactStore(
            workspace._metadata_root() / "context-artifacts"
        )
        self.paginator = ArtifactPaginator(self.artifacts, page_bytes=page_bytes)
        self._references: dict[tuple[str, str], dict[str, Any]] = {}
        self._recovered_log_leases: dict[tuple[str, str], str] = {}
        self._search_cache: dict[str, list[dict[str, Any]]] = {}
        self._cache_tree = ""

    def references(self) -> tuple[dict[str, Any], ...]:
        return tuple(deepcopy(item) for item in self._references.values())

    def _check(self, context: ToolContext, paths: Sequence[str]) -> CapabilityLease:
        task, grant = context.task, context.grant
        manifest = self.workspace.manifest
        if (
            task.task_id != manifest.task_id
            or task.workspace_id != manifest.workspace_id
            or task.repository != manifest.repository
            or task.base_commit != manifest.base_commit
            or grant.actor != task.actor
            or grant.workspace_id != task.workspace_id
            or grant.repository != task.repository
        ):
            raise DomainError("CONTEXT_ARTIFACT_SCOPE_DENIED", "Artifact scope differs", 403)
        if any(not grant.can_read(path) for path in paths):
            raise DomainError("HARNESS_READ_DENIED", "Artifact path is outside the grant", 403)
        lease = self.authority.check_scope(
            actor=task.actor,
            workspace_id=task.workspace_id,
            repository=task.repository,
            execution_epoch=task.execution_epoch,
            paths=paths,
            write=False,
        )
        # Authorize the requested name before following an internal link, then
        # independently authorize the canonical target under current authority.
        try:
            canonical_paths = tuple(self.workspace.resolve_read_path(path) for path in paths)
        except FileNotFoundError as exc:
            raise DomainError(
                "WORKSPACE_FILE_NOT_FOUND", "Artifact source path is missing", 404
            ) from exc
        if any(not grant.can_read(path) for path in canonical_paths):
            raise DomainError("HARNESS_READ_DENIED", "Artifact target is outside the grant", 403)
        try:
            self.authority.check_scope(
                actor=task.actor, workspace_id=task.workspace_id, repository=task.repository,
                execution_epoch=task.execution_epoch, paths=canonical_paths, write=False,
            )
        except DomainError as exc:
            if exc.code == "HARNESS_PROTECTED_PATH_DENIED" and canonical_paths != tuple(paths):
                raise DomainError(
                    "WORKSPACE_SYMLINK_DENIED", "Symlink targets protected workspace data", 403
                ) from exc
            raise
        self._unchanged(lease)
        return lease

    def _unchanged(self, lease: CapabilityLease) -> None:
        if self.authority.peek().lease_hash != lease.lease_hash:
            raise DomainError("CONTEXT_AUTHORITY_CHANGED", "Authority changed during read", 403)

    def archive(
        self,
        text: str,
        context: ToolContext,
        *,
        path: str,
        kind: Literal["source", "log"] = "source",
        complete: bool = True,
        total_bytes: int | None = None,
        provenance: ExecutionProvenance | None = None,
    ) -> dict[str, Any]:
        """Import a trusted observation, or a provenanced command-log capture.

        Legacy imports do not claim to establish a command's generating scope.
        Real execution adapters must use ``archive_command_log`` instead.
        """
        path = normalize_relative_path(path)
        lease = self._check(context, (path,))
        if provenance is not None:
            self._check_provenance(provenance, context, lease)
        tree = self.workspace.snapshot().tree_hash
        if context.workspace_tree != tree:
            raise DomainError("CONTEXT_TREE_STALE", "Artifact source tree changed", 409)
        raw = text.encode("utf-8")
        content_hash = self.artifacts.put(raw)
        self._unchanged(lease)
        reference: dict[str, Any] = {
            "schema_version": "harness-context-reference-v2",
            "artifact_hash": content_hash,
            "path": path,
            "kind": kind,
            "task_id": context.task.task_id,
            "run_id": str(context.task.run_id),
            "actor": context.task.actor,
            "repository": context.task.repository,
            "base_commit": context.task.base_commit,
            "grant_hash": canonical_hash(context.grant.model_dump(mode="json")),
            "canonical_path": self.workspace.resolve_read_path(path),
            "workspace_id": context.task.workspace_id,
            "acceptance_revision": context.task.acceptance_revision,
            "execution_epoch": context.task.execution_epoch,
            "tree": tree,
            "lease_hash": lease.lease_hash,
            "read_paths": list(lease.read_paths),
            "protected_paths": list(lease.protected_paths),
            "total_bytes": total_bytes if total_bytes is not None else len(raw),
            "retained_bytes": len(raw),
            "complete": complete,
            "provenance_kind": "command" if provenance is not None else "trusted_import",
            "provenance": provenance.model_dump(mode="json") if provenance is not None else None,
        }
        if provenance is not None:
            reference.update({
                "lease_hash": provenance.lease_hash,
                "read_paths": list(provenance.read_paths),
                "write_paths": list(provenance.write_paths),
                "protected_paths": list(provenance.protected_paths),
            })
        self._references[(content_hash, path)] = reference
        self._unchanged(lease)
        return deepcopy(reference)

    def _check_provenance(
        self, provenance: ExecutionProvenance, context: ToolContext, lease: CapabilityLease
    ) -> None:
        if (
            provenance.lease_hash != lease.lease_hash
            or provenance.lease_id != lease.lease_id
            or provenance.actor != context.task.actor
            or provenance.workspace_id != context.task.workspace_id
            or provenance.repository != context.task.repository
            or provenance.execution_epoch != context.task.execution_epoch
            or provenance.operation_id != context.operation_id
            or provenance.grant_hash != canonical_hash(context.grant.model_dump(mode="json"))
        ):
            raise DomainError(
                "CONTEXT_COMMAND_AUTHORITY_CHANGED", "Command generating authority changed", 403
            )

    def archive_command_log(
        self,
        text: str,
        context: ToolContext,
        *,
        provenance: ExecutionProvenance | None,
        complete: bool = True,
        total_bytes: int | None = None,
    ) -> dict[str, Any]:
        """Register real command output without re-sampling its origin lease."""
        if provenance is None:
            raise DomainError(
                "CONTEXT_COMMAND_PROVENANCE_REQUIRED", "Command provenance is missing", 403
            )
        reference = self.archive(
            text, context, path="", kind="log", complete=complete,
            total_bytes=total_bytes, provenance=provenance,
        )
        lease = self._check(context, ("",))
        self._check_provenance(provenance, context, lease)
        return reference

    def page(self, call: HarnessToolCall, context: ToolContext) -> HarnessToolResult:
        path, content_hash = call.arguments.get("path"), call.arguments.get("artifact_hash")
        cursor = call.arguments.get("cursor")
        if (
            call.name not in {"artifact_page", "artifact_reread"}
            or set(call.arguments) - {"path", "artifact_hash", "cursor"}
            or not isinstance(path, str)
            or not isinstance(content_hash, str)
            or (cursor is not None and (not isinstance(cursor, str) or not cursor))
            or (call.name == "artifact_reread" and cursor is not None)
        ):
            raise DomainError("HARNESS_TOOL_ARGUMENT", "Invalid artifact page arguments", 422)
        path = normalize_relative_path(path)
        lease = self._check(context, (path,))
        reference = self._references.get((content_hash, path))
        if reference is None:
            raise DomainError("CONTEXT_ARTIFACT_SCOPE_DENIED", "Artifact was not issued here", 403)
        self._validate_reference(reference, context)
        # Logs may contain output from any path visible to the command.  A
        # narrowed or otherwise replaced lease cannot expose that older scope.
        if reference["kind"] == "log" and lease.lease_hash not in {
            reference["lease_hash"], self._recovered_log_leases.get((content_hash, path)),
        }:
            raise DomainError("CONTEXT_ARTIFACT_SCOPE_DENIED", "Log lease changed", 403)
        if (
            reference.get("provenance_kind") == "command"
            and reference["provenance"]["grant_hash"]
            != canonical_hash(context.grant.model_dump(mode="json"))
        ):
            raise DomainError("CONTEXT_ARTIFACT_SCOPE_DENIED", "Log grant changed", 403)
        page = self.paginator.page(content_hash, cursor)
        payload = {
            **page.model_dump(mode="json"),
            "artifact_hash": content_hash,
            "path": path,
            "source_tree": reference["tree"],
            "source_revision": reference["acceptance_revision"],
            "current_tree": self.workspace.snapshot().tree_hash,
            "source_complete": reference["complete"],
            "source_total_bytes": reference["total_bytes"],
        }
        result = HarnessToolResult(
            call_id=call.call_id,
            status="ok",
            content=json.dumps(payload, ensure_ascii=False),
            data=payload,
        )
        self._unchanged(lease)
        return result

    def _validate_reference(self, reference: dict[str, Any], context: ToolContext) -> None:
        expected = {
            "schema_version": "harness-context-reference-v2",
            "task_id": context.task.task_id, "run_id": str(context.task.run_id),
            "workspace_id": context.task.workspace_id, "actor": context.task.actor,
            "repository": context.task.repository, "base_commit": context.task.base_commit,
            "grant_hash": canonical_hash(context.grant.model_dump(mode="json")),
        }
        if any(reference.get(key) != value for key, value in expected.items()):
            raise DomainError("CONTEXT_ARTIFACT_SCOPE_DENIED", "Artifact origin scope differs", 403)
        if reference.get("kind") not in {"source", "log"}:
            raise DomainError("CONTEXT_ARTIFACT_SCOPE_DENIED", "Artifact type differs", 403)
        canonical = reference.get("canonical_path")
        if not isinstance(canonical, str):
            raise DomainError("CONTEXT_ARTIFACT_SCOPE_DENIED", "Artifact target is missing", 403)
        lease = self._check(context, (reference["path"], canonical))
        if (
            tuple(reference.get("read_paths", ())) != lease.read_paths
            or tuple(reference.get("protected_paths", ())) != lease.protected_paths
        ):
            raise DomainError("CONTEXT_ARTIFACT_SCOPE_DENIED", "Artifact read scope changed", 403)

    def restore_known(self, context: ToolContext, recovery_io: Any) -> int:
        """Restore only Engine-selected, CAS-verified known tool receipts.

        No model-supplied reference or workspace journal can populate the
        provenance index. A new lease still checks every original target.
        """
        if (
            recovery_io.task_id != context.task.task_id
            or recovery_io.run_id != str(context.task.run_id)
            or recovery_io.execution_epoch != context.task.execution_epoch
        ):
            raise DomainError("CONTEXT_ARTIFACT_SCOPE_DENIED", "Recovery owner differs", 403)
        row = recovery_io.store.guard(context.task.task_id, context.task.execution_epoch)
        if (
            str(row["run_id"]) != str(context.task.run_id)
            or row["grant_scope"] != context.grant.model_dump(mode="json")
        ):
            raise DomainError("CONTEXT_ARTIFACT_SCOPE_DENIED", "Recovery grant differs", 403)
        restored = 0
        for operation in recovery_io.store.operations(context.task.task_id):
            if operation["state"] != "known" or operation["kind"] not in {"tool", "baseline"}:
                continue
            intent = operation.get("intent", {})
            baseline = operation["kind"] == "baseline"
            if not baseline and intent.get("name") not in {"read_file", "run_tests"}:
                continue
            receipt = recovery_io._verified_receipt(operation)
            data = receipt.get("data", {}) if not baseline else {}
            tool_error = data.get("tool_error")
            not_executed = data.get("executed") is False or (
                isinstance(tool_error, dict) and tool_error.get("executed") is False
            )
            if not baseline and intent.get("name") == "run_tests" and not_executed:
                # A known correction/denial is retained evidence, but produced
                # no process output. It must neither be decoded as an execution
                # receipt nor introduce a source/log capability during restore.
                if (
                    receipt.get("status") not in {"error", "denied"}
                    or data.get("receipt") is not None
                    or data.get("artifact") is not None
                ):
                    raise DomainError(
                        "CONTEXT_ARTIFACT_SCOPE_DENIED", "Non-executed command has output", 403
                    )
                continue
            recovered_command = (
                not baseline and intent.get("name") == "run_tests"
                and not isinstance(data.get("artifact"), dict)
            )
            reference: dict[str, Any] | None
            if baseline or recovered_command:
                try:
                    execution = ExecutionReceipt.model_validate(
                        receipt if baseline else data.get("receipt")
                    )
                except ValidationError as exc:
                    raise DomainError(
                        "CONTEXT_ARTIFACT_SCOPE_DENIED", "Command receipt is invalid", 403
                    ) from exc
                if execution.provenance is None:
                    raise DomainError(
                        "CONTEXT_ARTIFACT_SCOPE_DENIED", "Baseline provenance is missing", 403
                    )
                provenance = execution.provenance.model_dump(mode="json")
                text = execution.stdout + execution.stderr
                # A reconciled process can become known before the outer
                # registry archived its log. Its PG receipt is the trusted
                # source; creating CAS bytes cannot rerun the command.
                self.artifacts.put(text.encode())
                complete = (
                    not baseline and data.get("truncated") is False
                    and data.get("dropped_bytes", 0) == 0
                    and data.get("total_log_bytes") == len(text.encode())
                )
                reference = {
                    "schema_version": "harness-context-reference-v2",
                    "task_id": context.task.task_id, "run_id": str(context.task.run_id),
                    "workspace_id": context.task.workspace_id, "actor": context.task.actor,
                    "repository": context.task.repository, "base_commit": context.task.base_commit,
                    "grant_hash": provenance["grant_hash"], "canonical_path": "", "path": "",
                    "artifact_hash": sha256_text(text), "kind": "log",
                    "execution_epoch": provenance["execution_epoch"],
                    "acceptance_revision": context.task.acceptance_revision,
                    "tree": execution.tree_hash, "lease_hash": provenance["lease_hash"],
                    "read_paths": provenance["read_paths"],
                    "protected_paths": provenance["protected_paths"],
                    "complete": complete,
                    "total_bytes": max(len(text.encode()), data.get("total_log_bytes", 0)),
                    "retained_bytes": len(text.encode()),
                    "provenance_kind": "command", "provenance": provenance,
                }
            else:
                reference = data.get("artifact")
            if not isinstance(reference, dict):
                continue
            if reference.get("kind") == "source" and (
                intent.get("name") != "read_file"
                or intent.get("arguments", {}).get("path") != reference.get("path")
            ):
                raise DomainError("CONTEXT_ARTIFACT_SCOPE_DENIED", "Source operation differs", 403)
            if (
                reference.get("kind") == "log"
                and not baseline and intent.get("name") != "run_tests"
            ):
                raise DomainError("CONTEXT_ARTIFACT_SCOPE_DENIED", "Log operation differs", 403)
            # V1 references came from the same verified PG tool receipt. That
            # implementation rejected every symlink, so its source path was
            # also its original canonical name. Preserve historical recovery.
            if "schema_version" not in reference:
                if (
                    reference.get("task_id") != context.task.task_id
                    or reference.get("workspace_id") != context.task.workspace_id
                ):
                    raise DomainError("CONTEXT_ARTIFACT_SCOPE_DENIED", "Legacy owner differs", 403)
                reference = {
                    **reference, "schema_version": "harness-context-reference-v2",
                    "run_id": str(context.task.run_id), "actor": context.task.actor,
                    "repository": context.task.repository, "base_commit": context.task.base_commit,
                    "grant_hash": canonical_hash(context.grant.model_dump(mode="json")),
                    "canonical_path": reference.get("path"),
                }
            self._validate_reference(reference, context)
            origin_epoch = reference.get("execution_epoch")
            if (
                type(origin_epoch) is not int
                or not 1 <= origin_epoch <= context.task.execution_epoch
            ):
                raise DomainError("CONTEXT_ARTIFACT_SCOPE_DENIED", "Artifact epoch differs", 403)
            content_hash = reference.get("artifact_hash")
            if not isinstance(content_hash, str):
                raise DomainError("CONTEXT_ARTIFACT_SCOPE_DENIED", "Artifact hash is missing", 403)
            self.artifacts.read(content_hash)
            key = (content_hash, reference["path"])
            if reference.get("kind") == "log":
                origin = reference.get("provenance")
                current = self._check(context, (reference["path"],))
                if (
                    not isinstance(origin, dict)
                    or reference.get("provenance_kind") != "command"
                    or origin.get("operation_id") != operation["operation_id"]
                    or origin.get("lease_hash") != reference.get("lease_hash")
                    or origin.get("grant_hash") != reference["grant_hash"]
                    or origin.get("actor") != context.task.actor
                    or origin.get("workspace_id") != context.task.workspace_id
                    or origin.get("repository") != context.task.repository
                    or tuple(reference.get("read_paths", ())) != current.read_paths
                    or tuple(reference.get("protected_paths", ())) != current.protected_paths
                ):
                    raise DomainError(
                        "CONTEXT_ARTIFACT_SCOPE_DENIED", "Log origin scope differs", 403
                    )
                self._recovered_log_leases[key] = current.lease_hash
            if key not in self._references:
                self._references[key] = deepcopy(reference)
                restored += 1
        return restored

    def recover_page(self, call: HarnessToolCall, context: ToolContext) -> dict[str, Any]:
        """Return safe correction data without honoring invented offsets.

        The caller formats a recoverable argument error. Foreign hash cursors
        remain scope violations, even when some other argument is malformed.
        """
        path, content_hash = call.arguments.get("path"), call.arguments.get("artifact_hash")
        if not isinstance(path, str) or not isinstance(content_hash, str):
            raise DomainError("HARNESS_TOOL_ARGUMENT", "Use path and an issued artifact_hash", 422)
        cursor = call.arguments.get("cursor")
        if isinstance(cursor, str):
            try:
                decoded = json.loads(base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)))
            except (ValueError, TypeError, UnicodeError):
                decoded = None
            if isinstance(decoded, dict) and "hash" in decoded and decoded["hash"] != content_hash:
                raise DomainError(
                    "CONTEXT_ARTIFACT_SCOPE_DENIED", "Cursor belongs to another artifact", 403
                )
        arguments = {"path": path, "artifact_hash": content_hash}
        first = self.page(
            HarnessToolCall(call_id=call.call_id, name="artifact_page", arguments=arguments),
            context,
        )
        continuation = first.data.get("continuation")
        return {
            "page": first.data, "retry_arguments": arguments,
            "next_arguments": {**arguments, "cursor": continuation} if continuation else None,
            "detail": (
                "Use only path, artifact_hash and the opaque continuation exactly as returned. "
                "Numeric cursors, offsets and extra fields are not accepted. "
                "The first bounded page is included; use next_arguments for the next page."
            ),
        }

    def read_lines(self, call: HarnessToolCall, context: ToolContext) -> HarnessToolResult:
        if call.name != "read_lines" or set(call.arguments) - {"path", "start_line", "max_lines"}:
            raise DomainError("HARNESS_TOOL_ARGUMENT", "Invalid line read arguments", 422)
        path = call.arguments.get("path")
        start_line = call.arguments.get("start_line")
        max_lines = call.arguments.get("max_lines", 40)
        if (
            not isinstance(path, str)
            or type(start_line) is not int
            or start_line < 1
            or type(max_lines) is not int
            or not 1 <= max_lines <= 60
        ):
            raise DomainError("HARNESS_TOOL_ARGUMENT", "Invalid line read arguments", 422)
        path = normalize_relative_path(path)
        lease = self._check(context, (path,))
        tree = self.workspace.snapshot().tree_hash
        if context.workspace_tree != tree:
            raise DomainError("CONTEXT_TREE_STALE", "Source tree changed", 409)
        def authorize(candidate: str) -> None:
            self._check(context, (candidate,))

        source = self.workspace.read(path, authorize=authorize)
        lines = source.splitlines(keepends=True)
        if start_line > max(1, len(lines)):
            raise DomainError("HARNESS_TOOL_ARGUMENT", "Line is outside source", 422)
        selected = "".join(lines[start_line - 1:start_line - 1 + max_lines])
        raw = selected.encode("utf-8")
        truncated = len(raw) > 6144
        if truncated:
            selected = raw[:6144].decode("utf-8", errors="ignore")
        if self.workspace.snapshot().tree_hash != tree:
            raise DomainError("CONTEXT_TREE_STALE", "Source tree changed", 409)
        self._unchanged(lease)
        payload = {
            "path": path,
            "start_line": start_line,
            "end_line": min(len(lines), start_line - 1 + max_lines),
            "total_lines": len(lines),
            "text": selected,
            "truncated": truncated,
            "source_tree": tree,
            "file_sha256": sha256_text(source),
        }
        return HarnessToolResult(
            call_id=call.call_id,
            status="ok",
            content=json.dumps(payload, ensure_ascii=False),
            data=payload,
        )

    def read_symbol(self, call: HarnessToolCall, context: ToolContext) -> HarnessToolResult:
        path, symbol = call.arguments.get("path"), call.arguments.get("symbol")
        limit = call.arguments.get("max_lines", 160)
        if (call.name != "read_symbol" or set(call.arguments) - {"path", "symbol", "max_lines"}
                or not isinstance(path, str) or not isinstance(symbol, str)
                or not symbol or len(symbol) > 300
                or any(not part.isidentifier() for part in symbol.split("."))
                or type(limit) is not int or not 1 <= limit <= 200):
            raise DomainError("HARNESS_TOOL_ARGUMENT", "Invalid symbol read arguments", 422)
        path = normalize_relative_path(path)
        lease = self._check(context, (path,))
        tree = self.workspace.snapshot().tree_hash
        if context.workspace_tree != tree:
            raise DomainError("CONTEXT_TREE_STALE", "Source tree changed", 409)
        def authorize(candidate: str) -> None:
            self._check(context, (candidate,))

        source = self.workspace.read(path, authorize=authorize)
        if not path.endswith(".py") or len(source.encode()) > 200_000:
            raise DomainError(
                "HARNESS_TOOL_ARGUMENT", "Use read_lines for this source format/size", 422,
            )
        try:
            parsed = ast.parse(source)
        except (SyntaxError, ValueError, RecursionError):
            raise DomainError(
                "HARNESS_TOOL_ARGUMENT", "Source cannot be parsed; use read_lines", 422,
            ) from None
        found: list[tuple[str, int, int]] = []
        definitions: list[tuple[str, int, int]] = []
        pending: list[tuple[ast.AST, str]] = [(parsed, "")]
        while pending:
            node, prefix = pending.pop()
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                qualified = prefix + node.name
                start = min([node.lineno, *[d.lineno for d in node.decorator_list]])
                definition = (qualified, start, node.end_lineno or node.lineno)
                definitions.append(definition)
                if qualified == symbol or ("." not in symbol and node.name == symbol):
                    found.append(definition)
                prefix = qualified + "."
            pending.extend((child, prefix) for child in ast.iter_child_nodes(node))
        if self.workspace.snapshot().tree_hash != tree:
            raise DomainError("CONTEXT_TREE_STALE", "Source tree changed", 409)
        self._unchanged(lease)
        common = {"path": path, "source_tree": tree, "file_sha256": sha256_text(source)}
        if len(found) != 1:
            payload: dict[str, Any] = {**common, "symbol": symbol, "correctable": True,
                       "matches": [{"symbol": name, "start_line": first, "end_line": last}
                                   for name, first, last in sorted(found)[:20]],
                       "detail": "Use a qualified name, search_files, or read_lines."}
            if not found and "." in symbol:
                # Suggest only an actual definition in this already authorized
                # file. This is a navigation hint, not Python MRO resolution or
                # a claim that an inherited/dynamic attribute exists.
                leaf = symbol.rsplit(".", 1)[-1]
                candidates = [name for name, _, _ in definitions
                              if name.rsplit(".", 1)[-1] == leaf]
                owner = symbol.rsplit(".", 1)[0]
                if len(candidates) == 1:
                    suggested = candidates[0]
                elif any(name == owner for name, _, _ in definitions):
                    suggested = owner
                else:
                    suggested = None
                if suggested is not None:
                    payload["retry_arguments"] = {"path": path, "symbol": suggested}
            return HarnessToolResult(call_id=call.call_id, status="error",
                code="HARNESS_SYMBOL_AMBIGUOUS" if found else "HARNESS_SYMBOL_NOT_FOUND",
                content=json.dumps(payload, ensure_ascii=False), data=payload)
        name, start, end = found[0]
        # Never split a source line without a usable continuation. Long lines
        # can be retrieved through the existing byte-bounded artifact reader.
        selected: list[str] = []
        size = 0
        for line in source.splitlines(keepends=True)[start - 1:min(end, start - 1 + limit)]:
            if size + len(line.encode()) > 12288:
                break
            selected.append(line)
            size += len(line.encode())
        last = start + len(selected) - 1
        payload = {**common, "symbol": name, "start_line": start, "end_line": last,
                   "symbol_end_line": end, "text": "".join(selected), "truncated": last < end,
                   "next_read": {"tool": "read_file", "arguments": {"path": path}}
                   if not selected else {"tool": "read_lines", "arguments": {
                       "path": path, "start_line": last + 1, "max_lines": 60}}
                   if last < end else None}
        if self.workspace.snapshot().tree_hash != tree:
            raise DomainError("CONTEXT_TREE_STALE", "Source tree changed", 409)
        self._unchanged(lease)
        return HarnessToolResult(call_id=call.call_id, status="ok",
                                 content=json.dumps(payload, ensure_ascii=False), data=payload)

    def list_dir(self, call: HarnessToolCall, context: ToolContext) -> HarnessToolResult:
        path = call.arguments.get("path", "")
        if call.name != "list_dir" or not isinstance(path, str):
            raise DomainError("HARNESS_TOOL_ARGUMENT", "Directory needs a path", 422)
        path = normalize_relative_path(path)
        lease = self._check(context, (path,))

        def authorize(candidate: str) -> None:
            self._check(context, (candidate,))
            self._unchanged(lease)

        entries = []
        for name in self.workspace.list_dir(path, authorize=authorize):
            child = f"{path}/{name}" if path else name
            if (_is_protected(child, lease.protected_paths)
                    or not _within(child, lease.read_paths)
                    or not context.grant.can_read(child)):
                continue
            try:
                # Listing an alias must not disclose a protected or out-of-scope
                # target. A direct request for that alias is still denied.
                authorize(child)
            except DomainError as exc:
                if exc.code not in {
                    "HARNESS_READ_DENIED", "HARNESS_PROTECTED_PATH_DENIED",
                    "WORKSPACE_SYMLINK_DENIED", "WORKSPACE_PATH_DENIED",
                    "WORKSPACE_FILE_NOT_FOUND",
                }:
                    raise
                continue
            entries.append(name)
        self._unchanged(lease)
        return HarnessToolResult(
            call_id=call.call_id, status="ok", content="\n".join(entries),
            data={"path": path, "entries": entries},
        )

    def search(self, call: HarnessToolCall, context: ToolContext) -> HarnessToolResult:
        path, query = call.arguments.get("path"), call.arguments.get("query")
        if not isinstance(path, str) or not isinstance(query, str):
            raise DomainError("HARNESS_TOOL_ARGUMENT", "Search needs path and query", 422)
        path = normalize_relative_path(path)
        lease = self._check(context, (path,))
        tree = self.workspace.snapshot().tree_hash
        if context.workspace_tree != tree:
            raise DomainError("CONTEXT_TREE_STALE", "Search tree changed", 409)
        if self._cache_tree != tree:
            self._search_cache.clear()
            self._cache_tree = tree
        key = canonical_hash(
            {
                "workspace": context.task.workspace_id,
                "task": context.task.task_id,
                "acceptance_revision": context.task.acceptance_revision,
                "objective": canonical_hash(context.task.objective),
                "task_kind": context.task.task_kind,
                "tree": tree,
                "query": query,
                "path": path,
                "limit": 100,
                "lease": lease.lease_hash,
                "grant": context.grant.model_dump(mode="json"),
            }
        )
        hit = key in self._search_cache
        if not hit:
            def authorize(candidate: str) -> None:
                self._check(context, (candidate,))
                self._unchanged(lease)

            matches = self.workspace.search(query, path=path, authorize=authorize)
            # A search of a directory must not disclose protected descendants
            # or paths outside a narrowed read lease/grant.
            matches = [
                match
                for match in matches
                if not _is_protected(match["path"], lease.protected_paths)
                and _within(match["path"], lease.read_paths)
                and context.grant.can_read(match["path"])
            ]
            self._search_cache[key] = deepcopy(matches)
        matches = deepcopy(self._search_cache[key])
        if self.workspace.snapshot().tree_hash != tree:
            self._search_cache.clear()
            raise DomainError("CONTEXT_TREE_STALE", "Search source tree changed", 409)
        self._unchanged(lease)
        return HarnessToolResult(
            call_id=call.call_id,
            status="ok",
            content=json.dumps(matches, ensure_ascii=False),
            data={"matches": matches, "cache_hit": hit, "cache_tree": tree, "cache_key": key},
        )


def artifact_trailer(reference: dict[str, Any]) -> str:
    content_hash = str(reference["artifact_hash"])
    return (
        "\n\n<<mgar-artifact path=" + json.dumps(reference["path"])
        + " hash=" + content_hash + ">>\n"
        "Use artifact_page or artifact_reread with path and artifact_hash for the original text.\n"
    )
