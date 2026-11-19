"""Prepare the complete engineering suite in a disposable local checkout."""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
import tarfile
import tempfile
from pathlib import Path, PurePosixPath

REPO = Path(__file__).resolve().parents[2]
ARCHIVED_INPUTS = (
    "v3/experiments/harness-v4/tasks",
    "v3/experiments/harness-v4/judges",
    "v3/experiments/harness-v4/benchmark-v2",
    "v3/experiments/v34-audit-repair",
    "v3/experiments/v35-harness",
    "v3/eval/coordination-v1",
    "v3/eval/final-v1/oracles.json",
    "v3/eval/reports/final-v2*",
    "v3/eval/reports/final-v6*",
    "v3/eval/reports/coordination-*",
    "v3/docs/experiments/coordination-v1/trials",
    "v3/docs/repairs/R6-HOST-20261105/v2-current-official-pricing.json",
)
LOCAL_INPUTS = (
    "r03/sandbox-lock.json",
    "r5",
    "h12/sandbox-lock.json",
    "h12/environment-v4/got-offline-protocol-v4/sandbox-lock.json",
    "h12/environment-v4/express-node18.10.0/sandbox-lock.json",
    "h12/public-dev-20261113",
    "h12/sources",
)
REQUIRED_HISTORY = (
    "v3/experiments/harness-v4/tasks/dev.jsonl",
    "v3/experiments/harness-v4/tasks/holdout.jsonl",
    "v3/experiments/harness-v4/judges/dev/manifest.json",
    "v3/experiments/harness-v4/benchmark-v2/dev.jsonl",
    "v3/experiments/v34-audit-repair/cases.jsonl",
    "v3/experiments/v35-harness/tasks.jsonl",
    "v3/eval/reports/final-v6-answers.jsonl",
    "v3/eval/reports/final-v2-baseline-source.zip",
    "v3/eval/coordination-v1/oracle.json",
    "v3/eval/final-v1/oracles.json",
    "v3/docs/repairs/R6-HOST-20261105/v2-current-official-pricing.json",
)


def git(repository: Path, *args: str) -> str:
    return subprocess.check_output(("git", *args), cwd=repository, text=True).strip()


def file_hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def prepare(archive: Path, destination: Path) -> None:
    with tempfile.TemporaryFile() as stream:
        subprocess.run(("git", "archive", "HEAD"), cwd=REPO, stdout=stream, check=True)
        stream.seek(0)
        with tarfile.open(fileobj=stream) as snapshot:
            snapshot.extractall(destination, filter="data")
    git(destination, "init", "-q")
    git(destination, "fetch", "-q", "--no-tags", str(REPO), "HEAD")
    git(destination, "update-ref", "HEAD", "FETCH_HEAD")
    git(destination, "read-tree", "HEAD")

    publication = json.loads((destination / "PUBLICATION.json").read_bytes())
    original_hashes = {
        item["path"]: item["source_bytes_sha256"]
        for item in publication["historical_document_editions"]
    }
    baseline = json.loads(
        (destination / "v3/experiments/harness-v4/baseline.json").read_bytes()
    )
    original_hashes.update({
        f"v3/{path}": digest for path, digest in baseline["task_catalog"]["files"].items()
    })
    paths = git(archive, "ls-files", "-z", "--", *ARCHIVED_INPUTS).split("\0")
    restored = {}
    for path in filter(None, paths):
        relative = PurePosixPath(path)
        if relative.is_absolute() or ".." in relative.parts:
            raise ValueError(f"Invalid archive path: {path}")
        source = archive / path
        if source.is_symlink() or not source.is_file() or source.suffix == ".pyc":
            continue
        output = destination / path
        actual = file_hash(source)
        expected = original_hashes.get(path)
        if expected is not None and actual != expected:
            if path in REQUIRED_HISTORY:
                raise ValueError(f"Historical input differs from its original hash: {path}")
            continue
        if not output.is_file() or file_hash(output) != actual:
            output.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, output)
            restored[path] = actual
    missing = [
        path for path in REQUIRED_HISTORY
        if path not in paths or not (destination / path).is_file()
    ]
    if missing:
        raise ValueError("Missing original historical inputs: " + ", ".join(missing))

    local = REPO / "v3/.local"
    replay_local = destination / "v3/.local"
    for name in LOCAL_INPUTS:
        source, output = local / name, replay_local / name
        if not source.exists():
            raise ValueError(f"Missing prepared sandbox input: .local/{name}")
        output.parent.mkdir(parents=True, exist_ok=True)
        if source.is_dir():
            shutil.copytree(source, output, symlinks=True)
        else:
            shutil.copy2(source, output)
    for name in ("image-map.json", "locks/image-locks.json"):
        path = replay_local / "h12/public-dev-20261113" / name
        path.write_text(path.read_text().replace(str(local), str(replay_local)))

    (destination / ".engineering-replay.json").write_text(json.dumps({
        "source_commit": git(REPO, "rev-parse", "HEAD"),
        "archive_commit": git(archive, "rev-parse", "HEAD"),
        "restored_file_sha256": restored,
    }, indent=2) + "\n")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--archive", type=Path, required=True, help="local original repository")
    args = parser.parse_args()
    archive = args.archive.resolve(strict=True)
    if archive == REPO or git(archive, "rev-parse", "--show-toplevel") != str(archive):
        parser.error("--archive must point to a separate local repository root")
    for repository in (REPO, archive):
        if git(repository, "status", "--porcelain", "--untracked-files=no"):
            parser.error(f"Commit or set aside tracked changes in {repository}")
    destination = Path(tempfile.mkdtemp(prefix="magr-engineering-replay-", dir="/tmp"))
    try:
        prepare(archive, destination)
    except (OSError, ValueError, subprocess.CalledProcessError) as exc:
        shutil.rmtree(destination)
        parser.exit(2, f"{exc}\n")
    print(destination)


if __name__ == "__main__":
    main()
