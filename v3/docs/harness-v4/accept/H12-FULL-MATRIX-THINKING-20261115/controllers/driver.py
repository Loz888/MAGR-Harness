"""Execute the presealed complete matrix once, with a receipt before every dispatch."""
import json
import subprocess

from common import (BASE, BINDING, EXPERIMENT, ROOT, audit_case, budget_snapshot,
                    connect, now, ref, sha, write)
from scripts.verify import source_fingerprint
from mgar_v3.quality_eval import h12
from mgar_v3.quality_eval.h12_quality_runner import COMPLETE_SCHEMA, load_quality_freeze

plan = json.loads((BASE/'dispatch-plan.json').read_bytes())
assert plan['experiment_id']==EXPERIMENT and plan['source_sha256']==source_fingerprint(ROOT)
assert plan['driver_sha256']==sha(__file__) and plan['common_sha256']==sha(BASE/'common.py')
manifest = ROOT/plan['quality_freeze']['path']
frozen = load_quality_freeze(ROOT, manifest, plan['quality_freeze']['sha256'])
assert frozen['document']['schema']==COMPLETE_SCHEMA
main = ROOT.parent.parent/'magr-harness/v3'
wrapper = ROOT/'.local/h12/public-dev-20261113/run_h12_with_existing_settings.py'
assert sha(wrapper)==plan['settings_wrapper']['sha256']
common = ['--settings', BINDING['settings_file'], '--quality-freeze', str(manifest),
          '--quality-freeze-sha256', plan['quality_freeze']['sha256'],
          '--experiment-id', EXPERIMENT, '--out', str(BASE/'live')]


def assert_inputs():
    assert source_fingerprint(ROOT)==plan['source_sha256'], 'FROZEN_SOURCE_CHANGED'
    for entry in plan['immutable_inputs']:
        assert sha(ROOT/entry['path'])==entry['sha256'], 'FROZEN_INPUT_CHANGED'
    with connect() as conn:
        observed = budget_snapshot(conn)
    assert observed['unknown_calls']==0 and observed['budget']['reserved_micros']==0
    assert observed['budget']['limit_micros']==12_000_000
    return observed


def invoke(phase, args):
    assert_inputs()
    receipt = BASE/'dispatch-receipts'/(phase+'.json')
    log = BASE/'dispatch-logs'/(phase+'.log')
    log.parent.mkdir(parents=True, exist_ok=True)
    value = {'phase': phase, 'state': 'prepared', 'started_at': now(), 'experiment_id': EXPERIMENT,
             'quality_freeze': plan['quality_freeze'], 'source_sha256': plan['source_sha256']}
    write(receipt, value, exclusive=True)
    with log.open('x') as stream:
        value['state']='dispatching'
        write(receipt, value)
        result = subprocess.run([str(main/'.venv/bin/python'), str(wrapper), *common, *args],
                                cwd=main, stdout=stream, stderr=subprocess.STDOUT)
    value.update(state='returned', controller_exit=result.returncode, finished_at=now(), log=ref(log))
    write(receipt, value)
    return result.returncode


summaries = {}
audits = []
try:
    assert invoke('initialize', ['--initialize-experiment'])==0
    for split in ('dev','holdout'):
        tasks = frozen['dev']['tasks']['dev'] if split=='dev' else frozen['independent']['tasks']['holdout']
        assert [t['task_id'] for t in tasks]==plan['order'][split]
        records = []
        for index, task in enumerate(tasks, 1):
            case = BASE/'live/candidate'/split/task['task_id']/'score.json'
            assert not case.exists(), 'Never implicitly repeat a first sample'
            code = invoke(f'{split}-{index:02d}', ['--split', split, '--task-id', task['task_id'],
                                                  '--execute-live'])
            assert case.is_file(), 'CONTROLLER_RETURNED_WITHOUT_SCORE; inspect before any resend'
            row = json.loads(case.read_bytes())
            assert row['experiment_id']==EXPERIMENT and row['task_id']==task['task_id']
            assert row['arm']=='candidate' and row['split']==split
            if row.get('usage_known') is not True or row['status']=='UNKNOWN':
                raise RuntimeError('UNKNOWN_AWAIT_ACCOUNTING')
            with connect() as conn:
                audit = audit_case(conn, row)
                snapshot = budget_snapshot(conn)
            assert snapshot['unknown_calls']==snapshot['budget']['reserved_micros']==0
            audits.append(audit)
            write(BASE/'pg-case-audits.json', audits)
            records.append(row)
            summary = h12.score_records(records, split, expected_tasks=tasks)
            summaries[split]=summary
            write(BASE/f'aggregate-{split}-{index:02d}.json', summary, exclusive=True)
            write(BASE/'progress.json', {'updated_at': now(), 'summaries': summaries,
                                       'shared_budget': snapshot})
            print(json.dumps({'split':split, 'evaluated':index, 'pass':summary['pass_count'],
                'fail':summary['fail_count'], 'not_run':summary['not_run_count'],
                'threshold_met':summary['threshold_met'], 'case_calls':row.get('model_calls',0),
                'shared_budget':snapshot}, ensure_ascii=False), flush=True)
            if any(summary.get(k) for k in ('unknown_count','unauthorized_actions',
                                            'unknown_blind_retries')):
                raise RuntimeError('DIAGNOSTIC_SAFETY_STOP')
            # Ordinary FAIL/NOT_RUN/regression/resource veto is preserved in scores.
            # It cannot suppress coverage of the remaining first samples.
    write(BASE/'matrix-complete.json', {'status':'ALL_24_CASES_ATTEMPTED','finished_at':now(),
        'experiment_id':EXPERIMENT, 'summaries':summaries, 'quality_pass':all(
            s['threshold_met'] for s in summaries.values())}, exclusive=True)
    print(json.dumps({'complete_matrix':'ALL_24_CASES_ATTEMPTED',
                     'quality_pass':all(s['threshold_met'] for s in summaries.values())}), flush=True)
except BaseException as exc:
    write(BASE/'matrix-stop.json', {'state':'HARD_STOP','recorded_at':now(),
        'reason':str(exc),'exception_type':type(exc).__name__,'completed_case_audits':len(audits)},
        exclusive=True)
    raise
