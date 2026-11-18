"""Zero-model rehearsal of shared Engine and isolated per-case filesystem I/O."""
import json
import os
import subprocess
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from common import BASE, SETTINGS, now, ref, write
from parallel_support import EngineView, load_frozen, make_runner, validate_inputs
from mgar_v3.coordination.engine import Engine
from mgar_v3.evidence.artifacts import ArtifactStore
from mgar_v3.harness.service import HarnessCodingService
from mgar_v3.models.deepseek_policy import deepseek_request_options
from mgar_v3.quality_eval.h12_live import RecordedFlashModel

before=validate_inputs(idle=True)
engine=Engine(SETTINGS,ArtifactStore(SETTINGS.artifact_root))
checks={}
try:
    frozen=load_frozen()
    tasks=frozen['dev']['tasks']['dev'][-2:]
    views=[EngineView(engine,t['split'],t['task_id'])for t in tasks]
    assert views[0].settings.harness_root!=views[1].settings.harness_root
    assert all(v.harness is engine.harness and v.artifacts is engine.artifacts for v in views)
    assert all(v.transaction.__self__ is engine for v in views)
    checks['one_original_engine_store_and_artifact_authority']=True
    checks['distinct_case_runtime_roots']=True
    barrier=threading.Barrier(2)
    mutex=threading.Lock()
    concurrency={'active':0,'maximum':0}

    def query(view):
        barrier.wait(timeout=15)
        backends=[]
        for _ in range(6):
            with view.transaction()as conn:
                with mutex:
                    concurrency['active']+=1
                    concurrency['maximum']=max(concurrency['active'],concurrency['maximum'])
                try:
                    backends.append(conn.execute('SELECT pg_backend_pid() AS pid').fetchone()['pid'])
                    time.sleep(.01)
                finally:
                    with mutex:concurrency['active']-=1
        return backends
    with ThreadPoolExecutor(max_workers=2)as pool:
        backends=list(pool.map(query,views))
    assert concurrency['maximum']==1
    assert set(sum(backends,[]))=={engine.connection.info.backend_pid}
    checks['actual_pg_transactions_serialized_over_one_backend']=True
    checks['pg_transactions_exercised']=12

    def validate(task):
        runner=make_runner(engine,task,None)
        current=runner._validate_frozen_execution()
        return current==engine.harness.get_experiment(runner.experiment_id)['config']
    with ThreadPoolExecutor(max_workers=2)as pool:
        assert all(pool.map(validate,tasks))
    checks['original_frozen_execution_guards_accept_isolated_roots']=True

    with tempfile.TemporaryDirectory(prefix='magr-h12-parallel-rehearsal-')as temporary:
        root=Path(temporary)
        artifacts=ArtifactStore(root/'cas')
        contents=[b'shared-artifact'*1000,b'case-one'*12000,b'case-two'*12000,b'shared-artifact'*1000]
        with ThreadPoolExecutor(max_workers=2)as pool:
            hashes=list(pool.map(artifacts.put,contents))
        assert [artifacts.read(h)for h in hashes]==contents and hashes[0]==hashes[-1]
        checks['parallel_cas_distinct_and_identical_content_round_trips']=True
        models=[RecordedFlashModel(api_key='offline-rehearsal-never-send',
            evidence=root/f'provider-{i}',artifacts=artifacts)for i in range(2)]
        models[0].provider_receipts.append({'offline':True})
        assert models[1].provider_receipts==[] and models[0].evidence!=models[1].evidence
        assert deepseek_request_options()=={'thinking':{'type':'enabled'},'reasoning_effort':'max'}
        checks['per_case_provider_receipt_state_isolated_no_complete_called']=True
        environment={**os.environ,'GIT_CONFIG_NOSYSTEM':'1','GIT_CONFIG_GLOBAL':'/dev/null',
                     'GIT_TERMINAL_PROMPT':'0'}
        sources=[]
        for i in range(2):
            source=root/f'source-{i}';source.mkdir();(source/'example.py').write_text(f'VALUE = {i}\n')
            for command in (['git','init','-q'],['git','add','example.py'],
                            ['git','-c','user.name=Codex','-c','user.email=codex@localhost',
                             '-c','core.hooksPath=/dev/null','commit','-qm','offline fixture']):
                subprocess.run(command,cwd=source,env=environment,check=True,capture_output=True)
            sources.append(source)
        services=[HarnessCodingService(root/f'case-{i}',actor=SETTINGS.local_actor,
            image_lock=Path('/dev/null'),engine=views[i])for i in range(2)]
        def register(i):
            return services[i].register_project(str(sources[i]))
        with ThreadPoolExecutor(max_workers=2)as pool:
            registrations=list(pool.map(register,range(2)))
        for i in range(2):
            projects=services[i].list_projects()
            assert len(projects)==1 and projects[0]['project_id']==registrations[i]['project_id']
            assert Path(projects[0]['path'])==sources[i]
        checks['original_project_registries_concurrently_isolated']=True
    after=validate_inputs(idle=True)
    assert after==before
    checks['original_shared_budget_exactly_unchanged']=True
    checks['paid_model_calls']=0
    write(BASE/'parallel-rehearsal.json',{'schema':'h12-parallel-zero-model-rehearsal-v1',
        'status':'PASS','recorded_at':now(),'workers':2,'checks':checks,
        'budget_before':before,'budget_after':after,'source_unchanged':True,
        'engine_backend_pid':engine.connection.info.backend_pid,
        'rehearsal_source':ref(Path(__file__))},exclusive=True)
    print(json.dumps({'status':'PASS','checks':checks,'budget':after},ensure_ascii=False))
finally:
    engine.close()
