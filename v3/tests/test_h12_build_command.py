"""Fixed compilation is separate from tests; real PG/Docker and free models."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
from pydantic import ValidationError
from test_h12_public_probe_service import BoundedScriptedModel
from test_harness_v4_h10 import _call, _stop

from mgar_v3.coordination.engine import Engine
from mgar_v3.evidence.artifacts import ArtifactStore
from mgar_v3.harness.adapters import LocalVerifier
from mgar_v3.harness.commands import CommandSession
from mgar_v3.harness.contracts import (
    EvaluationProfile,
    EvaluationResources,
    ExecutionReceipt,
    TaskBinding,
    VerificationRequest,
    VerificationRequirement,
    WorkspaceSnapshot,
    canonical_hash,
    execution_matches,
    sha256_text,
)
from mgar_v3.harness.model import DEEPSEEK_CHAT
from mgar_v3.harness.service import (
    CodingTaskRequest,
    HarnessCodingService,
    WorkspaceBoundBroker,
    _build_status,
    _resolve_test_command,
    _test_summary,
)

ROOT = Path(__file__).resolve().parents[1]
LOCK = ROOT / ".local/h12/sandbox-lock.json"
TEST = ("python3", "-B", "-m", "unittest", "-q", "test_public")
BUILD = ("node", "--check", "source.js")
BEFORE = "function answer() { return 1; }\n"
AFTER = "function answer() { return 2; }\n"


def profile(**changes):
    values = dict(
        experiment_id="build-service-" + uuid4().hex[:12],
        task_id="synthetic-build-dev-0",
        split="dev",
        arm="candidate",
        task_kind="feature",
        objective="Update the exported answer, compile JavaScript and preserve public tests.",
        resources=EvaluationResources(
            max_model_calls=12,
            max_tool_calls=60,
            max_cost_micros=1_000_000,
            deadline_seconds=600,
            max_context_tokens=32768,
            max_repair_rounds=6,
            max_output_tokens=2048,
        ),
        model=DEEPSEEK_CHAT.model_dump(mode="json"),
        write_paths=("source.js",),
        public_test_argv=TEST,
        public_build_argv=BUILD,
    )
    return EvaluationProfile(**{**values, **changes})


def test_empty_build_field_preserves_legacy_serialization_and_roundtrip_hash():
    old = profile(public_build_argv=())
    payload = old.model_dump(mode="json")
    assert "public_build_argv" not in payload
    assert canonical_hash(EvaluationProfile.model_validate(payload).model_dump(mode="json")) == (
        canonical_hash(payload)
    )
    assert profile().model_dump(mode="json")["public_build_argv"] == list(BUILD)


@pytest.mark.parametrize("argv", [("",), ("node", "bad\nargument"), ("node", "bad\x00argument")])
def test_build_argv_has_same_bounded_command_validation(argv):
    with pytest.raises(ValidationError):
        profile(public_build_argv=argv)


def receipt(**changes):
    return ExecutionReceipt(
        **{
            "receipt_id": "a" * 64,
            "operation_id": "compile",
            "purpose": "build",
            "status": "passed",
            "argv": BUILD,
            "cwd": ".",
            "exit_code": 0,
            "stdout": "999 passed in 0.01s\n",
            "stderr": "",
            "duration_seconds": 0.1,
            "tree_hash": "b" * 64,
            "log_sha256": sha256_text("999 passed in 0.01s\n"),
            "environment_hash": "c" * 64,
            "tests_run": 0,
            "tests_failed": 0,
            **changes,
        }
    )


def task(requirements):
    return TaskBinding(
        task_id="build-test",
        thread_id=uuid4(),
        run_id=uuid4(),
        execution_epoch=1,
        actor="owner",
        repository="local/build",
        base_commit="0" * 40,
        workspace_id="build-workspace",
        acceptance_revision="build-v1",
        objective="Compile and test independently",
        task_kind="feature",
        requirements=requirements,
    )


def test_build_exit_zero_never_impersonates_tests_even_with_test_like_output():
    requirements = (
        VerificationRequirement(requirement_id="suite", command_argv=TEST),
        VerificationRequirement(requirement_id="build", kind="build", command_argv=BUILD),
    )
    bound = task(requirements)
    raw = receipt()
    result = LocalVerifier().verify(
        VerificationRequest(
            task=bound,
            snapshot=WorkspaceSnapshot(workspace_id=bound.workspace_id, tree_hash="b" * 64),
            receipts=(raw,),
        )
    )
    assert {check.requirement_id: check.status for check in result.checks} == {
        "suite": "missing",
        "build": "passed",
    }
    assert _build_status(0)[1:4] == (0, 0, 0)
    assert (
        _test_summary(SimpleNamespace(task_kind="feature", receipts=[raw]))["status"] == "not_run"
    )
    assert not execution_matches(bound, requirements[0], receipt(argv=TEST, tests_run=999))
    assert not execution_matches(bound, requirements[1], receipt(purpose="test"))
    # Historical requirements without exact argv keep their former matching.
    assert execution_matches(
        bound, VerificationRequirement(requirement_id="old", kind="build"), receipt(purpose="test")
    )
    assert _resolve_test_command({"command_id": "build"}, TEST, None, BUILD) == BUILD
    assert _resolve_test_command({"argv": list(BUILD)}, TEST, None, BUILD) is None
    assert (
        _resolve_test_command({"command_id": "build", "argv": list(BUILD)}, TEST, None, BUILD)
        is None
    )


@dataclass
class Case:
    engine: object
    source: Path
    profile: EvaluationProfile
    model: BoundedScriptedModel
    candidates: list = field(default_factory=list)

    def service(self, engine=None, **kwargs):
        def gate(workspace, candidate):
            del workspace
            self.candidates.append(candidate)
            return {"status": "NOT_RUN", "tree_hash": candidate["tree_after"]}

        selected = engine or self.engine
        return HarnessCodingService(
            selected.settings.harness_root,
            actor="owner",
            image_lock=LOCK,
            engine=selected,
            allow_paid=True,
            live_api_key="controlled-do-not-send",
            evaluation_profile=self.profile,
            model_factory=lambda request: self.model,
            final_gate=gate,
            **kwargs,
        )

    def request(self, service):
        project = service.register_project(str(self.source))
        return CodingTaskRequest(
            project_id=project["project_id"],
            objective=self.profile.objective,
            task_kind="feature",
            mode="live",
            approved=True,
        )


def case(engine, tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "source.js").write_text(BEFORE)
    (source / "test_public.py").write_text(
        "import unittest\nfrom pathlib import Path\nclass Public(unittest.TestCase):\n"
        " def test_interface(self):\n"
        "  self.assertIn('function answer()', Path('source.js').read_text())\n"
    )
    frozen = profile()
    matrix = [
        frozen.model_copy(update={"task_id": f"synthetic-build-dev-{i}", "arm": arm})
        for i in range(12)
        for arm in ("candidate", "legacy-v35")
    ]
    engine.harness.create_experiment(
        frozen.experiment_id,
        {
            "schema": "harness-h12-experiment-v1",
            "environment_revision": "h12-dev-environment-v4",
            "profiles": [p.model_dump(mode="json") for p in matrix],
        },
        {"approved_by": "owner", "allow_paid": True, "max_cost_micros": 12_000_000},
    )
    return Case(
        engine,
        source,
        frozen,
        BoundedScriptedModel(
            [
                lambda request: _call(
                    "apply_edits",
                    {
                        "expected_tree": request.workspace_tree,
                        "edits": [
                            {
                                "path": "source.js",
                                "expected_sha256": sha256_text(BEFORE),
                                "replacements": [{"old_text": BEFORE, "new_text": AFTER}],
                            }
                        ],
                    },
                ),
                _call("run_tests", {"command_id": "build"}),
                _call("run_tests", {"command_id": "tests"}),
                _stop(
                    "Updated the exported answer and completed both compile and test verification."
                ),
            ]
        ),
    )


def assert_candidate(current, result):
    assert result["independent_gate"]["status"] == "NOT_RUN"
    assert current.candidates
    candidate = current.candidates[-1]
    assert candidate["status"] == "completed", candidate
    builds = [r for r in candidate["receipts"] if r["purpose"] == "build"]
    suites = [r for r in candidate["receipts"] if r["purpose"] == "test"]
    assert len(builds) == 1 and builds[0]["status"] == "passed"
    assert builds[0]["tests_run"] == builds[0]["tests_failed"] == 0
    assert tuple(builds[0]["argv"]) == BUILD and builds[0]["tree_hash"] == candidate["tree_after"]
    assert len(suites) == 2 and all(r["status"] == "passed" for r in suites)
    assert suites[-1]["tests_run"] == 1
    assert {item["kind"] for item in candidate["checklist"]["items"]} >= {"build", "behavior"}
    return candidate, builds[0]


def test_real_pg_and_docker_require_both_build_and_tests(engine, tmp_path):
    current = case(engine, tmp_path)
    service = current.service()
    result = service.create_task(current.request(service))
    candidate, _ = assert_candidate(current, result)
    row = engine.harness.get(result["task_id"])
    assert tuple(row["grant_scope"]["allowed_commands"]) == ("python3", "node")
    assert row["request"]["trusted_build_argv"] == list(BUILD)
    assert row["evaluation_profile"]["public_build_argv"] == list(BUILD)
    assert candidate["tool_calls"] == 4  # original test, edit, compile, final test
    assert result["tests"]["run"] == 1


def test_known_build_recovers_with_original_purpose_and_no_relaunch(engine, tmp_path, monkeypatch):
    from test_h12_budget_context import working_state

    current = case(engine, tmp_path)

    def test_after_recovered_build(request):
        items = working_state(request)["checklist"]["items"]
        assert all(item["status"] == "passed" for item in items if item["kind"] == "build")
        assert any(item["status"] != "passed" for item in items if item["kind"] == "behavior")
        return _call("run_tests", {"command_id": "tests"})

    current.model.steps[2] = test_after_recovered_build
    launches = []
    launch = WorkspaceBoundBroker.launch

    def tracked(broker, request, *args, **kwargs):
        launches.append((request.operation_id, tuple(request.argv), request.purpose))
        return launch(broker, request, *args, **kwargs)

    monkeypatch.setattr(WorkspaceBoundBroker, "launch", tracked)
    monkeypatch.setattr(CommandSession, "_monitor", lambda *args: None)
    close = CommandSession.close
    interrupted = []
    monkeypatch.setattr(CommandSession, "close", lambda self: 0 if interrupted else close(self))
    recorded = []

    def crash(point, payload):
        del payload
        if point == "command.after_receipt":
            recorded.append(point)
            if len(recorded) == 2:
                interrupted.append(True)
                raise SystemExit("known compile receipt before outer tool receipt")

    service = current.service(fault_hook=crash)
    with pytest.raises(SystemExit, match="known compile receipt"):
        service.create_task(current.request(service))
    row = engine.harness.tasks()[0]
    run_id, origin_epoch = row["run_id"], row["epoch"]
    assert Counter(argv for _, argv, _ in launches) == {TEST: 1, BUILD: 1}
    interrupted.clear()
    engine.close()
    replacement = Engine(engine.settings, ArtifactStore(engine.settings.artifact_root))
    try:
        replacement.recover()
        result = current.service(engine=replacement).recover_task(row["task_id"])
        candidate, build = assert_candidate(current, result)
        resumed = replacement.harness.get(row["task_id"])
        assert resumed["run_id"] == run_id and resumed["epoch"] > origin_epoch
        assert build["provenance"]["execution_epoch"] == origin_epoch
        assert build["purpose"] == "build" and build["tests_run"] == 0
        assert Counter(argv for _, argv, _ in launches) == {TEST: 2, BUILD: 1}
        assert len({operation for operation, _, _ in launches}) == 3
        assert candidate["tool_calls"] == 4
    finally:
        replacement.close()
