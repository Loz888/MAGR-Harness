"""Prepare exact public H00 source pins; never execute project code or judges."""

from __future__ import annotations

import argparse
import configparser
import hashlib
import io
import json
import os
import re
import signal
import stat
import subprocess
import tarfile
import urllib.request
import zlib
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
ALLOWED = {
    "pallets/click",
    "psf/requests",
    "spf13/pflag",
    "expressjs/express",
    "sindresorhus/got",
    "golang/example",
}
SHA = re.compile(r"[a-f0-9]{40}")


def run_owned(command: list[str], environment: dict[str, str], timeout: float) -> dict[str, Any]:
    """Kill only the process group created by this exact Popen on timeout."""
    process = subprocess.Popen(
        command,
        env=environment,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )
    timed_out = False
    try:
        stdout, stderr = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        stdout, stderr = process.communicate(timeout=10)
    return {
        "argv": command,
        "exit_code": process.returncode,
        "timed_out": timed_out,
        "owned_process_group": process.pid,
        "stdout": stdout,
        "stderr": stderr,
    }


def git(target: Path, *arguments: str) -> list[str]:
    return [
        "git",
        "-C",
        str(target),
        "-c",
        "core.hooksPath=/dev/null",
        "-c",
        "core.fsmonitor=false",
        "-c",
        "core.untrackedCache=false",
        "-c",
        "credential.helper=",
        "-c",
        "protocol.file.allow=never",
        "-c",
        "http.version=HTTP/1.1",
        *arguments,
    ]


def download(url: str, maximum: int) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": "magr-h12-source-prepare"})
    with urllib.request.urlopen(request, timeout=45) as response:
        data = response.read(maximum + 1)
        if len(data) > maximum:
            raise ValueError("Public source download exceeds its bound")
        return data


def object_hash(kind: str, content: bytes) -> str:
    return hashlib.sha1(
        kind.encode() + b" " + str(len(content)).encode() + b"\0" + content
    ).hexdigest()


def store_object(target: Path, kind: str, content: bytes) -> str:
    identity = object_hash(kind, content)
    path = target / ".git" / "objects" / identity[:2] / identity[2:]
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        payload = kind.encode() + b" " + str(len(content)).encode() + b"\0" + content
        with path.open("xb") as stream:
            stream.write(zlib.compress(payload))
    return identity


def tree_hash(root: Path, *, objects: Path | None = None) -> str:
    entries = []
    for path in root.iterdir():
        if path.name == ".git":
            continue
        mode = path.lstat().st_mode
        if stat.S_ISLNK(mode):
            content, gitmode = os.readlink(path).encode(), b"120000"
            identity = (
                store_object(objects, "blob", content) if objects else object_hash("blob", content)
            )
        elif stat.S_ISDIR(mode):
            identity, gitmode = tree_hash(path, objects=objects), b"40000"
        elif stat.S_ISREG(mode):
            content = path.read_bytes()
            gitmode = b"100755" if mode & 0o111 else b"100644"
            identity = (
                store_object(objects, "blob", content) if objects else object_hash("blob", content)
            )
        else:
            raise ValueError("Source contains a non-Git file type")
        name = os.fsencode(path.name)
        entries.append(
            (
                name + (b"/" if gitmode == b"40000" else b""),
                gitmode + b" " + name + b"\0" + bytes.fromhex(identity),
            )
        )
    content = b"".join(value for _, value in sorted(entries))
    return store_object(objects, "tree", content) if objects else object_hash("tree", content)


def safe_extract(archive: bytes, target: Path) -> None:
    """Extract bounded GitHub source without any escaping or traversed links."""
    with tarfile.open(fileobj=io.BytesIO(archive), mode="r:gz") as tar:
        members = tar.getmembers()
        if len(members) > 20000 or sum(item.size for item in members) > 150_000_000:
            raise ValueError("Source archive exceeds extraction bounds")
        prefix = None
        plan = []
        for member in members:
            parts = PurePosixPath(member.name).parts
            if not parts or member.name.startswith("/") or ".." in parts:
                raise ValueError("Unsafe source archive path")
            if prefix is None:
                prefix = parts[0]
            if parts[0] != prefix or ".git" in parts:
                raise ValueError("Unexpected source archive root")
            if len(parts) == 1:
                continue
            relative = Path(*parts[1:])
            if not (member.isdir() or member.isfile() or member.issym()):
                raise ValueError("Unsupported source archive entry")
            if member.issym():
                resolved = (target / relative.parent / member.linkname).resolve()
                if not resolved.is_relative_to(target.resolve()):
                    raise ValueError("Source symlink escapes archive")
            plan.append((member, relative))
        links = {relative for member, relative in plan if member.issym()}
        for _, relative in plan:
            if any(parent in links for parent in relative.parents):
                raise ValueError("Source archive traverses its own symlink")
        for member, relative in plan:
            path = target / relative
            if member.isdir():
                path.mkdir(parents=True, exist_ok=True)
            else:
                path.parent.mkdir(parents=True, exist_ok=True)
                if member.issym():
                    path.symlink_to(member.linkname)
                else:
                    source = tar.extractfile(member)
                    assert source is not None
                    with path.open("xb") as stream:
                        stream.write(source.read())
                    path.chmod(0o755 if member.mode & 0o111 else 0o644)


