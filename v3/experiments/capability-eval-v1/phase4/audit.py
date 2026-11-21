"""Verify archived evidence links, trial denominators and historical provider accounting."""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
from collections import Counter
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
PLAN = HERE.parent
PHASE2 = PLAN / "phase2"
PHASE3 = PLAN / "phase3"
PHASE4 = ROOT / "evidence/CAPABILITY-PHASE4"


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def read(path):
    return json.loads(path.read_bytes())


def lines(path):
    with gzip.open(path, "rt") if path.suffix == ".gz" else path.open() as src:
        return [json.loads(line) for line in src if line.strip()]


def verify(base, manifest):
    for name, expected in manifest.items():
        if sha(base / name) != expected:
            raise ValueError(f"Archived file differs: {base / name}")
    return len(manifest)


def status_count(rows, field):
    return dict(Counter(row[field] for row in rows))


def original_e5():
    archive = read(ROOT / "experiments/memory-context-v1/acceptance/e5-20261120.json")
    raw = ROOT / archive["raw_directory"]
    main, native = 0, 0
    for row in [*archive["cells"], *archive["native_cells"]]:
        desc = row["raw"]
        file = raw / desc["file"]
        assert sha(file) == desc["sha256"]
        raw_rows = lines(file)
        assert len(raw_rows) == row["requests"] and status_count(raw_rows, "status") == {
            "PASS": row["requests"]
        }
        if row.get("cache") == "native_hot":
            native += len(raw_rows)
        else:
            main += len(raw_rows)
    assert main == archive["primary_requests"]
    assert native == archive["native_requests"]
    return {
        "primary_cells": len(archive["cells"]),
        "primary_requests": main,
        "native_cells": len(archive["native_cells"]),
        "native_requests": native,
    }


def original_e2():
    base = PHASE2 / "e2"
    proofs = read(base / "result-hashes.json")
    n = verify(base, proofs["public"])
    local = verify(ROOT / "evidence/CAPABILITY-PHASE2/e2", proofs["local_evidence"])
    rows = lines(base / "live-requests.jsonl.gz")
    completed = [r for r in rows if r["ledger_state"] == "SETTLED"]
    assert len({r["request_id"] for r in rows}) == len(rows) == 2324
    assert len(completed) == 2323
    tokens = [0, 0]
    cost = 0
    for r in completed:
        u = r["provider_usage"]
        assert r["input_tokens"] == u["prompt_tokens"]
        assert r["output_tokens"] == u["completion_tokens"]
        tokens[0] += u["prompt_tokens"]
        tokens[1] += u["completion_tokens"]
        upper = (3 * u["prompt_tokens"] + 12 * u["completion_tokens"] + 9) // 10
        assert upper == r["peak_cost_micros"]
        cost += upper
    trials = lines(base / "live-trials.jsonl.gz")
    effective = [r for r in trials if r["run_id"].startswith("route2-")]
    assert len(trials) == 1441 and len(effective) == 1440
    assert len({r["run_id"] for r in effective}) == 1440
    assert {c["request_id"] for r in effective for c in r["calls"]} == {
        r["request_id"] for r in completed
    }
    assert sum(len(r["calls"]) for r in effective) == len(completed)
    expected = read(PHASE2 / "acceptance.json")["e2_accounting"]
    assert [expected["input_tokens"], expected["output_tokens"]] == tokens
    assert expected["cost_upper_micros"] == cost
    independent = read(base / "independent-formal.json")
    assert independent["observed"] == 360 and independent["passed"] == 313
    return {
        "public_files": n,
        "local_files": local,
        "attempts": len(rows),
        "settled": len(completed),
        "not_sent_adjudicated": 1,
        "tokens": tokens,
        "peak_cost_micros": cost,
        "independent_holdout_pass": 313,
        "independent_holdout_total": 360,
        "status": "FAIL",
    }


