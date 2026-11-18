"""A real, synthetic Broker check for read-only pytest cache and image options."""

from __future__ import annotations

from pathlib import Path

from mgar_v3.harness.broker import IsolatedExecutionBroker, issue_lease
from mgar_v3.harness.commands import CommandSession
from mgar_v3.harness.contracts import CancellationToken, CapabilityGrant, ExecutionRequest
from mgar_v3.harness.workspace import _scan, _tree_hash

LOCK = Path(
    "/home/developer/magr-harness/v3/.local/h12/environment-v2/click-less-th0p1e75/sandbox-lock.json"
)


def test_real_readonly_pytest_cache_preserves_configured_options(tmp_path, monkeypatch):
    workspace = tmp_path / "project"
    workspace.mkdir()
    (workspace / "pytest.ini").write_text("[pytest]\nfilterwarnings = error\n")
    (workspace / "test_cache.py").write_text(
        "import os\nfrom pathlib import Path\nimport pytest\n"
        "def test_cache(pytestconfig):\n"
        "    assert os.environ['PYTEST_ADDOPTS'].startswith('--strict-markers ')\n"
        "    assert pytestconfig.getini('cache_dir') == '/scratch/pytest-cache'\n"
        "    pytestconfig.cache.set('public/value', 17)\n"
        "    assert pytestconfig.cache.get('public/value', None) == 17\n"
        "    with pytest.raises(OSError):\n"
        "        Path('/workspace/forbidden.txt').write_text('no write permission')\n"
    )
    for path in (workspace, tmp_path, tmp_path.parent):
        path.chmod(0o755)
    before = _tree_hash(_scan(workspace)[0])
    lease = issue_lease(
        mode="investigate",
        actor="cache-test",
        workspace_id="cache-workspace",
        repository="local/cache",
        execution_epoch=1,
        allowed_commands=("python3",),
        scratch_mb=256,
        timeout_seconds=30,
    )
    broker = IsolatedExecutionBroker(workspace, lease, lock_path=LOCK)
    create_args = broker._create_args

    def configured_image_options(*args, **kwargs):
        command, placeholders = create_args(*args, **kwargs)
        # This models an image's preconfigured options without rebuilding it.
        # No request/model option or production environment port is introduced.
        position = command.index("--workdir")
        command[position:position] = ["--env", "PYTEST_ADDOPTS=--strict-markers"]
        return command, placeholders

    monkeypatch.setattr(broker, "_create_args", configured_image_options)
    grant = CapabilityGrant(
        actor=lease.actor,
        workspace_id=lease.workspace_id,
        repository=lease.repository,
        execute=True,
        allowed_commands=("python3",),
        max_seconds=30,
    )
    session = CommandSession(broker)
    try:
        handle = session.run(
            ExecutionRequest(
                operation_id="readonly-cache",
                purpose="test",
                execution_epoch=lease.execution_epoch,
                argv=("python3", "-B", "-m", "pytest", "test_cache.py"),
                timeout_seconds=30,
            ),
            grant,
            CancellationToken(),
            purpose="test",
        )
        final = session.wait(handle.handle_id, timeout=35)
        output = session.read_log(handle.handle_id).data
        assert final.state == "exited" and final.exit_code == 0, output
        assert "1 passed" in output and "PytestCacheWarning" not in output
        assert session.assert_authority(handle.handle_id).write_paths == ()
    finally:
        session.close()
    assert not (workspace / ".pytest_cache").exists()
    assert not (workspace / "forbidden.txt").exists()
    assert _tree_hash(_scan(workspace)[0]) == before
