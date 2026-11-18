import json
import runpy
import shutil
import subprocess
from collections import Counter
from datetime import UTC, datetime
from hashlib import sha256
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
REPO = ROOT.parent
DESKTOP = Path(os.environ["H11_REVIEW_WORKSPACE"]).resolve()
E = Path((DESKTOP / 'work/h11_review_evidence_path.txt').read_text().strip())
DOC = ROOT / 'docs/harness-v4'
EXPECTED = 'a0cc8fac7467f7a99866df01c8ebea9f611f9844c0001c2d4130190fd9676cf2'
fingerprint = runpy.run_path(str(ROOT / 'scripts/verify.py'))['source_fingerprint']
assert fingerprint(ROOT) == EXPECTED
head = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=REPO, text=True).strip()
assert head == 'ccf5b8d330b28af6a31b6d642af4e3635d60ec93'
untouched = DOC / 'accept/HV4-H10/independent-recheck-20261111T104754Z.json'
assert sha256(untouched.read_bytes()).hexdigest() == '308152b41dcc75b1063aaee4709b290d303633f027418c9ba03c7dd35da2af5a'
assert subprocess.check_output(['git', 'diff', '--name-only'], cwd=REPO, text=True).splitlines() == ['v3/evidence/HV4-H11/acceptance.json']
gates = json.loads((E / 'run-results.json').read_text())
probes = json.loads((E / 'independent-probes/results.json').read_text())
services = json.loads((E / 'service-process-probes-v2/results.json').read_text())
assert gates['checks_passed'] and probes['failed'] == services['failed'] == 0
assert probes['passed'] == 12 and services['passed'] == 4
original_attempt = json.loads((E / 'service-process-probes/results.json').read_text())
assert original_attempt['failed'] == 4
assert probes['fixture_cleaned'] and services['fixture_cleaned'] and original_attempt['fixture_cleaned']

def audit_tree(root):
    manifests = list(root.rglob('sha256-manifest.json'))
    total = 0
    for manifest in manifests:
        data = json.loads(manifest.read_text())
        for item in data['files']:
            target = (manifest.parent / item['file']).resolve()
            assert manifest.parent.resolve() in target.parents
            content = target.read_bytes()
            assert sha256(content).hexdigest() == item['sha256'], target.name
            assert len(content) == item['bytes'], target.name
            total += 1
    processes = [json.loads(p.read_text()) for p in root.rglob('process.json')]
    events = []
    for path in root.rglob('postgres-events.json'):
        events.extend(json.loads(path.read_text())['events'])
    return {'manifests': len(manifests), 'verified_members': total,
        'fault_markers': len(list(root.rglob('fault.json'))),
        'process_exit_codes': dict(Counter(str(x['returncode']) for x in processes)),
        'postgres_event_kinds': dict(Counter(x.get('event', x.get('kind', '')) for x in events))}

integrity = {name: audit_tree(E / name) for name in [
    'h11-registered-faults', 'h00-h11-faults', 'independent-probes',
    'service-process-probes', 'service-process-probes-v2']}
for key in ['h11-registered-faults', 'h00-h11-faults']:
    assert integrity[key]['manifests'] == 23
    assert integrity[key]['verified_members'] == 159
    assert integrity[key]['process_exit_codes'] == {'0': 12, '-9': 10}
(E / 'evidence-integrity.json').write_text(json.dumps(integrity, indent=2))
shutil.copy2(DESKTOP / 'work/h11_service_process_probes.py', E / 'service-process-probes-v2/probe.py')
shutil.copy2(DESKTOP / 'work/h11_independent_runner.py', E / 'review-runner.py')
shutil.copy2(Path(__file__), E / 'finalize-review.py')

