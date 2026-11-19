"""Native adapters for the R4 technical index and GA10 project memory."""

from __future__ import annotations

import hashlib
from collections import Counter
from datetime import UTC, datetime
from typing import TYPE_CHECKING
from uuid import UUID

from mgar_v3.domain import DomainError
from mgar_v3.general_agent.knowledge_gateway import (
    BackendDocument,
    BackendHit,
    BackendSearch,
    KnowledgeACL,
    KnowledgeAdapter,
    KnowledgeLocation,
    KnowledgePrincipal,
    KnowledgeSourceDescriptor,
)
from mgar_v3.rag.domain import Search
from mgar_v3.rag.embedding import Embedder, normalize
from mgar_v3.rag.rank import bm25, rrf
from mgar_v3.rag.text import terms

if TYPE_CHECKING:
    from mgar_v3.coordination.engine import Engine, Row
    from mgar_v3.general_agent.personal_memory import PersonalMemory, PersonalMemoryStore
    from mgar_v3.knowledge.store import Knowledge


def _changed() -> DomainError:
    return DomainError("KNOWLEDGE_REVOKED", "The source is absent, revoked or has changed.", 403)


class TechnicalKnowledge:
    def __init__(self, index: Knowledge, *, projects: tuple[str, ...]) -> None:
        if not projects:
            raise ValueError("The technical index needs an explicit project allowlist.")
        self.index = index
        self._descriptor = KnowledgeSourceDescriptor(
            source_id="technical-docs",
            domain="research",
            version="1",
            acl=KnowledgeACL(roles=("operator", "reader", "contributor"), projects=projects),
            search_tool="technical.search",
            read_tool="technical.read",
            max_age_seconds=30 * 24 * 3600,
            trust="reference",
        )

    @property
    def descriptor(self) -> KnowledgeSourceDescriptor:
        return self._descriptor

    @staticmethod
    def _location(row: Row, snapshot_id: UUID, indexed_at: str | datetime) -> KnowledgeLocation:
        return KnowledgeLocation.model_validate(
            {
                "record_id": row["chunk_id"],
                "snapshot_id": snapshot_id,
                "uri": row["source_uri"],
                "version": row["source_version"],
                "indexed_at": indexed_at,
                "locator": row["locator"],
                "content_sha256": row["content_hash"],
            }
        )

    def search(self, principal: KnowledgePrincipal, query: str, top_k: int) -> BackendSearch:
        # R4 independently applies actor, snapshot and document ACLs before embedding.
        results = self.index.search(Search(query=query, top_k=top_k), actor=principal.actor)
        config = self.index.config()
        snapshot_id = UUID(results["snapshot_id"])
        return BackendSearch(
            hits=tuple(
                BackendHit(
                    location=self._location(row, snapshot_id, results["observed_at"]),
                    snippet=row["snippet"],
                )
                for row in results["hits"]
            ),
            embedding=config["embedding_model"],
            reranker=config["reranker"],
        )

    def read(self, principal: KnowledgePrincipal, location: KnowledgeLocation) -> BackendDocument:
        if location.snapshot_id is None:
            raise _changed()
        actor = principal.actor
        snapshot, _, scope = self.index.eligible(location.snapshot_id, actor)
        row = self.index.read(location.record_id, location.snapshot_id, actor)
        if self.index.eligible(location.snapshot_id, actor)[2] != scope:
            raise _changed()
        return BackendDocument(
            location=self._location(row, location.snapshot_id, snapshot["created_at"]),
            text=row["text"],
        )


