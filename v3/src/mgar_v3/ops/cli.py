"""Operator commands. All output is JSON and excludes connection secrets."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import Any
from uuid import UUID

import psycopg
from pydantic import ValidationError

from mgar_v3.domain import DomainError
from mgar_v3.ops.metrics import export_metrics
from mgar_v3.ops.snapshot import backup, restore
from mgar_v3.settings import Settings


def bounded(low: int, high: int) -> Any:
    def validate(value: str) -> int:
        integer = int(value)
        if not low <= integer <= high:
            raise argparse.ArgumentTypeError(f"expected an integer from {low} to {high}")
        return integer

    return validate


def register(parser: argparse.ArgumentParser) -> None:
    commands = parser.add_subparsers(dest="ops_command", required=True)
    bundle = commands.add_parser("backup", help="Snapshot PostgreSQL, CAS, and the credential key")
    bundle.add_argument("output", type=Path)
    recover = commands.add_parser("restore", help="Restore into a new database and runtime")
    recover.add_argument("snapshot", type=Path)
    recover.add_argument("--target-dsn-env", required=True)
    recover.add_argument("--runtime", required=True, type=Path)
    metrics = commands.add_parser("metrics", help="Export redacted Harness operation measurements")
    metrics.add_argument("--hours", type=bounded(1, 168), default=24)
    metrics.add_argument("--limit", type=bounded(1, 1000), default=200)
    metrics.add_argument("--run-id", type=UUID)


def run(args: argparse.Namespace) -> int:
    try:
        if args.ops_command == "restore":
            dsn = os.environ.get(args.target_dsn_env)
            if not dsn:
                raise DomainError("SNAPSHOT_TARGET_DSN_MISSING", "Target DSN variable is missing")
            result = restore(args.snapshot, dsn, args.runtime)
        elif args.ops_command == "backup":
            result = backup(Settings(), args.output)
        else:
            result = export_metrics(
                Settings(), within_hours=args.hours, limit=args.limit, run_id=args.run_id,
            )
    except DomainError as exc:
        failure: dict[str, Any] = {"status": "FAIL", "code": exc.code}
        if exc.code == "SNAPSHOT_RUNTIME_PUBLISH_FAILED":
            failure.update(database_restored=True, retry_same_command=True)
        print(json.dumps(failure))
        return 2
    except ValidationError:
        print(json.dumps({"status": "FAIL", "code": "CONFIGURATION_INVALID"}))
        return 2
    except psycopg.Error:
        print(json.dumps({"status": "FAIL", "code": "OPERATOR_DATABASE_ERROR"}))
        return 2
    except OSError:
        print(json.dumps({"status": "FAIL", "code": "OPERATOR_FILESYSTEM_ERROR"}))
        return 2
    print(json.dumps(result))
    return 0
