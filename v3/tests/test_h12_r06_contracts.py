"""R06 public policies, private assertion review and generic helper data flow."""

import copy
import hashlib
import importlib.util

import pytest

from mgar_v3.domain import DomainError, digest
from mgar_v3.harness.contracts import EvaluationProfile
from mgar_v3.harness.semantic_types import (
    JudgeTraceabilityManifest,
    SemanticRequirement,
    SemanticRevision,
    SharedHelperSpec,
    TaskBaselinePolicy,
    public_id,
)
from mgar_v3.quality_eval.h12_judge_review import assertion_inventory, review_judge
from mgar_v3.quality_eval.h12_regression_plan import regression_plan
from mgar_v3.quality_eval.h12_runner import EvaluationRunner
from mgar_v3.quality_eval.h12_structural_probe import PROGRAM, freeze_structural_probe

BEHAVIOR = (
    "Preserve Box behavior for mixed-case keys, overwrite, delete, iteration and missing keys."
)
STRUCTURE = (
    "Box __setitem__, __getitem__ and __delitem__ use one shared key canonicalizer; "
    "its name is unrestricted, and its result supplies each mapping key."
)
BASE = """class Box:
 def __init__(self): self.data={}
 def __setitem__(self,key,value): self.data[key.lower()]=(key,value)
 def __getitem__(self,key): return self.data[key.lower()][1]
 def __delitem__(self,key): del self.data[key.lower()]
"""


def requirement(text, kind):
    return SemanticRequirement(
        acceptance_requirement_id=public_id(text), description=text, kind=kind
    )


def refactor_policy():
    spec = SharedHelperSpec(
        acceptance_requirement_id=public_id(STRUCTURE),
        path="box.py",
        owner_class="Box",
        callers=("__setitem__", "__getitem__", "__delitem__"),
    )
    return TaskBaselinePolicy(
        benchmark_revision="r06-public-revision-2",
        task_kind="refactor",
        requirements=(requirement(BEHAVIOR, "behavior"), requirement(STRUCTURE, "structure")),
        behavior_plan=regression_plan([("python3", "-B", "-m", "pytest", "tests/")]),
        shared_helper=spec,
    )


def manifest_for(policy, sources, *, task_id="r06-refactor", bindings=None, commands=None):
    rows = list(assertion_inventory(sources).values())
    return JudgeTraceabilityManifest(
        task_id=task_id,
        benchmark_revision=policy.benchmark_revision,
        public_acceptance_hash=policy.public_acceptance_hash,
        command_hash=digest(commands) if commands is not None else None,
        files={name: hashlib.sha256(data).hexdigest() for name, data in sources.items()},
        assertions=[
            {k: v for k, v in row.items() if k != "named_constraints"}
            | {
                "acceptance_requirement_id": bindings[i]
                if bindings
                else policy.requirements[
                    min(i, len(policy.requirements) - 1)
                ].acceptance_requirement_id
            }
            for i, row in enumerate(rows)
        ],
    )


def helper_source(name="_fold"):
    return (
        BASE.replace("key.lower()", f"self.{name}(key)")
        + f" def {name}(self,key): return key.lower()\n"
    )


def analyze(source, spec):
    module_spec = importlib.util.spec_from_file_location("r06_generic_checker", PROGRAM)
    module = importlib.util.module_from_spec(module_spec)
    module_spec.loader.exec_module(module)
    return module.shared_helper(source, spec.model_dump(mode="json"))


@pytest.mark.parametrize("name", ["_fold", "normalize_key", "_lowerkey", "_canonical_key"])
def test_any_legitimate_helper_name_and_its_actual_return_flow_is_accepted(name):
    policy = refactor_policy()
    assert analyze(helper_source(name), policy.shared_helper)


@pytest.mark.parametrize(
    "variant", ["original", "unused", "identity-inline", "split", "bypass-delete"]
)
def test_unused_or_noncentralized_helper_cannot_pass_structure(variant):
    source = helper_source()
    if variant == "original":
        source = BASE
    elif variant == "unused":
        source = BASE + " def _fold(self,key):return key.casefold()\n"
    elif variant == "identity-inline":
        source = (
            BASE.replace("key.lower()", "self._fold(key.lower())")
            + " def _fold(self,key):return key\n"
        )
    elif variant == "split":
        source = source.replace(
            "return self.data[self._fold(key)]", "return self.data[self.other(key)]"
        )
        source += " def other(self,key):return key.casefold()\n"
    else:
        source = source.replace("del self.data[self._fold(key)]", "del self.data[key.lower()]")
    assert not analyze(source, refactor_policy().shared_helper)


