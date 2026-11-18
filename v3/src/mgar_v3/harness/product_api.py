"""Authenticated same-origin product routes; no additional server or writer."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any, cast

from fastapi import Depends, FastAPI, Request, Response
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse
from pydantic import Field, field_validator

from mgar_v3.domain import Model
from mgar_v3.harness.github_api import register_github_routes
from mgar_v3.harness.product import HarnessProductService


class ProductMessageInput(Model):
    text: str = Field(min_length=1, max_length=20_000)
    client_id: str = Field(default="", max_length=100)

    @field_validator("text", "client_id")
    @classmethod
    def no_nul(cls, value: str) -> str:
        if "\0" in value:
            raise ValueError("NUL is not allowed")
        return value


class ProductSessionInput(ProductMessageInput):
    project_id: str = Field(min_length=1, max_length=100)


class ProductAuthorizationInput(Model):
    allow_write: bool = Field(strict=True)
    allow_paid: bool = Field(strict=True)
    allow_github_read: bool = Field(default=False, strict=True)
    max_cost_micros: int = Field(default=250_000, strict=True, ge=1, le=12_000_000)
    max_model_calls: int = Field(default=64, strict=True, ge=1, le=512)
    days: int = Field(default=30, strict=True, ge=1, le=365)
    expected_revision: int | None = Field(default=None, strict=True, ge=0)


class ProductApplyInput(Model):
    task_id: str = Field(pattern=r"^h08-[0-9a-f]{12}$")
    expected_tree: str = Field(pattern=r"^[0-9a-f]{64}$")


class ProductEnvironmentInput(Model):
    image_lock: str = Field(min_length=1, max_length=4096)
    wheelhouse: str = Field(default="", max_length=4096)


class ProductPreviewInput(Model):
    task_id: str = Field(pattern=r"^h08-[0-9a-f]{12}$")


def register_product_routes(app: FastAPI, authenticated: Callable[[Request], None]) -> None:
    register_github_routes(app, authenticated)

    def service(request: Request) -> HarnessProductService:
        return cast(HarnessProductService, request.app.state.product)

    def public(value: Any) -> JSONResponse:
        return JSONResponse(jsonable_encoder(value), headers={"Cache-Control": "no-store"})

    @app.get("/api/v3/agent/profile", dependencies=[Depends(authenticated)])
    def profile(request: Request) -> Response:
        return public(service(request).profile())

    @app.get("/api/v3/agent/projects/{project_id}/readiness", dependencies=[Depends(authenticated)])
    async def readiness(project_id: str, request: Request) -> Response:
        return public(await asyncio.to_thread(service(request).readiness, project_id))

    @app.post(
        "/api/v3/agent/projects/{project_id}/authorization", dependencies=[Depends(authenticated)]
    )
    def authorize(project_id: str, body: ProductAuthorizationInput, request: Request) -> Response:
        return public(service(request).authorize(project_id, **body.model_dump()))

    @app.delete(
        "/api/v3/agent/projects/{project_id}/authorization", dependencies=[Depends(authenticated)]
    )
    def revoke(project_id: str, request: Request) -> Response:
        return public(service(request).revoke(project_id))

    @app.post(
        "/api/v3/agent/projects/{project_id}/environment", dependencies=[Depends(authenticated)]
    )
    async def environment(
        project_id: str, body: ProductEnvironmentInput, request: Request
    ) -> Response:
        return public(
            await asyncio.to_thread(
                service(request).prepare_environment, project_id, **body.model_dump()
            )
        )

    @app.get("/api/v3/agent/sessions", dependencies=[Depends(authenticated)])
    def sessions(request: Request) -> Response:
        return public(service(request).sessions())

    @app.post("/api/v3/agent/sessions", dependencies=[Depends(authenticated)])
    async def create(body: ProductSessionInput, request: Request) -> Response:
        return public(await asyncio.to_thread(service(request).create, **body.model_dump()))

    @app.get("/api/v3/agent/sessions/{session_id}", dependencies=[Depends(authenticated)])
    def session(session_id: str, request: Request) -> Response:
        return public(service(request).get(session_id))

    @app.get(
        "/api/v3/agent/sessions/{session_id}/export", dependencies=[Depends(authenticated)]
    )
    def export_session(session_id: str, request: Request) -> Response:
        return public(service(request).export(session_id))

    @app.post("/api/v3/agent/sessions/{session_id}/messages", dependencies=[Depends(authenticated)])
    async def send(session_id: str, body: ProductMessageInput, request: Request) -> Response:
        return public(
            await asyncio.to_thread(service(request).send, session_id, **body.model_dump())
        )

    @app.post("/api/v3/agent/sessions/{session_id}/apply", dependencies=[Depends(authenticated)])
    async def apply(session_id: str, body: ProductApplyInput, request: Request) -> Response:
        return public(
            await asyncio.to_thread(service(request).apply, session_id, **body.model_dump())
        )

    @app.post("/api/v3/agent/sessions/{session_id}/preview", dependencies=[Depends(authenticated)])
    async def preview(session_id: str, body: ProductPreviewInput, request: Request) -> Response:
        return public(
            await asyncio.to_thread(service(request).start_preview, session_id, body.task_id)
        )

    @app.delete(
        "/api/v3/agent/sessions/{session_id}/preview", dependencies=[Depends(authenticated)]
    )
    async def stop_preview(session_id: str, request: Request) -> Response:
        return public(await asyncio.to_thread(service(request).stop_preview, session_id))

    @app.get(
        "/api/v3/agent/sessions/{session_id}/preview/{path:path}",
        dependencies=[Depends(authenticated)],
    )
    async def fetch_preview(session_id: str, path: str, request: Request) -> Response:
        # Cookies/auth headers never reach the project process. Its HTML runs
        # in an opaque sandbox origin and cannot call the control API.
        status, media_type, body = await asyncio.to_thread(
            service(request).fetch_preview,
            session_id,
            path + ("?" + request.url.query if request.url.query else ""),
        )
        return Response(
            body,
            status_code=status,
            media_type=media_type,
            headers={
                "Cache-Control": "no-store",
                "X-Content-Type-Options": "nosniff",
                "Referrer-Policy": "no-referrer",
                "Content-Security-Policy": "sandbox allow-scripts allow-forms; "
                "default-src 'self' data: blob:; script-src 'self' 'unsafe-inline'; "
                "style-src 'self' 'unsafe-inline'; connect-src 'self'; "
                "form-action 'none'; frame-ancestors 'self'; base-uri 'none'",
            },
        )

    @app.get("/api/v3/agent/preview/{token}/{path:path}")
    async def preview_resource(token: str, path: str, request: Request) -> Response:
        # An opaque iframe cannot send the control-plane Strict cookies for
        # subresources. This unguessable, short-lived capability reads only its
        # owned preview; it grants no control API, host file or mutation access.
        status, media_type, body = await asyncio.to_thread(
            service(request).fetch_preview_token,
            token,
            path + ("?" + request.url.query if request.url.query else ""),
        )
        prefix = str(request.base_url).rstrip("/") + f"/api/v3/agent/preview/{token}/"
        return Response(
            body,
            status_code=status,
            media_type=media_type,
            headers={
                "Cache-Control": "no-store",
                "X-Content-Type-Options": "nosniff",
                "Referrer-Policy": "no-referrer",
                "Access-Control-Allow-Origin": "*",
                "Cross-Origin-Resource-Policy": "cross-origin",
                "Content-Security-Policy": "sandbox allow-scripts allow-forms; default-src 'none'; "
                f"script-src {prefix} 'unsafe-inline'; style-src {prefix} 'unsafe-inline'; "
                f"img-src {prefix} data: blob:; font-src {prefix} data:; "
                f"connect-src {prefix}; media-src {prefix}; "
                "form-action 'none'; frame-ancestors 'self'; base-uri 'none'",
            },
        )
