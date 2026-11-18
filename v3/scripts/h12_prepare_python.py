"""Opt-in, offline-image preparation for pinned public Click/Requests sources.

Default: metadata/preflight only.  --download permits official PyPI wheel GETs;
--build permits an isolated Docker build with network disabled.  Project setup
code is never executed on the host, and no project package is downloaded.
"""

from __future__ import annotations

import argparse
import ast
import configparser
import hashlib
import json
import os
import re
import shutil
import stat
import tempfile
import tomllib
import zipfile
from pathlib import Path, PurePosixPath
from typing import Any
from uuid import uuid4

import build_h12_sandbox as trusted
import httpx
from packaging.markers import default_environment
from packaging.requirements import Requirement
from packaging.specifiers import SpecifierSet
from packaging.utils import canonicalize_name, parse_wheel_filename
from packaging.version import InvalidVersion, Version

ALLOWLIST = {
    "pip",
    "setuptools",
    "wheel",
    "build",
    "flit-core",
    "hatchling",
    "hatch-vcs",
    "setuptools-scm",
    "vcs-versioning",
    "editables",
    "pathspec",
    "trove-classifiers",
    "packaging",
    "pyproject-hooks",
    "typing-extensions",
    "tomli",
    "pytest",
    "pytest-xdist",
    "execnet",
    "iniconfig",
    "pluggy",
    "pygments",
    "colorama",
    "pytest-httpbin",
    "httpbin",
    "pytest-cov",
    "pytest-mock",
    "trustme",
    "pysocks",
    "flask",
    "werkzeug",
    "jinja2",
    "markupsafe",
    "itsdangerous",
    "blinker",
    "flasgger",
    "pyyaml",
    "jsonschema",
    "mistune",
    "six",
    "decorator",
    "attrs",
    "jsonschema-specifications",
    "referencing",
    "rpds-py",
    "coverage",
    "cryptography",
    "cffi",
    "pycparser",
    "brotli",
    "brotlicffi",
    "greenlet",
    "idna",
    "certifi",
    "urllib3",
    "charset-normalizer",
}
PROJECTS = {"click", "requests"}
ROOTS = ("pip", "setuptools", "wheel", "build")
EXCLUDE = {
    ".git",
    ".hg",
    ".svn",
    ".venv",
    ".mgar",
    "node_modules",
    "__pycache__",
    ".pytest_cache",
    "verification",
    "hidden",
}


def record_result(output: Path, result: dict[str, Any]) -> None:
    trusted.write_json(output / ("attempt-" + uuid4().hex + ".json"), result)
    trusted.write_json(output / "result.json", result)


