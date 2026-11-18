"""New public plans cannot mutate old freezes or bypass original authority."""

import pytest
from test_h12_r07_service import profile
from test_harness_v4_h07 import BUGGY, TEST_CALC, ScriptedModel, _grant, _loop, _seed, _task

from mgar_v3.domain import DomainError
from mgar_v3.harness.contracts import EvaluationProfile
from mgar_v3.harness.public_feedback import PublicFeedbackPolicy
from mgar_v3.harness.service import _resolve_test_command
from mgar_v3.quality_eval.h12_regression_plan import regression_plan
from mgar_v3.quality_eval.h12_runner import EvaluationRunner


@pytest.mark.parametrize("change", ["no-contract", "no-probe", "unconfigured", "cwd", "executable"])
def test_opt_in_public_feedback_rejects_unbound_or_ungranted_plan(change):
    value = profile().model_dump(mode="json")
    if change == "no-contract":
        value["acceptance_protocol"] = "legacy-v1"
    elif change == "no-probe":
        value["verification_protocol"] = "receipt-v1"
        value["public_probe"] = None
    else:
        command = value["independent_regression_plan"]["commands"][0]
        if change == "unconfigured":
            command["framework"] = "unconfigured"
        if change == "cwd":
            command["cwd"] = "another"
        if change == "executable":
            command["argv"][0] = "sh"
    with pytest.raises(ValueError):
        EvaluationProfile.model_validate(value)


def test_command_ids_resolve_exact_frozen_regression_without_accepting_arbitrary_argv():
    plan = regression_plan([("python3", "-m", "pytest", "tests/")])
    trusted = ("python3", "-m", "pytest", "tests/target.py")
    assert _resolve_test_command({"command_id": "regression_1"}, trusted, feedback_plan=plan) == (
        plan.commands[0].argv
    )
    assert (
        _resolve_test_command({"command_id": "regression_2"}, trusted, feedback_plan=plan) is None
    )
    assert (
        _resolve_test_command({"argv": list(plan.commands[0].argv)}, trusted, feedback_plan=plan)
        is None
    )
    assert _resolve_test_command({"command_id": "regression_1"}, trusted) is None


def test_old_profile_serialization_and_unsigned_task_plan_do_not_silently_enable_r07(tmp_path):
    new = profile()
    legacy = new.model_copy(update={"public_feedback_policy": None})
    assert "public_feedback_policy" not in legacy.model_dump(mode="json")
    evaluator = EvaluationRunner.__new__(EvaluationRunner)
    evaluator.public_feedback_policy = PublicFeedbackPolicy()
    with pytest.raises(Exception, match="R06-reviewed"):
        evaluator._bind_public_feedback(legacy)
    loop, _, _ = _loop(
        _seed(tmp_path, {"calc.py": BUGGY, "test_calc.py": TEST_CALC}), ScriptedModel([])
    )
    task = _task("bugfix")
    plan = loop.plan(task)
    assert "public_feedback_policy" not in plan.model_dump(mode="json")
    loop.public_feedback_policy = PublicFeedbackPolicy()
    with pytest.raises(DomainError, match="Frozen feedback policy"):
        loop.run(task, _grant()[0], plan)
