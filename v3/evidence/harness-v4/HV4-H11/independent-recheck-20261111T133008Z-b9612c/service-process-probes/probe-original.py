"""Independent public-service SIGKILL probe with real PG, CAS, and Docker."""
import json
import os
import signal
import subprocess
import sys
import tempfile
import time
import traceback
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[5]
sys.path[:0] = [str(ROOT / 'src'), str(ROOT / 'tests')]
from h11_helpers import IsolatedPostgres, fixture_settings, private_json, make_traversable, wait_json, export_safe_files, _docker
from mgar_v3.coordination.engine import Engine
from mgar_v3.evidence.artifacts import ArtifactStore
from mgar_v3.harness.contracts import HarnessToolCall, ModelResponse, ModelUsage, sha256_text
from mgar_v3.harness.service import HarnessCodingService, CodingTaskRequest

BUGGY = 'def add(a, b):\n    return a - b\n'
FIXED = 'def add(a, b):\n    return a + b\n'
TEST = 'import unittest\nfrom calc import add\nclass Check(unittest.TestCase):\n    def test_add(self):\n        self.assertEqual(add(2, 3), 5)\n'
ARGV = ('python3', '-B', '-m', 'unittest', '-q')

def worker(config_path):
    config = json.loads(config_path.read_text())
    shared = Path(config['shared'])
    settings = fixture_settings(config['database_url'], shared)
    engine = Engine(settings, ArtifactStore(settings.artifact_root))
    task_id = config.get('task_id', '')
    def fault(point, payload=None):
        if point != config.get('fault'): return
        row = engine.harness.tasks()[0]
        private_json(config_path.parent / 'marker.json', {
            'task_id': row['task_id'], 'run_id': str(row['run_id']), 'epoch': row['epoch'],
            'tree_hash': row['tree_hash'], 'checkpoint_hash': row['checkpoint_hash'], 'point': point,
        })
        while True: time.sleep(.02)
    class Model:
        def complete(self, request, cancellation):
            index = int(request.operation_id.rsplit(':', 1)[1])
            with (shared / 'provider.jsonl').open('a') as log:
                log.write(json.dumps({'operation_id': request.operation_id,
                    'epoch': request.task.execution_epoch, 'tree': request.workspace_tree,
                    'message_count': len(request.messages)}) + '\n')
                log.flush(); os.fsync(log.fileno())
            if index == 1:
                calls = (
                    HarnessToolCall(call_id='patch', name='apply_edits', arguments={
                        'expected_tree': request.workspace_tree,
                        'edits': [{'path': 'calc.py', 'expected_sha256': sha256_text(BUGGY), 'content': FIXED}]}),
                    HarnessToolCall(call_id='read-after-patch', name='read_file', arguments={'path': 'calc.py'}),
                )
            elif index == 2:
                calls = (HarnessToolCall(call_id='test-after-patch', name='run_tests', arguments={'argv': list(ARGV)}),)
            else: calls = ()
            return ModelResponse(text='calc.py now adds correctly; the original unittest passes.',
                finish_reason='tool_calls' if calls else 'stop', tool_calls=calls,
                usage=ModelUsage(cost_micros=5))
    service = HarnessCodingService(settings.harness_root, engine=engine, actor='owner',
        image_lock=settings.harness_image_lock, model_factory=lambda request: Model(), fault_hook=fault)
    try:
        if config['mode'] == 'recover':
            engine.recover()
            result = service.recover_task(task_id)
        else:
            source = shared / 'source'; source.mkdir(parents=True)
            (source / 'calc.py').write_text(BUGGY); (source / 'test_calc.py').write_text(TEST)
            project = service.register_project(str(source))
            runtime = service._prepare(CodingTaskRequest(project_id=project['project_id'],
                objective='Fix add in calc.py and pass the original unittest', task_kind='bugfix',
                approved=True, target_path='calc.py', old_text='a - b', new_text='a + b',
                test_command=' '.join(ARGV)))
            make_traversable(runtime.workspace.root)
            result = service._run(runtime)
        row = engine.harness.get(result['task_id'])
        operations = engine.harness.operations(result['task_id'])
        audits = {key: row[key] for key in ['task_id', 'epoch', 'tree_hash', 'checkpoint_hash', 'checkpoint_revision', 'result_hash', 'limits']}
        audits.update(run_id=str(row['run_id']), status=row['status'], deadline_at=row['deadline_at'].isoformat(),
            operations=[{'id': x['operation_id'], 'kind': x['kind'], 'state': x['state'],
                'result_hash': x['result_hash'], 'receipt': x['receipt']} for x in operations])
        private_json(config_path.parent / 'audit.json', audits)
        private_json(config_path.parent / 'result.json', result)
    except BaseException:
        (config_path.parent / 'private-error.txt').write_text(traceback.format_exc())
        raise
    finally: engine.close()

if len(sys.argv) > 1:
    worker(Path(sys.argv[1])); sys.exit(0)

