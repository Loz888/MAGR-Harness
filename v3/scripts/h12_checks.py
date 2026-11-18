"""Run fixed H12 engineering checks and retain every attempt's raw evidence."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import time
import xml.etree.ElementTree as ET
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from verify import source_fingerprint

ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("stage", choices=("h12", "aggregate", "compatibility", "lint", "typecheck"))
    parser.add_argument(
        "--evidence-root", type=Path, default=Path("evidence/harness-v4/HV4-H12")
    )
    args = parser.parse_args()
    evidence_root = (ROOT / args.evidence_root).resolve()
    if not evidence_root.is_relative_to(ROOT / "evidence"):
        parser.error("evidence-root must be inside the repository evidence directory")
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    target = evidence_root / f"{stamp}-{args.stage}-{uuid4().hex[:8]}"
    target.mkdir(parents=True, mode=0o700)
    basetemp = Path("/tmp") / ("magr-h12-" + target.name)
    junit = target / "junit.xml"
    if args.stage in {"h12", "lint", "typecheck"}:
        command = (
            ["make", "verify", "TASK=HV4-H12"]
            if args.stage == "h12"
            else ["make", args.stage]
        )
    else:
        if args.stage == "aggregate":
            tests = sorted(
                str(p.relative_to(ROOT)) for p in (ROOT / "tests").glob("test_harness_v4_h*.py")
            )
            tests.append("tests/test_h11_loop.py")
        else:
            tests = [
                "tests/test_v33_context_compaction.py",
                "tests/test_r4_context.py",
                "tests/test_recovery.py",
            ]
        command = ["uv", "run", "--frozen", "pytest", "-q", *tests, f"--junitxml={junit}"]
    started = time.monotonic()
    record = {
        "stage": args.stage,
        "argv": command,
        "cwd": str(ROOT),
        "started_at": datetime.now(UTC).isoformat(),
        "source_sha256": source_fingerprint(ROOT),
        "paid_model_calls": 0,
        "pytest_basetemp": str(basetemp),
        "live_model_gate": "NOT_RUN",
        "live_agent_gate": "NOT_RUN",
        "live_publication_gate": "NOT_RUN",
    }
    (target / "command.json").write_text(json.dumps(record, indent=2) + "\n")
    env = {**os.environ, "UV_CACHE_DIR": str(ROOT / ".local/uv-cache")}
    env["PYTEST_ADDOPTS"] = (
        env.get("PYTEST_ADDOPTS", "") + f" --basetemp={basetemp} --tb=short"
    ).strip()
    with (target / "command.log").open("wb") as log:
        completed = subprocess.run(command, cwd=ROOT, env=env, stdout=log, stderr=subprocess.STDOUT)
    counts = None
    if junit.exists():
        counts = {key: 0 for key in ("tests", "failures", "errors", "skipped")}
        for suite in ET.parse(junit).getroot().iter("testsuite"):
            for key in counts:
                counts[key] += int(suite.get(key, "0"))
    record.update(
        exit_code=completed.returncode,
        finished_at=datetime.now(UTC).isoformat(),
        wall_seconds=round(time.monotonic() - started, 3),
        finished_source_sha256=source_fingerprint(ROOT),
        counts=counts,
    )
    passed = (
        completed.returncode == 0
        and record["source_sha256"] == record["finished_source_sha256"]
        and (counts is None or (counts["tests"] > 0 and not any(
            counts[key] for key in ("failures", "errors", "skipped")
        )))
    )
    record["status"] = "PASS" if passed else "FAIL"
    (target / "result.json").write_text(json.dumps(record, indent=2) + "\n")
    print(json.dumps({"evidence": str(target.relative_to(ROOT)), **record}))
    return 0 if passed else (completed.returncode or 1)


if __name__ == "__main__":
    raise SystemExit(main())
