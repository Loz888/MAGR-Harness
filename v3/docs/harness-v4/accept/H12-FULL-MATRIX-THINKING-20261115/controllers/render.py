"""Render all declared questions and measured outcomes after matrix closure."""
import json

from common import BASE, ROOT

report = json.loads((BASE/'final-report.json').read_bytes())
cases = [json.loads((ROOT/r['path']).read_bytes()) for r in report['case_records']]
path = ROOT/'docs/harness-v4/H12_FULL_MATRIX_THINKING_20261115.md'
existing = path.read_text()
assert '## 完整真实结果' not in existing
existing = existing.replace('状态：准备中，真实模型尚未派发。',
                            '以下为派发前计划，最终执行状态见“完整真实结果”。')


def money(n):
    return f'US${n/1_000_000:.6f}'


def local(p,label):
    return f'[{label}]({p})'


def cell(value):
    return str(value).replace('|','\\|').replace('\n',' ')


def command_summary(value):
    if isinstance(value,list):
        return [command_summary(v) for v in value]
    if isinstance(value,dict):
        keys={'argv','cwd','purpose','status','state','exit_code','tests_run','tests_failed',
              'tests_passed','duration_seconds','elapsed_seconds','log_sha256','log_hash',
              'tree_hash','receipt_id','complete','framework','code','failed_tests','passed_tests'}
        return {k:v for k,v in value.items() if k in keys}
    return value


dev, held = report['summaries']['dev'], report['summaries']['holdout']
lines = ['', '## 完整真实结果', '',
    '整体质量结论：**'+('达到冻结数量及否决条件门槛，待独立复验' if report['acceptance_pass']
                          else 'FAIL，编码MVP未放行')+'**。', '',
    f"本轮进入模型请求的题目：{report['model_entered_count']}/24；每题状态分别列明，"
    'NOT_RUN不算通过。开发/持出的分母各固定为12，不能仅按执行题数计算验收。', '',
    '| 集合 | PASS | FAIL | NOT_RUN | UNKNOWN | 固定分母通过率 | 门槛 |',
    '| --- | ---: | ---: | ---: | ---: | ---: | --- |']
for split,label,threshold in [('dev','开发',10),('holdout','独立持出',9)]:
    s=report['summaries'][split]
    lines.append(f"| {label} | {s['pass_count']} | {s['fail_count']} | {s['not_run_count']} | "
        f"{s['unknown_count']} | {s['pass_count']}/12（{s['pass_count']/12:.1%}） | ≥{threshold}/12 |")
before, after = report['budget_before']['budget'], report['budget_after']['budget']
lines += ['', f"新增模型调用{report['new_model_calls']}次，保守结算"
    f"{money(report['new_settled_cost_micros'])}；累计{after['used_calls']}次，"
    f"{money(after['settled_micros'])}/US$12。未核实用量"
    f"{report['budget_after']['unknown_calls']}次、当前预占{money(after['reserved_micros'])}。",
    '', f"精确源码提交：`{report['source_commit']}`；源码指纹：`{report['source_sha256']}`。",
    '', '本轮保留原题目标、Judge和资源；Click style仅做已披露的一文件范围修订。'
    '因此不能把与旧候选的全部成绩差异单独归因于思考模式。全部样本执行期间未按'
    '失败调参或修改源码，没有搬用旧PASS或采样择优。']
lines += ['', '共享PG增量与逐题调用/费用合计核对：'
          +('一致。' if report['ledger_matches_case_totals'] else '**不一致，须进一步核账。**'),
          '', '供应商实际请求策略核对：`'
          +json.dumps(report['observed_provider_policy'],ensure_ascii=False,sort_keys=True)+'`。']
if report.get('dispatch_resumption'):
    lines += ['', '原第二题后的外层审计断言和续跑说明均保留。已计分的暂停FAIL仅由原'
              'Engine关闭，树、调用与费用不变，未重采样。关闭回执及原停止记录见总机器记录。']
if report.get('parallel_execution'):
    lines += ['', '用户在第7题期间要求并发提速，因此前7题串行、剩余17题采用最多2题并发。'
              '并发零模型演练与实际启动路径检查通过；首次包装器导入失败未产生模型调用并已归档。'
              '原第7题正常结束后才切换，使用同一Engine/PG连接/互斥锁与账本，分题隔离本地目录、'
              '适配器和沙箱。按原顺序准入，完成顺序可不同，全部开发题核账后才准入持出。'
              '这也是本轮混合调度条件的披露，不能把全部成绩差异仅归因于思考模式。']
    lines += ['', '实际并发记录（耗时之和/批次墙钟只代表重叠度，不能当作严格对照提速比）：`'
              +json.dumps(report['parallel_performance'],ensure_ascii=False)+'`。']
if report['hard_stop']:
    lines += ['', '**硬停止：** '+cell(report['hard_stop'])]
lines += ['', '### 24题总表', '',
    '| 题号 | 仓库 / 类型 | 状态 | 模型调用 | 输入 / 输出Token | 独立Judge | 保守费用 | 原因类别 |',
    '| --- | --- | --- | ---: | ---: | --- | ---: | --- |']