def preflight(source: Path, base_lock: Path, *, test_dependencies: bool = False) -> dict[str, Any]:
    if source.is_symlink() or not source.is_dir():
        raise trusted.PreparationUnavailable("Public source directory is unavailable")
    lock = json.loads(base_lock.read_text())
    if (
        not re.fullmatch(r"sha256:[a-f0-9]{64}", str(lock.get("image_id", "")))
        or lock.get("python_version") != "Python 3.12.16"
        or lock.get("schema") != "h12-sandbox-lock-v1"
    ):
        raise trusted.PreparationUnavailable("A probed Python3.12.16 H12 base lock is required")
    project, backend, requires = "", "setuptools.build_meta", ["setuptools", "wheel"]
    pyproject = source / "pyproject.toml"
    if pyproject.is_file():
        document = tomllib.loads(pyproject.read_text())
        project = str(document.get("project", {}).get("name", ""))
        build = document.get("build-system", {})
        backend = str(build.get("build-backend", backend))
        requires = list(build.get("requires", requires))
        if build.get("backend-path"):
            raise trusted.PreparationUnavailable("Project-local build backends are not approved")
    if not project and (source / "setup.cfg").is_file():
        config = configparser.ConfigParser()
        config.read(source / "setup.cfg")
        project = config.get("metadata", "name", fallback="")
    if not project and (source / "setup.py").is_file():
        for node in ast.walk(ast.parse((source / "setup.py").read_text())):
            if isinstance(node, ast.Call):
                for keyword in node.keywords:
                    if keyword.arg == "name" and isinstance(keyword.value, ast.Constant):
                        project = str(keyword.value.value)
    project = canonicalize_name(project)
    if project not in PROJECTS:
        reason = (
            "Node dependencies are NOT_RUN"
            if (source / "package.json").is_file()
            else ("Only pinned public Click/Requests preparation is supported")
        )
        raise trusted.PreparationUnavailable(reason)
    if backend not in {
        "flit_core.buildapi",
        "setuptools.build_meta",
        "setuptools.build_meta:__legacy__",
        "hatchling.build",
    }:
        raise trusted.PreparationUnavailable("Build backend is outside the explicit allowlist")
    requirements = [*ROOTS, *requires]
    test_inputs: dict[str, str] = {}
    if test_dependencies:
        if project != "requests":
            raise trusted.PreparationUnavailable(
                "Explicit public test preparation supports Requests only"
            )
        dev = source / "requirements-dev.txt"
        if not dev.is_file() or dev.is_symlink():
            raise trusted.PreparationUnavailable(
                "Original public test requirements are unavailable"
            )
        test_inputs["requirements-dev.txt"] = trusted.sha256_file(dev)
        for line in dev.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if line == "-e .[socks]":
                requirements.append("PySocks>=1.5.6,!=1.5.7")
            elif line.startswith("-"):
                raise trusted.PreparationUnavailable(
                    "Unsupported public test requirement directive"
                )
            else:
                requirements.append(line)
    for requirement in requirements:
        parsed = Requirement(requirement)
        if parsed.url or canonicalize_name(parsed.name) not in ALLOWLIST:
            raise trusted.PreparationUnavailable(
                "Build dependency is outside the explicit allowlist"
            )
    return {
        "project": project,
        "backend": backend,
        "requirements": requirements,
        "test_dependency_inputs": test_inputs,
        "pytest_plugins": ["pytest_httpbin.plugin"] if test_dependencies else [],
        "base_image_id": lock["image_id"],
        "base_lock_sha256": trusted.sha256_file(base_lock),
        "provided_packages": lock.get("python_packages", {}),
        "base_lock": lock,
        "original_prepare": ["python", "-m", "pip", "install", "-e", ".", "--no-deps"],
        "executed_prepare": [
            "/usr/local/bin/python3",
            "-m",
            "pip",
            "install",
            "-e",
            ".",
            "--no-deps",
            "--no-build-isolation",
            "--no-index",
            "--no-cache-dir",
            "--break-system-packages",
        ],
    }


def _metadata(name: str, version: str | None = None) -> tuple[dict[str, Any], bytes, str]:
    url = "https://pypi.org/pypi/" + name + ("/" + version if version else "") + "/json"
    raw = trusted.fetch_bytes(url, hosts={"pypi.org"}, limit=16_000_000)
    return json.loads(raw), raw, url


def _compatible_wheel(file: dict[str, Any]) -> bool:
    if file.get("yanked") or file.get("packagetype") != "bdist_wheel":
        return False
    if not SpecifierSet(file.get("requires_python") or "").contains("3.12.16"):
        return False
    try:
        _, _, _, tags = parse_wheel_filename(file["filename"])
    except (ValueError, KeyError):
        return False
    for tag in tags:
        interpreter = tag.interpreter in {"py3", "py2.py3", "cp312"} or (
            tag.abi == "abi3" and tag.interpreter in {"cp38", "cp39", "cp310", "cp311"}
        )
        abi = tag.abi in {"none", "abi3", "cp312"}
        platform = tag.platform == "any" or tag.platform == "manylinux2014_x86_64"
        match = re.fullmatch(r"manylinux_2_([0-9]+)_x86_64", tag.platform)
        platform = platform or bool(match and 17 <= int(match[1]) <= 36)
        if interpreter and abi and platform:
            return True
    return False


