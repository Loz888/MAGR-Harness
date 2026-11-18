"""Prepare pinned Node dependencies in disposable containers; never run project hooks.

The source pin is unchanged. Repositories without a lock receive a separately
recorded environment lock before their original ``npm ci --ignore-scripts``.
Only dependency files enter the resulting image, under /node_modules, so ESM
and CommonJS resolve the same packages above a mounted /workspace.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import tempfile
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit
from uuid import uuid4

import build_h12_sandbox as trusted

_PACKAGE_NAME = re.compile(r"(?:@[A-Za-z0-9_-][A-Za-z0-9_.-]*/)?[A-Za-z0-9_-][A-Za-z0-9_.-]*")
_VERSION = r"v?(?:[0-9]+|[xX*])(?:\.(?:[0-9]+|[xX*])){0,2}(?:-[0-9A-Za-z.-]+)?(?:\+[0-9A-Za-z.-]+)?"
_COMPARATOR = rf"(?:[~^]|<=|>=|<|>|=)?\s*{_VERSION}"
_RANGE = re.compile(rf"(?:{_COMPARATOR})(?:\s+{_COMPARATOR})*|{_VERSION}\s+-\s+{_VERSION}")
_DEPENDENCIES = ("dependencies", "devDependencies", "optionalDependencies", "peerDependencies")


def _registry_spec(value: Any, *, allow_alias: bool = False) -> bool:
    if not isinstance(value, str) or not value or len(value) > 512:
        return False
    if allow_alias and value.startswith("npm:"):
        name, separator, version = value[4:].rpartition("@")
        return bool(separator and _PACKAGE_NAME.fullmatch(name) and _registry_spec(version))
    return all(bool(_RANGE.fullmatch(part.strip())) for part in value.split("||"))


def _dependency_map(value: Any, *, allow_alias: bool = False) -> None:
    if not isinstance(value, dict) or any(
        not isinstance(name, str)
        or not _PACKAGE_NAME.fullmatch(name)
        or not _registry_spec(spec, allow_alias=allow_alias)
        for name, spec in value.items()
    ):
        raise trusted.PreparationUnavailable(
            "Only registry semver dependency specifications are approved"
        )


def _overrides(value: Any, *, nested: bool = False, depth: int = 0) -> None:
    if not isinstance(value, dict) or depth > 16:
        raise trusted.PreparationUnavailable("Unsupported Node dependency overrides")
    for selector, replacement in value.items():
        if not isinstance(selector, str):
            raise trusted.PreparationUnavailable("Unsupported Node override selector")
        if selector == ".":
            valid = nested
        else:
            at = selector.rfind("@")
            name, version = (selector[:at], selector[at + 1 :]) if at > 0 else (selector, None)
            valid = bool(_PACKAGE_NAME.fullmatch(name)) and (
                version is None or _registry_spec(version)
            )
        if not valid:
            raise trusted.PreparationUnavailable(
                "Only registry semver override selectors are approved"
            )
        if isinstance(replacement, dict) and selector != ".":
            _overrides(replacement, nested=True, depth=depth + 1)
        elif not _registry_spec(replacement):
            raise trusted.PreparationUnavailable(
                "Only registry semver dependency overrides are approved"
            )


def validate_manifest(package: Any) -> None:
    """Reject source-changing npm entries before a lock-resolution network phase.

    This is static input validation, not a domain-enforced network boundary.
    Only the manifest and lock are staged; repository .npmrc/shrinkwrap files
    and lifecycle scripts never gain execution authority.
    """
    if not isinstance(package, dict):
        raise trusted.PreparationUnavailable("A Node dependency manifest object is required")
    for key in _DEPENDENCIES:
        if key in package:
            _dependency_map(package[key])
    if "overrides" in package:
        _overrides(package["overrides"])
    for key in ("workspaces", "bundleDependencies", "bundledDependencies", "resolutions"):
        if package.get(key):
            raise trusted.PreparationUnavailable(
                "Unsupported Node dependency resolution entry: " + key
            )


def _locked_source(record: dict[str, Any]) -> None:
    try:
        resolved = urlsplit(record.get("resolved", ""))
        approved = (
            resolved.scheme == "https"
            and resolved.hostname == "registry.npmjs.org"
            and not resolved.username
            and not resolved.password
            and resolved.port in (None, 443)
            and resolved.path.endswith(".tgz")
            and not resolved.query
            and not resolved.fragment
            and not record.get("link")
            and bool(re.fullmatch(r"sha(?:1|512)-[A-Za-z0-9+/=]+", record.get("integrity", "")))
        )
    except (ValueError, TypeError, AttributeError):
        approved = False
    if not approved:
        raise trusted.PreparationUnavailable("Node lock contains an unapproved source or identity")


def _legacy_dependencies(value: Any, *, depth: int = 0) -> None:
    if not isinstance(value, dict) or depth > 32:
        raise trusted.PreparationUnavailable("Unsupported legacy Node dependency lock")
    for name, record in value.items():
        if (
            not isinstance(name, str)
            or not _PACKAGE_NAME.fullmatch(name)
            or not isinstance(record, dict)
        ):
            raise trusted.PreparationUnavailable(
                "Node lock contains an unapproved source or identity"
            )
        _locked_source(record)
        if "requires" in record:
            _dependency_map(record["requires"], allow_alias=True)
        if "dependencies" in record:
            _legacy_dependencies(record["dependencies"], depth=depth + 1)


def plan(source: Path, lock_path: Path) -> dict[str, Any]:
    if source.is_symlink() or not source.is_dir() or (source / "package.json").is_symlink():
        raise trusted.PreparationUnavailable("A public source directory is required")
    package = json.loads((source / "package.json").read_text())
    validate_manifest(package)
    if package.get("name") not in {"express", "got"}:
        raise trusted.PreparationUnavailable(
            "Only pinned public Express/Got preparation is supported"
        )
    lock = json.loads(lock_path.read_text())
    node_version = str(lock.get("node_version", ""))
    parsed_version = re.fullmatch(r"v([1-9][0-9]*)\.[0-9]+\.[0-9]+", node_version)
    minimum = 20 if package["name"] == "got" else 14
    if (
        not re.fullmatch(r"sha256:[a-f0-9]{64}", str(lock.get("image_id", "")))
        or parsed_version is None
        or int(parsed_version[1]) < minimum
    ):
        raise trusted.PreparationUnavailable("A compatible probed Node H12 base lock is required")
    original = source / "package-lock.json"
    if original.is_symlink():
        raise trusted.PreparationUnavailable("Original Node lock is a symlink")
    original_lock = (
        validate_lock(
            original, trusted.sha256_file(source / "package.json"), source / "package.json"
        )
        if original.is_file()
        else None
    )
    return {
        "project": package["name"],
        "package_sha256": trusted.sha256_file(source / "package.json"),
        "has_original_lock": original.is_file(),
        "original_lock_sha256": original_lock["lock_sha256"] if original_lock else None,
        "base_lock_sha256": trusted.sha256_file(lock_path),
        "base_lock": lock,
        "base_image_id": lock["image_id"],
        "original_prepare": ["npm", "ci", "--ignore-scripts"],
    }


def validate_lock(path: Path, package_sha256: str, package_path: Path) -> dict[str, Any]:
    if trusted.sha256_file(package_path) != package_sha256:
        raise trusted.PreparationUnavailable("Package manifest changed during preparation")
    validate_manifest(json.loads(package_path.read_text()))
    if path.is_symlink():
        raise trusted.PreparationUnavailable("Node environment lock is a symlink")
    lock = json.loads(path.read_text())
    if lock.get("lockfileVersion") not in {2, 3} or not isinstance(lock.get("packages"), dict):
        raise trusted.PreparationUnavailable("The Node environment lock is unavailable")
    packages = lock["packages"]
    if "" not in packages or len(packages) > 3000:
        raise trusted.PreparationUnavailable("Node dependency resolution exceeds the package bound")
    for name, record in packages.items():
        if not isinstance(name, str) or not isinstance(record, dict):
            raise trusted.PreparationUnavailable(
                "Node lock contains an unapproved source or identity"
            )
        for key in _DEPENDENCIES:
            if key in record:
                _dependency_map(record[key], allow_alias=bool(name))
        if not name:
            continue
        if (
            not name.startswith("node_modules/")
            or ".." in Path(name).parts
            or "\\" in name
            or "\x00" in name
        ):
            raise trusted.PreparationUnavailable(
                "Node lock contains an unapproved source or identity"
            )
        _locked_source(record)
    if "dependencies" in lock:
        _legacy_dependencies(lock["dependencies"])
    return {"lock_sha256": trusted.sha256_file(path), "dependency_count": len(packages) - 1}


def prepare(source: Path, out: Path, metadata: dict[str, Any]) -> dict[str, Any]:
    context = Path(tempfile.mkdtemp(prefix="node-build-", dir=out))
    stage = context / "environment"
    stage.mkdir(mode=0o777)
    stage.chmod(0o777)
    shutil.copyfile(source / "package.json", stage / "package.json")
    if metadata["has_original_lock"]:
        original = source / "package-lock.json"
        if original.is_symlink():
            raise trusted.PreparationUnavailable("Original Node lock is a symlink")
        shutil.copyfile(original, stage / "package-lock.json")
    try:
        if trusted.sha256_file(stage / "package.json") != metadata["package_sha256"]:
            raise trusted.PreparationUnavailable("Package manifest changed during preparation")
        validate_manifest(json.loads((stage / "package.json").read_text()))
        if metadata["has_original_lock"]:
            validated = validate_lock(
                stage / "package-lock.json", metadata["package_sha256"], stage / "package.json"
            )
            if validated["lock_sha256"] != metadata.get("original_lock_sha256"):
                raise trusted.PreparationUnavailable(
                    "Original Node lock changed during preparation"
                )
    except (trusted.PreparationUnavailable, OSError, ValueError, KeyError) as exc:
        trusted.write_json(
            out / f"{context.name}-validation.json",
            {
                "status": "NOT_RUN",
                "phase": "before_dependency_network",
                "reason": str(exc),
                "docker_dispatched": False,
            },
        )
        raise
    commands = []
    if not metadata["has_original_lock"]:
        commands.append(["npm", "install", "--package-lock-only", "--ignore-scripts"])
    commands.append(["npm", "ci", "--ignore-scripts"])
    attempts = []
    for index, argv in enumerate(commands):
        result = trusted.docker(
            [
                "run",
                "--rm",
                "--pull=never",
                "--read-only",
                "--user=65532:65532",
                "--cap-drop=ALL",
                "--security-opt=no-new-privileges",
                "--pids-limit=256",
                "--memory=1536m",
                "--cpus=2",
                "--tmpfs",
                "/tmp:rw,nosuid,nodev,size=128m",
                "--label",
                "mgar.h12.prepare=" + uuid4().hex,
                "--mount",
                f"type=bind,source={stage},target=/prepare",
                "--workdir=/prepare",
                "--env=HOME=/tmp",
                "--env=NPM_CONFIG_REGISTRY=https://registry.npmjs.org",
                "--env=NPM_CONFIG_CACHE=/prepare/npm-cache",
                "--env=NPM_CONFIG_AUDIT=false",
                "--env=NPM_CONFIG_FUND=false",
                "--env=NPM_CONFIG_FETCH_RETRIES=1",
                "--env=NPM_CONFIG_FETCH_TIMEOUT=60000",
                "--env=NPM_CONFIG_IGNORE_SCRIPTS=true",
                "--entrypoint=" + argv[0],
                metadata["base_image_id"],
                *argv[1:],
            ],
            out,
        )
        log = out / f"{context.name}-npm-{index}.log"
        log.write_bytes(result.stdout + result.stderr)
        attempts.append(
            {
                "argv": argv,
                "exit_code": result.returncode,
                "log": str(log),
                "log_sha256": trusted.sha256_file(log),
            }
        )
        trusted.write_json(out / f"{context.name}-attempts.json", attempts)
        if result.returncode:
            raise trusted.PreparationUnavailable(
                "Isolated registry dependency preparation failed; see retained log"
            )
        validate_lock(
            stage / "package-lock.json", metadata["package_sha256"], stage / "package.json"
        )
    frozen = validate_lock(
        stage / "package-lock.json", metadata["package_sha256"], stage / "package.json"
    )
    shutil.copyfile(stage / "package-lock.json", out / f"{context.name}-package-lock.json")
    dockerfile = context / "Dockerfile"
    (context / ".dockerignore").write_text("environment/npm-cache\n")
    dockerfile.write_text(
        "ARG BASE_IMAGE\nFROM ${BASE_IMAGE}\nUSER root\n"
        "COPY environment/node_modules /node_modules\n"
        "ENV PATH=/node_modules/.bin:/usr/local/bin:/usr/local/go/bin:/usr/bin:/bin "
        "NODE_PATH=/node_modules NPM_CONFIG_OFFLINE=true NPM_CONFIG_IGNORE_SCRIPTS=true "
        "NPM_CONFIG_CACHE=/tmp/npm-cache NPM_CONFIG_AUDIT=false NPM_CONFIG_FUND=false\n"
        "WORKDIR /workspace\nUSER 65532:65532\n"
    )
    base_id = metadata["base_image_id"]
    base_tag = "mgar-h12-node-base:" + base_id.split(":")[1][:16]
    tagged = trusted.docker(["image", "tag", base_id, base_tag], out)
    if tagged.returncode:
        raise trusted.PreparationUnavailable("Pinned Node base image is unavailable")
    tag = "mgar-h12-node-" + hashlib.sha256(str(out).encode()).hexdigest()[:16] + ":local"
    image_path = context / "image.id"
    result = trusted.docker(
        [
            "build",
            "--pull=false",
            "--network=none",
            "--provenance=false",
            "--build-arg",
            "BASE_IMAGE=" + base_tag,
            "--iidfile",
            str(image_path),
            "--tag",
            tag,
            "--file",
            str(dockerfile),
            str(context),
        ],
        out,
    )
    log = out / f"{context.name}-build.log"
    log.write_bytes(result.stdout + result.stderr)
    if result.returncode:
        raise trusted.PreparationUnavailable(
            "Offline Node dependency image build failed; see retained log"
        )
    image = image_path.read_text().strip()
    if not re.fullmatch(r"sha256:[a-f0-9]{64}", image):
        raise trusted.PreparationUnavailable("Node dependency image identity is unavailable")
    lock = {
        **metadata["base_lock"],
        "schema": "h12-project-sandbox-lock-v1",
        "image": tag,
        "image_id": image,
        "node_preparation": {
            **{k: v for k, v in metadata.items() if k != "base_lock"},
            **frozen,
            "attempts": attempts,
            "environment_lock": str(out / f"{context.name}-package-lock.json"),
            "source_code_in_image": False,
            "deviation": "A separate registry resolution lock was frozen before npm ci"
            if not metadata["has_original_lock"]
            else None,
        },
    }
    trusted.write_json(out / "sandbox-lock.json", lock)
    return {
        "status": "PASS_PREPARED",
        "execution_readiness": "NOT_CHECKED",
        "project": metadata["project"],
        "image_lock": str(out / "sandbox-lock.json"),
        "image_id": image,
    }


def probe_image(
    image: str, out: Path, project: str, *, environment_revision: str = "h12-dev-environment-v3"
) -> dict[str, Any]:
    commands = [["node", "--version"]]
    commands.extend(
        [["node", "/node_modules/mocha/bin/mocha.js", "--version"]]
        if project == "express"
        else [
            ["node", "/node_modules/typescript/bin/tsc", "--version"],
            ["node", "/node_modules/ava/entrypoints/cli.mjs", "--version"],
            ["node", "--import=tsx/esm", "-e", "console.log('tsx-loader-ready')"],
            [
                "node",
                "--input-type=module",
                "-e",
                "await import('/node_modules/xo/lib/options-manager.js');"
                "console.log('xo-dependency-graph-ready')",
            ],
        ]
    )
    probes = []
    for index, command in enumerate(commands):
        result = trusted.docker(
            [
                "run",
                "--rm",
                "--pull=never",
                "--network=none",
                "--read-only",
                "--cap-drop=ALL",
                "--security-opt=no-new-privileges",
                "--user=65532:65532",
                "--pids-limit=64",
                "--memory=256m",
                "--cpus=1",
                "--ulimit=nofile=1024:1024",
                "--tmpfs",
                "/tmp:rw,nosuid,nodev,size=128m",
                "--env=HOME=/tmp",
                "--env=PATH=/usr/local/go/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
                "--workdir=/workspace",
                "--entrypoint=" + command[0],
                image,
                *command[1:],
            ],
            out,
        )
        log = out / f"offline-probe-{index}.log"
        log.write_bytes(result.stdout + result.stderr)
        probes.append(
            {
                "argv": command,
                "exit_code": result.returncode,
                "log": str(log),
                "log_sha256": trusted.sha256_file(log),
            }
        )
    report = {
        "status": "PASS" if all(p["exit_code"] == 0 for p in probes) else "NOT_RUN",
        "image_id": image,
        "execution_readiness": "NOT_CHECKED_REQUIRES_FULL_DEV_MATRIX",
        "environment_revision": environment_revision,
        "sandbox_nofile": 1024,
        "probes": probes,
    }
    trusted.write_json(out / "offline-probe.json", report)
    return report


def derive_ci_environment(
    metadata: dict[str, Any], out: Path, *, serial: bool = False
) -> dict[str, Any]:
    """Derive only trusted AVA CI settings from an existing exact Got image."""
    original = metadata["base_lock"]
    preparation = original.get("node_preparation", {})
    if (
        metadata["project"] != "got"
        or preparation.get("project") != "got"
        or preparation.get("package_sha256") != metadata["package_sha256"]
    ):
        raise trusted.PreparationUnavailable("An exact prepared Got dependency lock is required")
    context = Path(tempfile.mkdtemp(prefix="got-ci-", dir=out))
    base = metadata["base_image_id"]
    base_tag = "mgar-h12-got-ci-base:" + base.split(":")[1][:16]
    if trusted.docker(["image", "tag", base, base_tag], out).returncode:
        raise trusted.PreparationUnavailable("Prepared Got image is unavailable")
    dockerfile = context / "Dockerfile"
    runtime = {"CI": "true", "AVA_FORCE_CI": "1", "GOMAXPROCS": "1"}
    if serial:
        runtime["UV_THREADPOOL_SIZE"] = "1"
        (context / "ava-config.mjs").write_text(
            "import fs from 'node:fs';\nimport path from 'node:path';\n"
            "export default ({projectDir}) => {\n"
            "  const {ava} = JSON.parse(fs.readFileSync("
            "path.join(projectDir, 'package.json'), 'utf8'));\n"
            "  if (!ava || typeof ava !== 'object' || Array.isArray(ava)) "
            "throw new Error('Original AVA configuration required');\n"
            "  return {...ava, cache: false, concurrency: 1};\n};\n"
        )
    dockerfile.write_text(
        "ARG BASE_IMAGE\nFROM ${BASE_IMAGE}\nENV "
        + " ".join(key + "=" + value for key, value in runtime.items())
        + "\n"
        + ("COPY ava-config.mjs /opt/h12/ava-config.mjs\n" if serial else "")
    )
    iid = context / "image.id"
    tag = "mgar-h12-got-ci:" + uuid4().hex[:16]
    result = trusted.docker(
        [
            "build",
            "--pull=false",
            "--network=none",
            "--provenance=false",
            "--build-arg",
            "BASE_IMAGE=" + base_tag,
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
    log = context / "build.log"
    log.write_bytes(result.stdout + result.stderr)
    if result.returncode:
        raise trusted.PreparationUnavailable("Offline Got CI derivation failed; see retained log")
    image = iid.read_text().strip()
    if not re.fullmatch(r"sha256:[a-f0-9]{64}", image):
        raise trusted.PreparationUnavailable("Derived Got image identity is unavailable")
    trusted.write_json(
        out / "sandbox-lock.json",
        {
            **original,
            "image": tag,
            "image_id": image,
            "environment_revision": "h12-dev-environment-v4"
            if serial
            else "h12-dev-environment-v3",
            "runtime_environment": runtime,
            **(
                {
                    "ava_configuration": {
                        "path": "/opt/h12/ava-config.mjs",
                        "sha256": trusted.sha256_file(context / "ava-config.mjs"),
                        "inherits": "original package.json ava object",
                        "overrides": {"cache": False, "concurrency": 1},
                        "fixed_public_command": [
                            "npm",
                            "test",
                            "--",
                            "--config=/opt/h12/ava-config.mjs",
                        ],
                    }
                }
                if serial
                else {}
            ),
            "derivation": {
                "base_image_id": base,
                "base_lock_sha256": metadata["base_lock_sha256"],
                "build_log_sha256": trusted.sha256_file(log),
                "source_code_in_image": False,
            },
        },
    )
    return {
        "status": "PASS_PREPARED",
        "execution_readiness": "NOT_CHECKED",
        "image_id": image,
        "image_lock": str(out / "sandbox-lock.json"),
    }


def derive_node_toolchain(
    metadata: dict[str, Any], out: Path, version: str, archive: Path, sums: Path
) -> dict[str, Any]:
    """Replace only a verified toolchain; retain the exact existing dependency layer."""
    original = metadata["base_lock"]
    preparation = original.get("node_preparation", {})
    if (
        preparation.get("project") != metadata["project"]
        or preparation.get("package_sha256") != metadata["package_sha256"]
        or not preparation.get("lock_sha256")
    ):
        raise trusted.PreparationUnavailable("An exact prepared dependency lock is required")
    filename, expected = trusted.node_artifact(sums.read_text(), version)
    trusted.verify_archive(archive, filename, expected)
    context = Path(tempfile.mkdtemp(prefix="node-toolchain-", dir=out))
    trusted.unpack_toolchain(archive, context / "node", filename.removesuffix(".tar.xz"))
    base = metadata["base_image_id"]
    base_tag = "mgar-h12-node-pinned-base:" + base.split(":")[1][:16]
    if trusted.docker(["image", "tag", base, base_tag], out).returncode:
        raise trusted.PreparationUnavailable("Prepared dependency image is unavailable")
    dockerfile = context / "Dockerfile"
    dockerfile.write_text(
        "ARG BASE_IMAGE\nFROM ${BASE_IMAGE}\nUSER root\n"
        "RUN rm -rf /opt/h12/node\nCOPY node /opt/h12/node\nUSER 65532:65532\n"
    )
    iid = context / "image.id"
    tag = "mgar-h12-node-pinned:" + uuid4().hex[:16]
    result = trusted.docker(
        [
            "build",
            "--pull=false",
            "--network=none",
            "--provenance=false",
            "--build-arg",
            "BASE_IMAGE=" + base_tag,
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
    log = context / "build.log"
    log.write_bytes(result.stdout + result.stderr)
    if result.returncode:
        raise trusted.PreparationUnavailable("Offline Node derivation failed; see retained log")
    image = iid.read_text().strip()
    if not re.fullmatch(r"sha256:[a-f0-9]{64}", image):
        raise trusted.PreparationUnavailable("Derived Node image identity is unavailable")
    versions = {}
    for tool in ("node", "npm"):
        probe = trusted.docker(
            [
                "run",
                "--rm",
                "--pull=never",
                "--network=none",
                "--read-only",
                "--cap-drop=ALL",
                "--security-opt=no-new-privileges",
                "--pids-limit=64",
                "--memory=256m",
                "--cpus=1",
                "--entrypoint=" + tool,
                image,
                "--version",
            ],
            out,
        )
        (context / (tool + "-version.log")).write_bytes(probe.stdout + probe.stderr)
        if probe.returncode:
            raise trusted.PreparationUnavailable("Derived Node/npm version probe failed")
        versions[tool] = probe.stdout.decode().strip()
    if versions["node"] != "v" + version:
        raise trusted.PreparationUnavailable("Derived Node version differs from supplied archive")
    trusted.write_json(
        out / "sandbox-lock.json",
        {
            **original,
            "image": tag,
            "image_id": image,
            "node_version": versions["node"],
            "npm_version": versions["npm"],
            "node": {
                "status": "VERIFIED",
                "archive": filename,
                "sha256": expected,
                "metadata_sha256": trusted.sha256_file(sums),
                "origin": "explicit_supplied_official_metadata",
            },
            "environment_revision": "h12-dev-environment-v4",
            "toolchain_derivation": {
                "base_image_id": base,
                "base_lock_sha256": metadata["base_lock_sha256"],
                "build_log_sha256": trusted.sha256_file(log),
                "dependency_lock_sha256": preparation["lock_sha256"],
                "dependency_resolution_rerun": False,
                "source_code_in_image": False,
            },
        },
    )
    return {
        "status": "PASS_PREPARED",
        "execution_readiness": "NOT_CHECKED",
        "image_id": image,
        "image_lock": str(out / "sandbox-lock.json"),
        "versions": versions,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--base-lock", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--prepare", action="store_true")
    parser.add_argument("--probe-only", action="store_true")
    parser.add_argument("--derive-ci", action="store_true")
    parser.add_argument("--derive-serial", action="store_true")
    parser.add_argument("--derive-node-version")
    parser.add_argument("--node-archive", type=Path)
    parser.add_argument("--node-metadata", type=Path)
    args = parser.parse_args(argv)
    out = args.out.resolve()
    out.mkdir(parents=True, exist_ok=True)
    try:
        if args.derive_serial and not args.derive_ci:
            raise trusted.PreparationUnavailable("Serial runtime settings require CI derivation")
        if args.derive_node_version and (
            args.node_archive is None
            or args.node_metadata is None
            or args.derive_ci
            or args.prepare
            or args.probe_only
        ):
            raise trusted.PreparationUnavailable(
                "Node derivation needs exact archive/metadata and no other preparation mode"
            )
        if not args.derive_node_version and (
            args.node_archive is not None or args.node_metadata is not None
        ):
            raise trusted.PreparationUnavailable("Node archive options require explicit derivation")
        if args.probe_only:
            lock = json.loads((out / "sandbox-lock.json").read_text())
            image = lock["image_id"]
            if not re.fullmatch(r"sha256:[a-f0-9]{64}", image):
                raise trusted.PreparationUnavailable("Prepared image identity is unavailable")
            report = probe_image(
                image,
                out,
                lock["node_preparation"]["project"],
                environment_revision=lock.get("environment_revision", "h12-dev-environment-v3"),
            )
            print(json.dumps(report))
            return 0 if report["status"] == "PASS" else 2
        metadata = plan(args.source.resolve(), args.base_lock)
        trusted.write_json(
            out / "preflight.json", {k: v for k, v in metadata.items() if k != "base_lock"}
        )
        result = (
            derive_node_toolchain(
                metadata, out, args.derive_node_version, args.node_archive, args.node_metadata
            )
            if args.derive_node_version
            else derive_ci_environment(metadata, out, serial=args.derive_serial)
            if args.derive_ci
            else (
                prepare(args.source.resolve(), out, metadata)
                if args.prepare
                else {
                    "status": "PREFLIGHT_ONLY",
                    "image_lock": None,
                }
            )
        )
        trusted.write_json(out / "result.json", result)
        print(json.dumps(result))
        return 0
    except (trusted.PreparationUnavailable, OSError, ValueError, KeyError) as exc:
        result = {"status": "NOT_RUN", "image_lock": None, "reason": str(exc)}
        trusted.write_json(out / "result.json", result)
        print(json.dumps(result))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
