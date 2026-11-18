"""Opt-in, independently frozen public behavior plans for development candidates.

This controller port loads one explicit public manifest. It never scans an
asset directory, reads a private judge, changes the old catalog, grants model
budget, or treats a load-only check as an executed reproduction.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from mgar_v3.domain import DomainError
from mgar_v3.harness.contracts import canonical_hash
from mgar_v3.harness.public_probe import read_asset
from mgar_v3.harness.public_probe_types import PublicProbePlan
from mgar_v3.harness.workspace import _scan, _tree_hash
from mgar_v3.quality_eval import h12
from mgar_v3.quality_eval.harness_v4 import CatalogError

_HASH = re.compile(r"^[0-9a-f]{64}$")
_FIELDS = {
    "schema",
    "status",
    "task_id",
    "repository",
    "base_commit",
    "public_goal_sha256",
    "resources_sha256",
    "original_tree_sha256",
    "roles",
    "regression_argv",
    "public_basis",
    "probe",
}
PUBLIC_ROLES = {
    "reproduction": "same_public_probe_assertion_failure_on_original",
    "behavior": "same_public_probe_all_cases_pass_on_final",
    "regression": "original_public_regression_on_final",
}


def _sha(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _document(raw: bytes) -> dict[str, Any]:
    def pairs(values: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in values:
            if key in result:
                raise CatalogError("Duplicate public plan field")
            result[key] = value
        return result

    try:
        value = json.loads(raw, object_pairs_hook=pairs)
    except (ValueError, UnicodeError):
        raise CatalogError("Public plan is not valid JSON") from None
    if not isinstance(value, dict):
        raise CatalogError("Public plan must be an object")
    return value


def _manifest_bytes(path: Path, expected: str) -> bytes:
    if (
        not _HASH.fullmatch(expected)
        or any(part.is_symlink() for part in (path, *path.parents))
        or h12._PRIVATE_TOKEN.search(path.name)
    ):
        raise CatalogError("Public plan manifest path or hash is invalid")
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(descriptor, "rb") as stream:
            if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                raise CatalogError("Public plan manifest is not a regular file")
            raw = stream.read(65537)
    except OSError:
        raise CatalogError("Public plan manifest is unavailable") from None
    if len(raw) > 65536 or _sha(raw) != expected:
        raise CatalogError("Public plan manifest changed from its frozen hash")
    return raw


@dataclass(frozen=True)
class FrozenPublicPlan:
    plan: PublicProbePlan
    manifest_path: Path
    manifest_sha256: str
    asset_root: Path
    task_id: str
    repository: str
    base_commit: str
    public_goal_sha256: str
    resources_sha256: str
    original_tree_sha256: str
    regression_argv: tuple[str, ...]
    public_basis: tuple[tuple[str, str], ...]

    def freeze_metadata(self) -> dict[str, Any]:
        return {
            "schema": "h12-public-plan-v1",
            "manifest_sha256": self.manifest_sha256,
            "task_id": self.task_id,
            "repository": self.repository,
            "base_commit": self.base_commit,
            "public_goal_sha256": self.public_goal_sha256,
            "resources_sha256": self.resources_sha256,
            "original_tree_sha256": self.original_tree_sha256,
            "public_probe": self.plan.model_dump(mode="json"),
            "plan_hash": self.plan.plan_hash,
            "regression_argv": list(self.regression_argv),
            "roles": dict(PUBLIC_ROLES),
            "public_basis": [
                {"path": path, "sha256": expected} for path, expected in self.public_basis
            ],
        }


def load_public_plan(
    manifest: Path,
    expected_sha256: str,
    *,
    task: Mapping[str, Any],
    asset_root: Path,
    regression_argv: Sequence[str],
) -> FrozenPublicPlan:
    """Validate explicit dev-only inputs; no implicit or historical opt-in."""
    if task.get("split") != "dev":
        raise CatalogError("This public plan controller only accepts development input")
    if task.get("language") not in {"python", "go"}:
        raise CatalogError("Public plans require a validated Python or Go project runner")
    document = _document(_manifest_bytes(manifest, expected_sha256))
    if (
        set(document) != _FIELDS
        or document.get("schema") != "h12-public-plan-v1"
        or document.get("status") != "FROZEN"
    ):
        raise CatalogError("Public plan is not a complete frozen public schema")
    expected_identity = {
        "task_id": task.get("task_id"),
        "repository": task.get("repository"),
        "base_commit": task.get("base_commit"),
        "public_goal_sha256": _sha(str(task.get("goal", "")).encode()),
        "resources_sha256": canonical_hash(task.get("resources", {})),
    }
    if any(document.get(key) != value for key, value in expected_identity.items()):
        raise CatalogError("Public plan task, source, goal or resource binding differs")
    if (
        not isinstance(document["original_tree_sha256"], str)
        or not _HASH.fullmatch(document["original_tree_sha256"])
        or document["roles"] != PUBLIC_ROLES
    ):
        raise CatalogError("Public plan original-tree or verification roles differ")
    argv = document["regression_argv"]
    if (
        not isinstance(argv, list)
        or not argv
        or argv != list(regression_argv)
        or len(argv) > 128
        or any(
            not isinstance(item, str)
            or not item
            or len(item) > 4096
            or h12._PRIVATE_TOKEN.search(item)
            or h12._SHELL.search(item)
            for item in argv
        )
    ):
        raise CatalogError("Public plan regression must match the original fixed public argv")
    basis = document["public_basis"]
    if not isinstance(basis, list) or not 1 <= len(basis) <= 32:
        raise CatalogError("Public plan requires bounded public repository evidence")
    sources: list[tuple[str, str]] = []
    for entry in basis:
        if not isinstance(entry, dict) or set(entry) != {"path", "sha256"}:
            raise CatalogError("Public plan source evidence has unexpected fields")
        path, expected = entry["path"], entry["sha256"]
        if (
            not h12._safe_path(path)
            or "." in Path(path).parts
            or any(part.startswith(".") and part != ".github" for part in Path(path).parts)
            or not isinstance(expected, str)
            or not _HASH.fullmatch(expected)
            or path in {item[0] for item in sources}
        ):
            raise CatalogError("Public plan source evidence path or hash is invalid")
        sources.append((path, expected))
    try:
        plan = PublicProbePlan.model_validate(document["probe"])
        read_asset(plan, asset_root)
    except (DomainError, OSError, ValueError, ValidationError):
        raise CatalogError("Public plan probe or fixed read-only asset is unavailable") from None
    return FrozenPublicPlan(
        plan=plan,
        manifest_path=manifest,
        manifest_sha256=expected_sha256,
        asset_root=asset_root,
        task_id=document["task_id"],
        repository=document["repository"],
        base_commit=document["base_commit"],
        public_goal_sha256=document["public_goal_sha256"],
        resources_sha256=document["resources_sha256"],
        original_tree_sha256=document["original_tree_sha256"],
        regression_argv=tuple(argv),
        public_basis=tuple(sources),
    )


def validate_public_original(frozen: FrozenPublicPlan, workspace: Path) -> None:
    """Recheck bytes and original source immediately before a readiness/run gate."""
    _manifest_bytes(frozen.manifest_path, frozen.manifest_sha256)
    try:
        read_asset(frozen.plan, frozen.asset_root)
    except (DomainError, OSError, ValueError):
        raise CatalogError("Public probe asset changed before execution") from None
    root = workspace.resolve()
    if frozen.asset_root.resolve().is_relative_to(root):
        raise CatalogError("Public probe assets must be outside the model workspace")
    if _tree_hash(_scan(workspace)[0]) != frozen.original_tree_sha256:
        raise CatalogError("Public plan original source tree differs")
    for name, expected in frozen.public_basis:
        path = workspace / name
        if (
            any(
                parent.is_symlink()
                for parent in (path, *path.parents)
                if parent == workspace or parent.is_relative_to(workspace)
            )
            or not path.is_file()
            or not path.resolve().is_relative_to(root)
            or _sha(path.read_bytes()) != expected
        ):
            raise CatalogError("Public plan original public evidence changed")


def public_plan_readiness(frozen: FrozenPublicPlan, workspace: Path) -> dict[str, Any]:
    validate_public_original(frozen, workspace)
    return {
        "task_id": frozen.task_id,
        "status": "NOT_RUN",
        "asset_status": "BOUND",
        "manifest_sha256": frozen.manifest_sha256,
        "plan_hash": frozen.plan.plan_hash,
        "original_tree_sha256": frozen.original_tree_sha256,
        "reproduction": "NOT_RUN",
        "behavior": "AWAITING_IMPLEMENTATION",
        "regression": "NOT_RUN",
        "paid_model_calls": 0,
        "cost_micros": 0,
        "blockers": ["PUBLIC_REPRODUCTION_AND_REGRESSION_NOT_EXECUTED"],
    }
