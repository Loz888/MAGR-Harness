"""Independent E3 receipt check. CLI: GOLD.jsonl RAW.jsonl OUTPUT.json.
Only include holdout case IDs in raw. Runs with no request can be omitted or included
with empty actions; the output enumerates missing trial rows in either situation.
"""
import argparse
import json
from collections import Counter
from pathlib import Path

ARMS = ('off', 'bm25', 'hybrid', 'hybrid_reranked')

def load(path):
    return [json.loads(line) for line in path.read_text().splitlines()]

def evaluate(keys, rows):
    expected = {(k['case_id'],k['seed'],k['session']): k for k in keys}
    scheduled = {(c,s,a,n) for c,s,n in expected for a in ARMS}
    observed = set()
    decisions = []
    learned = {}
    for row in sorted(rows, key=lambda x:(x['case_id'],x['seed'],x['arm'],x['session'])):
        c,s,a,n = (row[k] for k in ('case_id','seed','arm','session'))
        trial = (c,s,a,n)
        if trial in observed or trial not in scheduled:
            raise ValueError(f'duplicate or unscheduled trial {trial!r}')
        observed.add(trial)
        k = expected[c,s,n]
        previous = learned.setdefault((c,s,a),{})
        mem_id = row.get('memory_id')
        allowed = set()
        if (a != 'off' and mem_id and not row.get('historical_memory',True)
                and previous.get(mem_id) == k['proof']):
            allowed.add(k['proof'])
        success = False
        stale = 0
        receipts = 0
        reasons = []
        for act in row['actions']:
            if act['action']=='inspect_current':
                if act.get('evidence_id') and act.get('proof')==k['proof']:
                    allowed.add(k['proof'])
                    receipts += 1
            elif act['action']=='submit':
                if act.get('version')!=k['version']:
                    stale += 1
                    reasons.append('version')
                elif act.get('proof')!=k['proof'] or k['proof'] not in allowed:
                    reasons.append('evidence')
                elif type(act.get('answer')) is not type(k['answer']) or act['answer']!=k['answer']:
                    reasons.append('answer')
                else:
                    success = True
                    break
        if success and row.get('created_memory_id'):
            previous[row['created_memory_id']] = k['proof']
        decisions.append(dict(case_id=c,seed=s,arm=a,session=n,graded_success=success,
                              declaration_agrees=row['final_success'] is success,
                              stale_submissions=stale,inspected=receipts,submission_errors=reasons))
    return dict(scheduled=len(scheduled),observed=len(observed),missing=sorted(scheduled-observed),
                passed=sum(d['graded_success'] for d in decisions),
                disagreements=[r for r in decisions if not r['declaration_agrees']],
                stale_submissions=sum(d['stale_submissions'] for d in decisions),
                arms=dict(Counter(r['arm'] for r in decisions if r['graded_success'])), rows=decisions)

def main():
    p=argparse.ArgumentParser(description=__doc__)
    for name in ('gold','raw','output'):
        p.add_argument(name,type=Path)
    args=p.parse_args()
    result=evaluate(load(args.gold),load(args.raw))
    with args.output.open('x') as out:
        json.dump(result,out,ensure_ascii=False,indent=2)
        out.write('\n')

if __name__=='__main__':
    main()
