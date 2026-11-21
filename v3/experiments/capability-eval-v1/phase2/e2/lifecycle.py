"""Budgeted formal-only runner. A unique durable ledger covers all arms and both splits."""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path
from time import perf_counter
from uuid import uuid4

import httpx
from matrix import (
    CALLS,
    HERE,
    INPUT_CAP,
    LOCAL,
    MODEL,
    MODEL_RELEASE,
    READS,
    TOOL,
    ArtifactStore,
    build,
    encoded,
    lines,
    load_case,
    messages,
    pin,
    schedule,
    sha256,
    tokenizer,
    verify,
)

from mgar_v3.domain import DomainError
from mgar_v3.quality_eval.e2_context import ENDPOINT, MAX_OUTPUT, wire_body
from mgar_v3.quality_eval.e2_grader import byte_exact_artifacts, context_grade
from mgar_v3.quality_eval.e2_live import read_page

OUT = LOCAL / "formal"
BUDGET = 369_000_000
MAX_CALLS = 5760
MAX_REPLY_BYTES = 524_288


def usd_micros(inp, out):
    return (inp * 30 + out * 120 + 99) // 100


def instant():
    return datetime.now(UTC).isoformat()


def rate_band(stamp):
    dt = datetime.fromisoformat(stamp).astimezone(UTC)
    # November has no China statutory public holidays in this experiment.
    if (dt.year, dt.month) != (2026, 11):
        return None
    return "peak" if dt.weekday() < 5 and (1 <= dt.hour < 4 or 6 <= dt.hour < 10) else "off_peak"


def tariff_nano(usage, start, end):
    hit = usage.get("prompt_cache_hit_tokens")
    miss = usage.get("prompt_cache_miss_tokens")
    band = rate_band(start)
    if (
        type(hit) is not int
        or type(miss) is not int
        or min(hit, miss) < 0
        or hit + miss != usage["prompt_tokens"]
        or band is None
        or band != rate_band(end)
    ):
        return None
    peak = hit * 6 + miss * 300 + usage["completion_tokens"] * 1200
    return peak if band == "peak" else peak // 2


