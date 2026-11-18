"""Explicit, hash-bound public-test amendments for a new benchmark copy only.

This preparation helper grants no paid execution and does not modify the H00
catalog, source repository, runtime tools, scoring rules, or historical results.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from pathlib import Path, PurePosixPath
from typing import Any

from mgar_v3.quality_eval.harness_v4 import CatalogError


def public_baseline_contract_matches(
    contract: Mapping[str, Any],
    task: Mapping[str, Any],
    actual_assets: Mapping[str, str],
    observed: Mapping[str, Any],
) -> bool:
    """A new revision declares expected red assets explicitly, without English heuristics."""
    if (
        contract.get("schema") != "h12-public-red-baseline-v2"
        or contract.get("repository") != task.get("repository")
        or contract.get("base_commit") != task.get("base_commit")
        or contract.get("judge_sha256") != task.get("judge_sha256")
    ):
        return False
    assets = contract.get("assets")
    if not isinstance(assets, dict) or not assets:
        return False
    for name, expected in assets.items():
        if (
            not isinstance(name, str)
            or not isinstance(expected, str)
            or not re.fullmatch(r"[a-f0-9]{64}", expected)
            or not name.startswith("tests/")
            or not name.endswith(".py")
            or ".." in PurePosixPath(name).parts
            or "\\" in name
            or actual_assets.get(name) != expected
        ):
            return False
    from mgar_v3.quality_eval.h12_readiness import reference_failures_reproduced

    return reference_failures_reproduced(observed, assets)


def apply_public_amendments(
    workspace: Path,
    manifest: Path,
    expected_sha256: str,
    *,
    repository: str,
    commit: str,
) -> dict[str, Any]:
    raw = manifest.read_bytes()
    if hashlib.sha256(raw).hexdigest() != expected_sha256:
        raise CatalogError("Benchmark amendment manifest changed")
    document = json.loads(raw)
    if (
        document.get("schema") != "h12-public-test-amendment-v1"
        or document.get("repository") != repository
        or document.get("base_commit") != commit
        or document.get("revision") != "h12-benchmark-v2"
    ):
        raise CatalogError("Benchmark amendment source binding differs")
    prepared: list[tuple[Path, bytes, dict[str, Any]]] = []
    seen: set[str] = set()
    for row in document["files"]:
        name = row["path"]
        relative = PurePosixPath(name)
        if (
            relative.is_absolute()
            or ".." in relative.parts
            or "\\" in name
            or not relative.parts
            or relative.parts[0] not in {"test", "tests"}
            or name in seen
        ):
            raise CatalogError("Amendments may only change distinct public test files")
        seen.add(name)
        path = workspace / name
        if any(
            (workspace.joinpath(*relative.parts[:i])).is_symlink()
            for i in range(1, len(relative.parts) + 1)
        ):
            raise CatalogError("Amendment target may not traverse symlinks")
        original = path.read_bytes()
        if hashlib.sha256(original).hexdigest() != row["before_sha256"]:
            raise CatalogError("Amendment preimage differs")
        content = original.decode("utf-8")
        for change in row["replacements"]:
            before, after = change["before"], change["after"]
            if not before or content.count(before) != 1:
                raise CatalogError("Amendment replacement is ambiguous")
            content = content.replace(before, after, 1)
        amended = content.encode("utf-8")
        if hashlib.sha256(amended).hexdigest() != row["after_sha256"]:
            raise CatalogError("Amendment result differs")
        prepared.append((path, amended, row))
    if not prepared:
        raise CatalogError("Empty amendment inventory")
    # Callers use a disposable, exclusively owned evaluator copy. All bindings
    # above are checked before the first file is changed; failures invalidate it.
    for path, amended, _ in prepared:
        path.write_bytes(amended)
    return {
        "revision": document["revision"],
        "manifest_sha256": expected_sha256,
        "repository": repository,
        "base_commit": commit,
        "files": [
            {k: row[k] for k in ("path", "before_sha256", "after_sha256")} for _, _, row in prepared
        ],
        "paid_calls": 0,
    }
