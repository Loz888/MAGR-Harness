"""Pure preparation tests: no network, Docker, project hooks or source imports."""

from __future__ import annotations

import hashlib
import io
import json
import runpy
import subprocess
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def prep(monkeypatch):
    monkeypatch.syspath_prepend(str(ROOT / "scripts"))
    return runpy.run_path(str(ROOT / "scripts/h12_prepare_python.py"), run_name="h12_prepare_test")


def _inputs(tmp_path):
    source = tmp_path / "public-source"
    source.mkdir()
    (source / "pyproject.toml").write_text(
        '[project]\nname="click"\nversion="8.0"\n'
        '[build-system]\nrequires=["flit_core>=3.2,<4"]\nbuild-backend="flit_core.buildapi"\n'
    )
    (source / "src/click").mkdir(parents=True)
    (source / "src/click/__init__.py").write_text("VALUE = 'original'\n")
    lock = tmp_path / "base-lock.json"
    lock.write_text(
        json.dumps(
            {
                "schema": "h12-sandbox-lock-v1",
                "image_id": "sha256:" + "a" * 64,
                "python_version": "Python 3.12.16",
                "python_packages": {"pytest": "9.2.0"},
            }
        )
    )
    return source, lock


def _wheel(files):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, content in files.items():
            archive.writestr(name, content)
    return buffer.getvalue()


def test_default_preflight_never_calls_network_docker_or_project_hooks(prep, tmp_path, monkeypatch):
    source, lock = _inputs(tmp_path)

    def forbidden(*args, **kwargs):
        raise AssertionError("Implicit effects are forbidden")

    module = prep["main"].__globals__
    monkeypatch.setitem(module, "resolve_download", forbidden)
    monkeypatch.setitem(module, "build_project", forbidden)
    assert (
        prep["main"](
            ["--source", str(source), "--base-lock", str(lock), "--out", str(tmp_path / "out")]
        )
        == 0
    )
    result = json.loads((tmp_path / "out/result.json").read_text())
    assert result["status"] == "PREFLIGHT_ONLY" and result["image_lock"] is None


def test_setup_metadata_is_parsed_without_executing_setup(prep, tmp_path):
    source, lock = _inputs(tmp_path)
    (source / "pyproject.toml").unlink()
    marker = tmp_path / "must-not-exist"
    (source / "setup.py").write_text(
        "from pathlib import Path\n"
        f"Path({str(marker)!r}).write_text('executed')\nsetup(name='Click')\n"
    )
    assert prep["preflight"](source, lock)["project"] == "click"
    assert not marker.exists()


def test_unapproved_backend_or_dependency_fails_before_download(prep, tmp_path):
    source, lock = _inputs(tmp_path)
    (source / "pyproject.toml").write_text(
        '[project]\nname="click"\n[build-system]\nrequires=["unapproved-package"]\n'
        'build-backend="flit_core.buildapi"\n'
    )
    with pytest.raises(prep["trusted"].PreparationUnavailable, match="allowlist"):
        prep["preflight"](source, lock)


def test_node_preparation_is_explicitly_not_run(prep, tmp_path):
    source, lock = _inputs(tmp_path)
    (source / "pyproject.toml").unlink()
    (source / "package.json").write_text('{"name":"express"}')
    with pytest.raises(prep["trusted"].PreparationUnavailable, match="Node dependencies"):
        prep["preflight"](source, lock)


def test_resolver_records_exact_official_wheel_and_metadata_hashes(prep, tmp_path, monkeypatch):
    wheel = _wheel({"flit_core/__init__.py": "VERSION = '3.10.1'"})
    digest = hashlib.sha256(wheel).hexdigest()
    file = {
        "filename": "flit_core-3.10.1-py3-none-any.whl",
        "packagetype": "bdist_wheel",
        "requires_python": ">=3.8",
        "url": "https://files.pythonhosted.org/flit.whl",
        "digests": {"sha256": digest},
    }

    def metadata(name, version=None):
        data = (
            {"releases": {"3.10.1": [file]}}
            if version is None
            else {
                "urls": [file],
                "info": {"requires_dist": ["packaging>=23"]},
            }
        )
        return data, json.dumps(data).encode(), "https://pypi.org/pypi/flit-core/json"

    module = prep["resolve_download"].__globals__
    monkeypatch.setitem(module, "_metadata", metadata)
    monkeypatch.setattr(prep["trusted"], "fetch_bytes", lambda *args, **kwargs: wheel)
    manifest = prep["resolve_download"](
        {
            "requirements": ["flit_core>=3.2,<4"],
            "provided_packages": {"packaging": "26.3"},
            "base_lock_sha256": "b" * 64,
        },
        tmp_path / "wheels",
    )
    assert len(manifest["wheels"]) == 1
    assert manifest["wheels"][0]["version"] == "3.10.1"
    assert manifest["wheels"][0]["sha256"] == digest
    assert (tmp_path / "wheels/flit-core-metadata.json").is_file()


