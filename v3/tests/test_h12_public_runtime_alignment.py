"""Real PG/Docker service alignment for the new frozen public runtime policy."""

from __future__ import annotations

import json
import os
import re
from functools import partial
from pathlib import Path

import test_h12_public_probe_service as toy

import mgar_v3.harness.service as service_module
from mgar_v3.harness.broker import IsolatedExecutionBroker
from mgar_v3.harness.contracts import EvaluationProfile
from mgar_v3.harness.ledger import PersistentBudgetLedger


def test_public_profile_scratch_reaches_pg_service_lease_and_real_container(
    engine, tmp_path, monkeypatch, record_property,
):
    # The helper freezes its experiment inside _case. Supply the field during
    # construction, before any PG registration, rather than editing a frozen profile.
    monkeypatch.setattr(toy, 'EvaluationProfile',
                        partial(EvaluationProfile, sandbox_scratch_mb=256))
    case = toy._case(engine, tmp_path)
    assert isinstance(case.profile, EvaluationProfile)
    assert case.profile.sandbox_scratch_mb == 256
    legacy = case.profile.model_copy(update={'sandbox_scratch_mb': 64})
    assert legacy.sandbox_scratch_mb == 64
    assert 'sandbox_scratch_mb' not in legacy.model_dump(mode='json')
    assert legacy.resources == case.profile.resources
    frozen = engine.harness.get_experiment(case.profile.experiment_id)
    profiles = frozen['config']['profiles']
    assert len(profiles) == 48
    assert all(profile['sandbox_scratch_mb'] == 256 for profile in profiles)

    issued = []
    original_issue = service_module.issue_lease
    def capture_lease(**kwargs):
        lease = original_issue(**kwargs)
        issued.append(lease)
        return lease
    monkeypatch.setattr(service_module, 'issue_lease', capture_lease)

    created = []
    original_docker = IsolatedExecutionBroker._docker
    def capture_container(broker, arguments, **kwargs):
        result = original_docker(broker, arguments, **kwargs)
        if arguments and arguments[0] == 'create' and result.returncode == 0:
            # Read only the exact ID returned by this broker's own successful create.
            container_id = result.stdout.strip().decode('ascii')
            assert re.fullmatch('[0-9a-f]{64}', container_id)
            inspected = original_docker(broker, ['inspect', container_id], timeout=10)
            assert inspected.returncode == 0
            rows = json.loads(inspected.stdout)
            assert len(rows) == 1 and rows[0]['Id'] == container_id
            scratch = rows[0]['HostConfig']['Tmpfs']['/scratch']
            assert 'size=256m' in scratch.split(',')
            assert rows[0]['HostConfig']['ReadonlyRootfs'] is True
            lease = broker.authority.peek()
            assert lease.scratch_mb == 256
            created.append({
                'container_id': container_id, 'lease_id': lease.lease_id,
                'scratch_mb': lease.scratch_mb, 'scratch_tmpfs': scratch,
                'image_id': rows[0]['Image'], 'readonly_rootfs': True,
            })
        return result
    monkeypatch.setattr(IsolatedExecutionBroker, '_docker', capture_container)

    case.model.steps = toy._successful_steps()
    service = case.service()
    result = service.create_task(case.request(service))
    candidate = toy._candidate(case, result)
    assert candidate['status'] == 'completed'
    assert len(case.model.requests) == candidate['model_calls'] == 5
    assert candidate['tool_calls'] == 6
    probes = [row for row in candidate['receipts'] if tuple(row['argv']) == case.plan.argv]
    suites = [row for row in candidate['receipts'] if tuple(row['argv']) == toy.REGRESSION]
    assert [row['public_probe']['outcome'] for row in probes] == ['red', 'green']
    assert len(suites) == 2 and all(row['status'] == 'passed' for row in suites)
    assert probes[0]['tree_hash'] == candidate['tree_before']
    assert probes[1]['tree_hash'] == candidate['tree_after']
    assert issued and all(lease.scratch_mb == 256 for lease in issued)
    assert len(created) == 4 and len({row['container_id'] for row in created}) == 4
    assert {row['lease_id'] for row in created} == {lease.lease_id for lease in issued}

    task = service.store.get(result['task_id'])
    assert task['evaluation_profile']['sandbox_scratch_mb'] == 256
    assert task['evaluation_profile']['resources'] == case.profile.resources.model_dump(mode='json')
    usage = PersistentBudgetLedger(service.store, result['task_id'], task['epoch']).snapshot(
        result['task_id'],
    )
    assert usage.model_calls == 5 and usage.tool_calls == 6
    proof = {
        'status': 'SERVICE_RUNTIME_ALIGNMENT_PASS', 'controlled_model_calls': 5,
        'paid_model_calls': 0, 'tool_calls': 6, 'quality_status': 'NOT_RUN',
        'experiment_id': case.profile.experiment_id, 'task_id': result['task_id'],
        'frozen_profiles': len(profiles), 'frozen_profile_scratch_mb': 256,
        'task_profile_scratch_mb': task['evaluation_profile']['sandbox_scratch_mb'],
        'leases': [{'lease_id': lease.lease_id, 'lease_hash': lease.lease_hash,
                    'mode': lease.mode, 'scratch_mb': lease.scratch_mb} for lease in issued],
        'owned_containers': created,
        'physical_scope': 'Real broker commands and exact owned-ID Docker inspect; no extra exec',
    }
    record_property('runtime_alignment', json.dumps(proof, sort_keys=True))
    if os.environ.get('H12_RUNTIME_ALIGNMENT_EVIDENCE'):
        output = Path(os.environ['H12_RUNTIME_ALIGNMENT_EVIDENCE'])
        output.write_text(json.dumps(proof, indent=2))
