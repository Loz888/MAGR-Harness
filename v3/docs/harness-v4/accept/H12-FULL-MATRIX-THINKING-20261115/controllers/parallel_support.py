"""External scheduling only; the frozen Runner/Engine remain unmodified."""
import json
from pathlib import Path

from common import (BASE, EXPERIMENT, ROOT, SETTINGS, audit_case, budget_snapshot,
                    connect, now, ref, sha, write)
from scripts.verify import source_fingerprint
from mgar_v3.quality_eval.h12_quality_runner import QualityRunner, load_quality_freeze

PLAN=json.loads((BASE/'dispatch-plan.json').read_bytes())
MANIFEST=ROOT/PLAN['quality_freeze']['path']


def validate_inputs(*, idle=False):
    assert source_fingerprint(ROOT)==PLAN['source_sha256'], 'FROZEN_SOURCE_CHANGED'
    assert sha(BASE/'driver.py')==PLAN['driver_sha256']
    assert sha(BASE/'common.py')==PLAN['common_sha256']
    for entry in PLAN['immutable_inputs']:
        assert sha(ROOT/entry['path'])==entry['sha256'], 'FROZEN_INPUT_CHANGED'
    with connect() as conn:
        observed=budget_snapshot(conn)
    assert observed['unknown_calls']==0, 'UNKNOWN_AWAIT_ACCOUNTING'
    assert observed['budget']['limit_micros']==12_000_000
    if idle:
        assert observed['budget']['reserved_micros']==0, 'WAIT_FOR_ALL_ORIGINAL_RECEIPTS'
    return observed


class EngineView:
    """Only filesystem roots differ; every method/store delegates to one Engine."""
    def __init__(self, engine, split, task_id):
        from mgar_v3.domain import digest
        self._engine=engine
        root=Path(engine.settings.harness_root)/'parallel-cases'/EXPERIMENT/split/digest(task_id)
        self.settings=engine.settings.model_copy(update={'harness_root':root})
        assert self.settings.model_copy(update={'harness_root':engine.settings.harness_root})==engine.settings

    def __getattr__(self,name):
        return getattr(self._engine,name)


def make_runner(engine, task, api_key):
    return QualityRunner(EngineView(engine,task['split'],task['task_id']),
        experiment_id=EXPERIMENT,evidence=BASE/'live',freeze_path=MANIFEST,
        freeze_sha256=PLAN['quality_freeze']['sha256'],api_key=api_key,execute_live=True,root=ROOT)


def load_frozen():
    return load_quality_freeze(ROOT,MANIFEST,PLAN['quality_freeze']['sha256'])


def close_scored_pause(engine,row):
    if not row.get('run_id'):
        return
    # One original Engine transaction locks both case facts and the budget
    # while the administrative close is compared. Other cases may otherwise
    # legitimately reserve/settle concurrently.
    with engine.transaction() as conn:
        task=conn.execute('SELECT t.task_id,t.tree_hash,r.status FROM app.harness_tasks t '
            'JOIN app.runs r USING(run_id) WHERE t.run_id=%s',(row['run_id'],)).fetchone()
        assert task is not None
        if task['status'] in {'succeeded','failed','cancelled'}:
            return
        assert row['status']=='FAIL' and task['status']=='paused','UNEXPECTED_NONTERMINAL_SCORE'
        assert task['tree_hash']==row['final_tree'] and row['usage_known'] is True
        assert row['reserved_micros']==0
        usages=conn.execute('SELECT status,settled_micros FROM app.usage_calls '
                           'WHERE run_id=%s',(row['run_id'],)).fetchall()
        assert len(usages)==row['model_calls'] and all(u['status']=='known'for u in usages)
        assert sum(u['settled_micros']or 0 for u in usages)==row['settled_cost_micros']
        operations=conn.execute('SELECT state FROM app.harness_operations WHERE run_id=%s',
                                (row['run_id'],)).fetchall()
        assert all(o['state']in {'known','cancelled','denied'}for o in operations)
        assert conn.execute('SELECT count(*) AS n FROM app.actions WHERE run_id=%s',
                            (row['run_id'],)).fetchone()['n']==0
        path=BASE/'administrative-closures'/(str(row['run_id'])+'.json')
        assert not path.exists(),'REVIEW_INTERRUPTED_CLOSURE'
        before=budget_snapshot(conn)
        entry={'state':'prepared','recorded_at':now(),'experiment_id':EXPERIMENT,
            'run_id':row['run_id'],'task_id':row['task_id'],'score_status':row['status'],
            'score_reason':row['reason'],'original_engine_status':task['status'],
            'tree_hash':row['final_tree'],'model_calls':row['model_calls'],
            'settled_cost_micros':row['settled_cost_micros'],
            'score':ref(BASE/'live/candidate'/row['split']/row['task_id']/'score.json'),
            'method':'original shared Engine.harness.cancel inside its unchanged transaction',
            'budget_before':before}
        write(path,entry,exclusive=True)
        closed=engine.harness.cancel(task['task_id'])
        assert closed['status']=='cancelled' and closed['tree_hash']==row['final_tree']
        after=budget_snapshot(conn)
        assert after==before,'CLOSURE_MUST_NOT_CHANGE_BUDGET_OR_CALL_MODEL'
    with connect()as conn:
        audit=audit_case(conn,row)
    entry.update(state='closed',finished_at=now(),engine_status='cancelled',
                 budget_after=after,audit=audit)
    write(path,entry)


def never_started(task):
    score=BASE/'live/candidate'/task['split']/task['task_id']/'score.json'
    assert not score.exists(),'NEVER_REPEAT_FIRST_SAMPLE'
    with connect()as conn:
        row=conn.execute("SELECT count(*) AS n FROM app.harness_tasks WHERE experiment_id=%s "
            "AND evaluation_profile->>'task_id'=%s AND evaluation_profile->>'split'=%s",
            (EXPERIMENT,task['task_id'],task['split'])).fetchone()
    assert row['n']==0,'EXISTING_PG_SAMPLE_REQUIRES_RECONCILIATION'
