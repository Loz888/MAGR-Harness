"""Paired R1 scoring. Inputs and ground truth enter only after model output is durable."""

import json
from collections import Counter
from statistics import median

import numpy as np

from mgar_v3.memory.calibrated import ARMS
from mgar_v3.quality_eval.e1_grade import metrics
from mgar_v3.quality_eval.e1_protocol import encode, pin

NAMES = (
    "recall_at_5",
    "recall_at_10",
    "precision_at_5",
    "mrr_at_10",
    "ndcg_at_10",
    "no_answer_fpr",
    "answerable_coverage",
    "stale_contamination_at_5",
    "invalid_version_contamination_at_5",
    "acl_leakage_at_10",
)
BOOT = 10000
SEED = 20261120


def index(path):
    rows = [json.loads(line) for line in path.read_bytes().splitlines()]
    result = {r["query_id"]: r for r in rows}
    if len(rows) != len(result):
        raise ValueError("Duplicate query ID")
    return result


def summarize(rows):
    groups = sorted({r["cohort"] for r in rows})
    # One multinomial row is one with-replacement cluster sample, shared by all arms.
    weights = np.random.default_rng(SEED).multinomial(
        len(groups), [1 / len(groups)] * len(groups), size=BOOT
    )
    cluster_index = {g: i for i, g in enumerate(groups)}
    bucket = np.zeros((len(groups), len(ARMS), len(NAMES), 2))
    for row in rows:
        for a, arm in enumerate(ARMS):
            bucket[cluster_index[row["cohort"]], a] += np.asarray(
                [row["arms"][arm][m] for m in NAMES]
            )
    totals = bucket.sum(axis=0)
    draws = (weights @ bucket.reshape(len(groups), -1)).reshape(BOOT, len(ARMS), len(NAMES), 2)
    estimates = np.divide(
        draws[:, :, :, 0],
        draws[:, :, :, 1],
        out=np.full(draws.shape[:-1], np.nan),
        where=draws[:, :, :, 1] > 0,
    )

    def ci(x):
        valid = x[np.isfinite(x)]
        return list(np.quantile(valid, [0.025, 0.975])) if len(valid) else None

    out = {}
    for a, arm in enumerate(ARMS):
        out[arm] = {}
        for m, name in enumerate(NAMES):
            num, den = totals[a, m]
            out[arm][name] = {
                "numerator": num,
                "denominator": den,
                "value": num / den if den else None,
                "ci95": ci(estimates[:, a, m]),
            }
    delta = {}
    for m, name in enumerate(NAMES):
        left, right = out["F"][name]["value"], out["A"][name]["value"]
        delta[name] = {
            "difference": left - right if right is not None and left is not None else None,
            "ci95": ci(estimates[:, 5, m] - estimates[:, 0, m]),
        }
    return {"n": len(rows), "cohorts": len(groups), "arms": out, "paired_F_minus_A": delta}


def write(path, value):
    with path.open("x", encoding="utf8") as out:
        json.dump(value, out, ensure_ascii=False, indent=2, allow_nan=False)
        out.write("\n")


