"""Score the repaired transport candidate and report both candidates' joint budget."""

import argparse
import json
from pathlib import Path

from analyze import extract, live_metrics, offline_metrics, report, write_jsonl
from matrix import HERE, LOCAL, lines, verify


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--export", action="store_true")
    p.add_argument("--grade", action="append", type=Path, default=[])
    args = p.parse_args()
    rows, requests, stop = extract()
    prefix = "route2-"
    corrected = [r for r in rows if r["run_id"].startswith(prefix)]
    calls = [r for r in requests if r["run_id"].startswith(prefix)]
    if args.export:
        write_jsonl(LOCAL / "formal/requests.jsonl", requests)
        write_jsonl(LOCAL / "formal/trials.jsonl", rows)
        for split in ("development", "holdout"):
            write_jsonl(
                LOCAL / f"formal/{split}-predictions.jsonl",
                [r for r in corrected if r["split"] == split],
            )
    grades = {}
    for path in args.grade:
        for row in lines(path):
            key = row["run_id"].removeprefix(prefix)
            if not row["run_id"].startswith(prefix) or key in grades:
                raise ValueError("Mismatched external grade ID")
            grades[key] = row
    normalized = [{**r, "run_id": r["run_id"].removeprefix(prefix)} for r in corrected]
    live = live_metrics(normalized, calls, grades, verify())
    offline = offline_metrics(lines(HERE / "raw.jsonl"))
    budget = dict(
        attempts=len(requests),
        cap=5760,
        unknown_held_micros=sum(r["held_micros"] for r in requests),
        settled_peak_micros=sum(r.get("peak_cost_micros", 0) for r in requests),
        reconciled_not_sent=1,
        cap_micros=369000000,
    )
    (HERE / "metrics.json").write_text(
        json.dumps(dict(offline=offline, live=live, budget=budget), indent=2) + "\n"
    )
    acceptance = report(offline, live, stop)
    (HERE / "acceptance.json").write_text(json.dumps(acceptance, indent=2) + "\n")


if __name__ == "__main__":
    main()
