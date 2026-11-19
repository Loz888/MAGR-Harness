"""Bounded data analysis requests and immutable results."""

from __future__ import annotations

from typing import Annotated, Literal, Self

from pydantic import Field, model_validator

from mgar_v3.domain import Model
from mgar_v3.general_agent.contracts import Identifier, Sha256, SourceRef, Text, unique

DATA_PROTOCOL: Literal["general-data-analysis-v1"] = "general-data-analysis-v1"
MAX_INPUT = 1_000_000
MAX_ROWS = 10_000
MAX_COLUMNS = 32
MAX_CELLS = 100_000
MAX_GROUPS = 100
MAX_DOCUMENT = 4_000_000
DatasetFormat = Literal["auto", "csv", "json", "xlsx"]
ResultFormat = Literal["numeric", "csv", "xlsx", "chart", "report", "manifest"]
Formula = Literal["sum", "mean", "count"]


class DataFilter(Model):
    field: Identifier
    equals: Text | Annotated[int, Field(strict=True, ge=-(2**53), le=2**53)] | bool | None


class DataAnalysisRequest(Model):
    source: SourceRef
    format: DatasetFormat = "auto"
    field: Identifier
    formula: Formula = "sum"
    group_by: Identifier | None = None
    date_field: Identifier | None = None
    filters: Annotated[tuple[DataFilter, ...], Field(max_length=8)] = ()
    seed: Annotated[int, Field(strict=True, ge=0, le=2**32 - 1)] = 0

    @model_validator(mode="after")
    def scope(self) -> Self:
        if self.source.kind not in {"artifact", "dataset"}:
            raise ValueError("The input must be a pinned dataset or artifact.")
        version = self.source.version or ""
        if len(version) != 64 or any(c not in "0123456789abcdef" for c in version):
            raise ValueError("The source version must pin the original CAS SHA256.")
        unique(tuple(item.field for item in self.filters), "filter fields")
        return self


class DataAnalysisResult(Model):
    protocol: Literal["general-data-analysis-v1"] = DATA_PROTOCOL
    source: SourceRef
    input_sha256: Sha256
    normalized_sha256: Sha256
    implementation_sha256: Sha256
    input_rows: Annotated[int, Field(ge=1, le=MAX_ROWS)]
    selected_rows: Annotated[int, Field(ge=1, le=MAX_ROWS)]
    sample_count: Annotated[int, Field(ge=1, le=MAX_ROWS)]
    artifacts: dict[ResultFormat, Sha256]
