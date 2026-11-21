"""Verify E5-R1 samples and build compact reports from the sealed local evidence."""

from __future__ import annotations

import gzip
import hashlib
import json
from collections import Counter
from statistics import median

from run import ARMS, CONCURRENCY, COUNTS, HERE, ROOT

EVIDENCE = ROOT / "evidence/CAPABILITY-PHASE4"
FORMAL = EVIDENCE / "formal01"
PROFILE = EVIDENCE / "profile01"


def read(file):
    return json.loads(file.read_bytes())


def sha(file):
    return hashlib.sha256(file.read_bytes()).hexdigest()


def jsonl(file):
    with gzip.open(file, "rt") if file.suffix == ".gz" else file.open() as stream:
        return [json.loads(line) for line in stream if line.strip()]


def emit(file, obj):
    (HERE / file).write_text(json.dumps(obj, indent=2, ensure_ascii=False) + "\n")


def detailed_profile():
    output = []
    for file in sorted(PROFILE.glob("plan-*.json")):
        record = read(file)[0]
        tree = record["Plan"]
        nodes = [tree]
        access = []
        selected = None
        while nodes:
            node = nodes.pop()
            if node.get("Subplan Name") == "CTE selected":
                selected = node["Actual Total Time"]
            if "Relation Name" in node:
                access.append(
                    {
                        "relation": node["Relation Name"],
                        "node": node["Node Type"],
                        "index": node.get("Index Name"),
                        "rows": node["Actual Rows"],
                        "loops": node["Actual Loops"],
                        "ms": node["Actual Total Time"],
                        "rows_removed": node.get("Rows Removed by Filter", 0),
                    }
                )
            nodes.extend(node.get("Plans", []))
        output.append(
            {
                "plan": file.name,
                "sha256": sha(file),
                "execution_ms": record["Execution Time"],
                "scope_selection_node_ms": selected,
                "buffer_hits": tree["Shared Hit Blocks"],
                "buffer_reads": tree["Shared Read Blocks"],
                "returned_rows": tree["Actual Rows"],
                "access_nodes": access,
            }
        )
    statements = read(PROFILE / "baseline-statements.json")
    detail_queries = sum(
        row["calls"]
        for row in statements
        if row["query"].startswith("SELECT") and "WHERE memory_id=$1" in row["query"]
    )
    return {
        "query_plans": output,
        "baseline_detail_queries": detail_queries,
        "baseline_measured_and_warm_queries": 256,
        "exact_record_checks": len(read(PROFILE / "tuple-parity.json")),
    }


def audit_cells():
    data = jsonl(FORMAL / "cells.jsonl")
    indices = []
    signed = {}
    count = 0
    for cell in data:
        address = (cell["n"], cell["workers"], cell["cache"], cell["arm"], cell["repeat"])
        if address in signed:
            raise ValueError("Duplicate performance cell")
        signed[address] = cell
        descriptor = cell["evidence"]
        raw = FORMAL / descriptor["file"]
        if sha(raw) != descriptor["sha256"]:
            raise ValueError("Raw performance evidence hash differs")
        records = jsonl(raw)
        assert len(records) == cell["requests"] and [r["ordinal"] for r in records] == list(
            range(cell["requests"])
        )
        counts = dict(Counter(r["status"] for r in records))
        assert counts == cell["status_counts"]
        digest = hashlib.sha256(
            (
                json.dumps(
                    [(r["query_id"], r["result_sha256"]) for r in records],
                    sort_keys=True,
                    ensure_ascii=False,
                )
                + "\n"
            ).encode()
        ).hexdigest()
        assert digest == cell["result_sha256"]
        count += len(records)
        indices.append(
            {
                "cell": address,
                "requests": len(records),
                "status_counts": counts,
                "raw_gzip": str(raw.relative_to(ROOT)),
                "sha256": descriptor["sha256"],
                "result_sha256": digest,
            }
        )
    expected = {
        (n, w, s, a, 0)
        for n in COUNTS
        for w in CONCURRENCY
        for s in ("cold_pg", "hot_pg")
        for a in ARMS
    }
    expected.update(
        (100_000, w, "hot_pg", a, r)
        for w in (5, 50)
        for a in ("baseline", "full")
        for r in (1, 2, 3)
    )
    missing = sorted(expected - set(signed))
    assert not (set(signed) - expected)
    parity_errors = []
    for key, cell in signed.items():
        n, w, cache, arm, repeat = key
        baseline = signed.get((n, w, cache, "baseline", repeat))
        hot = signed.get((n, w, "hot_pg", arm, repeat))
        for reference in (baseline, hot):
            if reference and reference["result_sha256"] != cell["result_sha256"]:
                parity_errors.append(key)
    failures = [
        key for key, row in signed.items() if row["status_counts"] != {"PASS": row["requests"]}
    ]
    query_starts = [
        key
        for key, row in signed.items()
        if (key[2] == "cold_pg") != (row["pg_initial_target_blocks"] == 0)
    ]
    (HERE / "raw.jsonl").write_text(
        "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in indices)
    )
    return signed, {
        "scheduled_cells": len(expected),
        "observed_cells": len(data),
        "requests": count,
        "missing": missing,
        "result_parity_errors": parity_errors,
        "request_errors": failures,
        "cache_state_errors": query_starts,
    }


