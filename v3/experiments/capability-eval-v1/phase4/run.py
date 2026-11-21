"""Reproduce E5 using the original ranker, request generator and measurement tools."""

from __future__ import annotations

import argparse
import json
import os
import platform
import random
from contextlib import contextmanager
from dataclasses import asdict
from pathlib import Path
from time import perf_counter
from unittest.mock import patch

import psycopg
from candidate import CLAUSES, INDEXES, STALE, CandidateStore, recall_sql

import mgar_v3.memory.retrieval as retrieval
from mgar_v3.memory.store import MemoryStore
from mgar_v3.quality_eval.e1_protocol import encode, pin
from mgar_v3.quality_eval.e2_live import write_record
from mgar_v3.quality_eval.e5_data import SEED_SQL, bank, request_order, seed, settings, version
from mgar_v3.quality_eval.e5_measure import Workload, instrumentation
from mgar_v3.quality_eval.e5_metrics import raw, summarize
from mgar_v3.quality_eval.e5_pg import (
    IMAGE,
    PG_OPTIONS,
    Resources,
    docker,
    isolated_server,
    pg_counters,
    prepare_metrics,
    resident_blocks,
)

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
MODEL = ROOT / ".local/models/zh_core_web_md-3.8.0"
WEIGHTS = ROOT / "evidence/E1/preparation-20261120/pairwise-ranker.json"
ARMS = ("baseline", "index", "topn", "full")
COUNTS = (1_000, 10_000, 100_000, 500_000)
CONCURRENCY = (1, 5, 10, 25, 50)
SEED = 418144
CELL_REQUESTS = 192
REPEAT_REQUESTS = 512
CLOCKED = "rrf"
SOURCE = "088b087"


def bindings():
    src = ROOT / "src/mgar_v3"
    files = [
        HERE / "run.py",
        HERE / "candidate.py",
        HERE / "protocol.md",
        SEED_SQL,
        ROOT / "uv.lock",
        MODEL / "mgar-model-receipt.json",
        WEIGHTS,
        ROOT / "config/rag-model-lock.json",
        *src.joinpath("memory").glob("*.py"),
        *src.joinpath("quality_eval").glob("e5_*.py"),
        *src.joinpath("rag").glob("*.py"),
        *ROOT.joinpath("migrations").glob("*.sql"),
    ]
    return {
        "product_commit": SOURCE,
        "files": {str(p.relative_to(ROOT)): pin(p) for p in sorted(files) if p.is_file()},
        "dataset_sizes": COUNTS,
        "arms": ARMS,
        "workers": CONCURRENCY,
        "cache_states": ["cold_pg", "hot_pg"],
        "requests_per_cell": CELL_REQUESTS,
        "priority_requests_per_repeat": REPEAT_REQUESTS,
        "repeats": 3,
        "random_seed": SEED,
        "algorithm": CLOCKED,
        "provider_model_calls": 0,
        "docker_image": docker("image", "inspect", IMAGE, "--format", "{{.Id}}"),
        "queries": [asdict(q) for q in bank()],
        "pg_settings": dict(PG_OPTIONS) | {"shared_preload_libraries": "pg_stat_statements"},
        "python": platform.python_version(),
    }


@contextmanager
def arm_scope(arm):
    def store(conf):
        if arm in ("baseline", "index"):
            return MemoryStore(conf)
        return CandidateStore(conf, arm)

    with patch.object(retrieval, "MemoryStore", store):
        yield


def configure(dsn, add):
    with psycopg.connect(dsn, autocommit=True) as conn:
        if add:
            conn.execute(INDEXES, prepare=False)
        conn.execute("CREATE EXTENSION IF NOT EXISTS pg_stat_statements")
        conn.execute("ANALYZE")
        conn.execute("CHECKPOINT")


