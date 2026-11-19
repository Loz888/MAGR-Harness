"""Authenticated account interface for GA10 personal memory."""

from collections.abc import Callable
from typing import Annotated
from uuid import UUID

from fastapi import FastAPI, Query, Request, Response
from fastapi.responses import JSONResponse

from mgar_v3.domain import Model
from mgar_v3.general_agent.contracts import Identifier, Revision
from mgar_v3.general_agent.personal_memory import (
    CreatePersonalMemory,
    PersonalMemory,
    PersonalMemoryStore,
    RevisePersonalMemory,
)

_BASE = "/api/v3/general/memory"


class Remember(CreatePersonalMemory):
    completed_task: UUID | None = None


class Revoke(Model):
    expected_version: Revision


def _store(request: Request) -> PersonalMemoryStore:
    store: PersonalMemoryStore = request.app.state.engine.personal_memory
    return store


def _response(body: PersonalMemory, status: int = 200) -> Response:
    return JSONResponse(
        body.model_dump(mode="json"), status_code=status, headers={"Cache-Control": "no-store"}
    )


def register_personal_memory_routes(
    app: FastAPI,
    account_user: Callable[[Request], UUID],
) -> None:
    @app.post(_BASE, status_code=201, response_model=PersonalMemory)
    def create(request: Request, body: Remember) -> Response:
        user = account_user(request)
        return _response(
            _store(request).create(user, body, completed_task=body.completed_task), 201
        )

    @app.get(_BASE, response_model=list[PersonalMemory])
    def recall(
        request: Request,
        project_id: Identifier | None = None,
        source_project_id: Identifier | None = None,
        include_personal: bool = False,
        q: Annotated[str, Query(max_length=200)] = "",
        limit: Annotated[int, Query(ge=1, le=100)] = 20,
    ) -> Response:
        user = account_user(request)
        records = _store(request).recall(
            user,
            project=project_id,
            source_project=source_project_id,
            include_personal=include_personal,
            query=q,
            limit=limit,
        )
        return JSONResponse(
            [row.model_dump(mode="json") for row in records], headers={"Cache-Control": "no-store"}
        )

    @app.get(_BASE + "/{memory_id}", response_model=PersonalMemory)
    def read(
        request: Request,
        memory_id: UUID,
        version: Annotated[int | None, Query(ge=1, le=2**63 - 1)] = None,
    ) -> Response:
        user = account_user(request)
        return _response(_store(request).get(user, memory_id, version=version))

    @app.patch(_BASE + "/{memory_id}", response_model=PersonalMemory)
    def revise(request: Request, memory_id: UUID, body: RevisePersonalMemory) -> Response:
        user = account_user(request)
        return _response(_store(request).revise(user, memory_id, body))

    @app.post(_BASE + "/{memory_id}/revoke", response_model=PersonalMemory)
    def revoke(request: Request, memory_id: UUID, body: Revoke) -> Response:
        user = account_user(request)
        return _response(_store(request).revoke(user, memory_id, body.expected_version))

    @app.delete(_BASE + "/{memory_id}", status_code=204)
    def delete(
        request: Request,
        memory_id: UUID,
        expected_version: Annotated[int, Query(ge=1, le=2**63 - 1)],
    ) -> Response:
        user = account_user(request)
        _store(request).delete(user, memory_id, expected_version)
        return Response(status_code=204, headers={"Cache-Control": "no-store"})
