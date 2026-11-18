"""Closed engineering evidence for R08; a standalone PASS JSON grants nothing."""

from __future__ import annotations

import json
import re
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

from mgar_v3.domain import DomainError


def validate_engineering(root: Path, candidate: dict[str, Any]) -> None:
    try:
        if candidate.get("engineering_protocol") == "h12-r08-targeted-repair-v1":
            from mgar_v3.quality_eval.h12_r08_retest import validate_targeted_engineering

            validate_targeted_engineering(root, candidate)
        else:
            _validate_engineering(root, candidate)
    except (KeyError, TypeError, ValueError, OSError, ET.ParseError) as exc:
        raise DomainError(
            "H12_R08_ENGINEERING_REQUIRED",
            "Registered engineering evidence is incomplete or changed",
            409,
        ) from exc


def _validate_engineering(root: Path, candidate: dict[str, Any]) -> None:
    from mgar_v3.quality_eval.h12_r08 import asset, document

    def denied() -> DomainError:
        return DomainError(
            "H12_R08_ENGINEERING_REQUIRED",
            "Registered engineering evidence is incomplete or changed",
            409,
        )

    result_path = asset(root, candidate["engineering"])
    record = document(root, candidate["engineering"])
    source = candidate["source_sha256"]
    registry = json.loads((root / "config/verification.json").read_bytes())["HV4-H12-R08"]
    counts = record.get("counts", {})
    if (
        record.get("task") != "HV4-H12-R08"
        or record.get("status") != "PASS"
        or record.get("engineering_gate") != "PASS"
        or record.get("command_exit_code") != 0
        or record.get("exit_code") != 0
        or record.get("source_sha256") != source
        or record.get("finished_source_sha256") != source
        or record.get("git_commit") != candidate["source_commit"]
        or not counts.get("tests")
        or any(counts.get(k) != 0 for k in ("failures", "errors", "skipped"))
    ):
        raise denied()
    evidence = candidate["engineering_evidence"]
    junit = asset(root, evidence["junit"])
    log = asset(root, evidence["log"])
    if (
        result_path.parent != junit.parent
        or result_path.parent != log.parent
        or record.get("attempt_path") != str(result_path.parent.relative_to(root))
        or record.get("log_path") != str(log.relative_to(root))
    ):
        raise denied()
    argv = record.get("argv", [])
    if (
        len(argv) < 5
        or argv[1:4] != ["-m", "pytest", "-q"]
        or argv[4:-1] != registry["tests"]
        or argv[-1] != "--junitxml=" + str(junit)
    ):
        raise denied()
    suites = ET.parse(junit).getroot()
    actual = {
        k: sum(int(s.get(k, "0")) for s in suites.iter("testsuite"))
        for k in ("tests", "failures", "errors", "skipped")
    }
    cases = list(suites.iter("testcase"))
    identities = {(c.get("classname"), c.get("name")) for c in cases}
    if (
        actual != counts
        or len(cases) != counts["tests"]
        or len(identities) != len(cases)
        or any(c.find(k) is not None for c in cases for k in ("failure", "error", "skipped"))
        or not re.search(r"\b" + str(counts["tests"]) + r" passed\b", log.read_text())
    ):
        raise denied()
    collection = document(root, evidence["collection"])
    if (
        collection.get("source_sha256") != source
        or collection.get("targets") != registry["tests"]
        or len(collection.get("nodeids", [])) != counts["tests"]
        or len(set(collection["nodeids"])) != counts["tests"]
    ):
        raise denied()
    static = document(root, evidence["static_checks"])
    if static.get("source_sha256") != source or static.get("finished_source_sha256") != source:
        raise denied()
    for key, pattern in (
        ("lint", r"All checks passed!"),
        ("typecheck", r"Success: no issues found in [1-9][0-9]* source files?"),
    ):
        check = static["commands"][key]
        if (
            check.get("argv") != ["make", key]
            or check.get("exit_code") != 0
            or not re.search(pattern, asset(root, check["log"]).read_text())
        ):
            raise denied()
