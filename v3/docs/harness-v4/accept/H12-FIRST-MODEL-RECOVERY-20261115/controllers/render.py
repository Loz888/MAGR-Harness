"""Append separately versioned recovery evidence to the complete question report."""
import json
from common import BASE,ROOT

r=json.loads((BASE/'final-report.json').read_bytes())
path=ROOT/'docs/harness-v4/H12_FULL_MATRIX_THINKING_20261115.md'
assert '## 三道未入模题的单独首样本补测'not in path.read_text()
money=lambda n:f'US${n/1_000_000:.6f}'
lines=['','## 三道未入模题的单独首样本补测','',
 '本节使用另一个已冻结源码候选，处理原07/08/09的前置检查阻塞。只允许原来零调用、'
 '零Run、零费用的题进入首个实际模型样本；其余21题未重采样。主轮6/12开发、6/12持出'
 '及三个NOT_RUN保持原样；补测是已见基准诊断，不构成独立质量放行。','',
 f"新源码`{r['source_commit']}`，指纹`{r['source_sha256']}`；工程246项、Ruff、mypy通过后，"
 '重新执行全部24题公开准备并另冻结。修复声明的Go新API红基线识别、重构原行为基线、'
 '公开required传递与独立全回归指标，原最终Judge/题目/资源保持。','',
 f"补测：{r['counts']['pass']} PASS、{r['counts']['fail']} FAIL、{r['counts']['not_run']} NOT_RUN、"
 f"{r['counts']['unknown']} UNKNOWN；主轮加补测共{r['total_questions_with_actual_model']}/24题已进入模型。",
 '',f"本节新增{r['new_model_calls']}次，保守{money(r['new_settled_cost_micros'])}；累计"
 f"{r['budget_after']['budget']['used_calls']}次、{money(r['budget_after']['budget']['settled_micros'])}/US$12，"
 f"未知{r['budget_after']['unknown_calls']}、预占{money(r['budget_after']['budget']['reserved_micros'])}。",
 '',f"{r['request_policy']['verified_receipts']}份供应商记录均enabled/max；峰值"
 f"{r['request_policy']['peak_physical_requests']}个物理请求并发。",
 '', '| 题目 | 补测状态 | 原因 | 模型调用 | 独立Judge | 保守费用 |',
 '| --- | --- | --- | ---: | --- | ---: |']
for c in r['cases']:
 score=c.get('score',{})
 lines.append('| '+' | '.join(str(x).replace('|','\\|')for x in
  [c['task']['task_id'],c['status'],c['reason'],c['model_calls'],score.get('judge_status'),
   money(c.get('cost_micros',0))])+' |')
for c in r['cases']:
 lines+=['', '### 补测：'+c['task']['task_id'], '', '**问题：** '+c['task']['goal'], '',
  '**公开通过条件：** '+json.dumps(c.get('public_acceptance',[]),ensure_ascii=False), '',
  '**整题条件：** '+ '、'.join(c.get('whole_task_conditions',[])), '',
  '**实际结果：** '+c['status']+' / '+c['reason']]
 score=c.get('score',{})
 lines+=['', f"最终树`{score.get('final_tree')}`；Judge树`{score.get('judge_tree')}`；"
  f"独立全回归证据`{score.get('regression_evidence_scope')}`，新增失败`{score.get('new_regression_failures')}`。"]
 for log in c.get('executed_logs',[]):
  lines+=['', f"- {log['phase']}：`{json.dumps(log['argv'],ensure_ascii=False)}`，"
   f"exit={log['exit_code']}；{json.dumps(log['parsed'],ensure_ascii=False)}。"
   f"[完整日志]({ROOT/log['log']['path']})"]
  if log.get('tail'):
   lines+=['','````text',log['tail'].rstrip(),'````']
 for key,label in [('score','补测原计分'),('model_calls','逐调用输入/答复/用量'),
                   ('runtime','完整运行记录'),('patch','最终补丁')]:
  if c.get('evidence',{}).get(key):lines+=['',f"[{label}]({ROOT/c['evidence'][key]['path']})"]
lines+=['','本节的源码、协议与主轮不同，不能把补测PASS拼回主轮计为单候选24题成绩。'
 '本批已经见过，正式质量门须使用新的独立持出；MVP仍未放行，H13未启动。','',
 '[补测机器记录]('+str(ROOT/'docs/harness-v4/accept/H12-FIRST-MODEL-RECOVERY-20261115/results.json')+')']
path.write_text(path.read_text()+'\n'.join(x.rstrip()for x in '\n'.join(lines).splitlines())+'\n')
print(json.dumps({'report':str(path),'actual_model_questions':r['total_questions_with_actual_model']}))
