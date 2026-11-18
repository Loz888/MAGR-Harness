"""Owner-requested two-case scheduler over the original frozen evaluation."""
import json
import threading
import time
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait

from common import (BASE, EXPERIMENT, ROOT, SETTINGS, audit_case, budget_snapshot,
                    connect, now, ref, sha, write)
from parallel_support import (PLAN, close_scored_pause, load_frozen, make_runner,
                              never_started, validate_inputs)
from mgar_v3.coordination.engine import Engine
from mgar_v3.evidence.artifacts import ArtifactStore
from mgar_v3.quality_eval import h12
from mgar_v3.settings import Settings

schedule=json.loads((BASE/'parallel-dispatch-plan-v2.json').read_bytes())
assert schedule['workers']==2 and schedule['source_sha256']==PLAN['source_sha256']
assert schedule['original_dispatch_plan']==ref(BASE/'dispatch-plan.json')
for item in schedule['controller_files']:
    assert sha(ROOT/item['path'])==item['sha256'],'PARALLEL_DISPATCHER_CHANGED'
assert schedule['rehearsal']==ref(BASE/'parallel-rehearsal.json')
assert json.loads((BASE/'parallel-rehearsal.json').read_bytes())['status']=='PASS'
transition=json.loads((BASE/'scheduling-transition.json').read_bytes())
assert transition['state']=='original_case_reconciled_controller_stopped'
validate_inputs(idle=True)
frozen=load_frozen()
configured=Settings()
assert configured.deepseek_api_key is not None,'EXISTING_CREDENTIAL_REQUIRED'
api_key=configured.deepseek_api_key.get_secret_value()
stop=threading.Event()
engine=Engine(SETTINGS,ArtifactStore(SETTINGS.artifact_root))
summaries={}
audits={}
timings=[]
started=time.monotonic()


def execute_case(task,phase,admission):
    receipt=BASE/'dispatch-receipts'/(phase+'.json')
    log=BASE/'dispatch-logs'/(phase+'.log')
    assert not receipt.exists() and not log.exists(),'NEVER_REDISPATCH_EXISTING_RECEIPT'
    never_started(task)
    validate_inputs()
    assert not stop.is_set(),'SCHEDULER_STOPPED_BEFORE_DISPATCH'
    value={'phase':phase,'state':'prepared','started_at':now(),'experiment_id':EXPERIMENT,
           'quality_freeze':PLAN['quality_freeze'],'source_sha256':PLAN['source_sha256'],
           'parallel_plan':ref(BASE/'parallel-dispatch-plan-v2.json'),
           'admission':admission,
           'shared_engine_backend_pid':engine.connection.info.backend_pid}
    write(receipt,value,exclusive=True)
    began=time.monotonic()
    with log.open('x')as stream:
        value.update(state='dispatching',worker=threading.current_thread().name)
        write(receipt,value)
        stream.write(json.dumps({'state':'dispatching','at':value['started_at']})+'\n')
        stream.flush()
        try:
            runner=make_runner(engine,task,api_key)
            # The original method revalidates live permission, all frozen
            # inputs, complete public readiness and original PG authority.
            row=runner.evaluate_task(task,'candidate')
            assert row['experiment_id']==EXPERIMENT and row['task_id']==task['task_id']
            assert row['split']==task['split'] and row['arm']=='candidate'
            if row.get('usage_known')is not True or row['status']=='UNKNOWN':
                stop.set()
                raise RuntimeError('UNKNOWN_AWAIT_ACCOUNTING')
            close_scored_pause(engine,row)
            with connect()as conn:
                audit=audit_case(conn,row)
            if row.get('unauthorized_actions')or row.get('unknown_blind_retries'):
                stop.set()
                raise RuntimeError('DIAGNOSTIC_SAFETY_STOP')
            value.update(state='returned',controller_exit=0,finished_at=now(),
                score=ref(BASE/'live/candidate'/task['split']/task['task_id']/'score.json'),
                wall_seconds=time.monotonic()-began)
            stream.write(json.dumps({'state':'returned','at':value['finished_at'],
                'status':row['status'],'model_calls':row['model_calls']})+'\n')
            stream.flush()
            value['log']=ref(log)
            write(receipt,value)
            return row,audit,value
        except BaseException as exc:
            stop.set()
            value.update(state='controller_error',finished_at=now(),
                         error_type=type(exc).__name__,error_code=getattr(exc,'code',None))
            stream.write(json.dumps({'state':'controller_error','at':value['finished_at'],
                'error_type':value['error_type'],'error_code':value['error_code']})+'\n')
            stream.flush()
            value['log']=ref(log)
            write(receipt,value)
            raise


