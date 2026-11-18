"""Directory discovery must not advertise inaccessible Harness metadata."""

from pathlib import Path

import pytest
from test_harness_v4_h10 import ScriptedModel, _call, _stop
from test_harness_v4_h10_tools import _fixture
from test_harness_v4_h11_service import _request, _service

from mgar_v3.harness.commands import CommandSession
from mgar_v3.harness.contracts import CancellationToken
from mgar_v3.harness.service import CodingToolRegistry, WorkspaceBoundBroker


def registry_fixture(tmp_path):
    scoped, context, authority, lease = _fixture(tmp_path)
    broker = WorkspaceBoundBroker(
        scoped.workspace,
        authority,
        lock_path=Path(__file__).resolve().parents[1] / ".local/r03/sandbox-lock.json",
    )
    session = CommandSession(broker)
    registry = CodingToolRegistry(scoped.workspace, broker, session, (), kind="inspect")
    return registry, context, authority, session


def test_listing_hides_protected_children_and_aliases_but_keeps_public_source(tmp_path):
    registry, context, authority, session = registry_fixture(tmp_path)
    workspace = registry.workspace
    (workspace.root / "public-alias.py").symlink_to("code.py")
    (workspace.root / "private-alias.py").symlink_to("private.py")
    (workspace.root / "metadata-alias").symlink_to(".mgar", target_is_directory=True)
    try:
        result = registry.invoke(
            _call("list_dir", {"path": ""}).tool_calls[0], context, CancellationToken()
        )
        assert result.status == "ok", result
        assert result.data["entries"] == ["code.py", "public-alias.py"]
        assert ".mgar" not in result.content and "private" not in result.content
        description = next(s.description for s in registry.specs() if s.name == "list_dir")
        assert "protected=" in description and ".mgar" in description
        for path in [".mgar", "metadata-alias", "private.py", "private-alias.py"]:
            direct = registry.invoke(
                _call("list_dir", {"path": path}).tool_calls[0], context, CancellationToken()
            )
            assert direct.status == "denied", direct
        authority.revoke()
        revoked = registry.invoke(
            _call("list_dir", {"path": ""}).tool_calls[0], context, CancellationToken()
        )
        assert revoked.status == "denied" and revoked.code == "HARNESS_LEASE_REVOKED"
    finally:
        session.close()


@pytest.mark.parametrize("change", ["revoke", "replace"])
def test_mid_listing_authority_change_never_returns_partial_names(tmp_path, monkeypatch, change):
    registry, context, authority, session = registry_fixture(tmp_path)
    original = registry.workspace.list_dir

    def listing(*args, **kwargs):
        names = original(*args, **kwargs)
        if change == "revoke":
            authority.revoke()
        else:
            old = authority.peek()
            new = old.model_copy(update={"protected_paths": (*old.protected_paths, "code.py")})
            authority.replace(new.model_copy(update={"lease_hash": new.computed_hash()}))
        return names

    monkeypatch.setattr(registry.workspace, "list_dir", listing)
    try:
        result = registry.invoke(
            _call("list_dir", {"path": ""}).tool_calls[0], context, CancellationToken()
        )
        assert result.status != "ok" and "entries" not in result.data
        assert result.code in {"HARNESS_LEASE_REVOKED", "CONTEXT_AUTHORITY_CHANGED"}
        assert "code.py" not in result.content
    finally:
        session.close()


def test_changed_task_scope_is_not_repaired_by_directory_filtering(tmp_path):
    registry, context, _, session = registry_fixture(tmp_path)
    other = context.model_copy(update={"task": context.task.model_copy(update={"actor": "other"})})
    try:
        result = registry.invoke(
            _call("list_dir", {"path": ""}).tool_calls[0], other, CancellationToken()
        )
        assert result.status == "denied" and result.code == "CONTEXT_ARTIFACT_SCOPE_DENIED"
    finally:
        session.close()


def test_real_pg_discovery_continues_and_recovery_reuses_completed_result(
    engine, settings, tmp_path
):
    def read_source(request):
        listing = next(m.content for m in reversed(request.messages) if m.name == "list_dir")
        assert "source.py" in listing and ".mgar" not in listing and ".git" not in listing
        return _call("read_file", {"path": "source.py"})

    model = ScriptedModel(
        [
            _call("list_dir", {"path": ""}),
            read_source,
            _stop("source.py defines value = 1, based on the inspected source."),
        ]
    )
    service = _service(engine, settings, model)
    result = service.create_task(_request(service, tmp_path))
    assert result["status"] == "completed" and result["model_calls"] == 3
    assert not model.steps
    stored = engine.harness.get(result["task_id"])
    assert stored["status"] == "succeeded"
    recovered = service.recover_task(result["task_id"])
    assert recovered["tree_after"] == result["tree_after"]
    assert recovered["model_calls"] == 3
