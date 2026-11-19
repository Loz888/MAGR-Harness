"""Freeze and evaluate GA12 fixture bundles with separate, preserved receipts."""

from __future__ import annotations

import hashlib
import json
import sys
from collections import Counter
from pathlib import Path
from time import perf_counter_ns
from typing import Any

from pydantic import ValidationError

from mgar_v3.quality_eval.ga12_contracts import Arm, Arms, Case, Domains, Freeze, Mode, Split
from mgar_v3.quality_eval.ga12_fixture import execute
from mgar_v3.quality_eval.ga12_grader import grade

_PROJECT = Path(__file__).resolve().parents[3]
_CONTRACT = _PROJECT / "docs/general-agent-v1/GA12_CASE_CONTRACT.md"
_DEVELOPMENT = _PROJECT / "experiments/general-agent/ga12/dev.jsonl"
_MAX_BUNDLE_BYTES = 2_000_000
_MEASURES = (
    "task_success",
    "planning_quality",
    "tool_selection",
    "evidence_quality",
    "external_effects_correct",
)
_USAGE = {
    "provider": "ga12-mock",
    "model": None,
    "model_calls": 0,
    "input_tokens": 0,
    "output_tokens": 0,
    "cost_microusd": 0,
    "real_external_writes": 0,
}


class BenchmarkError(ValueError):
    """A bounded error message that can be published without case contents."""


def _bounded(path: Path, size: int = _MAX_BUNDLE_BYTES) -> bytes:
    with path.open("rb") as stream:
        data = stream.read(size + 1)
    if len(data) > size:
        raise BenchmarkError("GA12 input exceeds its byte limit.")
    return data


def require_private(path: Path) -> None:
    if path.resolve().is_relative_to(_PROJECT.parent):
        raise BenchmarkError("Keep held-out cases and detailed receipts outside the repository.")


def _load(bundle: Path) -> tuple[bytes, tuple[Case, ...]]:
    data = _bounded(bundle)
    try:
        text = data.decode("utf-8")
    except UnicodeError:
        raise BenchmarkError("GA12 bundle must be UTF-8.") from None
    lines = text.split("\n")
    if lines[-1] != "" or not 60 <= len(lines) - 1 <= 1_000:
        raise BenchmarkError("GA12 requires 60–1,000 JSONL cases with a final newline.")
    try:
        cases = tuple(Case.model_validate_json(line) for line in lines[:-1])
    except ValidationError:
        raise BenchmarkError("A GA12 case does not meet the frozen schema.") from None
    if len({case.id for case in cases}) != len(cases):
        raise BenchmarkError("GA12 case IDs must be unique within the split.")
    return data, cases


def _freeze(data: bytes, cases: tuple[Case, ...], split: Split) -> Freeze:
    counts = Counter(case.domain for case in cases)
    if split == "holdout" and data == _bounded(_DEVELOPMENT):
        raise BenchmarkError("The published development bundle is ineligible for holdout.")
    try:
        return Freeze(
            split=split,
            bundle_sha256=hashlib.sha256(data).hexdigest(),
            counts={domain: counts[domain] for domain in Domains},
            author_role="independent" if split == "holdout" else "development",
            contract_sha256=hashlib.sha256(_CONTRACT.read_bytes()).hexdigest(),
        )
    except ValidationError:
        raise BenchmarkError("GA12 needs ten or more cases in every domain.") from None


def freeze(bundle: Path, split: Split) -> Freeze:
    if split == "holdout":
        require_private(bundle)
    return _freeze(*_load(bundle), split)


def _write_json(path: Path, obj: Any) -> None:
    with path.open("x", encoding="utf-8") as stream:
        json.dump(obj, stream, ensure_ascii=False, indent=2, sort_keys=True)
        stream.write("\n")


def write_freeze(bundle: Path, split: Split, destination: Path) -> Freeze:
    if split == "holdout":
        require_private(destination)
    manifest = freeze(bundle, split)
    _write_json(destination, manifest.model_dump(mode="json"))
    return manifest


def _benchmark_case(case: Case, arm: Arm, out: Path) -> dict[str, Any]:
    location = out / arm / case.id
    start = perf_counter_ns()
    actual = execute(case.public_input(), arm, location)
    elapsed = perf_counter_ns() - start
    score = grade(case, actual, location)
    return {
        "id": case.id,
        "domain": case.domain,
        "arm": arm,
        "expected": case.expected.model_dump(mode="json"),
        "actual": actual.model_dump(mode="json"),
        "score": score.model_dump(mode="json"),
        "passed": score.passed,
        "latency_ns": elapsed,
        "failure_code": next(
            (name for name in _MEASURES if not getattr(score, name)),
            "policy_violation" if score.policy_violations else None,
        ),
        "usage": _USAGE,
    }