def priority(signed):
    out = {}
    for w, metric in [(5, "verified_qps"), (50, "p99_ms")]:
        items = []
        for r in (1, 2, 3):
            a = signed.get((100000, w, "hot_pg", "baseline", r))
            d = signed.get((100000, w, "hot_pg", "full", r))
            if not a or not d:
                continue

            av, bv = (
                (cell[metric] if metric in cell else cell["latency"][metric]) for cell in (a, d)
            )
            items.append(
                {
                    "repeat": r,
                    "base": av,
                    "full": bv,
                    "delta_frac": bv / av - 1,
                    "sql_and_hydration_base_ms": a["stages"]["scope"]["p50_ms"],
                    "sql_and_hydration_full_ms": d["stages"]["scope"]["p50_ms"],
                    "base_reads": a["pg_rows_scanned"],
                    "full_reads": d["pg_rows_scanned"],
                }
            )
        ok = len(items) == 3 and all(
            ((x["delta_frac"] > 0) if w == 5 else (x["delta_frac"] < 0)) for x in items
        )
        out[str(w)] = {
            "metric": metric,
            "pairs": items,
            "reproducible": ok,
            "median_change": median(x["delta_frac"] for x in items) if items else None,
        }
    return out


def main():
    signed, coverage = audit_cells()
    critical = priority(signed)
    audit = read(EVIDENCE / "archive-review.json")
    profile = detailed_profile()
    review = {key: read(EVIDENCE / f"{key}-review.json") for key in ("development", "holdout")}
    gate = all(
        (
            not coverage["missing"],
            not coverage["result_parity_errors"],
            not coverage["request_errors"],
            not coverage["cache_state_errors"],
            all(r["reproducible"] for r in critical.values()),
            all(r["status"] == "PASS" for r in review.values()),
            audit["status"] == "PASS",
            profile["exact_record_checks"] == 48,
        )
    )
    output = {
        "status": "PASS" if gate else "FAIL",
        "coverage": coverage,
        "critical": critical,
        "historical_audit": audit,
        "heldout_parity": review,
        "profile": profile,
        "paid_calls": 0,
        "remote_writes": 0,
        "host": read(EVIDENCE / "host.json"),
        "pg_and_datasets": {
            str(f.relative_to(ROOT)): sha(f) for f in sorted(FORMAL.glob("dataset-*.json"))
        },
        "per_cell": [row for row in signed.values()],
    }
    emit("metrics.json", output)
    for k, v in review.items():
        emit(f"e1-{k}.json", v)
    emit("archive-audit.json", audit)
    print(
        json.dumps(
            {"status": output["status"], "coverage": coverage, "critical": critical}, indent=2
        )
    )


if __name__ == "__main__":
    main()
