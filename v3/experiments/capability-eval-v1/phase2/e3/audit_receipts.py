"""Reconcile every finished model step and verify the bytes of each delivered result."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path

BASE = Path(__file__).resolve().parent
STORE = BASE.parents[3] / "evidence/CAPABILITY-PHASE2/e3/formal"


def audit():
    expected = set()
    for line in (BASE / "cases.jsonl").read_text().splitlines():
        case = json.loads(line)
        for arm in ("off", "bm25", "hybrid", "hybrid_reranked"):
            for n in range(1, len(case["releases"]) + 1):
                expected.add(f"{case['split']}/{case['case_id']}/{arm}/{n}")
    work = STORE / (STORE / "latest.txt").read_text().strip()
    data = (work / "raw.jsonl").read_bytes()
    results = [json.loads(line) for line in data.splitlines()]
    with sqlite3.connect((STORE / "ledger.sqlite").as_uri() + "?mode=ro", uri=True) as db:
        db.row_factory = sqlite3.Row
        assert db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        records = db.execute("SELECT * FROM requests ORDER BY trial").fetchall()
        assert (
            db.execute("SELECT freeze FROM grant_meta").fetchone()[0]
            == hashlib.sha256((BASE / "manifest.json").read_bytes()).hexdigest()
        )
    by_session = defaultdict(list)
    off_peak = {}
    for row in records:
        assert row["state"] == "settled" and not row["reason"] and row["completion_id"]
        usage = json.loads(row["usage"])
        start, end = (
            datetime.fromisoformat(row[k]).astimezone(UTC) for k in ("started", "finished")
        )
        hit = usage.get("prompt_cache_hit_tokens")
        miss = usage.get("prompt_cache_miss_tokens")
        if (
            start.date() == end.date()
            and start.hour == end.hour
            and start.hour not in (1, 2, 3, 6, 7, 8, 9)
            and type(hit) is int
            and type(miss) is int
            and hit + miss == row["input"]
        ):
            off_peak[row["trial"]] = hit * 3 + miss * 150 + row["output"] * 600
        assert (
            row["input"] == usage["prompt_tokens"] and row["output"] == usage["completion_tokens"]
        )
        assert row["cost"] == (3 * row["input"] + 12 * row["output"] + 9) // 10
        assert (
            row["cost"] <= row["reserve"] and len(row["body_sha"]) == len(row["response_sha"]) == 64
        )
        by_session[row["trial"].rsplit("/", 1)[0]].append(row)
    assert set(by_session) == expected
    assert len({r["completion_id"] for r in records}) == len(records)
    seen = set()
    verified = 0
    for result in results:
        session = "{split}/{case_id}/{arm}/{session}".format(**result)
        steps = by_session[session]
        assert session not in seen
        seen.add(session)
        assert [r["trial"].split("/")[-1] for r in steps] == [str(n + 1) for n in range(len(steps))]
        assert result["model_calls"] == len(steps) <= 3
        for key, field in [
            ("cost_micros", "cost"),
            ("input_tokens", "input"),
            ("output_tokens", "output"),
        ]:
            assert result[key] == sum(r[field] for r in steps)
        if result["final_success"]:
            digest = result["delivery_sha256"]
            path = work / "cas" / (result["case_id"] + "-" + result["arm"]) / digest[:2] / digest
            assert hashlib.sha256(path.read_bytes()).hexdigest() == digest
            answer = json.loads(path.read_bytes())["submission"]
            assert {**answer, "error": None} == result["actions"][-1]
            verified += 1
    assert seen == expected
    return {
        "status": "PASS",
        "planned_and_observed_sessions": len(expected),
        "unique_settled_completions": len(records),
        "input_tokens": sum(r["input"] for r in records),
        "output_tokens": sum(r["output"] for r in records),
        "upper_cost_micros": sum(r["cost"] for r in records),
        "outstanding_micros": 0,
        "receipt_priced_rows": len(off_peak),
        "off_peak_estimated_nanodollars": sum(off_peak.values())
        if len(off_peak) == len(records)
        else None,
        "off_peak_estimated_nanodollars_by_arm": {
            arm: sum(v for key, v in off_peak.items() if key.split("/")[2] == arm)
            for arm in ("off", "bm25", "hybrid", "hybrid_reranked")
        }
        if len(off_peak) == len(records)
        else None,
        "public_rate_source": "https://api-docs.deepseek.com/quick_start/pricing/",
        "supplier_invoice_review": "NOT_RUN",
        "delivery_artifacts_verified": verified,
        "raw_sha256": hashlib.sha256(data).hexdigest(),
    }


if __name__ == "__main__":
    print(json.dumps(audit(), ensure_ascii=False, indent=2))
