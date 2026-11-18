"""R00-only collector: read original PG/CAS/files; write a new private evidence backup."""

from __future__ import annotations

import argparse
import gzip
import importlib.metadata
import io
import json
import os
import re
import subprocess
import tarfile
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import psycopg
from psycopg.rows import dict_row

from mgar_v3.domain import digest
from scripts.h12_eval import load_settings
from scripts.h12_r00 import (
    ReadonlyArchive,
    digest_bytes,
    exported_task_metadata,
    replay,
    verify_archive,
)

SUBTREES = (
    "src",
    "migrations",
    "config",
    "scripts",
    "tests",
    "fixtures/commerce",
    "web",
    "eval/final-v1",
    "eval/final-v3",
    "deploy",
)
PRUNE = {
    "sources",
    "projects",
    "downloads",
    "docker-config",
    "state",
    "__pycache__",
    "tmp",
    ".git",
    ".venv",
    "node_modules",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
}
EXTENSIONS = {
    ".json",
    ".jsonl",
    ".log",
    ".diff",
    ".patch",
    ".md",
    ".py",
    ".go",
    ".js",
    ".ts",
    ".txt",
    ".sql",
    ".yaml",
    ".yml",
    ".toml",
    ".lock",
    ".xml",
    ".csv",
}


def encoded(value: Any) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2, default=str) + "\n"
    ).encode()