class Ledger:
    def __init__(self, root):
        self.path = root / "ledger.sqlite"
        with self.db() as db:
            db.execute("pragma journal_mode=WAL")
            db.executescript("""
                create table if not exists calls (
                    run_id text, ordinal integer, state text, bound integer, charge integer,
                    entry text, payload text, primary key(run_id,ordinal));
                create table if not exists meta (name text primary key, value text);
                create table if not exists trials (run_id text primary key, row text);
            """)

    def db(self):
        db = sqlite3.connect(self.path, timeout=60)
        db.execute("pragma synchronous=FULL")
        return db

    def halted(self):
        with self.db() as db:
            return db.execute("select value from meta where name='halt'").fetchone()

    def stop(self, reason):
        with self.db() as db:
            db.execute("insert or ignore into meta values('halt',?)", (reason,))

    def bind(self, permit):
        with self.db() as db:
            db.execute("begin immediate")
            freeze = db.execute("select value from meta where name='permit'").fetchone()
            if freeze is not None and freeze != (permit,):
                raise ValueError("Ledger belongs to a different authorization")
            db.execute("insert or ignore into meta values('permit',?)", (permit,))
            # The exclusive process lock proves that a surviving reservation has no active sender.
            if db.execute("select count(*) from calls where state='RESERVED'").fetchone()[0]:
                db.execute("update calls set state='UNKNOWN' where state='RESERVED'")
                db.execute("insert or ignore into meta values('halt','UNRESOLVED_RESERVATION')")

    def reserve(self, entry, maximum):
        with self.db() as db:
            db.execute("begin immediate")
            key = (entry["run_id"], entry["ordinal"])
            old = db.execute(
                "select state,entry,payload from calls where run_id=? and ordinal=?", key
            ).fetchone()
            if old:
                prior = json.loads(old[1])
                if prior["request_sha256"] != entry["request_sha256"]:
                    raise ValueError("Replay diverged")
                return old[0], prior, json.loads(old[2]) if old[2] else None
            if db.execute("select count(*) from meta where name='halt'").fetchone()[0]:
                return "HALT", None, None
            used, calls = db.execute(
                "select coalesce(sum(coalesce(charge,bound)),0),count(*) from calls"
            ).fetchone()
            if used + maximum > BUDGET or calls >= MAX_CALLS:
                db.execute("insert or ignore into meta values('halt','BUDGET_EXHAUSTED')")
                return "HALT", None, None
            db.execute(
                "insert into calls values(?,?,'RESERVED',?,null,?,null)",
                (*key, maximum, encoded(entry).decode()),
            )
            return "SEND", None, None

    def settle(self, entry, payload, reason=None):
        with self.db() as db:
            db.execute("begin immediate")
            state = "SETTLED" if entry.get("provider_usage") else "UNKNOWN"
            entry["state"] = state
            changed = db.execute(
                "update calls set state=?,entry=?,payload=?,charge=?"
                "where run_id=? and ordinal=? and state='RESERVED'",
                (
                    state,
                    encoded(entry).decode(),
                    encoded(payload).decode(),
                    entry.get("peak_cost_micros"),
                    entry["run_id"],
                    entry["ordinal"],
                ),
            ).rowcount
            if changed != 1:
                raise RuntimeError("Missing pre-send reservation")
            if reason:
                db.execute("insert or ignore into meta values('halt',?)", (reason,))

    def completed(self, run_id):
        with self.db() as db:
            return bool(db.execute("select 1 from trials where run_id=?", (run_id,)).fetchone())

    def finish(self, row):
        with self.db() as db:
            db.execute("insert into trials values(?,?)", (row["run_id"], encoded(row).decode()))


def call(ledger, client, key, prompt, row, ordinal, purpose, expiry):
    if datetime.now(UTC) >= expiry:
        ledger.stop("AUTHORIZATION_EXPIRED")
        return None, None
    tok = tokenizer()
    rendered = tok.render(prompt)
    wire = wire_body(prompt)
    bound = len(rendered.encode()) + 4096
    if bound > INPUT_CAP:
        ledger.stop("INPUT_BOUND_EXCEEDS_CAP")
        return None, None
    budget = usd_micros(bound, MAX_OUTPUT)
    entry = dict(
        request_id=str(uuid4()),
        run_id=row["run_id"],
        ordinal=ordinal,
        purpose=purpose,
        request_sha256=sha256(wire),
        reference_input=tok.count(rendered),
        input_bound=bound,
        output_bound=MAX_OUTPUT,
        reservation_micros=budget,
        started=instant(),
    )
    action, receipt, payload = ledger.reserve(entry, budget)
    if action != "SEND":
        return (payload if action == "SETTLED" else None), receipt
    reason = None
    payload = {}
    start = perf_counter()
    try:
        with client.stream(
            "POST",
            ENDPOINT,
            content=wire,
            headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        ) as response:
            entry.update(
                http_status=response.status_code,
                provider_request_id=response.headers.get("x-request-id"),
            )
            data = bytearray()
            for chunk in response.iter_bytes():
                data.extend(chunk)
                if len(data) > MAX_REPLY_BYTES:
                    raise ValueError("RESPONSE_BYTE_LIMIT")
            obj = json.loads(data)
            if isinstance(obj, dict):
                payload = obj
            if response.status_code != 200:
                reason = f"HTTP_{response.status_code}"
    except (httpx.HTTPError, ValueError, RecursionError) as exc:
        reason = f"UNCONFIRMED_{type(exc).__name__}"
    entry.update(ended=instant(), latency_ms=round((perf_counter() - start) * 1000, 3))
    usage = payload.get("usage")
    if (
        isinstance(usage, dict)
        and type(usage.get("prompt_tokens")) is int
        and usage["prompt_tokens"] > 0
        and type(usage.get("completion_tokens")) is int
        and usage["completion_tokens"] >= 0
    ):
        inp, out = usage["prompt_tokens"], usage["completion_tokens"]
        charge = usd_micros(inp, out)
        entry.update(
            provider_usage=usage,
            input_tokens=inp,
            output_tokens=out,
            reference_delta=inp - entry["reference_input"],
            peak_cost_micros=charge,
            tariff_cost_nano_usd=tariff_nano(usage, entry["started"], entry["ended"]),
            model=payload.get("model"),
            fingerprint=payload.get("system_fingerprint"),
        )
        if inp > bound or out > MAX_OUTPUT or charge > budget:
            reason = "PROVIDER_USAGE_ABOVE_RESERVATION"
        if payload.get("model") not in (MODEL, MODEL_RELEASE):
            reason = "MODEL_ID_CHANGED"
    else:
        reason = reason or "PROVIDER_USAGE_MISSING"
        entry["provider_usage"] = None
    entry["reason"] = reason
    ledger.settle(entry, payload, reason)
    return payload if reason is None else None, entry


