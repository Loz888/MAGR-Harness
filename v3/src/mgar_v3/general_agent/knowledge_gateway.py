"""Task-scoped, read-only retrieval across independently operated knowledge sources."""

from __future__ import annotations

import hashlib
import hmac
from base64 import b64decode, urlsafe_b64encode
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Annotated, Literal, Protocol, Self
from urllib.parse import urlsplit
from uuid import UUID

import httpx
from pydantic import (
    AwareDatetime,
    Field,
    JsonValue,
    StringConstraints,
    ValidationError,
    field_validator,
    model_validator,
)

from mgar_v3.domain import DomainError, Model, digest
from mgar_v3.general_agent.contracts import (
    Identifier,
    Revision,
    Sha256,
    SourceRef,
    TaskDomain,
    Text,
)
from mgar_v3.harness.tenancy import active_membership

if TYPE_CHECKING:
    from mgar_v3.coordination.engine import Engine
    from mgar_v3.general_agent.contracts import TaskSnapshot

Role = Literal["operator", "reader", "contributor"]
Trust = Literal["reference", "project_assertion"]
Envelope = Literal["untrusted_reference"]
Content = Annotated[str, StringConstraints(min_length=1, max_length=4096, pattern=r"^[^\x00]+$")]
Ticket = Annotated[str, StringConstraints(min_length=1, max_length=10000)]
_ACL_DENIED = {
    "ACTOR_DENIED",
    "HARNESS_MEMBER_REVOKED",
    "HARNESS_PROJECT_UNKNOWN",
    "KNOWLEDGE_REVOKED",
    "PERSONAL_MEMORY_NOT_FOUND",
}


class KnowledgeACL(Model):
    roles: Annotated[tuple[Role, ...], Field(min_length=1, max_length=3)]
    projects: Annotated[tuple[Identifier, ...], Field(max_length=64)] = ()
    require_account: bool = False


class KnowledgeSourceDescriptor(Model):
    schema_version: Literal["general-knowledge-source-v1"] = "general-knowledge-source-v1"
    source_id: Identifier
    domain: TaskDomain
    version: Identifier
    acl: KnowledgeACL
    search_tool: Identifier
    read_tool: Identifier
    citation_schema: Literal["general-knowledge-citation-v1"] = "general-knowledge-citation-v1"
    max_age_seconds: Annotated[int, Field(strict=True, ge=1, le=31_536_000)]
    trust: Trust

    @model_validator(mode="after")
    def distinct_tools(self) -> Self:
        if self.search_tool == self.read_tool:
            raise ValueError("Search and read tools must be distinct.")
        return self

    def task_ref(self) -> SourceRef:
        return SourceRef(
            kind="document", reference=f"knowledge:{self.source_id}", version=self.version
        )


class KnowledgePrincipal(Model):
    task_id: UUID
    task_revision: Revision
    tenant_id: Identifier
    project_id: Identifier
    account_id: UUID | None
    membership_id: UUID | None
    actor: Identifier
    role: Role


class KnowledgeLocation(Model):
    record_id: UUID
    snapshot_id: UUID | None = None
    uri: Text
    version: Text
    indexed_at: AwareDatetime
    locator: Annotated[dict[Identifier, JsonValue], Field(min_length=1, max_length=12)]
    content_sha256: Sha256

    @field_validator("uri")
    @classmethod
    def safe_uri(cls, value: str) -> str:
        parts = urlsplit(value)
        if (
            parts.scheme not in {"kb", "https"}
            or not parts.netloc
            or parts.username is not None
            or parts.password is not None
            or any(ord(char) < 33 or ord(char) == 127 for char in value)
        ):
            raise ValueError("Source URI must be a safe public or knowledge URI.")
        return value


class BackendHit(Model):
    location: KnowledgeLocation
    snippet: Text


class BackendSearch(Model):
    hits: Annotated[tuple[BackendHit, ...], Field(max_length=8)] = ()
    embedding: Text
    reranker: Text


class BackendDocument(Model):
    location: KnowledgeLocation
    text: Content


class KnowledgeAdapter(Protocol):
    @property
    def descriptor(self) -> KnowledgeSourceDescriptor: ...

    def search(self, principal: KnowledgePrincipal, query: str, top_k: int) -> BackendSearch: ...

    def read(
        self, principal: KnowledgePrincipal, location: KnowledgeLocation
    ) -> BackendDocument: ...


