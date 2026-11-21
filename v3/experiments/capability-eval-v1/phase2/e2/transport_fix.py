"""Formal transport repair with one narrowly bound, append-only reconciliation."""

import argparse
import fcntl
import json
import os
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime

import httpx
from lifecycle import BUDGET, MAX_CALLS, OUT, Ledger, run_trial
from matrix import HERE, LOCAL, load_case, pin, schedule, verify

REQUEST = "a338f64a-73bd-47b3-b5b6-58ee23bc5e99"
PRIOR = "fef32397d832ef7cf6f8508ec511d59236675c214903003caeccb916b08501f7"
REPAIR = HERE / "transport-fix.json"


def bind(ledger, permit_hash, new_pin):
    # The process already owns the exclusive runner lock.
    with ledger.db() as db:
        db.execute("begin immediate")
        db.execute(
            "create table if not exists reconciliation (request_id text primary key,"
            " original_entry text, original_trial text, original_meta text, finding text,"
            " charge_micros integer, new_freeze text)"
        )
        old = db.execute(
            "select new_freeze from reconciliation where request_id=?", (REQUEST,)
        ).fetchone()
        if old:
            if old != (new_pin,):
                raise ValueError("Unexpected transport repair")
        else:
            rows = db.execute("select state,bound,charge,entry from calls").fetchall()
            ((state, bound, charge, entry),) = rows
            receipt = json.loads(entry)
            assert (state, bound, charge) == ("UNKNOWN", 10893, None)
            assert (receipt["request_id"], receipt["ordinal"], receipt["reason"]) == (
                REQUEST,
                1,
                "UNCONFIRMED_ConnectError",
            )
            assert receipt["run_id"] == "r1-e2-01-8192-1-truncate"
            assert (
                receipt["request_sha256"]
                == "43ad0d59720bd3d7016413e5f1be30b3746f255579da7a3cfca2f1a355c0cd28"
            )
            assert receipt["provider_usage"] is None and "http_status" not in receipt
            ((trial,),) = db.execute("select row from trials").fetchall()
            meta = dict(db.execute("select name,value from meta").fetchall())
            assert meta == {"halt": "UNCONFIRMED_ConnectError", "permit": permit_hash}
            db.execute(
                "insert into reconciliation values(?,?,?,?,?,0,?)",
                (REQUEST, entry, trial, json.dumps(meta), "CONFIRMED_NOT_SENT", new_pin),
            )
            # Keep the attempt and immutable raw entry, apply only the adjudicated charge.
            db.execute(
                "update calls set charge=0 where run_id=? and ordinal=1", (receipt["run_id"],)
            )
            db.execute("delete from meta where name='halt'")
            db.execute("update meta set value=? where name='permit'", (new_pin,))
    ledger.bind(new_pin)


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--workers", type=int, default=10, choices=range(1, 33))
    args = p.parse_args()
    repair = json.loads(REPAIR.read_bytes())
    for name, digest in repair["files"].items():
        if pin(HERE / name) != digest:
            raise ValueError("Repair identity mismatch")
    if pin(HERE / "manifest.json") != PRIOR:
        raise ValueError("Original freeze mismatch")
    auth = LOCAL / "authorization.json"
    permit = json.loads(auth.read_bytes())
    if (
        permit["freeze"] != PRIOR
        or pin(auth) != repair["authorization_sha256"]
        or permit["output"] != str(OUT.resolve())
        or permit["data"] != "synthetic-only"
        or permit["max_calls"] != MAX_CALLS
        or permit["max_usd_micros"] != BUDGET
    ):
        raise ValueError("Original shared permit mismatch")
    expires = datetime.fromisoformat(permit["expires"])
    if datetime.now(UTC) >= expires:
        raise ValueError("Permit expired")
    cases = verify()
    if pin(HERE / "raw.jsonl") != repair["offline_sha256"]:
        raise ValueError("Offline results no longer match")
    key = os.environ["DEEPSEEK_API_KEY"]
    if not key:
        raise ValueError("No credential")
    with (
        (OUT / "lock").open("a") as lock,
        httpx.Client(timeout=httpx.Timeout(180, connect=20), follow_redirects=False) as client,
    ):
        # Default HTTPTransport retries=0. Environment proxy and TLS trust apply.
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        ledger = Ledger(OUT)
        bind(ledger, pin(auth), pin(REPAIR))

        def block(index):
            case = load_case(index)
            for trial in schedule(case.case_id, case.scale):
                if ledger.halted():
                    return
                trial["run_id"] = "route2-" + trial["run_id"]
                if ledger.completed(trial["run_id"]):
                    continue
                row = run_trial(ledger, client, key, case, trial, expires)
                ledger.finish({**row, "split": index["split"]})

        block(cases[0])
        with ThreadPoolExecutor(max_workers=args.workers) as ex:
            for _ in ex.map(block, cases[1:]):
                pass
        print(json.dumps({"halt": ledger.halted(), "ledger": str(ledger.path)}))


if __name__ == "__main__":
    main()
