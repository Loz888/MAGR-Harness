"""Execute the preserved Phase 2 independent holdout as a regression, without exporting it."""

from __future__ import annotations

import argparse
import importlib.util
import json
import tempfile
from pathlib import Path
from types import SimpleNamespace

import psycopg
from candidate import INDEXES
from run import HERE, ROOT, arm_scope

from mgar_v3.quality_eval.e1_protocol import jsonl, pin

E1 = HERE.parent / "phase2/e1"


def module(name, source):
    spec = importlib.util.spec_from_file_location(name, source)
    entry = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(entry)
    return entry


def ranking_signature(out):
    # Exclude timing/tool duration only. Rank IDs, order, scores and ACL inputs all remain.
    rows = [json.loads(line) for line in (out / "raw.jsonl").read_text().splitlines()]
    for row in rows:
        del row["stage_ms"], row["elapsed_ms"]
    return rows


def check(args):
    runner = module("phase2_rank_for_e5", E1 / "run.py")
    assessor = module("phase2_grade_for_e5", E1 / "grade.py")
    original = json.loads((E1 / "e1-r1-manifest.json").read_bytes())
    old_commit = original["bindings"]
    ref = json.loads((HERE / "manifest.json").read_bytes())
    assert all(pin(ROOT / name) == expected for name, expected in ref["files"].items())
    for name, digest in old_commit["corpus"].items():
        assert pin(E1 / name) == digest
    assert old_commit["models"]["embedding_receipt"] == pin(
        runner.MODEL / "mgar-model-receipt.json"
    )
    assert old_commit["models"]["weights"] == pin(runner.WEIGHTS)
    spec = old_commit["split"][args.split]
    if args.split == "holdout":
        assert not Path(args.custody).resolve().is_relative_to(ROOT.parent)
        storage = Path(args.custody)
        queries = storage / "queries.jsonl"
        labels = storage / "labels.jsonl"
        prior = storage / "e1-r1-run"
    else:
        storage = Path(tempfile.mkdtemp(prefix="e5-public-review-"))
        queries = E1 / "development.queries.jsonl"
        labels = E1 / "development.labels.jsonl"
        prior = E1 / "development"
    assert pin(queries) == spec["queries"] and pin(labels) == spec["labels"]
    old_run = json.loads((prior / "run.json").read_bytes())
    assert pin(prior / "raw.jsonl") == old_run["raw_sha256"]
    assert pin(prior / "scope-filters.jsonl") == old_run["filter_sha256"]
    raw_dir = Path(tempfile.mkdtemp(prefix="e5-r1-regression-", dir=storage)) / "observed"
    settings = SimpleNamespace(
        out=raw_dir,
        split=args.split,
        freeze=pin(HERE / "manifest.json"),
        labels=labels,
        queries=queries,
    )
    qs = jsonl(queries.read_bytes(), runner.Query)
    assert len(qs) == spec["count"] == len({q.query_id for q in qs})
    # Prediction code has raw input queries and public/synthetic corpus only. Label use
    # starts after the new raw digest is finalized and checked by the grading entry point.
    original_seed = runner.seed_corpus

    def seeded_index(dsn, corpus, artifacts):
        original_seed(dsn, corpus, artifacts)
        with psycopg.connect(dsn, autocommit=True) as connection:
            connection.execute(INDEXES, prepare=False)
            connection.execute("ANALYZE")

    runner.seed_corpus = seeded_index
    with arm_scope("full"):
        runner.run(settings, qs)
    assessor.grade(settings, qs, runner.read_corpus())
    same_ranking = ranking_signature(prior) == ranking_signature(raw_dir)
    same_permission = pin(raw_dir / "scope-filters.jsonl") == old_run["filter_sha256"]
    before = json.loads((prior / "metrics.json").read_bytes())
    after = json.loads((raw_dir / "metrics.json").read_bytes())
    grade_equal = all(before[key] == after[key] for key in ("overall", "by_category"))
    checks = json.loads((raw_dir / "acceptance.json").read_bytes())
    summary = {
        "status": "PASS" if same_ranking and same_permission and grade_equal else "FAIL",
        "split": args.split,
        "queries": len(qs),
        "arms": 6,
        "rankings_identical": same_ranking,
        "permission_filter_identical": same_permission,
        "grade_metrics_identical": grade_equal,
        "reference_raw_sha256": old_run["raw_sha256"],
        "new_raw_sha256": pin(raw_dir / "raw.jsonl"),
        "grade_sha256": pin(raw_dir / "metrics.json"),
        "cases_sha256": pin(raw_dir / "cases.jsonl"),
        "query_commitment": pin(queries),
        "label_commitment": pin(labels),
        "original_quality_gate_preserved": checks["status"],
        "full_candidate_manifest_sha256": settings.freeze,
        "original_checks": checks["checks"],
        "arms_metrics": after["overall"]["arms"],
        "paid_calls": 0,
        "protocol": "independent-origin frozen labels, separate local process, same OS operator",
    }
    with args.out.open("x") as target:
        json.dump(summary, target, ensure_ascii=False, indent=2)
        target.write("\n")
    if summary["status"] != "PASS":
        raise ValueError("E1 parity did not pass")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("split", choices=("development", "holdout"))
    parser.add_argument("--custody", type=Path)
    parser.add_argument("--out", type=Path, required=True)
    check(parser.parse_args())


if __name__ == "__main__":
    main()
