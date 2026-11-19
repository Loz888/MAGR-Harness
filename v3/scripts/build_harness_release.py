"""Build a reproducible source release for a frozen Harness source revision."""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import os
import subprocess
import tarfile
import tempfile
from io import BytesIO
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PREFIX = "magr-harness"
RELEASE_PATHS = (
    "AGENTS.md", "Makefile", "README.md", "pyproject.toml", "uv.lock",
    "config", "deploy", "docs", "eval", "examples", "fixtures", "migrations",
    "scripts", "src", "tests", "web",
)
PUBLIC_GA12_PATHS = (
    "experiments/general-agent/ga12/dev.jsonl",
    "experiments/general-agent/ga12/dev.freeze.json",
    "experiments/general-agent/ga12/holdout.commitment.json",
    "experiments/general-agent/ga12/holdout.first.receipt.json",
    "experiments/general-agent/ga12/holdout.recheck.receipt.json",
)


def build_release(revision: str, output: Path) -> dict[str, str]:
    def git(*args: str) -> bytes:
        return subprocess.check_output(("git", "-C", str(ROOT.parent), *args))

    verified = git("rev-parse", "--verify", "--end-of-options", revision + "^{commit}")
    commit = verified.decode().strip()
    epoch = int(git("show", "-s", "--format=%ct", commit))
    tree = f"{commit}:v3"
    # Older tagged revisions predate the benchmark's public assets.
    listing = git("ls-tree", "-r", "--name-only", "-z", tree, "--", *PUBLIC_GA12_PATHS)
    tracked = set(listing.decode().split("\0"))
    paths = (*RELEASE_PATHS, *(path for path in PUBLIC_GA12_PATHS if path in tracked))
    raw = git("archive", "--format=tar", f"--prefix={PREFIX}/", tree, *paths)
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=output.parent, delete=False) as file:
        scratch = Path(file.name)
        try:
            items = {}
            with (tarfile.open(fileobj=BytesIO(raw), mode="r:") as source,
                  gzip.GzipFile(filename="", mode="wb", fileobj=file, mtime=0) as compressed,
                  tarfile.open(mode="w|", fileobj=compressed) as bundle):
                for entry in source:
                    entry.mtime = epoch
                    entry.pax_headers.pop("mtime", None)
                    if entry.issym() or (not entry.isfile() and not entry.isdir()):
                        raise ValueError(f"Unsupported release entry: {entry.name}")
                    if entry.isfile():
                        blob = source.extractfile(entry)
                        assert blob is not None
                        data = blob.read()
                        name = entry.name.removeprefix(PREFIX + "/")
                        items[name] = hashlib.sha256(data).hexdigest()
                        bundle.addfile(entry, BytesIO(data))
                    else:
                        bundle.addfile(entry)
                manifest = json.dumps({
                    "schema": "magr-harness-release-v1", "commit": commit, "files": items,
                }, sort_keys=True, indent=2).encode() + b"\n"
                info = tarfile.TarInfo(PREFIX + "/RELEASE.json")
                info.mtime, info.size, info.mode = epoch, len(manifest), 0o644
                bundle.addfile(info, BytesIO(manifest))
            file.flush()
            os.fsync(file.fileno())
            os.link(scratch, output)
        finally:
            scratch.unlink()
    with output.open("rb") as archive:
        checksum = hashlib.file_digest(archive, "sha256").hexdigest()
    return {"commit": commit, "archive": str(output), "sha256": checksum}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--revision", default="HEAD")
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    print(json.dumps(build_release(args.revision, args.output), sort_keys=True))
