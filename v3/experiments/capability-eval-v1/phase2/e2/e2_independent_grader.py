"""Adjudicate a JSONL of E2 predictions against separately frozen gold rows.

Usage: python3 e2_independent_grader.py GOLD.jsonl PREDICTIONS.jsonl RESULTS.jsonl
Gold row keys: case_id, scale, expected. Output contains only run_id, success, wrong_fields.
"""
import argparse
import json
from pathlib import Path

FIELDS = frozenset((
    "incident", "finding_code", "failure_code", "evidence_id", "commit", "constraint",
    "pending_approval", "open_question", "allowed_tools", "remaining_tool_calls",
))

def unambiguous(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate object key {key!r}")
        result[key] = value
    return result

def rows(path):
    for line in path.read_text().splitlines():
        yield json.loads(line, object_pairs_hook=unambiguous)

def grade(pred, expected):
    errors = []
    if pred["status"] != "ANSWERED":
        return ["status"]
    try:
        answer = json.loads(pred["answer"], object_pairs_hook=unambiguous)
    except (ValueError, TypeError):
        return ["answer_json"]
    if not isinstance(answer, dict):
        return ["answer_json"]
    for key in sorted(FIELDS):
        if (key not in answer or type(answer[key]) is not type(expected[key])
                or answer[key] != expected[key]):
            errors.append(key)
    if answer.keys() - FIELDS:
        errors.append("unexpected_fields")
    return errors

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("gold","predictions","results"):
        parser.add_argument(name, type=Path)
    args = parser.parse_args()
    gold = {}
    for row in rows(args.gold):
        key = (row["case_id"], row["scale"])
        if key in gold or row["expected"].keys() != FIELDS:
            raise ValueError(f"invalid gold row {key!r}")
        gold[key] = row["expected"]
    ids = set()
    with args.results.open("x") as out:
        for row in rows(args.predictions):
            if row["run_id"] in ids:
                raise ValueError("duplicate run_id")
            ids.add(row["run_id"])
            errors = grade(row, gold[row["case_id"],row["scale"]])
            print(json.dumps(dict(run_id=row["run_id"], success=not errors, wrong_fields=errors)), file=out)

if __name__ == "__main__":
    main()
