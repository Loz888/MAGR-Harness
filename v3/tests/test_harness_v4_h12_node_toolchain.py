"""Pinned Node derivation rejects ambiguous or mismatched trusted inputs."""

from __future__ import annotations

import hashlib
import json
import runpy
from pathlib import Path

import pytest


@pytest.fixture
def node_tools(monkeypatch):
    root = Path(__file__).resolve().parents[1]
    monkeypatch.syspath_prepend(str(root / "scripts"))
    return runpy.run_path(str(root / "scripts/h12_prepare_node.py"), run_name="node_toolchain_test")


def test_exact_node_release_ignores_other_releases_but_rejects_duplicates(node_tools):
    trusted = node_tools["trusted"]
    manifest = "a" * 64 + "  node-v18.10.0-linux-x64.tar.xz\n"
    another = "b" * 64 + "  node-v20.20.2-linux-x64.tar.xz\n"
    assert trusted.node_artifact(manifest + another, "18.10.0") == (
        "node-v18.10.0-linux-x64.tar.xz",
        "a" * 64,
    )
    with pytest.raises(trusted.PreparationUnavailable, match="unique"):
        trusted.node_artifact(manifest + manifest, "18.10.0")
    with pytest.raises(trusted.PreparationUnavailable, match="unique"):
        trusted.node_artifact(another, "18.10.0")


@pytest.mark.parametrize("version", ["latest", "18", "18.10", "../18.10.0", "18.10.0;id"])
def test_node_download_rejects_unpinned_versions_before_network(
    node_tools, tmp_path, monkeypatch, version
):
    trusted = node_tools["trusted"]

    def forbidden(*args, **kwargs):
        raise AssertionError("Invalid version must not start a download")

    monkeypatch.setattr(trusted, "fetch_bytes", forbidden)
    with pytest.raises(trusted.PreparationUnavailable, match="exact"):
        trusted.download_toolchain("node", tmp_path, node_version=version)


def test_project_compatibility_accepts_archival_express_but_not_old_got(node_tools, tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    lock = tmp_path / "lock.json"
    lock.write_text(json.dumps({"image_id": "sha256:" + "b" * 64, "node_version": "v18.10.0"}))
    (source / "package.json").write_text('{"name":"express"}')
    assert node_tools["plan"](source, lock)["project"] == "express"
    (source / "package.json").write_text('{"name":"got"}')
    with pytest.raises(node_tools["trusted"].PreparationUnavailable, match="compatible"):
        node_tools["plan"](source, lock)


def test_derivation_rejects_archive_hash_before_any_docker_dispatch(
    node_tools, tmp_path, monkeypatch
):
    trusted = node_tools["trusted"]
    archive = tmp_path / "node-v18.10.0-linux-x64.tar.xz"
    archive.write_bytes(b"mismatched")
    sums = tmp_path / "SHASUMS256.txt"
    sums.write_text("a" * 64 + "  " + archive.name + "\n")
    metadata = {
        "project": "express",
        "package_sha256": "b" * 64,
        "base_lock": {
            "node_preparation": {
                "project": "express",
                "package_sha256": "b" * 64,
                "lock_sha256": "c" * 64,
            }
        },
    }

    def forbidden(*args, **kwargs):
        raise AssertionError("Unverified archive must not dispatch Docker")

    monkeypatch.setattr(trusted, "docker", forbidden)
    with pytest.raises(trusted.PreparationUnavailable, match="SHA256"):
        node_tools["derive_node_toolchain"](metadata, tmp_path, "18.10.0", archive, sums)
    assert hashlib.sha256(archive.read_bytes()).hexdigest() != "a" * 64


def test_partial_derivation_options_fail_without_preparing_project(
    node_tools, tmp_path, monkeypatch
):
    def forbidden(*args, **kwargs):
        raise AssertionError("Partial derivation options cannot prepare a project")

    monkeypatch.setattr(node_tools["trusted"], "docker", forbidden)
    result = node_tools["main"](
        [
            "--source",
            str(tmp_path / "missing-source"),
            "--base-lock",
            str(tmp_path / "missing-lock"),
            "--out",
            str(tmp_path / "out"),
            "--derive-node-version",
            "18.10.0",
        ]
    )
    assert result == 2
    assert json.loads((tmp_path / "out/result.json").read_text())["status"] == "NOT_RUN"