def original_commit(metadata: dict[str, Any], expected: str) -> bytes:
    """Recover signed original bytes only if their exact frozen Git SHA matches."""
    if metadata.get("sha") != expected:
        raise ValueError("GitHub commit identity differs")
    verification = metadata.get("verification", {})
    payload, signature = verification.get("payload"), verification.get("signature")
    if not isinstance(payload, str) or not isinstance(signature, str):
        raise ValueError("Original signed commit bytes are unavailable")
    headers, message = payload.split("\n\n", 1)
    for signed in (signature, signature.rstrip("\n")):
        signed_header = "gpgsig " + signed.replace("\n", "\n ")
        for ending in ("", "\n"):
            content = (headers + "\n" + signed_header + "\n\n" + message + ending).encode()
            if object_hash("commit", content) == expected:
                return content
    raise ValueError("Original commit cannot be reconstructed exactly")


class Preparation:
    def __init__(
        self, cache: Path, output: Path, *, timeout: float = 45, metadata: Path | None = None
    ):
        if cache.absolute() != cache.resolve():
            raise ValueError("Cache root traverses a symlink")
        self.cache, self.output, self.timeout, self.metadata = (
            cache.resolve(),
            output,
            timeout,
            metadata,
        )
        git_home = output / "git-home"
        git_home.mkdir()
        self.environment = {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "HOME": str(git_home),
            "LANG": "C.UTF-8",
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": "/dev/null",
            "GIT_LFS_SKIP_SMUDGE": "1",
            "GIT_OPTIONAL_LOCKS": "0",
        }

    def step(self, record: dict[str, Any], command: list[str]) -> dict[str, Any]:
        result = run_owned(command, self.environment, self.timeout)
        index = len(record["steps"])
        stem = record["repository"].replace("/", "-") + "-" + record["commit"] + f"-{index}"
        for stream in ("stdout", "stderr"):
            (self.output / (stem + "." + stream + ".log")).write_bytes(result[stream])
        record["steps"].append(
            {key: value for key, value in result.items() if key not in {"stdout", "stderr"}}
            | {
                "stdout_log": stem + ".stdout.log",
                "stderr_log": stem + ".stderr.log",
            }
        )
        return result

    def require(self, record: dict[str, Any], command: list[str]) -> bytes:
        result = self.step(record, command)
        if result["timed_out"] or result["exit_code"]:
            raise RuntimeError(
                "owned_process_timeout" if result["timed_out"] else "git_step_failed"
            )
        return result["stdout"]

    def inspect_existing(self, record: dict[str, Any], target: Path) -> bool:
        if target.is_symlink() or (target / ".git").is_symlink() or not (target / ".git").is_dir():
            raise ValueError("Existing cache is not an owned Git directory")
        configuration = target / ".git" / "config"
        if configuration.is_symlink():
            raise ValueError("Cache Git configuration is a symlink")
        parsed = configparser.RawConfigParser()
        parsed.read(configuration)
        permitted = {
            "core": {"repositoryformatversion", "filemode", "bare", "logallrefupdates"},
            'remote "origin"': {"url", "fetch"},
        }
        if any(
            section not in permitted or set(parsed[section]) - permitted[section]
            for section in parsed.sections()
        ):
            raise ValueError("Cache has unsupported Git execution configuration")
        if parsed.get("core", "bare", fallback="true") != "false":
            raise ValueError("Cache is not an owned non-bare checkout")
        origin = self.require(
            record, git(target, "config", "--local", "--get", "remote.origin.url")
        )
        if origin.decode().strip() != "https://github.com/" + record["repository"] + ".git":
            raise ValueError("Existing cache origin differs")
        head = self.step(record, git(target, "rev-parse", "--verify", "HEAD"))
        if head["exit_code"] == 0:
            if head["stdout"].decode().strip() != record["commit"]:
                raise ValueError("Existing cache HEAD differs")
            dirty = self.require(
                record, git(target, "status", "--porcelain=v1", "--untracked-files=all")
            )
            if dirty:
                raise ValueError("Existing cache has working changes")
            return True
        if any(path.name != ".git" for path in target.iterdir()):
            raise ValueError("Incomplete cache contains working files; preserving them")
        quarantine = self.output / "incomplete" / record["repository"] / record["commit"]
        quarantine.parent.mkdir(parents=True, exist_ok=True)
        target.rename(quarantine)
        record["preserved_incomplete_cache"] = str(quarantine)
        return False

    def archive_fallback(self, record: dict[str, Any], target: Path) -> None:
        repo, commit = record["repository"], record["commit"]
        metadata_path = self.metadata / f"{commit}.json" if self.metadata else None
        metadata_bytes = (
            metadata_path.read_bytes()
            if metadata_path and metadata_path.is_file()
            else download(f"https://api.github.com/repos/{repo}/git/commits/{commit}", 1000000)
        )
        metadata = json.loads(metadata_bytes)
        commit_bytes = original_commit(metadata, commit)
        expected_tree = metadata["tree"]["sha"]
        if not SHA.fullmatch(expected_tree):
            raise ValueError("GitHub root tree identity is invalid")
        archive = download(f"https://codeload.github.com/{repo}/tar.gz/{commit}", 40_000_000)
        (self.output / f"{repo.replace('/', '-')}-{commit}.tar.gz").write_bytes(archive)
        (self.output / f"{commit}.github-commit.json").write_bytes(metadata_bytes)
        safe_extract(archive, target)
        observed_tree = tree_hash(target, objects=target)
        if observed_tree != expected_tree:
            raise ValueError("Archive raw Git tree differs from the exact commit tree")
        if store_object(target, "commit", commit_bytes) != commit:
            raise ValueError("Original commit identity differs")
        (target / ".git" / "shallow").write_text(commit + "\n")
        self.require(record, git(target, "update-ref", "HEAD", commit))
        self.require(record, git(target, "read-tree", commit))
        record.update(
            source_transport="github-codeload-exact",
            github_tree_sha=expected_tree,
            archive_sha256=hashlib.sha256(archive).hexdigest(),
            commit_object_verified=True,
        )

    def prepare(self, pin: tuple[str, str]) -> dict[str, Any]:
        repo, commit = pin
        if repo not in ALLOWED or not SHA.fullmatch(commit):
            raise ValueError("Frozen public source identity is invalid")
        target = self.cache / repo / commit
        record: dict[str, Any] = {
            "repository": repo,
            "commit": commit,
            "path": str(target),
            "steps": [],
        }
        try:
            for directory in (
                self.cache,
                self.cache / repo.split("/")[0],
                self.cache / repo,
                target,
            ):
                if directory.is_symlink():
                    raise ValueError("Cache path traverses a symlink")
            ready = target.exists() and self.inspect_existing(record, target)
            if not ready:
                target.mkdir(parents=True)
                self.require(record, ["git", "init", str(target)])
                self.require(
                    record,
                    git(target, "remote", "add", "origin", "https://github.com/" + repo + ".git"),
                )
                fetched = self.step(
                    record,
                    git(
                        target,
                        "fetch",
                        "--no-tags",
                        "--no-recurse-submodules",
                        "--depth=1",
                        "origin",
                        commit,
                    ),
                )
                if fetched["exit_code"] or fetched["timed_out"]:
                    record["git_fetch_failure"] = (
                        "timeout" if fetched["timed_out"] else "exit_nonzero"
                    )
                    self.archive_fallback(record, target)
                else:
                    self.require(record, git(target, "checkout", "--detach", commit))
                    record["source_transport"] = "git-exact-fetch"
            observed = self.require(record, git(target, "rev-parse", "HEAD")).decode().strip()
            expected_tree = (
                self.require(record, git(target, "rev-parse", "HEAD^{tree}")).decode().strip()
            )
            if observed != commit or tree_hash(target) != expected_tree:
                raise ValueError("Checked-out source does not match the frozen Git tree")
            record.update(status="READY", observed_commit=observed, observed_tree=expected_tree)
        except Exception as exc:
            record.update(status="NOT_RUN", reason=type(exc).__name__, detail=str(exc)[:300])
        return record


