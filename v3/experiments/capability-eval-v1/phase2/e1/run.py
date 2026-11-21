"""R1 frozen, label-blind retrieval and separate grading entry points."""

import argparse
import hashlib
import json
import os
import platform
import subprocess
from collections import Counter
from datetime import UTC, datetime
from importlib.metadata import version
from pathlib import Path
from tempfile import TemporaryDirectory
from time import perf_counter
from typing import Literal

import psycopg
from pydantic import SecretStr

from mgar_v3.domain import Model
from mgar_v3.evidence.artifacts import ArtifactStore
from mgar_v3.memory.retrieval import MemoryRetriever
from mgar_v3.quality_eval.e1_protocol import (
    CorpusRecord,
    Key,
    Note,
    Scope,
    candidate_fingerprint,
    encode,
    jsonl,
    pin,
)
from mgar_v3.quality_eval.e1_retrieval import PairwiseMemoryReranker
from mgar_v3.quality_eval.e1_run import ExperimentEmbedding, seed_corpus
from mgar_v3.quality_eval.e5_pg import isolated_server
from mgar_v3.rag.embedding import SpacyEmbedding
from mgar_v3.rag.rank import PairwiseRanker
from mgar_v3.settings import Settings
from mgar_v3.store.migrate import migrate

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[3]
MODEL = ROOT / ".local/models/zh_core_web_md-3.8.0"
WEIGHTS = ROOT / "evidence/E1/preparation-20261120/pairwise-ranker.json"
FILES = ("base-corpus.jsonl", "development-overlay.jsonl", "independent-overlay.jsonl")
IMAGE = "sha256:33198da2828a14c30348d2ccb4750833d5ed9a44c88d840a0e523d7417120337"
ADAPTER = "pretrained_spacy_mean300_plus_locally_trained_pairwise"


class Query(Model):
    query_id: Key
    cohort: Key
    query: Note
    category: Literal[
        "exact_symbol",
        "semantic_paraphrase",
        "time_validity",
        "commit_version",
        "conflict",
        "no_answer",
        "acl_negative",
    ]
    scope: Scope


def dump(path, value):
    with path.open("x", encoding="utf8") as out:
        json.dump(value, out, indent=2, ensure_ascii=False, allow_nan=False)
        out.write("\n")


def bindings():
    if os.environ.get("PYTHONHASHSEED") != "0":
        raise ValueError("Set PYTHONHASHSEED=0 to fix BM25 floating point tie order")
    seal = json.loads((HERE / "holdout-commitment.json").read_bytes())
    return {
        "base_commit": subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True
        ).strip(),
        "legacy_transitive_source_sha256": candidate_fingerprint(),
        "candidate_sources": {
            str(p.relative_to(ROOT)): pin(p)
            for p in [
                HERE / "run.py",
                HERE / "grade.py",
                HERE / "prepare.py",
                ROOT / "src/mgar_v3/quality_eval/e5_pg.py",
                ROOT / "src/mgar_v3/memory/calibrated.py",
                ROOT / "src/mgar_v3/memory/retrieval.py",
            ]
        },
        "corpus": {k: pin(HERE / k) for k in FILES},
        "corpus_concat_sha256": hashlib.sha256(
            b"".join((HERE / k).read_bytes() for k in FILES)
        ).hexdigest(),
        "split": {
            "development": {
                "queries": pin(HERE / "development.queries.jsonl"),
                "labels": pin(HERE / "development.labels.jsonl"),
                "count": 315,
            },
            "holdout": {
                "queries": seal["queries_sha256"],
                "labels": seal["labels_sha256"],
                "count": seal["query_count"],
            },
        },
        "development_label_review_sha256": pin(HERE / "development-label-review.json"),
        "holdout_commitment_sha256": pin(HERE / "holdout-commitment.json"),
        "models": {
            "embedding_receipt": pin(MODEL / "mgar-model-receipt.json"),
            "weights": pin(WEIGHTS),
            "lock": pin(ROOT / "config/rag-model-lock.json"),
        },
        "execution": {
            "postgres_image": IMAGE,
            "python_hash_seed": 0,
            "python": platform.python_version(),
            **{k: version(k) for k in ("spacy", "spacy-pkuseg", "jieba", "numpy", "psycopg")},
        },
        "gate": {
            "recall_at_5_min_delta": 0.05,
            "mrr_at_10_paired_ci_low_gt": 0,
            "no_answer_fpr_max": 0.10,
            "answerable_coverage_min": 0.90,
            "acl_leakage_max": 0,
        },
        "budget": {
            "top_k": 10,
            "max_scoped_records": 64,
            "bootstrap_replicates": 10000,
            "seed": 20261120,
            "paid_calls": 0,
        },
    }


def read_corpus():
    records = []
    for name in FILES:
        for line in (HERE / name).read_bytes().splitlines():
            obj = json.loads(line)
            # The schema stores public excerpts with evidence and synthetic controls without
            # evidence. Keep auxiliary control receipt IDs in the frozen raw input only.
            if obj["provenance"]["kind"] == "synthetic_control":
                obj["provenance"].pop("source_id", None)
            records.append(CorpusRecord.model_validate(obj))
    if len(records) != len({r.memory_id for r in records}):
        raise ValueError("Duplicate corpus ID")
    return records


