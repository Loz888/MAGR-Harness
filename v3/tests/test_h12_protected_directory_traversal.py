"""Real Node traversal sees empty metadata masks without access to host secrets."""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from mgar_v3.domain import DomainError
from mgar_v3.harness.broker import IsolatedExecutionBroker, issue_lease
from mgar_v3.harness.contracts import CancellationToken, CapabilityGrant, ExecutionRequest

LOCK = Path(__file__).resolve().parents[1] / ".local/h12/sandbox-lock.json"

SCRIPT = r"""const assert = require('assert');
const fs = require('fs');
const path = require('path');
const writable = process.argv[2] === 'modify_test';
function denied(operation) {
  assert.throws(operation, error =>
    ['EACCES', 'EPERM', 'EROFS', 'ENOENT', 'EBUSY'].includes(error.code));
}
function visit(directory) {
  for (const item of fs.readdirSync(directory, {withFileTypes: true})) {
    if (item.isDirectory()) visit(path.join(directory, item.name));
  }
}
visit('.');
assert.deepStrictEqual(fs.readdirSync('.git'), []);
assert.deepStrictEqual(fs.readdirSync('.mgar'), []);
assert.strictEqual(fs.readFileSync('source.txt', 'utf8'), 'ordinary public source\n');
for (const name of ['.git/config', '.git/objects/controlled-token', '.mgar/token', '.env']) {
  denied(() => fs.readFileSync(name, 'utf8'));
  denied(() => fs.writeFileSync(name, 'attempted overwrite'));
}
for (const directory of ['.git', '.mgar']) {
  denied(() => fs.mkdirSync(path.join(directory, 'new-directory')));
  denied(() => fs.writeFileSync(path.join(directory, 'new-file'), 'attempted write'));
  denied(() => fs.renameSync(directory, directory + '-moved'));
}
if (writable) {
  fs.writeFileSync('ordinary-write-control', 'permitted by fixture grant');
  fs.unlinkSync('ordinary-write-control');
} else {
  denied(() => fs.writeFileSync('ordinary-write-control', 'denied by fixture grant'));
}
console.log('TRAVERSAL_OK_EMPTY_READONLY_MASKS');
"""


def test_real_protected_directory_scan_stays_empty_readonly_and_scope_bound(tmp_path):
    workspace = tmp_path / "project"
    workspace.mkdir()
    (workspace / ".git/objects").mkdir(parents=True)
    (workspace / ".mgar").mkdir()
    content = {
        ".git/config": "CONTROLLED_GIT_CONFIG_SECRET\n",
        ".git/objects/controlled-token": "CONTROLLED_GIT_OBJECT_SECRET\n",
        ".mgar/token": "CONTROLLED_AUTH_TOKEN_SECRET\n",
        ".env": "CONTROLLED_ENV_SECRET\n",
        "source.txt": "ordinary public source\n",
        "check.js": SCRIPT,
    }
    for name, value in content.items():
        (workspace / name).write_text(value)
    # Only this owned /tmp fixture hierarchy becomes traversable by sandbox UID.
    assert Path("/tmp") in workspace.parents
    for parent in (workspace, *workspace.parents):
        if parent == Path("/tmp"):
            break
        parent.chmod(0o755)
    for path in workspace.rglob("*"):
        path.chmod(0o755 if path.is_dir() else 0o644)
    workspace.chmod(0o777)
    before = {name: hashlib.sha256((workspace / name).read_bytes()).hexdigest() for name in content}
    for mode in ("investigate", "modify_test"):
        writable = mode == "modify_test"
        lease = issue_lease(
            mode=mode,
            actor="mask-test",
            workspace_id="mask-workspace",
            repository="local/mask",
            execution_epoch=1,
            read_paths=("",),
            write_paths=("",) if writable else (),
            allowed_commands=("node",),
            timeout_seconds=30,
            approved_by="mask-test" if writable else "",
            approval_ref="controlled-mask-approval" if writable else "",
        )
        grant = CapabilityGrant(
            actor=lease.actor,
            workspace_id=lease.workspace_id,
            repository=lease.repository,
            read_paths=lease.read_paths,
            write_paths=lease.write_paths,
            execute=True,
            allowed_commands=lease.allowed_commands,
            max_seconds=30,
        )
        broker = IsolatedExecutionBroker(workspace, lease, lock_path=LOCK)
        request = ExecutionRequest(
            operation_id="protected-scan-" + mode,
            purpose="command",
            argv=("node", "check.js", mode),
            cwd=".",
            execution_epoch=1,
            timeout_seconds=30,
        )
        receipt = broker.execute(request, grant, CancellationToken())
        assert receipt.status == "passed" and receipt.exit_code == 0, receipt.stdout
        assert receipt.stdout == "TRAVERSAL_OK_EMPTY_READONLY_MASKS\n"
        assert not any("CONTROLLED_" in line for line in receipt.stdout.splitlines())
        for protected in (".git", ".mgar"):
            with pytest.raises(DomainError) as denied:
                broker.execute(
                    request.model_copy(update={"cwd": protected}), grant, CancellationToken()
                )
            assert denied.value.code == "HARNESS_PROTECTED_PATH_DENIED"
            with pytest.raises(DomainError):
                broker.authority.check_scope(
                    actor=lease.actor,
                    workspace_id=lease.workspace_id,
                    repository=lease.repository,
                    execution_epoch=lease.execution_epoch,
                    paths=(protected + "/token",),
                    write=False,
                )
        assert {
            name: hashlib.sha256((workspace / name).read_bytes()).hexdigest() for name in content
        } == before
        assert sorted(
            str(path.relative_to(workspace)) for path in workspace.rglob("*") if path.is_file()
        ) == sorted(content)
