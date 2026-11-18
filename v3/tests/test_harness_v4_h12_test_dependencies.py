"""Trusted dependency preparation validates inputs without executing them on the host."""

from __future__ import annotations

import hashlib
import json
import runpy

import pytest

from mgar_v3.quality_eval.harness_v4 import repo_root


@pytest.fixture
def preparation(monkeypatch):
    monkeypatch.syspath_prepend(str(repo_root() / "scripts"))
    return runpy.run_path(str(repo_root() / "scripts/h12_prepare_python.py"), run_name="test_tools")


def test_original_dev_constraints_and_explicit_plugin_are_frozen(preparation, tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "pyproject.toml").write_text(
        '[project]\nname="requests"\n[build-system]\nrequires=["setuptools"]\n'
        'build-backend="setuptools.build_meta"\n'
    )
    raw = b"-e .[socks]\npytest>=2.8.0,<9\npytest-httpbin==2.1.0\nhttpbin~=0.10.0\ntrustme\n"
    (source / "requirements-dev.txt").write_bytes(raw)
    lock = tmp_path / "base.json"
    lock.write_text(
        json.dumps(
            {
                "schema": "h12-sandbox-lock-v1",
                "image_id": "sha256:" + "a" * 64,
                "python_version": "Python 3.12.16",
            }
        )
    )
    ordinary = preparation["preflight"](source, lock)
    assert ordinary["pytest_plugins"] == [] and not ordinary["test_dependency_inputs"]
    full = preparation["preflight"](source, lock, test_dependencies=True)
    assert "pytest>=2.8.0,<9" in full["requirements"]
    assert "pytest-httpbin==2.1.0" in full["requirements"]
    assert full["pytest_plugins"] == ["pytest_httpbin.plugin"]
    assert full["test_dependency_inputs"]["requirements-dev.txt"] == hashlib.sha256(raw).hexdigest()
    (source / "requirements-dev.txt").write_text("--extra-index-url https://untrusted.invalid\n")
    with pytest.raises(preparation["trusted"].PreparationUnavailable, match="directive"):
        preparation["preflight"](source, lock, test_dependencies=True)


@pytest.mark.parametrize(
    "filename,allowed",
    [
        ("cryptography-50.0.2-cp311-abi3-manylinux_2_28_x86_64.whl", True),
        ("cffi-2.0.0-cp312-cp312-manylinux_2_17_x86_64.whl", True),
        ("cffi-2.0.0-cp313-cp313-manylinux_2_17_x86_64.whl", False),
        ("cffi-2.0.0-cp312-cp312-manylinux_2_39_x86_64.whl", False),
        ("cffi-2.0.0-cp312-cp312-win_amd64.whl", False),
    ],
)
def test_only_fixed_cpython312_and_base_glibc_compatible_wheels(preparation, filename, allowed):
    assert (
        preparation["_compatible_wheel"](
            {"filename": filename, "packagetype": "bdist_wheel", "requires_python": ">=3.9"}
        )
        is allowed
    )
