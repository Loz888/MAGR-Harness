"""Targeted real PostgreSQL/Broker regression with a controlled, free model."""

from test_harness_v4_h10 import ScriptedModel, _call, _stop
from test_harness_v4_h11_service import _service

from mgar_v3.harness.contracts import sha256_text
from mgar_v3.harness.service import CodingTaskRequest


def test_pg_command_correction_then_artifact_reread_keeps_original_run(engine, settings, tmp_path):
    def patch(request):
        return _call('apply_edits', {'expected_tree': request.workspace_tree, 'edits': [{
            'path': 'source.py', 'expected_sha256': sha256_text('value = 1\n'),
            'replacements': [{'old_text': 'value = 1', 'new_text': 'value = 2'}],
        }]})

    model = ScriptedModel([
        _call('run_tests', {'argv': ['python3', '-c', 'raise RuntimeError("MUST NOT EXECUTE")']}),
        _call('read_file', {'path': 'source.py'}),
        _call('artifact_reread', {'path': 'source.py',
                                  'artifact_hash': sha256_text('value = 1\n')}),
        patch, _call('run_tests', {'command_id': 'tests'}),
        _stop('Fixed source.py and verified the original regression suite'),
    ])
    service = _service(engine, settings, model)
    source = tmp_path / 'project'
    source.mkdir()
    (source / 'source.py').write_text('value = 1\n')
    (source / 'test_source.py').write_text(
        'import unittest\nfrom source import value\n'
        'class TestValue(unittest.TestCase):\n'
        '    def test_value(self):\n        self.assertEqual(value, 2)\n')
    project = service.register_project(str(source))
    result = service.create_task(CodingTaskRequest(
        project_id=project['project_id'], objective='Fix source.py value', task_kind='bugfix',
        target_path='source.py', old_text='value = 1', new_text='value = 2', approved=True,
        test_command='python3 -B -m unittest -v',
    ))
    assert result['status'] == 'completed', (result['reason'], result['summary'])
    assert len(model.requests) == 6 and len(result['tool_error_receipts']) == 1
    operations = engine.harness.operations(result['task_id'])
    denied = next(op for op in operations
                  if op['operation_id'] == result['tool_error_receipts'][0]['operation_id'])
    assert denied['state'] == 'known' and denied['receipt']['data']['executed'] is False
    reread = next(op for op in operations if op['intent'].get('name') == 'artifact_reread')
    assert reread['state'] == 'known' and reread['receipt']['status'] == 'ok'
    assert all(r.task.run_id == model.requests[0].task.run_id for r in model.requests)
    assert all(c['argv'] == ['python3', '-B', '-m', 'unittest', '-v'] for c in result['commands'])
    assert result['usage_known'] and result['reserved_micros'] == 0
    assert (source / 'source.py').read_text() == 'value = 1\n'
