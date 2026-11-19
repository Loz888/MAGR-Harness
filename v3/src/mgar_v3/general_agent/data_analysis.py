"""GA07 execution and evidence adapters for immutable, scoped datasets."""

from __future__ import annotations

import base64
import hashlib
import json
import subprocess
import sys
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from pydantic import ValidationError

from mgar_v3.domain import DomainError, Model
from mgar_v3.evidence.artifacts import ArtifactStore
from mgar_v3.general_agent.contracts import Sha256, SourceRef, TaskSnapshot
from mgar_v3.general_agent.data_contracts import (
    DATA_PROTOCOL,
    MAX_INPUT,
    DataAnalysisResult,
    ResultFormat,
)
from mgar_v3.general_agent.data_contracts import (
    DataAnalysisRequest as DataAnalysisRequest,
)
from mgar_v3.general_agent.graph import BranchValue, GraphNode
from mgar_v3.general_agent.graph_store import NodeClaim
from mgar_v3.general_agent.verification_contracts import EvidenceObservation, Numeric

_WORKER_ARTIFACTS = {"numeric", "csv", "xlsx", "chart", "report"}
_WORKER_BYTE_LIMIT = 4_000_000
_MAX_SECONDS = 8.0


class _WorkerOutput(Model):
    protocol: Literal["general-data-analysis-v1"]
    format: Literal["csv", "json", "xlsx"]
    input_sha256: Sha256
    request: DataAnalysisRequest
    counts: dict[str, int]
    overview: dict[str, dict[str, Any]]
    normalized: str
    artifacts: dict[ResultFormat, str]


def _implementation_hash() -> str:
    fingerprint = hashlib.sha256()
    for module in ("data_analysis", "data_contracts", "data_formats", "data_worker"):
        content = Path(__file__).with_name(module + ".py").read_bytes()
        fingerprint.update(module.encode() + b"\0" + content + b"\0")
    return fingerprint.hexdigest()


def _error(code: str, message: str, status: int = 422) -> DomainError:
    return DomainError(code, message, status)


