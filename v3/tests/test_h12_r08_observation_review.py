"""Explicit cross-language observer review; historical v1 remains closed."""

import hashlib

import pytest
from pydantic import ValidationError
from test_h12_r06_contracts import refactor_policy

from mgar_v3.domain import DomainError, digest
from mgar_v3.harness.semantic_types import JudgeTraceabilityManifest
from mgar_v3.quality_eval.h12_judge import (
    _normalize,
    _positive_checks,
    _reviewed_observation_checks,
)
from mgar_v3.quality_eval.h12_judge_observation import validate_observation_commands
from mgar_v3.quality_eval.h12_judge_review import assertion_inventory, review_judge

ARGV = ("node", "-e", "console.log(JSON.stringify({value: 7}))")


@pytest.mark.parametrize("language", ["go", "javascript", "typescript"])
def test_private_pytest_counts_follow_actual_runner_for_other_project_languages(language):
    argv = _normalize(["python3", "-m", "pytest", "--verbosity=2", "tests/hidden/test_raw.py"])
    log = "tests/hidden/test_raw.py::test_value PASSED [100%]\n=== 1 passed in 0.01s ===\n"
    assert _positive_checks(language, argv, log, 0)
    assert not _positive_checks(language, ["node", "observe.js"], log, 0)
    assert not _positive_checks(language, argv, log, 1)


SOURCE = b"""import json, subprocess
def test_raw_value():
 result = subprocess.run(["node", "-e", "console.log(JSON.stringify({value: 7}))"],
                         capture_output=True,text=True,timeout=20,check=False)
 assert result.returncode == 0
 assert json.loads(result.stdout)["value"] == 7
"""
NAME = "tests/hidden/test_observed.py"


def trace(sources=None, *, argv=ARGV):
    sources = sources or {NAME: SOURCE}
    rows = assertion_inventory(sources, observation_commands=[argv])
    policy = refactor_policy().model_copy(
        update={"requirements": refactor_policy().requirements[:1]}
    )
    requirement_id = policy.requirements[0].acceptance_requirement_id
    manifest = JudgeTraceabilityManifest(
        version="harness-judge-traceability-v2",
        task_id="r08-observe",
        benchmark_revision=policy.benchmark_revision,
        public_acceptance_hash=policy.public_acceptance_hash,
        files={n: hashlib.sha256(raw).hexdigest() for n, raw in sources.items()},
        assertions=[
            {k: v for k, v in row.items() if k != "named_constraints"}
            | {"acceptance_requirement_id": requirement_id}
            for row in rows.values()
        ],
        observation_commands=(argv,),
        command_hash=digest([["python3", "-m", "pytest", NAME]]),
    )
    return policy, manifest


def test_v2_actual_python_assertions_and_exact_observer_are_reviewed():
    policy, manifest = trace()
    result = review_judge(
        policy,
        manifest,
        {NAME: SOURCE},
        task_id="r08-observe",
        commands=[["python3", "-m", "pytest", NAME]],
    )
    assert result["status"] == "PASS" and result["assertion_count"] == 2


def test_v1_still_rejects_the_same_external_program():
    with pytest.raises(DomainError, match="Dynamic/external"):
        assertion_inventory({NAME: SOURCE})
    _, manifest = trace()
    with pytest.raises(ValidationError, match="explicit v2"):
        JudgeTraceabilityManifest.model_validate(
            manifest.model_dump() | {"version": "harness-judge-traceability-v1"}
        )


def test_v1_default_serialization_is_unchanged():
    _, manifest = trace()
    original = manifest.model_dump(exclude={"observation_commands", "observer_sources"})
    original["version"] = "harness-judge-traceability-v1"
    assert JudgeTraceabilityManifest.model_validate(original).model_dump() == original


@pytest.mark.parametrize(
    "extra",
    ["shell=True", "env={}", "cwd='/'", "executable='sh'", "input='unfrozen'", "timeout=61"],
)
def test_opaque_runtime_authority_or_unbounded_observer_is_denied(extra):
    raw = SOURCE.replace(b"check=False)", ("check=False," + extra + ")").encode())
    with pytest.raises(DomainError):
        assertion_inventory({NAME: raw}, observation_commands=[ARGV])


def test_observer_argv_and_all_source_bytes_are_bound():
    with pytest.raises(DomainError, match="differs from its frozen inventory"):
        assertion_inventory(
            {NAME: SOURCE.replace(b"value: 7", b"value: 8")}, observation_commands=[ARGV]
        )
    policy, manifest = trace()
    with pytest.raises(DomainError, match="files differ"):
        review_judge(policy, manifest, {NAME: SOURCE + b"\n"}, task_id="r08-observe")


def test_empty_wrapper_cannot_become_a_reviewed_judge():
    raw = SOURCE[: SOURCE.index(b" assert")]
    with pytest.raises(DomainError, match="empty assertion"):
        assertion_inventory({NAME: raw}, observation_commands=[ARGV])


@pytest.mark.parametrize(
    "program",
    [
        "assert(1)",
        "if (true) console.log('PASS')",
        "process.exit(0)",
        "eval('test()')",
        "require('child_process')",
        "log.Fatal(1)",
    ],
)
def test_external_verdict_or_opaque_program_is_not_an_observer(program):
    with pytest.raises(DomainError, match="raw observations"):
        validate_observation_commands({}, [("node", "-e", program)], {})


def test_unlisted_observer_and_unused_commands_are_rejected():
    with pytest.raises(DomainError, match="source closure"):
        validate_observation_commands({"tests/hidden/observe.go": b"package main"}, [ARGV], {})
    with pytest.raises(DomainError, match="absent from the Judge"):
        assertion_inventory({NAME: SOURCE}, observation_commands=[ARGV, ("node", "-e", "1")])


def test_go_observer_requires_the_full_exact_source_hash():
    name = "tests/hidden/observe.go"
    data = b'package main\nimport "fmt"\nfunc main(){fmt.Println(7)}\n'
    argv = ("go", "run", name)
    expected = {name: hashlib.sha256(data).hexdigest()}
    assert validate_observation_commands({name: data}, [argv], expected) == {argv}
    with pytest.raises(DomainError, match="scope or hash"):
        validate_observation_commands({name: data + b"\n"}, [argv], expected)


def test_literal_module_string_constant_is_resolved_without_exec():
    code = ARGV[2]
    source = (
        "import subprocess\nCODE="
        + repr(code)
        + "\n"
        + SOURCE.decode().replace('"console.log(JSON.stringify({value: 7}))"', "CODE")
    )
    assert len(assertion_inventory({NAME: source.encode()}, observation_commands=[ARGV])) == 2


@pytest.mark.parametrize("text", ["H12_CHECKS_PASSED:2\n", "2 passing\n", "2 passed\n"])
def test_reviewed_cross_language_judge_rejects_markers_without_test_identities(text):
    assert not _reviewed_observation_checks(
        ["python3", "-m", "pytest", "-vv", NAME], text, 0, complete=True
    )
