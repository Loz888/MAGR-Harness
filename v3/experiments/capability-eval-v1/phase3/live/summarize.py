"""Reconcile the frozen schedule, model receipts, and model-free output scores."""

from __future__ import annotations

import json
import random
import sqlite3
import statistics
from collections import Counter, defaultdict
from pathlib import Path

from grade import digest, grade

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[3]
EVIDENCE = ROOT / "evidence/CAPABILITY-PHASE3-LIVE"


def emit(path, data):
    path.write_text(json.dumps(data, ensure_ascii=False, sort_keys=True, indent=2) + "\n")


def read_lines(path):
    return [json.loads(line) for line in path.read_text().splitlines()]


def gate_receipts(path):
    calls = defaultdict(list)
    with sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True) as db:
        db.row_factory = sqlite3.Row
        assert db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        for raw in db.execute("SELECT * FROM calls ORDER BY started,operation"):
            row = dict(raw)
            row["receipt"] = json.loads(row["receipt"]) if row["receipt"] else None
            if row["state"] == "settled":
                use = row["receipt"]["usage"]
                assert use["known"]
                assert 0 < use["input_tokens"] <= row["model_input_upper"]
                assert 0 <= use["output_tokens"] <= row["model_output_limit"]
                assert (
                    row["settled"]
                    == (3 * use["input_tokens"] + 12 * use["output_tokens"] + 9) // 10
                )
                assert row["settled"] <= row["reserved"]
            calls[row["trial"]].append(row)
    return calls


def charges(calls):
    settled = [call for call in calls if call["state"] == "settled"]
    unsettled = [call for call in calls if call["state"] != "settled"]
    return {
        "calls": len(calls),
        "settled": len(settled),
        "unknown": len(unsettled),
        "held_micros": sum(call["reserved"] for call in unsettled),
        "peak_cost_micros": sum(call["settled"] for call in settled),
        "input_tokens": sum(call["receipt"]["usage"]["input_tokens"] for call in settled),
        "output_tokens": sum(call["receipt"]["usage"]["output_tokens"] for call in settled),
    }


def trial_grade(item, arm, ledger):
    study, case, seed = item["study"], item["case"], item["seed"]
    suffix = f"{study}-{case['case_id']}-{seed}-{arm}"
    key = f"bound4-{suffix}"
    path = EVIDENCE / "trials" / (key + ".json")
    if not path.exists() and key not in ledger:
        old_key = f"bound3-{suffix}"
        old = EVIDENCE / "trials" / (old_key + ".json")
        if old.is_file():
            earlier = json.loads(old.read_text())
            if not earlier["error"] and earlier["result"].get("usage_known"):
                key, path = old_key, old
    attempts = [
        prefix + "-" + suffix for prefix in ("bound3", "bound4") if prefix + "-" + suffix in ledger
    ]
    calls = ledger.get(key, [])
    result = None
    trial = None
    declared = []
    phase = "NOT_RUN"
    engine_reconciled = None
    test_changed = False
    if path.is_file():
        trial = json.loads(path.read_text())
        assert (trial["study"], trial["case_id"], trial["seed"], trial["arm"]) == (
            study,
            case["case_id"],
            seed,
            arm,
        )
        result = grade(trial, case, path.with_name(path.stem + "-files"))
        if not calls:
            phase = "BLOCKED"
        elif charges(calls)["unknown"]:
            phase = "BLOCKED"
        else:
            phase = "PASS" if result["success"] else "FAIL"
        if trial["error"] in {
            "MODEL_OUTPUT_SCOPE",
            "PHASE3_OUTPUT_SCOPE",
            "RECOVERY_BLOCKED",
            "PHASE3_BUDGET",
        } or trial["result"].get("reason") in {"PHASE3_BUDGET", "PHASE3_INPUT_BOUND"}:
            phase = "BLOCKED"
        if (
            trial["result"].get("reason") == "PHASE3_BUDGET"
            and len(calls) == 8
            and all(c["state"] == "settled" for c in calls)
        ):
            phase = "FAIL"
        declared = [r["receipt"] for r in trial["captures"]]
        tree = path.with_name(path.stem + "-files")
        test_changed = any(
            (tree / k).is_file() and digest(tree / k) != sha
            for k, sha in trial["source"].items()
            if k.startswith("test_")
        )
        if trial["result"].get("usage_known") and calls:
            engine_reconciled = charges(calls)["peak_cost_micros"] == trial["result"][
                "cost_micros"
            ] and all(call["operation"] in trial["model_ops"] for call in calls)
    elif attempts:
        phase = "BLOCKED"
    truncations = [
        (call["receipt"].get("output_receipt") or {})
        for call in calls
        if call["receipt"]
        and (call["receipt"].get("output_receipt") or {}).get("provider_finish_reason") == "length"
    ]
    later = {
        r.get("continuation", {}).get("parent_operation_id")
        for r in declared
        if r.get("continuation")
    }
    blocked_edit_prefix = {
        r["operation_id"].replace(":model:", ":tool:") + ":"
        for r in declared
        if r.get("provider_finish_reason") == "length"
    }
    unsafe_dispatch = bool(
        trial
        and (
            any(
                str(e["operation_id"]).startswith(prefix)
                for e in trial["edits"]
                for prefix in blocked_edit_prefix
            )
            or any(
                r.get("provider_finish_reason") == "length"
                and any(
                    t.get("execution_status") != "not_executed" for t in r.get("tool_calls", [])
                )
                for r in declared
            )
        )
    )
    return {
        "study": study,
        "case_id": case["case_id"],
        "seed": seed,
        "arm": arm,
        "attempts_in_ledger": attempts,
        "scored_attempt": key if trial else None,
        "stratum": case.get("stratum"),
        "status": phase,
        "grade": result,
        "calls": charges(calls),
        "settled_provider_truncations": len(truncations),
        "bound_later_continuations": sum(r.get("operation_id") in later for r in truncations),
        "successful_after_truncation": bool(truncations and phase == "PASS"),
        "unsafe_truncated_dispatch_detected": unsafe_dispatch,
        "test_changed": test_changed,
        "engine_reconciled": engine_reconciled,
        "reason": (trial["error"] or trial["result"].get("reason") or trial["result"].get("status"))
        if trial
        else None,
        "elapsed_seconds": trial["elapsed_seconds"] if trial else None,
        "consultation_tool_receipts": len(trial["consultations"]) if trial else 0,
        "expert_dispatches": sum(":expert:" in call["operation"] for call in calls),
        "trace_path": str(path.relative_to(ROOT)) if trial else None,
        "trace_sha256": digest(path) if trial else None,
    }


