"""Portable, hash-bound controller authority for an opt-in public probe.

No host path, arbitrary argv, source payload or private oracle is accepted by
this DTO. Historical profiles omit their default ``public_probe=None`` field.
"""

from __future__ import annotations

import base64
from typing import Literal, Self

from pydantic import Field, field_validator, model_validator

from mgar_v3.domain import Model, digest

PUBLIC_PROBE_PREFIX = "HARNESS_PUBLIC_PROBE_V1 "
PUBLIC_PROBE_ASSET_LIMIT = 262_144


class PublicProbePlan(Model):
    schema_version: Literal["harness-public-probe-plan-v1"] = "harness-public-probe-plan-v1"
    plan_id: str = Field(min_length=1, max_length=100, pattern=r"^[A-Za-z0-9_.:-]+$")
    asset_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    runner: Literal["python-json-v1"] = "python-json-v1"
    case_ids: tuple[str, ...] = Field(min_length=1, max_length=128)

    @field_validator("case_ids")
    @classmethod
    def distinct_case_ids(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        import re

        if len(set(values)) != len(values) or any(
            not re.fullmatch(r"[A-Za-z0-9_.:-]{1,100}", value) for value in values
        ):
            raise ValueError("Public probe case IDs must be unique stable identifiers")
        return values

    @property
    def plan_hash(self) -> str:
        return digest(self.model_dump(mode="json"))

    @property
    def container_path(self) -> str:
        return f"/harness-public-probe/{self.asset_sha256}.py"

    @property
    def argv(self) -> tuple[str, ...]:
        return ("python3", "-B", self.container_path)


class PublicProbeCase(Model):
    id: str = Field(min_length=1, max_length=100)
    status: Literal["passed", "assertion_failed", "error", "skipped", "xfailed"]
    detail: str = Field(default="", max_length=1000)


class PublicProbeDiagnostic(Model):
    """Bounded exact child bytes inside the single authenticated public frame."""

    stream: Literal["stdout", "stderr"]
    total_bytes: int = Field(ge=0, strict=True)
    retained_bytes: int = Field(ge=0, le=65536, strict=True)
    truncated: bool = Field(strict=True)
    retained_base64: str = Field(max_length=87384)

    @model_validator(mode="after")
    def exact_bytes(self) -> Self:
        raw = base64.b64decode(self.retained_base64, validate=True)
        if (len(raw) != self.retained_bytes or self.total_bytes < self.retained_bytes
                or self.truncated != (self.total_bytes > self.retained_bytes)
                or base64.b64encode(raw).decode("ascii") != self.retained_base64):
            raise ValueError("Public diagnostic bytes or truncation counters differ")
        return self


class PublicProbeFrame(Model):
    plan_id: str
    asset_sha256: str
    cases: tuple[PublicProbeCase, ...] = Field(min_length=1, max_length=128)
    diagnostics: tuple[PublicProbeDiagnostic, ...] = Field(
        default=(), max_length=2, exclude_if=lambda value: not value,
    )

    @model_validator(mode="after")
    def unique_cases(self) -> Self:
        if len({case.id for case in self.cases}) != len(self.cases):
            raise ValueError("Public probe output contains duplicate cases")
        if len({item.stream for item in self.diagnostics}) != len(self.diagnostics):
            raise ValueError("Public diagnostic streams must be distinct")
        return self
