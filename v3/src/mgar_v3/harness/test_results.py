"""Complete test identities from fixed reporters, separate from model summaries."""

from __future__ import annotations

import json
import re
from collections import Counter, defaultdict
from typing import Any

from mgar_v3.harness.project_commands import parse_tests
from mgar_v3.harness.public_probe_types import PUBLIC_PROBE_PREFIX, PublicProbeFrame
from mgar_v3.harness.regression_types import (
    RegressionCommand,
    TestCaseObservation,
    TestResult,
)

_ANSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")


def _unique(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("Duplicate JSON field in test evidence")
        value[key] = item
    return value


def _case(identity: str, outcome: str) -> TestCaseObservation:
    return TestCaseObservation(test_id=identity.removeprefix("/workspace/"), outcome=outcome)


def _result(
    cases: list[TestCaseObservation],
    ran: int,
    failed: int,
    skipped: int,
    exit_code: int,
) -> TestResult:
    if (
        ran <= 0
        or len(cases) != ran
        or len({c.test_id for c in cases}) != ran
        or sum(c.outcome == "failed" for c in cases) != failed
        or sum(c.outcome == "skipped" for c in cases) != skipped
        or exit_code != (1 if failed else 0)
    ):
        raise ValueError("Reported counts, complete test identities and exit status differ")
    return TestResult(
        status="BLOCKED" if skipped else "FAIL" if failed else "PASS",
        parsed=True,
        complete=skipped == 0,
        tests_run=ran,
        tests_failed=failed,
        tests_skipped=skipped,
        cases=tuple(cases),
        failure_ids=tuple(sorted(c.test_id for c in cases if c.outcome == "failed")),
        detail="Skipped or excluded assertions prevent complete coverage"
        if skipped
        else "Complete test identities and terminal summary verified",
    )


def _expected_exceptions(command: RegressionCommand, value: TestResult) -> TestResult:
    expected = command.expected_exceptions
    if not expected or not value.parsed or not value.cases:
        return value
    if command.expected_failure_sources:
        changed = [c.model_copy(update={"outcome": "xfailed"})
                   if c.test_id in command.expected_failure_sources and c.outcome == "passed"
                   else c for c in value.cases]
        value = value.model_copy(update={
            "cases": tuple(changed),
            "tests_xfailed": sum(c.outcome == "xfailed" for c in changed),
        })
    known = {case.test_id: case.outcome for case in value.cases}
    actual = {name: state for name, state in known.items() if state not in {"passed", "failed"}}
    if (not set(expected).issubset(known) or not set(actual.items()).issubset(expected.items())
            or not any(state in {"passed", "failed"} for state in known.values())):
        return value.model_copy(update={
            "status": "BLOCKED", "complete": False,
            "detail": "New, changed or missing non-PASS test identities require review",
        })
    return value.model_copy(update={
        "status": "FAIL" if value.tests_failed else "PASS", "complete": True,
        "detail": "Complete suite under frozen exceptions; skipped/xfailed remain non-PASS cases",
    })


def _python(command: RegressionCommand, text: str, exit_code: int) -> TestResult:
    status, ran, failed, skipped, detail = parse_tests("python", text, exit_code)
    if status not in {"passed", "failed", "no_tests"}:
        raise ValueError(detail)
    cases = []
    if command.framework == "pytest":
        collected = re.findall(
            r"(?m)^(?:collecting \.\.\.\s*)?collected (\d+) items?([^\n]*)$", text
        )
        if len(collected) != 1:
            raise ValueError("A complete pytest collection inventory is required")
        total, suffix = int(collected[0][0]), collected[0][1]
        deselected = re.search(r"(\d+) deselected", suffix)
        excluded = int(deselected[1]) if deselected else 0
        public = command.public_argv
        options = public[public.index("pytest") + 1 :] if "pytest" in public else public[1:]
        if excluded and not any(v in {"-k", "-m"} for v in options):
            raise ValueError("Undeclared pytest deselection is not full regression")
        extended = bool(command.expected_exceptions) or bool(
            re.search(r"\b(?:XFAIL|XPASS)\b", text))
        if extended:
            summaries = re.findall(r"(?m)^=+ (.+?\bin [0-9.]+s[^\n]*?) =+$", text)
            if len(summaries) != 1:
                raise ValueError("Exactly one complete pytest terminal summary is required")
            counters = dict((name, int(n)) for n, name in re.findall(
                r"(\d+) (passed|failed|skipped|xfailed|xpassed|errors?)\b", summaries[0]))
            if counters.get("error", 0) or counters.get("errors", 0):
                raise ValueError("Pytest collection/setup errors are not frozen exceptions")
            ran = sum(counters.values())
            failed, skipped = counters.get("failed", 0), counters.get("skipped", 0)
        if total - excluded != ran:
            raise ValueError("Pytest stopped before every selected test finished")
        # pytest-httpbin's server thread can write between pytest's node ID
        # and terminal outcome. Preserve that one started identity; a second
        # start or missing terminal remains an incomplete report.
        normalized_lines = []
        pending_httpbin = None
        for line in text.splitlines():
            interrupted = re.match(
                r"^([^\n]+\.py::[^\n]+?)\s+(?:pytest-httpbin server hit an exception "
                r"serving request:|attempting to ignore so the rest of the tests can run$)", line)
            if interrupted:
                if pending_httpbin is not None:
                    raise ValueError("Pytest started another test before its terminal outcome")
                pending_httpbin = interrupted[1]
                continue
            if pending_httpbin is not None and re.fullmatch(
                r"(?:PASSED|FAILED|ERROR|SKIPPED|XFAIL|XPASS)\s+\[\s*\d+%\][^\n]*", line
            ):
                line = pending_httpbin + " " + line
                pending_httpbin = None
            elif pending_httpbin is not None and re.match(r"^[^\s]+\.py::", line):
                raise ValueError("Pytest lost a started test before the next test")
            normalized_lines.append(line)
        if pending_httpbin is not None:
            raise ValueError("Pytest did not finish its interrupted HTTP test")
        normalized_text = "\n".join(normalized_lines)
        for match in re.finditer(
            r"^([^\n]+\.py::[^\n]+?)[ \t]+(PASSED|FAILED|ERROR|SKIPPED|XFAIL|XPASS)"
            r"(?:[ \t]+\([^\n]*|[ \t]+\[[ \t]*\d+%\][^\n]*)?[ \t]*$",
            normalized_text,
            re.M,
        ):
            cases.append(
                _case(
                    match[1],
                    {
                        "PASSED": "passed",
                        "FAILED": "failed",
                        "ERROR": "failed",
                        "SKIPPED": "skipped",
                        "XFAIL": "xfailed" if extended else "skipped",
                        "XPASS": "xpassed" if extended else "skipped",
                    }[match[2]],
                )
            )
    else:
        pending = None
        outcome = r"ok|FAIL|ERROR|skipped\b[^\n]*|expected failure|unexpected success"
        for line in text.splitlines():
            start = re.fullmatch(r"(.+?\([^\n]+\))\s+\.\.\.\s*(.*)", line)
            if start:
                if pending is not None:
                    raise ValueError("Unittest started another test before its outcome")
                pending = start[1]
                line = start[2]
            if re.fullmatch(outcome, line):
                if pending is None:
                    raise ValueError("Unittest outcome has no started test identity")
                cases.append(_case(pending, "passed" if line == "ok" else
                                   "failed" if line in {"FAIL", "ERROR"} else "skipped"))
                pending = None
        if pending is not None:
            raise ValueError("Unittest did not finish its last started test")
    if command.framework == "pytest" and extended:
        xfailed = sum(c.outcome == "xfailed" for c in cases)
        xpassed = sum(c.outcome == "xpassed" for c in cases)
        normalized = [c.model_copy(update={"outcome": "skipped"})
                      if c.outcome in {"xfailed", "xpassed"} else c for c in cases]
        result = _result(normalized, ran, failed, skipped + xfailed + xpassed, exit_code)
        if xfailed != counters.get("xfailed", 0) or xpassed != counters.get("xpassed", 0):
            raise ValueError("Pytest expected-failure counts differ from terminal identities")
        result = result.model_copy(update={
            "cases": tuple(cases), "tests_skipped": skipped,
            "tests_xfailed": xfailed, "tests_xpassed": xpassed,
        })
        return _expected_exceptions(command, result)
    return _expected_exceptions(command, _result(cases, ran, failed, skipped, exit_code))


def _go(text: str, exit_code: int) -> TestResult:
    started: set[str] = set()
    finished: dict[str, str] = {}
    packages: dict[str, str] = {}
    for line in text.splitlines():
        if not line.strip():
            continue
        value = json.loads(line, object_pairs_hook=_unique)
        action, package, name = value["Action"], value["Package"], value.get("Test")
        if name:
            identity = package + "::" + name
            if action == "run":
                if identity in started:
                    raise ValueError("Repeated Go test identity")
                started.add(identity)
            elif action in {"pass", "fail", "skip"}:
                if identity in finished:
                    raise ValueError("Repeated Go test outcome")
                finished[identity] = {"pass": "passed", "fail": "failed", "skip": "skipped"}[action]
        elif action in {"pass", "fail", "skip"}:
            if package in packages:
                raise ValueError("Repeated Go package terminal outcome")
            packages[package] = action
    if started != set(finished) or not packages:
        raise ValueError("Go did not finish every started test/package")
    for package, state in packages.items():
        tests = [outcome for key, outcome in finished.items() if key.startswith(package + "::")]
        if state == "fail" and "failed" not in tests:
            raise ValueError("Go package failed outside an executed test")
    cases = [_case(k, v) for k, v in sorted(finished.items())]
    return _result(
        cases,
        len(cases),
        sum(c.outcome == "failed" for c in cases),
        sum(c.outcome == "skipped" for c in cases),
        exit_code,
    )


def _mocha(text: str, exit_code: int, *, occurrence_ids: bool = False) -> TestResult:
    documents = []
    for match in re.finditer(r"(?m)^\s*\{", text):
        try:
            value, length = json.JSONDecoder(object_pairs_hook=_unique).raw_decode(
                text[match.end() - 1 :]
            )
        except ValueError:
            continue
        if isinstance(value, dict) and all(
            k in value for k in ("stats", "tests", "passes", "failures", "pending")
        ):
            if text[match.end() - 1 + length :].strip():
                raise ValueError("Unexpected text after Mocha terminal report")
            documents.append(value)
    if len(documents) != 1:
        raise ValueError("Exactly one complete Mocha JSON report is required")
    document = documents[0]
    stats = document["stats"]
    if any(type(stats[k]) is not int or stats[k] < 0
           for k in ("tests", "passes", "failures", "pending")):
        raise ValueError("Invalid Mocha counters")
    if stats["passes"] + stats["failures"] + stats["pending"] != stats["tests"]:
        raise ValueError("Mocha terminal counters differ")

    def identity(value: dict[str, Any]) -> str:
        filename, title = value.get("file", ""), value["fullTitle"]
        if not isinstance(filename, str) or not isinstance(title, str) or not title:
            raise ValueError("Mocha test identity is invalid")
        prefix = filename.removeprefix("/workspace/")
        return (prefix + "::" if prefix else "") + title

    states: dict[str, str] = {}
    if occurrence_ids:
        # The tests array is the serial reporter order. Exact full record bags
        # join each observation to its outcome before assigning duplicate ordinals.
        bags: dict[str, Counter[str]] = defaultdict(Counter)
        for key, state in (("passes", "passed"), ("failures", "failed"), ("pending", "skipped")):
            for item in document[key]:
                bags[json.dumps(item, sort_keys=True)][state] += 1
        if any(len(bag) != 1 for bag in bags.values()):
            raise ValueError("Identical Mocha observations have contradictory outcomes")
        totals = Counter(identity(item) for item in document["tests"])
        positions: Counter[str] = Counter()
        for item in document["tests"]:
            raw = json.dumps(item, sort_keys=True)
            if raw not in bags or sum(bags[raw].values()) <= 0:
                raise ValueError("Mocha outcome bag does not cover the full ordered inventory")
            state = next(iter(bags[raw]))
            bags[raw][state] -= 1
            base = identity(item)
            positions[base] += 1
            name = base + (f"::occurrence-{positions[base]}" if totals[base] > 1 else "")
            if name in states:
                raise ValueError("Mocha ordinal identity collides with an original title")
            states[name] = state
        if any(sum(bag.values()) for bag in bags.values()):
            raise ValueError("Mocha outcome bag contains unexecuted observations")
        stats = document["stats"]
        if (stats["tests"] != len(states) or stats["passes"] != sum(
            v == "passed" for v in states.values()) or stats["failures"] != sum(
            v == "failed" for v in states.values()) or stats["pending"] != sum(
            v == "skipped" for v in states.values())):
            raise ValueError("Mocha terminal counters differ from ordered observations")
        return _result([_case(k, v) for k, v in states.items()], len(states),
                       stats["failures"], stats["pending"], exit_code)
    for key, state in (("passes", "passed"), ("failures", "failed"), ("pending", "skipped")):
        for item in document[key]:
            name = identity(item)
            if name in states:
                raise ValueError("Ambiguous Mocha identity")
            states[name] = state
    ids = [identity(item) for item in document["tests"]]
    if len(set(ids)) != len(ids) or set(ids) != set(states):
        raise ValueError("Mocha outcomes do not cover every test")
    stats = document["stats"]
    if any(
        type(stats[k]) is not int or stats[k] < 0
        for k in ("tests", "passes", "failures", "pending")
    ):
        raise ValueError("Invalid Mocha counters")
    if stats["passes"] + stats["failures"] + stats["pending"] != stats["tests"]:
        raise ValueError("Mocha terminal counters differ")
    return _result(
        [_case(k, states[k]) for k in ids],
        stats["tests"],
        stats["failures"],
        stats["pending"],
        exit_code,
    )


def _tap(text: str, exit_code: int) -> TestResult:
    if re.search(r"^\s*Bail out!", text, re.M | re.I):
        raise ValueError("TAP bailed out")
    contexts: dict[int, str] = {}
    cases = []
    for line in text.splitlines():
        title = re.fullmatch(r"(\s*)# Subtest: (.+)", line)
        if title:
            depth = len(title[1])
            contexts = {k: v for k, v in contexts.items() if k < depth}
            contexts[depth] = title[2]
        match = re.fullmatch(r"(\s*)(not ok|ok)\s+\d+(?:\s+-)?\s+(.+)", line)
        if match:
            depth = len(match[1])
            name = match[3]
            skipped = re.search(r"\s+#\s*(?:SKIP|TODO)\b", name, re.I)
            if skipped:
                name = name[: skipped.start()]
            identity = " / ".join([v for k, v in sorted(contexts.items()) if k < depth] + [name])
            cases.append(
                _case(
                    identity, "skipped" if skipped else "passed" if match[2] == "ok" else "failed"
                )
            )
    counts = {
        k: int(v)
        for k, v in re.findall(
            r"(?m)^# (tests|pass|fail|skipped|skip|todo|cancelled) (\d+)\s*$", text
        )
    }
    plans = re.findall(r"(?m)^1\.\.(\d+)\s*$", text)
    if not plans:
        raise ValueError("TAP has no terminal test plan")
    ran = counts.get("tests", int(plans[-1]))
    if ran != len(cases):
        cases = [
            c
            for c in cases
            if not any(other.test_id.startswith(c.test_id + " / ") for other in cases)
        ]
    failed = sum(c.outcome == "failed" for c in cases)
    skipped_count = sum(c.outcome == "skipped" for c in cases)
    if counts and (
        counts.get("fail", 0) + counts.get("cancelled", 0) != failed
        or counts.get("skipped", counts.get("skip", 0)) + counts.get("todo", 0) != skipped_count
    ):
        raise ValueError("TAP terminal counters differ from test points")
    return _result(cases, ran, failed, skipped_count, exit_code)


def parse_independent_test(
    command: RegressionCommand,
    output: str,
    exit_code: int | None,
    *,
    complete: bool,
) -> TestResult:
    if not complete or exit_code not in {0, 1}:
        return TestResult(
            status="BLOCKED",
            parsed=False,
            complete=False,
            detail="Execution/log incomplete or terminal exit is not a test outcome",
        )
    text = _ANSI.sub("", output).replace("\r", "\n")
    try:
        if command.framework == "unconfigured":
            raise ValueError("The public project reporter has not been frozen")
        if command.framework in {"pytest", "unittest"}:
            return _python(command, text, exit_code)
        if command.framework == "go-json":
            return _expected_exceptions(command, _go(text, exit_code))
        if command.framework == "mocha-json":
            return _expected_exceptions(command, _mocha(
                text, exit_code,
                occurrence_ids=command.identity_policy == "mocha-file-title-occurrence-v1"))
        if command.framework == "tap":
            return _expected_exceptions(command, _tap(text, exit_code))
        plan = command.public_probe
        if plan is None or not output.startswith(PUBLIC_PROBE_PREFIX) or not output.endswith("\n"):
            raise ValueError("Missing public probe frame")
        frame = PublicProbeFrame.model_validate(
            json.loads(
                output[len(PUBLIC_PROBE_PREFIX) : -1],
                object_pairs_hook=_unique,
            )
        )
        if (
            frame.plan_id != plan.plan_id
            or frame.asset_sha256 != plan.asset_sha256
            or {c.id for c in frame.cases} != set(plan.case_ids)
            or any(c.status == "error" for c in frame.cases)
        ):
            raise ValueError("Public probe frame differs from the frozen test plan")
        cases = [
            _case(
                c.id,
                "passed"
                if c.status == "passed"
                else "failed"
                if c.status == "assertion_failed"
                else "skipped",
            )
            for c in frame.cases
        ]
        return _result(
            cases,
            len(cases),
            sum(c.outcome == "failed" for c in cases),
            sum(c.outcome == "skipped" for c in cases),
            exit_code,
        )
    except (ValueError, KeyError, TypeError, AttributeError, RecursionError) as exc:
        return TestResult(
            status="BLOCKED",
            parsed=False,
            complete=False,
            detail="Test report unverified: " + str(exc)[:500],
        )