def compare(trials):
    paired = defaultdict(list)
    indexed = {(t["case_id"], t["seed"], t["arm"]): t for t in trials}
    complete = all(t["status"] in {"PASS", "FAIL"} for t in trials)
    if not complete:
        return {"status": "INCOMPLETE", "comparable_clusters": None}
    for a in trials:
        if a["arm"] == "delegate":
            s = indexed[a["case_id"], a["seed"], "single"]
            paired[a["case_id"]].append(int(a["status"] == "PASS") - int(s["status"] == "PASS"))
    clusters = [statistics.mean(values) for values in paired.values()]
    assert len(clusters) == 30 and all(len(v) == 3 for v in paired.values())
    rng = random.Random(173211)
    boot = sorted(statistics.mean(rng.choice(clusters) for _ in clusters) for _ in range(5000))
    d, low, high = statistics.mean(clusters), boot[124], boot[4874]
    single = [t for t in trials if t["arm"] == "single"]
    expert = [t for t in trials if t["arm"] == "delegate"]

    def charge(arm):
        return sum(t["calls"]["peak_cost_micros"] for t in arm)

    def latency(arm):
        return statistics.median(t["elapsed_seconds"] for t in arm)

    cost_frac = charge(expert) / charge(single) - 1 if charge(single) else None
    latency_frac = latency(expert) / latency(single) - 1
    superior = low > 0 and d >= 2 / 30
    return {
        "status": "COMPLETE",
        "clusters": len(clusters),
        "paired_difference": d,
        "ci95": [low, high],
        "superiority": superior,
        "noninferiority": low >= -0.05,
        "relative_cost": cost_frac,
        "relative_median_wall": latency_frac,
        "meets_numeric_default_gates": (
            superior and cost_frac is not None and cost_frac <= 0.25 and latency_frac <= 0.20
        ),
        "default_change_supported": False,
    }


def main():
    usage = gate_receipts(EVIDENCE / "ledger.sqlite")
    frozen = read_lines(HERE / "schedule.jsonl")
    trials = [trial_grade(item, arm, usage) for item in frozen for arm in item["arms"]]
    assert len(trials) == 212
    summaries = {}
    for s in ("h12", "h17"):
        relevant = [t for t in trials if t["study"] == s]
        overall = charges([c for batch in usage.values() for c in batch if c["study"] == s])
        assert overall["calls"] <= (256 if s == "h12" else 1440)
        assert overall["peak_cost_micros"] + overall["held_micros"] <= (
            5_000_000 if s == "h12" else 24_000_000
        )
        arms = {}
        for a in sorted({t["arm"] for t in relevant}):
            chosen = [t for t in relevant if t["arm"] == a]
            reasons = Counter((t["status"], t["reason"]) for t in chosen)
            arms[a] = {
                "statuses": Counter(t["status"] for t in chosen),
                "reasons": [[st, why, n] for (st, why), n in reasons.items()],
                "strategy_match": sum(
                    bool((t["grade"] or {}).get("strategy_matched")) for t in chosen
                ),
            }
            for key in (
                "settled_provider_truncations",
                "bound_later_continuations",
                "successful_after_truncation",
                "consultation_tool_receipts",
                "expert_dispatches",
                "test_changed",
                "unsafe_truncated_dispatch_detected",
            ):
                arms[a][key] = sum(t[key] for t in chosen)
        summary = {
            "scheduled": len(relevant),
            "accounting": overall,
            "arms": arms,
            "independent_operator": "NOT_RUN",
        }
        if s == "h17":
            summary["comparison"] = compare(relevant)
        summaries[s] = summary
    (HERE / "raw.jsonl").write_text(
        "".join(json.dumps(t, ensure_ascii=False) + "\n" for t in trials)
    )
    (HERE / "provider-receipts.jsonl").write_text(
        "".join(json.dumps(c, ensure_ascii=False) + "\n" for group in usage.values() for c in group)
    )
    emit(HERE / "metrics.json", summaries)
    for study, result in summaries.items():
        print(study, result["accounting"], {a: d["statuses"] for a, d in result["arms"].items()})
    print(summaries["h17"]["comparison"])


if __name__ == "__main__":
    main()
