"""Prepare an isolated H12 toolchain image from trusted local inputs.

Docker runs only with --build; network runs only with --download-node/go.
No project source or user environment directory enters the build context.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import tomllib
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import urljoin, urlsplit
from uuid import uuid4

import httpx

ROOT = Path(__file__).resolve().parents[1]
BASE_IMAGE = "sha256:efad10932c12f9d6f2408fe960e9a292c4bdbac0f8365387838acbe4f0fdd2b6"
IMAGE_TAG = "mgar-v3-sandbox-h12:local"
NODE_SUMS_URL = "https://nodejs.org/dist/latest-v20.x/SHASUMS256.txt"
GO_MANIFEST_URL = "https://go.dev/dl/?mode=json&include=all"
GO_FILENAME = "go1.22.12.linux-amd64.tar.gz"
SELECTED_DISTRIBUTIONS = (
    "pytest",
    "iniconfig",
    "packaging",
    "pluggy",
    "pygments",
    "urllib3",
    "charset-normalizer",
    "idna",
    "certifi",
)
DOCKER = ("docker", "--host=unix:///var/run/docker.sock")


class PreparationUnavailable(RuntimeError):
    pass


def sha256_file(path: Path) -> str:
    value = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1_048_576), b""):
            value.update(block)
    return value.hexdigest()


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(dir=path.parent, prefix=".pending-")
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(value, stream, sort_keys=True, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


def fetch_bytes(url: str, *, hosts: set[str], limit: int) -> bytes:
    """Bounded HTTPS requests with an explicit redirect-host allowlist."""
    with httpx.Client(timeout=60, follow_redirects=False) as client:
        for _ in range(5):
            parsed = urlsplit(url)
            if (
                parsed.scheme != "https"
                or parsed.hostname not in hosts
                or parsed.port not in (None, 443)
                or parsed.username is not None
                or parsed.password is not None
            ):
                raise PreparationUnavailable("Toolchain URL is outside the official HTTPS hosts")
            with client.stream("GET", url) as response:
                if response.is_redirect:
                    url = urljoin(url, response.headers["location"])
                    continue
                response.raise_for_status()
                content = bytearray()
                for chunk in response.iter_bytes():
                    content.extend(chunk)
                    if len(content) > limit:
                        raise PreparationUnavailable("Toolchain response exceeds its size bound")
                return bytes(content)
    raise PreparationUnavailable("Toolchain response exceeded the redirect bound")


def node_artifact(shasums: str, version: str | None = None) -> tuple[str, str]:
    if version is not None and not re.fullmatch(r"[1-9][0-9]*\.[0-9]+\.[0-9]+", version):
        raise PreparationUnavailable("An exact Node release version is required")
    release = re.escape(version) if version is not None else r"20\.\d+\.\d+"
    matches = re.findall(
        rf"^([a-f0-9]{{64}})\s+(node-v{release}-linux-x64\.tar\.xz)$",
        shasums,
        flags=re.MULTILINE,
    )
    if len(matches) != 1:
        raise PreparationUnavailable(
            "Official Node metadata has no unique selected linux-x64 archive"
        )
    expected, filename = matches[0]
    return filename, expected


def go_artifact(payload: Any) -> tuple[str, str]:
    if not isinstance(payload, list):
        raise PreparationUnavailable("Official Go metadata is not a release list")
    matches = [
        file
        for release in payload
        if isinstance(release, dict) and release.get("version") == "go1.22.12"
        for file in release.get("files", [])
        if isinstance(file, dict)
        and file.get("filename") == GO_FILENAME
        and file.get("os") == "linux"
        and file.get("arch") == "amd64"
        and file.get("kind") == "archive"
    ]
    if len(matches) != 1 or not re.fullmatch(r"[a-f0-9]{64}", matches[0].get("sha256", "")):
        raise PreparationUnavailable(
            "Official Go metadata has no unique Go1.22.12 linux-amd64 archive"
        )
    return GO_FILENAME, str(matches[0]["sha256"])


def verify_archive(path: Path, filename: str, expected: str) -> None:
    if path.name != filename or sha256_file(path) != expected:
        raise PreparationUnavailable("Toolchain archive name or official SHA256 differs")


def unpack_toolchain(archive: Path, destination: Path, root_name: str) -> None:
    """Extract regular files only; validate and skip optional in-tree links."""
    destination.mkdir(parents=True, exist_ok=True)
    total, count = 0, 0
    with tarfile.open(archive) as source:
        for member in source:
            total += member.size
            count += 1
            if total > 1_000_000_000 or count > 100_000:
                raise PreparationUnavailable("Toolchain archive exceeds its extraction bound")
            name = PurePosixPath(member.name)
            if name.is_absolute() or ".." in name.parts or "\\" in member.name:
                raise PreparationUnavailable("Toolchain archive contains an unsafe path")
            if not name.parts or name.parts[0] != root_name:
                raise PreparationUnavailable("Toolchain archive root differs")
            relative = Path(*name.parts[1:])
            if member.issym() or member.islnk():
                link = PurePosixPath(member.linkname)
                if link.is_absolute() or ".." in (name.parent / link).parts[:1]:
                    raise PreparationUnavailable("Toolchain archive link escapes its root")
                resolved = os.path.normpath(str(name.parent / link))
                if (
                    not PurePosixPath(resolved).parts
                    or PurePosixPath(resolved).parts[0] != root_name
                ):
                    raise PreparationUnavailable("Toolchain archive link escapes its root")
                # npm/npx symlinks are recreated explicitly by the Dockerfile.
                continue
            target = destination / relative
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            if not member.isfile() or member.size < 0 or member.size > 200_000_000:
                raise PreparationUnavailable("Toolchain archive contains an unsupported entry")
            data = source.extractfile(member)
            if data is None:
                raise PreparationUnavailable("Toolchain archive entry is unavailable")
            target.parent.mkdir(parents=True, exist_ok=True)
            with data, target.open("xb") as stream:
                shutil.copyfileobj(data, stream)
            target.chmod(0o755 if member.mode & 0o111 else 0o644)


def stage_python(prefix: Path, site_root: Path, destination: Path) -> dict[str, str]:
    prefix, site_root = prefix.resolve(), site_root.resolve()
    if not (prefix / "bin/python3.12").is_file() or not (prefix / "lib/python3.12").is_dir():
        raise PreparationUnavailable("A local standalone Python3.12 installation is required")
    for path in prefix.rglob("*"):
        if path.is_symlink() and not path.resolve().is_relative_to(prefix):
            raise PreparationUnavailable("Standalone Python contains an external symlink")
    shutil.copytree(
        prefix,
        destination,
        symlinks=True,
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "site-packages"),
    )
    target_site = destination / "lib/python3.12/site-packages"
    if target_site.exists():
        shutil.rmtree(target_site)
    target_site.mkdir(parents=True)
    locked = {
        package["name"].lower().replace("_", "-"): package["version"]
        for package in tomllib.loads((ROOT / "uv.lock").read_text())["package"]
        if "version" in package
    }
    versions: dict[str, str] = {}
    for name in SELECTED_DISTRIBUTIONS:
        try:
            distribution = importlib.metadata.distribution(name)
        except importlib.metadata.PackageNotFoundError as exc:
            raise PreparationUnavailable(f"Locked runtime dependency is missing: {name}") from exc
        if distribution.files is None:
            raise PreparationUnavailable(f"Locked runtime dependency has no file manifest: {name}")
        if locked.get(name.lower().replace("_", "-")) != distribution.version:
            raise PreparationUnavailable(
                f"Installed runtime dependency differs from uv.lock: {name}"
            )
        versions[name] = distribution.version
        for entry in distribution.files:
            relative = Path(str(entry))
            if "__pycache__" in relative.parts or relative.suffix == ".pyc":
                continue
            if relative.name == "direct_url.json":
                # Optional installer metadata can contain authenticated URLs.
                continue
            if relative.is_absolute() or ".." in relative.parts:
                # Entry-point scripts are replaced by one fixed local wrapper.
                if str(entry).startswith("../../../bin/"):
                    continue
                raise PreparationUnavailable("Dependency manifest contains an unsafe path")
            source = Path(str(distribution.locate_file(entry))).resolve()
            if not source.is_relative_to(site_root) or not source.is_file():
                raise PreparationUnavailable("Dependency source is outside the locked virtualenv")
            target = target_site / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
    wrapper = destination / "bin/pytest"
    wrapper.write_text(
        "#!/usr/local/bin/python3\nimport pytest\nraise SystemExit(pytest.console_main())\n"
    )
    wrapper.chmod(0o755)
    return versions


def context_manifest(context: Path) -> list[dict[str, str]]:
    files = []
    for path in sorted(context.rglob("*")):
        if path.is_symlink():
            files.append({"path": path.relative_to(context).as_posix(), "link": os.readlink(path)})
        elif path.is_file():
            files.append(
                {"path": path.relative_to(context).as_posix(), "sha256": sha256_file(path)}
            )
    return files


def docker(arguments: list[str], directory: Path) -> subprocess.CompletedProcess[bytes]:
    config = directory / "docker-config"
    config.mkdir(exist_ok=True, mode=0o700)
    return subprocess.run(
        [*DOCKER, *arguments],
        capture_output=True,
        check=False,
        env={
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "DOCKER_CONFIG": str(config),
            "BUILDX_CONFIG": str(config / "buildx"),
        },
    )


def download_toolchain(
    kind: str, directory: Path, *, node_version: str | None = None
) -> tuple[Path, Path]:
    directory = directory / (kind + "-" + uuid4().hex[:12])
    directory.mkdir(parents=True, mode=0o700)
    if kind == "node":
        if node_version is not None and not re.fullmatch(
            r"[1-9][0-9]*\.[0-9]+\.[0-9]+", node_version
        ):
            raise PreparationUnavailable("An exact Node release version is required")
        release_url = (
            "https://nodejs.org/dist/v" + node_version + "/"
            if node_version is not None
            else "https://nodejs.org/dist/latest-v20.x/"
        )
        metadata = fetch_bytes(
            release_url + "SHASUMS256.txt", hosts={"nodejs.org"}, limit=4_000_000
        )
        manifest = directory / "SHASUMS256.txt"
        filename, _ = node_artifact(metadata.decode(), node_version)
        url, hosts = release_url + filename, {"nodejs.org"}
    else:
        metadata = fetch_bytes(GO_MANIFEST_URL, hosts={"go.dev"}, limit=16_000_000)
        manifest = directory / "go-downloads.json"
        filename, _ = go_artifact(json.loads(metadata))
        url, hosts = "https://go.dev/dl/" + filename, {"go.dev", "dl.google.com"}
    manifest.write_bytes(metadata)
    archive = directory / filename
    archive.write_bytes(fetch_bytes(url, hosts=hosts, limit=200_000_000))
    return archive, manifest


def build(context: Path, directory: Path, receipt: dict[str, Any]) -> dict[str, Any]:
    if docker(["image", "inspect", BASE_IMAGE], directory).returncode:
        raise PreparationUnavailable(
            "The frozen local R03 image is unavailable; no pull was attempted"
        )
    iid = context / "image.id"
    # BuildKit interprets a bare sha256:... FROM as a registry image name.
    # Bind a new local tag only after checking the exact existing image id.
    local_base = "mgar-v3-sandbox-h12-base:" + BASE_IMAGE.split(":")[1][:16]
    tagged = docker(["image", "tag", BASE_IMAGE, local_base], directory)
    if tagged.returncode:
        raise PreparationUnavailable("Could not bind the verified local base image")
    result = docker(
        [
            "build",
            "--pull=false",
            "--network=none",
            "--provenance=false",
            "--build-arg",
            "BASE_IMAGE=" + local_base,
            "--iidfile",
            str(iid),
            "--tag",
            IMAGE_TAG,
            "--file",
            str(ROOT / "deploy/Dockerfile.sandbox-h12"),
            str(context),
        ],
        directory,
    )
    (directory / (context.name + "-build.log")).write_bytes(result.stdout + result.stderr)
    if result.returncode:
        raise PreparationUnavailable("H12 offline image build failed; see private build log")
    image_id = iid.read_text().strip()
    if not re.fullmatch(r"sha256:[a-f0-9]{64}", image_id):
        raise PreparationUnavailable("Built H12 image has no pinned image identity")
    probes = {}
    for key, command in {
        "python": ["/usr/local/bin/python3", "-V"],
        "pytest": [
            "/usr/local/bin/python3",
            "-m",
            "pytest",
            "-q",
            "-p",
            "no:cacheprovider",
            "/opt/h12/probe/test_environment.py",
        ],
        "node": ["/usr/local/bin/node", "--version"],
        "npm": ["/usr/local/bin/npm", "--version"],
        "npx": ["/usr/local/bin/npx", "--version"],
        "go": ["/usr/local/bin/go", "version"],
    }.items():
        probe = docker(
            [
                "run",
                "--rm",
                "--pull=never",
                "--network=none",
                "--read-only",
                "--cap-drop=ALL",
                "--security-opt=no-new-privileges",
                "--pids-limit=96",
                "--memory=512m",
                "--cpus=1",
                "--tmpfs",
                "/tmp:rw,nosuid,nodev,size=64m",
                "--entrypoint",
                command[0],
                image_id,
                *command[1:],
            ],
            directory,
        )
        output = (probe.stdout + probe.stderr).decode(errors="replace").strip()
        probes[key] = {
            "status": "PASS" if probe.returncode == 0 else "NOT_RUN",
            "output": output,
            "sha256": hashlib.sha256(probe.stdout + probe.stderr).hexdigest(),
        }
    if (
        probes["python"]["output"] != "Python 3.12.16"
        or probes["pytest"]["status"] != "PASS"
        or "1 passed" not in probes["pytest"]["output"]
    ):
        raise PreparationUnavailable("H12 Python3.12/real pytest probe did not pass")
    if receipt["node"]["status"] == "VERIFIED" and (
        "node-" + probes["node"]["output"] + "-linux-x64.tar.xz" != receipt["node"]["archive"]
    ):
        raise PreparationUnavailable("Prepared Node version differs from the verified archive")
    if receipt["node"]["status"] == "VERIFIED" and any(
        probes[key]["status"] != "PASS" for key in ("npm", "npx")
    ):
        raise PreparationUnavailable("Prepared Node npm/npx probes did not pass")
    if (
        receipt["go"]["status"] == "VERIFIED"
        and "go1.22.12 linux/amd64" not in probes["go"]["output"]
    ):
        raise PreparationUnavailable("Prepared Go version is not Go1.22.12 linux/amd64")
    for name in ("bootstrap", "runner"):
        probe = docker(
            [
                "run",
                "--rm",
                "--pull=never",
                "--network=none",
                "--entrypoint",
                "sha256sum",
                image_id,
                "/opt/mgar/" + name + ".py",
            ],
            directory,
        )
        if probe.returncode or probe.stdout.decode().split()[0] != sha256_file(
            ROOT / "deploy/r03" / (name + ".py")
        ):
            raise PreparationUnavailable("Trusted inherited sandbox file differs")
    return {
        **receipt,
        "status": "PASS_PYTHON_TOOLCHAIN",
        "image": IMAGE_TAG,
        "image_id": image_id,
        "base_image_id": BASE_IMAGE,
        "probes": probes,
        "python_version": probes["python"]["output"],
        "go_version": probes["go"]["output"],
        "node_version": probes["node"]["output"],
        "bootstrap_sha256": sha256_file(ROOT / "deploy/r03/bootstrap.py"),
        "runner_sha256": sha256_file(ROOT / "deploy/r03/runner.py"),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--build", action="store_true")
    parser.add_argument("--directory", type=Path, default=ROOT / ".local/h12")
    parser.add_argument("--node-version", help="Exact trusted Node release; default retains Node20")
    for tool in ("node", "go"):
        parser.add_argument("--" + tool + "-archive", type=Path)
        parser.add_argument("--" + tool + "-metadata", type=Path)
        parser.add_argument("--download-" + tool, action="store_true")
    args = parser.parse_args(argv)
    directory = args.directory.resolve()
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    context = Path(tempfile.mkdtemp(prefix="build-", dir=directory))
    try:
        if sys.version_info[:3] != (3, 12, 16):
            raise PreparationUnavailable(
                "Run this builder with the locked Python3.12.16 interpreter"
            )
        site_root = Path(sys.prefix) / "lib/python3.12/site-packages"
        versions = stage_python(Path(sys.base_prefix), site_root, context / "python")
        receipt: dict[str, Any] = {
            "schema": "h12-sandbox-lock-v1",
            "built_at": datetime.now(UTC).isoformat(),
            "python_packages": versions,
            "uv_lock_sha256": sha256_file(ROOT / "uv.lock"),
        }
        for tool in ("node", "go"):
            archive, metadata = getattr(args, tool + "_archive"), getattr(args, tool + "_metadata")
            if getattr(args, "download_" + tool):
                if archive is not None or metadata is not None:
                    raise PreparationUnavailable(
                        "Choose supplied files or explicit download, not both"
                    )
                if tool == "node" and args.node_version is not None:
                    archive, metadata = download_toolchain(
                        tool, directory / "downloads", node_version=args.node_version
                    )
                else:
                    archive, metadata = download_toolchain(tool, directory / "downloads")
            destination = context / tool
            if archive is None and metadata is None:
                destination.mkdir()
                receipt[tool] = {"status": "NOT_RUN", "reason": "optional toolchain not supplied"}
                continue
            if archive is None or metadata is None:
                raise PreparationUnavailable(
                    "Both archive and official checksum metadata are required"
                )
            filename, expected = (
                node_artifact(metadata.read_text(), args.node_version)
                if tool == "node"
                else (go_artifact(json.loads(metadata.read_text())))
            )
            verify_archive(archive, filename, expected)
            unpack_toolchain(
                archive, destination, filename.removesuffix(".tar.xz") if tool == "node" else "go"
            )
            receipt[tool] = {
                "status": "VERIFIED",
                "archive": filename,
                "sha256": expected,
                "metadata_sha256": sha256_file(metadata),
                "origin": "official_https_download"
                if getattr(args, "download_" + tool)
                else "explicit_supplied_metadata",
            }
        probe = context / "probe"
        probe.mkdir()
        (probe / "test_environment.py").write_text(
            "import sys\nimport pytest\nimport urllib3, charset_normalizer, idna, certifi\n"
            "def test_trusted_environment():\n    assert sys.version_info[:3] == (3, 12, 16)\n"
            "    assert pytest.__version__\n"
        )
        manifest = context_manifest(context)
        receipt["source_sha256"] = hashlib.sha256(
            json.dumps(manifest, sort_keys=True).encode()
            + (ROOT / "deploy/Dockerfile.sandbox-h12").read_bytes()
        ).hexdigest()
        write_json(directory / (context.name + "-manifest.json"), manifest)
        receipt["context"] = str(context)
        receipt = (
            build(context, directory, receipt)
            if args.build
            else {
                **receipt,
                "status": "PREPARED_NOT_BUILT",
            }
        )
        write_json(directory / ("sandbox-lock.json" if args.build else "preparation.json"), receipt)
        print(json.dumps(receipt, sort_keys=True))
        return 0
    except (PreparationUnavailable, OSError, ValueError, httpx.HTTPError, tarfile.TarError) as exc:
        failure = {"status": "NOT_RUN", "reason": str(exc), "context": str(context)}
        write_json(directory / (context.name + "-not-run.json"), failure)
        print(json.dumps(failure, sort_keys=True))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
