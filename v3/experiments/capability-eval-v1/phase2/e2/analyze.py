"""Compute split-specific mechanical metrics and strictly paired live outcomes."""

from __future__ import annotations

import argparse
import json
import math
import random
import sqlite3
from collections import Counter, defaultdict
from pathlib import Path

from matrix import ARMS, HERE, LOCAL, SEED, encoded, lines, schedule, verify

from mgar_v3.quality_eval.e2_grader import offline_summary


def quantile(data, p):
    return sorted(data)[max(0, math.ceil(len(data) * p) - 1)] if data else None


def paired(rows, token_key, success=False):
    groups = defaultdict(dict)
    for row in rows:
        if row["condition"] in ("full_history", "magr_compaction"):
            groups[row["case_id"], row["scale"], row["rep"]][row["condition"]] = row
    clusters = defaultdict(lambda: [0, 0, 0, 0, 0])
    for (c, _, _), arms in groups.items():
        if set(arms) != {"full_history", "magr_compaction"}:
            continue
        a, d = arms["full_history"], arms["magr_compaction"]
        if a.get(token_key) is None or d.get(token_key) is None:
            continue
        if success and (a.get("success") is None or d.get("success") is None):
            continue
        delta = [
            a[token_key],
            d[token_key],
            1,
            int(bool(a.get("success"))),
            int(bool(d.get("success"))),
        ]
        clusters[c] = [p + q for p, q in zip(clusters[c], delta, strict=True)]
    values = list(clusters.values())
    if not values:
        return {
            "clusters": 0,
            "pairs": 0,
            "reduction": None,
            "reduction_ci": None,
            "success_difference": None,
            "success_difference_ci": None,
        }

    def calc(sample):
        a, d, n, pa, pd = map(sum, zip(*sample, strict=True))
        return 1 - d / a if a else None, (pd - pa) / n

    rng = random.Random(SEED)
    draws = [calc(rng.choices(values, k=len(values))) for _ in range(5000)]
    ratios = [v for v, _ in draws if v is not None]
    deltas = [v for _, v in draws]
    ratio, delta = calc(values)
    return dict(
        clusters=len(values),
        pairs=sum(v[2] for v in values),
        reduction=ratio,
        reduction_ci=[quantile(ratios, 0.025), quantile(ratios, 0.975)],
        success_difference=delta if success else None,
        success_difference_ci=[quantile(deltas, 0.025), quantile(deltas, 0.975)]
        if success
        else None,
    )


def offline_metrics(raw):
    by_split = {}
    for split in ("development", "holdout"):
        rows = [r for r in raw if r["split"] == split]
        by_split[split] = {
            "rows": len(rows),
            "arms": offline_summary(rows)["arms"],
            "first_input_reference": paired(rows, "input_tokens_reference"),
            "history_reference": paired(rows, "history_tokens_reference"),
        }
    return by_split


def extract():
    with sqlite3.connect(f"file:{LOCAL}/formal/ledger.sqlite?mode=ro", uri=True) as db:
        rows = [json.loads(r[0]) for r in db.execute("select row from trials order by run_id")]
        requests = [
            dict(
                **json.loads(entry), ledger_state=state, held_micros=bound if charge is None else 0
            )
            for state, bound, charge, entry in db.execute(
                "select state,bound,charge,entry from calls order by rowid"
            )
        ]
        stop = db.execute("select value from meta where name='halt'").fetchone()
    return rows, requests, stop[0] if stop else None


def write_jsonl(path, rows):
    path.write_bytes(b"".join(encoded(r) + b"\n" for r in rows))