def run_trial(ledger, client, key, case, trial, expires):
    row = {**trial, "mode": "live", "calls": [], "reads": [], "answer": None, "status": "FAIL"}
    cas = ArtifactStore(LOCAL / "cas")
    start = perf_counter()
    bundle = build(case, trial["condition"], tokenizer(), cas)
    row["context_ms"] = round((perf_counter() - start) * 1000, 3)
    guard = context_grade(case, bundle, byte_exact_artifacts(case, cas))
    row["integrity"] = guard
    if guard["permission_mutation"]:
        ledger.stop("PERMISSION_MUTATION")
        row["reason"] = "PERMISSION_MUTATION"
        return row
    prompt = messages(bundle)
    purpose = "initial"
    retried = False
    for ordinal in range(1, CALLS + 1):
        payload, entry = call(ledger, client, key, prompt, row, ordinal, purpose, expires)
        if entry:
            row["calls"].append(entry)
        if payload is None:
            row.update(
                status="UNKNOWN" if entry and not entry.get("provider_usage") else "BLOCKED",
                reason=ledger.halted()[0] if ledger.halted() else "HALTED",
            )
            return row
        try:
            (choice,) = payload["choices"]
            reply = choice["message"]
            finish = choice["finish_reason"]
            if (
                finish == "stop"
                and isinstance(reply.get("content"), str)
                and not reply.get("tool_calls")
            ):
                row.update(status="ANSWERED", answer=reply["content"], reason=None)
                return row
            if finish == "length" and not retried and ordinal < CALLS:
                # Account for this settled request and the entire recovery prompt.
                retried = True
                purpose = "retry_length"
                prompt.extend(
                    [
                        {"role": "assistant", "content": reply.get("content") or ""},
                        {"role": "user", "content": "Return the concise final handoff JSON now."},
                    ]
                )
                continue
            if finish != "tool_calls" or len(row["reads"]) >= READS or ordinal == CALLS:
                row["reason"] = (
                    "TOOL_OR_ROUND_LIMIT" if finish == "tool_calls" else f"FINISH_{finish}"
                )
                return row
            (tool_call,) = reply["tool_calls"]
            if tool_call["type"] != "function" or tool_call["function"]["name"] != TOOL:
                raise ValueError("UNAVAILABLE_TOOL")
            args = json.loads(tool_call["function"]["arguments"])
            tool_id = tool_call["id"]
            if not isinstance(tool_id, str) or not 1 <= len(tool_id) <= 200:
                raise ValueError("INVALID_TOOL_ID")
            page = read_page(case, bundle, cas, args)
            body = page["text"].encode()
            start_at, stop_at = page["offset"], page["end_offset"]
            if body != cas.read(page["content_hash"])[start_at:stop_at]:
                ledger.stop("BYTE_MISMATCH")
                raise ValueError("BYTE_MISMATCH")
            duplicate = any(
                r["hash"] == page["content_hash"] and r["start"] == start_at for r in row["reads"]
            )
            row["reads"].append(
                dict(
                    ordinal=ordinal,
                    id=args["evidence_id"],
                    hash=page["content_hash"],
                    start=start_at,
                    end=stop_at,
                    bytes=len(body),
                    sha256=sha256(body),
                    body_reference_tokens=tokenizer().count(page["text"]),
                    wire_reference_tokens=tokenizer().count(encoded(page).decode()),
                    repeated=duplicate,
                )
            )
            prompt.extend(
                [
                    {
                        "role": "assistant",
                        "content": reply.get("content") or "",
                        "reasoning_content": reply.get("reasoning_content") or "",
                        "tool_calls": [tool_call],
                    },
                    {"role": "tool", "tool_call_id": tool_id, "content": encoded(page).decode()},
                ]
            )
            purpose = "repeated_read" if duplicate else "artifact_read"
        except (ValueError, DomainError, KeyError, TypeError, AttributeError) as exc:
            row["reason"] = f"TOOL_OR_MODEL_FORMAT_{type(exc).__name__}"
            return row
    row["reason"] = "ROUND_LIMIT"
    return row


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--authorization", required=True, type=Path)
    parser.add_argument("--workers", type=int, default=5, choices=range(1, 13))
    args = parser.parse_args()
    permit = json.loads(args.authorization.read_bytes())
    expires = datetime.fromisoformat(permit["expires"]).astimezone(UTC)
    if (
        permit["schema"] != "e2-r1-formal-v1"
        or permit["max_calls"] != MAX_CALLS
        or permit["max_usd_micros"] != BUDGET
        or permit["freeze"] != pin(HERE / "manifest.json")
        or permit["output"] != str(OUT.resolve())
        or permit["data"] != "synthetic-only"
        or datetime.now(UTC) >= expires
    ):
        raise ValueError("Formal permit does not bind this freeze/budget/path")
    indexes = verify()
    key = os.environ["DEEPSEEK_API_KEY"]
    if not key:
        raise ValueError("Credential missing")
    offline = lines(HERE / "raw.jsonl")
    expected = {t["run_id"] for i in indexes for t in schedule(i["case_id"], i["scale"])}
    if (
        len(offline) != 1440
        or {r["run_id"] for r in offline} != expected
        or any(r["integrity"]["permission_mutation"] for r in offline)
        or any(
            r["integrity"][c]["kept"] != r["integrity"][c]["total"]
            for r in offline
            if r["condition"] == "magr_compaction"
            for c in ("artifacts", "failures", "constraints", "evidence", "approvals")
        )
    ):
        raise ValueError("Offline gate incomplete")
    OUT.mkdir(mode=0o700, parents=True, exist_ok=True)
    with (
        (OUT / "lock").open("a") as lock,
        httpx.Client(
            timeout=httpx.Timeout(180, connect=20),
            follow_redirects=False,
            transport=httpx.HTTPTransport(retries=0),
        ) as client,
    ):
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        ledger = Ledger(OUT)
        ledger.bind(pin(args.authorization))

        def block(index):
            case = load_case(index)
            for trial in schedule(case.case_id, case.scale):
                if ledger.halted():
                    return
                if ledger.completed(trial["run_id"]):
                    continue
                row = run_trial(ledger, client, key, case, trial, expires)
                row["split"] = index["split"]
                ledger.finish(row)

        block(indexes[0])
        with ThreadPoolExecutor(max_workers=args.workers) as ex:
            for _ in ex.map(block, indexes[1:]):
                pass
        print(json.dumps({"halt": ledger.halted(), "ledger": str(ledger.path)}))


if __name__ == "__main__":
    main()
