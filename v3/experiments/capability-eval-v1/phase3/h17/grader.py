"""Score evidence and output bytes without consulting the model script or its receipts."""

from pathlib import Path


def judge(case: dict, data: dict, project: Path, worktree: Path) -> dict:
    root = project
    task_files = {str(p.relative_to(root)): p.read_bytes() for p in root.glob("*") if p.is_file()}
    observed = {
        str(p.relative_to(worktree)): p.read_bytes() for p in worktree.glob("*") if p.is_file()
    }
    research = case["stratum"] in {"simple", "complex", "research"}
    if research:
        paths = [f"source_{i}.txt" for i in range(case["operations"])]
        citation = all(path in data["summary"] for path in paths)
        fact = all(f"[{case['datum'] + i}]" in data["summary"] for i in range(len(paths)))
        checks = {"citations": citation, "answer": fact, "expected_tree": observed == task_files}
    else:
        targets = [f"unit_{i}.py" for i in range(case["operations"])]
        correct = dict(task_files)
        for i, path in enumerate(targets):
            constant = case["datum"] + i
            correct[path] = f"def result(value):\n    return value + {constant}\n".encode()
        checks = {
            "same_test_bytes": observed.get("test_suite.py") == task_files["test_suite.py"],
            "all_tests_executed": data["tests"]["status"] == "passed"
            and data["tests"]["run"] == len(targets),
            "expected_tree": observed == correct,
        }
    checks["terminal"] = data["status"] == "completed"
    return {
        "pass": all(checks.values()),
        "checks": checks,
        "quality": sum(checks.values()) / len(checks),
    }
