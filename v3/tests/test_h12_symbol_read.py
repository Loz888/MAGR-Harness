"""Whole-function reads preserve existing scope, tree and output bounds."""

from pathlib import Path

import pytest
from test_harness_v4_h10_tools import _call, _fixture

from mgar_v3.domain import DomainError
from mgar_v3.harness.commands import CommandSession
from mgar_v3.harness.contracts import CancellationToken, sha256_text
from mgar_v3.harness.service import CodingToolRegistry, WorkspaceBoundBroker
from mgar_v3.harness.test_failures import failed_test_ids


def prepared(tmp_path, source):
    tools, context, authority, lease = _fixture(tmp_path)
    (tools.workspace.root / 'code.py').write_text(source)
    context = context.model_copy(update={'workspace_tree': tools.workspace.snapshot().tree_hash})
    return tools, context, authority, lease


def test_qualified_method_includes_decorator_and_never_executes_source(tmp_path):
    sentinel = tmp_path / 'not-executed'
    source = (f"open({str(sentinel)!r}, 'w').write('bad')\n"
              'class First:\n    @staticmethod\n    def parse(value):\n        return value\n'
              'class Second:\n    def parse(self):\n        return None\n')
    tools, context, _, _ = prepared(tmp_path, source)
    result = tools.read_symbol(_call('read_symbol', path='code.py', symbol='First.parse'), context)
    assert result.status == 'ok' and result.data['text'].startswith('    @staticmethod')
    assert result.data['file_sha256'] == sha256_text(source)
    assert result.data['source_tree'] == context.workspace_tree
    assert not result.data['truncated'] and result.data['next_read'] is None
    assert not sentinel.exists()
    ambiguous = tools.read_symbol(_call('read_symbol', path='code.py', symbol='parse'), context)
    assert ambiguous.status == 'error' and ambiguous.data['correctable']
    assert [x['symbol'] for x in ambiguous.data['matches']] == ['First.parse', 'Second.parse']


def test_large_symbol_has_bounded_utf8_and_explicit_continuation(tmp_path):
    source = 'def long_function():\n' + ''.join('    value = "中文🙂"\n' for _ in range(300))
    tools, context, _, _ = prepared(tmp_path, source)
    result = tools.read_symbol(
        _call('read_symbol', path='code.py', symbol='long_function'), context)
    assert result.data['truncated'] and result.data['end_line'] == 160
    assert len(result.data['text'].encode()) <= 12288
    continuation = result.data['next_read']
    following = tools.read_lines(_call(continuation['tool'], **continuation['arguments']), context)
    assert following.data['start_line'] == 161


@pytest.mark.parametrize('symbol', ['missing', 'First.parse'])
def test_missing_symbol_is_correctable_without_inventing_source(tmp_path, symbol):
    tools, context, _, _ = prepared(tmp_path, 'def existing():\n    return 1\n')
    result = tools.read_symbol(_call('read_symbol', path='code.py', symbol=symbol), context)
    assert result.status == 'error' and result.code == 'HARNESS_SYMBOL_NOT_FOUND'
    assert result.data['matches'] == []


@pytest.mark.parametrize('arguments', [
    {'path': 'code.py', 'symbol': 'existing', 'max_lines': True},
    {'path': 'code.py', 'symbol': 'existing', 'max_lines': 201},
    {'path': 'code.py', 'symbol': '__import__("os")'},
    {'path': 'code.py', 'symbol': 'existing', 'offset': 123},
])
def test_invalid_symbol_arguments_are_not_executed(tmp_path, arguments):
    tools, context, _, _ = prepared(tmp_path, 'def existing():\n    return 1\n')
    with pytest.raises(DomainError) as exc:
        tools.read_symbol(_call('read_symbol', **arguments), context)
    assert exc.value.code == 'HARNESS_TOOL_ARGUMENT'


def test_symbol_registry_keeps_protection_epoch_and_tree_checks(tmp_path):
    tools, context, authority, lease = prepared(tmp_path, 'def existing():\n    return 1\n')
    broker = WorkspaceBoundBroker(tools.workspace, lease,
        lock_path=Path(__file__).resolve().parents[1] / '.local/r03/sandbox-lock.json')
    session = CommandSession(broker)
    try:
        registry = CodingToolRegistry(
            tools.workspace, broker, session, ('python3',), kind='feature')
        assert 'read_symbol' in {spec.name for spec in registry.specs()}
        result = registry.invoke(_call('read_symbol', path='code.py', symbol='existing'),
                                 context, CancellationToken())
        assert result.status == 'ok' and 'return 1' in result.data['text']
        denied = registry.invoke(_call('read_symbol', path='private.py', symbol='existing'),
                                 context, CancellationToken())
        assert denied.status == 'denied'
    finally:
        session.close()
    for changed, code in [
        (context.model_copy(update={'workspace_tree': 'f' * 64}), 'CONTEXT_TREE_STALE'),
        (context.model_copy(update={'task': context.task.model_copy(
            update={'execution_epoch': 2})}), 'HARNESS_EPOCH_MISMATCH'),
    ]:
        with pytest.raises(DomainError) as exc:
            tools.read_symbol(_call('read_symbol', path='code.py', symbol='existing'), changed)
        assert exc.value.code == code


@pytest.mark.parametrize('argv,log,expected', [
    (['go', 'test', '-v', './...'],
     '--- FAIL: TestA (0.01s)\nFAIL\texample/one\t0.1s\n'
     '--- FAIL: TestA (0.01s)\nFAIL\texample/two\t0.1s\n',
     ('example/one::TestA', 'example/two::TestA')),
    (['node', '--test', 'a.js'], 'not ok 1 - a.js\nnot ok 2 - b.js\n', ('a.js', 'b.js')),
    (['python3', '-m', 'unittest'], 'FAIL: test_a (suite.One)\nERROR: test_b (suite.Two)\n',
     ('test_a (suite.One)', 'test_b (suite.Two)')),
])
def test_failure_ids_cover_complete_runner_reports(argv, log, expected):
    assert failed_test_ids(argv, log, 2) == expected
    assert failed_test_ids(argv, log, 3) is None


def test_ambiguous_or_partial_failure_report_stays_unknown():
    assert failed_test_ids(['go', 'test'], '--- FAIL: TestA (0.01s)\n', 1) is None
    assert failed_test_ids(['node', '--test'], 'not ok 1 - same\nnot ok 2 - same\n', 2) is None
    assert failed_test_ids(['mocha'], '2 failing\n', 2) is None
