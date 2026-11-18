"""Independent H11 review probes. No production/test source edits or paid calls."""
import json
import os
import runpy
import sys
import tempfile
import threading
import traceback
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[5]
sys.path[:0] = [str(ROOT / 'src'), str(ROOT / 'tests')]
from h11_helpers import IsolatedPostgres, Worker, fixture_settings, private_json, _docker
from mgar_v3.coordination.engine import Engine
from mgar_v3.domain import DomainError
from mgar_v3.evidence.artifacts import ArtifactStore
from mgar_v3.harness.contracts import (
    BudgetLimits, CancellationToken, CapabilityGrant, EventDraft, ModelMessage,
    ModelRequest, ModelResponse, ModelUsage, TaskBinding, VerificationRequirement,
    WorkspaceEdit, sha256_text,
)
from mgar_v3.harness.ledger import PersistentBudgetLedger
from mgar_v3.harness.persistence import DurableContinuation, DurableModel, EngineInbox
from mgar_v3.harness.recovery_io import RecoveryIO, workspace_lock
from mgar_v3.harness.service import HarnessCodingService, CodingTaskRequest
from mgar_v3.harness.workspace import WorkspaceRegistry

DESKTOP = Path(os.environ["H11_REVIEW_WORKSPACE"]).resolve()
OUT = Path((DESKTOP / 'work/h11_review_evidence_path.txt').read_text().strip()) / 'independent-probes'
OUT.mkdir(mode=0o700)
os.environ['H11_EVIDENCE_DIR'] = str(OUT / 'fault-exports')
EXPECTED = 'a0cc8fac7467f7a99866df01c8ebea9f611f9844c0001c2d4130190fd9676cf2'
fingerprint = runpy.run_path(str(ROOT / 'scripts/verify.py'))['source_fingerprint']
assert fingerprint(ROOT) == EXPECTED
TEMP = Path(tempfile.mkdtemp(prefix='magr-h11-independent-', dir='/tmp'))
results = []
pg = IsolatedPostgres()
engine = None

def check(name, fn):
    try:
        detail = fn() or {}
        result = {'name': name, 'status': 'PASS', 'detail': detail}
    except BaseException as exc:
        (OUT / (name + '.private-traceback.txt')).write_text(traceback.format_exc())
        result = {'name': name, 'status': 'FAIL', 'exception': type(exc).__name__}
    results.append(result)
    private_json(OUT / 'results.json', {'source_sha256': EXPECTED, 'checks': results})
    print(json.dumps(result), flush=True)

def denied(fn, codes=None):
    try:
        fn()
    except DomainError as exc:
        if codes is not None:
            assert exc.code in codes, exc.code
        return exc.code
    raise AssertionError('Expected fail-closed DomainError')

def task():
    task_id = 'h08-' + uuid4().hex[:12]
    directory = TEMP / task_id
    source = directory / 'source'
    source.mkdir(parents=True)
    for name, body in [('a.py', 'value = 1\n'), ('b.py', 'other = 1\n')]:
        (source / name).write_text(body)
    registry = WorkspaceRegistry(directory / 'registry')
    reg = registry.register_local(source, actor='owner')
    ws = registry.create_task_workspace(reg, task_id)
    grant = CapabilityGrant(actor='owner', workspace_id=reg.workspace_id,
        repository=reg.repository, read_paths=('',), write_paths=('',), execute=True,
        allowed_commands=('python3',), max_seconds=120)
    store = engine.harness
    store.create(task_id, reg, {'project_id': reg.workspace_id, 'workspace_root': str(ws.root),
        'objective': 'independent review', 'mode': 'deterministic'},
        grant.model_dump(mode='json'), BudgetLimits(max_model_calls=12,
        max_tool_calls=48, max_cost_micros=1000, max_seconds=600).model_dump(mode='json'))
    row = store.begin(task_id)
    store.record_tree(task_id, row['epoch'], ws.snapshot().tree_hash,
        manifest_hash=ws.manifest.manifest_hash)
    return task_id, store.get(task_id), ws, grant

