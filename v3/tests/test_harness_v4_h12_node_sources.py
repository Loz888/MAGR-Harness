"""Node preparation validates dependency sources before dispatching network work."""

from __future__ import annotations

import hashlib
import json
import runpy
from pathlib import Path
from types import SimpleNamespace

import pytest


@pytest.fixture
def node_sources(monkeypatch):
    root = Path(__file__).resolve().parents[1]
    monkeypatch.syspath_prepend(str(root / "scripts"))
    return runpy.run_path(str(root / "scripts/h12_prepare_node.py"), run_name="node_sources_test")


def _inputs(tmp_path, *, original_lock=False):
    source = tmp_path / "source"
    source.mkdir()
    (source / "package.json").write_text(
        json.dumps({"name": "express", "dependencies": {"pkg": "^1.0.0"}})
    )
    base = tmp_path / "base.json"
    base.write_text(json.dumps({"image_id": "sha256:" + "b" * 64, "node_version": "v20.20.2"}))
    if original_lock:
        (source / "package-lock.json").write_text(json.dumps(_lock()))
    out = tmp_path / "out"
    out.mkdir()
    return source, base, out


def _lock(resolved="https://registry.npmjs.org/pkg/-/pkg-1.0.0.tgz"):
    return {
        "lockfileVersion": 3,
        "packages": {
            "": {},
            "node_modules/pkg": {
                "resolved": resolved,
                "integrity": "sha512-YWJjZA==",
            },
        },
    }


def _forbid_docker(*args, **kwargs):
    raise AssertionError("Rejected dependency metadata must never dispatch Docker/network")


def test_existing_nonofficial_lock_is_rejected_in_plan_before_any_dispatch(
    node_sources,
    tmp_path,
    monkeypatch,
):
    source, base, out = _inputs(tmp_path, original_lock=True)
    (source / "package-lock.json").write_text(json.dumps(_lock("https://outside.invalid/pkg.tgz")))
    monkeypatch.setattr(node_sources["trusted"], "docker", _forbid_docker)
    assert (
        node_sources["main"](
            [
                "--source",
                str(source),
                "--base-lock",
                str(base),
                "--out",
                str(out),
                "--prepare",
            ]
        )
        == 2
    )
    failure = json.loads((out / "result.json").read_text())
    assert failure["status"] == "NOT_RUN" and "unapproved" in failure["reason"]


def test_prepare_revalidates_original_lock_before_ci_even_after_a_valid_plan(
    node_sources,
    tmp_path,
    monkeypatch,
):
    source, base, out = _inputs(tmp_path, original_lock=True)
    metadata = node_sources["plan"](source, base)
    (source / "package-lock.json").write_text(
        json.dumps(_lock("git+https://outside.invalid/pkg.git"))
    )
    monkeypatch.setattr(node_sources["trusted"], "docker", _forbid_docker)
    with pytest.raises(node_sources["trusted"].PreparationUnavailable, match="unapproved"):
        node_sources["prepare"](source, out, metadata)
    report = json.loads(next(out.glob("*-validation.json")).read_text())
    assert report["docker_dispatched"] is False
    assert report["phase"] == "before_dependency_network"


def test_safe_original_lock_is_checked_before_first_ci_dispatch(
    node_sources, tmp_path, monkeypatch
):
    source, base, out = _inputs(tmp_path, original_lock=True)
    metadata = node_sources["plan"](source, base)
    order = []
    validate = node_sources["validate_lock"]

    def checked(*args):
        order.append("validated")
        return validate(*args)

    def stop_after_dispatch(args, output):
        assert "ci" in args and "--ignore-scripts" in args
        order.append("ci")
        raise RuntimeError("stop at safe dispatch")

    monkeypatch.setitem(node_sources["prepare"].__globals__, "validate_lock", checked)
    monkeypatch.setattr(node_sources["trusted"], "docker", stop_after_dispatch)
    with pytest.raises(RuntimeError, match="safe dispatch"):
        node_sources["prepare"](source, out, metadata)
    assert order == ["validated", "ci"]


@pytest.mark.parametrize(
    "extra",
    [
        {"peerDependencies": {"pkg": "https://outside.invalid/pkg.tgz"}},
        {"dependencies": {"pkg": "npm:other@^1"}},
        {"optionalDependencies": {"pkg": "file:../outside"}},
        {"devDependencies": {"pkg": "github:outside/repository"}},
        {"overrides": {"pkg": "https://outside.invalid/pkg.tgz"}},
        {"overrides": {"pkg@^1": {"other": "git+https://outside.invalid/repo.git"}}},
        {"overrides": {"pkg": {".": "$other"}}},
        {"overrides": {"pkg@https://outside.invalid/pkg.tgz": "1.0.0"}},
        {"workspaces": ["../outside"]},
        {"resolutions": {"pkg": "https://outside.invalid/pkg.tgz"}},
        {"bundledDependencies": ["pkg"]},
        {"peerDependencies": {"pkg": "latest"}},
        {"dependencies": {"pkg": {"url": "https://outside.invalid/pkg.tgz"}}},
    ],
)
def test_no_lock_manifest_source_entries_are_rejected_without_network(
    node_sources,
    tmp_path,
    monkeypatch,
    extra,
):
    source, base, out = _inputs(tmp_path)
    package = json.loads((source / "package.json").read_text())
    package.update(extra)
    (source / "package.json").write_text(json.dumps(package))
    monkeypatch.setattr(node_sources["trusted"], "docker", _forbid_docker)
    assert (
        node_sources["main"](
            [
                "--source",
                str(source),
                "--base-lock",
                str(base),
                "--out",
                str(out),
                "--prepare",
            ]
        )
        == 2
    )
    assert json.loads((out / "result.json").read_text())["status"] == "NOT_RUN"


