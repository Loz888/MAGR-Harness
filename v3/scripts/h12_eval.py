"""H12 evaluator entry point with one persistent, private budget database.

This developer controller is not exposed to the coding model. It emits only
aggregate scores; held-out instructions, provider keys and judges stay private.
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import re
import secrets
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import psycopg
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo
from pydantic import ValidationError

from mgar_v3.coordination.engine import Engine
from mgar_v3.evidence.artifacts import ArtifactStore
from mgar_v3.quality_eval.h12_runner import ARMS, EvaluationRunner, private_json
from mgar_v3.settings import Settings
from mgar_v3.store.migrate import migrate

ROOT = Path(__file__).resolve().parents[1]
STATE = ROOT / ".local/h12/state"


def initialize_database(settings_file: Path) -> None:
    """Operational DB creation only; all experiment business writes use Engine."""
    STATE.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor = os.open(STATE / "initialize.lock", os.O_WRONLY | os.O_CREAT, 0o600)
    with os.fdopen(descriptor, "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        _initialize_database_locked(settings_file)


def _initialize_database_locked(settings_file: Path) -> None:
    binding = STATE / "budget-database-binding.json"
    if binding.exists():
        # A crash between binding, CREATE DATABASE and migrations must resume
        # the selected identity, never silently return or mint another budget.
        settings = load_settings(settings_file)
        database_url = settings.database_url.get_secret_value()
        name = json.loads(binding.read_text())["database"]
        _ensure_bound_database(database_url, name)
        return
    if settings_file.exists():
        raise ValueError("H12 refuses an unbound pre-existing settings file")
    configured = Settings()
    base = configured.database_url.get_secret_value()
    name = "magr_h12_" + uuid4().hex
    settings_file.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    # Persist the exact chosen identity before opening the database. A failure
    # is retained for repair rather than minting another $12 on a new DB.
    database_url = make_conninfo(base, dbname=name)
    payload = {
        "database_url": database_url,
        "project_id": "harness-h12-v1",
        "local_actor": configured.local_actor,
        "local_token": secrets.token_urlsafe(32),
        "allowed_actors": [configured.local_actor],
        "repository": configured.repository,
        "artifact_root": str(STATE / "artifacts"),
        "harness_root": str(STATE / "harness"),
        "allow_paid": True,
        "allow_external_write": False,
        "router_mode": "disabled",
    }
    private_json(settings_file, payload)
    private_json(
        binding,
        {
            "settings_file": str(settings_file),
            "database": name,
            "database_identity_sha256": hashlib.sha256(database_url.encode()).hexdigest(),
            "authorization": "h12-user-12-usd-v1",
        },
    )
    _ensure_bound_database(database_url, name)


def _ensure_bound_database(database_url: str, name: str) -> None:
    if not re.fullmatch(r"magr_h12_[a-f0-9]{32}", name):
        raise ValueError("H12 requires its dedicated database identity")
    if conninfo_to_dict(database_url).get("dbname") != name:
        raise ValueError("H12 database name does not match the binding")
    configured = Settings()
    with psycopg.connect(configured.database_url.get_secret_value(), autocommit=True) as conn:
        exists = conn.execute("SELECT 1 FROM pg_database WHERE datname=%s", (name,)).fetchone()
        if not exists:
            conn.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
    migrate(database_url)


def load_settings(settings_file: Path) -> Settings:
    binding_file = STATE / "budget-database-binding.json"
    if not binding_file.is_file() or not settings_file.is_file():
        raise ValueError("Initialize the private H12 budget database first")
    binding = json.loads(binding_file.read_text())
    values: dict[str, Any] = json.loads(settings_file.read_text())
    actual = hashlib.sha256(values["database_url"].encode()).hexdigest()
    if (
        Path(binding["settings_file"]) != settings_file
        or actual != binding["database_identity_sha256"]
        or values["project_id"] != "harness-h12-v1"
        or conninfo_to_dict(values["database_url"]).get("dbname") != binding["database"]
    ):
        raise ValueError("H12 database identity changed; refusing budget reset")
    if os.stat(settings_file).st_mode & 0o077:
        raise ValueError("H12 settings must be private to the current user")
    return Settings(_env_file=None, **values)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--settings", type=Path, default=STATE / "settings.json")
    parser.add_argument("--initialize-db", action="store_true")
    parser.add_argument("--initialize-experiment", action="store_true")
    parser.add_argument("--experiment-id", default="hv4-h12-20261111-v1")
    parser.add_argument("--image-map", type=Path)
    parser.add_argument("--registry", type=Path)
    parser.add_argument("--registry-sha256")
    parser.add_argument("--benchmark-freeze", type=Path)
    parser.add_argument("--benchmark-freeze-sha256")
    parser.add_argument("--public-dev-freeze", type=Path)
    parser.add_argument("--public-dev-freeze-sha256")
    parser.add_argument("--quality-freeze", type=Path)
    parser.add_argument("--quality-freeze-sha256")
    preparation = parser.add_mutually_exclusive_group()
    preparation.add_argument("--prepare-public-readiness", action="store_true")
    preparation.add_argument("--prepare-full-dev-readiness", action="store_true")
    preparation.add_argument("--prepare-holdout-readiness", action="store_true")
    parser.add_argument("--split", choices=("dev", "holdout"), default="dev")
    parser.add_argument("--arm", choices=ARMS, default="candidate")
    parser.add_argument("--task-id")
    parser.add_argument("--execute-live", action="store_true")
    parser.add_argument(
        "--environment-revision", choices=("h12-dev-environment-v3", "h12-dev-environment-v4")
    )
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    if bool(args.benchmark_freeze) != bool(args.benchmark_freeze_sha256):
        parser.error("v2 freeze path and SHA256 must be supplied together")
    if bool(args.public_dev_freeze) != bool(args.public_dev_freeze_sha256):
        parser.error("public dev freeze path and SHA256 must be supplied together")
    if bool(args.quality_freeze) != bool(args.quality_freeze_sha256):
        parser.error("quality freeze path and SHA256 must be supplied together")
    choices = (args.public_dev_freeze, args.benchmark_freeze, args.quality_freeze)
    if sum(bool(p) for p in choices) > 1:
        parser.error("choose exactly one frozen protocol")
    frozen_protocol = args.benchmark_freeze or args.public_dev_freeze or args.quality_freeze
    if frozen_protocol and (
        args.environment_revision or args.image_map or args.registry or args.registry_sha256
    ):
        parser.error("v2 freeze alone selects environments and judge registries")
    prepare = (args.prepare_public_readiness or args.prepare_full_dev_readiness
               or args.prepare_holdout_readiness)
    if args.public_dev_freeze and (args.split != "dev" or args.arm != "candidate"):
        parser.error("public development candidates require --split dev --arm candidate")
    if prepare and (
        not (args.public_dev_freeze or args.quality_freeze)
        or args.execute_live or args.initialize_db
        or args.initialize_experiment or args.task_id
    ):
        parser.error("readiness requires a public dev freeze and forbids paid or database actions")
    if args.prepare_holdout_readiness and not args.quality_freeze:
        parser.error("independent preparation requires an explicit quality freeze")
    if args.quality_freeze and prepare and not args.prepare_holdout_readiness:
        parser.error("quality preparation selects the independent holdout only")
    if args.execute_live and not frozen_protocol and (
        args.environment_revision not in {"h12-dev-environment-v3", "h12-dev-environment-v4"}
        or args.split != "dev"
    ):
        parser.error(
            "new paid execution requires --split dev "
            "--environment-revision h12-dev-environment-v3 or h12-dev-environment-v4"
        )
    settings_file = args.settings.resolve()
    try:
        if args.quality_freeze:
            from mgar_v3.quality_eval.h12_quality_runner import QualityRunner

            target = args.out.resolve()
            quality = QualityRunner(
                SimpleNamespace(harness=None), experiment_id=args.experiment_id,
                evidence=target.with_name(target.name + '-controller') if prepare else target,
                freeze_path=args.quality_freeze.resolve(), freeze_sha256=args.quality_freeze_sha256,
                api_key=None, execute_live=args.execute_live,
            )
            if prepare:
                report = quality.prepare_holdout_readiness(target)
                print(json.dumps({"status": report["status"], "ready_count": report["ready_count"],
                                  "paid_calls": 0, "quality_status": "NOT_RUN"}))
                return 0 if report["status"] == "FULL_HOLDOUT_READY" else 1
            quality._require_public_development_frozen()
        if args.public_dev_freeze:
            from mgar_v3.quality_eval.h12_v2_runner import BenchmarkV2Runner

            # Validate source, plans and pinned reports before opening Engine or
            # reading a provider key. Preparation has no database at all.
            target = args.out.resolve()
            prepared = BenchmarkV2Runner(
                SimpleNamespace(harness=None), experiment_id=args.experiment_id,
                evidence=target.with_name(target.name + '-controller') if prepare else target,
                freeze_path=args.public_dev_freeze.resolve(),
                freeze_sha256=args.public_dev_freeze_sha256, api_key=None,
                execute_live=args.execute_live, public_development=True,
            )
            if prepare:
                report = (
                    prepared.prepare_public_readiness(args.out.resolve())
                    if args.prepare_public_readiness
                    else prepared.prepare_full_dev_readiness(args.out.resolve())
                )
                print(json.dumps({
                    "status": report["status"], "paid_calls": 0,
                    "quality_status": "NOT_RUN", "readiness_only": True,
                }))
                return 0 if report["status"] in {"PUBLIC_SUBSET_READY", "FULL_DEV_READY"} else 1
            prepared._require_public_development_frozen()
        if args.initialize_db:
            initialize_database(settings_file)
        settings = load_settings(settings_file)
        if args.image_map is None and not frozen_protocol:
            if args.initialize_db and not args.execute_live:
                print(json.dumps({"status": "DATABASE_BOUND", "paid_calls": 0}))
                return 0
            raise ValueError("A frozen project image map is required")
        image_map = json.loads(args.image_map.read_text()) if args.image_map else {}
        configured = Settings()
        key = (
            configured.deepseek_api_key.get_secret_value()
            if configured.deepseek_api_key is not None
            else None
        )
        engine = Engine(settings, ArtifactStore(settings.artifact_root))
        try:
            evaluator: EvaluationRunner
            if args.quality_freeze:
                from mgar_v3.quality_eval.h12_quality_runner import QualityRunner

                evaluator = QualityRunner(
                    engine, experiment_id=args.experiment_id, evidence=args.out.resolve(),
                    freeze_path=args.quality_freeze.resolve(),
                    freeze_sha256=args.quality_freeze_sha256, api_key=key,
                    execute_live=args.execute_live,
                )
            elif args.benchmark_freeze or args.public_dev_freeze:
                from mgar_v3.quality_eval.h12_v2_runner import BenchmarkV2Runner
                evaluator = BenchmarkV2Runner(
                    engine, experiment_id=args.experiment_id,
                    evidence=args.out.resolve(),
                    freeze_path=(args.public_dev_freeze or args.benchmark_freeze).resolve(),
                    freeze_sha256=args.public_dev_freeze_sha256 or args.benchmark_freeze_sha256,
                    api_key=key, execute_live=args.execute_live,
                    public_development=bool(args.public_dev_freeze),
                )
            else:
                evaluator = EvaluationRunner(
                    engine, experiment_id=args.experiment_id,
                    evidence=args.out.resolve(), image_locks=image_map,
                    api_key=key, execute_live=args.execute_live,
                    registry_path=args.registry.resolve() if args.registry else None,
                    registry_sha256=args.registry_sha256,
                    environment_revision=args.environment_revision,
                )
            if args.initialize_experiment:
                evaluator.initialize()
                if not args.execute_live:
                    print(json.dumps({"status": "EXPERIMENT_FROZEN", "paid_calls": 0}))
                    return 0
            report = evaluator.evaluate(split=args.split, arm=args.arm, task_id=args.task_id)
            summary = report["summary"]
            print(
                json.dumps(
                    {
                        "split": args.split,
                        "arm": args.arm,
                        "pass_count": summary["pass_count"],
                        "denominator": 12,
                        "not_run": summary["not_run_count"],
                        "unknown": summary["unknown_count"],
                        "threshold_met": summary["threshold_met"],
                        "shared_budget": report["shared_budget"],
                        "usage": report["usage"],
                    }
                )
            )
            return 0 if summary["threshold_met"] else 1
        finally:
            engine.close()
    except Exception as exc:
        # Never echo a DSN, an API key, held-out objective or raw provider body.
        print(
            json.dumps(
                {
                    "status": "BLOCKED",
                    "error_type": type(exc).__name__,
                    "error_code": getattr(exc, "code", "H12_CONTROLLER_ERROR"),
                    "validation_fields": [
                        {"loc": item["loc"], "type": item["type"]}
                        for item in exc.errors(include_input=False, include_context=False)
                    ]
                    if isinstance(exc, ValidationError)
                    else [],
                }
            )
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
