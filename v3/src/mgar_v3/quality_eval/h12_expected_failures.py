"""Validate exact original AVA test.failing declarations before report projection."""

from __future__ import annotations

import hashlib
import re
from pathlib import Path

from mgar_v3.domain import DomainError
from mgar_v3.harness.regression_types import RegressionPlan


def validate_expected_failures(source: Path, plan: RegressionPlan) -> None:
    for command in plan.commands:
        for identity, item in command.expected_failure_sources.items():
            path = source / item.path
            if (
                Path(item.path).is_absolute()
                or ".." in Path(item.path).parts
                or not path.resolve().is_relative_to(source.resolve())
                or any(p.is_symlink() for p in (path, *path.parents))
                or not path.is_file()
                or hashlib.sha256(path.read_bytes()).hexdigest() != item.sha256
            ):
                raise DomainError("EXPECTED_FAILURE_SOURCE_CHANGED", "Original AVA source differs")
            lines = path.read_text().splitlines()
            call = lines[item.line - 1] if item.line <= len(lines) else ""
            marker = re.search(r"\btest(?:\.serial)?\.failing\(\s*(['\"])(.*?)\1\s*,", call)
            if (
                marker is None
                or marker[2] != item.title
                or item.title not in identity
                or command.expected_exceptions.get(identity) != "xfailed"
            ):
                raise DomainError("EXPECTED_FAILURE_SOURCE_CHANGED", "AVA declaration differs")
