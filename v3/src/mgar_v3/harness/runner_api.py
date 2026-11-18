"""Owner routes and the separate, outbound-only device protocol."""

from collections.abc import Callable
from typing import Annotated, Any, cast
from uuid import UUID

from fastapi import Depends, FastAPI, Query, Request
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse

from mgar_v3.harness.runner_contract import (
    RUNNER_API,
    Finish,
    JobInput,
    Pairing,
    Poll,
    Start,
    TaskId,
    Ticket,
    WorkspaceSpec,
)
from mgar_v3.harness.runner_store import RunnerStore


def register_runner_routes(app: FastAPI, authenticated: Callable[[Request], None]) -> None:
    owner = [Depends(authenticated)]
    device = RUNNER_API + "/device"

    def service(request: Request) -> RunnerStore:
        return cast(RunnerStore, request.app.state.harness.runners)

    def respond(value: Any) -> JSONResponse:
        return JSONResponse(jsonable_encoder(value), headers={"Cache-Control": "no-store"})

    def credential(request: Request) -> str:
        scheme, _, token = request.headers.get("Authorization", "").partition(" ")
        return token if scheme.lower() == "bearer" else ""

    @app.post(RUNNER_API + "/pairings", dependencies=owner)
    def pairing(request: Request) -> JSONResponse:
        return respond(service(request).issue_pairing())

    @app.get(RUNNER_API + "/devices", dependencies=owner)
    def devices(request: Request) -> JSONResponse:
        return respond(service(request).devices())

    @app.delete(RUNNER_API + "/devices/{device_id}", dependencies=owner)
    def revoke(device_id: UUID, request: Request) -> JSONResponse:
        return respond(service(request).revoke(device_id))

    @app.get(RUNNER_API + "/workspaces", dependencies=owner)
    def workspaces(request: Request) -> JSONResponse:
        return respond(service(request).workspaces())

    @app.get(RUNNER_API + "/jobs", dependencies=owner)
    def jobs(request: Request) -> JSONResponse:
        return respond(service(request).jobs())

    @app.post(RUNNER_API + "/jobs", dependencies=owner)
    def enqueue(body: JobInput, request: Request) -> JSONResponse:
        return respond(service(request).enqueue(body.workspace_id, body.client_id))

    @app.get(RUNNER_API + "/jobs/{task_id}", dependencies=owner)
    def job(task_id: TaskId, request: Request) -> JSONResponse:
        return respond(service(request).job(task_id))

    @app.get(RUNNER_API + "/events", dependencies=owner)
    def events(
        request: Request, after: Annotated[int, Query(ge=0, le=2**63 - 1)] = 0
    ) -> JSONResponse:
        return respond(service(request).events(after))

    @app.post(device + "/pair")
    def pair(body: Pairing, request: Request) -> JSONResponse:
        return respond(service(request).pair(body))

    @app.post(device + "/workspaces")
    def attach(body: WorkspaceSpec, request: Request) -> JSONResponse:
        return respond(service(request).attach(credential(request), body))

    @app.post(device + "/claim")
    def claim(body: Poll, request: Request) -> JSONResponse:
        return respond({"claim": service(request).claim(credential(request), body.ticket)})

    @app.post(device + "/jobs/{task_id}/heartbeat")
    def heartbeat(task_id: TaskId, body: Ticket, request: Request) -> JSONResponse:
        return respond(service(request).heartbeat(credential(request), task_id, body))

    @app.post(device + "/jobs/{task_id}/start")
    def start(task_id: TaskId, body: Start, request: Request) -> JSONResponse:
        return respond(service(request).start(credential(request), task_id, body))

    @app.post(device + "/jobs/{task_id}/finish")
    def finish(task_id: TaskId, body: Finish, request: Request) -> JSONResponse:
        result = service(request).finish(credential(request), task_id, body)
        return respond({"task_id": result["task_id"], "status": result["status"]})