def grade(args, queries, corpus):
    out = args.out
    manifest = json.loads((out / "run.json").read_bytes())
    if (
        manifest["freeze_sha256"] != args.freeze
        or pin(out / "raw.jsonl") != manifest["raw_sha256"]
        or pin(out / "scope-filters.jsonl") != manifest["filter_sha256"]
        or manifest["split"] != args.split
    ):
        raise ValueError("Raw run binding mismatch")
    records = {str(r.memory_id): r for r in corpus}
    expected = {q.query_id for q in queries}
    labels = index(args.labels)
    raw = index(out / "raw.jsonl")
    if labels.keys() != expected or raw.keys() != expected:
        raise ValueError("Queries, labels and raw must cover identical unique IDs")
    rows = []
    for q in queries:
        label = labels[q.query_id]
        rel = {k: v for k, v in label["relevance"].items() if v > 0}
        invalid = set(label["invalid_version_ids"])
        forbidden = set(label["forbidden_ids"])
        if (
            type(label["expected_answerable"]) is not bool
            or bool(rel) != label["expected_answerable"]
            or not all(type(v) is int and 0 <= v <= 3 for v in label["relevance"].values())
            or not (forbidden | invalid | set(label["relevance"])) <= records.keys()
            or any(not records[k].allows(q.scope) or k in invalid or k in forbidden for k in rel)
            or any(records[k].allows(q.scope) for k in forbidden)
        ):
            raise ValueError("Inconsistent annotation")
        r = raw[q.query_id]
        pool = r["candidate_ids"]
        pool_leak = sum(not records[k].allows(q.scope) for k in pool)
        if r["category"] != q.category or q.cohort != r["cohort"] or len(pool) != len(set(pool)):
            raise ValueError("Raw identity mismatch")
        scored = {
            "query_id": q.query_id,
            "cohort": q.cohort,
            "category": q.category,
            "pool_leaks": pool_leak,
            "abstained": r["abstention_reason"],
            "arms": {},
        }
        misses = []
        for arm in ARMS:
            ranked = [pool[i] for i, _ in r["rankings"][arm]]
            if len(ranked) != len(set(ranked)):
                raise ValueError("Repeated ranking ID")
            ids = ranked[:10]
            vals = metrics(ids, rel)
            pairs = {m: ([v, 1] if v is not None else [0, 0]) for m, v in vals.items()}
            pairs.update(
                no_answer_fpr=[int(not rel and bool(ids)), int(not rel)],
                answerable_coverage=[int(bool(rel) and bool(ids)), int(bool(rel))],
                stale_contamination_at_5=[
                    sum(records[k].intended_lifecycle == "stale" for k in ids[:5]),
                    len(ids[:5]),
                ],
                invalid_version_contamination_at_5=[
                    sum(
                        k in invalid or records[k].version(q.scope) in {"historical", "stale"}
                        for k in ids[:5]
                    ),
                    len(ids[:5]),
                ],
                acl_leakage_at_10=[
                    sum((not records[k].allows(q.scope) or k in forbidden) for k in ids),
                    len(ids),
                ],
            )
            scored["arms"][arm] = pairs
            if rel and vals["recall_at_5"] != 1:
                misses.append(arm + ":incomplete_recall_at_5")
            if not rel and ids:
                misses.append(arm + ":false_positive")
            if any(
                pairs[m][0] for m in ("invalid_version_contamination_at_5", "acl_leakage_at_10")
            ):
                misses.append(arm + ":invalid_version_or_acl_exposure")
        scored["failures"] = misses
        rows.append(scored)
    overall = summarize(rows)
    cats = sorted({q.category for q in queries})
    bycat = {cat: summarize([r for r in rows if r["category"] == cat]) for cat in cats}
    stage_times = {
        k: median(r["stage_ms"][k] for r in raw.values())
        for k in next(iter(raw.values()))["stage_ms"]
    }
    checks = {
        "recall_improvement": overall["paired_F_minus_A"]["recall_at_5"]["difference"] >= 0.05,
        "mrr_paired_lower_bound": overall["paired_F_minus_A"]["mrr_at_10"]["ci95"][0] > 0,
        "no_answer_fpr": overall["arms"]["F"]["no_answer_fpr"]["value"] <= 0.10,
        "answerable_coverage": overall["arms"]["F"]["answerable_coverage"]["value"] >= 0.90,
        "acl": not any(
            r["pool_leaks"] or any(v["acl_leakage_at_10"][0] for v in r["arms"].values())
            for r in rows
        ),
    }
    checks = {k: bool(v) for k, v in checks.items()}
    write(
        out / "metrics.json",
        {
            "overall": overall,
            "by_category": bycat,
            "bootstrap": {
                "method": "paired percentile by resampled query cohort",
                "seed": SEED,
                "iterations": BOOT,
            },
            "median_stage_ms_warm_text_cache": stage_times,
            "abstention_reasons": dict(
                Counter(r["abstention_reason"] or "accepted" for r in raw.values())
            ),
        },
    )
    with (out / "cases.jsonl").open("xb") as stream:
        for row in rows:
            stream.write(encode(row))
    with (out / "failures.md").open("x", encoding="utf8") as stream:
        stream.write("# 检索漏召、误答和版本污染\n\n")
        for r in rows:
            if r["failures"] or r["pool_leaks"]:
                stream.write(f"- {r['query_id']}  {', '.join(r['failures'])}\n")
    write(
        out / "acceptance.json",
        {
            "status": "PASS" if all(checks.values()) else "FAIL",
            "checks": checks,
            "split": args.split,
            "candidate_manifest_sha256": args.freeze,
            "metrics_sha256": pin(out / "metrics.json"),
            "cases_sha256": pin(out / "cases.jsonl"),
            "labels_sha256": pin(args.labels),
            "scope": "Frozen R1 retrieval mechanism only. No release promotion.",
        },
    )
    print(json.dumps(checks))