for c in cases:
    s=c['score']; t=c['task']
    tokens=(str(s.get('input_tokens',0))+' / '+str(s.get('output_tokens',0))
            if s.get('usage_known') else '未知')
    lines.append('| '+ ' | '.join(cell(v) for v in [c['case'],t['repository']+' / '+t['type'],
        s['status'],s.get('model_calls',0),tokens,s.get('judge_status','NOT_RUN'),
        money(s.get('settled_cost_micros',s.get('cost_micros',0))),c['cause']['category']])+' |')
lines += ['', '### 逐题问题、通过条件与实际证据', '',
    '每题“通过条件”包括公开目标、原声明验收和本报告前述整题条件。独立判定以原'
    'Judge的冻结哈希与实际执行回执为准，不以模型新增测试替代。私有思考只保留原'
    'Engine/CAS引用，下面的导出不包含思考文本。']
for c in cases:
    s=c['score']; t=c['task']; e=c['evidence']
    lines += ['', f"#### {c['case']}：{t['task_id']} — {s['status']}", '',
        f"仓库 `{t['repository']}`，固定提交 `{t['base_commit']}`，类型 `{t['type']}`。", '',
        '**测试的问题 / 目标：** '+t['goal'], '', '**逐题通过条件：**', '']
    required=(c.get('declared_acceptance')or{}).get('required',[])
    if isinstance(required,str):required=[required]
    for condition in required:lines.append('- '+str(condition))
    rule=c.get('judge_specification',{}).get('pass_rule')
    if rule:lines.append('- 冻结判定规则：'+str(rule))
    lines.append('- 合格终态、独立判定、同一最终树、公开回归与类型条件全部满足。')
    lines += ['', f"可写范围：`{', '.join(t['scope'])}`。资源上限："
        f"`{json.dumps(t['resources'],ensure_ascii=False)}`；输出上限8192，包含思考。", '',
        '**实际结果与错误原因：** '+c['cause']['category'], '',
        f"停止码 `{s.get('reason','')}`；候选终态 `{s.get('candidate_status','未启动')}`；"
        f"运行时 `{s.get('raw_runtime_status','not_run')}`；独立Judge `{s.get('judge_status','NOT_RUN')}`。",
        '', f"最终树 `{s.get('final_tree','无')}`；Judge树 `{s.get('judge_tree','无')}`。"]
    if c['cause'].get('runtime_summary'):lines += ['',str(c['cause']['runtime_summary'])]
    if not s.get('regression_evidence_known',False):
        lines += ['', '公开回归最终证据未核实/未执行，不能将缺失记作新增失败0。']
    else:
        lines += ['',f"公开回归新增失败：{s.get('new_regression_failures')}，有实测证据。"]
        baseline=s.get('regression_baseline_failures')
        final=s.get('regression_final_failures')
        introduced=s.get('regression_introduced_failures')
        lines += ['', '公开回归原始失败清单（最终仍失败与新增失败分别记录）：', '',
            '````json',json.dumps({'baseline_failed':baseline,'final_failed':final,
                'introduced_failed':introduced,
                'baseline_failed_count':len(baseline)if isinstance(baseline,list)else None,
                'final_failed_count':len(final)if isinstance(final,list)else None},
                ensure_ascii=False,indent=2),'````']
    lines += ['', '**实际命令 / 测试：**', '', '````json',
        json.dumps({'declared_public_commands':t.get('public_commands'),
                    'executed_public_command':s.get('executed_public_command'),
                    'runtime_commands':command_summary(c.get('runtime_commands')),
                    'runtime_tests':command_summary(c.get('runtime_tests'))},
                    ensure_ascii=False,indent=2,default=str),
        '````']
    for log in c['executed_error_logs']:
        if log['error_excerpt']:
            label=log['phase']+' 原始错误日志'
            if log['phase']=='baseline-private':
                label+='（基线的预期失败不等于最终失败）'
            lines += ['', local(ROOT/log['log']['path'],label),
                      '', '````text', log['error_excerpt'], '````']
    links=[]
    for key,label in [('score','原始计分'),('runtime','原始运行结果'),
                      ('public_model_transcript','逐次模型输入/答复与用量'),
                      ('public_runtime','工具/验证完整记录'),('patch','最终补丁')]:
        if e.get(key):links.append(local(ROOT/e[key]['path'],label))
    lines += ['', ' · '.join(links), '',
        local(BASE/'export'/c['case']/'case.json','该题完整机器记录（含哈希与PG核账）')]
lines += ['', '### 记录完整性及后续使用', '',
    '所有状态、调用、费用和树均以原PG及不可变回执核对。供应商原始身份/用量元数据、'
    '命令日志、源码差异和逐次模型记录在各题机器记录链接中；它们不是只保留结论的摘要。', '',
    '本轮独立持出已经随完整诊断报告封存；若根据上述错误继续改进，下一轮需要新独立'
    '持出，不能把本批重跑成绩当作未见验收。原历史分数和失败均保留，MVP未放行，H13未启动。', '',
    local(ROOT/'docs/harness-v4/accept/H12-FULL-MATRIX-THINKING-20261115/results.json',
          '本轮总机器记录与PG账本')]
path.write_text(existing+'\n'.join(lines)+'\n')
print(json.dumps({'report':str(path),'case_count':len(cases),'model_entered':report['model_entered_count']}))
