"""Model-period registry uses the same frozen real reporters as independent scoring."""

import json

import pytest
from test_h12_r05_reporters import EXPRESS, LOCK
from test_harness_v4_h10_tools import _fixture

from mgar_v3.harness.broker import issue_lease
from mgar_v3.harness.commands import CommandSession
from mgar_v3.harness.contracts import CancellationToken, HarnessToolCall, sha256_text
from mgar_v3.harness.public_feedback import PREFIX, PublicTestFeedback
from mgar_v3.harness.service import CodingToolRegistry, WorkspaceBoundBroker
from mgar_v3.quality_eval.h12_regression_plan import regression_plan


@pytest.mark.parametrize("runner", ["unittest", "go-json", "node-tap", "mocha-json"])
def test_actual_public_tool_preserves_reporter_ids_counts_exit_and_raw_log(tmp_path, runner):
    scoped, context, _, _ = _fixture(tmp_path)
    package = None
    if runner == "unittest":
        files = {
            "test_public.py": "import unittest\nclass Values(unittest.TestCase):\n"
            " def test_ok(self):self.assertEqual(1,1)\n"
            " def test_bad(self):self.assertEqual(1,2)\n"
        }
        argv = ("python3", "-B", "-m", "unittest", "test_public")
    elif runner == "go-json":
        files = {
            "go.mod": "module example/r07\n\ngo 1.22\n",
            "calc.go": "package calc\n",
            "calc_test.go": 'package calc\nimport "testing"\n'
            "func TestOk(t *testing.T){}\n"
            'func TestBad(t *testing.T){t.Fatal("expected 2 actual 1")}\n',
        }
        argv = ("go", "test", "-buildvcs=false", "./...")
    elif runner == "node-tap":
        files = {
            "test_public.cjs": "const t=require('node:test');const a=require('node:assert/strict');"
            "t('ok',()=>a.equal(1,1));t('bad',()=>a.equal(1,2));\n"
        }
        argv = ("node", "--test", "test_public.cjs")
    else:
        package = {
            "name": "r07-mocha",
            "version": "1.0.0",
            "scripts": {"test": "mocha test_public.js"},
        }
        files = {
            "package.json": json.dumps(package),
            "test_public.js": "const a=require('assert');"
            "describe('public',()=>{it('ok',()=>a.equal(1,1));it('bad',()=>a.equal(1,2));});\n",
        }
        argv = ("npm", "test")
    for name, data in files.items():
        (scoped.workspace.root / name).write_text(data)
    ancestor = scoped.workspace.root
    while str(ancestor) != "/tmp":
        ancestor.chmod(0o755)
        ancestor = ancestor.parent
    plan = regression_plan([argv], package=package)
    command = plan.commands[0]
    lease = issue_lease(
        mode="modify_test",
        actor=context.task.actor,
        workspace_id=context.task.workspace_id,
        repository=context.task.repository,
        execution_epoch=context.task.execution_epoch,
        write_paths=("",),
        allowed_commands=(command.argv[0],),
        approved_by="owner",
        approval_ref="r07-public-reporters",
        memory_mb=1024,
        nofile_limit=1024,
        max_output_bytes=1048576,
        scratch_mb=256,
    )
    broker = WorkspaceBoundBroker(
        scoped.workspace, lease, lock_path=EXPRESS if runner == "mocha-json" else LOCK
    )
    session = CommandSession(broker)
    context = context.model_copy(
        update={
            "grant": context.grant.model_copy(
                update={
                    "execute": True,
                    "write_paths": ("",),
                    "allowed_commands": (command.argv[0],),
                }
            ),
            "workspace_tree": scoped.workspace.snapshot().tree_hash,
        }
    )
    tools = CodingToolRegistry(
        scoped.workspace, broker, session, command.argv, kind="bugfix", feedback_plan=plan
    )
    try:
        result = tools.invoke(
            HarnessToolCall(
                call_id="public-tool", name="run_tests", arguments={"command_id": "tests"}
            ),
            context,
            CancellationToken(),
        )
        assert result.status == "error" and result.data["test_status"] == "failed", result
        feedback = PublicTestFeedback.model_validate(result.data["receipt"]["public_feedback"])
        assert feedback.tests_run == 2 and feedback.tests_failed == 1
        assert feedback.outcome == "failed" and feedback.failure_ids
        assert feedback.tree_hash == context.workspace_tree and feedback.exit_code == 1
        raw = tools.context_tools.artifacts.read(feedback.artifact_hash).decode()
        assert sha256_text(raw) == feedback.log_sha256 and raw == result.data["receipt"]["stdout"]
        assert result.content.startswith(PREFIX)
        assert scoped.workspace.snapshot().tree_hash == context.workspace_tree
        secret = "r07-private-judge-not-issued"
        private_hash = tools.context_tools.artifacts.put(secret.encode())
        denied = tools.invoke(
            HarnessToolCall(
                call_id="private-page",
                name="artifact_page",
                arguments={"path": "", "artifact_hash": private_hash},
            ),
            context.model_copy(update={"operation_id": "private-page"}),
            CancellationToken(),
        )
        assert denied.status == "denied" and secret not in denied.content
        assert denied.code == "CONTEXT_ARTIFACT_SCOPE_DENIED"
    finally:
        session.close()
