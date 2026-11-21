"""Freeze E2-R1 inputs, rebuild randomized offline trials and preserve their traces."""

from __future__ import annotations

import argparse
import json
import multiprocessing
import os
import random
from functools import lru_cache
from pathlib import Path
from time import perf_counter

from mgar_v3.context.builder import ContextRequest, EvidenceRef, RemainingBudget
from mgar_v3.context.compactor import LineRange, Observation, PermissionSnapshot
from mgar_v3.evidence.artifacts import ArtifactStore
from mgar_v3.quality_eval.e2_context import (
    ARMS,
    MODEL,
    MODEL_RELEASE,
    ROOT,
    SCALES,
    SYSTEM,
    TOKENIZER_FILES,
    TOKENIZER_LIBRARY,
    TOKENIZER_RELEASE,
    TOOL,
    CaseInput,
    ReferenceTokenizer,
    build,
    encoded,
    full_bundle,
    messages,
    sha256,
    token_features,
)
from mgar_v3.quality_eval.e2_grader import byte_exact_artifacts, context_grade

HERE = Path(__file__).resolve().parent
LOCAL = ROOT / "evidence/CAPABILITY-PHASE2/e2"
SOURCE = "d4fee2f555c5e031720302e98ba164cf1bd8db74"
SEED = "E2-R1-20261120"
INPUT_CAP = 196608
CALLS = 4
READS = 3
PROMPT = SYSTEM.replace("one tool read", "three tool reads")
TOKENIZER = ROOT / ".local/memory-context-v1/tokenizer"
SOURCES = [
    "context/builder.py",
    "context/compactor.py",
    "evidence/artifacts.py",
    "quality_eval/e2_context.py",
    "quality_eval/e2_grader.py",
    "quality_eval/e2_live.py",
    "models/deepseek_policy.py",
]


def lines(path):
    return [json.loads(line) for line in path.read_bytes().splitlines()]


def dump(path, obj):
    with path.open("x") as stream:
        json.dump(obj, stream, ensure_ascii=False, indent=2, allow_nan=False)
        stream.write("\n")


def pin(path):
    return sha256(path.read_bytes())


def files():
    return (
        [
            HERE / s
            for s in (
                "protocol.md",
                "custody.jsonl",
                "make_dev.py",
                "development.jsonl",
                "matrix.py",
                "lifecycle.py",
                "analyze.py",
                "e2_independent_grader.py",
                "e2-holdout.jsonl",
            )
        ]
        + [ROOT / "src/mgar_v3" / s for s in SOURCES]
        + [ROOT / "uv.lock", HERE.parent.parent / "PLAN_V1.0.md"]
    )


@lru_cache
def tokenizer():
    return ReferenceTokenizer(TOKENIZER)


def filename(case_id, scale):
    return LOCAL / "frozen" / f"{case_id}-{scale}.json"


def schedule(case_id, scale):
    for rep in range(1, 4):
        arms = list(ARMS)
        random.Random(f"{SEED}/{case_id}/{scale}/{rep}").shuffle(arms)
        for order, arm in enumerate(arms, 1):
            yield dict(
                case_id=case_id,
                scale=scale,
                rep=rep,
                condition=arm,
                order=order,
                run_id=f"r1-{case_id}-{scale}-{rep}-{arm}",
            )


def observation(case_id, name, body, evidence, error=None):
    digest = sha256(body.encode())
    return Observation(
        tool="audit_check" if error else "read_log",
        reference=f"fixture://{case_id}/{name}",
        content=body,
        evidence_id=evidence,
        source_version=digest,
        artifact_hash=digest,
        locator=LineRange(start_line=1, end_line=max(1, len(body.splitlines()))),
        error_code=error,
    )


def case_at(facts, scale, width):
    case_id, incident = facts["case_id"], facts["incident"]
    seq = facts["sequence"]
    count = {8192: 13, 16384: 20, 32768: 29}[scale]
    pos = 2 + int(case_id[-2:]) * 5 % (count - 3)
    observations = []
    for i in range(count):
        rows = []
        for j in range(1, 110):
            line = f"{incident} station={i:02d} batch={j:03d} {seq[(i + j) % len(seq)]}\n"
            rows.append(line)
        body = "".join(rows)[:width]
        evidence = f"{case_id}-trace-{i}"
        error = None
        if i == pos:
            evidence = facts["evidence_id"]
            body = f"Signed diagnostic for {incident}.\n{body}\n{facts['diagnostic']}\n"
        if i == pos - 2:
            error = facts["failure_code"]
            body = f"Prior {incident} check failed: {error}.\n" + body
        observations.append(observation(case_id, str(i), body, evidence, error))
    receipt = observations[pos]
    request = ContextRequest(
        system_instructions=PROMPT,
        task=facts["task"],
        repository="synthetic/e2-r1",
        commit=SOURCE,
        constraints=[facts["constraint"]],
        current_plan=["Locate the signed result", "Reconcile the pinned handoff facts"],
        permissions=PermissionSnapshot(
            project_id="e2-r1",
            repository="synthetic/e2-r1",
            commit=SOURCE,
            actor="r1-reader",
            read_only=True,
            allowed_tools=[TOOL],
            budgets={"max_model_calls": CALLS, "max_tool_calls": READS},
        ),
        remaining_budget=RemainingBudget(
            model_calls=CALLS, tool_calls=READS, input_tokens=INPUT_CAP * CALLS
        ),
        observations=observations,
        evidence=[
            EvidenceRef(
                evidence_id=receipt.evidence_id,
                source_uri=receipt.reference,
                source_version=receipt.source_version,
                artifact_hash=receipt.artifact_hash,
            )
        ],
        failed_attempts=[f"Initial {incident} verification failed: {facts['failure_code']}."],
        pending_approvals=[facts["pending_approval"]],
        pending_actions=[f"Hold {incident} release"],
        open_questions=[facts["open_question"]],
        available_tools=[TOOL],
    )
    return CaseInput(case_id=case_id, scale=scale, request=request)