def _aggregate(rows: list[dict[str, Any]]) -> dict[str, Any]:
    latency = sorted(row["latency_ns"] for row in rows)
    return {
        "evaluated": len(rows),
        "passed": sum(row["passed"] for row in rows),
        "failed": sum(not row["passed"] for row in rows),
        **{measure: sum(row["score"][measure] for row in rows) for measure in _MEASURES},
        "policy_violations": sum(len(row["score"]["policy_violations"]) for row in rows),
        "external_effect_errors": sum(not row["score"]["external_effects_correct"] for row in rows),
        "statuses": dict(Counter(row["actual"]["status"] for row in rows)),
        "failures": dict(Counter(row["failure_code"] for row in rows if not row["passed"])),
        "failure_reasons": dict(
            Counter(
                (row["actual"]["reason"] or row["failure_code"])
                if not row["score"]["task_success"]
                else row["failure_code"]
                for row in rows
                if not row["passed"]
            )
        ),
        "accepted_fixture_effects": sum(len(row["actual"]["effects"]) for row in rows),
        "latency_ns": {
            "sum": sum(latency),
            "p50": latency[(len(latency) - 1) // 2],
            "p95": latency[(95 * len(latency) - 1) // 100],
        },
        "usage": _USAGE,
    }


def _source_hashes() -> dict[str, str]:
    return {
        source.name: hashlib.sha256(source.read_bytes()).hexdigest()
        for source in sorted(Path(__file__).parent.glob("ga12_*.py"))
    }


def run(
    manifest_path: Path,
    bundle: Path,
    out: Path,
    *,
    mode: Mode = "mock",
    arms: tuple[Arm, ...] = Arms,
) -> dict[str, Any]:
    try:
        manifest = Freeze.model_validate_json(_bounded(manifest_path, 16_384))
    except ValidationError:
        raise BenchmarkError("Invalid GA12 freeze manifest.") from None
    if manifest.split == "holdout":
        require_private(bundle)
        require_private(out)
    data, cases = _load(bundle)
    if manifest != _freeze(data, cases, manifest.split):
        raise BenchmarkError("GA12 bundle or contract differs from the frozen SHA-256.")
    if not arms or len(set(arms)) != len(arms) or set(arms) - set(Arms):
        raise BenchmarkError("Provide distinct GA12 ablation arms.")
    out.mkdir(mode=0o700, parents=True, exist_ok=False)
    report: dict[str, Any] = {
        "schema_version": "ga12-run-v1",
        "freeze": manifest.model_dump(mode="json"),
        "mode": mode,
        "fixture_environment": "ga12-fixture-v1",
        "python": sys.version.split()[0],
        "source_sha256": _source_hashes(),
        "verdict": "NOT_RUN",
        "arms": {},
    }
    if mode == "mock":
        with (out / "raw.jsonl").open("x", encoding="utf-8") as log:
            for arm in arms:
                rows = []
                for case in cases:
                    row = _benchmark_case(case, arm, out)
                    rows.append(row)
                    log.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
                    log.flush()
                report["arms"][arm] = {
                    "all": _aggregate(rows),
                    "domains": {
                        domain: _aggregate([row for row in rows if row["domain"] == domain])
                        for domain in Domains
                    },
                }
        scored_arms = ("multi_domain",) if "multi_domain" in arms else arms
        report["verdict"] = (
            "PASS"
            if all(report["arms"][arm]["all"]["failed"] == 0 for arm in scored_arms)
            else "FAIL"
        )
        report["raw_sha256"] = hashlib.sha256((out / "raw.jsonl").read_bytes()).hexdigest()
    elif mode in ("real-provider", "real-external"):
        report["reason"] = "No live runner is configured for this benchmark."
        report["usage"] = {
            "provider": None,
            "model": None,
            "model_calls": 0,
            "input_tokens": 0,
            "output_tokens": 0,
            "cost_microusd": 0,
            "real_external_writes": 0,
        }
    else:
        raise BenchmarkError("Unsupported GA12 evaluation mode.")
    _write_json(out / "summary.json", report)
    return report
