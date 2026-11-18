"""Controller restarts preserve the dedicated budget identity before any send."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

from mgar_v3.settings import Settings


@pytest.fixture
def controller(tmp_path, monkeypatch):
    path = Path(__file__).resolve().parents[1] / "scripts/h12_eval.py"
    spec = importlib.util.spec_from_file_location("h12_controller_test", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, "STATE", tmp_path / "private-state")
    real_settings = Settings

    def settings(**kwargs):
        values = {
            "database_url": "postgresql://fixture:fixture@localhost/fixture",
            "repository": "fixture/project",
            "local_actor": "owner",
            "local_token": "synthetic-private-controller-token",
            **{k: v for k, v in kwargs.items() if k != "_env_file"},
        }
        return real_settings(_env_file=None, **values)

    monkeypatch.setattr(module, "Settings", settings)
    return module


def test_initialization_failure_resumes_exact_identity(controller, monkeypatch):
    seen = []

    def unavailable(dsn, name):
        seen.append((dsn, name))
        if len(seen) == 1:
            raise RuntimeError("controlled database creation interruption")

    monkeypatch.setattr(controller, "_ensure_bound_database", unavailable)
    settings = controller.STATE / "settings.json"
    with pytest.raises(RuntimeError):
        controller.initialize_database(settings)
    bound = (controller.STATE / "budget-database-binding.json").read_bytes()
    controller.initialize_database(settings)
    assert seen[0] == seen[1]
    assert (controller.STATE / "budget-database-binding.json").read_bytes() == bound
    assert not settings.stat().st_mode & 0o077


def test_existing_binding_cannot_be_redirected_to_another_settings_file(controller, monkeypatch):
    monkeypatch.setattr(controller, "_ensure_bound_database", lambda *_: None)
    controller.initialize_database(controller.STATE / "settings.json")
    with pytest.raises(ValueError):
        controller.initialize_database(controller.STATE / "another-settings.json")


def test_database_identity_tampering_fails_before_connect(controller, monkeypatch):
    monkeypatch.setattr(controller, "_ensure_bound_database", lambda *_: None)
    target = controller.STATE / "settings.json"
    controller.initialize_database(target)
    settings = json.loads(target.read_text())
    settings["database_url"] = "postgresql://localhost/a_different_budget"
    target.write_text(json.dumps(settings))
    with pytest.raises(ValueError, match="identity changed"):
        controller.load_settings(target)


def test_public_settings_are_rejected(controller, monkeypatch):
    monkeypatch.setattr(controller, "_ensure_bound_database", lambda *_: None)
    target = controller.STATE / "settings.json"
    controller.initialize_database(target)
    target.chmod(0o644)
    with pytest.raises(ValueError, match="private"):
        controller.load_settings(target)
