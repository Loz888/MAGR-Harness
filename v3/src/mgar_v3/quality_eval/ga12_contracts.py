"""GA12 case, frozen split, and score contracts."""

from __future__ import annotations

from typing import Annotated, Literal, Self

from pydantic import Field, StringConstraints, model_validator

from mgar_v3.domain import Model
from mgar_v3.general_agent.contracts import Sha256, unique

Domain = Literal["coding", "research", "data", "office", "autonomy", "cross-domain"]
Tool = Literal["coding.patch", "research.read", "data.aggregate", "office.send", "autonomy.resume"]
Status = Literal["completed", "denied", "unknown", "unavailable"]
Mode = Literal["mock", "real-provider", "real-external"]
Arm = Literal["multi_domain", "no_planner", "no_memory", "no_reranker", "single_domain"]
Split = Literal["dev", "holdout"]
Domains: tuple[Domain, ...] = ("coding", "research", "data", "office", "autonomy", "cross-domain")
Arms: tuple[Arm, ...] = (
    "multi_domain",
    "no_planner",
    "no_memory",
    "no_reranker",
    "single_domain",
)
Sequences: dict[Domain, tuple[Tool, ...]] = {
    "coding": ("coding.patch",),
    "research": ("research.read",),
    "data": ("data.aggregate",),
    "office": ("office.send",),
    "autonomy": ("autonomy.resume",),
    "cross-domain": ("research.read", "data.aggregate"),
}
Key = Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9-]{2,79}$")]
Text = Annotated[str, StringConstraints(min_length=1, max_length=1024, pattern=r"^[^\x00]+$")]
Goal = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=500)]
Number = Annotated[int, Field(strict=True, ge=-100_000, le=100_000)]
Count = Annotated[int, Field(strict=True, ge=0)]
Flag = Annotated[bool, Field(strict=True)]
Value = Annotated[int, Field(strict=True)] | str | None


class CodingState(Model):
    text: Text
    old: Text
    new: Text
    memory_hint: Flag

    @model_validator(mode="after")
    def replacement_is_unique(self) -> Self:
        if self.text.count(self.old) != 1:
            raise ValueError("The coding pre-image must occur exactly once.")
        return self


class FixtureSource(Model):
    id: Key
    value: Number
    rank: Annotated[int, Field(strict=True, ge=0, le=100)]
    trusted: Flag


class ResearchState(Model):
    sources: Annotated[tuple[FixtureSource, ...], Field(min_length=2, max_length=16)]

    @model_validator(mode="after")
    def sources_are_unique(self) -> Self:
        unique(tuple(source.id for source in self.sources), "source IDs")
        return self


class DataState(Model):
    values: Annotated[tuple[Number, ...], Field(min_length=1, max_length=32)]
    formula: Literal["sum", "max", "count"]


class OfficeState(Model):
    action_id: Key
    message: Text
    approved: Flag
    ack: Literal["confirmed", "lost"]
    lookup_available: Flag


class AutonomyState(Model):
    checkpoint_id: Key
    receipt: Literal["confirmed", "unknown"]
    value: Number


class CrossDomainState(ResearchState):
    values: Annotated[tuple[Number, ...], Field(min_length=1, max_length=32)]


State = CodingState | ResearchState | DataState | OfficeState | AutonomyState | CrossDomainState
_STATE_TYPES: dict[Domain, type[State]] = {
    "coding": CodingState,
    "research": ResearchState,
    "data": DataState,
    "office": OfficeState,
    "autonomy": AutonomyState,
    "cross-domain": CrossDomainState,
}


class Expected(Model):
    status: Status
    result: Value
    plan: Annotated[tuple[Tool, ...], Field(min_length=1, max_length=2)]
    executed_tools: Annotated[tuple[Tool, ...], Field(max_length=2)]
    evidence_ids: Annotated[tuple[Key, ...], Field(max_length=2)]
    effects: Annotated[int, Field(strict=True, ge=0, le=1)]

    @model_validator(mode="after")
    def result_matches_terminal_status(self) -> Self:
        if (self.result is not None) != (self.status == "completed"):
            raise ValueError("Only a completed fixture returns a result.")
        unique(self.evidence_ids, "evidence IDs")
        return self


class FixtureInput(Model):
    id: Key
    domain: Domain
    goal: Goal
    environment: Literal["ga12-fixture-v1"]
    authorized_tools: Annotated[tuple[Tool, ...], Field(max_length=2)]
    initial_state: State

    @model_validator(mode="after")
    def check_domain_scope(self) -> Self:
        if type(self.initial_state) is not _STATE_TYPES[self.domain]:
            raise ValueError("Initial state does not match the domain.")
        unique(self.authorized_tools, "authorized tools")
        if set(self.authorized_tools) - set(Sequences[self.domain]):
            raise ValueError("A fixture cannot grant tools in other workflows.")
        return self


class Case(FixtureInput):
    expected: Expected

    @model_validator(mode="after")
    def expected_has_no_extra_authority(self) -> Self:
        if self.expected.plan != Sequences[self.domain]:
            raise ValueError("Expected plan must cover the domain.")
        if self.expected.plan[
            : len(self.expected.executed_tools)
        ] != self.expected.executed_tools or set(self.expected.executed_tools) - set(
            self.authorized_tools
        ):
            raise ValueError("Expected trace is outside the granted workflow.")
        if self.domain != "office" and self.expected.effects:
            raise ValueError("Only an Office fixture can produce an external effect.")
        return self

    def public_input(self) -> FixtureInput:
        # The fixture runner receives a typed input with no access to expected answers.
        return FixtureInput.model_validate(self.model_dump(exclude={"expected"}))


class Freeze(Model):
    schema_version: Literal["ga12-freeze-v1"] = "ga12-freeze-v1"
    split: Split
    bundle_sha256: Sha256
    counts: dict[Domain, Count]
    author_role: Literal["development", "independent"]
    contract_sha256: Sha256

    @model_validator(mode="after")
    def independent_splits(self) -> Self:
        if set(self.counts) != set(Domains) or any(value < 10 for value in self.counts.values()):
            raise ValueError("The split needs at least ten cases in each domain.")
        if (self.split == "holdout") != (self.author_role == "independent"):
            raise ValueError("Holdout must be frozen under the independent author role.")
        return self