def test_module_helper_and_local_result_alias_are_valid():
    source = BASE.replace("self.data[key.lower()]", "self.data[normal(key)]")
    source = "def normal(k):return k.lower()\n" + source
    assert analyze(source, refactor_policy().shared_helper)
    source = helper_source().replace(
        "return self.data[self._fold(key)][1]",
        "canonical=self._fold(key); return self.data[canonical][1]",
    )
    assert analyze(source, refactor_policy().shared_helper)


@pytest.mark.parametrize("shadow", ["duplicate-class", "rebind", "replace-method"])
def test_shadowed_class_or_replaced_method_cannot_certify_static_helper(shadow):
    source = helper_source()
    if shadow == "duplicate-class":
        source += "\n" + BASE
    elif shadow == "rebind":
        source += "\nBox = dict\n"
    else:
        source += "\nBox.__getitem__ = lambda self,key: self.data[key.lower()][1]\n"
    assert not analyze(source, refactor_policy().shared_helper)


def test_undeclared_specific_helper_name_cannot_enter_public_policy():
    policy = refactor_policy()
    values = policy.model_dump(mode="json")
    values["shared_helper"]["required_name"] = "_canonical_key"
    with pytest.raises(ValueError, match="explicitly public"):
        TaskBaselinePolicy.model_validate(values)


def test_explicitly_public_function_name_is_enforced():
    text = STRUCTURE + " The helper must be named _canonical_key."
    policy = refactor_policy()
    values = policy.model_dump(mode="json")
    values["requirements"][1] = requirement(text, "structure").model_dump(mode="json")
    values["shared_helper"].update(
        acceptance_requirement_id=public_id(text), required_name="_canonical_key"
    )
    bound = TaskBaselinePolicy.model_validate(values)
    assert not analyze(helper_source("_lowerkey"), bound.shared_helper)
    assert analyze(helper_source("_canonical_key"), bound.shared_helper)


def test_public_structural_asset_is_content_frozen_and_not_project_code(tmp_path):
    spec = refactor_policy().shared_helper
    plan = freeze_structural_probe(spec, tmp_path)
    assert freeze_structural_probe(spec, tmp_path) == plan
    target = tmp_path / plan.asset_sha256 / "probe.py"
    assert hashlib.sha256(target.read_bytes()).hexdigest() == plan.asset_sha256
    target.write_text("import os\n")
    with pytest.raises(ValueError):
        freeze_structural_probe(spec, tmp_path)


JUDGE = b"""from box import Box
def test_behavior():
 b=Box(); b['AA']=7
 assert b['aa']==7
def test_public_structure():
 assert callable(Box.__setitem__) and callable(Box.__getitem__) and callable(Box.__delitem__)
"""


def test_complete_assertion_to_public_acceptance_mapping_is_bidirectional():
    policy = refactor_policy()
    sources = {"tests/hidden/reviewed.py": JUDGE}
    manifest = manifest_for(policy, sources)
    result = review_judge(policy, manifest, sources, task_id="r06-refactor")
    assert result["status"] == "PASS" and result["assertion_count"] == 2
    assert all(result["requirement_assertions"].values())
    assert "assert b" not in str(result)


@pytest.mark.parametrize(
    "variant", ["missing", "unknown", "duplicate", "hash", "revision", "uncovered", "extra"]
)
def test_missing_changed_or_unmapped_assertion_blocks_new_freeze(variant):
    policy = refactor_policy()
    sources = {"tests/hidden/reviewed.py": JUDGE}
    values = manifest_for(policy, sources).model_dump(mode="json")
    if variant == "missing":
        values["assertions"].pop()
    elif variant == "unknown":
        values["assertions"][0]["acceptance_requirement_id"] = "public." + "f" * 16
    elif variant == "duplicate":
        values["assertions"].append(copy.deepcopy(values["assertions"][0]))
    elif variant == "hash":
        values["files"]["tests/hidden/reviewed.py"] = "f" * 64
    elif variant == "revision":
        values["benchmark_revision"] = "older"
    elif variant == "uncovered":
        values["assertions"][1]["acceptance_requirement_id"] = policy.requirements[
            0
        ].acceptance_requirement_id
    else:
        sources = {"tests/hidden/reviewed.py": JUDGE + b"\nassert 1==1\n"}
    with pytest.raises(DomainError, match="."):
        review_judge(
            policy,
            JudgeTraceabilityManifest.model_validate(values),
            sources,
            task_id="r06-refactor",
        )


def test_old_undeclared_canonical_key_assertion_is_not_relabelled_as_behavior():
    policy = refactor_policy()
    sources = {
        "tests/hidden/reviewed.py": JUDGE
        + b"\ndef test_name():\n assert hasattr(Box,'_canonical_key')\n"
    }
    manifest = manifest_for(policy, sources)
    with pytest.raises(DomainError, match="symbol name absent"):
        review_judge(policy, manifest, sources, task_id="r06-refactor")


