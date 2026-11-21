"""Read-only recheck of the Phase 0 evidence freeze. Run with Python 3.12."""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
import tarfile
from collections import Counter
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
V3 = HERE.parents[2]
HOME = V3.parent.parent
ROOTS = {
    "benchmark": V3,
    "H12": HOME / "magr-harness-h12-repair/v3",
    "H12-review": HOME / "h12-r08-independent-timing-review-20261118",
    "H17": HOME / "magr-harness-h17/v3",
    "H18": HOME / "magr-harness-h18/v3",
    "E1-review": HOME / "magr-e1-independent-holdout-20261120",
}
SUBTREES = (
    "src",
    "migrations",
    "config",
    "scripts",
    "tests",
    "fixtures/commerce",
    "web",
    "eval/final-v1",
    "eval/final-v3",
    "deploy",
)
E1_PUBLIC = {
    "commitment.json",
    "freeze.json",
    "run/run.json",
    "grade/summary.json",
    "independent-verification.json",
}


def digest(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def check(ok: bool, reason: str) -> None:
    if not ok:
        raise ValueError(reason)


def resolve(root: str, name: str) -> Path:
    if root == "E1-review":
        check(name in E1_PUBLIC, "E1 private inputs are outside the freeze")
    base = ROOTS[root].resolve()
    path = (base / name).resolve()
    check(path.is_relative_to(base), "path escapes the evidence root")
    return path


def source_digest(repo: Path, commit: str) -> str:
    """Apply scripts.verify.source_fingerprint ordering to immutable Git bytes."""
    revision = subprocess.check_output(
        ["git", "-C", str(repo), "rev-parse", "--verify", f"{commit}^{{commit}}"], text=True
    ).strip()
    check(revision == commit, "expected a full immutable commit")
    with subprocess.Popen(
        ["git", "-C", str(repo.parent), "archive", commit, "v3"], stdout=subprocess.PIPE
    ) as git:
        files = {}
        with tarfile.open(fileobj=git.stdout, mode="r|") as archive:
            for member in archive:
                name = member.name.removeprefix("v3/")
                if not (
                    name.startswith(tuple(t + "/" for t in SUBTREES))
                    or name in {"pyproject.toml", "uv.lock"}
                ):
                    continue
                if "__pycache__" in Path(name).parts or member.isdir():
                    continue
                check(member.isfile(), f"non-regular source in archive: {name}")
                stream = archive.extractfile(member)
                if stream is None:
                    raise ValueError(f"missing source: {name}")
                files[name] = stream.read()
        check(git.wait() == 0, "git archive failed")
    h = hashlib.sha256()
    for tree in SUBTREES:
        names = (n for n in files if n.startswith(tree + "/"))
        for name in sorted(names, key=lambda n: Path(n).parts):
            h.update(name.encode() + b"\0" + files[name] + b"\0")
    for name in ("pyproject.toml", "uv.lock"):
        if name in files:
            h.update(name.encode() + b"\0" + files[name])
    return h.hexdigest()


def check_case_scores(h12: dict[str, Any]) -> int:
    audit = json.loads(resolve("H12", h12["audit"]).read_bytes())
    for split, expected in h12["by_split"].items():
        rows = sorted((r for r in audit["rows"] if r["split"] == split), key=lambda r: r["index"])
        check([r["index"] for r in rows] == list(range(1, 13)), "invalid H12 indices")
        check([r["score"]["sha256"] for r in rows] == expected, "H12 score order mismatch")
        for row in rows:
            score = row["score"]
            path = resolve("H12", score["path"])
            check(digest(path) == score["sha256"], f"H12 {split} score mismatch")
        files = (ROOTS["H12"] / h12["archive"] / split).glob("*/score.json")
        check(sorted(digest(f) for f in files) == sorted(expected), "unexpected H12 score set")
    dev = json.loads(resolve("H12-review", h12["original_dev_index"]).read_bytes())
    check(len(dev) == h12["original_dev_files"], "H12 development inventory size differs")
    for name, expected_hash in dev.items():
        check(digest(resolve("H12", name)) == expected_hash, "changed development archive")
    return len(dev)


def check_source_maps(manifest: dict[str, Any]) -> int:
    count = 0
    for group in manifest["historical_file_maps"]:
        document = json.loads(resolve("benchmark", group["receipt"]).read_bytes())
        for key in group["field"]:
            document = document[key]
        for path, expected in document.items():
            data = subprocess.check_output(
                ["git", "-C", str(V3.parent), "show", f"{group['commit']}:v3/{path}"]
            )
            check(hashlib.sha256(data).hexdigest() == expected, f"historical blob mismatch: {path}")
            count += 1
    evaluator = manifest["timing_evaluator"]
    data = subprocess.check_output(
        [
            "git",
            "-C",
            str(ROOTS["H12"].parent),
            "show",
            evaluator["commit"] + ":" + evaluator["path"],
        ]
    )
    check(hashlib.sha256(data).hexdigest() == evaluator["sha256"], "H12 timing evaluator mismatch")
    return count


def verify(expected_manifest_sha256: str | None = None) -> None:
    if expected_manifest_sha256:
        check(digest(HERE / "manifest.json") == expected_manifest_sha256, "changed manifest")
    manifest = json.loads((HERE / "manifest.json").read_bytes())
    required = {
        "cases.jsonl",
        "raw.jsonl",
        "metrics.json",
        "verify.py",
        "acceptance.json",
        "report.md",
        "failures.md",
        "independent_review.md",
    }
    check(set(manifest["deliverables_sha256"]) == required, "incomplete deliverable inventory")
    for name, expected in manifest["deliverables_sha256"].items():
        check(digest(HERE / name) == expected, f"changed deliverable: {name}")
    check(digest(HERE.parent / "PLAN_V1.0.md") == manifest["plan_sha256"], "changed taskbook")
    for source in manifest["sources"]:
        actual = source_digest(ROOTS[source["root"]], source["commit"])
        check(actual == source["source_sha256"], f"changed source: {source['id']}")
    for archive in manifest["local_evidence_archives"]:
        path = resolve(archive["root"], archive["path"])
        check(digest(path) == archive["sha256"], f"archive hash mismatch: {archive['root']}")
    counts: Counter[str] = Counter()
    for line in (HERE / "raw.jsonl").read_text().splitlines():
        row = json.loads(line)
        path = resolve(row["root"], row["path"])
        check(digest(path) == row["sha256"], f"changed evidence: {row['root']}/{row['path']}")
        counts[row["experiment"]] += 1
    check(dict(counts) == manifest["evidence_counts"], "evidence count mismatch")
    development_files = check_case_scores(manifest["h12_score_commitments"])
    source_files = check_source_maps(manifest)
    cases = [json.loads(line)["id"] for line in (HERE / "cases.jsonl").read_text().splitlines()]
    check(len(set(cases)) == len(cases) == 8 and set(counts) == set(cases), "scope mismatch")
    print(
        f"PASS: {len(counts)} experiments, {sum(counts.values())} file bindings, "
        f"{len(manifest['sources'])} source fingerprints, "
        f"{source_files} historical source bindings, "
        f"24 H12 score commitments, {development_files} historical H12 dev files"
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--manifest-sha256", help="pin the manifest to a separately recorded digest"
    )
    args = parser.parse_args()
    try:
        verify(args.manifest_sha256)
    except (KeyError, OSError, ValueError, tarfile.TarError, subprocess.SubprocessError) as exc:
        sys.exit(f"FAIL: {exc}")