def request(tid, row, ws):
    binding = TaskBinding(task_id=tid, run_id=row['run_id'], thread_id=row['thread_id'],
        execution_epoch=row['epoch'], actor='owner', repository=ws.manifest.repository,
        workspace_id=ws.manifest.workspace_id, base_commit='a' * 40,
        acceptance_revision='independent-review-v1', objective='independent review',
        task_kind='inspect', requirements=(VerificationRequirement(requirement_id='review'),))
    return ModelRequest(operation_id=tid + ':model:1', task=binding,
        messages=(ModelMessage(role='user', content='Read a.py'),), tools=(),
        model_id='free-local-review', price_version='review-v1', max_output_tokens=16,
        reservation_micros=20, context_revision='review-context-1',
        workspace_tree=ws.snapshot().tree_hash)

class Model:
    def __init__(self): self.calls = []
    def complete(self, req, cancellation):
        self.calls.append(req.operation_id)
        return ModelResponse(text='review response', usage=ModelUsage(cost_micros=3))

def io_for(tid, row, ws, fault=None):
    return RecoveryIO(engine.harness, tid, row['epoch'], ws, artifacts=engine.artifacts,
        run_id=str(row['run_id']), fault=fault)

def continuity():
    tid, before, ws, grant = task()
    store = engine.harness
    store.pause(tid)
    after = store.claim(tid)
    assert after['run_id'] == before['run_id'] and after['epoch'] > before['epoch']
    for key in ['deadline_at', 'limits', 'profile_hash', 'grant_scope', 'identity_hash']:
        assert after[key] == before[key], key
    return {'run_id': str(before['run_id']), 'old_epoch': before['epoch'],
        'new_epoch': after['epoch'], 'deadline_preserved': True, 'same_engine': store.engine is engine}

def stale_fences():
    tid, old, ws, grant = task()
    store = engine.harness
    old_io = io_for(tid, old, ws)
    old_ledger = PersistentBudgetLedger(store, tid, old['epoch'])
    old_inbox = EngineInbox(store, tid, epoch=old['epoch'])
    req = request(tid, old, ws)
    model = Model()
    store.pause(tid)
    new = store.claim(tid)
    current = EngineInbox(store, tid, epoch=new['epoch'])
    current.post(message_id='current', kind='instruction', text='keep this')
    before = ws.snapshot()
    actions = {
        'record_tree': lambda: store.record_tree(tid, old['epoch'], 'f' * 64),
        'checkpoint': lambda: store.save_checkpoint(tid, old['epoch'], engine.artifacts.put(b'old'), 1),
        'operation': lambda: store.start_operation(tid, old['epoch'], 'stale', 'tool', 'a' * 64),
        'budget': lambda: old_ledger.reserve(tid, 'stale-model', 'model', reserved_micros=20),
        'model_dispatch': lambda: DurableModel(model, store, tid, old['epoch'], lambda *x: None).complete(req, CancellationToken()),
        'inbox_ack': lambda: old_inbox.mark_applied('current', 1),
        'edit': lambda: old_io.edit([WorkspaceEdit(path='a.py', expected_sha256=sha256_text('value = 1\n'), content='value = 999\n')], expected_tree=before.tree_hash, operation_id='stale-edit'),
        'tool_dispatch': lambda: old_io.invoke('stale-tool', 'tool', {}, lambda: (_ for _ in ()).throw(AssertionError('called')), encode=lambda x: x, decode=lambda x: x),
        'completion': lambda: store.finish(tid, old['epoch'], 'completed', engine.artifacts.put(b'old'), before.tree_hash),
        'event': lambda: store.emit(tid, old['epoch'], EventDraft(run_id=old['run_id'], operation_id='stale-event', execution_epoch=old['epoch'], revision=1, kind='review', payload={})),
    }
    codes = {name: denied(fn, {'FENCED'}) for name, fn in actions.items()}
    assert ws.snapshot() == before and model.calls == []
    assert current.messages()[0]['status'] == 'received'
    assert store.get(tid)['epoch'] == new['epoch'] and store.get(tid)['result_hash'] is None
    return codes