def query_details(dsn, arm, out):
    with psycopg.connect(dsn) as conn:
        for label, suffix, extra in (
            ("broad", [], []),
            ("commit", ["m.scope_kind=%s", "m.scope_key=%s"], ["commit", version(0)]),
        ):
            params = ["e5-project-0", "example/e5-repo-0", "e5-owner-0", *extra, 64]
            q = recall_sql([*CLAUSES, STALE, *suffix], arm)
            cursor = conn.execute("EXPLAIN (ANALYZE,BUFFERS,FORMAT JSON) " + q, params)
            (out / f"plan-{arm}-{label}.json").write_bytes(encode(cursor.fetchone()[0]))


def query_counter(dsn):
    with psycopg.connect(dsn) as conn:
        rows = conn.execute(
            "SELECT query, calls, round(total_exec_time::numeric,3)::float, rows,shared_blks_hit,"
            "shared_blks_read, temp_blks_read, temp_blks_written "
            "FROM pg_stat_statements WHERE dbid=(SELECT oid FROM pg_database "
            "WHERE datname=current_database()) AND query ILIKE %s "
            "ORDER BY total_exec_time DESC, query LIMIT 30",
            ("%knowledge.memory_%",),
        ).fetchall()
    keys = (
        "query",
        "calls",
        "server_exec_ms",
        "rows",
        "shared_hits",
        "shared_reads",
        "temp_reads",
        "temp_writes",
    )
    return [dict(zip(keys, r, strict=True)) for r in rows]


def cell(worker, server, dbname, arm, size, concurrency, cache, trial, requests, embedding, out):
    conninfo = server.dsn(dbname)
    plan = request_order(bank(), requests)
    if cache == "hot_pg":
        warm, _ = worker.drive(
            conninfo, CLOCKED, request_order(bank(), 64), concurrency, embedding, pooled=True
        )
        if any(r["status"] != "PASS" for r in warm):
            raise ValueError("Failed warm-up")
    blocks = resident_blocks(conninfo)
    valid = blocks == 0 if cache == "cold_pg" else blocks > 0
    if not valid:
        raise ValueError(f"Invalid cache state {cache} ({blocks} pages)")
    pre = pg_counters(conninfo)
    resource = Resources(server.cgroup())
    start = perf_counter()
    with resource.observe():
        records, wall = worker.drive(conninfo, CLOCKED, plan, concurrency, embedding, pooled=True)
    after = pg_counters(conninfo)
    result = summarize(
        records,
        wall,
        pre,
        after,
        blocks,
        resource.cpu_usage_us,
        resource.memory,
        perf_counter() - start,
    )
    link = raw(out / f"{size}-{arm}-{concurrency}-{cache}-{trial}.jsonl.gz", records)
    entry = dict(
        n=size,
        arm=arm,
        workers=concurrency,
        cache=cache,
        repeat=trial,
        requests=requests,
        evidence=link,
        **{k: v for k, v in result.items() if k != "requests"},
    )
    write_record(out / "cells.jsonl", entry)
    if result["status_counts"] != {"PASS": requests}:
        raise ValueError(f"Stop on incomplete retrieval or ACL: {result['error_counts']}")
    print(
        size,
        arm,
        concurrency,
        cache,
        trial,
        round(result["verified_qps"], 1),
        round(result["latency"]["p99_ms"], 1),
        flush=True,
    )


def model_probe(dsn):
    result = []
    for q in bank()[::8]:
        for kind in (None, ["working"], ["procedural", "semantic"]):
            for stale in (True, False):
                args = dict(scope=q.scope, commit=q.commit, memory_kinds=kind, include_stale=stale)
                conf = settings(dsn, q.cohort)
                baseline = MemoryStore(conf).recall(**args)
                a = CandidateStore(conf, "topn").recall(**args)
                b = CandidateStore(conf, "full").recall(**args)
                if baseline != a or baseline != b:
                    raise ValueError("Complete candidate record identities or details changed")
                result.append(
                    {"query": q.query_id, "stale": stale, "kinds": kind, "count": len(baseline)}
                )
    return result


