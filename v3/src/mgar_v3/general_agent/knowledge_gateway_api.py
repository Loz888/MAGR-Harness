"""Authenticated task endpoints for explicitly selected knowledge sources."""

from collections.abc import Callable
from typing import Annotated
from uuid import UUID

from fastapi import FastAPI, Query, Request, Response

from mgar_v3.general_agent.knowledge_gateway import (
    KnowledgeCitation,
    KnowledgeCiteRequest,
    KnowledgeEvidence,
    KnowledgeGateway,
    KnowledgeQuery,
    KnowledgeReadRequest,
    KnowledgeSearchResult,
    KnowledgeSourceDescriptor,
)

_BASE = "/api/v3/general/tasks/{task_id}/knowledge"


def _gateway(request: Request) -> KnowledgeGateway:
    gateway: KnowledgeGateway = request.app.state.engine.general_knowledge
    return gateway


def register_knowledge_gateway_routes(
    app: FastAPI,
    account_user: Callable[[Request], UUID | None],
) -> None:
    @app.get(_BASE + "/sources")
    def sources(
        request: Request,
        response: Response,
        task_id: UUID,
        expected_revision: Annotated[int, Query(ge=1, le=2**63 - 1)],
    ) -> tuple[KnowledgeSourceDescriptor, ...]:
        response.headers["Cache-Control"] = "no-store"
        return _gateway(request).sources(task_id, expected_revision, account_user(request))

    @app.post(_BASE + "/search")
    def search(
        request: Request,
        response: Response,
        task_id: UUID,
        body: KnowledgeQuery,
    ) -> KnowledgeSearchResult:
        response.headers["Cache-Control"] = "no-store"
        return _gateway(request).search(task_id, body, account_user(request))

    @app.post(_BASE + "/read")
    def read(
        request: Request,
        response: Response,
        task_id: UUID,
        body: KnowledgeReadRequest,
    ) -> KnowledgeEvidence:
        response.headers["Cache-Control"] = "no-store"
        return _gateway(request).read(task_id, body, account_user(request))

    @app.post(_BASE + "/cite")
    def cite(
        request: Request,
        response: Response,
        task_id: UUID,
        body: KnowledgeCiteRequest,
    ) -> KnowledgeCitation:
        response.headers["Cache-Control"] = "no-store"
        return _gateway(request).cite(task_id, body, account_user(request))
