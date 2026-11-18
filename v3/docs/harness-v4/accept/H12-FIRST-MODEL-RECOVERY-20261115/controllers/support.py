"""One original Engine, exact new freeze, and per-case filesystem isolation."""
import json
from pathlib import Path

from common import (BASE, EXPERIMENT, ROOT, SETTINGS, audit_case, budget_snapshot,
                    connect, now, ref, sha, write)
from scripts.verify import source_fingerprint
from mgar_v3.domain import digest
from mgar_v3.quality_eval.h12_first_model_recovery import FirstModelRecoveryRunner


def plan():
    return json.loads((BASE/'dispatch-plan.json').read_bytes())


def validate(*,idle=False):
    frozen=plan()
    assert source_fingerprint(ROOT)==frozen['source_sha256'],'FROZEN_SOURCE_CHANGED'
    for item in frozen['controller_files']+frozen['immutable_inputs']:
        assert sha(ROOT/item['path'])==item['sha256'],'FROZEN_DISPATCH_INPUT_CHANGED'
    with connect()as conn:
        observed=budget_snapshot(conn)
    assert observed['unknown_calls']==0,'UNKNOWN_AWAIT_ACCOUNTING'
    assert observed['budget']['limit_micros']==12_000_000
    assert observed['budget']['call_limit']==1536
    if idle:
        assert observed['budget']['reserved_micros']==0,'UNSETTLED_CALLS'
    return observed


class EngineView:
    def __init__(self,engine,task):
        self._engine=engine
        self.settings=engine.settings.model_copy(update={'harness_root':
            Path(engine.settings.harness_root)/'first-model-cases'/EXPERIMENT/digest(task['task_id'])})
    def __getattr__(self,name):
        return getattr(self._engine,name)


def runner(engine,task,key=None,*,live=True):
    frozen=plan()
    document=ROOT/frozen['recovery_authority']['path']
    return FirstModelRecoveryRunner(EngineView(engine,task),experiment_id=EXPERIMENT,
        evidence=BASE/'live',recovery_path=document,
        recovery_sha256=frozen['recovery_authority']['sha256'],
        api_key=key,execute_live=live,root=ROOT)


def finish_score(engine,row):
    assert row['usage_known']is True and row['reserved_micros']==0,'UNKNOWN_AWAIT_ACCOUNTING'
    if row.get('run_id'):
        with connect()as conn:
            task=conn.execute('SELECT t.task_id,t.tree_hash,r.status FROM app.harness_tasks t '
                'JOIN app.runs r USING(run_id) WHERE t.run_id=%s',(row['run_id'],)).fetchone()
        assert task
        if task['status']not in {'succeeded','failed','cancelled'}:
            assert task['status']=='paused' and row['status']=='FAIL'
            path=BASE/'administrative-closures'/(row['run_id']+'.json')
            entry={'state':'prepared','recorded_at':now(),'run_id':row['run_id'],
                'score':ref(BASE/'live/candidate/holdout'/row['task_id']/'score.json'),
                'reason':'Already-scored fully-known paused FAIL; close, never continue/resample'}
            write(path,entry,exclusive=True)
            with engine.transaction()as conn:
                current=engine.harness.get(task['task_id'])
                assert current['status']=='paused' and current['tree_hash']==row['final_tree']
                usages=conn.execute('SELECT status,settled_micros FROM app.usage_calls '
                                   'WHERE run_id=%s',(row['run_id'],)).fetchall()
                assert len(usages)==row['model_calls'] and all(u['status']=='known'for u in usages)
                assert sum(u['settled_micros']or 0 for u in usages)==row['settled_cost_micros']
                operations=conn.execute('SELECT state FROM app.harness_operations WHERE run_id=%s',
                                        (row['run_id'],)).fetchall()
                assert all(o['state']in {'known','cancelled','denied'}for o in operations)
                assert conn.execute('SELECT count(*)AS n FROM app.actions WHERE run_id=%s',
                                    (row['run_id'],)).fetchone()['n']==0
                before=budget_snapshot(conn)
                closed=engine.harness.cancel(task['task_id'])
                after=budget_snapshot(conn)
                assert before==after and closed['tree_hash']==row['final_tree']
            entry.update(state='closed',finished_at=now(),budget_before=before,budget_after=after)
            write(path,entry)
    with connect()as conn:
        audit=audit_case(conn,row)
    assert not row.get('unauthorized_actions') and not row.get('unknown_blind_retries')
    return audit
