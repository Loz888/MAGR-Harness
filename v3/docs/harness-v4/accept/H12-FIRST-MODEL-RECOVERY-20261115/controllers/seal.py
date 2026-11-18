"""Seal only original unstarted cases after all fresh zero-model preparation."""
import ast
import json
import subprocess

from common import BASE,EXPERIMENT,ROOT,SETTINGS,budget_snapshot,connect,now,ref,sha,write
from scripts.verify import source_fingerprint
from mgar_v3.quality_eval.h12_first_model_recovery import FirstModelRecoveryRunner,SCHEMA
from mgar_v3.quality_eval.h12_quality_runner import load_quality_freeze
from types import SimpleNamespace

source=source_fingerprint(ROOT)
engineering=json.loads((ROOT/'docs/harness-v4/accept/H12-FIRST-MODEL-RECOVERY-20261115/engineering.json').read_bytes())
assert source==engineering['source_sha256'] and engineering['status']=='PASS'
quality=BASE/'quality-frozen.json'
frozen=load_quality_freeze(ROOT,quality,sha(quality))
assert frozen['document']['phase']=='FROZEN' and frozen['dev']['candidate_source_sha256']==source
primary_path=ROOT/'docs/harness-v4/accept/H12-FULL-MATRIX-THINKING-20261115/results.json'
primary=json.loads(primary_path.read_bytes())
cases=[json.loads((ROOT/r['path']).read_bytes())for r in primary['case_records']]
eligible=[c['score']['task_id']for c in cases if c['score']['status']=='NOT_RUN']
assert len(eligible)==3
tasks=[t for t in frozen['independent']['tasks']['holdout']if t['task_id']in eligible]
assert len(tasks)==3
authority=BASE/'recovery-authority.json'
document={'schema':SCHEMA,'status':'USER_REQUESTED','recorded_at':now(),
 'user_request':'再进行新一轮的真是模型测试，每一题都测试，并给出完整的记录文档，给出错误的原因和测试的问题和通过条件',
 'followup':'Only first actual model samples for original NOT_RUN/no-Run/no-send cases; primary retained',
 'quality_status':'DIAGNOSTIC_ONLY','allow_model_resample':False,'allow_score_replacement':False,
 'allow_unknown_retry':False,'allow_publication':False,'shared_budget_micros':12000000,
 'shared_call_limit':1536,'thinking':{'type':'enabled'},'reasoning_effort':'max',
 'primary_report':ref(primary_path),'quality_freeze':ref(quality),
 'eligible_task_ids':sorted(eligible),'max_first_model_samples':len(eligible),
 'source_commit':subprocess.check_output(['git','rev-parse','HEAD'],cwd=ROOT,text=True).strip(),
 'source_sha256':source,'independence':'ALREADY_SEEN_DIAGNOSTIC_CASES'}
write(authority,document,exclusive=True)
checked=FirstModelRecoveryRunner(SimpleNamespace(harness=None),experiment_id=EXPERIMENT,
 evidence=BASE/'authority-check-only',recovery_path=authority,recovery_sha256=sha(authority),
 api_key=None,root=ROOT)
checked._require_public_development_frozen()
assert checked.recovery['eligible_task_ids']==sorted(eligible)
with connect()as conn:
 budget=budget_snapshot(conn)
 for task in tasks:
  assert conn.execute("SELECT count(*)AS n FROM app.harness_tasks WHERE evaluation_profile->>'task_id'=%s "
                      "AND evaluation_profile->>'split'='holdout'",(task['task_id'],)).fetchone()['n']==0
assert budget['unknown_calls']==budget['budget']['reserved_micros']==0
assert budget['budget']['used_calls']==783 and budget['budget']['settled_micros']==3988681
names=['driver.py','support.py','common.py','run_with_existing_settings.py']
for name in names:ast.parse((BASE/name).read_text())
inputs=[ref(authority),ref(quality),ref(BASE/'public-dev-frozen.json'),ref(primary_path),
 ref(BASE/'subset/report.json'),ref(BASE/'full-dev/report.json'),ref(BASE/'holdout/report.json'),
 *primary['case_records']]
plan={'schema':'h12-first-model-recovery-dispatch-plan-v1','recorded_at':now(),
 'experiment_id':EXPERIMENT,'quality_status':'DIAGNOSTIC_ONLY','workers':2,
 'source_commit':document['source_commit'],'source_sha256':source,'budget_before':budget,
 'recovery_authority':ref(authority),'quality_freeze':ref(quality),
 'controller_files':[ref(BASE/name)for name in names],'immutable_inputs':inputs,'tasks':tasks,
 'primary_model_cases_never_repeated':primary['model_entered_count'],
 'original_scores_unchanged':True,'mvp':'NOT_RELEASED'}
write(BASE/'dispatch-plan.json',plan,exclusive=True)
public={k:v for k,v in plan.items()if k!='tasks'}
public['task_count']=len(tasks);public['task_order']=[t['task_id']for t in tasks]
write(ROOT/'docs/harness-v4/accept/H12-FIRST-MODEL-RECOVERY-20261115/readiness.json',public,exclusive=True)
print(json.dumps({'sealed':True,'eligible_cases':len(tasks),'source_commit':plan['source_commit'],
 'source_sha256':source,'workers':2,'budget':budget},ensure_ascii=False))