stamp = datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')
name = 'independent-recheck-' + stamp + '.json'
rel = 'accept/HV4-H11/' + name
record = {
    'card': 'HV4-H11', 'status': 'PASS', 'review_status': 'PASS_AFTER_INDEPENDENT_RECHECK',
    'recorded_at': datetime.now(UTC).isoformat(), 'reviewer': 'Codex independent review, frozen source',
    'source_commit': 'd3f3457b3d9dfab68bc73a40add299e4e633fbeb', 'head_at_review': head,
    'source_sha256': EXPECTED, 'finished_source_sha256': fingerprint(ROOT),
    'source_fingerprint_method': 'scripts/verify.py::source_fingerprint',
    'scope': 'H11 recovery engineering and security boundaries on the listed source plus recorded regressions. No product-wide or live-model acceptance.',
    'engineering_gate': 'PASS', 'security_gate': 'PASS_REVIEWED_H11_SCOPE', 'regression_gate': 'PASS',
    'counts': {c['name']: c.get('counts', {'exit_code': c['exit_code']}) for c in gates['commands']},
    'independent_probe_groups_passed': 16,
    'independent_boundary_probes': probes,
    'independent_full_service_sigkill_probes': services,
    'evidence_root': str(E.relative_to(ROOT)), 'commands': gates['commands'],
    'evidence_integrity': integrity,
    'observations': [
        'Public API passes the existing Engine into HarnessCodingService; HarnessStore/ledger/inbox/event writes reuse that Engine transaction and PG rows.',
        'Same Run/new epoch preserves deadline, grant, identity and limits; ten stale-worker write/dispatch/publication paths reject FENCED.',
        'Known model response replays under the new epoch without a provider send; six request-binding changes reject IDEMPOTENCY_CONFLICT.',
        'Real SIGKILL with lost model settlement stays blocked across two independent recovery processes; one provider send and original positive reservation remain.',
        'Partial edit completes once from CAS images; conflict, inventory drift and missing CAS reject without further mutation.',
        'Uncheckpointed instruction acknowledgements replay; selected watermark stops replay; terminal completion and inbox post share row-lock authority. Twelve concurrent races and both sequential winners were exercised.',
        'Four real public-service SIGKILL cuts cover known model settlement, partial physical patch, baseline log archival and final result. Each restores one Run with epoch 1->3; original failed baseline and final passing test survive, one edit operation, three unique model sends and four tool budget units including baseline.',
        'Cross-process cancellation stops and archives the original command identity before owned cleanup; repeated cancellation is terminal and creates no command.',
        'Both registered and aggregate matrices independently performed actual dedicated PG abrupt stop/start and original worker connection rejection; SHA manifests and cleanup receipts verified.',
    ],
    'new_blocking_findings': [],
    'preserved_review_attempts': [
        {'status': 'SETUP_FAILURE', 'exit_code': 1, 'description': 'First independent-probe launch failed in bash before Python because an unquoted inherited Windows PATH contained spaces. Retried using absolute uv path; no fixture or source mutation from failed launch.'},
        {'status': 'PROBE_ASSERTION_FAILURE', 'record': str((E / 'service-process-probes/results.json').relative_to(ROOT)),
         'failed_cases': 4, 'reason': 'Independent probe expected 3 tool budget units and omitted the baseline unit. All four recovery chains had completed with the expected failed baseline/final passing test before that assertion. Original code, traces, outputs and cleanup retained.',
         'corrected_rerun': str((E / 'service-process-probes-v2/results.json').relative_to(ROOT))},
    ],
    'historical_records_preserved': {
        'implementation_accept_original_fields': True,
        'h10_original_fail_and_scoped_pass': True,
        'untracked_h10_report': str(untouched.relative_to(ROOT)),
        'untracked_h10_report_sha256': sha256(untouched.read_bytes()).hexdigest(),
    },
    'other_reports': 'All pre-existing H09/H10 OPEN findings remain OPEN. H11 race checks add evidence and do not rewrite the separate reports or claim their dedicated closure.',
    'live_model_gate': 'NOT_RUN', 'live_agent_gate': 'NOT_RUN', 'external_publication_gate': 'NOT_RUN',
    'paid_model_calls': 0, 'paid_cost_micros': 0,
    'fixture_model_usage_note': 'Positive cost_micros in controlled-model receipts are synthetic ledger test amounts, not paid invoices.',
    'h12_started': False, 'pushes': 0, 'holdout_content_read': False,
    'product_source_modified': False,
}
(DOC / rel).write_text(json.dumps(record, ensure_ascii=False, indent=2) + '\n')
original_accept_path = DOC / 'accept/HV4-H11/accept.json'
original_accept = json.loads(original_accept_path.read_text())
(E / 'implementation-accept-before-independent.json').write_text(json.dumps(original_accept, ensure_ascii=False, indent=2) + '\n')
original_accept['independent_recheck_result'] = {
    'status': 'PASS', 'review_status': 'PASS_AFTER_INDEPENDENT_RECHECK', 'record': 'docs/harness-v4/' + rel,
    'source_commit': record['source_commit'], 'source_sha256': EXPECTED, 'scope': record['scope'],
    'note': 'All original implementation status/gate/attempt fields above are retained as historical values; this appended object is the current independent result.'}
original_accept_path.write_text(json.dumps(original_accept, ensure_ascii=False, indent=2) + '\n')

def replace(path, before, after):
    content = path.read_text()
    assert before in content, (path.name, before)
    path.write_text(content.replace(before, after, 1))

replace(DOC / 'README.md',
    'H11 已实施且工程门 PASS，停在 `PENDING_INDEPENDENT_RECHECK`，\nH12–H18 仍为 `PLANNED`；H11 尚无独立放行结论。',
    'H11 在 `d3f3457` 上已通过范围限定的独立复验。\nH12–H18 仍为 `PLANNED`；live 三门均未运行，其他报告的 OPEN 项保持原状态。')
replace(DOC / 'README.md',
    'H11 注册68项、H00–H11聚合349项、旧上下文15项与既有 Engine 恢复9项全通过，lint/typecheck通过。见 [本轮证据](accept/HV4-H11/accept.json) 和 [独立复验入口](CODEX_CLI_H11_REVIEW_TASK.md)。',
    'H11 独立复跑注册68项、H00–H11聚合349项、旧上下文15项与既有 Engine 恢复9项全通过，lint/typecheck通过；另有16组自建探针通过，包含完整服务真实 SIGKILL 恢复。首轮探针计数断言失败已保留，修正后四场景全通过。见 [独立复验报告](' + rel + ')；[实施证据](accept/HV4-H11/accept.json) 的原始字段保留。')
