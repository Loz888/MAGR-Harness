"""Trusted environment staging tests; no Docker, network or project code runs."""

from __future__ import annotations

import io
import json
import runpy
import tarfile
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def builder():
    return runpy.run_path(str(ROOT / "scripts/build_h12_sandbox.py"), run_name="h12_builder_test")


def _tar(path, files):
    with tarfile.open(path, "w:gz") as archive:
        for name, content in files.items():
            member = tarfile.TarInfo(name)
            member.size = len(content)
            archive.addfile(member, io.BytesIO(content))


def test_node_selection_is_major_arch_specific_and_unambiguous(builder):
    manifest = "a" * 64 + "  node-v20.99.1-linux-x64.tar.xz\n"
    manifest += "b" * 64 + "  node-v22.1.0-linux-x64.tar.xz\n"
    assert builder["node_artifact"](manifest) == ("node-v20.99.1-linux-x64.tar.xz", "a" * 64)
    with pytest.raises(builder["PreparationUnavailable"], match="unique"):
        builder["node_artifact"](manifest + manifest)


def test_go_selection_requires_exact_patch_os_arch_and_archive(builder):
    file = {
        "filename": "go1.22.12.linux-amd64.tar.gz",
        "sha256": "a" * 64,
        "os": "linux",
        "arch": "amd64",
        "kind": "archive",
    }
    assert builder["go_artifact"]([{"version": "go1.22.12", "files": [file]}]) == (
        file["filename"],
        file["sha256"],
    )
    with pytest.raises(builder["PreparationUnavailable"]):
        builder["go_artifact"]([{"version": "go1.27.1", "files": [file]}])
    with pytest.raises(builder["PreparationUnavailable"]):
        builder["go_artifact"]({"not": "a release list"})


def test_archive_checksum_is_checked_before_unpacking(builder, tmp_path):
    archive = tmp_path / "go1.22.12.linux-amd64.tar.gz"
    archive.write_bytes(b"modified archive")
    with pytest.raises(builder["PreparationUnavailable"], match="SHA256"):
        builder["verify_archive"](archive, archive.name, "a" * 64)
    builder["verify_archive"](archive, archive.name, builder["sha256_file"](archive))


@pytest.mark.parametrize("name", ["go/../escape", "/absolute", "other/bin/go", "go/evil\\path"])
def test_archive_rejects_traversal_absolute_and_wrong_root(builder, tmp_path, name):
    archive = tmp_path / "archive.tar.gz"
    _tar(archive, {name: b"bad"})
    with pytest.raises(builder["PreparationUnavailable"]):
        builder["unpack_toolchain"](archive, tmp_path / "unpacked", "go")
    assert not (tmp_path / "escape").exists()


def test_archive_regular_files_preserve_executable_bit_without_unsafe_links(builder, tmp_path):
    archive = tmp_path / "archive.tar.gz"
    with tarfile.open(archive, "w:gz") as stream:
        executable = tarfile.TarInfo("go/bin/go")
        executable.size = 2
        executable.mode = 0o4755
        stream.addfile(executable, io.BytesIO(b"go"))
    builder["unpack_toolchain"](archive, tmp_path / "go", "go")
    assert (tmp_path / "go/bin/go").read_bytes() == b"go"
    assert (tmp_path / "go/bin/go").stat().st_mode & 0o7777 == 0o755


