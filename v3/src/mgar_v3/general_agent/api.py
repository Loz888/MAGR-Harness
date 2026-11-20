"""General task intake on the existing authenticated Harness API."""

from collections.abc import Callable
from typing import Annotated, cast
from uuid import UUID

from fastapi import FastAPI, Path, Query, Request, Response
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse
from pydantic import Field

from mgar_v3.coordination.engine import Engine
from mgar_v3.domain import DomainError, Model
from mgar_v3.general_agent.contracts import (
    GeneralTaskSpec,
    Identifier,
    RecentTask,
    Revision,
    TaskSnapshot,
)
from mgar_v3.general_agent.graph import ReplanTrigger, TaskGraph, deterministic_plan
from mgar_v3.general_agent.graph_store import GraphSnapshot
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


class InstallGraph(Model):
    expected_revision: Revision
    expected_version: Revision
    proposal: TaskGraph | None = None


class ReplanGraph(Model):
    expected_revision: Revision
    expected_graph_revision: Revision
    expected_graph_version: Revision
    trigger: ReplanTrigger
    proposal: TaskGraph


def _response(snapshot: Model, status: int = 200) -> Response:
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
        try:
            membership = _membership(product, task.project_id, user_id, write=False)
        except DomainError as exc:
            if exc.code != "HARNESS_PROJECT_UNKNOWN":
                raise
            membership = None
        if membership is None or membership != task.membership_id:
            raise DomainError("GENERAL_TASK_NOT_FOUND", "任务不存在。", 404)
        if write:
            product.tenancy.require_binding(task.project_id, str(membership), write=True)

    @app.get("/api/v3/general/tasks", response_model=list[RecentTask])
    def recent(request: Request, limit: Annotated[int, Query(ge=1, le=100)] = 50) -> Response:
        user_id = member_user(request)
        engine: Engine = request.app.state.engine
        return JSONResponse(
            jsonable_encoder(engine.general_tasks.recent(user_id, limit)),
            headers={"Cache-Control": "no-store"},
        )

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

    @app.post(
        "/api/v3/general/tasks/{task_id}/graph",
        status_code=201,
        response_model=GraphSnapshot,
    )
    def install_graph(task_id: UUID, body: InstallGraph, request: Request) -> Response:
        user_id = member_user(request)
        engine: Engine = request.app.state.engine
        with engine.transaction():
            task = engine.general_tasks.get(task_id)
            current_access(request, task, user_id, write=True)
            if (task.revision, task.version) != (body.expected_revision, body.expected_version):
                raise DomainError("GENERAL_TASK_STALE", "任务已更新，请读取当前版本。", 409)
            graph = body.proposal or deterministic_plan(task.spec, task.revision)
            value = engine.general_graphs.install(task_id, task.revision, task.version, graph)
        return _response(value, 201)

    @app.get("/api/v3/general/tasks/{task_id}/graph", response_model=GraphSnapshot)
    def read_graph(task_id: UUID, request: Request) -> Response:
        user_id = member_user(request)
        engine: Engine = request.app.state.engine
        with engine.transaction():
            current_access(request, engine.general_tasks.get(task_id), user_id, write=False)
            value = engine.general_graphs.get(task_id)
        return _response(value)

    @app.patch("/api/v3/general/tasks/{task_id}/graph", response_model=GraphSnapshot)
    def replan_graph(task_id: UUID, body: ReplanGraph, request: Request) -> Response:
        user_id = member_user(request)
        engine: Engine = request.app.state.engine
        with engine.transaction():
            current_access(request, engine.general_tasks.get(task_id), user_id, write=True)
            value = engine.general_graphs.replan(
                task_id,
                body.expected_revision,
                body.expected_graph_revision,
                body.expected_graph_version,
                body.proposal,
                body.trigger,
            )
        return _response(value)

    @app.get("/api/v3/general/tasks/{task_id}/graph/events")
    def graph_events(task_id: UUID, request: Request) -> Response:
        user_id = member_user(request)
        engine: Engine = request.app.state.engine
        with engine.transaction():
            current_access(request, engine.general_tasks.get(task_id), user_id, write=False)
            value = engine.general_graphs.events(task_id)
        return JSONResponse(jsonable_encoder(value), headers={"Cache-Control": "no-store"})
