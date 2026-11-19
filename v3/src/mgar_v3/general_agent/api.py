"""General task intake on the existing authenticated Harness API."""

from collections.abc import Callable
from typing import Annotated, cast
from uuid import UUID

from fastapi import FastAPI, Path, Request, Response
from fastapi.responses import JSONResponse
from pydantic import Field

from mgar_v3.coordination.engine import Engine
from mgar_v3.domain import DomainError, Model
from mgar_v3.general_agent.contracts import GeneralTaskSpec, Identifier, Revision, TaskSnapshot
from mgar_v3.harness.product import HarnessProductService


class CreateGeneralTask(Model):
    project_id: Identifier
    request_id: UUID
    spec: GeneralTaskSpec
    draft: Annotated[bool, Field(strict=True)] = False


class ReviseGeneralTask(Model):
    expected_revision: Revision
    expected_version: Revision
    spec: GeneralTaskSpec
    draft: Annotated[bool, Field(strict=True)] = False


def _response(snapshot: TaskSnapshot, status: int = 200) -> Response:
    return JSONResponse(
        snapshot.model_dump(mode="json"), status_code=status, headers={"Cache-Control": "no-store"}
    )


def _membership(
    product: HarnessProductService,
    project_id: str,
    user_id: UUID | None,
    *,
    write: bool,
) -> UUID | None:
    if user_id is None:
        product._project(project_id)
        return None
    member = product.tenancy.require_project(project_id, user_id)
    if write and member["role"] != "contributor":
        raise DomainError("HARNESS_MEMBER_WRITE_DENIED", "当前成员没有任务修改权限。", 403)
    return cast(UUID, member["membership_id"])


def register_general_task_routes(
    app: FastAPI,
    member_user: Callable[[Request], UUID | None],
) -> None:
    def current_access(
        request: Request,
        task: TaskSnapshot,
        user_id: UUID | None,
        *,
        write: bool,
    ) -> None:
        if user_id is None:
            return
        product: HarnessProductService = request.app.state.product
        membership = _membership(product, task.project_id, user_id, write=False)
        if membership != task.membership_id:
            raise DomainError("GENERAL_TASK_NOT_FOUND", "任务不存在。", 404)
        if write:
            product.tenancy.require_binding(task.project_id, str(membership), write=True)

    @app.post("/api/v3/general/tasks", status_code=201, response_model=TaskSnapshot)
    def create(body: CreateGeneralTask, request: Request) -> Response:
        user_id = member_user(request)
        engine: Engine = request.app.state.engine
        product: HarnessProductService = request.app.state.product
        with engine.transaction():
            membership = _membership(product, body.project_id, user_id, write=True)
            snapshot = engine.general_tasks.create(
                body.project_id,
                membership,
                body.request_id,
                body.spec,
                draft=body.draft,
            )
        return _response(snapshot, 201)

    @app.get("/api/v3/general/tasks/{task_id}", response_model=TaskSnapshot)
    def read(task_id: UUID, request: Request) -> Response:
        user_id = member_user(request)
        engine: Engine = request.app.state.engine
        with engine.transaction():
            snapshot = engine.general_tasks.get(task_id)
            current_access(request, snapshot, user_id, write=False)
        return _response(snapshot)

    @app.get("/api/v3/general/tasks/{task_id}/revisions/{revision}", response_model=TaskSnapshot)
    def read_revision(
        task_id: UUID,
        revision: Annotated[int, Path(ge=1, le=2**63 - 1)],
        request: Request,
    ) -> Response:
        user_id = member_user(request)
        engine: Engine = request.app.state.engine
        with engine.transaction():
            current_access(request, engine.general_tasks.get(task_id), user_id, write=False)
            snapshot = engine.general_tasks.get(task_id, revision=revision)
        return _response(snapshot)

    @app.post(
        "/api/v3/general/tasks/{task_id}/revisions",
        status_code=201,
        response_model=TaskSnapshot,
    )
    def revise(task_id: UUID, body: ReviseGeneralTask, request: Request) -> Response:
        user_id = member_user(request)
        engine: Engine = request.app.state.engine
        with engine.transaction():
            current_access(request, engine.general_tasks.get(task_id), user_id, write=True)
            snapshot = engine.general_tasks.revise(
                task_id,
                body.expected_revision,
                body.expected_version,
                body.spec,
                draft=body.draft,
            )
        return _response(snapshot, 201)