def resolve_download(plan: dict[str, Any], output: Path) -> dict[str, Any]:
    """Resolve only allowlisted, pure wheels; never run an sdist/setup hook."""
    output.mkdir(parents=True, exist_ok=True, mode=0o700)
    provided = {
        canonicalize_name(name): version for name, version in plan["provided_packages"].items()
    }
    constraints: dict[str, list[Requirement]] = {}
    selected: dict[str, Any] = {}
    queue = list(plan["requirements"])
    environment: dict[str, str] = {
        **{str(key): str(value) for key, value in default_environment().items()},
        "python_version": "3.12",
        "python_full_version": "3.12.16",
        "sys_platform": "linux",
        "platform_system": "Linux",
        "extra": "",
    }
    while queue:
        if len(selected) > 40 or sum(map(len, constraints.values())) > 200:
            raise trusted.PreparationUnavailable("Dependency resolution exceeds its fixed bounds")
        requirement = Requirement(queue.pop(0))
        if requirement.marker and not requirement.marker.evaluate(environment):
            continue
        name = canonicalize_name(requirement.name)
        allowed_dependency_click = (
            plan.get("project") == "requests"
            and bool(plan.get("test_dependency_inputs"))
            and name == "click"
        )
        if (
            requirement.url
            or (name not in ALLOWLIST and not allowed_dependency_click)
            or (name in PROJECTS and not allowed_dependency_click)
        ):
            raise trusted.PreparationUnavailable(
                f"Transitive dependency is outside the explicit allowlist: {name}"
            )
        constraints.setdefault(name, []).append(requirement)
        if name in provided and all(
            req.specifier.contains(provided[name]) for req in constraints[name]
        ):
            continue
        if name in selected:
            if not all(
                req.specifier.contains(selected[name]["version"]) for req in constraints[name]
            ):
                raise trusted.PreparationUnavailable(
                    "Dependency constraints require a new frozen resolution"
                )
            continue
        index, index_raw, index_url = _metadata(name)
        versions = []
        for version, files in index.get("releases", {}).items():
            try:
                parsed = Version(version)
            except InvalidVersion:
                continue
            if parsed.is_prerelease or not all(
                req.specifier.contains(parsed) for req in constraints[name]
            ):
                continue
            if any(_compatible_wheel(file) for file in files):
                versions.append(parsed)
        if not versions:
            raise trusted.PreparationUnavailable(
                "No approved Python3.12 pure wheel satisfies requirements"
            )
        version = str(max(versions))
        metadata, raw, metadata_url = _metadata(name, version)
        files = sorted(
            (file for file in metadata.get("urls", []) if _compatible_wheel(file)),
            key=lambda file: file["filename"],
        )
        if not files:
            raise trusted.PreparationUnavailable("Version metadata has no compatible wheel")
        file = files[0]
        expected = str(file.get("digests", {}).get("sha256", ""))
        if not re.fullmatch(r"[a-f0-9]{64}", expected):
            raise trusted.PreparationUnavailable("Official wheel SHA256 is unavailable")
        content = trusted.fetch_bytes(
            file["url"], hosts={"files.pythonhosted.org"}, limit=100_000_000
        )
        if hashlib.sha256(content).hexdigest() != expected:
            raise trusted.PreparationUnavailable("Official wheel SHA256 differs")
        wheel = output / file["filename"]
        if Path(file["filename"]).name != file["filename"]:
            raise trusted.PreparationUnavailable("Wheel filename is unsafe")
        wheel.write_bytes(content)
        (output / (name + "-index.json")).write_bytes(index_raw)
        (output / (name + "-metadata.json")).write_bytes(raw)
        selected[name] = {
            "name": name,
            "version": version,
            "wheel": wheel.name,
            "sha256": expected,
            "metadata_sha256": hashlib.sha256(raw).hexdigest(),
            "metadata_url": metadata_url,
            "index_url": index_url,
            "index_sha256": hashlib.sha256(index_raw).hexdigest(),
        }
        extras = {"", *requirement.extras}
        for dependency in metadata["info"].get("requires_dist") or []:
            dependency_requirement = Requirement(dependency)
            if dependency_requirement.marker is None or any(
                dependency_requirement.marker.evaluate({**environment, "extra": extra})
                for extra in extras
            ):
                queue.append(str(dependency_requirement).split(";", 1)[0])
    return {
        "schema": "h12-python-tools-v1",
        "project": plan.get("project"),
        "requirements": plan["requirements"],
        "base_lock_sha256": plan["base_lock_sha256"],
        "wheels": list(selected.values()),
        "provided_by_base": provided,
    }


