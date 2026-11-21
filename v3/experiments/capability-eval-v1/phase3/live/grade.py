"""An output-only oracle for frozen synthetic cases. It never runs generated code."""

from __future__ import annotations

import ast
import hashlib
import re


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def source_match(record, path):
    return set(record["observed_paths"]) == set(record["source"]) and all(
        (path / name).is_file() for name in record["source"]
    )


def patch_shape(record):
    forms = []
    for row in record["captures"]:
        blob = row["cas"] or {}
        if row["receipt"]["response_status"] != "complete":
            continue
        for call in blob.get("raw_tool_calls", []):
            if call.get("name") == "apply_edits":
                import json

                raw = call.get("arguments", "")
                try:
                    forms.extend(json.loads(raw)["edits"])
                except (TypeError, ValueError, KeyError):
                    pass
    arm = record["arm"]
    if arm == "full_file":
        return len(forms) == 1 and isinstance(forms[0].get("content"), str)
    if arm == "structured_plan":
        return len(forms) == 1 and len(forms[0].get("replacements", ())) == 3
    return len(forms) == 3 and all(
        len(op.get("replacements", ())) == 1
        and bool(op.get("expected_sha256"))
        and (arm != "verified_resume" or bool(op["replacements"][0].get("old_sha256")))
        for op in forms
    )


def grade(record, case, path):
    result = record["result"]
    checks = {"terminal": result.get("status") == "completed", "paths": source_match(record, path)}
    if record["study"] == "h12":
        source = path / "source.py"
        want = "".join(
            f"{k} = {v}\n"
            for k, v in zip(("width", "height", "margin"), case["positions"], strict=True)
        ) + "".join(f"# filler {i:03} 測定\n" for i in range(case["padding_lines"]))
        checks["exact_replacement"] = source.is_file() and source.read_text() == want
        original_test = path / "test_source.py"
        checks["tests_unchanged"] = (
            original_test.is_file() and digest(original_test) == record["source"]["test_source.py"]
        )
        checks["all_tests_confirmed"] = (
            result.get("tests", {}).get("status") == "passed" and result["tests"].get("run") == 3
        )
    elif case["stratum"] in {"simple", "complex", "research"}:
        answer = (result.get("summary") or "").replace("`", "")
        checks["unchanged"] = checks["paths"] and all(
            digest(path / name) == expected for name, expected in record["source"].items()
        )
        checks["facts_and_references"] = all(
            re.search(rf"source_{i}\.txt[^\n]{{0,70}}?\[{case['datum'] + i}\]", answer)
            for i in range(case["operations"])
        )
    else:
        original_test = path / "test_suite.py"
        checks["tests_unchanged"] = (
            original_test.is_file() and digest(original_test) == record["source"]["test_suite.py"]
        )
        checks["all_tests_confirmed"] = (
            result.get("tests", {}).get("status") == "passed"
            and result["tests"].get("run") == case["operations"]
        )
        matches = []
        for i in range(case["operations"]):
            source = path / f"unit_{i}.py"
            value = case["datum"] + i
            allowed = {
                ast.dump(ast.parse(f"def result(value):\n    return {expr}\n"))
                for expr in (f"value+{value}", f"{value}+value")
            }
            try:
                matches.append(ast.dump(ast.parse(source.read_text())) in allowed)
            except (OSError, SyntaxError):
                matches.append(False)
        checks["independent_ast"] = all(matches)
    return {
        "success": all(checks.values()),
        "checks": checks,
        "quality": sum(checks.values()) / len(checks),
        "strategy_matched": patch_shape(record) if record["study"] == "h12" else None,
    }
