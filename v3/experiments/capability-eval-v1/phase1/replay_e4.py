"""Replay the original E4 regression dataset and grader on a committed Phase 1 candidate."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

from mgar_v3.quality_eval.e1_protocol import ROOT, encode
from mgar_v3.quality_eval.e3_metrics import journal
from mgar_v3.quality_eval.e4_cases import cases
from mgar_v3.quality_eval.e4_grade import grade
from mgar_v3.quality_eval.e4_run import execute

sys.path.insert(0, str(ROOT / "scripts"))
from memory_context_e4 import RAW_FILES, inputs  # noqa: E402
from verify import source_fingerprint  # noqa: E402

OLD_DATA_SHA = "e409b7f55fc465d1335b8ad787d5b8db6d322eaa615e189dd551ff16fc8be3a8"
BASE = "fcabf97"
FIXED = ("quality_eval/e4_cases.py", "quality_eval/e4_grade.py", "quality_eval/e4_run.py")


def git(*args: str) -> bytes:
    return subprocess.check_output(["git", *args], cwd=ROOT)


def save(path: Path, value: object) -> str:
    data = encode(value)
    with path.open("xb") as stream:
        stream.write(data)
    return hashlib.sha256(data).hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--candidate", required=True)
    parser.add_argument("--out", required=True, type=Path)
    args = parser.parse_args()
    if git("rev-parse", "HEAD").decode().strip() != args.candidate:
        raise ValueError("Check out the exact candidate before replay")
    if git("status", "--porcelain", "--untracked-files=all", "--", "src", "scripts", "migrations",
           "config", "uv.lock", "experiments/capability-eval-v1/phase1/replay_e4.py"):
        raise ValueError("Candidate is dirty")
    files = [f"src/mgar_v3/{p}" for p in FIXED]
    if git("diff", BASE, "HEAD", "--", *files, "scripts/memory_context_e4.py"):
        raise ValueError("Historical fixtures or grader changed")
    data = inputs()
    if data["dataset_sha256"] != OLD_DATA_SHA:
        raise ValueError("Historical dataset hash mismatch")
    dsn = os.environ["MGAR_E4_ADMIN_DATABASE_URL"]
    source = source_fingerprint(ROOT)
    args.out.mkdir(mode=0o700, parents=True, exist_ok=False)
    receipt = save(args.out / "freeze.json", dict(
        candidate=args.candidate, source_sha256=source, dataset_sha256=OLD_DATA_SHA,
        code_sha256=data["source_sha256"], historical_regression=True, model_calls=0,
    ))
    execute(dsn, cases(), args.out)
    if source_fingerprint(ROOT) != source:
        raise ValueError("Source changed during replay")
    result = grade(cases(), *(journal(args.out / name) for name in RAW_FILES))
    result.update(candidate=args.candidate, freeze_sha256=receipt, dataset_sha256=OLD_DATA_SHA,
                  raw_sha256={n: hashlib.sha256((args.out / n).read_bytes()).hexdigest()
                              for n in RAW_FILES})
    save(args.out / "summary.json", result)
    print(json.dumps(result["case_counts"]))
    return 0 if result["status"] == "PASS" else 2


if __name__ == "__main__":
    raise SystemExit(main())