def extract_wheels(manifest: dict[str, Any], wheels: Path, target: Path) -> None:
    target.mkdir(parents=True)
    for file in manifest["wheels"]:
        name = canonicalize_name(file["name"])
        allowed_dependency_click = manifest.get("project") == "requests" and name == "click"
        if (
            (name not in ALLOWLIST and not allowed_dependency_click)
            or (name in PROJECTS and not allowed_dependency_click)
            or Path(file["wheel"]).name != file["wheel"]
        ):
            raise trusted.PreparationUnavailable("Cached wheel is outside the explicit allowlist")
        path = wheels / file["wheel"]
        wheel_name, wheel_version, _, _ = parse_wheel_filename(file["wheel"])
        if canonicalize_name(wheel_name) != name or str(wheel_version) != file["version"]:
            raise trusted.PreparationUnavailable("Cached wheel identity differs")
        if trusted.sha256_file(path) != file["sha256"]:
            raise trusted.PreparationUnavailable("Cached wheel checksum differs")
        with zipfile.ZipFile(path) as archive:
            if (
                len(archive.infolist()) > 50000
                or sum(member.file_size for member in archive.infolist()) > 200_000_000
            ):
                raise trusted.PreparationUnavailable("Cached wheel exceeds extraction bounds")
            for member in archive.infolist():
                relative = PurePosixPath(member.filename)
                if (
                    relative.is_absolute()
                    or ".." in relative.parts
                    or "\\" in member.filename
                    or stat.S_ISLNK(member.external_attr >> 16)
                ):
                    raise trusted.PreparationUnavailable("Wheel contains an unsafe path or link")
                if member.is_dir():
                    continue
                if any(part.endswith(".data") for part in relative.parts):
                    continue
                destination = target / str(relative)
                destination.parent.mkdir(parents=True, exist_ok=True)
                content = archive.read(member)
                if destination.exists() and destination.read_bytes() != content:
                    raise trusted.PreparationUnavailable(
                        "Selected wheels contain conflicting files"
                    )
                destination.write_bytes(content)


def reuse_verified_wheels(plan: dict[str, Any], cached: Path, target: Path) -> dict[str, Any]:
    """Reuse a recorded official resolution only when all new roots still fit."""
    manifest = json.loads((cached.parent / "python-tools.json").read_text())
    if (
        manifest["base_lock_sha256"] != plan["base_lock_sha256"]
        or manifest.get("project") != plan["project"]
    ):
        raise trusted.PreparationUnavailable(
            "Cached dependencies belong to another base or project"
        )
    versions = {
        **manifest["provided_by_base"],
        **{file["name"]: file["version"] for file in manifest["wheels"]},
    }
    for value in plan["requirements"]:
        requirement = Requirement(value)
        name = canonicalize_name(requirement.name)
        if (
            requirement.url
            or name not in versions
            or not requirement.specifier.contains(versions[name])
        ):
            raise trusted.PreparationUnavailable(
                "Cached version does not satisfy public requirements"
            )
    temporary = Path(tempfile.mkdtemp(prefix="h12-wheel-validation-"))
    try:
        extract_wheels(manifest, cached, temporary / "site")
    finally:
        shutil.rmtree(temporary)
    target.mkdir(mode=0o700)
    for file in manifest["wheels"]:
        for name, expected in (
            (file["wheel"], file["sha256"]),
            (file["name"] + "-metadata.json", file["metadata_sha256"]),
            (file["name"] + "-index.json", file["index_sha256"]),
        ):
            path = cached / name
            if path.is_symlink() or trusted.sha256_file(path) != expected:
                raise trusted.PreparationUnavailable("Recorded dependency cache identity changed")
            shutil.copyfile(path, target / name)
    return {
        **manifest,
        "requirements": plan["requirements"],
        "reused_resolution_sha256": trusted.sha256_file(cached.parent / "python-tools.json"),
    }


def copy_public_source(
    source: Path, target: Path, *, omit_tests_from_build: bool = False
) -> list[dict[str, str]]:
    target.mkdir()
    total = 0
    manifest: list[dict[str, str]] = []
    for directory, subdirs, files in os.walk(source, followlinks=False):
        subdirs[:] = sorted(
            name
            for name in subdirs
            if name not in EXCLUDE
            and not name.startswith(".")
            and not (omit_tests_from_build and name in {"test", "tests"})
        )
        for filename in sorted(files):
            path = Path(directory) / filename
            relative = path.relative_to(source)
            if (
                filename.startswith(".env")
                or filename.startswith("hidden-")
                or filename.startswith("hidden_")
                or "_hidden" in filename
                or filename.endswith(".pyc")
                or any(part in EXCLUDE for part in relative.parts)
            ):
                continue
            if path.is_symlink() or not path.is_file():
                raise trusted.PreparationUnavailable("Public source contains unsupported links")
            total += path.stat().st_size
            if total > 64_000_000 or len(manifest) >= 10000:
                raise trusted.PreparationUnavailable("Public source exceeds its preparation bound")
            destination = target / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, destination)
            manifest.append({"path": relative.as_posix(), "sha256": trusted.sha256_file(path)})
    return manifest