def write_new(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("xb") as stream:
        stream.write(encoded(value))


def git(root: Path, *args: str) -> bytes:
    return subprocess.run(["git", *args], cwd=root, capture_output=True, check=True).stdout


def task_records(path: Path) -> list[dict[str, Any]]:
    records = []
    for line in path.read_text(errors="replace").splitlines():
        try:
            item = json.loads(line)
        except ValueError:
            # Partial historical logs remain archived byte-for-byte; they are not task catalogs.
            continue
        if isinstance(item, dict) and item.get("task_id") and item.get("base_commit"):
            records.append(item)
    return records


def is_cas_object(path: Path) -> bool:
    """The original ArtifactStore uses a two-hex shard and a full SHA256 filename."""
    return bool(re.fullmatch("[0-9a-f]{64}", path.name)) and path.parent.name == path.name[:2]


def archived_source(root: Path, commit: str) -> tuple[bytes, dict[str, Any]]:
    repository = Path(git(root, "rev-parse", "--show-toplevel").decode().strip())
    raw = git(repository, "archive", "--format=tar", commit)
    files = {}
    with tarfile.open(fileobj=io.BytesIO(raw), mode="r:") as bundle:
        for member in bundle.getmembers():
            if member.isfile() and member.name.startswith("v3/"):
                stream = bundle.extractfile(member)
                assert stream is not None
                files[member.name[3:]] = stream.read()
    # Reproduce scripts.verify.source_fingerprint, using the immutable Git bytes.
    import hashlib

    checksum = hashlib.sha256()
    selected = []
    for tree in SUBTREES:
        selected.extend(
            sorted(
                (
                    p
                    for p in files
                    if p.startswith(tree + "/") and "__pycache__" not in Path(p).parts
                ),
                key=lambda p: Path(p).parts,
            )
        )
    for name in selected:
        checksum.update(name.encode() + b"\0" + files[name] + b"\0")
    for name in ("pyproject.toml", "uv.lock"):
        if name in files:
            selected.append(name)
            checksum.update(name.encode() + b"\0" + files[name])
    return gzip.compress(raw, mtime=0), {
        "commit": commit,
        "source_sha256": checksum.hexdigest(),
        "files": [
            {"path": p, "bytes": len(files[p]), "sha256": digest_bytes(files[p])} for p in selected
        ],
        "dependency_locks": {p: digest_bytes(files[p]) for p in ("uv.lock", "pyproject.toml")},
        "task_model_acceptance_files": [
            {"path": p, "sha256": digest_bytes(files[p]), "bytes": len(files[p])}
            for p in sorted(files)
            if (p.startswith("experiments/harness-v4/") and p.endswith((".json", ".jsonl")))
            or p == "docs/harness-v4/ACCEPTANCE.md"
        ],
    }


def budget(conn: Any, key: str) -> dict[str, Any]:
    row = conn.execute(
        "SELECT limit_micros,reserved_micros,settled_micros,used_calls,call_limit "
        "FROM app.authorization_budgets WHERE profile_hash=%s",
        (key,),
    ).fetchone()
    unknown = conn.execute(
        "SELECT count(*) AS n FROM app.usage_calls WHERE profile_hash=%s AND status='unknown'",
        (key,),
    ).fetchone()["n"]
    return {"budget": row, "unknown_calls": unknown}


def snapshot_pg(settings: Any) -> tuple[dict[str, Any], dict[str, Any]]:
    key = digest(
        {
            "card": "HV4-H12",
            "project": settings.project_id,
            "shared_authorization": "user-approved-12-usd-v1",
        }
    )
    with psycopg.connect(
        settings.database_url.get_secret_value(),
        row_factory=dict_row,
        options="-c default_transaction_read_only=on",
    ) as conn:
        conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
        assert (
            conn.execute("SHOW transaction_read_only").fetchone()["transaction_read_only"] == "on"
        )
        snapshot = {
            "isolation": "REPEATABLE READ READ ONLY",
            "snapshot": conn.execute("SELECT txid_current_snapshot() AS id").fetchone()["id"],
            "schema_migrations": conn.execute(
                "SELECT * FROM app.schema_migrations ORDER BY name"
            ).fetchall(),
            "budget": budget(conn, key),
            "experiments": conn.execute(
                "SELECT * FROM app.harness_experiments WHERE project_id=%s "
                "AND experiment_id LIKE %s ORDER BY created_at",
                (settings.project_id, "hv4-h12%"),
            ).fetchall(),
            "usage_calls": conn.execute(
                "SELECT * FROM app.usage_calls WHERE profile_hash=%s ORDER BY call_id",
                (key,),
            ).fetchall(),
        }
        experiment_ids = [r["experiment_id"] for r in snapshot["experiments"]]
        snapshot["tasks"] = conn.execute(
            "SELECT * FROM app.harness_tasks WHERE experiment_id=ANY(%s) ORDER BY created_at",
            (experiment_ids,),
        ).fetchall()
        run_ids = [r["run_id"] for r in snapshot["tasks"]]
        snapshot["operations"] = conn.execute(
            "SELECT * FROM app.harness_operations WHERE run_id=ANY(%s::uuid[]) ORDER BY created_at",
            (run_ids,),
        ).fetchall()
        snapshot["runs"] = conn.execute(
            "SELECT * FROM app.runs WHERE run_id=ANY(%s::uuid[]) ORDER BY run_id",
            (run_ids,),
        ).fetchall()
        snapshot["actions"] = conn.execute(
            "SELECT * FROM app.actions WHERE run_id=ANY(%s::uuid[])",
            (run_ids,),
        ).fetchall()
    return snapshot, {"key": key, "project_id": settings.project_id}


def candidates(root: Path) -> list[Path]:
    """Exclude dependencies/temp checkouts, not receipts or historical score versions."""
    found: set[Path] = set()
    local = root / ".local/h12"
    if local.exists():
        for directory, folders, names in os.walk(local, followlinks=False):
            folders[:] = [
                p
                for p in folders
                if p not in PRUNE and not p.startswith(("build-", "pytest-", "tmp-", "test_"))
            ]
            for name in names:
                path = Path(directory) / name
                if path.suffix in EXTENSIONS and not path.is_symlink():
                    found.add(path)
    selections = []
    for parent in (
        root / "evidence",
        root / "evidence/harness-v4",
        root / "docs/harness-v4/accept",
    ):
        if parent.is_dir():
            selections.extend(
                p
                for p in parent.iterdir()
                if "H12" in p.name.upper() and "R00" not in p.name.upper()
            )
    docs = root / "docs/harness-v4"
    if docs.exists():
        selections.extend(
            p for p in docs.iterdir() if "H12" in p.name.upper() and "R00" not in p.name.upper()
        )
    for selected in selections:
        if selected.is_file() and not selected.is_symlink():
            found.add(selected)
        elif selected.is_dir():
            for directory, folders, names in os.walk(selected, followlinks=False):
                folders[:] = [
                    p
                    for p in folders
                    if p not in PRUNE and not p.startswith(("pytest-", "tmp-", "test_"))
                ]
                for name in names:
                    path = Path(directory) / name
                    if path.suffix in EXTENSIONS and not path.is_symlink():
                        found.add(path)
    return sorted(found)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--attempt", required=True, type=Path)
    parser.add_argument("--root", action="append", required=True, type=Path)
    parser.add_argument("--binding", required=True, type=Path)
    parser.add_argument(
        "--private-cause-map", type=Path, help="Historical held-out cause annotations"
    )
    args = parser.parse_args()
    attempt, root = args.attempt.resolve(), Path(__file__).resolve().parents[1]
    assert attempt.is_relative_to(root / "evidence/harness-v4/HV4-H12-R00")
    bootstrap = json.loads((attempt / "bootstrap.json").read_bytes())
    assert bootstrap["permission"] == {
        "paid_model_calls": False,
        "runtime_repair": False,
        "start_r01_to_r08": False,
    }
    assert git(root, "rev-parse", "HEAD").decode().strip() == bootstrap["source_commit"]
    archive = ReadonlyArchive(attempt)
    source_bytes, source = archived_source(root, bootstrap["source_commit"])
    assert source["source_sha256"] == bootstrap["source_sha256"]
    archive.add(source_bytes, origin=f"git-archive://{source['commit']}", category="source")
    write_new(attempt / "source-manifest.json", source)
    for supplied in bootstrap["taskbooks"]:
        path = Path(supplied["path"])
        assert digest_bytes(path.read_bytes()) == supplied["sha256"]
        archive.add_file(path, category="user_taskbook")
    binding = json.loads(args.binding.read_bytes())
    settings = load_settings(Path(binding["settings_file"]))
    pg, identity = snapshot_pg(settings)
    original_budget = pg["budget"]
    assert original_budget == {
        "budget": {
            "limit_micros": 12000000,
            "reserved_micros": 0,
            "settled_micros": 4211490,
            "used_calls": 813,
            "call_limit": 1536,
        },
        "unknown_calls": 0,
    }, "Shared ledger differs from the supplied R00 basis"
    assert len(pg["usage_calls"]) == 813
    assert sum(r["settled_micros"] or 0 for r in pg["usage_calls"]) == 4211490
    archive.add(encoded(pg), origin="pg-snapshot://HV4-H12/shared-budget", category="postgresql")
    for row in pg["schema_migrations"]:
        assert digest_bytes((root / "migrations" / row["name"]).read_bytes()) == row["sha256"]
    discovered: dict[str, dict[str, Any]] = {}
    task_metadata: dict[str, dict[str, Any]] = {}
    roots = [p.resolve() for p in args.root]
    for history in roots:
        for path in (history / "experiments/harness-v4").rglob("*.jsonl"):
            for item in task_records(path):
                task_metadata[item["task_id"]] = item
        for path in candidates(history):
            if path.name == "settings.json" or "docker-config" in path.parts:
                continue
            archive.add_file(path, category="historical_file")
            if path.suffix == ".jsonl":
                for item in task_records(path):
                    task_metadata[item["task_id"]] = item
            if path.suffix == ".json":
                try:
                    value = json.loads(path.read_bytes())
                except (ValueError, UnicodeError):
                    continue
                exported = exported_task_metadata(value)
                if exported is not None:
                    task_metadata[exported["task_id"]] = exported
                if isinstance(value, dict) and path.name == "score.json" and "task_id" in value:
                    discovered[str(path)] = value
        print(
            json.dumps({"archived_root": str(history), "files": len(archive.entries)}), flush=True
        )
    # Back up all original CAS bytes and durable tool/checkpoint files, without a new CAS writer.
    artifact_root = Path(settings.artifact_root)
    for path in sorted(artifact_root.glob("*/*")):
        if path.is_file() and not path.is_symlink():
            if is_cas_object(path):
                assert digest_bytes(path.read_bytes()) == path.name
                archive.add_file(path, category="original_cas")
            else:
                archive.add_file(path, category="cas_root_auxiliary_file")
    harness_root = Path(settings.harness_root)
    for directory, folders, names in os.walk(harness_root, followlinks=False):
        folders[:] = [
            p for p in folders if p not in {".git", ".venv", "node_modules", "__pycache__"}
        ]
        for name in names:
            path = Path(directory) / name
            if not path.is_symlink() and path.is_file():
                archive.add_file(path, category="durable_harness_file")
    by_run = {str(t["run_id"]): t for t in pg["tasks"]}
    index = []
    primary_id = "hv4-h12-complete-thinking-max-20261115"
    recovery_id = "hv4-h12-first-model-recovery-20261115"
    primary_causes = {
        "hv4-dev-click-short-help": "HARNESS_OUTPUT_TRUNCATION",
        "hv4-dev-pflag-unknown-flag": "AGENT_PUBLIC_BEHAVIOR",
        "hv4-dev-express-healthz": "AGENT_ENTRYPOINT_IMPORT",
        "hv4-dev-express-server-lifecycle": "AGENT_READINESS_OBSERVABILITY",
        "hv4-dev-got-local-roundtrip": "HARNESS_PUBLIC_ACCEPTANCE_PROJECTION",
        "hv4-dev-click-multifile-placeholder": "HARNESS_TOOL_DENIAL_RECOVERY",
    }
    if args.private_cause_map is not None:
        annotations = json.loads(args.private_cause_map.read_text(encoding="utf-8"))
        if not isinstance(annotations, dict) or not all(
            isinstance(key, str) and key.startswith("h12-v2-holdout-")
            and isinstance(value, str) for key, value in annotations.items()
        ):
            raise ValueError("private cause map must be a string-to-string JSON object")
        primary_causes.update(annotations)
    for origin, row in discovered.items():
        task = by_run.get(str(row.get("run_id")), {})
        profile = task.get("evaluation_profile") or {}
        metadata = task_metadata.get(row["task_id"], {})
        experiment = row.get("experiment_id") or task.get("experiment_id")
        if not experiment and "/full-matrix-thinking-20261115/live/" in origin:
            experiment = primary_id
        if not experiment and "/first-model-recovery-20261115/live/" in origin:
            experiment = recovery_id
        category = "NONE" if row["status"] == "PASS" else "UNCLASSIFIED_HISTORICAL_REASON"
        if experiment == primary_id:
            category = primary_causes.get(row["task_id"], category)
            if row["status"] == "NOT_RUN":
                category = (
                    "BENCHMARK_FEATURE_BASELINE"
                    if row["type"] == "feature"
                    else ("BENCHMARK_REFACTOR_BASELINE")
                )
        elif experiment == recovery_id and row["status"] == "FAIL":
            category = "BENCHMARK_UNDISCLOSED_IDENTIFIER"
        index.append(
            {
                "task_id": row["task_id"],
                "experiment_id": experiment,
                "arm": row.get("arm"),
                "split": row.get("split"),
                "repository": profile.get("repository") or metadata.get("repository"),
                "base_commit": profile.get("base_commit") or metadata.get("base_commit"),
                "type": row.get("type") or metadata.get("type"),
                "raw_status": row["status"],
                "reason": row.get("reason"),
                "run_id": row.get("run_id"),
                "root_cause_category": category,
                "evidence": archive.entries[origin],
            }
        )
    assert sum(r["experiment_id"] == primary_id for r in index) == 24
    assert sum(r["experiment_id"] == recovery_id for r in index) == 3
    # Keep all task metadata, including zero-Run/NOT_RUN records, available to later indexing.
    write_new(
        attempt / "h12-case-index.json",
        {
            "schema": "h12-r00-case-index-v1",
            "raw_score_count": len(index),
            "cases": index,
            "note": (
                "Raw scores are versioned observations; never merge them into a new quality score"
            ),
        },
    )
    fixtures = root / "tests/fixtures/h12_r00/diagnostic-fixtures"
    diagnostics = []
    for path in sorted((fixtures / "public").glob("*.json")):
        fixture = json.loads(path.read_bytes())
        observation = replay(fixture)
        assert observation["issue_observed"] is True
        target = attempt / "diagnostic-fixtures/public" / path.name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(path.read_bytes())
        diagnostics.append(
            {
                "fixture": path.name,
                "sha256": digest_bytes(path.read_bytes()),
                "observation": observation,
                "status": "PASS",
            }
        )
    assert len(diagnostics) == 8
    write_new(
        attempt / "diagnostic-fixtures/replay-results.json",
        {
            "status": "PASS",
            "model_calls": 0,
            "diagnostics": diagnostics,
            "scope": "Reproduction only, not implementation of R01-R06",
        },
    )
    # Verify the three source candidates from the supplied latest complete report.
    versions = []
    for commit in (
        "d272a01c577a9f7e3dcc826c569eaad28e3adb6d",
        "0655b59210d247cede5a43fab3c8279ebf3dc2d6",
    ):
        raw, manifest = archived_source(root, commit)
        archive.add(raw, origin=f"git-archive://{commit}", category="historical_source")
        versions.append({k: manifest[k] for k in ("commit", "source_sha256")})
    assert [v["source_sha256"] for v in versions] == [
        "b7dfff2c71320db22c3f5e541b4ba72e878475041a22d3c54ceb034ff1f84629",
        "14379894ecc51a06c005d3244052f11e2f1d58ceb575182bec7accf4563591e4",
    ]
    versions.append({k: source[k] for k in ("commit", "source_sha256")})
    with psycopg.connect(
        settings.database_url.get_secret_value(),
        row_factory=dict_row,
        options="-c default_transaction_read_only=on",
    ) as conn:
        after = budget(conn, identity["key"])
    assert after == original_budget
    manifest_path = archive.seal()
    verified = verify_archive(attempt)
    assert verified["status"] == "PASS"
    # Re-read only original archived inputs, proving our collection did not alter them.
    changed = []
    for entry in archive.entries.values():
        if entry["origin"].startswith("/") and (
            digest_bytes(Path(entry["origin"]).read_bytes()) != entry["sha256"]
        ):
            changed.append(entry["origin"])
    assert not changed
    baseline = {
        "schema": "h12-r00-baseline-v1",
        "card": "HV4-H12-R00",
        "recorded_at": datetime.now(UTC).isoformat(),
        "status": "PASS",
        "source": bootstrap,
        "historical_sources_verified": versions,
        "history_roots": [str(p) for p in roots],
        "migrations": pg["schema_migrations"],
        "budget_before": original_budget,
        "budget_after": after,
        "new_paid_calls": 0,
        "new_cost_micros": 0,
        "pg_counts": {k: len(pg[k]) for k in ("experiments", "tasks", "usage_calls", "operations")},
        "raw_score_statuses": dict(Counter(r["raw_status"] for r in index)),
        "archive_verification": verified,
        "original_files_changed": changed,
        "evidence_manifest_sha256": digest_bytes(manifest_path.read_bytes()),
        "installed_packages": sorted(
            (d.metadata["Name"], d.version) for d in importlib.metadata.distributions()
        ),
        "future_cards": {f"HV4-H12-R{i:02d}": "NOT_RUN" for i in range(1, 9)},
        "mvp": "NOT_RELEASED",
        "h13": "NOT_STARTED",
    }
    write_new(attempt / "baseline.json", baseline)
    for name in ("baseline.json", "source-manifest.json", "h12-case-index.json"):
        (attempt / name).chmod(0o400)
    print(
        json.dumps(
            {
                "status": "PASS",
                "attempt": str(attempt),
                "raw_scores": len(index),
                "archive": verified,
                "ledger": after,
                "new_paid_calls": 0,
            }
        ),
        flush=True,
    )


if __name__ == "__main__":
    main()