class KnowledgeQuery(Model):
    expected_revision: Revision
    query: Annotated[Text, Field(max_length=2000)]
    source_ids: Annotated[tuple[Identifier, ...], Field(min_length=1, max_length=8)]
    top_k: Annotated[int, Field(strict=True, ge=1, le=8)] = 4
    max_context_bytes: Annotated[int, Field(strict=True, ge=512, le=16384)] = 8192

    @model_validator(mode="after")
    def unique_sources(self) -> Self:
        if len(set(self.source_ids)) != len(self.source_ids):
            raise ValueError("Select each knowledge source once.")
        return self


class KnowledgeHit(Model):
    source_id: Identifier
    trust: Trust
    authority: Envelope = "untrusted_reference"
    location: KnowledgeLocation
    observed_at: AwareDatetime
    snippet: Text
    read_ticket: Ticket


class KnowledgeSourceState(Model):
    source_id: Identifier
    status: Literal["ready", "denied", "stale", "unavailable", "timeout"]
    reason: Literal["scope", "expired", "backend_failure", "backend_timeout"] | None = None
    embedding: Text | None = None
    reranker: Text | None = None


class KnowledgeSearchResult(Model):
    hits: tuple[KnowledgeHit, ...]
    sources: tuple[KnowledgeSourceState, ...]


class KnowledgeReadRequest(Model):
    expected_revision: Revision
    read_ticket: Ticket


class KnowledgeCiteRequest(KnowledgeReadRequest):
    excerpt: Text


class KnowledgeEvidence(Model):
    source_id: Identifier
    trust: Trust
    authority: Envelope = "untrusted_reference"
    location: KnowledgeLocation
    observed_at: AwareDatetime
    text: Content


class KnowledgeCitation(Model):
    schema_version: Literal["general-knowledge-citation-v1"] = "general-knowledge-citation-v1"
    source_id: Identifier
    trust: Trust
    authority: Envelope = "untrusted_reference"
    location: KnowledgeLocation
    observed_at: AwareDatetime
    excerpt: Text

    def source_ref(self) -> SourceRef:
        return SourceRef(
            kind="document", reference=self.location.uri, version=self.location.version
        )


class _ReadBinding(Model):
    principal: KnowledgePrincipal
    descriptor_sha256: Sha256
    source_id: Identifier
    location: KnowledgeLocation
    expires_at: AwareDatetime


def _scope_denied() -> DomainError:
    return DomainError("GENERAL_KNOWLEDGE_DENIED", "Knowledge is outside this task's scope.", 403)


def _unavailable() -> DomainError:
    return DomainError("GENERAL_KNOWLEDGE_UNAVAILABLE", "The knowledge source is unavailable.", 503)


def _encoded_size(value: Model) -> int:
    return len(value.model_dump_json().encode())


