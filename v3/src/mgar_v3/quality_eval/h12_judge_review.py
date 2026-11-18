"""Private pre-freeze assertion inventory and two-way public requirement review."""

from __future__ import annotations

import ast
import hashlib
import re
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from mgar_v3.domain import DomainError, digest
from mgar_v3.harness.semantic_types import JudgeTraceabilityManifest, TaskBaselinePolicy


def _denied(reason: str) -> DomainError:
    return DomainError("JUDGE_TRACEABILITY_REQUIRED", reason, 409)


def assertion_inventory(
    sources: Mapping[str, bytes], *,
    observation_commands: Sequence[Sequence[str]] = (),
    observer_sources: Mapping[str, str] | None = None,
) -> dict[str, dict[str, Any]]:
    """Inventory actual assertions without exporting their expressions or private examples.

    Dynamic/external assertion programs need their own reviewed scanner. They
    must not be certified by claiming that a Python launcher has zero asserts.
    """
    result = {}
    observed: set[tuple[str, ...]] = set()
    expected: set[tuple[str, ...]] = set()
    if observation_commands:
        from mgar_v3.quality_eval.h12_judge_observation import validate_observation_commands

        expected = validate_observation_commands(
            sources, observation_commands, observer_sources or {})
    for name, data in sorted(sources.items()):
        if name in (observer_sources or {}):
            continue
        path = Path(name)
        if path.is_absolute() or ".." in path.parts or not name.endswith(".py"):
            raise _denied("This review scanner requires explicit canonical Python Judge sources")
        tree = ast.parse(data, filename=name)
        allowed_calls = set()
        if observation_commands:
            from mgar_v3.quality_eval.h12_judge_observation import observation_calls

            for call, argv in observation_calls(tree):
                if argv not in expected:
                    raise _denied("External observation argv differs from its frozen inventory")
                allowed_calls.add(id(call))
                observed.add(argv)
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and (
                isinstance(node.func, ast.Name)
                and node.func.id in {"exec", "eval", "compile"}
                or isinstance(node.func, ast.Attribute)
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id in {"subprocess", "os", "runpy", "importlib"}
            ):
                if id(node) not in allowed_calls:
                    raise _denied("Dynamic/external Judge program is not covered by this inventory")
            is_assert = isinstance(node, ast.Assert)
            is_raise = isinstance(node, ast.Raise)
            is_call = isinstance(node, ast.Call) and (
                isinstance(node.func, ast.Attribute)
                and (node.func.attr.startswith("assert") or node.func.attr in {"raises", "fail"})
            )
            if not (is_assert or is_raise or is_call):
                continue
            assert isinstance(node, (ast.Assert, ast.Raise, ast.Call))
            expression = hashlib.sha256(
                ast.dump(node, include_attributes=False).encode()
            ).hexdigest()
            fields = dict(
                file=name, line=node.lineno, column=node.col_offset, expression_hash=expression
            )
            identity = digest(fields)
            symbols = set()
            for part in ast.walk(node):
                if isinstance(part, ast.Attribute) and part.attr.startswith("_"):
                    symbols.add(part.attr)
                if (
                    isinstance(part, ast.Call)
                    and isinstance(part.func, ast.Name)
                    and (
                        part.func.id in {"getattr", "hasattr"}
                        and len(part.args) >= 2
                        and isinstance(part.args[1], ast.Constant)
                        and isinstance(part.args[1].value, str)
                    )
                ):
                    symbols.add(part.args[1].value)
                if isinstance(part, ast.Compare) and any(
                    isinstance(op, (ast.In, ast.NotIn)) for op in part.ops
                ):
                    for operand in [part.left, *part.comparators]:
                        if (
                            isinstance(operand, ast.Constant)
                            and isinstance(operand.value, str)
                            and re.fullmatch(r"[A-Za-z_]\w*", operand.value)
                        ):
                            symbols.add(operand.value)
            result[identity] = {
                "assertion_id": identity,
                **fields,
                "named_constraints": sorted(symbols),
            }
    if not result:
        raise _denied("A private Judge cannot establish coverage with an empty assertion inventory")
    if expected != observed:
        raise _denied("Frozen external observation command is absent from the Judge")
    return result


