import hashlib
import json
import os
from pathlib import Path
from typing import Literal

from mgar_v3.domain import DomainError
from mgar_v3.mcp.client import ReadOnlyGitHubMCP
from mgar_v3.settings import ROOT, Settings


def verified_github_binary(path: Path) -> Path:
    try:
        binary = path.resolve(strict=True)
        lock = json.loads((ROOT / "config/github-mcp-lock.json").read_text())
        if binary.is_file() and os.access(binary, os.X_OK):
            with binary.open("rb") as stream:
                actual = hashlib.file_digest(stream, "sha256").hexdigest()
            if actual == lock["binary_sha256"]:
                return binary
    except (OSError, ValueError, KeyError, RuntimeError):
        pass
    raise DomainError("MCP_BINARY_UNVERIFIED", "Official MCP server is missing or changed", 503)


def github_client(
    settings: Settings,
    repository: str,
    *,
    readonly: bool = True,
    max_bytes: int = 65536,
    tool_group: Literal["r1", "r5_read", "r5_write", "analysis"] = "r1",
) -> ReadOnlyGitHubMCP:
    if tool_group in {"r5_read", "r5_write"} and (
        settings.github_mcp_binary is None or readonly != (tool_group == "r5_read")
    ):
        raise DomainError("R5_MCP_REQUIRED", "R5 requires the pinned official MCP tool group", 403)
    if tool_group == "analysis" and not readonly:
        raise DomainError("REPO_ANALYSIS_READONLY", "Repository analysis is read-only", 403)
    token = settings.github_read_token if readonly else settings.github_write_token
    if settings.github_mcp_binary is None:
        client = ReadOnlyGitHubMCP(
            repository, token=token, max_bytes=max_bytes, redis_url=settings.redis_url
        )
    else:
        binary = verified_github_binary(settings.github_mcp_binary)
        tools = "issue_read,get_file_contents" if readonly else "add_issue_comment"
        if tool_group == "r5_read":
            tools = "get_commit,list_pull_requests,pull_request_read"
        if tool_group == "r5_write":
            tools = "create_branch,push_files,create_pull_request"
        if tool_group == "analysis":
            tools = (
                "get_commit,get_file_contents,search_code,list_branches,search_repositories,"
                "issue_read,list_issues,pull_request_read,list_pull_requests"
            )
        args: tuple[str, ...] = ("stdio", "--tools=" + tools)
        if readonly:
            args += ("--read-only",)
        client = ReadOnlyGitHubMCP(
            repository,
            command=str(binary),
            args=args,
            token=token,
            max_bytes=max_bytes,
            redis_url=settings.redis_url,
        )
    client.readonly = readonly
    return client