class KnowledgeGateway:
    def __init__(self, engine: Engine, sources: Sequence[KnowledgeAdapter]) -> None:
        self.engine = engine
        self._sources = {source.descriptor.source_id: source for source in sources}
        if len(self._sources) != len(sources) or len(sources) > 64:
            raise ValueError("Knowledge source IDs must be unique and bounded.")

    def _principal(
        self,
        task_id: UUID,
        revision: int,
        account_id: UUID | None,
    ) -> tuple[TaskSnapshot, KnowledgePrincipal]:
        with self.engine.transaction() as conn:
            task = self.engine.general_tasks.lock_current(conn, task_id)
            if task.project_id is None:
                raise DomainError("GENERAL_TASK_NOT_FOUND", "任务不存在。", 404)
            actor = self.engine.settings.local_actor
            role: Role = "operator"
            membership_id = None
            if account_id is not None:
                account = conn.execute(
                    "SELECT username,is_owner FROM app.web_accounts WHERE user_id=%s",
                    (account_id,),
                ).fetchone()
                if account is None:
                    raise _scope_denied()
                if not account["is_owner"]:
                    member = active_membership(conn, task.project_id, user_id=account_id)
                    if member is None or member["membership_id"] != task.membership_id:
                        raise DomainError("GENERAL_TASK_NOT_FOUND", "任务不存在。", 404)
                    actor, role, membership_id = (
                        account["username"],
                        member["role"],
                        member["membership_id"],
                    )
            if task.revision != revision:
                raise DomainError("GENERAL_TASK_STALE", "Task revision changed.", 409)
            if task.state not in {"ready", "running", "waiting", "verifying"}:
                raise _scope_denied()
            if task.spec.deadline is not None and task.spec.deadline <= datetime.now(UTC):
                raise _scope_denied()
            return task, KnowledgePrincipal(
                task_id=task_id,
                task_revision=revision,
                tenant_id=self.engine.settings.project_id,
                project_id=task.project_id,
                account_id=account_id,
                membership_id=membership_id,
                actor=actor,
                role=role,
            )

    def _current(self, principal: KnowledgePrincipal) -> None:
        _, accepted = self._principal(
            principal.task_id,
            principal.task_revision,
            principal.account_id,
        )
        if accepted != principal:
            raise _scope_denied()

    @staticmethod
    def _authorized(
        task: TaskSnapshot,
        principal: KnowledgePrincipal,
        descriptor: KnowledgeSourceDescriptor,
    ) -> bool:
        acl = descriptor.acl
        return (
            descriptor.task_ref() in task.spec.source_refs
            and principal.role in acl.roles
            and (not acl.projects or principal.project_id in acl.projects)
            and (not acl.require_account or principal.account_id is not None)
        )

    def sources(
        self,
        task_id: UUID,
        revision: int,
        account_id: UUID | None,
    ) -> tuple[KnowledgeSourceDescriptor, ...]:
        task, principal = self._principal(task_id, revision, account_id)
        return tuple(
            source.descriptor
            for source in self._sources.values()
            if self._authorized(task, principal, source.descriptor)
        )

    def _signature(self, payload: bytes) -> str:
        secret = self.engine.settings.local_token.get_secret_value().encode()
        return hmac.new(secret, b"ga11-read-v1\0" + payload, hashlib.sha256).hexdigest()

    def _ticket(
        self,
        principal: KnowledgePrincipal,
        descriptor: KnowledgeSourceDescriptor,
        location: KnowledgeLocation,
        now: datetime,
    ) -> str:
        content = (
            _ReadBinding(
                principal=principal,
                descriptor_sha256=digest(descriptor.model_dump(mode="json")),
                source_id=descriptor.source_id,
                location=location,
                expires_at=now + timedelta(minutes=10),
            )
            .model_dump_json()
            .encode()
        )
        return urlsafe_b64encode(content).decode() + "." + self._signature(content)

    def _binding(self, value: str) -> _ReadBinding:
        try:
            if len(value) > 10000:
                raise ValueError
            data, signature = value.split(".")
            payload = b64decode(data, altchars=b"-_", validate=True)
            if not hmac.compare_digest(self._signature(payload).encode(), signature.encode()):
                raise ValueError
            binding = _ReadBinding.model_validate_json(payload)
            if binding.expires_at <= datetime.now(UTC):
                raise ValueError
            return binding
        except (ValueError, UnicodeError):
            raise DomainError(
                "GENERAL_KNOWLEDGE_TICKET", "Invalid or expired read ticket.", 403
            ) from None

    @staticmethod
    def _fresh(
        descriptor: KnowledgeSourceDescriptor, location: KnowledgeLocation, now: datetime
    ) -> bool:
        return (
            timedelta(0)
            <= now - location.indexed_at
            <= timedelta(seconds=descriptor.max_age_seconds)
        )

    def search(
        self,
        task_id: UUID,
        query: KnowledgeQuery,
        account_id: UUID | None,
    ) -> KnowledgeSearchResult:
        task, principal = self._principal(task_id, query.expected_revision, account_id)
        hits: list[KnowledgeHit] = []
        states: list[KnowledgeSourceState] = []
        remaining = query.max_context_bytes
        for source_id in query.source_ids:
            adapter = self._sources.get(source_id)
            if adapter is None or not self._authorized(task, principal, adapter.descriptor):
                states.append(
                    KnowledgeSourceState(source_id=source_id, status="denied", reason="scope")
                )
                continue
            descriptor = adapter.descriptor
            self._current(principal)
            try:
                result = adapter.search(principal, query.query, query.top_k)
            except (TimeoutError, httpx.TimeoutException):
                states.append(
                    KnowledgeSourceState(
                        source_id=source_id,
                        status="timeout",
                        reason="backend_timeout",
                    )
                )
                continue
            except (DomainError, ValidationError, OSError, httpx.HTTPError) as exc:
                states.append(
                    KnowledgeSourceState(
                        source_id=source_id,
                        status="denied"
                        if isinstance(exc, DomainError) and exc.code in _ACL_DENIED
                        else "unavailable",
                        reason="scope"
                        if isinstance(exc, DomainError) and exc.code in _ACL_DENIED
                        else "backend_failure",
                    )
                )
                continue
            now = datetime.now(UTC)
            fresh = [
                candidate
                for candidate in result.hits[: query.top_k]
                if self._fresh(descriptor, candidate.location, now)
            ]
            states.append(
                KnowledgeSourceState(
                    source_id=source_id,
                    status="stale" if result.hits and not fresh else "ready",
                    reason="expired" if result.hits and not fresh else None,
                    embedding=result.embedding,
                    reranker=result.reranker,
                )
            )
            for candidate in fresh:
                if _encoded_size(candidate.location) > 2048:
                    continue
                snippet = candidate.snippet.encode()[:remaining].decode(errors="ignore").strip()
                if not snippet:
                    break
                hits.append(
                    KnowledgeHit(
                        source_id=source_id,
                        location=candidate.location,
                        observed_at=now,
                        trust=descriptor.trust,
                        snippet=snippet,
                        read_ticket=self._ticket(principal, descriptor, candidate.location, now),
                    )
                )
                remaining -= len(snippet.encode())
        self._current(principal)
        return KnowledgeSearchResult(hits=tuple(hits), sources=tuple(states))

    def read(
        self,
        task_id: UUID,
        request: KnowledgeReadRequest,
        account_id: UUID | None,
    ) -> KnowledgeEvidence:
        task, principal = self._principal(task_id, request.expected_revision, account_id)
        binding = self._binding(request.read_ticket)
        adapter = self._sources.get(binding.source_id)
        if (
            binding.principal != principal
            or adapter is None
            or not self._authorized(task, principal, adapter.descriptor)
            or binding.descriptor_sha256 != digest(adapter.descriptor.model_dump(mode="json"))
        ):
            raise _scope_denied()
        if not self._fresh(adapter.descriptor, binding.location, datetime.now(UTC)):
            raise DomainError(
                "GENERAL_KNOWLEDGE_STALE", "The source is past its freshness limit.", 409
            )
        try:
            document = adapter.read(principal, binding.location)
        except (TimeoutError, httpx.TimeoutException):
            raise DomainError(
                "GENERAL_KNOWLEDGE_TIMEOUT", "The knowledge source timed out.", 504
            ) from None
        except DomainError as exc:
            if exc.code in _ACL_DENIED or exc.code == "NOT_FOUND":
                raise _scope_denied() from None
            raise _unavailable() from None
        except (ValidationError, OSError, httpx.HTTPError):
            raise _unavailable() from None
        self._current(principal)
        now = datetime.now(UTC)
        if (
            document.location != binding.location
            or hashlib.sha256(document.text.encode()).hexdigest() != binding.location.content_sha256
        ):
            raise DomainError(
                "GENERAL_KNOWLEDGE_CHANGED", "Source version changed. Search again.", 409
            )
        if not self._fresh(adapter.descriptor, document.location, now):
            raise DomainError(
                "GENERAL_KNOWLEDGE_STALE", "The source is past its freshness limit.", 409
            )
        return KnowledgeEvidence(
            source_id=binding.source_id,
            location=document.location,
            observed_at=now,
            trust=adapter.descriptor.trust,
            text=document.text,
        )

    def cite(
        self,
        task_id: UUID,
        request: KnowledgeCiteRequest,
        account_id: UUID | None,
    ) -> KnowledgeCitation:
        evidence = self.read(task_id, request, account_id)
        if request.excerpt not in evidence.text:
            raise DomainError(
                "GENERAL_KNOWLEDGE_EXCERPT", "The excerpt does not match the source.", 422
            )
        return KnowledgeCitation(
            source_id=evidence.source_id,
            trust=evidence.trust,
            location=evidence.location,
            observed_at=evidence.observed_at,
            excerpt=request.excerpt,
        )
