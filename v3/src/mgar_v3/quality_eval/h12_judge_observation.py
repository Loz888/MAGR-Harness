"""Explicit v2 review of fixed raw observers; Python owns all truth assertions.

This is a deliberately narrow adapter, not a general JavaScript/Go verifier.
The full source closure and command strings are hash-bound by the private
traceability manifest. The independent curator must also review their meaning.
No observer is a Worker command and no observer grants sandbox authority.
"""

from __future__ import annotations

import ast
import hashlib
import re
from collections.abc import Mapping, Sequence
from pathlib import PurePosixPath

from mgar_v3.domain import DomainError


def _deny(message: str) -> DomainError:
    return DomainError("JUDGE_TRACEABILITY_REQUIRED", message, 409)


def _raw_observer(source: str) -> None:
    # Fail closed on explicit independent verdicts or another opaque program.
    # These checks supplement full independent semantic review, not replace it.
    forbidden = (
        r"\b(?:assert\w*|throw|panic|eval|exec|spawn\w*)\s*\(",
        r"\b(?:process\s*\.\s*exit|os\s*\.\s*Exit|log\s*\.\s*Fatal\w*|"
        r"\w+\s*\.\s*(?:Fatalf?|Errorf|FailNow|Skipf?))\s*\(",
        r"\b(?:child_process|syscall|unsafe)\b",
        r"\b(?:if|switch|select)\s*(?:\(|\{|\w)",
    )
    if len(source.encode()) > 262144 or any(re.search(p, source) for p in forbidden):
        raise _deny("Observer must return raw observations without an external verdict/program")


def validate_observation_commands(
    sources: Mapping[str, bytes],
    commands: Sequence[Sequence[str]],
    observer_sources: Mapping[str, str],
) -> set[tuple[str, ...]]:
    expected = {tuple(argv) for argv in commands}
    if len(expected) != len(commands) or not expected:
        raise _deny("Fixed observation command inventory is empty or duplicated")
    actual_observers = {name for name in sources if not name.endswith(".py")}
    if actual_observers != set(observer_sources):
        raise _deny("Every external observer needs its complete source closure")
    used_sources: set[str] = set()
    for name in actual_observers:
        path = PurePosixPath(name)
        if (
            not name.startswith("tests/hidden/")
            or ".." in path.parts
            or path.is_absolute()
            or path.suffix not in {".go", ".js", ".mjs"}
            or hashlib.sha256(sources[name]).hexdigest() != observer_sources[name]
        ):
            raise _deny("External observer source scope or hash differs")
        _raw_observer(sources[name].decode())
    for argv in expected:
        if not argv or len(argv) > 32 or any(not v or len(v) > 262144 or "\x00" in v for v in argv):
            raise _deny("Observer command is not bounded")
        if argv[:2] == ("node", "-e") and len(argv) == 3:
            _raw_observer(argv[2])
        elif argv[:3] == ("node", "--input-type=module", "-e") and len(argv) == 4:
            _raw_observer(argv[3])
        elif (
            argv[:4] == ("node", "--import=tsx/esm", "--input-type=module", "-e") and len(argv) == 5
        ):
            _raw_observer(argv[4])
        elif argv[:2] == ("go", "run") and (
            len(argv) == 3 or len(argv) == 5 and argv[2:4] == ("-p=1", "-buildvcs=false")
        ):
            if argv[-1] not in actual_observers or not argv[-1].endswith(".go"):
                raise _deny("Go observer needs a fixed complete private source")
            used_sources.add(argv[-1])
        elif argv[0] == "node" and len(argv) == 2:
            if argv[1] not in actual_observers or not argv[1].endswith((".js", ".mjs")):
                raise _deny("Node observer needs a fixed complete private source")
            used_sources.add(argv[1])
        else:
            raise _deny("Unsupported external observation adapter")
    if used_sources != actual_observers:
        raise _deny("Unused observer files may not carry undisclosed assertions")
    return expected


def observation_calls(tree: ast.Module) -> list[tuple[ast.Call, tuple[str, ...]]]:
    constants: dict[str, object] = {}
    for item in tree.body:
        if isinstance(item, ast.Assign) and len(item.targets) == 1:
            if isinstance(item.targets[0], ast.Name):
                try:
                    constants[item.targets[0].id] = ast.literal_eval(item.value)
                except (ValueError, TypeError):
                    pass

    class Resolve(ast.NodeTransformer):
        def visit_Name(self, node: ast.Name) -> ast.AST:
            value = constants.get(node.id)
            return ast.Constant(value=value) if isinstance(value, (str, int, bool)) else node

    result = []
    for node in ast.walk(tree):
        if not (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "subprocess"
            and node.func.attr == "run"
        ):
            continue
        if len(node.args) != 1 or any(isinstance(a, ast.Starred) for a in node.args):
            raise _deny("Observation argv must be one frozen literal vector")
        try:
            expression = ast.parse(ast.unparse(node.args[0]), mode="eval")
            argv = ast.literal_eval(Resolve().visit(expression))
            options = {kw.arg: ast.literal_eval(kw.value) for kw in node.keywords}
        except (ValueError, TypeError, SyntaxError):
            raise _deny(
                "Observation argv/options cannot depend on unreviewed runtime values"
            ) from None
        if (
            not isinstance(argv, (list, tuple))
            or not all(isinstance(v, str) for v in argv)
            or len(options) != len(node.keywords)
            or set(options) != {"capture_output", "text", "timeout", "check"}
            or options["capture_output"] is not True
            or options["text"] is not True
            or options["check"] is not False
            or type(options["timeout"]) not in {int, float}
            or not 0 < options["timeout"] <= 60
        ):
            raise _deny("Observer requires bounded raw capture without shell/env/cwd authority")
        result.append((node, tuple(argv)))
    return result
