"""Exactly one first model sample per eligible original zero-Run case."""
import json
import threading
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from datetime import datetime,UTC

from common import (BASE,EXPERIMENT,ROOT,SETTINGS,audit_case,budget_snapshot,connect,
                    now,ref,write)
from support import finish_score,plan,runner,validate
from mgar_v3.coordination.engine import Engine
from mgar_v3.evidence.artifacts import ArtifactStore
from mgar_v3.settings import Settings

frozen=plan()
assert not (BASE/'live').exists(),'Recovery evidence must be a fresh separate namespace'
before=validate(idle=True)
assert before==frozen['budget_before'],'Review changed shared budget before dispatch'
configured=Settings()
assert configured.deepseek_api_key is not None,'EXISTING_PROVIDER_CREDENTIAL_REQUIRED'
key=configured.deepseek_api_key.get_secret_value()
engine=Engine(SETTINGS,ArtifactStore(SETTINGS.artifact_root))
stop=threading.Event()
records=[]
audits=[]
try:
    # Initialization does not send a model request or allocate another quota.
    first_task=frozen['tasks'][0]
    prepared=runner(engine,first_task,None,live=False)
    prepared.initialize()
    prepared._primary_pg_facts()
    after_initialize=validate(idle=True)
    assert after_initialize==before
    write(BASE/'initialization.json',{'state':'FROZEN','recorded_at':now(),
        'experiment_id':EXPERIMENT,'plan':ref(BASE/'dispatch-plan.json'),
        'budget_before':before,'budget_after':after_initialize,
        'primary_pg_facts':'ALL_24_VERIFIED','paid_calls':0},exclusive=True)

    def execute(task,index):
        alias=f'recovery-{index:02d}'
        receipt=BASE/'dispatch-receipts'/(alias+'.json')
        score=BASE/'live/candidate/holdout'/task['task_id']/'score.json'
        assert not receipt.exists() and not score.exists(),'NEVER_REDISPATCH_FIRST_SAMPLE'
        validate()
        assert not stop.is_set()
        value={'state':'prepared','phase':alias,'started_at':now(),'task_id':task['task_id'],
               'experiment_id':EXPERIMENT,'plan':ref(BASE/'dispatch-plan.json')}
        write(receipt,value,exclusive=True)
        try:
            authority=runner(engine,task,key)
            value['state']='dispatching';write(receipt,value)
            row=authority.evaluate_task(task,'candidate')
            assert row['experiment_id']==EXPERIMENT and row['task_id']==task['task_id']
            audit=finish_score(engine,row)
            value.update(state='returned',finished_at=now(),score=ref(score),
                model_calls=row.get('model_calls',0),status=row['status'],pg_audit=audit)
            write(receipt,value)
            return row,audit
        except BaseException as exc:
            stop.set();value.update(state='controller_error',finished_at=now(),
                                    error_type=type(exc).__name__,error_code=getattr(exc,'code',None))
            write(receipt,value)
            raise

    pending={};iterator=iter(enumerate(frozen['tasks'],1));failure=None
    with ThreadPoolExecutor(max_workers=2,thread_name_prefix='h12-first-model')as pool:
        def fill():
            while len(pending)<2 and not stop.is_set():
                try:index,task=next(iterator)
                except StopIteration:return
                validate()
                pending[pool.submit(execute,task,index)]=task['task_id']
        fill()
        while pending:
            completed,_=wait(pending,return_when=FIRST_COMPLETED)
            for future in completed:
                pending.pop(future)
                try:
                    row,audit=future.result();records.append(row);audits.append(audit)
                    with connect()as conn:budget=budget_snapshot(conn)
                    assert budget['unknown_calls']==0
                    summary={'recorded_at':now(),'quality_status':'DIAGNOSTIC_ONLY',
                        'finished':len(records),'expected':len(frozen['tasks']),
                        'pass':sum(r['status']=='PASS'for r in records),
                        'fail':sum(r['status']=='FAIL'for r in records),
                        'not_run':sum(r['status']=='NOT_RUN'for r in records),
                        'budget':budget,'records':records,'audits':audits}
                    write(BASE/'progress.json',summary)
                    print(json.dumps({k:v for k,v in summary.items()if k not in {'records','audits'}}),flush=True)
                except BaseException as exc:
                    stop.set();failure=exc
            if not stop.is_set():fill()
        if failure:raise failure
    assert len(records)==len(frozen['tasks']) and not stop.is_set()
    after=validate(idle=True)
    calls=sum(r.get('model_calls',0)for r in records)
    cost=sum(r.get('settled_cost_micros',r.get('cost_micros',0))for r in records)
    assert after['budget']['used_calls']-before['budget']['used_calls']==calls
    assert after['budget']['settled_micros']-before['budget']['settled_micros']==cost
    write(BASE/'recovery-complete.json',{'status':'ALL_ELIGIBLE_CASES_ATTEMPTED',
        'finished_at':now(),'quality_status':'DIAGNOSTIC_ONLY','mvp':'NOT_RELEASED',
        'experiment_id':EXPERIMENT,'plan':ref(BASE/'dispatch-plan.json'),
        'budget_before':before,'budget_after':after,'new_model_calls':calls,
        'new_settled_cost_micros':cost,'original_scores_unchanged':True,
        'records':records,'audits':audits},exclusive=True)
except BaseException as exc:
    stop.set();write(BASE/'recovery-stop.json',{'state':'HARD_STOP','recorded_at':now(),
        'exception_type':type(exc).__name__,'reason':str(exc),'finished_cases':len(records)},exclusive=True)
    raise
finally:
    engine.close()
