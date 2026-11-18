import json
from collections import Counter
from common import BASE,EXPERIMENT,budget_snapshot,connect

phases=[]
for name in ('subset','full-dev','holdout'):
 values=[json.loads(p.read_bytes())for p in (BASE/name).glob('*/result.json')]
 phases.append({'phase':name,'finished':len(values),'statuses':dict(Counter(v.get('status')for v in values))})
with connect()as conn:
 budget=budget_snapshot(conn)
 active=conn.execute("SELECT t.evaluation_profile->>'task_id'AS task_id,t.public_status,"
  "(SELECT count(*)FROM app.usage_calls u WHERE u.run_id=t.run_id)AS calls "
  "FROM app.harness_tasks t WHERE t.experiment_id=%s AND t.public_status='running'",(EXPERIMENT,)).fetchall()
result={'preparation':phases,'quality_frozen':(BASE/'quality-frozen.json').exists(),
 'budget':budget,'active':active,'complete':(BASE/'recovery-complete.json').exists()}
if(BASE/'progress.json').exists():
 p=json.loads((BASE/'progress.json').read_bytes())
 result['scores']={k:v for k,v in p.items()if k not in {'records','audits'}}
if(BASE/'recovery-stop.json').exists():result['stop']=json.loads((BASE/'recovery-stop.json').read_bytes())
print(json.dumps(result,ensure_ascii=False))