def pins_only() -> tuple[set[tuple[str, str]], dict[str, str]]:
    """Consume repository/base_commit only; never print goals or judge fields."""
    pins: set[tuple[str, str]] = set()
    manifests = {}
    for split in ("dev", "holdout"):
        content = (ROOT / f"experiments/harness-v4/tasks/{split}.jsonl").read_bytes()
        manifests[split] = hashlib.sha256(content).hexdigest()
        for line in content.decode().splitlines():
            row = json.loads(line)
            pins.add((row["repository"], row["base_commit"]))
    return pins, manifests


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cache", type=Path, default=ROOT / ".local/h12/sources")
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--timeout", type=float, default=45)
    parser.add_argument("--metadata", type=Path)
    args = parser.parse_args()
    if not 1 <= args.timeout <= 120:
        parser.error("timeout must be 1 to 120 seconds")
    args.out.mkdir(parents=True, exist_ok=False)
    pins, manifests = pins_only()
    preparation = Preparation(args.cache, args.out, timeout=args.timeout, metadata=args.metadata)
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(preparation.prepare, sorted(pins)))
    report = {
        "started_scope": "exact public source preparation; no project execution",
        "recorded_at": datetime.now(UTC).isoformat(),
        "frozen_manifests": manifests,
        "pins": results,
        "paid_model_calls": 0,
    }
    (args.out / "sources.json").write_text(json.dumps(report, indent=2) + "\n")
    print(
        json.dumps(
            {
                "pins": len(results),
                "ready": sum(row["status"] == "READY" for row in results),
                "not_run": sum(row["status"] == "NOT_RUN" for row in results),
                "record": str(args.out / "sources.json"),
                "paid_model_calls": 0,
            }
        )
    )


if __name__ == "__main__":
    main()
