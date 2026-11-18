"""Build gates preserve genuine bug reproduction without requiring an unrelated red build."""

from test_harness_public_probe import task
from test_public_probe_verification import requirements

from mgar_v3.harness.coding_loop import CodingLoop
from mgar_v3.harness.contracts import VerificationRequirement


def checklist(*items):
    bound = task(requirements=tuple(items))
    return CodingLoop._base_items(None, bound)


def test_supplemental_build_does_not_require_a_green_original_compile_to_fail():
    test = VerificationRequirement(requirement_id="tests", command_argv=("python3", "tests.py"))
    build = VerificationRequirement(
        requirement_id="build", kind="build", command_argv=("compiler", "check")
    )
    items = checklist(test, build)
    assert [(item.kind, item.requirement_id) for item in items] == [
        ("reproduction", "tests"),
        ("regression", "tests"),
        ("build", "build"),
    ]


def test_compile_only_bugfix_still_requires_original_build_failure():
    build = VerificationRequirement(requirement_id="build", kind="build")
    assert [item.kind for item in checklist(build)] == ["reproduction", "build"]


def test_public_probe_build_is_a_separate_required_gate():
    build = VerificationRequirement(
        requirement_id="build",
        kind="build",
        acceptance_role="regression",
        command_argv=("compiler", "check"),
    )
    items = checklist(*requirements(), build)
    assert sum(item.kind == "reproduction" for item in items) == 1
    found = [item for item in items if item.requirement_id == "build"]
    assert len(found) == 1 and found[0].kind == "build" and found[0].required
