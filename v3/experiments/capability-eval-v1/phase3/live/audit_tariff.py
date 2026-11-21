"""Reconcile public off-peak prices with unmodified provider SSE usage frames."""

from __future__ import annotations

import base64
import json
import sqlite3
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[3] / "evidence/CAPABILITY-PHASE3-LIVE"


def audit():
    with sqlite3.connect((ROOT / "ledger.sqlite").as_uri() + "?mode=ro", uri=True) as db:
        db.row_factory = sqlite3.Row
        receipts = {
            r["operation"]: dict(r) for r in db.execute("SELECT * FROM calls WHERE state='settled'")
        }
    chunks = defaultdict(dict)
    for path in (ROOT / "runtime").glob("worker-*/cas/*/*"):
        if not path.is_file():
            continue
        contents = path.read_bytes()
        if not contents.startswith(b"{") or b'"wire_base64"' not in contents:
            continue
        try:
            row = json.loads(contents)
        except (ValueError, UnicodeError):
            continue
        if (
            row.get("operation_id") in receipts
            and "wire_sequence" in row
            and isinstance(row.get("wire_base64"), str)
        ):
            op, sequence = row["operation_id"], row["wire_sequence"]
            chunk = base64.b64decode(row["wire_base64"], validate=True)
            if sequence in chunks[op] and chunks[op][sequence] != chunk:
                raise ValueError("A provider wire sequence contains different bytes")
            chunks[op][sequence] = chunk
    totals = defaultdict(Counter)
    failed = Counter()
    n = 0
    for op, receipt in receipts.items():
        s = receipt["study"]
        if not chunks[op] or sorted(chunks[op]) != list(range(len(chunks[op]))):
            failed[s] += 1
            continue
        settled = json.loads(receipt["receipt"])["usage"]
        found = None
        wire = b"".join(chunks[op][i] for i in range(len(chunks[op])))
        for line in wire.splitlines():
            if line.startswith(b"data:"):
                data = line[5:].strip()
                if not data or data == b"[DONE]":
                    continue
                payload = json.loads(data)
                if payload.get("usage"):
                    found = payload["usage"]
        if not found:
            failed[s] += 1
            continue
        hit, miss = found.get("prompt_cache_hit_tokens"), found.get("prompt_cache_miss_tokens")
        start, end = (datetime.fromisoformat(receipt[key]) for key in ("started", "ended"))
        if not (
            type(hit) is int
            and type(miss) is int
            and hit + miss == found["prompt_tokens"] == settled["input_tokens"]
            and found["completion_tokens"] == settled["output_tokens"]
            and start.weekday() >= 5
            and start.date() == end.date()
        ):
            failed[s] += 1
            continue
        n += 1
        totals[s].update(
            {
                "priced_requests": 1,
                "input_cache_hit_tokens": hit,
                "input_cache_miss_tokens": miss,
                "output_tokens": found["completion_tokens"],
                "estimated_usd_nano": hit * 3 + miss * 150 + 600 * found["completion_tokens"],
            }
        )
    return {
        "settled": len(receipts),
        "priced": n,
        "complete": n == len(receipts),
        "missing_or_not_rateable": dict(failed),
        "studies": {s: dict(r) for s, r in totals.items()},
        "total_estimated_usd_nano": sum(r["estimated_usd_nano"] for r in totals.values()),
        "pricing_url": "https://api-docs.deepseek.com/quick_start/pricing/",
        "invoice_reconciliation": "NOT_RUN",
    }


if __name__ == "__main__":
    print(json.dumps(audit(), indent=2, ensure_ascii=False))