def test_direct_no_lock_prepare_rechecks_manifest_resolution_fields(
    node_sources, tmp_path, monkeypatch
):
    source, base, out = _inputs(tmp_path)
    metadata = node_sources["plan"](source, base)
    package = json.loads((source / "package.json").read_text())
    package["overrides"] = {"pkg": "https://outside.invalid/pkg.tgz"}
    (source / "package.json").write_text(json.dumps(package))
    metadata["package_sha256"] = hashlib.sha256((source / "package.json").read_bytes()).hexdigest()
    monkeypatch.setattr(node_sources["trusted"], "docker", _forbid_docker)
    with pytest.raises(node_sources["trusted"].PreparationUnavailable, match="semver"):
        node_sources["prepare"](source, out, metadata)
    assert json.loads(next(out.glob("*-validation.json")).read_text())["docker_dispatched"] is False


@pytest.mark.parametrize("approved", [False, True])
def test_generated_lock_is_validated_before_any_ci(node_sources, tmp_path, monkeypatch, approved):
    source, base, out = _inputs(tmp_path)
    metadata = node_sources["plan"](source, base)
    events = []
    validate = node_sources["validate_lock"]

    def checked(*args):
        events.append("validated")
        return validate(*args)

    def dispatch(args, output):
        if "--package-lock-only" in args:
            events.append("generate")
            mount = next(arg for arg in args if arg.startswith("type=bind,source="))
            stage = Path(mount.split("source=", 1)[1].split(",", 1)[0])
            (stage / "package-lock.json").write_text(
                json.dumps(
                    _lock(
                        "https://registry.npmjs.org/pkg/-/pkg-1.0.0.tgz"
                        if approved
                        else "https://outside.invalid/pkg.tgz",
                    )
                )
            )
            return SimpleNamespace(returncode=0, stdout=b"lock generated", stderr=b"")
        events.append("ci")
        raise RuntimeError("stop before install execution")

    monkeypatch.setitem(node_sources["prepare"].__globals__, "validate_lock", checked)
    monkeypatch.setattr(node_sources["trusted"], "docker", dispatch)
    if approved:
        with pytest.raises(RuntimeError, match="install execution"):
            node_sources["prepare"](source, out, metadata)
        assert events == ["generate", "validated", "ci"]
    else:
        with pytest.raises(node_sources["trusted"].PreparationUnavailable, match="unapproved"):
            node_sources["prepare"](source, out, metadata)
        assert events == ["generate", "validated"]
    attempts = json.loads(next(out.glob("*-attempts.json")).read_text())
    assert len(attempts) == 1 and attempts[0]["argv"][1] == "install"
    assert Path(attempts[0]["log"]).read_bytes() == b"lock generated"


def test_legacy_lock_sources_and_transitive_url_dependencies_are_validated(node_sources, tmp_path):
    source, _, _ = _inputs(tmp_path)
    package = source / "package.json"
    digest = hashlib.sha256(package.read_bytes()).hexdigest()
    lock = _lock()
    lock["lockfileVersion"] = 2
    lock["dependencies"] = {
        "pkg": {"resolved": "https://outside.invalid/pkg.tgz", "integrity": "sha512-YWJjZA=="}
    }
    path = source / "package-lock.json"
    path.write_text(json.dumps(lock))
    with pytest.raises(node_sources["trusted"].PreparationUnavailable, match="unapproved"):
        node_sources["validate_lock"](path, digest, package)
    lock.pop("dependencies")
    lock["packages"]["node_modules/pkg"]["dependencies"] = {
        "other": "https://outside.invalid/other.tgz"
    }
    path.write_text(json.dumps(lock))
    with pytest.raises(node_sources["trusted"].PreparationUnavailable, match="semver"):
        node_sources["validate_lock"](path, digest, package)


def test_current_development_node_manifests_remain_valid(node_sources):
    root = Path(__file__).resolve().parents[1]
    paths = [
        root
        / ".local/h12/sources/expressjs/express"
        / "8368dc178af16b91b576c4c1d135f701a0007e5d/package.json",
        root
        / ".local/h12/sources/sindresorhus/got"
        / "de19b463b1875847f955a53d2f5b0a539ed0ded3/package.json",
    ]
    if not all(path.is_file() for path in paths):
        pytest.skip("exact development source cache is unavailable")
    for path in paths:
        node_sources["validate_manifest"](json.loads(path.read_text()))
    node_sources["validate_manifest"]({"overrides": {"pkg@^1": {".": "^1.2.3", "other": ">=2 <3"}}})