DESKTOP = Path(os.environ["H11_REVIEW_WORKSPACE"]).resolve()
OUT = Path((DESKTOP / 'work/h11_review_evidence_path.txt').read_text().strip()) / 'service-process-probes'
OUT.mkdir(mode=0o700)
os.environ['H11_EVIDENCE_DIR'] = str(OUT / 'fault-exports')
base = Path(tempfile.mkdtemp(prefix='magr-h11-independent-service-', dir='/tmp'))
make_traversable(base)
pg = IsolatedPostgres()
records = []

def launch(directory, payload):
    directory.mkdir(parents=True)
    private_json(directory / 'config.json', payload)
    log = (directory / 'private-process.log').open('wb')
    process = subprocess.Popen([sys.executable, '-B', str(Path(__file__).resolve()), str(directory / 'config.json')],
        cwd=ROOT, stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
    log.close()
    return process

try:
    pg.start()
    for point in ['model_after_settlement', 'edit.after_file_replace:calc.py', 'command.before_log_cas', 'result_before_commit']:
        name = point.replace('.', '-').replace(':', '-')
        directory = base / name
        payload = {'database_url': pg.create_database(), 'shared': str(directory / 'shared'), 'mode': 'start', 'fault': point}
        first = resumed = marker = None
        record = {'name': name}
        try:
            first = launch(directory / 'first', payload)
            marker = wait_json(directory / 'first/marker.json', timeout=90)
            os.killpg(first.pid, signal.SIGKILL)
            assert first.wait(timeout=15) == -9
            resumed = launch(directory / 'resumed', {**payload, 'mode': 'recover', 'fault': '', 'task_id': marker['task_id']})
            assert resumed.wait(timeout=120) == 0, 'Recovery child failed; private log retained'
            result = json.loads((directory / 'resumed/result.json').read_text())
            audit = json.loads((directory / 'resumed/audit.json').read_text())
            sends = [json.loads(x) for x in (directory / 'shared/provider.jsonl').read_text().splitlines()]
            assert result['status'] == 'completed', result.get('reason')
            assert result['run_id'] == marker['run_id'] == audit['run_id']
            assert result['execution_epoch'] > marker['epoch']
            assert result['tree_after'] == audit['tree_hash']
            assert len(sends) == 3 and all(v == 1 for v in Counter(x['operation_id'] for x in sends).values())
            assert result['model_calls'] == 3 and result['cost_micros'] == 15
            assert result['receipts'][0]['status'] == 'failed'
            assert result['receipts'][-1]['status'] == 'passed'
            assert all(x['state'] == 'known' for x in audit['operations'])
            assert len([x for x in audit['operations'] if x['kind'] == 'edit']) == 1
            assert result['tool_calls'] == 3
            remaining = _docker('ps', '--all', '--quiet', '--filter', 'label=mgar.v4.h11.task=' + marker['task_id']).stdout.strip()
            assert remaining == ''
            record.update(status='PASS', run_id=marker['run_id'], original_epoch=marker['epoch'],
                resumed_epoch=result['execution_epoch'], source_provider_calls=len(sends), model_cost_micros=result['cost_micros'],
                tool_calls=result['tool_calls'], original_baseline='failed', final_test='passed', owned_containers_remaining=0)
        except BaseException as exc:
            record.update(status='FAIL', exception=type(exc).__name__, message=str(exc))
            (OUT / (name + '.private-traceback.txt')).write_text(traceback.format_exc())
        finally:
            for process in [first, resumed]:
                if process is not None and process.poll() is None:
                    os.killpg(process.pid, signal.SIGKILL); process.wait(timeout=15)
            if marker:
                remaining = _docker('ps', '--all', '--quiet', '--filter', 'label=mgar.v4.h11.task=' + marker['task_id']).stdout.strip()
                if remaining:
                    settings = fixture_settings(payload['database_url'], Path(payload['shared']))
                    cleanup_engine = Engine(settings, ArtifactStore(settings.artifact_root))
                    try:
                        cleanup_engine.recover()
                        cleanup = HarnessCodingService(settings.harness_root, engine=cleanup_engine,
                            actor='owner', image_lock=settings.harness_image_lock)
                        receipt = cleanup.cancel_task(marker['task_id'], 'Independent probe cleanup')
                        record['cleanup_errors'] = receipt['reconciliation_errors']
                    finally: cleanup_engine.close()
                    assert _docker('ps', '--all', '--quiet', '--filter', 'label=mgar.v4.h11.task=' + marker['task_id']).stdout.strip() == ''
            # Whitelist only public results/markers/provider requests, never private config/DSN.
            exports = {'provider.jsonl': directory / 'shared/provider.jsonl'}
            for stage in ['first', 'resumed']:
                for filename in ['marker.json', 'audit.json', 'result.json']:
                    exports[stage + '-' + filename] = directory / stage / filename
            export_safe_files(OUT / name, exports)
        records.append(record)
        private_json(OUT / 'results.json', {'checks': records, 'paid_calls': 0})
        print(json.dumps(record), flush=True)
finally: pg.close()
private_json(OUT / 'results.json', {'checks': records, 'passed': sum(x['status'] == 'PASS' for x in records),
    'failed': sum(x['status'] == 'FAIL' for x in records), 'paid_calls': 0, 'fixture_cleaned': not pg.created})
sys.exit(1 if any(x['status'] == 'FAIL' for x in records) else 0)
