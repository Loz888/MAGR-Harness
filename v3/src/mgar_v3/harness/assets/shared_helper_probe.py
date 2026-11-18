"""Fixed stdlib-only public structural check. Never imports the project."""

from __future__ import annotations

import ast
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any


def shared_helper(source: str, spec: dict[str, Any]) -> bool:
    tree = ast.parse(source)
    owners = [n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == spec["owner_class"]]
    if len(owners) != 1:
        return False
    owner = owners[0]
    for node in tree.body:
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            if any(
                isinstance(t, ast.Name)
                and t.id == owner.name
                or isinstance(t, ast.Attribute)
                and isinstance(t.value, ast.Name)
                and t.value.id == owner.name
                for t in targets
            ):
                return False
    methods = {
        n.name: n for n in owner.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    module = {n.name: n for n in tree.body if isinstance(n, ast.FunctionDef)}
    available = {"method:" + n for n in methods if n not in spec["callers"]}
    available |= {"module:" + n for n in module}
    common = set(available)
    for name in spec["callers"]:
        method = methods.get(name)
        if method is None or len(method.args.args) < 2:
            return False
        if any(
            isinstance(node, (ast.If, ast.For, ast.While, ast.Try, ast.Match, ast.Lambda))
            for node in ast.walk(method)
        ):
            raise ValueError("Dynamic caller flow needs a separate structural adapter")
        receiver = method.args.args[0].arg
        environment = {method.args.args[1].arg: {"@key"}}
        accesses: list[set[str]] = []

        def expression(
            node: ast.AST,
            bindings: dict[str, set[str]] = environment,
            current_receiver: str = receiver,
        ) -> set[str]:
            if isinstance(node, ast.Name):
                return bindings.get(node.id, set())
            if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
                if node.value.id == current_receiver:
                    if node.attr in methods:
                        return {"@call:method:" + node.attr}
                    return {"@mapping"}
            if isinstance(node, ast.Call):
                values = set().union(*(expression(a) for a in node.args))
                if isinstance(node.func, ast.Attribute):
                    values |= expression(node.func.value)
                callee = expression(node.func)
                if isinstance(node.func, ast.Name) and node.func.id in module:
                    callee = {"@call:module:" + node.func.id}
                if "@key" in values:
                    helpers = {c.removeprefix("@call:") for c in callee if c.startswith("@call:")}
                    values |= helpers if helpers else {"@outside-transform"}
                return values
            return set().union(*(expression(child) for child in ast.iter_child_nodes(node)))

        def visit(
            statements: list[ast.stmt],
            bindings: dict[str, set[str]] = environment,
            found: list[set[str]] = accesses,
        ) -> None:
            for statement in statements:
                if isinstance(statement, (ast.Assign, ast.AnnAssign)):
                    value = expression(statement.value) if statement.value is not None else set()
                    targets = (
                        statement.targets
                        if isinstance(statement, ast.Assign)
                        else [statement.target]
                    )
                    for target in targets:
                        if isinstance(target, ast.Name):
                            bindings[target.id] = value
                for node in ast.walk(statement):
                    if isinstance(node, ast.Subscript) and "@mapping" in expression(node.value):
                        # A subscript result is a value, not the original mapping.
                        if isinstance(node.value, (ast.Name, ast.Attribute)):
                            flow = expression(node.slice)
                            found.append(
                                set() if "@outside-transform" in flow else flow & available
                            )
                    if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                        if "@mapping" in expression(node.func.value) and node.args:
                            if node.func.attr in {"get", "pop", "setdefault", "__contains__"}:
                                flow = expression(node.args[0])
                                found.append(
                                    set() if "@outside-transform" in flow else flow & available
                                )
                # Flow-sensitive bindings in branches/loops are conservative:
                # each mapping use must independently depend on the same helper.
                children: list[ast.stmt] = []
                for field in ("body", "orelse", "finalbody"):
                    body_value = getattr(statement, field, None)
                    if isinstance(body_value, list):
                        children.extend(body_value)
                if children:
                    visit(children)

        visit(method.body)
        if not accesses:
            return False
        shared = accesses[0].copy()
        for access in accesses[1:]:
            shared &= access
        common &= shared
    if spec.get("required_name"):
        common &= {"method:" + spec["required_name"], "module:" + spec["required_name"]}
    return len(common) == 1


def main(spec: dict[str, Any], plan_id: str, go_program: str | None = None) -> None:
    status, detail = "error", "Structural source could not be verified"
    try:
        if spec.get("language") == "go":
            if go_program is None:
                raise ValueError("Fixed Go parser asset is missing")
            target = Path("/scratch") / (
                "r06-go-" + hashlib.sha256(go_program.encode()).hexdigest() + ".go"
            )
            target.write_text(go_program)
            result = subprocess.run(
                [
                    "go",
                    "run",
                    "-p=1",
                    str(target),
                    "--",
                    str(Path.cwd() / spec["path"]),
                    json.dumps(spec["callers"]),
                    spec.get("required_name") or "",
                ],
                capture_output=True,
                text=True,
                timeout=90,
                env={**os.environ, "GO111MODULE": "off", "GOMAXPROCS": "2"},
            )
            if result.returncode != 0:
                raise ValueError(
                    f"Fixed Go source parser exited {result.returncode}: "
                    + result.stderr[-800:]
                )
            observed = json.loads(result.stdout)
            if type(observed.get("passed")) is not bool or observed.get("supported") is not True:
                raise ValueError("Go structural observation malformed")
            passed = observed["passed"]
        else:
            passed = shared_helper((Path.cwd() / spec["path"]).read_text(), spec)
        status = "passed" if passed else "assertion_failed"
        detail = (
            "One shared helper return feeds every declared result"
            if spec.get("language") == "go"
            else "One shared helper return feeds every declared mapping-key operation"
        )
    except (OSError, ValueError, SyntaxError, TypeError, subprocess.TimeoutExpired) as exc:
        detail = ("Structural source could not be verified: " + str(exc))[:1000]
    print(
        "HARNESS_PUBLIC_PROBE_V1 "
        + json.dumps(
            {
                "plan_id": plan_id,
                "asset_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
                "cases": [
                    {"id": spec["acceptance_requirement_id"], "status": status, "detail": detail}
                ],
            }
        )
    )
    sys.exit(0 if status == "passed" else 1 if status == "assertion_failed" else 2)
