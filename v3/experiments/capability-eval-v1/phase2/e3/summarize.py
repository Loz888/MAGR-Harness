"""Summarize a single execution mode of E3. Usage: summarize.py RAW.jsonl OUTPUT.json [--cases CASES.jsonl ...].

Pass the full planned case manifests to identify omitted chains and partial trials.
A case may specify seed or seeds, and its releases array fixes the intended session count.
"""
import argparse
import json
import math
import random
from collections import defaultdict
from pathlib import Path

ARMS = ("off", "bm25", "hybrid", "hybrid_reranked")
REPLICATES = 2000
BOOTSTRAP_SEED = 691369
ERROR_MIN = 20
COUNTS = ("model_calls", "input_tokens", "output_tokens", "cost_micros")
AMOUNT_KEYS = ("sessions", "successes", "first_pass", "model_calls", "input_tokens", "cost_micros")


def load(path):
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def ratio(n, d):
    return n / d if n is not None and d else None


def quantile(values, q):
    if not values:
        return None
    v = sorted(values)
    ix = (len(v) - 1) * q
    lo, hi = math.floor(ix), math.ceil(ix)
    return v[lo] + (v[hi] - v[lo]) * (ix - lo)


def total(rows, key):
    values = [row.get(key) for row in rows]
    if not values or any(v is None for v in values):
        return None
    return sum(values)


def rate(rows, numerator, denominator):
    n = total(rows, numerator)
    d = len(rows) if denominator == "sessions" else total(rows, denominator)
    return {"numerator": n, "denominator": d, "rate": ratio(n, d)}


def arm_metrics(rows):
    success = total(rows, "final_success")
    duration = [row["duration_ms"] for row in rows if row.get("duration_ms") is not None]
    accounting = {}
    for key in COUNTS:
        known = [r for r in rows if r.get(key) is not None]
        accounting[key] = {
            "known_rows": len(known), "known_total": sum(r[key] for r in known),
            "all_rows_total": total(rows, key), "successful_sessions": success,
            "per_successful_session": ratio(total(rows, key), success),
        }
    errors = rate(rows, "repeated_errors", "known_error_opportunities")
    if (errors["denominator"] or 0) < ERROR_MIN:
        errors.update(rate=None, status="INCONCLUSIVE_LOW_OPPORTUNITIES")
    else:
        errors["status"] = "ESTIMATED"
    return {
        "sessions": len(rows), "chains": len({r["case_id"] for r in rows}),
        "first_pass_success": rate(rows, "first_pass_success", "sessions"),
        "final_success": rate(rows, "final_success", "sessions"),
        "retrieval_hit": rate(rows, "retrieval_hit", "sessions"),
        "adoption_given_hit": rate(rows, "memory_adopted", "retrieval_hit"),
        "repeated_error_rate": errors, "repeated_tool_calls": rate(rows, "repeat_tool_calls", "tool_calls"),
        "historical_memory_observed": total(rows, "historical_memory"),
        "stale_attempts": total(rows, "stale_attempts"), "stale_accepted": total(rows, "stale_accepted"),
        "accounting": accounting,
        "duration_ms": {"known_rows": len(duration), "p50": quantile(duration, .5), "p95": quantile(duration, .95)},
    }


def amounts(rows):
    return [len(rows), total(rows, "final_success"), total(rows, "first_pass_success"),
            total(rows, "model_calls"), total(rows, "input_tokens"), total(rows, "cost_micros")]


def project(v):
    sessions, successes, first, calls, tokens, cost = v
    return [ratio(successes, sessions), ratio(first, sessions), ratio(calls, successes),
            ratio(tokens, successes), ratio(cost, successes)]


def combine(vectors):
    return [None if any(x is None for x in xs) else sum(xs) for xs in zip(*vectors)]


def deltas(clusters):
    base = project(combine([pair[0] for pair in clusters]))
    arm = project(combine([pair[1] for pair in clusters]))
    return [b-a if b is not None and a is not None else None for a,b in zip(base,arm)]


