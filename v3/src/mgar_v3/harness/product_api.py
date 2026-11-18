"""Authenticated same-origin product routes; no additional server or writer."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any, Literal, cast
from uuid import UUID

from fastapi import Depends, FastAPI, Request, Response
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse
from pydantic import Field, SecretStr, field_validator

from mgar_v3.context.policy import ContextPolicy
from mgar_v3.domain import Model
from mgar_v3.harness.github_api import register_github_routes
from mgar_v3.harness.product import HarnessProductService
from mgar_v3.web_access import LoginInput, WebAccounts


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


class ProjectQuotaInput(Model):
    max_parallel: int = Field(default=1, strict=True, ge=1, le=4)
    max_queued: int = Field(default=8, strict=True, ge=1, le=64)


class ProjectMemberInput(Model):
    role: Literal["reader", "contributor"]


class ProjectCredentialInput(Model):
    deepseek_api_key: SecretStr = Field(max_length=1024)
    github_read_token: SecretStr | None = Field(default=None, max_length=1024)
    model_id: str = Field(default="deepseek-flash", max_length=100)
    context_policy: ContextPolicy = Field(default_factory=ContextPolicy)


def register_product_routes(
    app: FastAPI,
    authenticated: Callable[[Request], None],
    member_user: Callable[[Request], UUID | None],
) -> None:
    register_github_routes(app, authenticated)

    def service(request: Request) -> HarnessProductService:
        return cast(HarnessProductService, request.app.state.product)

    def public(value: Any) -> JSONResponse:
        return JSONResponse(jsonable_encoder(value), headers={"Cache-Control": "no-store"})

    @app.get("/api/v3/agent/accounts", dependencies=[Depends(authenticated)])
    def accounts(request: Request) -> Response:
        store: WebAccounts = request.app.state.web_accounts
        return public(store.list_members())

    @app.post("/api/v3/agent/accounts", dependencies=[Depends(authenticated)], status_code=201)
    def create_account(request: Request, body: LoginInput) -> Response:
        store: WebAccounts = request.app.state.web_accounts
        result = public({"user_id": store.create_member(body), "username": body.username})
        result.status_code = 201
        return result

    @app.put("/api/v3/agent/projects/{project_id}/quota", dependencies=[Depends(authenticated)])
    def set_quota(project_id: str, body: ProjectQuotaInput, request: Request) -> Response:
        return public(service(request).tenancy.configure(project_id, **body.model_dump()))

    @app.get("/api/v3/agent/projects/{project_id}/members", dependencies=[Depends(authenticated)])
    def members(project_id: str, request: Request) -> Response:
        return public(service(request).tenancy.members(project_id))

    @app.put(
        "/api/v3/agent/projects/{project_id}/members/{user_id}",
        dependencies=[Depends(authenticated)],
    )
    def set_member(
        project_id: str,
        user_id: UUID,
        body: ProjectMemberInput,
        request: Request,
    ) -> Response:
        return public(service(request).tenancy.grant(project_id, user_id, body.role))

    @app.delete(
        "/api/v3/agent/projects/{project_id}/members/{user_id}",
        dependencies=[Depends(authenticated)],
    )
    def revoke_member(project_id: str, user_id: UUID, request: Request) -> Response:
        service(request).tenancy.revoke(project_id, user_id)
        return Response(status_code=204)

    @app.put(
        "/api/v3/agent/projects/{project_id}/credentials",
        dependencies=[Depends(authenticated)],
    )
    def set_credentials(
        project_id: str,
        body: ProjectCredentialInput,
        request: Request,
    ) -> Response:
        revision = service(request).tenancy.save_credentials(
            project_id,
            deepseek_api_key=body.deepseek_api_key.get_secret_value(),
            github_read_token=(
                body.github_read_token.get_secret_value() if body.github_read_token else None
            ),
            model_id=body.model_id,
            context_policy=body.context_policy,
        )
        return public({"project_id": project_id, "revision": revision})

    @app.get("/api/v3/agent/profile", dependencies=[Depends(member_user)])
    def profile(request: Request) -> Response:
        return public(service(request).profile(member_user(request)))

    @app.get("/api/v3/agent/projects/{project_id}/readiness", dependencies=[Depends(member_user)])
    async def readiness(project_id: str, request: Request) -> Response:
        return public(
            await asyncio.to_thread(service(request).readiness, project_id, member_user(request))
        )

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

    @app.get("/api/v3/agent/sessions", dependencies=[Depends(member_user)])
    def sessions(request: Request) -> Response:
        return public(service(request).sessions(member_user(request)))

    @app.post("/api/v3/agent/sessions", dependencies=[Depends(member_user)])
    async def create(body: ProductSessionInput, request: Request) -> Response:
        return public(
            await asyncio.to_thread(
                service(request).create,
                **body.model_dump(),
                user_id=member_user(request),
            )
        )

    @app.get("/api/v3/agent/sessions/{session_id}", dependencies=[Depends(member_user)])
    def session(session_id: str, request: Request) -> Response:
        return public(service(request).get(session_id, member_user(request)))

    @app.get("/api/v3/agent/sessions/{session_id}/export", dependencies=[Depends(member_user)])
    def export_session(session_id: str, request: Request) -> Response:
        return public(service(request).export(session_id, member_user(request)))

    @app.post("/api/v3/agent/sessions/{session_id}/messages", dependencies=[Depends(member_user)])
    async def send(session_id: str, body: ProductMessageInput, request: Request) -> Response:
        return public(
            await asyncio.to_thread(
                service(request).send,
                session_id,
                **body.model_dump(),
                user_id=member_user(request),
            )
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
