"""Verify the separately retained evidence for the GA00 frozen baseline."""

from __future__ import annotations

import hashlib
import json
import subprocess
from collections import Counter
from pathlib import Path
from xml.etree import ElementTree as ET

APP = Path(__file__).resolve().parents[2]
RECORD = Path(__file__).with_name("accept") / "GA00" / "accept.json"


def require(predicate: bool, message: str) -> None:
    if not predicate:
        raise SystemExit(message)


def main() -> None:
    record = json.loads(RECORD.read_text(encoding="utf-8"))
    baseline = record["baseline"]
    tree = subprocess.check_output(
        ("git", "rev-parse", baseline["commit"] + "^{tree}"), cwd=APP, text=True,
    ).strip()
    require(tree == baseline["git_tree"], "Frozen Git tree differs")
    pytest = record["checks"]["pytest"]
    for entry in [*record["evidence"], record["taskbook"], pytest["non_passing_cases"]]:
        path = (APP / entry["path"]).resolve()
        require(path.is_relative_to(APP) and path.is_file(), f"Missing evidence: {entry['path']}")
        with path.open("rb") as stream:
            digest = hashlib.file_digest(stream, "sha256").hexdigest()
        require(digest == entry["sha256"], f"Evidence checksum differs: {entry['path']}")
    counts: Counter[str] = Counter()
    observed: set[str] = set()
    non_passing: dict[str, str] = {}
    for shard in pytest["shards"]:
        result = json.loads((APP / shard["result_path"]).read_text(encoding="utf-8"))
        require(result["exit_code"] == shard["exit_code"], f"Exit code differs: {shard['id']}")
        report = (APP / shard["report_path"]).resolve()
        require(report.is_relative_to(APP), "Report path escapes the checkout")
        shard_counts: Counter[str] = Counter({key: 0 for key in pytest["totals"]})
        for case in ET.parse(report).getroot().iter("testcase"):
            path = case.attrib["file"]
            module = path.removesuffix(".py").replace("/", ".")
            classname = case.attrib["classname"].removeprefix(module).removeprefix(".")
            node = "::".join(part for part in (
                path, classname.replace(".", "::"), case.attrib["name"],
            ) if part)
            require(node not in observed, f"Duplicate JUnit case: {node}")
            observed.add(node)
            if case.find("error") is not None:
                outcome = "errors"
            elif case.find("failure") is not None:
                outcome = "failed"
            elif case.find("skipped") is not None:
                outcome = "skipped"
            else:
                outcome = "passed"
            shard_counts[outcome] += 1
            if outcome != "passed":
                non_passing[node] = outcome
        require(dict(shard_counts) == shard["counts"], f"Counts differ: {shard['id']}")
        counts.update(shard_counts)
    require(dict(counts) == pytest["totals"], "JUnit totals differ")
    register = json.loads((APP / pytest["non_passing_cases"]["path"]).read_text(encoding="utf-8"))
    registered = {case["nodeid"]: case["kind"] for case in register}
    require(len(register) == len(registered), "Duplicate non-passing case in record")
    require(registered == non_passing, "Non-passing cases differ from the JUnit reports")
    passing = not non_passing and all(shard["exit_code"] == 0 for shard in pytest["shards"])
    require(pytest["status"] == ("PASS" if passing else "FAIL"), "Pytest status differs")
    collection = APP / Path(pytest["shards"][0]["report_path"]).parents[1] / "collected-nodeids.txt"
    expected = collection.read_text(encoding="utf-8").splitlines()
    require(observed == set(expected), "Test identities differ from the collection")
    require(
        len(expected) == sum(counts.values()) == pytest["collected"],
        "Collection count differs",
    )
    require(record["real_provider"]["model_calls"] == 0, "Unexpected model calls")
    print(f"GA00 evidence verified for {baseline['commit'][:12]}")
    print(json.dumps(pytest["totals"], ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