def paired(rows, arm, planned):
    # Repeated seeds for one case share a single bootstrap draw.
    indexed = defaultdict(lambda: defaultdict(dict))
    for r in rows:
        indexed[r["case_id"]][r["arm"]][r["seed"],r["session"]] = r
    valid = []
    omitted = []
    for case in sorted(set(indexed) | set(planned)):
        arms = indexed[case]
        expected = planned.get(case)
        if expected is None:
            expected = set().union(*(set(by_key) for by_key in arms.values()))
        if not expected or set(arms["off"]) != expected or set(arms[arm]) != expected:
            omitted.append(case)
        else:
            valid.append((amounts(arms["off"].values()), amounts(arms[arm].values())))
    keys = ("final_success_rate", "first_pass_success_rate", "calls_per_success",
            "input_tokens_per_success", "cost_micros_per_success")
    est = deltas(valid) if valid else [None]*len(keys)
    draws = [[] for _ in keys]
    if len(valid) >= 2:
        rng = random.Random(BOOTSTRAP_SEED)
        for _ in range(REPLICATES):
            d = deltas(rng.choices(valid,k=len(valid)))
            for values, value in zip(draws,d):
                if value is not None:
                    values.append(value)
    results = {}
    for k, point, values in zip(keys, est, draws):
        # Avoid conditioning a ratio CI on potentially many resamples with no successes.
        ci = [quantile(values,.025),quantile(values,.975)] if len(values)==REPLICATES else None
        results[k] = {"estimate":point, "ci95":ci, "defined_bootstrap_draws":len(values)}
    success_ci = results["final_success_rate"]["ci95"]
    cost_ci = results["cost_micros_per_success"]["ci95"]
    return {
        "matched_case_clusters": len(valid), "excluded_case_ids":omitted,
        "paired_on_minus_off":results,
        "success_improvement": (
            "ESTABLISHED" if success_ci and success_ci[0]>0 and not omitted
            else "INCONCLUSIVE"
        ),
        "significant_cost_increase": (cost_ci[0]>0 if cost_ci and not omitted else None),
    }


def planned_cases(manifests, rows):
    splits = {r["case_id"]: r["split"] for r in rows}
    planned = defaultdict(dict)
    for path in manifests:
        for r in load(path):
            split = r.get("split", splits.get(r["case_id"]))
            if split is None:
                raise ValueError(f"case without observed rows requires split field: {r['case_id']}")
            seeds = r["seeds"] if "seeds" in r else [r["seed"]]
            keys = {(s,n) for s in seeds for n in range(1,len(r["releases"])+1)}
            if r["case_id"] in planned[split]:
                raise ValueError(f"duplicate case in planned manifests: {r['case_id']}")
            planned[split][r["case_id"]] = keys
    return planned


def summarize(rows, manifests):
    observed = set()
    split_for = {}
    for r in rows:
        case = r["case_id"]
        if r["split"] != split_for.setdefault(case,r["split"]):
            raise ValueError(f"case in multiple splits: {case}")
        key = (case,r["seed"],r["arm"],r["session"])
        if key in observed or r["arm"] not in ARMS:
            raise ValueError(f"duplicate or unrecognized trial: {key}")
        observed.add(key)
        if r["memory_adopted"] and not r["retrieval_hit"]:
            raise ValueError(f"adoption without a retrieval hit: {key}")
        if r["repeated_errors"] > r["known_error_opportunities"]:
            raise ValueError(f"repeated errors exceed opportunities: {key}")
    plans = planned_cases(manifests,rows)
    if manifests:
        schedule = {(c,s,a,n) for cases in plans.values() for c,keys in cases.items()
                    for s,n in keys for a in ARMS}
        if not observed <= schedule:
            raise ValueError("observed trial outside planned manifests")
    else:
        schedule = None
    results = {}
    for split in sorted(set(split_for.values()) | set(plans) | {"all"}):
        subset = rows if split=="all" else [r for r in rows if r["split"]==split]
        plan = {c:v for p in plans.values() for c,v in p.items()} if split=="all" else plans.get(split,{})
        results[split] = {
            "arms":{a:arm_metrics([r for r in subset if r["arm"]==a]) for a in ARMS},
            "comparisons":{a:paired(subset,a,plan) for a in ARMS if a!="off"},
        }
    return {
        "bootstrap":{"unit":"case_id (all sessions, seeds and both arms retained)",
                     "replicates":REPLICATES,"seed":BOOTSTRAP_SEED,"ci":"percentile 95%"},
        "task_unit":"session; ratio costs include failed and successful observed sessions",
        "trial_rows":len(rows),"planned_trials":len(schedule) if schedule is not None else None,
        "missing_trials":sorted(schedule-observed) if schedule is not None else None,
        "limitations":["Case-weighting follows number of observed sessions per case.",
                       "Paired effects apply to matched cases only. Missing trials can bias the estimate.",
                       "Completeness requires planned case manifests. Latency uses recorded durations only.",
                       "Calibrated known failure opportunities <20 yield an inconclusive rate.",
                       "Deterministic agent decisions give engineering evidence only."],
        "results":results,
    }


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument("raw",type=Path)
    p.add_argument("output",type=Path)
    p.add_argument("--cases",type=Path,nargs="+",default=[])
    args=p.parse_args()
    result=summarize(load(args.raw),args.cases)
    with args.output.open("x") as out:
        json.dump(result,out,indent=2,ensure_ascii=False,allow_nan=False)
        out.write("\n")


if __name__=="__main__":
    main()
