"""Observed child failures survive the public reporter; assertions remain authoritative."""

import ast
import base64
import json
import subprocess
import sys

import pytest

from mgar_v3.domain import DomainError
from mgar_v3.harness.public_probe_types import PublicProbeFrame
from mgar_v3.quality_eval.public_probe_diagnostics import instrument_public_probe


def probe(check):
    return '''from __future__ import annotations
import contextlib,io,json,subprocess,sys
def check():
''' + ''.join('    ' + line + '\n' for line in check.splitlines()) + '''
def main():
    state='passed';detail=''
    try:
        with contextlib.redirect_stdout(io.StringIO()),contextlib.redirect_stderr(io.StringIO()):
            check()
    except AssertionError as error:
        state='assertion_failed';detail=str(error)[:800]
    print(json.dumps({'status':state,'detail':detail}))
    return 0 if state=='passed' else 1
if __name__=='__main__':sys.exit(main())
'''


def run(tmp_path, source):
    path = tmp_path / 'probe.py'
    path.write_text(source)
    return subprocess.run([sys.executable, str(path)], capture_output=True, text=True,
                          timeout=10, check=False)


def test_empty_assertion_reports_real_child_error_without_changing_assertions_or_fixed_commands(
    tmp_path,
):
    original = probe("success=subprocess.run([sys.executable,'-c',"
                     "\"import sys;sys.stderr.write('retry expects plain object');sys.exit(1)\"],"
                     "capture_output=True,text=True,check=False)\nassert success.returncode==0")
    revised = instrument_public_probe(original)
    before, after = ast.parse(original), ast.parse(revised)
    def checks(tree):
        return next(n for n in tree.body
                    if isinstance(n, ast.FunctionDef) and n.name == 'check')

    assert ast.dump(checks(before)) == ast.dump(checks(after))
    old = run(tmp_path, original)
    assert old.returncode == 1 and json.loads(old.stdout)['detail'] == ''
    new = run(tmp_path, revised)
    data = json.loads(new.stdout)
    assert new.returncode == 1 and data['status'] == 'assertion_failed'
    assert 'exit=1' in data['detail'] and 'retry expects plain object' in data['detail']


def test_observed_output_is_bounded_and_failure_stays_red(tmp_path):
    original = probe("result=subprocess.run([sys.executable,'-c',"
                     "\"import sys;sys.stderr.write('x'*20000);sys.exit(3)\"],"
                     "capture_output=True,text=True,check=False)\nassert result.returncode==0")
    result = run(tmp_path, instrument_public_probe(original))
    data = json.loads(result.stdout)
    assert result.returncode == 1 and 'exit=3' in data['detail']
    assert len(data['detail']) <= 800


def test_passing_check_and_explicit_assertion_message_keep_original_meaning(tmp_path):
    passing = run(tmp_path, instrument_public_probe(probe('assert 2 + 2 == 4')))
    assert passing.returncode == 0
    assert json.loads(passing.stdout) == {'status': 'passed', 'detail': ''}
    failing = run(tmp_path, instrument_public_probe(
        probe("assert False, 'explicit public failure'")))
    assert failing.returncode == 1
    assert json.loads(failing.stdout)['detail'] == 'explicit public failure'


def test_unrecognized_reporter_is_refused():
    with pytest.raises(DomainError) as caught:
        instrument_public_probe('def check():\n    assert False\n')
    assert caught.value.code == 'PUBLIC_PROBE_DIAGNOSTICS_UNSUPPORTED'


@pytest.mark.parametrize('size', [3000, 90000])
def test_raw_child_diagnostic_is_retained_for_command_cas_with_explicit_truncation(tmp_path, size):
    original = probe("result=subprocess.run([sys.executable,'-c',"
                     f"\"import sys;sys.stderr.write('x'*{size});sys.exit(7)\"],"
                     "capture_output=True,text=True,check=False)\nassert result.returncode==0")
    result = run(tmp_path, instrument_public_probe(original))
    data = json.loads(result.stdout)
    metadata = data['diagnostics'][0]
    assert result.returncode == 1 and data['status'] == 'assertion_failed'
    assert result.stderr == '' and result.stdout.count('\n') == 1
    assert {k: metadata[k] for k in ('stream', 'total_bytes', 'retained_bytes', 'truncated')} == {
        'stream': 'stderr', 'total_bytes': size,
        'retained_bytes': min(size, 65536), 'truncated': size > 65536}
    assert base64.b64decode(metadata['retained_base64']) == b'x' * min(size, 65536)
    assert len(data['detail']) <= 800


@pytest.mark.parametrize('mode', ['valid', 'invalid-base64', 'wrong-truncated'])
def test_public_frame_validates_optional_raw_diagnostics_without_relaxing_original_cases(mode):
    item = {'stream': 'stderr', 'total_bytes': 7, 'retained_bytes': 7, 'truncated': False,
            'retained_base64': base64.b64encode(b'failure').decode()}
    if mode == 'invalid-base64':
        item['retained_base64'] = '!invalid!'
    if mode == 'wrong-truncated':
        item['truncated'] = True
    payload = {'plan_id': 'public', 'asset_sha256': 'a' * 64,
               'cases': [{'id': 'contract', 'status': 'assertion_failed'}], 'diagnostics': [item]}
    if mode == 'valid':
        frame = PublicProbeFrame.model_validate(payload)
        assert frame.cases[0].status == 'assertion_failed'
        assert frame.diagnostics[0].retained_bytes == 7
        legacy = PublicProbeFrame.model_validate({k: v for k, v in payload.items()
                                                 if k != 'diagnostics'})
        assert 'diagnostics' not in legacy.model_dump(mode='json')
    else:
        with pytest.raises(ValueError):
            PublicProbeFrame.model_validate(payload)
