"""Run only the source-frozen synthetic tasks, through the real Harness product path."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import random
import secrets
import subprocess
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from contextvars import ContextVar
from dataclasses import replace
from pathlib import Path
from uuid import uuid4

import httpx
import psycopg
from budget import CALLS_PER_TRIAL, OUTPUT_CAPS, BoundedAdapter, Ledger, canonical
from dotenv import dotenv_values
from psycopg.conninfo import make_conninfo
from pydantic import SecretStr

from mgar_v3.coordination.engine import Engine
from mgar_v3.domain import DomainError
from mgar_v3.evidence.artifacts import ArtifactStore
from mgar_v3.harness.coding_loop import CodingLoop
from mgar_v3.harness.delegation import CONSULT
from mgar_v3.harness.output_protocol import OutputPolicy
from mgar_v3.harness.product import HarnessProductService, infer_intent
from mgar_v3.harness.product_models import product_model_capabilities
from mgar_v3.harness.service import CodingToolRegistry, HarnessCodingService
from mgar_v3.quality_eval.e1_run import isolated_database
from mgar_v3.settings import Settings
from mgar_v3.store.migrate import migrate
from mgar_v3.web_access import (
    AnalysisSettingsInput,
    BrowserSession,
    LoginInput,
    WebAccounts,
    WebAnalysisSettings,
)

HERE = Path(__file__).resolve().parent
BASE = HERE.parent
ROOT = BASE.parents[2]
OUT = ROOT / "evidence/CAPABILITY-PHASE3-LIVE"
SANDBOX = ROOT / ".local/r03/sandbox-lock.json"
PG = "pgvector/pgvector:0.8.1-pg16-bookworm"
H12_INSTRUCTIONS = {
    "full_file": (
        "Use complete content for source.py in the apply_edits tool. Retain every comment line."
    ),
    "chunked": "Use one separate apply_edits call per assignment, each with only one replacement.",
    "structured_plan": "Use one apply_edits call with an explicit list of all three replacements.",
    "verified_resume": (
        "Use one separate apply_edits call per assignment. Include the latest file and snippet "
        "SHA256 hashes. If a reply is truncated, submit a complete tool call based on the "
        "existing completed edits and current file state."
    ),
}


RUN_ARM = ContextVar("frozen_phase3_run_arm", default=None)
BASE_BUDGET = CodingLoop._output_budget
BASE_INIT = CodingLoop.__init__
BASE_SPECS = CodingToolRegistry.specs
BASE_ASK = CodingLoop._ask_user


def trial_budget(loop, state):
    budget = BASE_BUDGET(loop, state)
    trial = RUN_ARM.get()
    if not budget or trial is None:
        return budget
    study, arm, _key, _ledger = trial
    cap = min(budget.completion_tokens, OUTPUT_CAPS[study])
    if (
        study == "h12"
        and arm == "verified_resume"
        and budget.stage == "implement"
        and not any(
            r.output_budget and r.output_budget.completion_tokens == 128
            for r in state.output_receipts
        )
    ):
        cap = min(cap, 128)
    return OutputPolicy().budget(budget.stage, cap)


def trial_init(loop, *args, **kwargs):
    BASE_INIT(loop, *args, **kwargs)
    original = loop.before_model_request
    if RUN_ARM.get() is None:
        return

    def admit(request):
        if original:
            original(request)
        active = RUN_ARM.get()
        if active:
            study, _arm, key, ledger = active
            ledger.check_admission(study, key, request)

    loop.before_model_request = admit


def trial_specs(registry):
    answerable_tools = BASE_SPECS(registry)
    if RUN_ARM.get() is None:
        return answerable_tools
    return tuple(t for t in answerable_tools if t.name != "ask_user")


def trial_ask(loop, state, call):
    if RUN_ARM.get() is None:
        return BASE_ASK(loop, state, call)
    from mgar_v3.harness.contracts import HarnessToolResult

    return HarnessToolResult(
        call_id=call.call_id,
        status="denied",
        content="Frozen synthetic study. The original task grant is final.",
    )


def rows(path):
    return [json.loads(row) for row in path.read_text().splitlines()]


def put(folder, name, value):
    path = folder / name
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w") as file:
        file.write(canonical(value) + "\n")
        file.flush()
        os.fsync(file.fileno())
    os.replace(tmp, path)


def input_project(study, case, source, arm, seed):
    source.mkdir()
    if study == "h12":
        names = ("width", "height", "margin")
        values = dict(zip(names, case["positions"], strict=True))
        content = "".join(f"{k} = -{v}\n" for k, v in values.items()) + "".join(
            f"# filler {i:03} 測定\n" for i in range(case["padding_lines"])
        )
        (source / "source.py").write_text(content)
        tests = "import unittest\nimport source\nclass Bounds(unittest.TestCase):\n"
        (source / "test_source.py").write_text(
            tests
            + "".join(
                f"    def test_{k}(self): self.assertEqual(source.{k}, {v})\n"
                for k, v in values.items()
            )
        )
        text = (
            "修复 source.py 里 width、height、margin 的符号，让它们为正。保留原来的测量注释，"
            "原测试文件必须逐字节保留。每次只调用一个工具，先读取 source.py，"
            "再用固定测试命令验证全部三个行为。" + H12_INSTRUCTIONS[arm]
        )
        return text, True
    datum, count = case["datum"], case["operations"]
    inspect = case["stratum"] in {"simple", "complex", "research"}
    paths = []
    for i in range(count):
        if inspect:
            path, text = f"source_{i}.txt", f"Reference for {case['topic']}: [{datum + i}].\n"
        else:
            path, text = f"unit_{i}.py", f"def result(value):\n    return value - {datum + i}\n"
        (source / path).write_text(text)
        paths.append(path)
    random.Random(seed).shuffle(paths)
    if inspect:
        return (
            f"解释 {case['topic']} 的资料。检查这份清单的每个文件 {', '.join(paths)}，"
            "每次只调用一个工具。回答按一文件一项列出真实文件名及其方括号内编号，保留方括号格式。"
        ), False
    imports = [f"from unit_{i} import result as result_{i}" for i in range(count)]
    tests = [
        f"    def test_unit_{i}(self): self.assertEqual(result_{i}(7), {7 + datum + i})"
        for i in range(count)
    ]
    (source / "test_suite.py").write_text(
        "\n".join(
            ["import unittest", *imports, "class CalculationTests(unittest.TestCase):", *tests, ""]
        )
    )
    return (
        f"修复 {case['topic']} 的计算错误。范围为 {', '.join(paths)}。"
        "保留接口和原测试文件。每次只调用一个工具，运行固定测试命令后汇报结果。"
    ), True


def run_trial(engine, client, ledger, key, product, harness, data, study, case, seed, arm, base):
    tail = f"{study}-{case['case_id']}-{seed}-{arm}"
    prior = data / f"bound3-{tail}.json"
    if prior.is_file():
        base_record = json.loads(prior.read_text())
        if not base_record["error"] and base_record["result"].get("usage_known"):
            return
    uid = f"bound4-{tail}"
    if (data / (uid + ".json")).exists() or ledger.halted:
        return
    if ledger.used(uid):
        raise RuntimeError("A previously dispatched partial trial requires recovery")
    source = base / uid
    objective, writable = input_project(study, case, source, arm, seed)
    if (infer_intent(objective) != "inspect") != writable:
        raise RuntimeError("Frozen objective selects the wrong product intent")
    engine.settings.harness_experts_enabled = arm == "delegate"
    ident = harness.register_project(str(source))["project_id"]
    product.authorize(
        ident,
        allow_write=writable,
        allow_paid=True,
        max_cost_micros=160_000,
        max_model_calls=CALLS_PER_TRIAL,
        days=1,
    )
    created = []
    caps = product_model_capabilities()

    def model_factory(request):
        adapter = BoundedAdapter(
            ledger=ledger,
            study=study,
            trial=uid,
            arm=arm,
            api_key=key,
            client=client,
            capabilities=caps,
            artifacts=engine.artifacts,
        )
        created.append(adapter)
        return adapter

    harness.model_factory = model_factory
    start = time.monotonic()
    error = None
    active = RUN_ARM.set((study, arm, uid, ledger))
    try:
        session = product.create(ident, objective, uid)
        task_id = session.get("task_id")
        result = harness.get_task(task_id) if task_id else {}
    except DomainError as exc:
        session = {}
        result = {}
        error = exc.code
    finally:
        RUN_ARM.reset(active)
    took = time.monotonic() - start
    task = result.get("task_id")
    if not task and error:
        candidates = [
            x["task_id"]
            for x in engine.harness.tasks()
            if x.get("request", {}).get("project_id") == ident
        ]
        task = candidates[0] if len(candidates) == 1 else None
    runtime = harness._runtimes.get(task) if task else None
    traces = engine.harness.operations(task) if task else []
    tree = runtime.workspace.root if runtime else None
    copy = data / (uid + "-files")
    if tree:
        copy.mkdir()
        for original in source.iterdir():
            final = tree / original.name
            if final.is_file() and not final.is_symlink():
                (copy / original.name).write_bytes(final.read_bytes())
        observed_paths = sorted(p.name for p in tree.iterdir() if p.is_file() or p.is_symlink())
    else:
        observed_paths = []
    captures = []
    for receipt in result.get("output_receipts", []):
        digest = receipt.get("artifact_hash")
        captures.append(
            {
                "receipt": receipt,
                "cas": json.loads(engine.artifacts.read(digest)) if digest else None,
            }
        )
    if not captures:
        with ledger.lock:
            accounted = ledger.db.execute(
                "SELECT receipt FROM calls WHERE trial=? AND receipt IS NOT NULL ORDER BY started",
                (uid,),
            ).fetchall()
        for entry in accounted:
            receipt = json.loads(entry[0]).get("output_receipt")
            if receipt and receipt.get("artifact_hash"):
                blob = engine.artifacts.read(receipt["artifact_hash"])
                captures.append({"receipt": receipt, "cas": json.loads(blob)})
    consultations = [
        {"status": op["state"], "receipt": op["receipt"]}
        for op in traces
        if op["kind"] == "tool" and op["intent"].get("name") == CONSULT.name
    ]
    record = dict(
        study=study,
        case_id=case["case_id"],
        stratum=case.get("stratum"),
        seed=seed,
        arm=arm,
        key=uid,
        elapsed_seconds=took,
        error=error,
        result=result,
        run_id=str(runtime.task.run_id) if runtime and runtime.task else None,
        observed_paths=observed_paths,
        consultations=consultations,
        captures=captures,
        source={p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in source.iterdir()},
        model_ops=[op["operation_id"] for op in traces if op["kind"] == "model"],
        edits=[op for op in traces if op["kind"] == "edit"],
    )
    put(data, uid + ".json", record)


def worker(shard, schedule, admin, client, ledger, key, data):
    runtime = OUT / "runtime" / f"worker-{shard}-{uuid4().hex[:8]}"
    runtime.mkdir(parents=True)
    tasks = Path(tempfile.mkdtemp(prefix="magr-phase3-live-"))
    tasks.chmod(0o755)
    with isolated_database(admin) as dsn:
        migrate(dsn)
        settings = Settings(
            database_url=dsn,
            local_token=SecretStr(secrets.token_urlsafe(32)),
            artifact_root=runtime / "cas",
            harness_root=runtime / "harness",
            harness_image_lock=SANDBOX,
            allow_paid=False,
            allow_external_write=False,
            local_actor="owner",
            allowed_actors=["owner"],
            _env_file=None,
        )
        engine = Engine(settings, ArtifactStore(settings.artifact_root))
        harness = HarnessCodingService(
            settings.harness_root, actor="owner", image_lock=SANDBOX, engine=engine
        )
        web = WebAnalysisSettings(engine, settings)
        account = WebAccounts(engine).create_owner(
            LoginInput(username="owner", password=SecretStr(secrets.token_urlsafe(24)))
        )
        browser = BrowserSession(user_id=account, username="owner", csrf_token="synthetic-runner")
        revision, _ = web.snapshot()
        web.save(
            AnalysisSettingsInput(
                expected_revision=revision,
                deepseek_api_key=SecretStr(key),
                allow_paid=True,
                model_id="deepseek-flash",
            ),
            browser,
        )
        product = HarnessProductService(harness, web)
        harness.submit_task = lambda request: harness.create_task(
            replace(request, product_expert_max_tokens=settings.harness_expert_token_limit)
        )
        try:
            for pair in schedule:
                if ledger.halted:
                    return
                for arm in pair["arms"]:
                    run_trial(
                        engine,
                        client,
                        ledger,
                        key,
                        product,
                        harness,
                        data,
                        pair["study"],
                        pair["case"],
                        pair["seed"],
                        arm,
                        tasks,
                    )
        finally:
            product.close()
            engine.close()


def check_inputs():
    manifest = HERE / "manifest.json"
    document = json.loads(manifest.read_text())
    for path, digest in document["file_sha256"].items():
        if hashlib.sha256((ROOT / path).read_bytes()).hexdigest() != digest:
            raise ValueError(f"Frozen Phase 3 input differs: {path}")
    source = subprocess.check_output(
        ["git", "status", "--porcelain", "--", "src", "config", "migrations"], cwd=ROOT, text=True
    )
    changes = subprocess.check_output(
        [
            "git",
            "diff",
            "--name-only",
            document["product_commit"],
            "--",
            "src",
            "config",
            "migrations",
        ],
        cwd=ROOT,
        text=True,
    )
    if source or changes:
        raise ValueError("The product checkout changed from the declared source")
    return hashlib.sha256(manifest.read_bytes()).hexdigest()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--workers", type=int, default=12, choices=range(1, 17))
    parser.add_argument("--launch", choices=("formal", "offline"), required=True)
    options = parser.parse_args()
    digest = check_inputs()
    credentials = dotenv_values(Path.home() / "magr-harness/v3/.env")
    key = os.environ.get("DEEPSEEK_API_KEY") or credentials.get("MGAR_V3_DEEPSEEK_API_KEY")
    if options.launch == "formal" and not key:
        raise RuntimeError("Locked DeepSeek credential is unavailable")
    if options.launch == "offline":
        key = "synthetic-offline-test-token"
    path = OUT if options.launch == "formal" else OUT / "offline"
    path.mkdir(parents=True, exist_ok=True)
    data = path / "trials"
    data.mkdir(exist_ok=True)
    previous = tuple(
        hashlib.sha256((HERE / name).read_bytes()).hexdigest()
        for name in ("manifest-first.json", "manifest-binding.json", "manifest-input-bound.json")
    )
    ledger = Ledger(path / "ledger.sqlite", digest, previous)
    CodingLoop._output_budget = trial_budget
    CodingLoop.__init__ = trial_init
    CodingToolRegistry.specs = trial_specs
    CodingLoop._ask_user = trial_ask
    if ledger.halted:
        ledger.close()
        raise RuntimeError("The previous journal contains usage that needs reconciliation")
    grouping = rows(HERE / "schedule.jsonl")
    cookie = secrets.token_urlsafe(32)
    env = {**os.environ, "POSTGRES_PASSWORD": cookie}
    cid = subprocess.check_output(
        [
            "docker",
            "run",
            "-d",
            "--rm",
            "--name",
            "mgar-phase3-live-" + uuid4().hex[:12],
            "-p",
            "127.0.0.1::5432",
            "-e",
            "POSTGRES_PASSWORD",
            PG,
        ],
        env=env,
        text=True,
    ).strip()
    try:
        port = (
            subprocess.check_output(["docker", "port", cid, "5432/tcp"], text=True)
            .strip()
            .rsplit(":", 1)[1]
        )
        dsn = make_conninfo(
            host="127.0.0.1", port=port, dbname="postgres", user="postgres", password=cookie
        )
        for _ in range(75):
            try:
                with psycopg.connect(dsn, connect_timeout=2) as check:
                    put(
                        path,
                        "postgres.json",
                        {"version": check.execute("SELECT version()").fetchone()[0]},
                    )
                break
            except psycopg.OperationalError:
                time.sleep(0.5)
        else:
            raise RuntimeError("Dedicated PG is unavailable")
        if options.launch == "offline":

            def complete(request):
                payload = json.loads(request.content)
                assert (
                    payload["thinking"] == {"type": "enabled"}
                    and payload["reasoning_effort"] == "max"
                )
                msg = [
                    {
                        "id": "mock",
                        "choices": [{"index": 0, "delta": {"content": "Synthetic declaration"}}],
                    },
                    {"id": "mock", "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]},
                    {
                        "id": "mock",
                        "choices": [],
                        "usage": {"prompt_tokens": 40, "completion_tokens": 5},
                    },
                ]
                body = (
                    "".join("data: " + json.dumps(row) + "\n\n" for row in msg) + "data: [DONE]\n\n"
                )
                return httpx.Response(200, content=body.encode())

            transport = httpx.MockTransport(complete)
        else:
            transport = None
        with httpx.Client(timeout=httpx.Timeout(120, connect=15), transport=transport) as client:
            if options.launch == "formal":
                status = client.head("https://api.deepseek.com/").status_code
                put(path, "connection-check.json", {"unauthenticated_head": status})
                if not 200 <= status < 500:
                    raise RuntimeError("Endpoint readiness failed")
            else:
                grouping = [
                    next(
                        p
                        for p in grouping
                        if p["study"] == "h17" and p["case"]["stratum"] == "simple"
                    )
                ]
            # Keep the first corrected arm sequential as an admission check.
            if options.launch == "formal":
                first = {**grouping[0], "arms": grouping[0]["arms"][:1]}
                worker("first-corrected", [first], dsn, client, ledger, key, data)
                match = data / "bound4-h12-p3-patch-01-2103-verified_resume.json"
                if ledger.halted or not match.exists():
                    raise RuntimeError("Corrected first arm did not produce a settled record")
                row = json.loads(match.read_text())
                if row["error"] in {"MODEL_OUTPUT_SCOPE", "PHASE3_OUTPUT_SCOPE"}:
                    grouping = [p for p in grouping if p["study"] == "h17"]
            else:
                worker("first-offline", grouping[:1], dsn, client, ledger, key, data)
            if not ledger.halted and len(grouping) > 1:
                with ThreadPoolExecutor(max_workers=options.workers) as pool:
                    batches = (grouping[i :: options.workers] for i in range(options.workers))
                    list(
                        pool.map(
                            lambda item: worker(item[0], item[1], dsn, client, ledger, key, data),
                            enumerate(batches),
                        )
                    )
    finally:
        ledger.close()
        subprocess.run(["docker", "stop", cid], stdout=subprocess.DEVNULL, check=False)


if __name__ == "__main__":
    sys.exit(main())
