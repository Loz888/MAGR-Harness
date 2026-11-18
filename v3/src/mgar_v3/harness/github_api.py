"""Authenticated GitHub coding entry and exact-content delivery approvals."""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Callable
from typing import Any, cast
from uuid import UUID

from fastapi import Depends, FastAPI, Request, Response
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse
from pydantic import Field

from mgar_v3.domain import Model
from mgar_v3.harness.github_delivery import HarnessGitHubService


class GitHubProjectInput(Model):
    repository_url: str = Field(min_length=1, max_length=300)
    commit: str = Field(pattern=r"^[a-f0-9]{40}$")


class GitHubAuthorizationInput(Model):
    allow_publish: bool = Field(strict=True)
    allow_merge: bool = Field(strict=True)
    base_branch: str = Field(default="main", pattern=r"^[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+){0,3}$")
    days: int = Field(default=1, ge=1, le=30, strict=True)
    expected_revision: int = Field(default=0, ge=0, strict=True)


class GitHubReviewInput(Model):
    task_id: str = Field(pattern=r"^h08-[a-f0-9]{12}$")
    expected_tree: str = Field(pattern=r"^[a-f0-9]{64}$")
    base_branch: str = Field(default="main", pattern=r"^[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+){0,3}$")
    title: str = Field(min_length=1, max_length=200)
    body: str = Field(min_length=1, max_length=60_000)


class GitHubApprovalInput(Model):
    payload_hash: str = Field(pattern=r"^[a-f0-9]{64}$")


class GitHubMergeInput(Model):
    pull_number: int = Field(gt=0, strict=True)
    head_sha: str = Field(pattern=r"^[a-f0-9]{40}$")
    base_sha: str = Field(pattern=r"^[a-f0-9]{40}$")
    review_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    confirm: bool = Field(strict=True)


def register_github_routes(app: FastAPI, authenticated: Callable[[Request], None]) -> None:
    lock = threading.Lock()

    def service(request: Request) -> HarnessGitHubService:
        with lock:
            existing = getattr(request.app.state, "harness_github", None)
            if existing is None:
                existing = HarnessGitHubService(request.app.state.product)
                request.app.state.harness_github = existing
            return cast(HarnessGitHubService, existing)

    def public(value: Any) -> Response:
        return JSONResponse(jsonable_encoder(value), headers={"Cache-Control": "no-store"})

    prefix = "/api/v3/agent/github"
    secured = [Depends(authenticated)]

    @app.get(prefix + "/profile", dependencies=secured)
    def profile(request: Request) -> Response:
        value = service(request)
        return public(
            {
                "writer_available": value.writer is not None,
                "analysis_read_only": True,
                "prerequisites": [
                    "fixed_commit",
                    "workspace_write_grant",
                    "sandbox_environment",
                    "verified_tests",
                    "complete_diff_review",
                    "github_write_grant",
                ],
                "credential_setting": "MGAR_V3_GITHUB_WRITE_TOKEN",
                "enable_setting": "MGAR_V3_ALLOW_EXTERNAL_WRITE=1",
                "merge_requires": [
                    "exact_pr_and_head",
                    "ready_for_review",
                    "passing_ci",
                    "github_branch_and_review_rules",
                    "separate_explicit_approval",
                ],
            }
        )

    @app.post(prefix + "/projects", dependencies=secured)
    async def project(body: GitHubProjectInput, request: Request) -> Response:
        return public(
            await asyncio.to_thread(service(request).register_project, **body.model_dump())
        )

    @app.get(prefix + "/projects/{project_id}/authorization", dependencies=secured)
    def authorization(project_id: str, request: Request) -> Response:
        return public(service(request).authorization(project_id))

    @app.post(prefix + "/projects/{project_id}/authorization", dependencies=secured)
    def authorize(project_id: str, body: GitHubAuthorizationInput, request: Request) -> Response:
        return public(service(request).authorize(project_id, **body.model_dump()))

    @app.delete(prefix + "/projects/{project_id}/authorization", dependencies=secured)
    def revoke(project_id: str, request: Request) -> Response:
        return public(service(request).revoke(project_id))

    @app.get(prefix + "/sessions/{session_id}/deliveries", dependencies=secured)
    def deliveries(session_id: str, request: Request) -> Response:
        return public(service(request).for_session(session_id))

    @app.post(prefix + "/sessions/{session_id}/review", dependencies=secured)
    async def review(session_id: str, body: GitHubReviewInput, request: Request) -> Response:
        return public(
            await asyncio.to_thread(service(request).review, session_id, **body.model_dump())
        )

    @app.get(prefix + "/deliveries/{publication_id}", dependencies=secured)
    def delivery(publication_id: UUID, request: Request) -> Response:
        return public(service(request).get(publication_id))

    @app.post(prefix + "/deliveries/{publication_id}/approve", dependencies=secured)
    def approve(publication_id: UUID, body: GitHubApprovalInput, request: Request) -> Response:
        return public(service(request).approve(publication_id, body.payload_hash))

    @app.post(prefix + "/deliveries/{publication_id}/reconcile", dependencies=secured)
    async def reconcile(publication_id: UUID, request: Request) -> Response:
        return public(await asyncio.to_thread(service(request).reconcile, publication_id))

    @app.post(prefix + "/deliveries/{publication_id}/resume", dependencies=secured)
    async def resume(publication_id: UUID, body: GitHubApprovalInput, request: Request) -> Response:
        return public(
            await asyncio.to_thread(service(request).resume, publication_id, body.payload_hash)
        )

    @app.post(prefix + "/deliveries/{publication_id}/dispatch", dependencies=secured)
    def dispatch(publication_id: UUID, body: GitHubApprovalInput, request: Request) -> Response:
        return public(service(request).dispatch(publication_id, body.payload_hash))

    @app.get(prefix + "/deliveries/{publication_id}/merge-review", dependencies=secured)
    async def merge_review(publication_id: UUID, request: Request) -> Response:
        return public(await asyncio.to_thread(service(request).merge_review, publication_id))

    @app.post(prefix + "/deliveries/{publication_id}/merge", dependencies=secured)
    async def merge(publication_id: UUID, body: GitHubMergeInput, request: Request) -> Response:
        return public(
            await asyncio.to_thread(service(request).merge, publication_id, **body.model_dump())
        )
