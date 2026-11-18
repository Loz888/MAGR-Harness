"""Resume only absent first samples; never redispatch an existing receipt.

Scored paused failures are closed through the original Engine after proving
that usage, operations and the scored tree are final. All original receipts,
scores, controller source, candidate source and the freeze remain unchanged.
"""
import json
import subprocess
from pathlib import Path

from common import (BASE, BINDING, EXPERIMENT, ROOT, SETTINGS, audit_case,
                    budget_snapshot, connect, now, ref, sha, write)
from scripts.verify import source_fingerprint
from mgar_v3.coordination.engine import Engine
from mgar_v3.evidence.artifacts import ArtifactStore
from mgar_v3.quality_eval import h12
from mgar_v3.quality_eval.h12_quality_runner import COMPLETE_SCHEMA, load_quality_freeze

plan = json.loads((BASE/'dispatch-plan.json').read_bytes())
assert plan['experiment_id']==EXPERIMENT
assert plan['driver_sha256']==sha(BASE/'driver.py')
assert plan['common_sha256']==sha(BASE/'common.py')
manifest = ROOT/plan['quality_freeze']['path']
frozen = load_quality_freeze(ROOT, manifest, plan['quality_freeze']['sha256'])
assert frozen['document']['schema']==COMPLETE_SCHEMA
assert (BASE/'matrix-stop.json').is_file()
assert not (BASE/'matrix-complete.json').exists()
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


def close_scored_pause(row):
    """Administrative closure of an already failed, fully accounted sample."""
    if not row.get('run_id'):
        return
    with connect() as conn:
        task = conn.execute('SELECT t.task_id,t.tree_hash,r.status FROM app.harness_tasks t '
            'JOIN app.runs r USING(run_id) WHERE t.run_id=%s', (row['run_id'],)).fetchone()
    if task['status'] in {'succeeded','failed','cancelled'}:
        return
    assert row['status']=='FAIL' and task['status']=='paused', 'UNEXPECTED_NONTERMINAL_SCORE'
    assert task['tree_hash']==row['final_tree'] and row['usage_known'] is True
    assert row['reserved_micros']==0
    closure = BASE/'administrative-closures'/(str(row['run_id'])+'.json')
    assert not closure.exists(), 'Review interrupted closure before continuing'
    before = assert_inputs()
    engine = Engine(SETTINGS, ArtifactStore(SETTINGS.artifact_root))
    try:
        with engine.transaction() as conn:
            current = engine.harness.get(task['task_id'])
            assert str(current['run_id'])==row['run_id'] and current['status']=='paused'
            assert current['tree_hash']==row['final_tree']
            usages = conn.execute('SELECT status,settled_micros FROM app.usage_calls '
                'WHERE run_id=%s', (row['run_id'],)).fetchall()
            assert len(usages)==row['model_calls'] and all(u['status']=='known' for u in usages)
            assert sum(u['settled_micros'] or 0 for u in usages)==row['settled_cost_micros']
            operations = conn.execute('SELECT state FROM app.harness_operations WHERE run_id=%s',
                                      (row['run_id'],)).fetchall()
            assert all(o['state'] in {'known','cancelled','denied'} for o in operations)
            effects = conn.execute('SELECT count(*) AS n FROM app.actions WHERE run_id=%s',
                                   (row['run_id'],)).fetchone()
            assert effects['n']==0, 'UNEXPECTED_EXTERNAL_ACTION'
        entry = {'state':'prepared','recorded_at':now(),'experiment_id':EXPERIMENT,
            'run_id':row['run_id'],'task_id':row['task_id'],'score_status':row['status'],
            'score_reason':row['reason'],'original_engine_status':task['status'],
            'tree_hash':row['final_tree'],'model_calls':row['model_calls'],
            'settled_cost_micros':row['settled_cost_micros'],
            'score':ref(BASE/'live/candidate'/row['split']/row['task_id']/'score.json'),
            'method':'original Engine.harness.cancel; no continuation or sample retry',
            'budget_before':before}
        write(closure, entry, exclusive=True)
        closed = engine.harness.cancel(task['task_id'])
        assert closed['status']=='cancelled' and closed['tree_hash']==row['final_tree']
        after = assert_inputs()
        assert after==before, 'CLOSURE_MUST_NOT_CALL_MODEL_OR_CHANGE_BUDGET'
        with connect() as conn:
            audit = audit_case(conn, row)
        entry.update(state='closed',finished_at=now(),engine_status='cancelled',
                     budget_after=after,audit=audit)
        write(closure, entry)
    finally:
        engine.close()


