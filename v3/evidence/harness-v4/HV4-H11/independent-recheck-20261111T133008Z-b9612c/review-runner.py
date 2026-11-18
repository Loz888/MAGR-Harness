import json
import os
import runpy
import shutil
import subprocess
import sys
import time
import uuid
import xml.etree.ElementTree as ET
from datetime import UTC, datetime
from pathlib import Path

root = Path(__file__).resolve().parents[4]
desktop = Path(os.environ["H11_REVIEW_WORKSPACE"]).resolve()
fingerprint = runpy.run_path(str(root / "scripts/verify.py"))["source_fingerprint"]
expected = "a0cc8fac7467f7a99866df01c8ebea9f611f9844c0001c2d4130190fd9676cf2"
assert fingerprint(root) == expected
out = root / "evidence/harness-v4/HV4-H11" / (
    "independent-recheck-" + datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:6]
)
out.mkdir(mode=0o700, parents=True)
(desktop / "work/h11_review_evidence_path.txt").write_text(str(out))
receipt = {
    "source_commit": "d3f3457b3d9dfab68bc73a40add299e4e633fbeb",
    "head_at_review": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=root, text=True).strip(),
    "source_sha256": expected, "started_at": datetime.now(UTC).isoformat(),
    "commands": [], "paid_calls": 0,
}
(out / "start.json").write_text(json.dumps(receipt, indent=2))
print("EVIDENCE=" + str(out), flush=True)
env = dict(os.environ, PATH=str(Path.home() / ".local/bin") + os.pathsep + os.environ["PATH"],
           UV_CACHE_DIR=str(root / ".local/uv-cache"), UV_OFFLINE="1", UV_PYTHON_DOWNLOADS="never")
aggregate = [str(p.relative_to(root)) for p in sorted((root / "tests").glob("test_harness_v4_h*.py"))]
aggregate.append("tests/test_h11_loop.py")
commands = [
    ("lint", ["make", "lint"]),
    ("typecheck", ["make", "typecheck"]),
    ("h11-registered", ["make", "verify", "TASK=HV4-H11"]),
    ("legacy-context", ["uv", "run", "--frozen", "pytest", "-q",
                        "tests/test_v33_context_compaction.py", "tests/test_r4_context.py"]),
    ("engine-recovery", ["uv", "run", "--frozen", "pytest", "-q", "tests/test_recovery.py"]),
    ("h00-h11", ["uv", "run", "--frozen", "pytest", "-q", *aggregate]),
]
for name, command in commands:
    assert fingerprint(root) == expected
    if name == "h11-registered":
        pointer = root / "evidence/HV4-H11/acceptance.json"
        if pointer.exists():
            shutil.copy2(pointer, out / "h11-previous-latest-acceptance.json")
    fault_dir = out / (name + "-faults")
    fault_dir.mkdir()
    run_env = dict(env, PYTEST_ADDOPTS="--tb=short --basetemp=/tmp/magr-h11-review-" + uuid.uuid4().hex,
                   H11_EVIDENCE_DIR=str(fault_dir))
    junit = out / (name + ".xml")
    if "pytest" in command:
        command = [*command, "--junitxml=" + str(junit)]
    before = time.monotonic()
    with (out / (name + ".log")).open("wb") as log:
        process = subprocess.Popen(command, cwd=root, env=run_env, stdout=log, stderr=subprocess.STDOUT)
        try:
            code = process.wait(timeout=1200)
        except subprocess.TimeoutExpired:
            process.terminate()
            process.wait(timeout=30)
            code = 124
    entry = {
        "name": name, "argv": command, "cwd": str(root), "exit_code": code,
        "seconds": round(time.monotonic() - before, 3),
        "log": str((out / (name + ".log")).relative_to(root)),
        "fault_evidence": str(fault_dir.relative_to(root)),
    }
    if name == "h11-registered":
        pointer = root / "evidence/HV4-H11/acceptance.json"
        if pointer.exists():
            shutil.copy2(pointer, out / "h11-current-latest-acceptance.json")
            data = json.loads(pointer.read_text())
            entry.update(counts=data["counts"], registered_result=data["attempt_path"] + "/result.json",
                         registered_source_sha256=data["source_sha256"])
    elif junit.exists():
        tree = ET.parse(junit).getroot()
        suites = list(tree) if tree.tag == "testsuites" else [tree]
        entry["counts"] = {key: sum(int(s.get(key, 0)) for s in suites)
                           for key in ["tests", "failures", "errors", "skipped"]}
        entry["junit"] = str(junit.relative_to(root))
    receipt["commands"].append(entry)
    receipt["finished_source_sha256"] = fingerprint(root)
    (out / "run-results.json").write_text(json.dumps(receipt, indent=2))
    print(json.dumps(entry), flush=True)
    assert receipt["finished_source_sha256"] == expected
receipt["finished_at"] = datetime.now(UTC).isoformat()
receipt["checks_passed"] = all(c["exit_code"] == 0 for c in receipt["commands"])
(out / "run-results.json").write_text(json.dumps(receipt, indent=2))
sys.exit(0 if receipt["checks_passed"] else 1)
