"""Conservative identities from complete public test reports, never count deltas."""

from __future__ import annotations

import re
from collections.abc import Sequence

from mgar_v3.harness.project_commands import infer_test_runner

_ANSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")


def failed_test_ids(argv: Sequence[str], output: str, expected: int) -> tuple[str, ...] | None:
    """Return identities only when the report enumerates every observed failure.

    Unsupported, incomplete and ambiguous reporters remain unknown. In
    particular equal failure counts do not establish equal failing tests.
    """
    if expected <= 0:
        return () if expected == 0 else None
    text = _ANSI.sub("", output)
    runner = infer_test_runner(argv)
    rows: list[str] = []
    if runner == "pytest":
        rows = [line[7:].split(" - ", 1)[0].strip()
                for line in text.splitlines() if line.startswith("FAILED ")]
        if any("::" not in row or not row.split("::", 1)[0].endswith(".py") for row in rows):
            return None
    elif runner == "go":
        pending: list[str] = []
        for line in text.splitlines():
            test = re.fullmatch(r"--- FAIL: (\S+) \([\d.]+s\)", line)
            package = re.match(r"^FAIL\s+(\S+)\s+[\d.]+s(?:\s|$)", line)
            if test:
                pending.append(test[1])
            elif package:
                rows.extend(package[1] + "::" + name for name in pending)
                pending = []
        if pending:
            return None
    elif runner == "node":
        rows = re.findall(r"^not ok \d+ - (.+)$", text, re.MULTILINE)
    elif runner == "unittest":
        rows = re.findall(r"^(?:FAIL|ERROR): (.+)$", text, re.MULTILINE)
    else:
        return None
    if len(rows) != expected or len(set(rows)) != expected or any(not row for row in rows):
        return None
    return tuple(sorted(rows))
