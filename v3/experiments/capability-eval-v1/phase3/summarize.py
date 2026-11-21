"""Reconcile the Phase 3 fixture matrices against their frozen schedules."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import random
import statistics
import xml.etree.ElementTree as ET
from pathlib import Path

BASE = Path(__file__).resolve().parent


def load_lines(path):
    return [json.loads(line) for line in path.read_text().splitlines()]


def write(path, obj):
    path.write_text(json.dumps(obj, indent=2, ensure_ascii=False, sort_keys=True) + "\n")


def raw(path, records):
    path.write_text(
        "".join(json.dumps(x, ensure_ascii=False, sort_keys=True) + "\n" for x in records)
    )


def section_counts(path):
    source = path / "junit.xml"
    if not source.exists():
        return {"status": "NOT_RUN"}
    values = ET.parse(source).getroot().find("testsuite").attrib
    return {k: int(values[k]) for k in ("tests", "failures", "errors", "skipped")}


def integrity(records):
    for row in records:
        if row["partial_writes"] or row["reserved_micros"]:
            return False
        seen = {}
        for capture in row["captures"]:
            receipt = capture["receipt"]
            value = capture["original_cas_json"]
            op = value["operation_id"]
            if op in seen or value["run_id"] != row["run_id"] or op != receipt["operation_id"]:
                return False
            if value["finish_reason"] == "length" and (
                value["execution_status"] != "not_executed"
                or receipt["response_status"] != "incomplete"
            ):
                return False
            continuation = value["continuation"]
            if continuation:
                source = seen.get(continuation["parent_operation_id"])
                if not source or source["artifact_hash"] != continuation["source_artifact"]:
                    return False
            seen[op] = receipt
    return True


def patch_summary(source, environment):
    target = BASE / "h12"
    patches = [json.loads(f.read_text()) for f in sorted(source.glob("p3-patch-*.json"))]
    readiness = [json.loads(f.read_text()) for f in sorted(environment.glob("p3-env-*.json"))]
    arms = ("full_file", "chunked", "structured_plan", "verified_resume")
    data = {}
    for arm in arms:
        trials = [x for x in patches if x["arm"] == arm]
        success = sum(x["success"] for x in trials)
        data[arm] = {
            "trials": len(trials),
            "succeeded": success,
            "patches_parsed_and_applied": sum(x["apply_receipts"] for x in trials),
            "trials_with_truncation": sum(bool(x["truncated"]) for x in trials),
            "truncated_replies": sum(x["truncated"] for x in trials),
            "truncation_trial_success": sum(bool(x["truncated"]) and x["success"] for x in trials),
            "partial_writes": sum(x["partial_writes"] for x in trials),
            "same_run_recoveries": sum(x["recovered_original_run"] for x in trials),
            "fixture_model_calls": sum(x["model_calls"] for x in trials),
            "fixture_input_tokens": sum(x["fixture_input_tokens"] for x in trials),
            "fixture_output_tokens": sum(x["fixture_output_tokens"] for x in trials),
            "fixture_cost_micros": sum(x["fixture_cost_micros"] for x in trials),
        }
    bad_env = [row for row in readiness if row["ground_truth"] != "READY"]
    healthy = [row for row in readiness if row["ground_truth"] == "READY"]
    preflight = {
        "scheduled": len(load_lines(target / "preflight-cases.jsonl")),
        "observed": len(readiness),
        "faults": len(bad_env),
        "before_model_detections": sum(r["gate"]["status"] == "BLOCKED" for r in bad_env),
        "healthy": len(healthy),
        "false_blocks": sum(r["gate"]["status"] == "BLOCKED" for r in healthy),
        "correct_categories": sum(r["gate"]["category"] == r["ground_truth"] for r in readiness),
        "recovered_faults": sum(r["gate_plus_hint"]["schedule_allowed"] for r in bad_env),
        "avoided_invalid_first_slots": sum(
            r["no_gate"]["scheduled_model_slots"]
            for r in bad_env
            if r["gate"]["status"] == "BLOCKED"
        ),
        "actual_model_calls": sum(r["gate"]["actual_model_calls"] for r in readiness),
    }
    expected_patch = {
        (case["case_id"], arm) for case in load_lines(target / "cases.jsonl") for arm in arms
    }
    actual_patch = {(row["case_id"], row["arm"]) for row in patches}
    expected_env = {row["case_id"] for row in load_lines(target / "preflight-cases.jsonl")}
    missing_patch = sorted(expected_patch - actual_patch)
    missing_env = sorted(expected_env - {r["case_id"] for r in readiness})
    assert actual_patch <= expected_patch and len(actual_patch) == len(patches)
    assert len(readiness) == len({r["case_id"] for r in readiness})
    junit = {"patch": section_counts(source), "preflight": section_counts(environment)}
    passed = not missing_patch and not missing_env and integrity(patches)
    passed &= all(
        v.get("failures") == v.get("errors") == v.get("skipped") == 0 for v in junit.values()
    )
    passed &= all(data[arm]["succeeded"] == 8 for arm in arms if arm != "full_file")
    passed &= preflight["correct_categories"] == 12 and preflight["recovered_faults"] == 9
    raw(target / "raw.jsonl", patches + readiness)
    write(
        target / "metrics.json",
        {
            "patch": data,
            "preflight": preflight,
            "junit": junit,
            "missing_patch": missing_patch,
            "missing_preflight": missing_env,
        },
    )
    write(
        target / "acceptance.json",
        {
            "engineering": "PASS" if passed else "FAIL",
            "real_model": "NOT_RUN",
            "independent_operator": "NOT_RUN",
        },
    )
    return {"patch": data, "preflight": preflight}


def bootstrap(groups):
    rng = random.Random(173211)
    keys = sorted(groups)
    if not keys:
        return None
    results = sorted(statistics.mean(groups[rng.choice(keys)] for _ in keys) for _ in range(5000))
    return [results[124], results[4874]]


def repeated_source_reads(row):
    review = row["consultation_receipt"]
    if not review or review["status"] != "ok":
        return 0
    reviewed = set(review["data"]["source_paths"])
    read = {
        call["arguments"]["path"]
        for operation in row["operations"]
        for call in operation["response"]["tool_calls"]
        if call["name"] == "read_file"
    }
    return len(reviewed & read)


def delegation_summary(slices):
    target = BASE / "h17"
    schedule = load_lines(target / "schedule.jsonl")
    cases = load_lines(target / "cases.jsonl")
    expected = {(r["case_id"], r["seed"], arm) for r in schedule for arm in r["arms"]}
    results = [row for s in slices for f in sorted(s.glob("p3-*.jsonl")) for row in load_lines(f)]
    indexed = {(r["case_id"], r["seed"], r["arm"]): r for r in results}
    assert len(indexed) == len(results) and set(indexed) <= expected
    complete = set(indexed) == expected
    outcomes = {}
    budgets = (
        "fixture_cost_micros",
        "fixture_input_tokens",
        "fixture_output_tokens",
        "model_calls",
    )
    for a in ("single", "delegate"):
        rows = [r for r in results if r["arm"] == a]
        item = {
            "successes": sum(r["grade"]["pass"] for r in rows),
            "observed": len(rows),
            "mean_quality": statistics.mean(r["grade"]["quality"] for r in rows) if rows else None,
            "p50_seconds": statistics.median(r["wall_seconds"] for r in rows) if rows else None,
            "parallel_wait_seconds": None,
        }
        for key in (
            *budgets,
            "duplicate_calls",
            "expert_calls",
            "overlapping_write_conflicts",
            "verifier_failures",
            "delegated_model_seconds",
            "reserved_micros",
        ):
            item[key] = sum(r[key] for r in rows)
        item["parent_and_expert_reviewed_same_source"] = sum(repeated_source_reads(r) for r in rows)
        item["p95_seconds"] = (
            sorted(r["wall_seconds"] for r in rows)[math.ceil(0.95 * len(rows)) - 1]
            if rows
            else None
        )
        outcomes[a] = item
    by_label = {}
    pairs = {}
    same_tree = 0
    for case in cases:
        cid = case["case_id"]
        paired = []
        for seed in (17, 419, 9841):
            single = indexed.get((cid, seed, "single"))
            delegate = indexed.get((cid, seed, "delegate"))
            if single and delegate:
                paired.append(int(delegate["grade"]["pass"]) - int(single["grade"]["pass"]))
                same_tree += int(
                    single["source_tree"] == delegate["source_tree"]
                    and single["tree"] == delegate["tree"]
                    and single["diff"] == delegate["diff"]
                )
        if len(paired) == 3:
            pairs[cid] = statistics.mean(paired)
            by_label.setdefault(case["stratum"], []).extend(paired)
    diff = statistics.mean(pairs.values()) if pairs else None
    ci = bootstrap(pairs)
    s, d = outcomes["single"], outcomes["delegate"]
    cost_ratio = d["fixture_cost_micros"] / s["fixture_cost_micros"] if complete else None
    wall_ratio = d["p50_seconds"] / s["p50_seconds"] if complete else None
    junit = [section_counts(x) for x in slices]
    engineering = (
        complete
        and same_tree == len(schedule)
        and all(r.get("failures") == r.get("errors") == r.get("skipped") == 0 for r in junit)
    )
    raw(target / "raw.jsonl", sorted(results, key=lambda r: (r["case_id"], r["seed"], r["arm"])))
    write(
        target / "acceptance.json",
        {
            "engineering": "PASS" if engineering else "FAIL",
            "live": "NOT_RUN",
            "real_model_advantage": "NOT_ESTABLISHED",
            "independent_operator": "NOT_RUN",
            "default": "single",
        },
    )
    stats = {
        "results": outcomes,
        "junit": junit,
        "paired_case_clusters": len(pairs),
        "same_deliveries": same_tree,
        "missing": sorted(expected - set(indexed)),
        "fixture_paired_difference": diff,
        "fixture_paired_interval95": ci,
        "fixture_superiority": complete and ci[0] > 0 and diff >= 2 / 30 if ci else False,
        "fixture_noninferiority": complete and ci[0] >= -0.05 if ci else False,
        "fixture_cost_ratio": cost_ratio,
        "fixture_p50_wall_ratio": wall_ratio,
        "fixture_stratum_rates": {
            k: dict(pairs=len(v), difference=statistics.mean(v)) for k, v in by_label.items()
        },
    }
    write(target / "metrics.json", stats)
    return stats


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--h12-patch", type=Path, required=True)
    parser.add_argument("--h12-preflight", type=Path, required=True)
    parser.add_argument("--h17", type=Path, nargs="+", required=True)
    args = parser.parse_args()
    results = {
        "h12": patch_summary(args.h12_patch, args.h12_preflight),
        "h17": delegation_summary(args.h17),
    }
    for study, directory in (("h12", BASE / "h12"), ("h17", BASE / "h17")):
        files = ["cases.jsonl", "raw.jsonl", "metrics.json", "acceptance.json"]
        experiment = {
            "study": study,
            "parent_manifest_sha256": hashlib.sha256(
                (BASE / "manifest.json").read_bytes()
            ).hexdigest(),
            "file_sha256": {
                name: hashlib.sha256((directory / name).read_bytes()).hexdigest() for name in files
            },
        }
        write(directory / "manifest.json", experiment)
    print(json.dumps(results, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
