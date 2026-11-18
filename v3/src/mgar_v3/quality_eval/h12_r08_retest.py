"""User-directed targeted repair evidence, without claiming a new full-suite run."""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

from mgar_v3.domain import DomainError


def _identities(collection: dict[str, Any], junit: Path) -> dict[str, str]:
    expected = {}
    for node in collection["nodeids"]:
        filename, test = node.split("::", 1)
        base, bracket, parameters = test.partition("[")
        parts = base.split("::")
        classname = ".".join([filename.removesuffix(".py").replace("/", "."), *parts[:-1]])
        key = (classname, parts[-1] + bracket + parameters)
        if key in expected:
            raise ValueError("Duplicate collected test identity")
        expected[key] = node
    outcomes = {}
    for case in ET.parse(junit).getroot().iter("testcase"):
        node = expected.get((case.get("classname", ""), case.get("name", "")))
        if node is None or node in outcomes:
            raise ValueError("JUnit identity differs from the collected scope")
        outcomes[node] = next((k for k in ("failure", "error", "skipped")
                               if case.find(k) is not None), "passed")
    return outcomes


def repair_coverage(
    previous: dict[str, str], current: set[str], retested: dict[str, str], changed: set[str]
) -> None:
    """Old passes are retained; every old failure and new/changed check must be executed."""
    if (not previous or not set(previous).issubset(current)
            or any(v not in {"passed", "failure"} for v in previous.values())
            or not changed.issubset(current)):
        raise ValueError("Repair cannot remove old checks or retain errors/skips")
    failed = {node for node, state in previous.items() if state == "failure"}
    required = failed | (current - set(previous)) | changed
    if (not failed or set(retested) != required
            or any(state != "passed" for state in retested.values())):
        raise ValueError("Targeted repair must pass exactly failed/new/changed identities")


def collection_scope(
    old: dict[str, Any], current: dict[str, Any],
    original_targets: list[str], registered_targets: list[str],
) -> None:
    """Preserve the original files and add exactly files owning new collected checks."""
    new_files = {n.split("::", 1)[0] for n in set(current["nodeids"]) - set(old["nodeids"])}
    if (old["targets"] != original_targets or current["targets"] != registered_targets
            or registered_targets[:len(original_targets)] != original_targets
            or len(set(registered_targets)) != len(registered_targets)
            or set(registered_targets) != set(original_targets) | new_files):
        raise ValueError("Collection cannot omit old scope or hide added test files")


