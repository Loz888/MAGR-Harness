"""Run frozen synthetic E3-R1 chains in ephemeral PG with persistent CAS/receipts."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import random
import subprocess
import threading
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from time import perf_counter
from uuid import uuid4

import httpx
from dotenv import dotenv_values
from provider import Ledger, Stopped
from pydantic import SecretStr, ValidationError

import mgar_v3
from mgar_v3.evidence.artifacts import ArtifactStore
from mgar_v3.memory.retrieval import MemoryRetriever
from mgar_v3.quality_eval.e1_protocol import encode
from mgar_v3.quality_eval.e1_retrieval import PairwiseMemoryReranker
from mgar_v3.quality_eval.e1_run import ExperimentEmbedding, isolated_database
from mgar_v3.quality_eval.e3_protocol import (
    ARMS,
    DECISIONS,
    Case,
    Inspect,
    Submit,
    check,
    expected,
    snapshot,
)
from mgar_v3.quality_eval.e3_run import Environment
from mgar_v3.rag.embedding import SpacyEmbedding
from mgar_v3.rag.rank import PairwiseRanker
from mgar_v3.settings import Settings
from mgar_v3.store.migrate import migrate

BASE = Path(__file__).resolve().parent
V3 = BASE.parents[3]
OUT = V3 / "evidence" / "CAPABILITY-PHASE2" / "e3"
MODEL = V3 / ".local/models/zh_core_web_md-3.8.0"
WEIGHTS = V3 / "evidence/E1/preparation-20261120/pairwise-ranker.json"


def rows(path):
    return [json.loads(line) for line in path.read_text().splitlines()]


def check_freeze():
    manifest = BASE / "manifest.json"
    spec = json.loads(manifest.read_text())
    runtime_root = Path(mgar_v3.__file__).resolve().parents[3]
    head = subprocess.check_output(
        ["git", "-C", runtime_root, "rev-parse", "HEAD"], text=True
    ).strip()
    if head != spec["runtime_commit"] or subprocess.check_output(
        [
            "git",
            "-C",
            runtime_root,
            "status",
            "--porcelain",
            "--",
            "v3/src",
            "v3/migrations",
            "v3/config",
        ]
    ):
        raise Stopped("RUNTIME_FREEZE_MISMATCH")
    for name, digest in spec["files"].items():
        path = V3 / name
        if hashlib.sha256(path.read_bytes()).hexdigest() != digest:
            raise Stopped("SOURCE_OR_DATA_CHANGED " + name)
    return hashlib.sha256(manifest.read_bytes()).hexdigest()


def grouped_trials():
    tasks = []
    seen = set()
    for split, name, count in [
        ("development", "development.jsonl", 30),
        ("holdout", "e3-holdout.jsonl", 10),
    ]:
        cases = rows(BASE / name)
        assert len(cases) == count
        for raw in cases:
            assert (
                len(raw["releases"]) in (3, 4, 5)
                and raw["releases"][:2] == [1, 1]
                and sorted(set(raw["releases"])) in ([1], [1, 2])
                and sorted(raw["releases"]) == raw["releases"]
            )
            assert raw["case_id"] not in seen
            seen.add(raw["case_id"])
            tasks.extend((split, raw, arm) for arm in ARMS)
    random.Random(371911).shuffle(tasks)
    return tasks


def one_session(env, case, seed, arm, session, release, split, history, ledger):
    t0 = perf_counter()
    current = snapshot(case, seed, 1 if release == 1 else 3)
    run = env.start(session, case.question)
    recall = env.recall(case, current, session)
    advisory = recall.advisory
    valid_memory = bool(advisory and not advisory.requires_reverification)
    allowed = {advisory.submission.proof} if valid_memory else set()
    prior_errors, prior_inspected = history[current.version]
    observed = set()
    evidence = []
    actions = []
    uses = []
    success = None
    reason = None
    submit_count = 0
    first_pass = False
    stale_attempts = 0
    repeat_tools = 0
    for ordinal in range(1, 4):
        if ledger is None:
            decision = (
                advisory.submission
                if valid_memory
                else Submit(
                    answer=expected(case, current), proof=current.proof, version=current.version
                )
                if evidence
                else Inspect()
            )
        else:
            payload = {
                "task": case.question,
                "source": case.source,
                "current_version": current.version,
                "allowed_actions": ["submit"] if evidence else ["inspect_current", "submit"],
                "verified_memory": advisory.model_dump(mode="json") if advisory else None,
                "inspection": {"snapshot": current.model_dump(mode="json"), "proof": current.proof}
                if evidence
                else None,
                "previous_error": reason,
            }
            trial = f"{split}/{case.case_id}/{arm}/{session}/{ordinal}"
            response, usage = ledger.complete(trial, payload)
            uses.append(usage)
            try:
                decision = DECISIONS.validate_json(response)
            except ValidationError:
                reason = "INVALID_MODEL_REPLY"
                observed.add(reason)
                actions.append({"action": "invalid", "error": reason})
                continue
        if isinstance(decision, Inspect):
            if evidence:
                reason = "CURRENT_RECEIPT_ALREADY_AVAILABLE"
                observed.add(reason)
                actions.append(
                    {
                        "action": "repeat_inspection_rejected",
                        "proof": current.proof,
                        "error": reason,
                    }
                )
                repeat_tools += 1
            else:
                proof_event = env.inspect(run, current)
                allowed.add(current.proof)
                evidence.append(proof_event)
                repeat_tools += int(prior_inspected)
                actions.append(
                    {
                        "action": "inspect_current",
                        "proof": current.proof,
                        "evidence_id": str(proof_event),
                    }
                )
                reason = None
            continue
        submit_count += 1
        reason = check(case, current, decision, allowed)
        actions.append({**decision.model_dump(mode="json"), "error": reason})
        if reason is None:
            success = decision
            first_pass = submit_count == 1 and not observed
            break
        observed.add(reason)
        if (
            advisory
            and advisory.requires_reverification
            and (
                decision.version == advisory.submission.version
                or decision.proof == advisory.submission.proof
            )
        ):
            stale_attempts += 1
    passed = success is not None
    reason = None if passed else reason or "NO_VERIFIED_SUBMISSION"
    if not passed:
        observed.add(reason)
    delivery_sha = None
    if passed:
        delivery = encode({"source": case.source, "submission": success.model_dump(mode="json")})
        delivery_sha = env.artifacts.put(delivery)
        assert env.artifacts.read(delivery_sha) == delivery
    env.finish(run, passed, reason)
    if session == 1 and passed:
        env.learn(run, case, success, evidence)
    n_known = len(prior_errors)
    n_repeated = len(observed & prior_errors)
    history[current.version] = (observed | prior_errors, prior_inspected or bool(evidence))
    return dict(
        case_id=case.case_id,
        domain=case.domain,
        split=split,
        seed=seed,
        arm=arm,
        session=session,
        run_id=str(run.run_id),
        actions=actions,
        reason=reason,
        release=release,
        version=current.version,
        retrieval_mode=recall.mode,
        degraded_reason=recall.degraded,
        final_success=passed,
        first_pass_success=first_pass,
        delivery_sha256=delivery_sha,
        memory_id=str(recall.memory_id) if recall.memory_id else None,
        created_memory_id=str(env.memory_id) if session == 1 and env.memory_id else None,
        historical_memory=bool(advisory and advisory.requires_reverification),
        retrieval_hit=bool(advisory),
        memory_adopted=bool(passed and valid_memory and not evidence),
        stale_attempts=stale_attempts,
        stale_accepted=int(passed and success.version != current.version),
        known_error_opportunities=n_known,
        repeated_errors=n_repeated,
        repeat_tool_calls=repeat_tools,
        tool_calls=len(evidence),
        model_calls=len(uses),
        input_tokens=sum(u["input"] for u in uses),
        output_tokens=sum(u["output"] for u in uses),
        cost_micros=sum(u["cost"] for u in uses),
        duration_ms=round((perf_counter() - t0) * 1000, 3),
    )


class SafeEmbedding(ExperimentEmbedding):
    def __init__(self, model):
        super().__init__(model)
        self.lock = threading.Lock()

    def embed(self, texts):
        with self.lock:
            return super().embed(texts)


def run_all(dsn, work, ledger, workers):
    raw_file = work / "raw.jsonl"
    repository_lock = threading.Lock()
    stop = threading.Event()
    tasks = grouped_trials()
    model = SpacyEmbedding(MODEL)
    embedder = SafeEmbedding(model)
    reranker = PairwiseMemoryReranker(embedder, PairwiseRanker.load(WEIGHTS, model))
    with isolated_database(dsn) as temporary:
        migrate(temporary)

        def chain(task):
            split, raw, arm = task
            if stop.is_set():
                return
            case = Case(
                **{k: raw[k] for k in ("case_id", "domain", "label", "source", "operation")}
            )
            project = f"{case.case_id}-{arm}"
            cas = ArtifactStore(work / "cas" / project)
            settings = Settings(
                _env_file=None,
                database_url=SecretStr(temporary),
                local_token=SecretStr("ephemeral-e3-evaluation-reader-with-isolated-pg"),
                artifact_root=cas.root,
                project_id=project,
                repository=f"synthetic-experiment/{case.case_id}",
                local_actor="e3-reader",
                allowed_actors=["e3-reader"],
                allow_paid=False,
                allow_external_write=False,
            )
            retriever = (
                None
                if arm == "off"
                else MemoryRetriever(
                    settings,
                    embedder=embedder if arm.startswith("hybrid") else None,
                    reranker=reranker if arm == "hybrid_reranked" else None,
                )
            )
            env = Environment(settings, cas, retriever)
            history = defaultdict(lambda: (set(), False))
            try:
                for n, release in enumerate(raw["releases"], 1):
                    if stop.is_set():
                        break
                    entry = one_session(
                        env, case, raw["seed"], arm, n, release, split, history, ledger
                    )
                    with repository_lock, raw_file.open("ab") as file:
                        file.write(encode(entry))
                        file.flush()
                        os.fsync(file.fileno())
            except BaseException:
                stop.set()
                raise

        chain(tasks[0])
        with ThreadPoolExecutor(max_workers=workers) as pool:
            list(pool.map(chain, tasks[1:]))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=("fixture", "formal"))
    parser.add_argument("--workers", type=int, default=8, choices=range(1, 17))
    args = parser.parse_args()
    freeze = check_freeze()
    settings = dotenv_values(Path.home() / "magr-harness/v3/.env")
    admin = os.environ.get("MGAR_E3_ADMIN_DATABASE_URL") or settings.get("MGAR_V3_DATABASE_URL")
    if not admin:
        raise Stopped("ADMIN_DATABASE_UNAVAILABLE")
    store = OUT / args.mode
    store.mkdir(exist_ok=True, parents=True)
    with httpx.Client(timeout=httpx.Timeout(180, connect=15.0), follow_redirects=False) as client:
        ledger = None
        if args.mode == "formal":
            receipt = json.loads((OUT / "fixture" / "acceptance.json").read_text())
            if receipt != {"status": "PASS", "sessions": 640, "freeze": freeze}:
                raise Stopped("FIXTURE_GATE")
            key = os.environ.get("DEEPSEEK_API_KEY") or settings.get("MGAR_V3_DEEPSEEK_API_KEY")
            if not key:
                raise Stopped("CREDENTIAL_UNAVAILABLE")
            ledger = Ledger(store / "ledger.sqlite", freeze, key, client)
        work = store / str(uuid4())
        work.mkdir()
        try:
            if ledger and ledger.halted:
                raise Stopped(ledger.halted)
            run_all(admin, work, ledger, args.workers)
            data = rows(work / "raw.jsonl")
            assert len(data) == len({(r["arm"], r["case_id"], r["session"]) for r in data}) == 640
            if args.mode == "fixture":
                assert all(r["final_success"] and not r["stale_accepted"] for r in data)
            (store / "latest.txt").write_text(work.name + "\n")
            (store / "acceptance.json").write_bytes(
                encode(
                    dict(
                        status="PASS" if args.mode == "fixture" else "EXECUTED",
                        sessions=len(data),
                        freeze=freeze,
                    )
                )
            )
        finally:
            if ledger:
                ledger.close()
        print(args.mode, len(data), work)


if __name__ == "__main__":
    main()