def known_model_binding():
    tid, before, ws, grant = task()
    req = request(tid, before, ws)
    store = engine.harness
    ledger = PersistentBudgetLedger(store, tid, before['epoch'])
    ledger.reserve(tid, req.operation_id, 'model', reserved_micros=20)
    model = Model()
    adapter = DurableModel(model, store, tid, before['epoch'], lambda *x: None)
    first = adapter.complete(req, CancellationToken())
    store.pause(tid)
    after = store.claim(tid)
    resumed = req.model_copy(update={'task': req.task.model_copy(update={'execution_epoch': after['epoch']})})
    adapter = DurableModel(model, store, tid, after['epoch'], lambda *x: None)
    assert adapter.complete(resumed, CancellationToken()) == first
    changes = {'workspace_tree': 'b' * 64, 'context_revision': 'other',
        'model_id': 'other', 'price_version': 'other', 'max_output_tokens': 17,
        'messages': (ModelMessage(role='user', content='changed'),)}
    codes = {key: denied(lambda key=key, value=value: adapter.complete(resumed.model_copy(update={key: value}), CancellationToken()), {'IDEMPOTENCY_CONFLICT'}) for key, value in changes.items()}
    assert model.calls == [req.operation_id]
    snap = PersistentBudgetLedger(store, tid, after['epoch']).snapshot(tid)
    assert snap.cost_micros == 3 and snap.model_calls == 1
    return {'denied_changes': codes, 'provider_calls': len(model.calls), 'cost_micros': snap.cost_micros}

def inbox_watermark():
    tid, row, ws, grant = task()
    store = engine.harness
    inbox = EngineInbox(store, tid, epoch=row['epoch'])
    inbox.post(message_id='instruction', kind='instruction', text='new objective')
    inbox.mark_applied('instruction', 1)
    assert len(EngineInbox(store, tid, epoch=row['epoch']).pending_instructions()) == 1
    result_hash = engine.artifacts.put(b'result')
    assert denied(lambda: store.finish(tid, row['epoch'], 'completed', result_hash, ws.snapshot().tree_hash)) == 'HARNESS_INBOX_PENDING'
    cp = DurableContinuation(store, tid, row['epoch'], ws, lambda *x: None)
    cp.save({'expected_tree': ws.snapshot().tree_hash, 'state': {'instruction_revision': 1}})
    assert not EngineInbox(store, tid, epoch=row['epoch']).pending_instructions()
    store.finish(tid, row['epoch'], 'completed', result_hash, ws.snapshot().tree_hash)
    code = denied(lambda: inbox.post(message_id='late', kind='instruction', text='late'))
    return {'unselected_ack_replayed': True, 'selected_ack_not_replayed': True, 'late_post': code}

def concurrent_terminal_inbox():
    outcomes = {'post_won': 0, 'finish_won': 0}
    for _ in range(12):
        tid, row, ws, grant = task()
        store = engine.harness
        inbox = EngineInbox(store, tid)
        barrier = threading.Barrier(2)
        def post():
            barrier.wait()
            try:
                inbox.post(message_id='racing', kind='instruction', text='do additional work')
                return 'accepted'
            except DomainError as exc: return exc.code
        def finish():
            barrier.wait()
            try:
                store.finish(tid, row['epoch'], 'completed', engine.artifacts.put(b'finish'), ws.snapshot().tree_hash)
                return 'completed'
            except DomainError as exc: return exc.code
        with ThreadPoolExecutor(max_workers=2) as pool:
            a, b = pool.submit(post), pool.submit(finish)
            post_result, finish_result = a.result(10), b.result(10)
        state = store.get(tid)
        if post_result == 'accepted':
            assert finish_result == 'HARNESS_INBOX_PENDING' and state['status'] == 'running'
            outcomes['post_won'] += 1
        else:
            assert post_result == 'HARNESS_TASK_FINISHED' and finish_result == 'completed'
            assert state['status'] == 'succeeded' and inbox.messages() == []
            outcomes['finish_won'] += 1
    return outcomes

class Crash(BaseException): pass

