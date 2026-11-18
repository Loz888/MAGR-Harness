"""Record separate first actual model samples; never merge primary scores."""
import json
from datetime import datetime
from pathlib import Path

from common import BASE,EXPERIMENT,ROOT,audit_case,budget_snapshot,connect,now,ref,sha,write
from support import validate
from mgar_v3.quality_eval import h12
from mgar_v3.harness.project_commands import parse_tests
from scripts.verify import source_fingerprint

assert (BASE/'recovery-complete.json').exists()or(BASE/'recovery-stop.json').exists()
dispatch=json.loads((BASE/'dispatch-plan.json').read_bytes())
assert not(BASE/'final-report.json').exists(),'Keep an earlier complete export immutable'
assert source_fingerprint(ROOT)==dispatch['source_sha256']
for item in dispatch['controller_files']+dispatch['immutable_inputs']:
 assert sha(ROOT/item['path'])==item['sha256']
private={'reasoning_content','reasoning','provider_context','chain_of_thought','internal_reasoning',
         'hidden_reasoning','authorization','api_key'}
def public(value):
 if isinstance(value,dict):return{k:public(v)for k,v in value.items()if k.lower()not in private}
 if isinstance(value,list):return[public(v)for v in value]
 return value

cases=[];policy=[];intervals=[]
with connect()as conn:
 budget=budget_snapshot(conn)
 for index,task in enumerate(dispatch['tasks'],1):
  directory=BASE/'live/candidate/holdout'/task['task_id'];score_path=directory/'score.json'
  if not score_path.exists():
   cases.append({'task':h12.project_task(task),'status':'NOT_RUN',
                 'reason':'RECOVERY_STOP_BEFORE_SCORE','model_calls':0});continue
  score=json.loads(score_path.read_bytes())
  admission=json.loads((BASE/'dispatch-receipts'/f'recovery-{index:02d}.json').read_bytes())
  assert admission['state']=='returned' and admission['task_id']==task['task_id']
  audit=audit_case(conn,score)if score.get('usage_known')is True else None
  target=BASE/'export'/f'case-{index:02d}';target.mkdir(parents=True,exist_ok=True)
  runtime_path=directory/'runtime-result.json'
  runtime=json.loads(runtime_path.read_bytes())if runtime_path.exists()else{}
  rows=conn.execute('SELECT u.call_id,u.step_key,u.status,u.usage,u.result,u.settled_micros,'
   'u.reserved_micros,o.intent,o.state AS operation_state FROM app.usage_calls u '
   'LEFT JOIN app.harness_operations o ON o.model_call_id=u.call_id '
   'WHERE u.run_id=%s ORDER BY o.created_at,u.step_key',(score.get('run_id'),)).fetchall()if score.get('run_id')else[]
  write(target/'model-calls.json',public(rows),exclusive=True)
  write(target/'runtime-public.json',public(runtime),exclusive=True)
  patch=runtime.get('diff')or{}
  if isinstance(patch,dict):patch=patch.get('patch','')
  if patch:(target/'patch.diff').write_text(str(patch))
  providers=[]
  for p in sorted((directory/'provider').glob('*.json')):
   receipt=json.loads(p.read_bytes());options=receipt.get('request_options',{})
   assert options=={'thinking':{'type':'enabled'},'reasoning_effort':'max'}
   assert receipt['physical_attempts']==1;policy.append(options)
   if receipt.get('started_at')and receipt.get('finished_at'):
    intervals.append((receipt['started_at'],receipt['finished_at']))
   providers.append({'receipt':ref(p),'request_options':options,'physical_attempts':1,
                     'finish_reason':receipt.get('finish_reason'),'error_code':receipt.get('error_code'),
                     'usage':receipt.get('normalized_usage')})
  logs=[]
  for phase in ('environment-private','baseline-private','judge-private'):
   judge_path=directory/phase/'judge-result.json'
   if not judge_path.exists():continue
   j=json.loads(judge_path.read_bytes())
   for receipt in j.get('receipts',[]):
    log=Path(receipt['private_log']);assert log.resolve().is_relative_to(directory.resolve())
    raw=log.read_bytes()
    assert sha(log)==receipt['log_sha256'] and len(raw)==receipt['log_bytes']
    text=raw.decode('utf-8',errors='replace')
    kind='python'if task['language']=='python'else'go'if task['language']=='go'else'node'
    parsed=parse_tests(kind,text,receipt['exit_code'])
    logs.append({'phase':phase,'judge_status':j['status'],'judge_reason':j['reason'],
     'argv':receipt['argv'],'state':receipt['state'],'exit_code':receipt['exit_code'],
     'log_matches_receipt':True,'complete':receipt.get('complete'),
     'log_truncated':receipt.get('log_truncated'),
     'parsed':dict(zip(('status','tests_run','tests_failed','tests_skipped','detail'),parsed)),
     'log':ref(log),'tail':text[-4500:]if receipt['exit_code']else None})
  record={'task':h12.project_task(task),'public_acceptance':task.get('acceptance',{}).get('required',[]),
   'whole_task_conditions':['合格终态','原独立Judge通过','最终树一致','原公开回归无新增失败',
                            '类型、权限、资源、就绪/清理守卫通过'],
   'score':score,'pg_audit':audit,'status':score['status'],'reason':score['reason'],
   'dispatch_receipt':ref(BASE/'dispatch-receipts'/f'recovery-{index:02d}.json'),
   'started_at':admission['started_at'],'finished_at':admission['finished_at'],
   'wall_seconds':(datetime.fromisoformat(admission['finished_at'])-
                   datetime.fromisoformat(admission['started_at'])).total_seconds(),
   'model_calls':score.get('model_calls',0),'cost_micros':score.get('settled_cost_micros',score.get('cost_micros'))or 0,
   'judge_sha256':task['judge_sha256'],'provider_receipts':providers,'executed_logs':logs,
   'evidence':{'score':ref(score_path),'model_calls':ref(target/'model-calls.json'),
    'runtime':ref(target/'runtime-public.json'),'patch':ref(target/'patch.diff')if patch else None}}
  write(target/'case.json',record,exclusive=True);cases.append(record)