def check(args):
    path = HERE / "e1-r1-manifest.json"
    if pin(path) != args.freeze:
        raise ValueError("Manifest commitment mismatch")
    freeze = json.loads(path.read_bytes())
    if bindings() != freeze["bindings"]:
        raise ValueError("Source, input or environment changed after freeze")
    spec = freeze["bindings"]["split"][args.split]
    if pin(args.queries) != spec["queries"]:
        raise ValueError("Query bytes differ from freeze")
    if args.split == "holdout" and any(
        p.resolve().is_relative_to(ROOT.parent)
        for p in [args.queries, args.out] + ([args.labels] if args.cmd == "score" else [])
    ):
        raise ValueError("Holdout raw and labels must remain outside repository")
    qs = jsonl(args.queries.read_bytes(), Query)
    if len(qs) != spec["count"] or len(qs) != len({q.query_id for q in qs}):
        raise ValueError("Query count/identity differs from commitment")
    return spec, qs


def run(args, queries):
    corpus = read_corpus()
    args.out.mkdir(mode=0o700, parents=True, exist_ok=False)
    model = ExperimentEmbedding(SpacyEmbedding(MODEL))
    reranker = PairwiseMemoryReranker(model, PairwiseRanker.load(WEIGHTS, model))
    seen = set()
    start = perf_counter()
    with isolated_server(IMAGE) as server, TemporaryDirectory() as cas:
        dsn = server.dsn()
        migrate(dsn)
        seed_corpus(dsn, corpus, ArtifactStore(Path(cas)))
        with psycopg.connect(dsn) as conn:
            pg_version = conn.execute("SELECT version()").fetchone()[0]
        with (
            (args.out / "raw.jsonl").open("xb") as raw,
            (args.out / "scope-filters.jsonl").open("xb") as acl,
        ):
            for q in queries:
                begin = perf_counter()
                scope = q.scope
                settings = Settings(
                    _env_file=None,
                    database_url=SecretStr(dsn),
                    project_id=scope.project_id,
                    repository=scope.repository,
                    local_actor=scope.actor,
                    allowed_actors=[scope.actor],
                    allow_paid=False,
                    local_token=SecretStr("isolated-evaluation-local-session"),
                    allow_external_write=False,
                )
                retriever = MemoryRetriever(
                    settings, embedder=model, reranker=reranker, now=lambda scope=scope: scope.as_of
                )
                records = retriever._load(
                    scope=None, commit=scope.commit, memory_kinds=None, actor=scope.actor
                )
                ids = [str(r.memory_id) for r in records]
                pool = set(ids)
                roles = {}
                for r in corpus:
                    reason = next(
                        (
                            k
                            for k in ("project_id", "repository", "actor")
                            if getattr(scope, k) != getattr(r.draft, k)
                        ),
                        None,
                    )
                    if reason:
                        roles[str(r.memory_id)] = f"different_{reason}"
                    elif str(r.memory_id) not in pool:
                        roles[str(r.memory_id)] = "authorized_not_loaded"
                key = hashlib.sha256(
                    encode([scope.project_id, scope.repository, scope.actor, ids])
                ).hexdigest()
                if key not in seen:
                    acl.write(
                        encode(
                            {"scope_key": key, "candidate_ids": ids, "filter_reason_by_id": roles}
                        )
                    )
                    seen.add(key)
                if any(k in roles for k in ids):
                    raise ValueError("Candidate ACL violation; stop immediately")
                rank, hits = retriever._rank_calibrated(q.query, records, commit=scope.commit)
                offsets = {k: i for i, k in enumerate(ids)}
                raw.write(
                    encode(
                        {
                            "query_id": q.query_id,
                            "cohort": q.cohort,
                            "category": q.category,
                            "scope_key": key,
                            "filters": dict(Counter(roles.values())),
                            "candidate_ids": ids,
                            "versions": [hits[k].version_status for k in ids],
                            "rankings": {
                                a: [[offsets[k], round(v, 10)] for k, v in items]
                                for a, items in rank.rankings.items()
                            },
                            "pairwise_raw": [rank.pairwise.get(k) for k in ids],
                            "absolute_support": [rank.support.get(k) for k in ids],
                            "abstention_reason": rank.abstention_reason,
                            "stage_ms": rank.elapsed_ms,
                            "elapsed_ms": 1000 * (perf_counter() - begin),
                        }
                    )
                )
    dump(
        args.out / "run.json",
        {
            "freeze_sha256": args.freeze,
            "queries_sha256": pin(args.queries),
            "split": args.split,
            "raw_sha256": pin(args.out / "raw.jsonl"),
            "filter_sha256": pin(args.out / "scope-filters.jsonl"),
            "count": len(queries),
            "postgres": pg_version,
            "embedding": model.model_id,
            "reranker": reranker.model_id,
            "model_mode": ADAPTER,
            "paid_requests": 0,
            "provider_tokens": 0,
            "cost": 0,
            "raw_ranking_format": "[candidate index, score], full ranking. Evaluate first 10.",
            "elapsed_seconds_including_database": perf_counter() - start,
        },
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("freeze")
    for name in ("rank", "score"):
        p = sub.add_parser(name)
        p.add_argument("--split", choices=("development", "holdout"), required=True)
        p.add_argument("--queries", type=Path, required=True)
        p.add_argument("--out", type=Path, required=True)
        p.add_argument("--freeze", required=True)
        if name == "score":
            p.add_argument("--labels", type=Path, required=True)
    args = parser.parse_args()
    if args.cmd == "freeze":
        dump(
            HERE / "e1-r1-manifest.json",
            {
                "schema": "e1-r1-1",
                "frozen_at": datetime.now(UTC).isoformat(),
                "bindings": bindings(),
            },
        )
        print(pin(HERE / "e1-r1-manifest.json"))
        return
    spec, qs = check(args)
    if args.cmd == "rank":
        run(args, qs)
    else:
        from grade import grade

        if pin(args.labels) != spec["labels"]:
            raise ValueError("Label commitment mismatch")
        grade(args, qs, read_corpus())


if __name__ == "__main__":
    main()