def invoke(phase, args):
    assert_inputs()
    receipt = BASE/'dispatch-receipts'/(phase+'.json')
    log = BASE/'dispatch-logs'/(phase+'.log')
    assert not receipt.exists() and not log.exists(), 'NEVER_REDISPATCH_EXISTING_RECEIPT'
    value = {'phase':phase,'state':'prepared','started_at':now(),'experiment_id':EXPERIMENT,
             'quality_freeze':plan['quality_freeze'],'source_sha256':plan['source_sha256'],
             'resumption':ref(BASE/'dispatch-resumption.json')}
    write(receipt,value,exclusive=True)
    with log.open('x') as stream:
        value['state']='dispatching'
        write(receipt,value)
        result=subprocess.run([str(main/'.venv/bin/python'),str(wrapper),*common,*args],
                              cwd=main,stdout=stream,stderr=subprocess.STDOUT)
    value.update(state='returned',controller_exit=result.returncode,finished_at=now(),log=ref(log))
    write(receipt,value)


summaries={}
audits=[]
try:
    before=assert_inputs()
    write(BASE/'dispatch-resumption.json', {
        'schema':'h12-controller-resumption-v1','recorded_at':now(),
        'experiment_id':EXPERIMENT,'source_sha256':plan['source_sha256'],
        'original_plan':ref(BASE/'dispatch-plan.json'),'original_stop':ref(BASE/'matrix-stop.json'),
        'resumption_driver':ref(Path(__file__)),'budget':before,
        'reason':'Already scored model_length FAIL has Engine paused, not terminal; '
                 'close known, effect-free failed runs through original Engine, preserve FAIL, '
                 'then dispatch only never-attempted cases in the original frozen order.',
        'no_source_or_freeze_changes':True,'no_repeat_sampling':True,
        'no_threshold_budget_or_scope_changes':True,
    },exclusive=True)
    initialized=json.loads((BASE/'dispatch-receipts/initialize.json').read_bytes())
    assert initialized['state']=='returned' and initialized['controller_exit']==0
    for split in ('dev','holdout'):
        tasks=frozen['dev']['tasks']['dev'] if split=='dev' else frozen['independent']['tasks']['holdout']
        assert [t['task_id'] for t in tasks]==plan['order'][split]
        records=[]
        for index,task in enumerate(tasks,1):
            case=BASE/'live/candidate'/split/task['task_id']/'score.json'
            phase=f'{split}-{index:02d}'
            existing=case.is_file()
            if existing:
                receipt=json.loads((BASE/'dispatch-receipts'/(phase+'.json')).read_bytes())
                assert receipt['state']=='returned', 'REVIEW_UNFINISHED_EXISTING_DISPATCH'
            else:
                invoke(phase,['--split',split,'--task-id',task['task_id'],'--execute-live'])
            assert case.is_file(), 'CONTROLLER_RETURNED_WITHOUT_SCORE; no resend'
            row=json.loads(case.read_bytes())
            assert row['experiment_id']==EXPERIMENT and row['task_id']==task['task_id']
            assert row['arm']=='candidate' and row['split']==split
            if row.get('usage_known') is not True or row['status']=='UNKNOWN':
                raise RuntimeError('UNKNOWN_AWAIT_ACCOUNTING')
            close_scored_pause(row)
            with connect() as conn:
                audit=audit_case(conn,row)
                snapshot=budget_snapshot(conn)
            assert snapshot['unknown_calls']==snapshot['budget']['reserved_micros']==0
            audits.append(audit)
            write(BASE/'pg-case-audits.json',audits)
            records.append(row)
            summary=h12.score_records(records,split,expected_tasks=tasks)
            summaries[split]=summary
            aggregate=BASE/f'aggregate-{split}-{index:02d}.json'
            if aggregate.exists():
                assert json.loads(aggregate.read_bytes())==summary, 'EXISTING_AGGREGATE_CHANGED'
            else:
                write(aggregate,summary,exclusive=True)
            write(BASE/'progress.json',{'updated_at':now(),'summaries':summaries,
                'shared_budget':snapshot,'resumption':ref(BASE/'dispatch-resumption.json')})
            print(json.dumps({'split':split,'evaluated':index,'pass':summary['pass_count'],
                'fail':summary['fail_count'],'not_run':summary['not_run_count'],
                'threshold_met':summary['threshold_met'],'case_calls':row.get('model_calls',0),
                'reused_original_receipt':existing,'shared_budget':snapshot}),flush=True)
            if any(summary.get(k) for k in ('unknown_count','unauthorized_actions','unknown_blind_retries')):
                raise RuntimeError('DIAGNOSTIC_SAFETY_STOP')
    write(BASE/'matrix-complete.json',{'status':'ALL_24_CASES_ATTEMPTED','finished_at':now(),
        'experiment_id':EXPERIMENT,'summaries':summaries,'quality_pass':all(
            s['threshold_met'] for s in summaries.values()),
        'resumption':ref(BASE/'dispatch-resumption.json')},exclusive=True)
    print(json.dumps({'complete_matrix':'ALL_24_CASES_ATTEMPTED',
        'quality_pass':all(s['threshold_met'] for s in summaries.values())}),flush=True)
except BaseException as exc:
    write(BASE/'resume-stop.json',{'state':'HARD_STOP','recorded_at':now(),
        'reason':str(exc),'exception_type':type(exc).__name__,
        'completed_case_audits':len(audits)},exclusive=True)
    raise