class ProjectKnowledge:
    def __init__(self, memory: PersonalMemoryStore, *, embedder: Embedder | None = None) -> None:
        self.memory = memory
        self.embedder = embedder
        self._descriptor = KnowledgeSourceDescriptor(
            source_id="project-memory",
            domain="office",
            version="1",
            acl=KnowledgeACL(roles=("operator", "reader", "contributor"), require_account=True),
            search_tool="project.search",
            read_tool="project.read",
            max_age_seconds=365 * 24 * 3600,
            trust="project_assertion",
        )

    @property
    def descriptor(self) -> KnowledgeSourceDescriptor:
        return self._descriptor

    @staticmethod
    def _location(record: PersonalMemory) -> KnowledgeLocation:
        return KnowledgeLocation(
            record_id=record.memory_id,
            uri=f"kb://project-memory/{record.project_id}/{record.memory_id}",
            version=str(record.version),
            indexed_at=record.version_created_at,
            locator={
                "subject": record.subject,
                "kind": record.kind,
                "provenance": record.source.kind,
            },
            content_sha256=hashlib.sha256(record.content.encode()).hexdigest(),
        )

    def search(self, principal: KnowledgePrincipal, query: str, top_k: int) -> BackendSearch:
        if principal.account_id is None:
            raise _changed()
        records = [
            item
            for item in self.memory.recall(
                principal.account_id, project=principal.project_id, limit=100
            )
            if item.kind in {"project_fact", "experience"}
        ]
        corpus = [item.subject + " " + item.content for item in records]
        by_id = {str(item.memory_id): item for item in records}
        documents = []
        for item, text in zip(records, corpus, strict=True):
            tokens = terms(text)
            documents.append(
                {
                    "chunk_id": item.memory_id,
                    "token_count": len(tokens),
                    "terms": dict(Counter(tokens)),
                }
            )
        lexical = [(key, score) for key, score in bm25(query, documents) if score > 0]
        embedding = "lexical-bm25"
        ranking = lexical
        if self.embedder is not None:
            embedding = self.embedder.model_id
            if records:
                vectors = self.embedder.embed([query, *corpus])
                if len(vectors) != len(records) + 1:
                    raise DomainError("EMBEDDING_DIMENSION", "Project embedding response changed.")
                common = normalize(vectors[0], self.embedder.dimension)
                dense = []
                for record, vector in zip(records, vectors[1:], strict=True):
                    values = normalize(vector, self.embedder.dimension)
                    overlap = sum(a * b for a, b in zip(common, values, strict=True))
                    if overlap > 0:
                        dense.append((str(record.memory_id), overlap))
                dense.sort(key=lambda item: (-item[1], item[0]))
                ranking = rrf(lexical, dense)
        hits = []
        for key, _ in ranking[:top_k]:
            row = by_id[key]
            # Permissions, validity and the memory revision can change during ranking.
            current = self.memory.get(principal.account_id, row.memory_id)
            if current != row or not self._active(current, principal):
                continue
            hits.append(BackendHit(location=self._location(row), snippet=row.content))
        return BackendSearch(hits=tuple(hits), embedding=embedding, reranker="none")

    @staticmethod
    def _active(record: PersonalMemory, principal: KnowledgePrincipal) -> bool:
        return (
            record.project_id == principal.project_id
            and record.tenant_id == principal.tenant_id
            and record.user_id == principal.account_id
            and record.kind in {"project_fact", "experience"}
            and record.status == "active"
            and (record.valid_until is None or record.valid_until > datetime.now(UTC))
        )

    def read(self, principal: KnowledgePrincipal, location: KnowledgeLocation) -> BackendDocument:
        if principal.account_id is None or location.snapshot_id is not None:
            raise _changed()
        record = self.memory.get(principal.account_id, location.record_id)
        if not self._active(record, principal):
            raise _changed()
        return BackendDocument(location=self._location(record), text=record.content)


def native_sources(engine: Engine) -> tuple[KnowledgeAdapter, ...]:
    projects = tuple(
        dict.fromkeys(
            (
                engine.settings.project_id,
                *engine.settings.knowledge_gateway_projects,
            )
        )
    )
    return (
        TechnicalKnowledge(engine.knowledge, projects=projects),
        ProjectKnowledge(engine.personal_memory),
    )