def calibrate(arg):
    facts, scale = arg
    tok = tokenizer()
    lo, hi = 1, 9000
    while lo <= hi:
        width = (lo + hi) // 2
        case = case_at(facts, scale, width)
        prompt = tok.render(messages(full_bundle(case.request)))
        n = tok.count(prompt)
        if abs(n - scale) <= scale * 0.02:
            break
        if n < scale:
            lo = width + 1
        else:
            hi = width - 1
    if abs(n - scale) > scale * 0.04:
        raise ValueError((case.case_id, scale, n))
    path = filename(case.case_id, scale)
    path.write_bytes(encoded(case) + b"\n")
    cas = ArtifactStore(LOCAL / "cas")
    for obs in case.request.observations:
        if cas.put(obs.content.encode()) != obs.artifact_hash:
            raise ValueError("CAS byte identity")
    return dict(
        case_id=case.case_id,
        scale=scale,
        split="development" if int(case.case_id[-2:]) <= 30 else "holdout",
        task=case.request.task,
        sha256=pin(path),
        full_input_reference=n,
        full_first_input_bound=len(prompt.encode()) + 4096,
        artifacts=len(case.request.observations),
    )


def freeze(workers):
    # Independent task definitions are copied verbatim from the custodian.
    tasks = lines(HERE / "development.jsonl") + lines(HERE / "e2-holdout.jsonl")
    if [r["case_id"] for r in tasks] != [f"e2-{i:02}" for i in range(1, 41)]:
        raise ValueError("Expected 30 development and 10 unique ordered holdout tasks.")
    (LOCAL / "frozen").mkdir(parents=True, exist_ok=False)
    with multiprocessing.Pool(workers) as pool:
        cases = pool.map(calibrate, [(f, s) for f in tasks for s in SCALES])
    with (HERE / "cases.jsonl").open("xb") as out:
        for case in cases:
            out.write(encoded(case) + b"\n")
    labels = [
        dict(
            case_id=f["case_id"],
            scale=s,
            expected={
                **{
                    k: f[k]
                    for k in (
                        "incident",
                        "finding_code",
                        "failure_code",
                        "evidence_id",
                        "constraint",
                        "pending_approval",
                        "open_question",
                    )
                },
                "commit": SOURCE,
                "allowed_tools": [TOOL],
                "remaining_tool_calls": READS,
            },
        )
        for f in tasks[:30]
        for s in SCALES
    ]
    (LOCAL / "development-labels.jsonl").write_bytes(b"".join(encoded(r) + b"\n" for r in labels))
    manifest = dict(
        source_commit=SOURCE,
        model=MODEL,
        provider="deepseek",
        release=MODEL_RELEASE,
        tokenizer=dict(files=TOKENIZER_FILES, package=TOKENIZER_LIBRARY, release=TOKENIZER_RELEASE),
        python=os.sys.version,
        database="none; local filesystem CAS",
        sandbox="local read-only evidence pagination",
        prompt_sha256=sha256(PROMPT.encode()),
        seed=SEED,
        cases_sha256=pin(HERE / "cases.jsonl"),
        dataset_sha256=sha256(encoded([r["sha256"] for r in cases])),
        sources={str(p.relative_to(ROOT)): pin(p) for p in files()},
        development_labels_sha256=pin(LOCAL / "development-labels.jsonl"),
        independent_commitment=lines(HERE / "custody.jsonl")[0],
    )
    dump(HERE / "manifest.json", manifest)


def verify():
    manifest = json.loads((HERE / "manifest.json").read_bytes())
    for name, digest in manifest["sources"].items():
        if pin(ROOT / name) != digest:
            raise ValueError(f"Source or input differs from freeze: {name}")
    if pin(HERE / "cases.jsonl") != manifest["cases_sha256"]:
        raise ValueError("Case index differs from freeze.")
    return lines(HERE / "cases.jsonl")


def load_case(index):
    path = filename(index["case_id"], index["scale"])
    if pin(path) != index["sha256"]:
        raise ValueError("Input differs from freeze")
    return CaseInput.model_validate_json(path.read_bytes())


def offline(arg):
    index, trial = arg
    tok = tokenizer()
    case = load_case(index)
    cas = ArtifactStore(LOCAL / "cas")
    start = perf_counter()
    bundle = build(case, trial["condition"], tok, cas)
    ms = round((perf_counter() - start) * 1000, 3)
    recovered = byte_exact_artifacts(case, cas)
    return {
        **trial,
        "split": index["split"],
        "mode": "offline",
        "input_sha256": index["sha256"],
        "status": "MEASURED",
        "context_ms": ms,
        **token_features(bundle, tok),
        "integrity": context_grade(case, bundle, recovered),
        "provider_usage": None,
        "model_calls": 0,
        "billable_cost_micros": 0,
    }


def run_offline(workers):
    # Independent clusters keep the originally randomized serial block order.
    args = verify()
    with (HERE / "raw.jsonl").open("xb") as out, multiprocessing.Pool(workers) as pool:
        for rows in pool.imap_unordered(offline_block, args):
            for row in rows:
                out.write(encoded(row) + b"\n")
            out.flush()


def offline_block(index):
    return [offline((index, t)) for t in schedule(index["case_id"], index["scale"])]


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=("freeze", "offline"))
    parser.add_argument("--workers", type=int, default=4)
    args = parser.parse_args()
    if args.command == "freeze":
        freeze(args.workers)
    else:
        run_offline(args.workers)