calls=sum(c['model_calls']for c in cases);cost=sum(c.get('cost_micros',0)for c in cases)
assert budget['budget']['used_calls']-dispatch['budget_before']['budget']['used_calls']==calls
assert budget['budget']['settled_micros']-dispatch['budget_before']['budget']['settled_micros']==cost
assert len(policy)==calls
events=[]
for a,b in intervals:events.extend([(datetime.fromisoformat(a),1),(datetime.fromisoformat(b),-1)])
active=peak=0
for _,delta in sorted(events):active+=delta;peak=max(peak,active)
# Use the explicit authority reference rather than a new grading aggregate.
authority=json.loads((ROOT/dispatch['recovery_authority']['path']).read_bytes())
primary=json.loads((ROOT/authority['primary_report']['path']).read_bytes())
report={'schema':'h12-first-model-recovery-report-v1','recorded_at':now(),'experiment_id':EXPERIMENT,
 'source_commit':dispatch['source_commit'],'source_sha256':dispatch['source_sha256'],
 'dispatch_plan':ref(BASE/'dispatch-plan.json'),'recovery_authority':dispatch['recovery_authority'],
 'quality_freeze':dispatch['quality_freeze'],'controller_files':dispatch['controller_files'],
 'quality_status':'DIAGNOSTIC_ONLY','mvp':'NOT_RELEASED','primary_report':authority['primary_report'],
 'primary_quality_scores_unchanged':True,'original_primary':primary['summaries'],
 'primary_model_entered':primary['model_entered_count'],
 'recovery_model_entered':sum(bool(c['model_calls'])for c in cases),
 'total_questions_with_actual_model':primary['model_entered_count']+sum(bool(c['model_calls'])for c in cases),
 'counts':{'pass':sum(c['status']=='PASS'for c in cases),'fail':sum(c['status']=='FAIL'for c in cases),
           'not_run':sum(c['status']=='NOT_RUN'for c in cases),
           'unknown':sum(c['status']=='UNKNOWN'for c in cases)},
 'new_model_calls':calls,'new_settled_cost_micros':cost,'budget_after':budget,
 'request_policy':{'thinking':{'type':'enabled'},'reasoning_effort':'max','verified_receipts':len(policy),
                   'peak_physical_requests':peak},'cases':cases,
 'independence':'Previously seen benchmark diagnostics; future quality needs a new independent holdout'}
write(BASE/'final-report.json',report,exclusive=True)
write(ROOT/'docs/harness-v4/accept/H12-FIRST-MODEL-RECOVERY-20261115/results.json',report,exclusive=True)
print(json.dumps({k:report[k]for k in ('counts','new_model_calls','new_settled_cost_micros',
 'total_questions_with_actual_model','budget_after')},ensure_ascii=False))