def aggregate(split,tasks):
    records=[]
    for task in tasks:
        path=BASE/'live/candidate'/split/task['task_id']/'score.json'
        if path.exists() and (split,task['task_id']) in audits:
            records.append(json.loads(path.read_bytes()))
    summary=h12.score_records(records,split,expected_tasks=tasks)
    summaries[split]=summary
    with connect()as conn:
        snapshot=budget_snapshot(conn)
    if snapshot['unknown_calls']:
        stop.set()
        raise RuntimeError('UNKNOWN_AWAIT_ACCOUNTING')
    write(BASE/'pg-case-audits.json',list(audits.values()))
    write(BASE/f'parallel-aggregate-{split}-{len(records):02d}-{time.time_ns()}.json',
          summary,exclusive=True)
    write(BASE/'progress.json',{'updated_at':now(),'summaries':summaries,
        'shared_budget':snapshot,'parallel_plan':ref(BASE/'parallel-dispatch-plan-v2.json')})
    print(json.dumps({'split':split,'evaluated':len(records),'pass':summary['pass_count'],
        'fail':summary['fail_count'],'not_run':summary['not_run_count'],
        'threshold_met':summary['threshold_met'],'workers':2,'shared_budget':snapshot}),flush=True)
    if any(summary.get(k)for k in ('unknown_count','unauthorized_actions','unknown_blind_retries')):
        stop.set()
        raise RuntimeError('DIAGNOSTIC_SAFETY_STOP')


try:
    write(BASE/'parallel-start.json',{'started_at':now(),'plan':ref(BASE/'parallel-dispatch-plan-v2.json'),
        'budget':validate_inputs(idle=True),'engine_backend_pid':engine.connection.info.backend_pid},exclusive=True)
    for split in ('dev','holdout'):
        tasks=frozen['dev']['tasks']['dev']if split=='dev'else frozen['independent']['tasks']['holdout']
        assert [t['task_id']for t in tasks]==PLAN['order'][split]
        remaining=[]
        for index,task in enumerate(tasks,1):
            path=BASE/'live/candidate'/split/task['task_id']/'score.json'
            phase=f'{split}-{index:02d}'
            receipt=BASE/'dispatch-receipts'/(phase+'.json')
            if path.exists():
                value=json.loads(receipt.read_bytes())
                assert value['state']=='returned','RECONCILE_EXISTING_RECEIPT_FIRST'
                row=json.loads(path.read_bytes())
                assert row['usage_known']is True and row['status']!='UNKNOWN'
                close_scored_pause(engine,row)
                with connect()as conn:
                    audits[(split,task['task_id'])]=audit_case(conn,row)
            else:
                assert not receipt.exists(),'NO_BLIND_RESEND'
                never_started(task)
                remaining.append((task,phase))
        aggregate(split,tasks)
        pending={}
        iterator=iter(remaining)
        failure=None
        with ThreadPoolExecutor(max_workers=2,thread_name_prefix='h12-case')as pool:
            def fill():
                while len(pending)<2 and not stop.is_set():
                    try:
                        task,phase=next(iterator)
                    except StopIteration:
                        return
                    validate_inputs()
                    admission={'phase':phase,'admitted_at':now(),
                        'canonical_index':int(phase.rsplit('-',1)[1]),'workers':2}
                    admission_path=BASE/'parallel-admissions'/(phase+'.json')
                    write(admission_path,admission,exclusive=True)
                    pending[pool.submit(execute_case,task,phase,ref(admission_path))]=(task,phase)
            fill()
            while pending:
                finished,_=wait(pending,return_when=FIRST_COMPLETED)
                for future in finished:
                    task,phase=pending.pop(future)
                    try:
                        row,audit,receipt=future.result()
                        audits[(split,task['task_id'])]=audit
                        timings.append({'phase':phase,'started_at':receipt['started_at'],
                            'finished_at':receipt['finished_at'],'wall_seconds':receipt['wall_seconds']})
                        write(BASE/'parallel-case-timings.json',timings)
                        aggregate(split,tasks)
                    except BaseException as exc:
                        stop.set()
                        failure=exc
                if not stop.is_set():
                    fill()
            if failure:
                raise failure
        assert not stop.is_set(),'PARALLEL_SAFETY_STOP'
        validate_inputs(idle=True)
        for task in tasks:
            row=json.loads((BASE/'live/candidate'/split/task['task_id']/'score.json').read_bytes())
            with connect()as conn:
                audit_case(conn,row)
        aggregate(split,tasks)
        # No holdout is admitted until all 12 original dev first samples have
        # immutable scores and terminal, fully settled PG facts.
    write(BASE/'matrix-complete.json',{'status':'ALL_24_CASES_ATTEMPTED','finished_at':now(),
        'experiment_id':EXPERIMENT,'summaries':summaries,'quality_pass':all(
            s['threshold_met']for s in summaries.values()),'parallel_plan':ref(BASE/'parallel-dispatch-plan-v2.json'),
        'parallel_wall_seconds':time.monotonic()-started},exclusive=True)
    print(json.dumps({'complete_matrix':'ALL_24_CASES_ATTEMPTED','quality_pass':all(
        s['threshold_met']for s in summaries.values())}),flush=True)
except BaseException as exc:
    stop.set()
    write(BASE/'parallel-stop.json',{'state':'HARD_STOP','recorded_at':now(),'reason':str(exc),
        'exception_type':type(exc).__name__,'completed_case_audits':len(audits)},exclusive=True)
    raise
finally:
    engine.close()
