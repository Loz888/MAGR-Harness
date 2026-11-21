"""Prepare a dedicated local PG instance and run a frozen engineering study."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import secrets
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from uuid import uuid4

import psycopg
from psycopg.conninfo import make_conninfo

ROOT = Path(__file__).resolve().parents[3]
SPEC = Path(__file__).resolve().parent
EVIDENCE = ROOT / "evidence/CAPABILITY-PHASE3"
IMAGE = "pgvector/pgvector:0.8.1-pg16-bookworm"
FILES = {"h12": "h12/test_matrix.py", "h17": "h17/test_paired.py"}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("study", choices=FILES)
    parser.add_argument("attempt")
    parser.add_argument("--slice", nargs=2, metavar=("INDEX", "COUNT"), type=int)
    parser.add_argument("--select", choices=("preflight", "test_patch_transport", "full_file"))
    args = parser.parse_args()
    if args.select and args.study != "h12":
        parser.error("H12 selectors require the h12 study.")
    if args.slice and not (args.study == "h17" and 0 <= args.slice[0] < args.slice[1] <= 12):
        parser.error("H17 slices require 0 <= INDEX < COUNT <= 12.")
    if not args.attempt.isalnum():
        parser.error("Name attempts with letters and digits.")
    manifest = json.loads((SPEC / "manifest.json").read_text())
    for relative, digest in manifest["sources"].items():
        if hashlib.sha256((ROOT / relative).read_bytes()).hexdigest() != digest:
            raise RuntimeError(f"Frozen input differs: {relative}")
    output = EVIDENCE / args.study / args.attempt
    output.mkdir(parents=True, exist_ok=False)
    env = dict(os.environ, POSTGRES_PASSWORD=secrets.token_urlsafe(32))
    name = "mgar-phase3-" + uuid4().hex[:12]
    cid = subprocess.check_output(
        [
            "docker",
            "run",
            "-d",
            "--rm",
            "--name",
            name,
            "-p",
            "127.0.0.1::5432",
            "-e",
            "POSTGRES_PASSWORD",
            IMAGE,
        ],
        cwd=ROOT,
        env=env,
        text=True,
    ).strip()
    try:
        port = subprocess.check_output(["docker", "port", cid, "5432/tcp"], text=True).strip()
        dsn = make_conninfo(
            host="127.0.0.1",
            port=port.rsplit(":", 1)[1],
            user="postgres",
            dbname="postgres",
            password=env.pop("POSTGRES_PASSWORD"),
        )
        env.update(
            MGAR_V3_DATABASE_URL=dsn,
            MGAR_V3_LOCAL_TOKEN=secrets.token_urlsafe(32),
            MGAR_V3_ALLOW_PAID="false",
            MGAR_V3_ALLOW_EXTERNAL_WRITE="false",
            MAGR_PHASE3_EVIDENCE=str(output),
            PYTHONPATH=str(ROOT / "tests"),
        )
        for _ in range(60):
            try:
                with psycopg.connect(dsn, connect_timeout=1) as connection:
                    version = connection.execute("SELECT version()").fetchone()
                    (output / "postgres-version.txt").write_text(version[0] + "\n")
                break
            except psycopg.OperationalError:
                time.sleep(0.4)
        else:
            raise RuntimeError("Dedicated PostgreSQL startup timed out")
        scratch = Path(tempfile.mkdtemp(prefix="magr-phase3-"))
        command = [
            sys.executable,
            "-m",
            "pytest",
            "-p",
            "conftest",
            "-q",
            str(SPEC / FILES[args.study]),
            "--junitxml=" + str(output / "junit.xml"),
            "--basetemp=" + str(scratch / "tests"),
        ]
        if args.select:
            command.extend(("-k", args.select))
        if args.slice:
            index, count = args.slice
            schedule = [
                json.loads(line) for line in (SPEC / "h17/schedule.jsonl").read_text().splitlines()
            ]
            selected = schedule[index::count]
            test_file = str(SPEC / FILES[args.study])
            pos = command.index(test_file)
            command[pos : pos + 1] = [
                f"{test_file}::test_paired_product_trial[{row['case_id']}-{row['seed']}]"
                for row in selected
            ]
        with (output / "command.log").open("x") as log:
            proc = subprocess.run(command, cwd=ROOT, env=env, stdout=log, stderr=subprocess.STDOUT)
        print(f"{args.study} {output} exit {proc.returncode}")
        return proc.returncode
    finally:
        subprocess.run(["docker", "stop", cid], stdout=subprocess.DEVNULL, check=False)


if __name__ == "__main__":
    sys.exit(main())