def live_metrics(rows, usage, grades, indexes):
    by_id = {r["run_id"]: r for r in rows}
    expected = {t["run_id"] for i in indexes for t in schedule(i["case_id"], i["scale"])}
    if not (set(by_id) <= expected and set(grades) <= set(by_id)) or len(rows) != len(by_id):
        raise ValueError("Unmatched formal trace or score")
    by_split = {}
    for split in ("development", "holdout"):
        split_rows = []
        for r in rows:
            if r["split"] != split:
                continue
            calls = r["calls"]
            known = bool(calls) and all(c.get("provider_usage") for c in calls)
            split_rows.append(
                {
                    **r,
                    "cumulative_input": sum(c["input_tokens"] for c in calls) if known else None,
                    "success": grades.get(r["run_id"], {}).get("success"),
                }
            )
        arms = {}
        for arm in ARMS:
            trials = [r for r in split_rows if r["condition"] == arm]
            calls = [c for r in trials for c in r["calls"] if c.get("provider_usage")]
            reads = [e for r in trials for e in r["reads"]]
            graded = sum(r["success"] is not None for r in trials)
            n_ok = sum(r["success"] is True for r in trials)
            unknown = any(r["cumulative_input"] is None for r in trials)
            input_sum = sum(c["input_tokens"] for c in calls)
            output_sum = sum(c["output_tokens"] for c in calls)
            peak = sum(c["peak_cost_micros"] for c in calls)
            tariff = [c["tariff_cost_nano_usd"] for c in calls]
            rateable = all(v is not None for v in tariff)
            denom = n_ok if graded == len(trials) and not unknown else 0
            arms[arm] = dict(
                trials=len(trials),
                graded=graded,
                success=n_ok,
                confirmed_requests=len(calls),
                confirmed_input=input_sum,
                confirmed_output=output_sum,
                confirmed_peak_cost_micros=peak,
                tariff_cost_nano_usd=sum(tariff) if rateable and not unknown and trials else None,
                ledger_invoice_usd=None,
                first_input_tokens=[c["input_tokens"] for c in calls if c["ordinal"] == 1],
                cumulative_input_per_trial=[r["cumulative_input"] for r in trials],
                total_io_tokens_per_success=(input_sum + output_sum) / denom if denom else None,
                input_tokens_per_success=input_sum / denom if denom else None,
                peak_cost_micros_per_success=peak / denom if denom else None,
                tariff_cost_nano_usd_per_success=sum(tariff) / denom
                if rateable and denom
                else None,
                purposes=dict(Counter(c["purpose"] for c in calls)),
                grade_model_calls=0,
                reads=len(reads),
                repeated_reads=sum(r["repeated"] for r in reads),
                read_bytes=sum(r["bytes"] for r in reads),
                read_reference_tokens=sum(r["body_reference_tokens"] for r in reads),
                reread_bytes=sum(r["bytes"] for r in reads if r["repeated"]),
                reread_reference_tokens=sum(
                    r["body_reference_tokens"] for r in reads if r["repeated"]
                ),
                compaction_ms={
                    k: quantile([r["context_ms"] for r in trials], p)
                    for k, p in (("p50", 0.5), ("p95", 0.95))
                },
            )
        by_split[split] = dict(
            arms=arms, paired=paired(split_rows, "cumulative_input", success=True)
        )
    complete = (
        set(by_id) == expected == set(grades)
        and all(r["status"] in ("ANSWERED", "FAIL") for r in rows)
        and all(c["ledger_state"] == "SETTLED" for c in usage)
        and sum(len(r["calls"]) for r in rows) == len(usage)
    )
    return dict(
        splits=by_split,
        expected=len(expected),
        terminal=len(by_id),
        graded=len(grades),
        complete=complete,
        actual_requests=len(usage),
        held_micros=sum(c["held_micros"] for c in usage),
        settled_peak_micros=sum(c.get("peak_cost_micros", 0) for c in usage),
    )


def report(offline, live, stop):
    offline_ok = all(
        v["rows"] in (360, 1080)
        and all(d["permission_mutations"] == 0 for d in v["arms"].values())
        and all(x["kept"] == x["total"] for x in v["arms"]["magr_compaction"]["integrity"].values())
        for v in offline.values()
    )
    status = "BLOCKED" if stop or live["actual_requests"] else "NOT_RUN"
    if live["complete"]:
        status = (
            "PASS"
            if offline_ok
            and all(
                v["paired"]["reduction_ci"][0] > 0
                and v["paired"]["success_difference_ci"][0] >= -0.05
                for v in live["splits"].values()
            )
            else "FAIL"
        )
    return dict(
        offline="PASS" if offline_ok else "FAIL",
        formal=status,
        halt_reason=stop,
        expected_trials=1440,
        terminal_trials=live["terminal"],
        independently_graded=live["graded"],
    )


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--grade", action="append", type=Path, default=[])
    p.add_argument("--export", action="store_true")
    args = p.parse_args()
    indexes = verify()
    raw = lines(HERE / "raw.jsonl")
    if (LOCAL / "formal/ledger.sqlite").exists():
        rows, calls, stop = extract()
    else:
        rows, calls, stop = [], [], None
    if args.export:
        for split in ("development", "holdout"):
            write_jsonl(
                LOCAL / f"{split}-predictions.jsonl", [r for r in rows if r["split"] == split]
            )
        write_jsonl(LOCAL / "provider-requests.jsonl", calls)
    grade_rows = [r for path in args.grade for r in lines(path)]
    grades = {r["run_id"]: r for r in grade_rows}
    if len(grades) != len(grade_rows):
        raise ValueError("duplicate grades")
    offline = offline_metrics(raw)
    live = live_metrics(rows, calls, grades, indexes)
    (HERE / "metrics.json").write_text(
        json.dumps(dict(offline=offline, live=live), indent=2) + "\n"
    )
    (HERE / "acceptance.json").write_text(json.dumps(report(offline, live, stop), indent=2) + "\n")


if __name__ == "__main__":
    main()
