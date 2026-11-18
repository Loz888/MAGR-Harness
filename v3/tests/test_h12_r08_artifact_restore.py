"""Known non-executed corrections are not command log receipts."""

from types import SimpleNamespace

import pytest
from test_harness_v4_h10_tools import _call, _fixture

from mgar_v3.domain import DomainError
from mgar_v3.harness.context_tools import ScopedContextTools
from mgar_v3.harness.contracts import (
    ExecutionProvenance,
    ExecutionReceipt,
    canonical_hash,
    sha256_text,
)
from mgar_v3.harness.recovery_io import RecoveryIO


def recovery(tools, context, receipts):
    operations = []
    store = SimpleNamespace(
        guard=lambda *_: {'run_id': context.task.run_id,
                          'grant_scope': context.grant.model_dump(mode='json')},
        operations=lambda *_: operations,
    )
    io = RecoveryIO(store, context.task.task_id, context.task.execution_epoch, tools.workspace,
                    artifacts=tools.artifacts, run_id=str(context.task.run_id))
    for operation_id, name, arguments, receipt in receipts:
        operations.append({'operation_id': operation_id, 'state': 'known', 'kind': 'tool',
                           'intent': {'name': name, 'arguments': arguments}, 'receipt': receipt,
                           'result_hash': io._receipt_hash(receipt)})
    return io


@pytest.mark.parametrize('marker', ['command_correction', 'guarded_denial'])
def test_known_nonexecuted_command_does_not_break_reread_of_issued_source(tmp_path, marker):
    tools, context, authority, _ = _fixture(tmp_path)
    reference = tools.archive("value = 'needle'\n", context, path='code.py')
    data = ({'executed': False, 'correctable': True} if marker == 'command_correction'
            else {'tool_error': {'executed': False}})
    correction = {'status': 'error', 'code': 'HARNESS_COMMAND_DENIED', 'data': data}
    io = recovery(tools, context, [
        ('denied-command', 'run_tests', {'command_id': 'typo'}, correction),
        ('read-source', 'read_file', {'path': 'code.py'},
         {'status': 'ok', 'data': {'artifact': reference}}),
    ])
    fresh = ScopedContextTools(tools.workspace, authority, artifacts=tools.artifacts)
    assert fresh.restore_known(context, io) == 1
    page = fresh.page(_call('artifact_reread', path='code.py',
                           artifact_hash=reference['artifact_hash']), context)
    assert page.status == 'ok' and page.data['text'] == "value = 'needle'\n"
    assert io.store.operations()[0]['receipt'] == correction
    assert not any(r['kind'] == 'log' for r in fresh.references())


def test_nonexecuted_marker_cannot_smuggle_a_log_reference(tmp_path):
    tools, context, _, _ = _fixture(tmp_path)
    io = recovery(tools, context, [
        ('denied-command', 'run_tests', {}, {'status': 'error', 'data': {
            'executed': False, 'artifact': {'kind': 'log', 'path': ''}}}),
    ])
    with pytest.raises(DomainError) as caught:
        tools.restore_known(context, io)
    assert caught.value.code == 'CONTEXT_ARTIFACT_SCOPE_DENIED'
    assert not tools.references()


def test_real_failed_command_receipt_still_restores_its_verified_log(tmp_path):
    tools, context, _, lease = _fixture(tmp_path)
    text = 'FAILED test_value: expected 2, got 1\n'
    provenance = ExecutionProvenance(
        operation_id='real-failed-command', lease_id=lease.lease_id, lease_hash=lease.lease_hash,
        actor=context.task.actor, workspace_id=context.task.workspace_id,
        repository=context.task.repository, execution_epoch=context.task.execution_epoch,
        read_paths=lease.read_paths, write_paths=lease.write_paths,
        protected_paths=lease.protected_paths,
        grant_hash=canonical_hash(context.grant.model_dump(mode='json')),
    )
    execution = ExecutionReceipt(
        receipt_id='a' * 64, operation_id=provenance.operation_id, purpose='test', status='failed',
        argv=('synthetic-fixed-command',), cwd='.', exit_code=1, stdout=text, stderr='',
        duration_seconds=0.1, tree_hash=context.workspace_tree, log_sha256=sha256_text(text),
        environment_hash='b' * 64, tests_run=1, tests_failed=1, provenance=provenance,
    )
    io = recovery(tools, context, [(provenance.operation_id, 'run_tests', {}, {
        'status': 'error', 'data': {'receipt': execution.model_dump(mode='json'),
                                  'truncated': False, 'total_log_bytes': len(text.encode())},
    })])
    assert tools.restore_known(context, io) == 1
    page = tools.page(_call('artifact_reread', path='', artifact_hash=sha256_text(text)), context)
    assert page.data['text'] == text and page.data['source_complete'] is True