def original_e3():
    base = PHASE2 / "e3"
    audit = read(base / "receipt-audit.json")
    rows = lines(base / "provider-receipts.jsonl")
    assert len({r["request_id"] for r in rows}) == len(rows) == audit["unique_settled_completions"]
    assert status_count(rows, "state") == {"settled": len(rows)}
    cost = 0
    tokens = [0, 0]
    for r in rows:
        u = r["usage"]
        assert r["input"] == u["prompt_tokens"] and r["output"] == u["completion_tokens"]
        assert r["cost"] == (3 * r["input"] + 12 * r["output"] + 9) // 10
        cost += r["cost"]
        tokens[0] += r["input"]
        tokens[1] += r["output"]
    assert tokens == [audit["input_tokens"], audit["output_tokens"]]
    assert cost == audit["upper_cost_micros"]
    pointers = lines(base / "raw.jsonl")
    measured = [x for x in pointers if x.get("mode") == "formal"]
    assert len(measured) == 1
    info = measured[0]["raw"]
    assert sha(ROOT / info["path"]) == info["sha256"] == audit["raw_sha256"]
    trials = lines(ROOT / info["path"])
    assert len(trials) == 640 and all(t["final_success"] for t in trials)
    assert len({(t["case_id"], t["arm"], t["session"]) for t in trials}) == 640
    assert sum(t["model_calls"] for t in trials) == len(rows)
    assert sum(t["cost_micros"] for t in trials) == cost
    return {
        "settled": len(rows),
        "tokens": tokens,
        "peak_cost_micros": cost,
        "success": 640,
        "trials": len(trials),
        "status": "INCONCLUSIVE",
    }


def phase3_live():
    base = PHASE3 / "live"
    accepted = read(base / "acceptance.json")
    nfiles = verify(base, accepted["checks"])
    rows = lines(base / "provider-receipts.jsonl")
    assert len({r["operation"] for r in rows}) == len(rows) == accepted["supplier_settled"]
    assert status_count(rows, "state") == {"settled": len(rows)}
    cost = 0
    tokens = [0, 0]
    for r in rows:
        u = r["receipt"]["usage"]
        assert (
            r["settled"]
            == (3 * u["input_tokens"] + 12 * u["output_tokens"] + 9) // 10
            <= r["reserved"]
        )
        cost += r["settled"]
        tokens[0] += u["input_tokens"]
        tokens[1] += u["output_tokens"]
    assert cost == accepted["peak_usd_micros"]
    trials = lines(base / "raw.jsonl")
    assert len(trials) == 212
    summary = {}
    traced = 0
    for study in ("h12", "h17"):
        relevant = [r for r in trials if r["study"] == study]
        summary[study] = status_count(relevant, "status")
        for row in relevant:
            if row["trace_path"]:
                assert sha(ROOT / row["trace_path"]) == row["trace_sha256"]
                traced += 1
    assert summary == accepted["trial_counts"]
    return {
        "files": nfiles,
        "paid_calls": len(rows),
        "tokens": tokens,
        "traces": traced,
        "peak_cost_micros": cost,
        "denominators": summary,
    }


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--out", type=Path, required=True)
    args = p.parse_args()
    overview = read(PHASE2 / "acceptance.json")
    phase2 = verify(PHASE2, overview["checksums"])
    earlier = read(PHASE3 / "acceptance.json")
    phase3 = verify(PHASE3, earlier["artifacts"])
    assert sha(PHASE3 / "evidence-index.json") == earlier["evidence_index_sha256"]
    e4 = read(PLAN / "phase1-write-repair/acceptance.json")
    assert e4["blind_planned"] == e4["blind_executed"] + e4["blind_not_run"] == 448
    e2 = original_e2()
    e3 = original_e3()
    assert e2["settled"] + e3["settled"] == overview["total_receipts"]
    assert e2["peak_cost_micros"] + e3["peak_cost_micros"] == overview["total_cost_upper_micros"]
    e5 = original_e5()
    prior = phase3_live()
    z = {
        "status": "PASS",
        "phase2_digests": phase2,
        "phase3_engineering_digests": phase3,
        "e2": e2,
        "e3": e3,
        "e5_baseline": e5,
        "phase3_live": prior,
        "phase1_e4": {
            "executed": e4["blind_executed"],
            "not_run": e4["blind_not_run"],
            "registered": e4["blind_planned"],
            "status": "FAIL",
        },
        "historical_statuses_preserved": True,
        "new_paid_calls": 0,
    }
    with args.out.open("x") as f:
        json.dump(z, f, indent=2, ensure_ascii=False)
        f.write("\n")
    print("PASS archival hashes, recorded request traces, budgets and denominators")


if __name__ == "__main__":
    main()