def partial_patch(mode):
    tid, row, ws, grant = task()
    def crash(point):
        if point == 'edit.after_file_replace:a.py': raise Crash()
    io = io_for(tid, row, ws, crash)
    edits = [WorkspaceEdit(path='a.py', expected_sha256=sha256_text('value = 1\n'), content='value = 2\n'),
        WorkspaceEdit(path='b.py', expected_sha256=sha256_text('other = 1\n'), content='other = 2\n')]
    try: io.edit(edits, expected_tree=ws.snapshot().tree_hash, operation_id='partial-edit')
    except Crash: pass
    else: raise AssertionError('did not interrupt after first physical replacement')
    assert ws.read('a.py') == 'value = 2\n' and ws.read('b.py') == 'other = 1\n'
    store = engine.harness
    store.pause(tid)
    paused = store.get(tid)
    recovery = io_for(tid, paused, ws)
    op = store.operation(tid, 'partial-edit')
    saved_artifact = None
    if mode == 'third-image': (ws.root / 'b.py').write_text('manual conflict\n')
    if mode == 'unrelated-drift': (ws.root / 'extra.py').write_text('new file\n')
    if mode == 'cas-missing':
        h = op['intent']['images']['b.py']['after']
        artifact = engine.settings.artifact_root / h[:2] / h
        saved_artifact = (artifact, artifact.read_bytes())
        artifact.unlink()
    before_files = {p.name: p.read_bytes() for p in ws.root.glob('*.py')}
    try:
        with workspace_lock(ws.root):
            if mode == 'clean':
                snapshot = recovery.reconcile_edit_locked('partial-edit')
                assert ws.read('b.py') == 'other = 2\n'
                assert store.operation(tid, 'partial-edit')['state'] == 'known'
                # Repeat reconciliation is byte-stable and cannot apply a patch twice.
                first = {p.name: (p.stat().st_ino, p.stat().st_mtime_ns) for p in ws.root.glob('*.py')}
                recovery.reconcile_edit_locked('partial-edit')
                assert first == {p.name: (p.stat().st_ino, p.stat().st_mtime_ns) for p in ws.root.glob('*.py')}
                return {'tree_hash': snapshot.tree_hash, 'already_applied_inode_unchanged': True}
            try: recovery.reconcile_edit_locked('partial-edit')
            except (DomainError, FileNotFoundError, ValueError) as exc: code = getattr(exc, 'code', type(exc).__name__)
            else: raise AssertionError('unsafe recovery accepted')
            assert before_files == {p.name: p.read_bytes() for p in ws.root.glob('*.py')}
            assert store.get(tid)['status'] == 'paused'
            assert store.operation(tid, 'partial-edit')['state'] != 'known'
            return {'blocked': code, 'all_files_unchanged': True}
    finally:
        if saved_artifact:
            saved_artifact[0].write_bytes(saved_artifact[1])

def selected_cas():
    # A local result/status and an unselected valid CAS object cannot grant success.
    source = TEMP / ('projection-source-' + uuid4().hex[:6])
    source.mkdir(); (source / 'a.py').write_text('value = 1\n')
    service = HarnessCodingService(engine.settings.harness_root, engine=engine,
        actor='owner', image_lock=engine.settings.harness_image_lock)
    project = service.register_project(str(source))
    runtime = service._prepare(CodingTaskRequest(project_id=project['project_id'], objective='Inspect', task_kind='inspect', target_path='a.py'))
    local = service.runs / runtime.task_id / 'result.json'
    local.write_text(json.dumps({'status': 'completed', 'task_id': runtime.task_id, 'summary': 'unselected'}))
    engine.artifacts.put(local.read_bytes())
    assert service.get_task(runtime.task_id)['status'] == 'queued'
    assert engine.harness.get(runtime.task_id)['result_hash'] is None
    return {'unselected_projection_ignored': True}

def worker_unknown_repeat():
    directory = TEMP / 'real-kill-unknown'
    dsn = pg.create_database()
    payload = {'database_url': dsn, 'directory': str(directory / 'shared'),
        'mode': 'start', 'scenario': 'model', 'fault': 'model_before_settlement'}
    first = Worker.launch(directory / 'first', payload)
    try:
        marker = first.wait_marker(); first.kill()
        states = []
        for n in range(2):
            worker = Worker.launch(directory / ('recover-' + str(n)), {**payload,
                'mode': 'recover', 'task_id': marker['task_id'], 'fault': ''})
            try: result = worker.finish()
            finally: worker.close()
            assert result['error'] == 'RECOVERY_BLOCKED'
            states.append(result['error'])
        calls = (directory / 'shared/provider_calls.jsonl').read_text().splitlines()
        assert len(calls) == 1
        import psycopg
        from psycopg.rows import dict_row
        with psycopg.connect(dsn, autocommit=True, row_factory=dict_row) as conn:
            row = conn.execute('SELECT h.*,r.status,r.execution_epoch,t.mandate_id FROM app.harness_tasks h JOIN app.runs r USING(run_id) JOIN app.tickets t USING(ticket_id) WHERE task_id=%s', (marker['task_id'],)).fetchone()
            usage = conn.execute('SELECT status,reserved_micros,result FROM app.usage_calls WHERE run_id=%s', (row['run_id'],)).fetchall()
            money = conn.execute('SELECT reserved_micros FROM app.budgets WHERE mandate_id=%s', (row['mandate_id'],)).fetchone()
        assert len(usage) == 1 and usage[0]['status'] == 'unknown' and usage[0]['result'] is None
        assert money['reserved_micros'] == usage[0]['reserved_micros'] > 0
        assert row['result_hash'] is None and str(row['run_id']) == marker['run_id']
        return {'sigkill_exit': -9, 'repeated_recovery': states, 'provider_calls': 1,
            'reservation_retained': money['reserved_micros'], 'run_id': marker['run_id']}
    finally: first.close()

