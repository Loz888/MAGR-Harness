"""Explicit plans derived only from public goals and public project metadata."""

from __future__ import annotations

import re
from collections.abc import Mapping
from pathlib import PurePosixPath
from typing import Any, Literal

from mgar_v3.quality_eval import h12

Plan = tuple[tuple[str, ...], Literal["http", "oneshot"], int, str]


def application_plan(task: Mapping[str, Any], safe: Mapping[str, Any]) -> Plan:
    empty: Plan = ((), "http", 8000, "/")
    goal = str(safe["goal"])
    scope = tuple(safe["scope"])
    interpreters = {
        "python": ("python", "python3"),
        "javascript": ("node",),
        "typescript": ("node",),
    }.get(task["language"], ())
    one_shot = bool(
        re.search(r"\b(?:ephemeral|selfcheck|one.shot|cli|command.line)\b", goal, re.I)
        and re.search(r"\bexits?\b", goal, re.I)
    )
    if one_shot:
        commands = h12.public_commands(
            {"environment": task.get("environment", {}), "acceptance": {"regression": []}}
        )
        candidates = []
        for command in commands:
            if (
                task["language"] in {"javascript", "typescript"}
                and len(command) == 3
                and command[:2] == ["npm", "run"]
                and "package.json" in scope
                and re.fullmatch(r"[A-Za-z0-9_:-]{1,100}", command[2])
                and command[2] not in {"test", "lint", "build"}
            ):
                candidates.append(tuple(command))
                continue
            if not command or command[0] not in interpreters:
                continue
            args = list(command)
            if args[0] in {"python", "python3"}:
                args[0] = "python3"
                if args[1:2] == ["-B"]:
                    args.pop(1)
            if len(args) < 2 or args[1].startswith("-") or not _entry(args[1], scope):
                continue
            if any(part in {";", "&&", "||", "|", ">", "<"} for part in args):
                continue
            if args[0] == "python3":
                args.insert(1, "-B")
            candidates.append(tuple(args))
        unique = set(candidates)
        return (unique.pop(), "oneshot", 8000, "/") if len(unique) == 1 else empty
    suffixes = (".py",) if task["language"] == "python" else (".js", ".mjs")
    entries = [name for name in scope if name.endswith(suffixes) and _entry(name, scope)]
    port = re.search(r"\b(?:default(?:\s+port)?|port)\s*[:=]?\s*([0-9]{2,5})\b", goal, re.I)
    route = re.search(r"\bGET\s+(?:[`'\"](/[^`'\"]+)[`'\"]|(/\S+))", goal)
    if len(entries) != 1 or port is None or route is None or not interpreters:
        return empty
    path = route[1] or route[2].rstrip(".,;")
    if not re.fullmatch(r"/[A-Za-z0-9_./~%?&=+:-]*", path) or len(path) > 1024:
        return empty
    number = int(port[1])
    if not 1 <= number <= 65535:
        return empty
    argv = ("python3", "-B", entries[0]) if task["language"] == "python" else ("node", entries[0])
    return argv, "http", number, path


def _entry(path: str, scope: tuple[str, ...]) -> bool:
    parts = PurePosixPath(path).parts
    return bool(
        h12._safe_path(path)
        and not set(parts) & {"test", "tests", "verification"}
        and any(path == root or path.startswith(root.rstrip("/") + "/") for root in scope)
    )