def validate_targeted_engineering(root: Path, candidate: dict[str, Any]) -> None:
    from mgar_v3.quality_eval.h12_r08 import asset, document

    scope = document(root, candidate["user_retest_scope"])
    if scope.get("status") != "USER_DIRECTED_TARGETED_RETEST_ONLY":
        raise DomainError("H12_R08_ENGINEERING_REQUIRED", "Explicit repair scope required", 409)
    proof = candidate["engineering_repair"]
    baseline = document(root, proof["baseline_result"])
    old_collection = document(root, proof["baseline_collection"])
    current = document(root, candidate["engineering_evidence"]["collection"])
    registry = json.loads((root / "config/verification.json").read_bytes())
    original_targets = registry["HV4-H12-R08"]["tests"]
    if "baseline_registry" in proof:
        commit = baseline.get("git_commit", "")
        if not re.fullmatch(r"[a-f0-9]{40}", commit):
            raise ValueError("Original source commit is required for its registry")
        original = subprocess.run(
            ["git", "show", commit + ":v3/config/verification.json"], cwd=root,
            check=False, capture_output=True, timeout=10,
        )
        if (original.returncode or hashlib.sha256(original.stdout).hexdigest()
                != proof["baseline_registry"]["sha256"]):
            raise ValueError("Baseline registry differs from the original committed source")
        registered_original = document(root, proof["baseline_registry"])
        original_targets = registered_original["HV4-H12-R08"]["tests"]
    collection_scope(old_collection, current, original_targets, registry["HV4-H12-R08"]["tests"])
    source = candidate["source_sha256"]
    if (baseline.get("task") != "HV4-H12-R08" or baseline.get("status") != "FAIL"
            or baseline["source_sha256"] != baseline["finished_source_sha256"]
            or old_collection["source_sha256"] != baseline["source_sha256"]
            or current["source_sha256"] != source
            or current["finished_source_sha256"] != source):
        raise ValueError("Original full run and current collection must remain distinct and bound")
    old_junit = asset(root, proof["baseline_junit"])
    old_log = asset(root, proof["baseline_log"])
    if (asset(root, proof["baseline_result"]).parent != old_junit.parent
            or old_log.parent != old_junit.parent):
        raise ValueError("Original full evidence must share its actual attempt directory")
    previous = _identities(old_collection, old_junit)
    counts = baseline["counts"]
    if (len(previous) != counts["tests"] or len(previous) != len(old_collection["nodeids"])
            or sum(s == "failure" for s in previous.values()) != counts["failures"]
            or counts["errors"] or counts["skipped"]
            or not re.search(r"\b" + str(counts["failures"]) + r" failed\b", old_log.read_text())):
        raise ValueError("Original full failure inventory is incomplete")
    record = document(root, candidate["engineering"])
    evidence = candidate["engineering_evidence"]
    junit, log = asset(root, evidence["junit"]), asset(root, evidence["log"])
    targets = registry["HV4-H12-R08-REPAIR"]["tests"]
    if (record.get("task") != "HV4-H12-R08-REPAIR" or record.get("status") != "PASS"
            or record.get("command_exit_code") != 0 or record.get("exit_code") != 0
            or record.get("source_sha256") != source
            or record.get("finished_source_sha256") != source
            or record.get("git_commit") != candidate["source_commit"]
            or record["argv"][1:4] != ["-m", "pytest", "-q"]
            or record["argv"][4:-1] != targets
            or record["argv"][-1] != "--junitxml=" + str(junit)
            or asset(root, candidate["engineering"]).parent != junit.parent
            or log.parent != junit.parent):
        raise ValueError("Targeted run lacks its original registered source-bound receipt")
    retested = _identities(current, junit)
    repair_coverage(previous, set(current["nodeids"]), retested, set(proof["changed_check_nodes"]))
    if (set(retested) != set(targets) or record["counts"] != {
            "tests": len(retested), "failures": 0, "errors": 0, "skipped": 0}
            or not re.search(r"\b" + str(len(retested)) + r" passed\b", log.read_text())):
        raise ValueError("Targeted results differ from the exact approved scope")
    static = document(root, evidence["static_checks"])
    if static["source_sha256"] != source or static["finished_source_sha256"] != source:
        raise ValueError("Static checks belong to another source")
    for key, pattern in (("lint", r"All checks passed!"),
                         ("typecheck", r"Success: no issues found in [1-9][0-9]* source files?")):
        check = static["commands"][key]
        if (check["argv"] != ["make", key] or check["exit_code"] != 0
                or not re.search(pattern, asset(root, check["log"]).read_text())):
            raise ValueError("Static check evidence is incomplete")


def validate_retained_readiness(
    root: Path, candidate: dict[str, Any], ready: dict[str, Any], split: str,
    bundle: dict[str, Any],
) -> None:
    from mgar_v3.quality_eval.h12_r08 import document, load_bundle

    scope = document(root, candidate["user_retest_scope"])
    if (scope.get("status") != "USER_DIRECTED_TARGETED_RETEST_ONLY"
            or candidate.get("engineering_protocol") != "h12-r08-targeted-repair-v1"
            or ready.get("source_binding") != "validation-with-retained-executions"):
        raise ValueError("Retained executions require explicit user-directed scope")
    for name, row in ready["tasks"].items():
        origin = document(root, row["origin_readiness"])
        if (origin["source_sha256"] != origin["source_sha256_after"]
                or origin["tasks"][name]["status"] != "PASS"
                or origin["tasks"][name]["result"] != row["result"]
                or origin["experiment_id"] != ready["experiment_id"]):
            raise ValueError("Retained result is not an unchanged original executed PASS")
        old = load_bundle(root, origin["bundle"], split)
        task = next(t for t in bundle["tasks"] if t["task_id"] == name)
        old_task = next(t for t in old["tasks"] if t["task_id"] == name)
        key = task["repository"] + "@" + task["base_commit"]
        if (old_task != task or old["document"]["per_task"][name]
                != bundle["document"]["per_task"][name]
                or old["document"]["image_locks"][key]
                != bundle["document"]["image_locks"][key]):
            raise ValueError("Historical execution belongs to a changed task/plan/environment")