def baseline_profiles(out):
    link = bindings()
    with isolated_server(link["docker_image"]) as server, instrumentation():
        server.create("profile")
        dsn = server.dsn("profile")
        (out / "dataset.json").write_bytes(encode(seed(dsn, 100_000, out)))
        settings_info = prepare_metrics(dsn)
        (out / "postgres.json").write_bytes(encode(settings_info))
        configure(dsn, False)
        query_details(dsn, "baseline", out)
        work = Workload(MODEL, WEIGHTS)
        em = work.embedding(True)
        with arm_scope("baseline"):
            cell(work, server, "profile", "baseline", 100_000, 5, "hot_pg", 0, 192, em, out)
        (out / "baseline-statements.json").write_bytes(encode(query_counter(dsn)))
        configure(dsn, True)
        for a in ARMS[1:]:
            query_details(dsn, a, out)
        (out / "tuple-parity.json").write_bytes(encode(model_probe(dsn)))


def run(out):
    f = HERE / "manifest.json"
    lock = json.loads(f.read_text())
    if encode(bindings()) != encode(lock):
        raise ValueError("Phase 4 freeze differs")
    work = Workload(MODEL, WEIGHTS)
    with isolated_server(lock["docker_image"]) as server, instrumentation():
        for size in (100_000, 1_000, 10_000, 500_000):
            databases = {}
            for variant in ("base", "indexed"):
                name = f"e5_{size}_{variant}"
                server.create(name)
                conn = server.dsn(name)
                info = seed(conn, size, out)
                p = prepare_metrics(conn)
                configure(conn, variant == "indexed")
                databases[variant] = name
                (out / f"dataset-{size}-{variant}.json").write_bytes(encode(info | p))
                if size == 100_000 and variant == "indexed":
                    (out / "tuple-parity.json").write_bytes(encode(model_probe(conn)))
            embedding = work.embedding(True)
            dsn = server.dsn(databases["base"])
            with arm_scope("baseline"):
                work.drive(dsn, CLOCKED, bank(), 1, embedding, pooled=True)
            jobs = [(a, w, 0) for a in ARMS for w in CONCURRENCY]
            random.Random(SEED + size).shuffle(jobs)
            if size == 100_000:
                priority = [
                    (a, w, r) for a in ("baseline", "full") for w in (5, 50) for r in (1, 2, 3)
                ]
                random.Random(SEED).shuffle(priority)
                jobs = priority + jobs
            for arm, w, trial in jobs:
                db = databases["base" if arm == "baseline" else "indexed"]
                with arm_scope(arm):
                    if trial == 0:
                        server.restart()
                        cell(
                            work,
                            server,
                            db,
                            arm,
                            size,
                            w,
                            "cold_pg",
                            trial,
                            CELL_REQUESTS,
                            embedding,
                            out,
                        )
                    cell(
                        work,
                        server,
                        db,
                        arm,
                        size,
                        w,
                        "hot_pg",
                        trial,
                        REPEAT_REQUESTS if trial else CELL_REQUESTS,
                        embedding,
                        out,
                    )
            if size == 100_000:
                for key, dbname in databases.items():
                    (out / f"statements-{key}.json").write_bytes(
                        encode(query_counter(server.dsn(dbname)))
                    )
    if encode(bindings()) != encode(lock):
        raise ValueError("Frozen source changed during run")


def main():
    p = argparse.ArgumentParser()
    p.add_argument("mode", choices=("freeze", "profile", "formal"))
    p.add_argument("--out", type=Path)
    a = p.parse_args()
    if os.environ.get("PYTHONHASHSEED") != "0":
        raise ValueError("Set PYTHONHASHSEED=0 for stable lexical ties")
    PG_OPTIONS["shared_preload_libraries"] = "pg_stat_statements"
    if a.mode == "freeze":
        (HERE / "manifest.json").write_bytes(encode(bindings()))
        (HERE / "cases.jsonl").write_bytes(b"".join(encode(asdict(q)) for q in bank()))
        return
    a.out.mkdir(parents=True, exist_ok=False)
    if a.mode == "profile":
        baseline_profiles(a.out)
    else:
        run(a.out)


if __name__ == "__main__":
    main()