def test_resolver_never_downloads_another_copy_of_the_project(prep, tmp_path):
    with pytest.raises(prep["trusted"].PreparationUnavailable, match="allowlist"):
        prep["resolve_download"](
            {"requirements": ["click"], "provided_packages": {}, "base_lock_sha256": "a" * 64},
            tmp_path / "wheels",
        )


def test_offline_wheel_checksum_and_zip_traversal_are_rejected(prep, tmp_path):
    wheels = tmp_path / "wheels"
    wheels.mkdir()
    path = wheels / "pip-1.0-py3-none-any.whl"
    path.write_bytes(_wheel({"../outside": "bad"}))
    manifest = {
        "wheels": [
            {
                "name": "pip",
                "version": "1.0",
                "wheel": path.name,
                "sha256": prep["trusted"].sha256_file(path),
            }
        ]
    }
    with pytest.raises(prep["trusted"].PreparationUnavailable, match="unsafe"):
        prep["extract_wheels"](manifest, wheels, tmp_path / "tools")
    assert not (tmp_path / "outside").exists()
    manifest["wheels"][0]["sha256"] = "a" * 64
    with pytest.raises(prep["trusted"].PreparationUnavailable, match="checksum"):
        prep["extract_wheels"](manifest, wheels, tmp_path / "tools-two")


def test_public_source_copy_excludes_credentials_and_oracles(prep, tmp_path):
    source, _ = _inputs(tmp_path)
    (source / ".env").write_text("not copied")
    (source / "verification").mkdir()
    (source / "verification/hidden.py").write_text("not copied")
    (source / "test_hidden.py").write_text("not copied")
    target = tmp_path / "copy"
    prep["copy_public_source"](source, target)
    assert (target / "src/click/__init__.py").exists()
    assert not (target / ".env").exists()
    assert not (target / "verification").exists()
    assert not (target / "test_hidden.py").exists()


def test_editable_cleanup_removes_old_code_and_finder_and_preserves_metadata(prep, tmp_path):
    site, workspace = tmp_path / "site", tmp_path / "workspace"
    site.mkdir()
    workspace.mkdir()
    (workspace / "old.py").write_text("old source")
    (site / "click").mkdir()
    (site / "click/__init__.py").write_text("baked source")
    (site / "click-8.dist-info").mkdir()
    (site / "__editable__click.pth").write_text("import old_finder")
    (site / "__editable__click_finder.py").write_text("old mapping")
    prep["cleanup_editable"](site, workspace, "click")
    assert list(workspace.iterdir()) == []
    assert not (site / "click").exists()
    assert not (site / "__editable__click_finder.py").exists()
    assert (site / "click-8.dist-info").is_dir()
    assert (site / "h12-project-source.pth").read_text() == "/workspace/src\n"


def test_explicit_build_is_offline_and_failure_keeps_exit_code_log(prep, tmp_path, monkeypatch):
    source, lock = _inputs(tmp_path)
    out = tmp_path / "out"
    out.mkdir()
    (out / "wheels").mkdir()
    plan = prep["preflight"](source, lock)
    manifest = {
        "requirements": plan["requirements"],
        "base_lock_sha256": plan["base_lock_sha256"],
        "wheels": [],
    }
    commands = []

    def docker(command, directory):
        commands.append(command)
        if command[:2] == ["image", "inspect"]:
            return subprocess.CompletedProcess(
                command, 0, (plan["base_image_id"] + "\n").encode(), b""
            )
        if command[:2] == ["image", "tag"]:
            return subprocess.CompletedProcess(command, 0, b"", b"")
        return subprocess.CompletedProcess(command, 42, b"offline build failed", b"no network")

    monkeypatch.setattr(prep["trusted"], "docker", docker)
    with pytest.raises(prep["trusted"].PreparationUnavailable, match="retained log"):
        prep["build_project"](source, out, plan, manifest)
    build = next(command for command in commands if command[0] == "build")
    assert "--network=none" in build and "--pull=false" in build
    assert "BASE_IMAGE=mgar-h12-project-base:" + "a" * 16 in build
    receipt = json.loads(next(out.glob("*-receipt.json")).read_text())
    assert receipt["prepare_exit_code"] == 42 and receipt["prepare_log_sha256"]
    dockerfile = next(out.glob("project-build-*/Dockerfile")).read_text()
    assert "--no-build-isolation" in dockerfile
    # uv's standalone Python keeps its PEP668 marker in the base image. The
    # explicit override belongs only to the disposable, offline Docker build.
    assert "--break-system-packages" in dockerfile
    assert "--break-system-packages" in receipt["executed_prepare"]
    assert not (out / "sandbox-lock.json").exists()


