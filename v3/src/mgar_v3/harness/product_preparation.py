"""Explicit, offline dependency preparation from an approved wheelhouse.

Only the existing restricted Broker runs pip. The derived image build contains
COPY/ENV instructions only: no project scripts, host installation, network,
credentials, or dependency build hooks. Image selection is the other supported
preparation path, useful for Node/Go and already prepared Python environments.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import tempfile
import zipfile
from pathlib import Path
from typing import Any
from uuid import uuid4

from mgar_v3.domain import DomainError, digest
from mgar_v3.harness.broker import IsolatedExecutionBroker, _docker_environment, issue_lease
from mgar_v3.harness.contracts import CancellationToken, CapabilityGrant, ExecutionRequest
from mgar_v3.harness.product_runtime import (
    discover_product_plan,
    product_readiness,
    validate_product_image_lock,
)
from mgar_v3.harness.workspace import _read_regular_bytes

_SENSITIVE = frozenset({".git", ".env", ".ssh", ".aws", ".config", ".gnupg", ".codex"})
_PACKAGE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]*(?:\[[A-Za-z0-9_,.-]+\])?==[A-Za-z0-9_.+!-]+")


def _outside_project(path: Path, project: Path, *, existing: bool) -> Path:
    original = Path(path).absolute()
    if any(part in _SENSITIVE for part in original.parts):
        raise DomainError("PRODUCT_PREPARATION_PATH", "Sensitive source path is forbidden", 403)
    for ancestor in (original, *original.parents):
        if ancestor.is_symlink():
            raise DomainError("PRODUCT_PREPARATION_PATH", "Symlink paths are forbidden", 403)
    resolved = original.resolve()
    if resolved == project or project in resolved.parents:
        raise DomainError(
            "PRODUCT_PREPARATION_PATH", "Preparation assets must be outside project", 403
        )
    if existing and not resolved.exists():
        raise DomainError("PRODUCT_PREPARATION_PATH", "Preparation asset does not exist", 409)
    return resolved


def _requirements(project: Path) -> str:
    try:
        source = _read_regular_bytes(project, "requirements.txt", 262144).decode("utf-8")
    except (DomainError, UnicodeError, OSError) as exc:
        raise DomainError(
            "PRODUCT_DEPENDENCY_LOCK_REQUIRED", "Provide a hash-pinned requirements.txt", 409
        ) from exc
    joined = source.replace("\\\n", " ")
    requirements = []
    for line in joined.splitlines():
        line = line.split("#", 1)[0].strip()
        if not line:
            continue
        parts = line.split()
        if (
            not _PACKAGE.fullmatch(parts[0]) or len(parts) < 2
            or any(not re.fullmatch(r"--hash=sha256:[a-f0-9]{64}", part) for part in parts[1:])
        ):
            raise DomainError(
                "PRODUCT_DEPENDENCY_LOCK_REQUIRED",
                "Offline preparation requires name==version and SHA256 hashes; "
                "URLs, editable installs, includes and index options are unsupported", 409,
            )
        requirements.append(" ".join(parts))
    if not requirements or len(requirements) > 256:
        raise DomainError("PRODUCT_DEPENDENCY_LOCK_REQUIRED", "Invalid dependency lock size", 409)
    return "\n".join(requirements) + "\n"


def _copy_wheels(source: Path, destination: Path) -> dict[str, str]:
    if not source.is_dir():
        raise DomainError("PRODUCT_WHEELHOUSE_INVALID", "Wheelhouse must be a directory", 422)
    destination.mkdir()
    manifest: dict[str, str] = {}
    compressed = unpacked = 0
    entries = sorted(source.iterdir())
    if not entries or len(entries) > 256:
        raise DomainError("PRODUCT_WHEELHOUSE_INVALID", "Invalid wheelhouse size", 413)
    for entry in entries:
        if entry.is_symlink() or not entry.is_file() or not re.fullmatch(
            r"[A-Za-z0-9_.+!-]+\.whl", entry.name
        ):
            raise DomainError(
                "PRODUCT_WHEELHOUSE_INVALID", "Only regular wheel files are allowed", 403
            )
        fd = os.open(entry, os.O_RDONLY | os.O_NONBLOCK | getattr(os, "O_NOFOLLOW", 0))
        with os.fdopen(fd, "rb") as reader:
            if not stat.S_ISREG(os.fstat(reader.fileno()).st_mode):
                raise DomainError("PRODUCT_WHEELHOUSE_INVALID", "Regular wheels required", 403)
            data = reader.read(64 * 1024 * 1024 + 1)
        compressed += len(data)
        if len(data) > 64 * 1024 * 1024 or compressed > 128 * 1024 * 1024:
            raise DomainError("PRODUCT_WHEELHOUSE_INVALID", "Wheelhouse byte limit exceeded", 413)
        copied = destination / entry.name
        copied.write_bytes(data)
        manifest[entry.name] = hashlib.sha256(data).hexdigest()
        try:
            with zipfile.ZipFile(copied) as wheel:
                seen: set[str] = set()
                for member in wheel.infolist():
                    mode = member.external_attr >> 16
                    if (
                        member.filename in seen or "\\" in member.filename
                        or member.filename.startswith("/") or ".." in Path(member.filename).parts
                        or stat.S_ISLNK(mode)
                    ):
                        raise ValueError("unsafe wheel member")
                    seen.add(member.filename)
                    unpacked += member.file_size
                    if unpacked > 256 * 1024 * 1024:
                        raise ValueError("unpacked wheelhouse limit exceeded")
        except (ValueError, zipfile.BadZipFile) as exc:
            raise DomainError(
                "PRODUCT_WHEELHOUSE_INVALID", "Unsafe or invalid wheel archive", 403
            ) from exc
    return manifest


def _write_lock(path: Path, payload: dict[str, Any]) -> None:
    data = json.dumps(payload, indent=2, sort_keys=True).encode() + b"\n"
    descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    with os.fdopen(descriptor, "wb") as writer:
        writer.write(data)


def prepare_product_environment(
    project_path: Path,
    base_lock: Path,
    *,
    destination_lock: Path,
    wheelhouse: Path | None = None,
    cancellation: CancellationToken | None = None,
) -> dict[str, Any]:
    """Select a pinned image or install hash-pinned wheels in an offline sandbox.

    Callers must authorize the exact project, image and source paths before
    calling. No value here can mint network or paid-model authorization.
    Destination locks are append-only; select a new filename for another image.
    """
    token = cancellation or CancellationToken()
    token.ensure_active()
    original = Path(project_path).absolute()
    if original.is_symlink() or not original.is_dir():
        raise DomainError("PRODUCT_PREPARATION_PATH", "Invalid project root", 403)
    project = original.resolve()
    base_path = _outside_project(base_lock, project, existing=True)
    destination = _outside_project(destination_lock, project, existing=False)
    if destination.exists():
        raise DomainError("PRODUCT_IMAGE_LOCK_EXISTS", "Image locks cannot be overwritten", 409)
    if not destination.parent.is_dir():
        raise DomainError("PRODUCT_PREPARATION_PATH", "Destination directory must exist", 409)
    base = validate_product_image_lock(base_path)
    if wheelhouse is None:
        result = product_readiness(project, base_path)
        payload = {"schema": "harness-product-image-v1", "image_id": base["image_id"],
                   "base_image_id": base["image_id"], "preparation": "pinned-image-selection"}
        token.ensure_active()
        _write_lock(destination, payload)
        return {"status": result["status"], "image_lock": str(destination),
                "image_id": base["image_id"], "readiness": result, "network": "none"}
    wheel_path = _outside_project(wheelhouse, project, existing=True)
    requirements = _requirements(project)
    # This phase handles Python wheels only; Node/Go use a prepared pinned image.
    profile = discover_product_plan({"requirements.txt": requirements})
    assert profile.kind == "python"
    with tempfile.TemporaryDirectory(prefix="product-prep-", dir=destination.parent) as temporary:
        work = Path(temporary)
        work.chmod(0o755)
        wheels = _copy_wheels(wheel_path, work / "wheels")
        (work / "requirements.txt").write_text(requirements)
        target = work / ".harness-tmp"
        target.mkdir(mode=0o777)
        target.chmod(0o777)
        lease = issue_lease(
            mode="modify_test", actor="product-preparation", workspace_id="product-preparation",
            repository="local/preparation", execution_epoch=1, read_paths=("",),
            write_paths=(".harness-tmp",), allowed_commands=("python3",),
            timeout_seconds=120, max_calls=2, memory_mb=512, scratch_mb=512,
            approved_by="operator", approval_ref="offline-wheels-" + digest(wheels),
        )
        grant = CapabilityGrant(
            actor=lease.actor, workspace_id=lease.workspace_id, repository=lease.repository,
            read_paths=lease.read_paths, write_paths=lease.write_paths, execute=True,
            allowed_commands=lease.allowed_commands, max_seconds=120,
        )
        broker = IsolatedExecutionBroker(work, lease, lock_path=base_path)
        installer = (
            "import glob,runpy,sys\n"
            "try: import pip\n"
            "except ImportError:\n"
            " wheels=glob.glob('wheels/pip-*.whl')\n"
            " if len(wheels)!=1: raise RuntimeError('Provide one pip wheel for bootstrap')\n"
            " sys.path.insert(0,wheels[0])\n"
            "sys.argv=['pip','install','--no-index','--only-binary=:all:','--require-hashes',"
            "'--no-compile','--disable-pip-version-check','--no-cache-dir',"
            "'--find-links=wheels','--target=.harness-tmp/python','-r','requirements.txt']\n"
            "runpy.run_module('pip',run_name='__main__')\n"
        )
        receipt = broker.execute(
            ExecutionRequest(
                operation_id="offline-wheels-" + uuid4().hex[:12], purpose="command",
                argv=("python3", "-B", "-c", installer), cwd=".", timeout_seconds=120,
                execution_epoch=1,
            ), grant, token,
        )
        # The sandbox UID owns installed files. Normalize its own result tree
        # before the host copies/removes it; no host chmod/chown or elevated
        # container is used, including for an unsuccessful installation.
        permissions = broker.execute(
            ExecutionRequest(
                operation_id="offline-permissions-" + uuid4().hex[:12], purpose="command",
                argv=("python3", "-B", "-c", (
                    "import os,pathlib\n"
                    "root=pathlib.Path('.harness-tmp/python')\n"
                    "if root.exists() and not root.is_symlink():\n"
                    " os.chmod(root,0o777)\n"
                    " for folder,dirs,files in os.walk(root,followlinks=False):\n"
                    "  for name in dirs+files:\n"
                    "   path=pathlib.Path(folder,name)\n"
                    "   if not path.is_symlink():\n"
                    "    os.chmod(path,0o777 if path.is_dir() else 0o666)\n"
                )), cwd=".", timeout_seconds=15, execution_epoch=1,
            ), grant, CancellationToken(),
        )
        if permissions.status != "passed":
            raise DomainError("PRODUCT_PREPARATION_CLEANUP", "Cannot normalize package files", 409)
        if receipt.status != "passed":
            raise DomainError(
                "PRODUCT_OFFLINE_INSTALL_FAILED", "Offline installation failed: "
                + (receipt.stdout + receipt.stderr)[-2000:], 409,
            )
        token.ensure_active()
        installed = target / "python"
        if installed.is_symlink() or not installed.is_dir():
            raise DomainError(
                "PRODUCT_PREPARATION_PATH", "Invalid installed package directory", 403
            )
        inventory: dict[str, str] = {}
        total = 0
        for folder, dirs, files in os.walk(installed, followlinks=False):
            for name in (*dirs, *files):
                path = Path(folder) / name
                if path.is_symlink():
                    raise DomainError("PRODUCT_PREPARATION_PATH", "Installed symlinks denied", 403)
            for name in files:
                path = Path(folder) / name
                if not stat.S_ISREG(path.stat(follow_symlinks=False).st_mode):
                    raise DomainError("PRODUCT_PREPARATION_PATH", "Special installed file", 403)
                with path.open("rb") as package_file:
                    data = package_file.read(64 * 1024 * 1024 + 1)
                total += len(data)
                if (len(data) > 64 * 1024 * 1024 or total > 256 * 1024 * 1024
                        or len(inventory) >= 20000):
                    raise DomainError(
                        "PRODUCT_PREPARATION_LIMIT", "Installed files exceed limit", 413
                    )
                inventory[path.relative_to(installed).as_posix()] = hashlib.sha256(data).hexdigest()
        if not inventory:
            raise DomainError("PRODUCT_OFFLINE_INSTALL_FAILED", "No installed package files", 409)
        context = work / "image"
        context.mkdir()
        shutil.copytree(installed, context / "python")
        base_tag = "mgar-product-offline-base:" + uuid4().hex
        tagged = subprocess.run(
            ["docker", "image", "tag", base["image_id"], base_tag], env=_docker_environment(),
            capture_output=True, timeout=15, check=False,
        )
        if tagged.returncode:
            raise DomainError("PRODUCT_IMAGE_BUILD_FAILED", "Pinned base image unavailable", 409)
        (context / "Dockerfile").write_text(
            f"FROM {base_tag}\nCOPY python/ /opt/harness-product/python/\n"
            "ENV PYTHONPATH=/opt/harness-product/python\n"
        )
        identity = digest({"base": base["image_id"], "wheels": wheels,
                           "requirements": requirements, "installed": inventory})
        image_output = work / "image-id"
        try:
            built = subprocess.run(
                ["docker", "build", "--network=none", "--pull=false", "--no-cache",
                 "--label", "mgar.product.preparation=" + identity, "--iidfile", str(image_output),
                 str(context)], env=_docker_environment(), capture_output=True, timeout=120,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise DomainError(
                "PRODUCT_IMAGE_BUILD_FAILED", "Offline image assembly failed", 409
            ) from exc
        finally:
            subprocess.run(
                ["docker", "image", "rm", base_tag], env=_docker_environment(),
                capture_output=True, timeout=15, check=False,
            )
        if built.returncode:
            raise DomainError("PRODUCT_IMAGE_BUILD_FAILED", "Offline image assembly failed", 409)
        image_id = image_output.read_text().strip()
        if not re.fullmatch(r"sha256:[a-f0-9]{64}", image_id):
            raise DomainError("PRODUCT_IMAGE_BUILD_FAILED", "Invalid derived image digest", 409)
        payload = {
            "schema": "harness-product-image-v1", "image_id": image_id,
            "base_image_id": base["image_id"], "preparation": "offline-hash-pinned-wheels",
            "source_hash": identity, "wheel_hashes": wheels,
            "requirements_sha256": hashlib.sha256(requirements.encode()).hexdigest(),
            "installed_tree_hash": digest(inventory), "network": "none",
            "preparation_receipt": receipt.receipt_id,
        }
        try:
            token.ensure_active()
            _write_lock(destination, payload)
        except BaseException:
            # This invocation owns the derived image; never leave an orphan
            # after cancellation or a competing immutable-lock creation.
            subprocess.run(
                ["docker", "image", "rm", image_id], env=_docker_environment(),
                capture_output=True, timeout=15, check=False,
            )
            raise
    return {"status": "prepared", "image_lock": str(destination), "image_id": image_id,
            "source_hash": identity, "network": "none", "readiness": product_readiness(
                project, destination
            )}
