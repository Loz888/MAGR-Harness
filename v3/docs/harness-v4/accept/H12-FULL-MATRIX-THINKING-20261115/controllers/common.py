import hashlib
import importlib.util
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

import psycopg
from psycopg.rows import dict_row

from mgar_v3.domain import digest

ROOT = Path(__file__).resolve().parents[5]
BASE = ROOT / '.local/h12/full-matrix-thinking-20261115'
EXPERIMENT = 'hv4-h12-complete-thinking-max-20261115'
spec = importlib.util.spec_from_file_location('h12_evaluation_configuration', ROOT/'scripts/h12_eval.py')
m = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = m
spec.loader.exec_module(m)
BINDING = json.loads((m.STATE/'budget-database-binding.json').read_bytes())
SETTINGS = m.load_settings(Path(BINDING['settings_file']))
BUDGET_KEY = digest({'card': 'HV4-H12', 'project': SETTINGS.project_id,
                     'shared_authorization': 'user-approved-12-usd-v1'})


def now():
    return datetime.now(UTC).isoformat()


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def ref(path):
    return {'path': str(Path(path).relative_to(ROOT)), 'sha256': sha(path)}


def write(path, value, *, exclusive=False):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if exclusive:
        with path.open('x') as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2, default=str)
            stream.write('\n')
    else:
        temporary = path.with_suffix(path.suffix+'.pending')
        temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2, default=str)+'\n')
        temporary.replace(path)


def connect():
    return psycopg.connect(SETTINGS.database_url.get_secret_value(),
        options='-c default_transaction_read_only=on', row_factory=dict_row)


def budget_snapshot(conn):
    budget = conn.execute('SELECT limit_micros,reserved_micros,settled_micros,used_calls,call_limit '
        'FROM app.authorization_budgets WHERE profile_hash=%s', (BUDGET_KEY,)).fetchone()
    unknown = conn.execute("SELECT count(*) AS n FROM app.usage_calls WHERE profile_hash=%s "
        "AND status='unknown'", (BUDGET_KEY,)).fetchone()['n']
    return {'budget': budget, 'unknown_calls': unknown}


def audit_case(conn, score):
    task = conn.execute("SELECT t.run_id,t.tree_hash,r.status FROM app.harness_tasks t "
        "JOIN app.runs r ON r.run_id=t.run_id WHERE t.experiment_id=%s "
        "AND t.evaluation_profile->>'task_id'=%s AND t.evaluation_profile->>'split'=%s",
        (EXPERIMENT, score['task_id'], score['split'])).fetchall()
    if not score.get('run_id'):
        assert score['status']=='NOT_RUN' and not task
        assert score['cost_micros']==score['reserved_micros']==score.get('model_calls', 0)==0
        return {'task_id': score['task_id'], 'run_id': None, 'status': 'NOT_STARTED',
                'calls': 0, 'cost_micros': 0, 'unknown': 0}
    assert len(task)==1 and str(task[0]['run_id'])==score['run_id']
    assert task[0]['tree_hash']==score.get('final_tree')
    assert task[0]['status'] in {'succeeded', 'failed', 'cancelled'}
    rows = conn.execute('SELECT u.status,u.settled_micros,o.state AS operation_state '
        'FROM app.usage_calls u LEFT JOIN app.harness_operations o ON o.model_call_id=u.call_id '
        'WHERE u.run_id=%s', (score['run_id'],)).fetchall()
    calls = sum(u['status'] in {'known','unknown'} or u['operation_state'] in {'dispatched','unknown'}
                for u in rows)
    unknown = sum(u['status']=='unknown' or u['status']=='reserved' and
                  u['operation_state'] in {'dispatched','unknown'} for u in rows)
    cost = sum(u['settled_micros'] or 0 for u in rows if u['status']=='known')
    assert calls==score['model_calls'] and cost==score['settled_cost_micros']
    assert unknown==0 and score['usage_known'] is True and score['reserved_micros']==0
    return {'task_id': score['task_id'], 'run_id': score['run_id'], 'status': task[0]['status'],
            'calls': calls, 'cost_micros': cost, 'unknown': unknown,
            'tree_hash': task[0]['tree_hash']}