def review_judge(
    policy: TaskBaselinePolicy,
    manifest: JudgeTraceabilityManifest,
    sources: Mapping[str, bytes],
    *,
    task_id: str,
    commands: Sequence[Sequence[str]] | None = None,
) -> dict[str, Any]:
    if (
        manifest.task_id != task_id
        or manifest.benchmark_revision != policy.benchmark_revision
        or manifest.public_acceptance_hash != policy.public_acceptance_hash
    ):
        raise _denied("Judge review belongs to another public contract/revision")
    hashes = {name: hashlib.sha256(data).hexdigest() for name, data in sources.items()}
    if hashes != manifest.files:
        raise _denied("Private Judge files differ from the frozen review")
    if commands is not None:
        if not commands or manifest.command_hash != digest(commands):
            raise _denied("Judge execution plan differs from its frozen review")
        for command in commands:
            args = list(command)
            from mgar_v3.quality_eval.h12_judge import _normalize

            bootstrap = _normalize(["python3", "-m", "pytest"])
            prefix = next(
                (
                    p
                    for p in (
                        bootstrap,
                        ["python3", "-m", "pytest"],
                        ["python3", "-B", "-m", "pytest"],
                    )
                    if args[: len(p)] == p
                ),
                None,
            )
            if prefix is None:
                raise _denied("Private review requires the fixed Python pytest execution adapter")
            permitted = {
                "-q",
                "-v",
                "-vv",
                "-s",
                "--disable-warnings",
                "--tb=short",
                "--verbosity=2",
                "--maxfail=0",
                *sources,
            }
            if any(value not in permitted for value in args[len(prefix) :]):
                raise _denied("A reviewed Judge may not silently select only part of its source")
        if any(
            not any(
                value == name or name.startswith(value.rstrip("/") + "/")
                for command in commands
                for value in command
                if not value.startswith("-")
            )
            for name in sources if name not in manifest.observer_sources
        ):
            raise _denied("A frozen Judge source is absent from the execution plan")
    actual = assertion_inventory(
        sources, observation_commands=manifest.observation_commands,
        observer_sources=manifest.observer_sources,
    )
    claims = {row.assertion_id: row for row in manifest.assertions}
    if len(claims) != len(manifest.assertions) or set(claims) != set(actual):
        raise _denied("Every actual Judge assertion needs exactly one public requirement binding")
    requirements = {r.acceptance_requirement_id: r for r in policy.requirements}
    reverse: dict[str, list[str]] = {key: [] for key in requirements}
    for identity, row in claims.items():
        recorded = row.model_dump(mode="json", exclude={"acceptance_requirement_id"})
        if recorded != {k: v for k, v in actual[identity].items() if k != "named_constraints"}:
            raise _denied("Assertion location/expression differs from its frozen inventory")
        requirement = requirements.get(row.acceptance_requirement_id)
        if requirement is None:
            raise _denied("Judge assertion refers to an undeclared public requirement")
        for symbol in actual[identity]["named_constraints"]:
            if (
                re.search(r"(?<!\w)" + re.escape(symbol) + r"(?!\w)", requirement.description)
                is None
            ):
                raise _denied("Judge imposes a symbol name absent from its public requirement")
        reverse[row.acceptance_requirement_id].append(identity)
    # Lifecycle is independently verified by R04; it must not be fabricated as a Judge assertion.
    if any(
        not reverse[r.acceptance_requirement_id]
        for r in policy.requirements
        if r.kind != "lifecycle"
    ):
        raise _denied("A required public behavior/target/structure has no Judge assertion")
    return {
        "status": "PASS",
        "policy_hash": policy.policy_hash,
        "public_acceptance_hash": policy.public_acceptance_hash,
        "benchmark_revision": policy.benchmark_revision,
        "assertion_count": len(actual),
        "source_hashes": hashes,
        "requirement_assertions": reverse,
        "execution_plan_verified": commands is not None,
    }


def read_manifest(path: Path, expected_sha256: str) -> JudgeTraceabilityManifest:
    if path.is_symlink() or not path.is_file():
        raise _denied("Frozen private traceability file unavailable")
    raw = path.read_bytes()
    if hashlib.sha256(raw).hexdigest() != expected_sha256:
        raise _denied("Private traceability manifest changed")
    return JudgeTraceabilityManifest.model_validate_json(raw)
