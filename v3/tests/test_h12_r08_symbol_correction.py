"""General navigation feedback for qualified names absent from the parsed file."""

import json

import pytest
from test_h12_symbol_read import prepared
from test_harness_v4_h10 import ScriptedModel, _call, _runtime, _stop
from test_harness_v4_h10_tools import _call as tool_call

from mgar_v3.harness.contracts import HarnessToolResult
from mgar_v3.harness.tool_errors import ToolErrorPolicy, ToolErrorReceipt, safe_error_result

SOURCE = ('class Parent:\n    def parse(self):\n        return 1\n'
          'class Child(Parent):\n    pass\n')


def test_missing_qualified_definition_suggests_actual_definition_without_claiming_inheritance(
    tmp_path,
):
    tools, context, _, _ = prepared(tmp_path, SOURCE)
    result = tools.read_symbol(tool_call('read_symbol', path='code.py', symbol='Child.parse'),
                               context)
    assert result.status == 'error' and result.code == 'HARNESS_SYMBOL_NOT_FOUND'
    assert result.data['retry_arguments'] == {'path': 'code.py', 'symbol': 'Parent.parse'}
    assert 'text' not in result.data


def test_symbol_retry_survives_real_safe_error_projection_and_keeps_original_correction_budget(
    tmp_path,
):
    def corrected(request):
        feedback = json.loads(next(m.content for m in reversed(request.messages)
                                   if m.role == 'tool'))
        assert feedback['retry_arguments'] == {'path': 'source.py', 'symbol': 'Parent.parse'}
        assert feedback['tool_error']['remaining_corrections'] == 3
        return _call('read_symbol', feedback['retry_arguments'])

    model = ScriptedModel([
        _call('read_symbol', {'path': 'source.py', 'symbol': 'Child.parse'}),
        corrected, _stop(),
    ])
    loop, task, grant, session = _runtime(tmp_path, model, files={'source.py': SOURCE},
                                        max_tokens=32768)
    loop.tool_error_policy = ToolErrorPolicy()
    try:
        result = loop.run(task, grant, loop.plan(task))
    finally:
        session.close()
    assert result.status == 'completed', result.reason
    assert len(model.requests) == 3 and result.tool_calls == 2
    assert len(result.tool_error_receipts) == len(result.tool_correction_receipts) == 1


def test_unknown_method_can_suggest_its_actual_class_without_inventing_a_method(tmp_path):
    tools, context, _, _ = prepared(tmp_path, SOURCE)
    result = tools.read_symbol(tool_call('read_symbol', path='code.py', symbol='Child.unknown'),
                               context)
    assert result.status == 'error' and result.data['matches'] == []
    assert result.data['retry_arguments'] == {'path': 'code.py', 'symbol': 'Child'}


@pytest.mark.parametrize('arguments', [
    {'path': '../private.py', 'symbol': 'Parent.parse'},
    {'path': 'other.py', 'symbol': 'Parent.parse'},
    {'path': 'source.py', 'symbol': '__import__("os")'},
])
def test_symbol_retry_never_projects_out_of_scope_or_executable_arguments(arguments):
    receipt = ToolErrorReceipt(
        operation_id='op', call_id='call', tool='read_symbol', code='HARNESS_SYMBOL_NOT_FOUND',
        category='INVALID_ARGUMENT', task_id='task', run_id='run', workspace_id='workspace',
        execution_epoch=1, tree_hash='a' * 64, acceptance_hash='b' * 64,
        request_hash='c' * 64, policy_hash='d' * 64, allowed_read_roots=('source.py',),
        recoverable=True, remaining_corrections=3, next_action='Use a valid qualified name',
    )
    result = safe_error_result(HarnessToolResult(
        call_id='call', status='error', code=receipt.code,
        data={'correctable': True, 'retry_arguments': arguments},
    ), receipt)
    assert 'retry_arguments' not in json.loads(result.content)