def test_python_staging_copies_only_selected_locked_packages(builder, tmp_path, monkeypatch):
    prefix, site = tmp_path / "python", tmp_path / "locked-site"
    (prefix / "bin").mkdir(parents=True)
    (prefix / "bin/python3.12").write_text("synthetic interpreter")
    base_site = prefix / "lib/python3.12/site-packages"
    base_site.mkdir(parents=True)
    (base_site / "unselected.py").write_text("must not copy")
    (prefix / "lib/python3.12/os.py").write_text("stdlib")
    site.mkdir()
    (site / "pytest.py").write_text("selected")
    (site / "pytest-9.2.0.dist-info").mkdir()
    (site / "pytest-9.2.0.dist-info/METADATA").write_text("Version: 9.2.0")
    distribution = SimpleNamespace(
        version="9.2.0",
        files=[Path("pytest.py"), Path("pytest-9.2.0.dist-info/METADATA")],
        locate_file=lambda entry: site / entry,
    )
    globals_ = builder["stage_python"].__globals__
    monkeypatch.setitem(globals_, "SELECTED_DISTRIBUTIONS", ("pytest",))
    monkeypatch.setattr(globals_["importlib"].metadata, "distribution", lambda name: distribution)
    output = tmp_path / "context/python"
    assert builder["stage_python"](prefix, site, output) == {"pytest": "9.2.0"}
    assert (output / "lib/python3.12/os.py").is_file()
    assert (output / "lib/python3.12/site-packages/pytest.py").is_file()
    assert not (output / "lib/python3.12/site-packages/unselected.py").exists()
    assert (output / "bin/pytest").read_text().startswith("#!/usr/local/bin/python3\n")


def test_python_staging_refuses_external_symlinks(builder, tmp_path):
    prefix = tmp_path / "python"
    (prefix / "bin").mkdir(parents=True)
    (prefix / "lib/python3.12").mkdir(parents=True)
    (tmp_path / "outside").write_text("outside")
    (prefix / "bin/python3.12").symlink_to(tmp_path / "outside")
    with pytest.raises(builder["PreparationUnavailable"], match="external symlink"):
        builder["stage_python"](prefix, tmp_path, tmp_path / "context/python")


def test_official_download_refuses_redirect_to_an_untrusted_host(builder, monkeypatch):
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(302, headers={"location": "https://example.invalid/toolchain"})

    original = httpx.Client
    monkeypatch.setattr(
        httpx,
        "Client",
        lambda **kwargs: original(
            transport=httpx.MockTransport(handler),
            follow_redirects=False,
        ),
    )
    with pytest.raises(builder["PreparationUnavailable"], match="official HTTPS hosts"):
        builder["fetch_bytes"]("https://nodejs.org/sums", hosts={"nodejs.org"}, limit=1024)
    assert len(requests) == 1


def test_default_prepare_does_not_invoke_docker_or_network(builder, monkeypatch, tmp_path):
    globals_ = builder["main"].__globals__

    def stage(prefix, site, destination):
        destination.mkdir()
        return {"pytest": "controlled"}

    def forbidden(*args, **kwargs):
        raise AssertionError("Implicit Docker or download is forbidden")

    monkeypatch.setitem(globals_, "stage_python", stage)
    monkeypatch.setitem(globals_, "docker", forbidden)
    monkeypatch.setitem(globals_, "download_toolchain", forbidden)
    assert builder["main"](["--directory", str(tmp_path)]) == 0
    receipt = json.loads((tmp_path / "preparation.json").read_text())
    assert receipt["status"] == "PREPARED_NOT_BUILT"
    assert receipt["node"]["status"] == receipt["go"]["status"] == "NOT_RUN"
    assert not (tmp_path / "sandbox-lock.json").exists()


def test_missing_dependency_records_not_run_without_creating_an_image_lock(
    builder,
    monkeypatch,
    tmp_path,
):
    def missing(*args):
        raise builder["PreparationUnavailable"]("Locked runtime dependency is missing")

    monkeypatch.setitem(builder["main"].__globals__, "stage_python", missing)
    assert builder["main"](["--directory", str(tmp_path)]) == 2
    assert not (tmp_path / "sandbox-lock.json").exists()
    assert json.loads(next(tmp_path.glob("*-not-run.json")).read_text())["status"] == "NOT_RUN"


def test_dockerfile_uses_new_toolchains_and_never_installs_project_code():
    dockerfile = (ROOT / "deploy/Dockerfile.sandbox-h12").read_text()
    assert "FROM ${BASE_IMAGE}" in dockerfile
    assert "rm -rf /usr/local/go" in dockerfile
    assert "ln -sf /opt/h12/python/bin/python3.12 /usr/local/bin/python3" in dockerfile
    assert "pip install" not in dockerfile and "apt-get" not in dockerfile