@pytest.fixture
def node_prep(monkeypatch):
    monkeypatch.syspath_prepend(str(ROOT / "scripts"))
    return runpy.run_path(str(ROOT / "scripts/h12_prepare_node.py"), run_name="h12_node_test")


def test_node_default_preflight_never_resolves_or_executes_project_hooks(
    node_prep, tmp_path, monkeypatch
):
    source = tmp_path / "source"
    source.mkdir()
    (source / "package.json").write_text(
        json.dumps(
            {
                "name": "express",
                "dependencies": {"debug": "^2.6.9"},
                "scripts": {"prepare": "write-host-secret"},
            }
        )
    )
    lock = tmp_path / "base.json"
    lock.write_text(json.dumps({"image_id": "sha256:" + "b" * 64, "node_version": "v20.20.2"}))

    def forbidden(*args, **kwargs):
        raise AssertionError("Effects require explicit --prepare")

    monkeypatch.setitem(node_prep["main"].__globals__, "prepare", forbidden)
    assert (
        node_prep["main"](
            [
                "--source",
                str(source),
                "--base-lock",
                str(lock),
                "--out",
                str(tmp_path / "out"),
            ]
        )
        == 0
    )
    assert not (tmp_path / "out/sandbox-lock.json").exists()
    package = json.loads((source / "package.json").read_text())
    package["dependencies"]["debug"] = "git+https://unapproved.invalid/source.git"
    (source / "package.json").write_text(json.dumps(package))
    with pytest.raises(node_prep["trusted"].PreparationUnavailable, match="semver"):
        node_prep["plan"](source, lock)


@pytest.mark.parametrize(
    "change",
    [
        {"resolved": "https://evil.invalid/pkg.tgz"},
        {"link": True},
        {"integrity": ""},
        {"resolved": "http://registry.npmjs.org/pkg.tgz"},
    ],
)
def test_node_environment_lock_rejects_unapproved_sources_and_missing_integrity(
    node_prep, tmp_path, change
):
    package = tmp_path / "package.json"
    package.write_text('{"name":"express"}')
    dependency = {
        "resolved": "https://registry.npmjs.org/pkg/-/pkg-1.0.0.tgz",
        "integrity": "sha512-YWJjZA==",
        **change,
    }
    lock = tmp_path / "package-lock.json"
    lock.write_text(
        json.dumps({"lockfileVersion": 3, "packages": {"": {}, "node_modules/pkg": dependency}})
    )
    with pytest.raises(node_prep["trusted"].PreparationUnavailable, match="unapproved"):
        node_prep["validate_lock"](lock, hashlib.sha256(package.read_bytes()).hexdigest(), package)


def test_node_environment_lock_records_hash_and_detects_manifest_change(node_prep, tmp_path):
    package = tmp_path / "package.json"
    package.write_text('{"name":"express"}')
    digest = hashlib.sha256(package.read_bytes()).hexdigest()
    lock = tmp_path / "package-lock.json"
    lock.write_text(
        json.dumps(
            {
                "lockfileVersion": 3,
                "packages": {
                    "": {},
                    "node_modules/pkg": {
                        "resolved": "https://registry.npmjs.org/pkg/-/pkg-1.0.0.tgz",
                        "integrity": "sha512-YWJjZA==",
                    },
                },
            }
        )
    )
    assert node_prep["validate_lock"](lock, digest, package)["dependency_count"] == 1
    package.write_text('{"name":"changed"}')
    with pytest.raises(node_prep["trusted"].PreparationUnavailable, match="changed"):
        node_prep["validate_lock"](lock, digest, package)