@pytest.mark.parametrize(
    "source",
    [
        b"exec('assert True')",
        b"import subprocess\nsubprocess.run(['go','test'])\nassert True",
        b"value=1",
    ],
)
def test_uninventoried_dynamic_external_or_empty_judge_cannot_be_reviewed(source):
    with pytest.raises(DomainError):
        assertion_inventory({"judge.py": source})


@pytest.mark.parametrize(
    "variant",
    [
        "valid",
        "wrong-kind",
        "missing-review",
        "wrong-version",
        "unpublic-description",
        "forged-asset",
    ],
)
def test_frozen_profile_validates_semantic_bindings_without_changing_legacy_hashes(
    tmp_path, variant
):
    from test_h12_r06_service import profile_for_case

    policy = refactor_policy()
    probe = freeze_structural_probe(policy.shared_helper, tmp_path)
    profile = profile_for_case(policy, probe, "b" * 64, "r06-typed")
    values = profile.model_dump(mode="json")
    if variant == "valid":
        assert EvaluationProfile.model_validate(values) == profile
        legacy = profile.model_copy(
            update={
                "baseline_policy": None,
                "structural_probe": None,
                "judge_traceability_sha256": None,
                "judge_registry_sha256": None,
            }
        )
        assert (
            not {
                "baseline_policy",
                "structural_probe",
                "judge_traceability_sha256",
                "judge_registry_sha256",
            }
            & legacy.model_dump(mode="json").keys()
        )
        return
    if variant == "wrong-kind":
        values["task_kind"] = "feature"
    elif variant == "missing-review":
        values["judge_traceability_sha256"] = None
    elif variant == "wrong-version":
        values["acceptance_revision"] = "legacy-v1"
    elif variant == "unpublic-description":
        values["public_requirements"] = []
    else:
        values["structural_probe"]["asset_sha256"] = "c" * 64
    with pytest.raises(ValueError):
        EvaluationProfile.model_validate(values)


def test_evaluator_cannot_narrow_declared_regression_when_binding_new_semantics(tmp_path):
    from test_h12_r06_service import profile_for_case

    policy = refactor_policy()
    probe = freeze_structural_probe(policy.shared_helper, tmp_path)
    profile = profile_for_case(policy, probe, "b" * 64, "r06-scope")
    revision = SemanticRevision(
        policy=policy,
        traceability_path=tmp_path / "private-review.json",
        traceability_sha256="b" * 64,
        registry_path=tmp_path / "private-registry.json",
        registry_sha256="c" * 64,
        structural_probe=probe,
    )
    evaluator = object.__new__(EvaluationRunner)
    evaluator.semantic_revisions = {profile.task_id: revision}
    evaluator.environment_revision = None
    original = profile.model_copy(
        update={
            "baseline_policy": None,
            "structural_probe": None,
            "judge_traceability_sha256": None,
            "judge_registry_sha256": None,
            "acceptance_revision": "harness-v4-scoring-v1",
        }
    )
    task = {
        "task_id": profile.task_id,
        "repository": "toy/scope",
        "acceptance": {"regression": ["python3 -B -m pytest tests/"]},
    }
    assert evaluator._bind_semantic_revision(task, original) == profile
    task["acceptance"]["regression"] = ["python3 -B -m pytest tests/ other_tests/"]
    with pytest.raises(ValueError, match="full regression"):
        evaluator._bind_semantic_revision(task, original)


@pytest.mark.parametrize(
    "variant", ["valid", "subset", "other-file", "fake-adapter", "changed-command"]
)
def test_review_freezes_all_private_sources_and_exact_executor_plan(variant):
    policy = refactor_policy()
    filename = "tests/hidden/reviewed.py"
    sources = {filename: JUDGE}
    commands = [["python3", "-m", "pytest", "-q", filename]]
    manifest = manifest_for(policy, sources, commands=commands)
    if variant == "subset":
        commands[0] += ["-k", "test_behavior"]
    elif variant == "other-file":
        commands[0][-1] = "tests/hidden/other.py"
    elif variant == "fake-adapter":
        commands = [["python3", "-c", "print('2 passed')", filename]]
    elif variant == "changed-command":
        commands[0].append("--ignore=" + filename)
    if variant == "valid":
        assert review_judge(policy, manifest, sources, task_id="r06-refactor", commands=commands)[
            "execution_plan_verified"
        ]
    else:
        # Even a caller that recomputes the command hash may not filter frozen assertions.
        if variant != "changed-command":
            manifest = manifest.model_copy(update={"command_hash": digest(commands)})
        with pytest.raises(DomainError):
            review_judge(policy, manifest, sources, task_id="r06-refactor", commands=commands)