class DataAnalysisExecutor:
    def __init__(self, artifacts: ArtifactStore) -> None:
        self.artifacts = artifacts
        self.implementation_sha256 = _implementation_hash()

    def _read_input(self, request: DataAnalysisRequest) -> bytes:
        content_hash = request.source.version
        if content_hash is None:
            raise _error("GENERAL_DATA_SOURCE", "A pinned source hash is required.")
        try:
            maximum = min(MAX_INPUT, self.artifacts.max_bytes)
            if not 1 <= self.artifacts.path(content_hash).stat().st_size <= maximum:
                raise _error("GENERAL_DATA_LIMITS", "Dataset size exceeds the analysis limit.")
        except FileNotFoundError as exc:
            raise _error(
                "GENERAL_DATA_SOURCE", "The approved dataset is unavailable.", 404
            ) from exc
        raw = self.artifacts.read(content_hash)
        if not 1 <= len(raw) <= maximum:
            raise _error("GENERAL_DATA_LIMITS", "Dataset size exceeds the analysis limit.")
        return raw

    def execute(
        self, request: DataAnalysisRequest, *, timeout_seconds: float = _MAX_SECONDS
    ) -> DataAnalysisResult:
        """Analyze one trusted, CAS-pinned request without shell, file or URL arguments."""
        if not 0 < timeout_seconds <= _MAX_SECONDS:
            raise _error("GENERAL_DATA_DEADLINE", "Data analysis time budget is invalid.", 409)
        raw = self._read_input(request)
        payload = json.dumps(
            {
                "request": request.model_dump(mode="json"),
                "input": base64.b64encode(raw).decode("ascii"),
            }
        ).encode()
        try:
            process = subprocess.run(
                [sys.executable, "-I", "-m", "mgar_v3.general_agent.data_worker"],
                input=payload,
                capture_output=True,
                env={},
                timeout=timeout_seconds,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise _error(
                "GENERAL_DATA_DEADLINE", "Data analysis exceeded its time budget.", 409
            ) from exc
        if process.returncode == 2:
            raise _error(
                "GENERAL_DATA_INPUT", "Invalid, unsupported or oversized dataset or analysis."
            )
        if process.returncode != 0 or len(process.stdout) > _WORKER_BYTE_LIMIT:
            raise _error("GENERAL_DATA_WORKER", "The bounded analysis worker stopped.", 409)
        try:
            result = _WorkerOutput.model_validate_json(process.stdout)
            normalized_bytes = base64.b64decode(result.normalized, validate=True)
            output_bytes = {
                kind: base64.b64decode(encoded, validate=True)
                for kind, encoded in result.artifacts.items()
            }
        except (ValidationError, ValueError) as exc:
            raise _error(
                "GENERAL_DATA_WORKER", "The worker returned an invalid bundle.", 409
            ) from exc
        if (
            result.request != request
            or result.input_sha256 != request.source.version
            or set(result.artifacts) != _WORKER_ARTIFACTS
        ):
            raise _error(
                "GENERAL_DATA_WORKER", "Analysis result differs from its input binding.", 409
            )
        normalized = self.artifacts.put(normalized_bytes)
        published = {kind: self.artifacts.put(content) for kind, content in output_bytes.items()}
        manifest = {
            "protocol": DATA_PROTOCOL,
            "implementation_sha256": self.implementation_sha256,
            "input_sha256": result.input_sha256,
            "format": result.format,
            "request": request.model_dump(mode="json"),
            "counts": result.counts,
            "schema_overview": result.overview,
            "normalized_sha256": normalized,
            "artifacts": dict(published),
        }
        published["manifest"] = self.artifacts.put(
            json.dumps(manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
        )
        return DataAnalysisResult(
            source=request.source,
            input_sha256=result.input_sha256,
            normalized_sha256=normalized,
            implementation_sha256=self.implementation_sha256,
            input_rows=result.counts["input_rows"],
            selected_rows=result.counts["selected_rows"],
            sample_count=result.counts["sample_count"],
            artifacts=published,
        )

    def handler(
        self, request: DataAnalysisRequest, *, outputs: Mapping[str, ResultFormat]
    ) -> Callable[[TaskSnapshot, GraphNode, NodeClaim], dict[str, BranchValue]]:
        """Adapt a service-authored plan to one current GA02 executor claim."""
        requested = dict(outputs)
        if set(requested.values()) - _WORKER_ARTIFACTS - {"manifest"}:
            raise _error("GENERAL_DATA_SCOPE", "Unknown analysis output format.", 422)

        def run(task: TaskSnapshot, node: GraphNode, claim: NodeClaim) -> dict[str, BranchValue]:
            if (
                request.source not in task.spec.source_refs
                or node.domain != "data"
                or claim.capability_id != "data.analyze"
                or claim.task_id != task.task_id
                or claim.task_revision != task.revision
                or claim.node_id != node.id
                or set(requested) != set(node.outputs)
            ):
                raise _error(
                    "GENERAL_DATA_SCOPE", "Dataset or format is outside its node scope.", 403
                )
            expiry = (claim.expires_at - datetime.now(UTC)).total_seconds()
            result = self.execute(request, timeout_seconds=min(expiry, _MAX_SECONDS))
            return {key: result.artifacts[format_name] for key, format_name in requested.items()}

        return run

    def verify_manifest(
        self,
        request: DataAnalysisRequest,
        manifest_sha256: str,
        *,
        timeout_seconds: float = _MAX_SECONDS,
    ) -> DataAnalysisResult:
        """Recompute a declared bundle before deriving evidence from it."""
        result = self.execute(request, timeout_seconds=timeout_seconds)
        if result.artifacts["manifest"] != manifest_sha256:
            raise _error("GENERAL_DATA_EVIDENCE", "Reproduction differs from the declared bundle.")
        return result

    def verify_bound_manifest(
        self,
        manifest_sha256: str,
        allowed_sources: tuple[SourceRef, ...],
        *,
        deadline_at: datetime,
    ) -> tuple[DataAnalysisRequest, DataAnalysisResult]:
        """Restore a predecessor's numeric plan under the receiving node's source scope."""
        raw = self.artifacts.read(manifest_sha256)
        if len(raw) > MAX_INPUT:
            raise _error("GENERAL_DATA_EVIDENCE", "The manifest exceeds its byte limit.")
        try:
            payload = json.loads(raw)
            if not isinstance(payload, dict) or payload.get("protocol") != DATA_PROTOCOL:
                raise ValueError("Unknown manifest type.")
            request = DataAnalysisRequest.model_validate(payload.get("request"))
        except (ValueError, RecursionError) as exc:
            raise _error("GENERAL_DATA_EVIDENCE", "The analysis manifest is invalid.") from exc
        if request.source not in allowed_sources:
            raise _error("GENERAL_DATA_SCOPE", "The source is outside the task's data scope.", 403)
        timeout_seconds = min(_MAX_SECONDS, (deadline_at - datetime.now(UTC)).total_seconds())
        return request, self.verify_manifest(
            request, manifest_sha256, timeout_seconds=timeout_seconds
        )

    @staticmethod
    def numeric_evidence(
        result: DataAnalysisResult,
        request: DataAnalysisRequest,
        *,
        output_id: str,
        collector: str,
        observed_at: datetime,
    ) -> tuple[Numeric, EvidenceObservation]:
        """Supply normalized numeric evidence after the authorized transformation."""
        if result.source != request.source or result.input_sha256 != request.source.version:
            raise _error("GENERAL_DATA_SCOPE", "Normalized evidence differs from the source.", 403)
        return (
            Numeric(
                id="numeric." + hashlib.sha256(output_id.encode()).hexdigest(),
                output_id=output_id,
                kind="numeric",
                source=result.source,
                source_sha256=result.normalized_sha256,
                field=request.field,
                formula=request.formula,
            ),
            EvidenceObservation(
                source=result.source,
                collector=collector,
                observed_at=observed_at,
                content_sha256=result.normalized_sha256,
            ),
        )
