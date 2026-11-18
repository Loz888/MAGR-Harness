"""Real PG/Broker path exposes advisory edit feedback to a controlled model."""

from test_h12_public_probe_service import BUGGY, _candidate, _case, _edit
from test_harness_v4_h10 import _call, _stop


def test_model_sees_missing_helper_and_repairs_it_before_real_tests(engine, tmp_path):
    case = _case(engine, tmp_path)
    incomplete = 'def add(a, b):\n    return sum_pair(a, b)\n'
    complete = incomplete + '\ndef sum_pair(a, b):\n    return a + b\n'

    def repair(request):
        reply = next(m for m in reversed(request.messages) if m.role == 'tool')
        assert 'sum_pair' in reply.content
        assert 'Advisory static diagnostics' in reply.content
        return _edit(request, before=incomplete, after=complete)

    case.model.steps = [
        lambda request: _edit(request, before=BUGGY, after=incomplete),
        repair,
        _call('run_tests', {'command_id': 'public_probe'}),
        _call('run_tests', {'command_id': 'tests'}),
        _stop('calc.py helper and public tests verified'),
    ]
    service = case.service()
    result = service.create_task(case.request(service))
    candidate = _candidate(case, result)
    assert candidate['status'] == 'completed'
    assert candidate['model_calls'] == 5
    with engine.transaction() as conn:
        rows = conn.execute(
            "SELECT receipt FROM app.harness_operations WHERE run_id=%s "
            "AND receipt->'data' ? 'diagnostics'", (result['run_id'],),
        ).fetchall()
    assert len(rows) == 1
    assert rows[0]['receipt']['data']['diagnostics'][0]['name'] == 'sum_pair'
