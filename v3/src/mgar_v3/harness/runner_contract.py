"""H15 wire types. This protocol never accepts a host path, command or environment."""

from typing import Annotated, Final, Literal
from uuid import UUID

from pydantic import Field, field_validator, model_validator

from mgar_v3.domain import Model

RUNNER_API = "/api/v3/runner"
RUNNER_VERSION: Final = "harness-runner-v1"
LEASE_SECONDS = 12
CONTACT_SECONDS = 20
TEST_SECONDS = 120
OUTPUT_BYTES = 8192

Name = Annotated[str, Field(min_length=1, max_length=80)]
Hash = Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]
Secret = Annotated[str, Field(pattern=r"^[A-Za-z0-9_-]{43}$")]
TaskId = Annotated[str, Field(pattern=r"^h15-[a-f0-9]{12}$")]
ProjectLanguage = Literal["python", "node", "go"]
PreflightFailure = Literal[
    "RUNNER_WORKSPACE_UNAVAILABLE", "RUNNER_PLAN_CHANGED", "SANDBOX_NOT_PREPARED"
]


class Named(Model):
    name: Name

    @field_validator("name")
    @classmethod
    def display_name(cls, value: str) -> str:
        value = value.strip()
        if not value or not value.isprintable() or "/" in value or "\\" in value:
            raise ValueError("Use a display name without path separators")
        return value


class Pairing(Named):
    code: Secret
    device_id: UUID
    credential: Secret


class WorkspaceSpec(Named):
    workspace_id: UUID
    plan_hash: Hash
    project_kind: ProjectLanguage


class JobInput(Model):
    workspace_id: UUID
    client_id: UUID


class Poll(Model):
    ticket: Secret


class Ticket(Poll):
    execution_epoch: int = Field(strict=True, ge=1)


class Start(Ticket):
    tree_hash: Hash
    manifest_hash: Hash


class Finish(Ticket):
    tree_hash: Hash | None = None
    state: Literal["exited", "timeout", "unavailable", "rejected"]
    exit_code: int | None = Field(default=None, strict=True, ge=0, le=255)
    output: str = Field(default="", max_length=OUTPUT_BYTES)
    truncated: bool = Field(default=False, strict=True)
    failure: PreflightFailure | None = None

    @model_validator(mode="after")
    def consistent_receipt(self) -> "Finish":
        if len(self.output.encode("utf-8")) > OUTPUT_BYTES:
            raise ValueError("Output exceeds the Runner wire limit")
        if self.state == "rejected":
            if (
                self.failure is None
                or self.tree_hash is not None
                or self.output
                or self.exit_code is not None
                or self.truncated
            ):
                raise ValueError("A rejected setup has no process receipt")
        elif (
            self.tree_hash is None
            or self.failure is not None
            or ((self.state == "exited") != (self.exit_code is not None))
        ):
            raise ValueError("A process receipt needs its tree and a consistent exit status")
        return self


class Claim(Model):
    schema_version: Literal["harness-runner-v1"] = RUNNER_VERSION
    task_id: TaskId
    run_id: UUID
    execution_epoch: int = Field(strict=True, ge=1)
    workspace: WorkspaceSpec
    device_id: UUID
    phase: Literal["claimed", "running"]
    lease_ms: int = Field(ge=1, le=LEASE_SECONDS * 1000)