def worker_command_cancel():
    directory = TEMP / 'real-kill-command'
    dsn = pg.create_database()
    payload = {'database_url': dsn, 'directory': str(directory / 'shared'),
        'mode': 'start', 'scenario': 'command', 'fault': 'command.after_start'}
    first = Worker.launch(directory / 'first', payload)
    try:
        marker = first.wait_marker(); first.kill()
        before = _docker('ps', '--all', '--quiet', '--filter', 'label=mgar.v4.h11.task=' + marker['task_id']).stdout.strip()
        assert before
        worker = Worker.launch(directory / 'cancel', {**payload, 'mode': 'cancel', 'task_id': marker['task_id'], 'fault': ''})
        try: result = worker.finish()
        finally: worker.close()
        assert result['status'] == 'cancelled' and result['reconciliation_errors'] == []
        assert result['run_id'] == marker['run_id'] and result['epoch'] > marker['epoch']
        receipt = result['receipts'][0]
        assert receipt['launched']['identity']['execution_epoch'] == marker['epoch']
        raw_hash = receipt['raw_log_hash']
        raw = (directory / 'shared/artifacts' / raw_hash[:2] / raw_hash).read_bytes()
        assert sha256(raw).hexdigest() == raw_hash
        assert _docker('ps', '--all', '--quiet', '--filter', 'label=mgar.v4.h11.task=' + marker['task_id']).stdout.strip() == ''
        # Repeated cancellation remains terminal and never creates another command.
        again = Worker.launch(directory / 'cancel-again', {**payload, 'mode': 'cancel', 'task_id': marker['task_id'], 'fault': ''})
        try: repeated = again.finish()
        finally: again.close()
        assert repeated['status'] == 'cancelled' and len(repeated['operations']) == 1
        return {'sigkill_exit': -9, 'owned_orphan_removed': True,
            'original_epoch': marker['epoch'], 'cancel_epoch': result['epoch'], 'log_hash': raw_hash,
            'repeated_cancel_idempotent': True}
    finally: first.close()

try:
    pg.start()
    settings = fixture_settings(pg.create_database(), TEMP / 'engine')
    engine = Engine(settings, ArtifactStore(settings.artifact_root))
    for name, fn in [
        ('same-run-new-epoch-fixed-authority', continuity),
        ('stale-worker-ten-mutation-fences', stale_fences),
        ('known-model-replay-and-six-binding-denials', known_model_binding),
        ('inbox-ack-checkpoint-and-terminal', inbox_watermark),
        ('concurrent-inbox-terminal-twelve-races', concurrent_terminal_inbox),
        ('partial-edit-no-double-apply', lambda: partial_patch('clean')),
        ('partial-edit-third-image-blocked', lambda: partial_patch('third-image')),
        ('partial-edit-unrelated-drift-blocked', lambda: partial_patch('unrelated-drift')),
        ('partial-edit-missing-cas-blocked', lambda: partial_patch('cas-missing')),
        ('unselected-local-result-ignored', selected_cas),
        ('real-sigkill-unknown-repeated-recovery', worker_unknown_repeat),
        ('real-sigkill-command-cross-process-cancel', worker_command_cancel),
    ]: check(name, fn)
finally:
    if engine is not None: engine.close()
    pg.close()
assert fingerprint(ROOT) == EXPECTED
summary = {'source_sha256': EXPECTED, 'finished_source_sha256': fingerprint(ROOT),
    'finished_at': datetime.now(UTC).isoformat(), 'checks': results,
    'passed': sum(x['status'] == 'PASS' for x in results),
    'failed': sum(x['status'] == 'FAIL' for x in results), 'paid_calls': 0,
    'fixture_container': pg.name, 'fixture_cleaned': not pg.created}
private_json(OUT / 'results.json', summary)
sys.exit(1 if summary['failed'] else 0)
