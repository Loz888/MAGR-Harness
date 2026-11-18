"""Synthetic reproductions of missing names, lost failures and premature stalls."""

from pathlib import Path

import pytest
from test_h11_loop import Crash, MemoryContinuation, fresh_loop
from test_harness_v4_h07 import (
    BUGGY,
    EmptySearchRegistry,
    ScriptedModel,
    _grant,
    _loop,
    _seed,
    _stop,
    _task,
    _tool,
)

from mgar_v3.harness.code_diagnostics import edit_diagnostics
from mgar_v3.harness.service import _test_feedback


def test_missing_import_and_helper_are_advisory_without_execution(tmp_path: Path):
    sentinel = tmp_path / 'never-run'
    source = (f"open({str(sentinel)!r}, 'w').write('unsafe')\n"
              'def convert(value):\n    return parse_value(value) + adjust(value)\n')
    result = edit_diagnostics({'convert.py': source}, ['convert.py'])
    assert {d['name'] for d in result} == {'parse_value', 'adjust'}
    assert not sentinel.exists()
    fixed = 'from parser import parse_value\ndef adjust(x):\n    return x\n' + source
    assert edit_diagnostics({'convert.py': fixed}, ['convert.py']) == []


@pytest.mark.parametrize('source', [
    'def outer(x):\n    def inner(y):\n        return x + y\n    return inner(1)\n',
    'import math\ndef f(x):\n    return math.ceil(x)\n',
    'def f(x):\n    return [v + x for v in range(3)]\n',
    'from plugin import *\ndef f():\n    return dynamic_name()\n',
    "globals()['dynamic_name'] = 1\ndef f():\n    return dynamic_name\n",
])
def test_scopes_imports_and_dynamic_globals_are_not_reported(source):
    assert edit_diagnostics({'sample.py': source}, ['sample.py']) == []


def test_diagnostics_only_examine_selected_bounded_python_files():
    result = edit_diagnostics({'visible.py': 'def bad(:\n', 'secret.py': 'private_name()'},
                              ['visible.py', 'missing.py'])
    assert len(result) == 1 and result[0]['path'] == 'visible.py'
    assert result[0]['code'] == 'syntax_error'
    assert not edit_diagnostics({'big.py': 'x\n' * 100_001}, ['big.py'])
    result = edit_diagnostics({'many.py': '\n'.join(f'missing_{i}()' for i in range(80))},
                              ['many.py'])
    assert len(result) == 20


def test_middle_assertion_survives_large_warning_log():
    output = ('collection noise\n' * 400
              + '================ FAILURES ================\n'
              + 'expected normalized key, received quoted key\nE NameError: missing_helper\n'
              + '================ warnings summary ================\n'
              + 'unrelated warning\n' * 400
              + 'FAILED tests/test_example.py::test_normalization\n1 failed, 500 passed\n')
    feedback = _test_feedback('500 passed; 1 failed', output)
    assert 'expected normalized key, received quoted key' in feedback
    assert 'NameError: missing_helper' in feedback
    assert 'test_normalization' in feedback and len(feedback) < 7500


def _negative_loop(tmp_path, steps):
    model = ScriptedModel(steps)
    loop, _, _ = _loop(_seed(tmp_path, {'calc.py': BUGGY}), model,
                       write_paths=(), mode='investigate', execute=False,
                       registry=EmptySearchRegistry)
    return loop, model, _task('inspect'), _grant(write_paths=(), mode='investigate',
                                              execute=False)[0]


def test_distinct_negative_queries_allow_a_later_source_read(tmp_path):
    loop, model, task, grant = _negative_loop(tmp_path, [
        _tool('search_files', {'query': 'from parser import'}),
        _tool('search_files', {'query': 'parser.'}),
        _tool('read_file', {'path': 'calc.py'}),
        _stop('calc.py contains the function implementation'),
    ])
    result = loop.run(task, grant, loop.plan(task))
    assert result.status == 'completed', result.reason
    assert model.calls == 4


def test_repeated_negative_query_stops_and_cannot_verify_absence_as_evidence(tmp_path):
    loop, model, task, grant = _negative_loop(tmp_path, [
        _tool('search_files', {'query': 'missing'}) for _ in range(8)
    ])
    result = loop.run(task, grant, loop.plan(task))
    assert (result.reason, model.calls) == ('no_progress', 3)
    assert result.status == 'blocked'


def test_negative_search_allowance_survives_recovery_and_remains_bounded(tmp_path):
    class CrashAfterFour(MemoryContinuation):
        def save(self, payload):
            super().save(payload)
            if payload['phase'] == 'model_request' and payload['state']['model_calls'] == 4:
                raise Crash()

    loop, model, task, grant = _negative_loop(tmp_path, [
        _tool('search_files', {'query': 'absent' + str(i)}) for i in range(12)
    ])
    continuation = CrashAfterFour()
    loop.continuation = continuation
    with pytest.raises(Crash):
        loop.run(task, grant, loop.plan(task))
    assert len(continuation.value['state']['progress_empty_queries']) == 4
    recovered = MemoryContinuation()
    recovered.value = continuation.value
    resumed = fresh_loop(loop, recovered)
    result = resumed.run(task, grant, resumed.plan(task))
    assert (result.reason, model.calls) == ('no_progress', 6)
