import concurrent.futures,hashlib,json,subprocess
from pathlib import Path

root=Path(__file__).resolve().parents[5];base=root/'.local/h12/first-model-recovery-20261115';python=str(root/'.venv/bin/python')
def sha(p):return hashlib.sha256(p.read_bytes()).hexdigest()
def run(name,args):
    log=base/(name+'.log')
    if log.exists():raise RuntimeError('Immutable preparation log exists')
    result=subprocess.run([python,*args],cwd=root,capture_output=True,text=True)
    log.write_text(result.stdout+result.stderr)
    print(json.dumps({'phase':name,'exit_code':result.returncode,'output':result.stdout+result.stderr}),flush=True)
    if result.returncode:raise RuntimeError(name+' failed')
anchor=root/'.local/h12/benchmark-v2/freeze-manifest-d8ee974.json'
dev=base/'preparation.json';quality=base/'quality-preparation.json'
devargs=['scripts/h12_public_dev_freeze.py','--benchmark-freeze',str(anchor),'--benchmark-freeze-sha256',sha(anchor),'--public-plan-map',str(root/'experiments/harness-v4/public-plans-v2/public-plan-map.json'),'--public-probe-root',str(root/'experiments/harness-v4/public-plans-v1/assets'),'--dev-image-map',str(root/'.local/h12/public-dev-20261113/image-map.json')]
amendment=root/'.local/h12/quality-repair-20261114/click-amendment-v2/amendment.json'
devargs+=['--development-amendment',str(amendment),'--development-amendment-sha256',sha(amendment)]
scope=root/'.local/h12/full-matrix-thinking-20261115/scope-amendment/amendment.json'
devargs+=['--development-scope-amendment',str(scope),'--development-scope-amendment-sha256',sha(scope)]
auth=root/'.local/h12/full-matrix-thinking-20261115/complete-matrix-authorization.json'
quality_args=['scripts/h12_quality_freeze.py','--complete-matrix-authorization',str(auth),'--complete-matrix-authorization-sha256',sha(auth)]
run('bind-development',devargs+['--out',str(dev)])
run('bind-quality',quality_args+['--public-dev-freeze',str(dev),'--public-dev-freeze-sha256',sha(dev),'--holdout-image-map',str(root/'.local/h12/quality-repair-20261114/holdout-image-map.json'),'--out',str(quality)])
jobs=[]
with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
    for name,flag in [('subset','--prepare-public-readiness'),('full-dev','--prepare-full-dev-readiness')]:
        jobs.append(pool.submit(run,'execute-'+name+'',['scripts/h12_eval.py','--public-dev-freeze',str(dev),'--public-dev-freeze-sha256',sha(dev),'--experiment-id','quality-zero-model',flag,'--out',str(base/(name+''))]))
    jobs.append(pool.submit(run,'execute-holdout',['scripts/h12_eval.py','--quality-freeze',str(quality),'--quality-freeze-sha256',sha(quality),'--experiment-id','quality-zero-model','--prepare-holdout-readiness','--out',str(base/'holdout')]))
    for job in concurrent.futures.as_completed(jobs):job.result()
subset=base/'subset/report.json';full=base/'full-dev/report.json';held=base/'holdout/report.json';sealed=base/'public-dev-frozen.json'
run('seal-development',devargs+['--public-subset-readiness',str(subset),'--public-subset-readiness-sha256',sha(subset),'--full-dev-readiness',str(full),'--full-dev-readiness-sha256',sha(full),'--out',str(sealed)])
run('seal-quality',quality_args+['--public-dev-freeze',str(sealed),'--public-dev-freeze-sha256',sha(sealed),'--holdout-image-map',str(root/'.local/h12/quality-repair-20261114/holdout-image-map.json'),'--holdout-readiness',str(held),'--holdout-readiness-sha256',sha(held),'--out',str(base/'quality-frozen.json')])