replace(DOC / 'README.md', '| H11 | Engine/PG 一致恢复工程门通过 | IMPLEMENTED，待独立复验 |',
    '| H11 | d3f3457 一致恢复独立复验 PASS | 仅 H11 工程范围；live 未跑，其他 OPEN 项保留 |')
replace(DOC / 'README.md', '| 崩溃后的工作恢复 | 有 checkpoint/epoch；完整工作区、子进程、未结算调用和事件的一致恢复尚未验收 | P1：H11 |',
    '| 崩溃后的工作恢复 | H11 已验证完整工作区、owned 子进程、账本和事件的一致恢复；未知模型结果保留预占并阻止重发 | d3f3457 独立工程复验 PASS；真实能力门见 H12 |')
replace(DOC / 'TASKS.md', '用户确认后 H11 为 IN_PROGRESS，H12–H18 仍为 PLANNED。',
    'H11 `d3f3457` 的恢复工程范围已独立复验 PASS，H12–H18 仍为 PLANNED。')
replace(DOC / 'TASKS.md', '> 当前：IMPLEMENTED / PENDING_INDEPENDENT_RECHECK。',
    '> 当前：ACCEPTED / PASS_AFTER_INDEPENDENT_RECHECK（仅 H11 恢复工程范围）。见 [独立报告](' + rel + ')；16组自建探针及列明回归通过。实施交付时的历史状态：IMPLEMENTED / PENDING_INDEPENDENT_RECHECK。')
replace(DOC / 'TASKS.md', '最终计数与门禁以 H11 新证据为准，尚不写独立 PASS。',
    '实施时尚无独立 PASS；现已完成上述独立工程复验。其他 OPEN 项未关闭，live 三门 NOT_RUN。')
replace(DOC / 'CODEX_CLI_H11_REVIEW_TASK.md', '状态：PENDING_INDEPENDENT_RECHECK。实现方工程门PASS不代表独立放行。',
    '当前：PASS_AFTER_INDEPENDENT_RECHECK（仅 H11 工程范围）。见 [独立复验报告](' + rel + ')。\n\n历史交付状态（保留）：PENDING_INDEPENDENT_RECHECK。实现方工程门PASS不代表独立放行。')
replace(DOC / 'CODEX_CLI_H11_TASK.md', '状态：PENDING_INDEPENDENT_RECHECK（工程门 PASS）。',
    '当前独立复验：PASS_AFTER_INDEPENDENT_RECHECK（仅 H11 工程范围），见 [独立报告](' + rel + ')。\n\n实施交付历史状态（保留）：PENDING_INDEPENDENT_RECHECK（工程门 PASS）。')
plan_path = DOC / 'plan.json'
plan = json.loads(plan_path.read_text())
h11 = next(x for x in plan['tasks'] if x['id'] == 'HV4-H11')
plan['history'].append({'recorded_at': record['recorded_at'], 'task_id': 'HV4-H11',
    'status': h11['status'], 'review_status': h11['review_status'], 'note': 'Implementation delivery state retained before independent recheck.'})
h11.update(status='ACCEPTED', review_status='PASS_AFTER_INDEPENDENT_RECHECK', independent_recheck=rel,
    acceptance_scope=record['scope'])
plan['scope'] = 'H10 c140dad F01/F02 scoped independent PASS retained; H11 d3f3457 recovery engineering independently PASS. Other OPEN findings remain OPEN; live gates NOT_RUN. H12-H18 PLANNED.'
plan['updated_at'] = record['recorded_at']
assert all(x['status'] == 'PLANNED' for x in plan['tasks'] if int(x['id'].split('-H')[1]) >= 12)
plan_path.write_text(json.dumps(plan, ensure_ascii=False, indent=2) + '\n')

assert fingerprint(ROOT) == EXPECTED
new_accept = json.loads(original_accept_path.read_text())
old_accept = json.loads((E / 'implementation-accept-before-independent.json').read_text())
assert all(new_accept[k] == v for k, v in old_accept.items())
assert sha256(untouched.read_bytes()).hexdigest() == record['historical_records_preserved']['untracked_h10_report_sha256']
outputs = DESKTOP / 'outputs'
outputs.mkdir(exist_ok=True)
shutil.copy2(DOC / rel, outputs / 'HV4-H11-INDEPENDENT-RECHECK.json')
(DESKTOP / 'work/h11_review_record_path.txt').write_text(str(DOC / rel))
print(json.dumps({'record': str(DOC / rel), 'integrity': integrity, 'independent_groups': 16,
    'source_sha256': fingerprint(ROOT), 'original_accept_fields_preserved': True}, ensure_ascii=False))