def cleanup_editable(site: Path, workspace: Path, project: str) -> None:
    """Trusted image-only cleanup; tests use temporary synthetic directories."""
    if (
        project not in {"click", "requests"}
        or workspace == Path("/")
        or site.is_relative_to(workspace)
    ):
        raise RuntimeError("Unsafe editable cleanup scope")
    for path in site.iterdir():
        if (
            path.name.startswith("__editable__")
            or path.name in {project, project + ".py"}
            or path.name == project + ".egg-link"
        ):
            shutil.rmtree(path) if path.is_dir() and not path.is_symlink() else path.unlink()
        elif path.suffix == ".pth" and "/workspace" in path.read_text(errors="replace"):
            path.unlink()
    (site / "h12-project-source.pth").write_text("/workspace/src\n")
    shutil.rmtree(workspace)
    workspace.mkdir(mode=0o755)


def build_project(
    source: Path, out: Path, plan: dict[str, Any], manifest: dict[str, Any]
) -> dict[str, Any]:
    if (
        manifest["requirements"] != plan["requirements"]
        or manifest["base_lock_sha256"] != plan["base_lock_sha256"]
    ):
        raise trusted.PreparationUnavailable(
            "Cached preparation belongs to another base or requirements"
        )
    context = Path(tempfile.mkdtemp(prefix="project-build-", dir=out))
    extract_wheels(manifest, out / "wheels", context / "python-tools")
    sources = copy_public_source(
        source, context / "source", omit_tests_from_build=bool(plan.get("test_dependency_inputs"))
    )
    project = plan["project"]
    # Select by the trusted function name rather than a live code object's
    # line number: a long dependency download can outlive an editor save.
    module = ast.parse(Path(__file__).read_text())
    cleanup = next(
        node
        for node in module.body
        if isinstance(node, ast.FunctionDef) and node.name == "cleanup_editable"
    )
    finalizer = "from pathlib import Path\nimport shutil\n" + ast.unparse(cleanup) + "\n"
    finalizer += (
        "\ncleanup_editable(Path('/opt/h12/python/lib/python3.12/site-packages'), "
        f"Path('/workspace'), {project!r})\n"
    )
    (context / "finalize.py").write_text(finalizer)
    dockerfile = context / "Dockerfile"
    dockerfile.write_text(
        "ARG BASE_IMAGE\nFROM ${BASE_IMAGE}\nUSER root\n"
        "COPY python-tools /opt/h12/python/lib/python3.12/site-packages/\n"
        "COPY source /workspace\nCOPY finalize.py /opt/h12/prepare/finalize.py\n"
        "WORKDIR /workspace\nENV PIP_NO_INDEX=1 PIP_DISABLE_PIP_VERSION_CHECK=1\n"
        + ("ENV PYTEST_PLUGINS=pytest_httpbin.plugin\n" if plan.get("pytest_plugins") else "")
        + "RUN /usr/local/bin/python3 -m pip install -e . --no-deps "
        "--no-build-isolation --no-index --no-cache-dir --break-system-packages "
        "&& /usr/local/bin/python3 /opt/h12/prepare/finalize.py\nUSER 65532:65532\n"
    )
    tag = "mgar-h12-project-" + hashlib.sha256(str(out).encode()).hexdigest()[:16] + ":local"
    iid = context / "image.id"
    base_id = plan["base_image_id"]
    inspected = trusted.docker(["image", "inspect", "--format", "{{.Id}}", base_id], out)
    if inspected.returncode or inspected.stdout.decode().strip() != base_id:
        raise trusted.PreparationUnavailable("Pinned project base image is unavailable or differs")
    local_base = "mgar-h12-project-base:" + base_id.split(":")[1][:16]
    tagged = trusted.docker(["image", "tag", base_id, local_base], out)
    if tagged.returncode:
        raise trusted.PreparationUnavailable("Could not bind the verified local project base")
    result = trusted.docker(
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
            tag,
            "--file",
            str(dockerfile),
            str(context),
        ],
        out,
    )
    log = out / (context.name + "-prepare.log")
    log.write_bytes(result.stdout + result.stderr)
    receipt = {
        "project": project,
        "prepare_exit_code": result.returncode,
        "prepare_log": str(log),
        "prepare_log_sha256": trusted.sha256_file(log),
        "source_files": sources,
        "test_dependency_inputs": plan.get("test_dependency_inputs", {}),
        "pytest_plugins": plan.get("pytest_plugins", []),
        "project_tests_in_image": False
        if plan.get("test_dependency_inputs")
        else "removed_after_build",
        "base_image_id": plan["base_image_id"],
        "build_base_tag": local_base,
        "base_lock_sha256": plan["base_lock_sha256"],
        "requirements": plan["requirements"],
        "wheels": manifest["wheels"],
        "original_prepare": plan["original_prepare"],
        "executed_prepare": plan["executed_prepare"],
    }
    trusted.write_json(out / (context.name + "-receipt.json"), receipt)
    if result.returncode:
        raise trusted.PreparationUnavailable(
            "Offline isolated project preparation failed; see retained log"
        )
    image = iid.read_text().strip()
    if not re.fullmatch(r"sha256:[a-f0-9]{64}", image):
        raise trusted.PreparationUnavailable("Prepared project image identity is not pinned")
    body = (
        "import importlib.util,json,pathlib,importlib.metadata;"
        f"assert importlib.util.find_spec({project!r}) is None;"
        "assert not list(pathlib.Path('/workspace').iterdir());"
        "assert pathlib.Path('/opt/h12/python/lib/python3.12/site-packages/"
        "h12-project-source.pth').read_text().splitlines()==['/workspace/src'];"
        f"print(json.dumps({{'project':{project!r},'version':importlib.metadata.version({project!r}),'source_importable':False}}))"
    )
    probe = trusted.docker(
        [
            "run",
            "--rm",
            "--pull=never",
            "--network=none",
            "--read-only",
            "--entrypoint",
            "/usr/local/bin/python3",
            image,
            "-c",
            body,
        ],
        out,
    )
    (out / (context.name + "-probe.log")).write_bytes(probe.stdout + probe.stderr)
    if probe.returncode:
        raise trusted.PreparationUnavailable(
            "Prepared image retains old code or lacks editable metadata"
        )
    lock = {
        **plan["base_lock"],
        "schema": "h12-project-sandbox-lock-v1",
        "image": tag,
        "image_id": image,
        "python_preparation": receipt,
        "python_packages": {
            **plan["base_lock"].get("python_packages", {}),
            **{file["name"]: file["version"] for file in manifest["wheels"]},
        },
        "editable_probe": json.loads(probe.stdout),
        "source_sha256": hashlib.sha256(json.dumps(receipt, sort_keys=True).encode()).hexdigest(),
    }
    trusted.write_json(out / "sandbox-lock.json", lock)
    return {
        "status": "PASS_PREPARED",
        "execution_readiness": "NOT_CHECKED",
        "project": project,
        "image_lock": str(out / "sandbox-lock.json"),
        "image_id": image,
        "prepare_receipt": str(out / (context.name + "-receipt.json")),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--base-lock", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--download", action="store_true")
    parser.add_argument("--build", action="store_true")
    parser.add_argument("--test-dependencies", action="store_true")
    parser.add_argument("--reuse-wheels", type=Path)
    args = parser.parse_args(argv)
    out = args.out.resolve()
    out.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        plan = preflight(
            args.source.resolve(), args.base_lock, test_dependencies=args.test_dependencies
        )
        trusted.write_json(
            out / "preflight.json",
            {key: value for key, value in plan.items() if key != "base_lock"},
        )
        if args.reuse_wheels:
            manifest = reuse_verified_wheels(plan, args.reuse_wheels, out / "wheels")
            trusted.write_json(out / "python-tools.json", manifest)
        elif args.download:
            manifest = resolve_download(plan, out / "wheels")
            trusted.write_json(out / "python-tools.json", manifest)
        elif args.build:
            if not (out / "python-tools.json").is_file():
                raise trusted.PreparationUnavailable(
                    "Offline wheels are absent; explicit --download required"
                )
            manifest = json.loads((out / "python-tools.json").read_text())
        if args.build:
            result = build_project(args.source.resolve(), out, plan, manifest)
        else:
            result = {
                "status": "DEPENDENCIES_CACHED" if args.download else "PREFLIGHT_ONLY",
                "project": plan["project"],
                "image_lock": None,
            }
        record_result(out, result)
        print(json.dumps(result, sort_keys=True))
        return 0
    except (
        trusted.PreparationUnavailable,
        OSError,
        ValueError,
        KeyError,
        httpx.HTTPError,
        zipfile.BadZipFile,
        configparser.Error,
        SyntaxError,
    ) as exc:
        result = {"status": "NOT_RUN", "reason": str(exc), "image_lock": None}
        record_result(out, result)
        print(json.dumps(result, sort_keys=True))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
