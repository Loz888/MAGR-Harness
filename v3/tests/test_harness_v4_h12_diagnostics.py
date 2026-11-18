"""Real bounded Docker diagnostics and inference boundaries, without model calls."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from test_harness_v4_h06 import _broker, _grant, _lease, _request, _workspace

from mgar_v3.harness.broker import issue_lease
from mgar_v3.harness.commands import CommandSession
from mgar_v3.harness.contracts import CancellationToken
from mgar_v3.harness.diagnostics import ResourceSnapshot, failure_diagnosis, merge_snapshot


def test_unavailable_counters_are_not_fabricated_zeroes() -> None:
    snapshot = ResourceSnapshot(sampled_at="first", unavailable=("memory.events",))
    diagnostic = merge_snapshot(None, snapshot)
    assert diagnostic.event_deltas == diagnostic.observed_events == {}
    later = snapshot.model_copy(update={"sampled_at": "second", "memory_events": {"oom_kill": 1}})
    diagnostic = merge_snapshot(diagnostic, later, stop_reason="natural_exit")
    assert diagnostic.event_deltas == {"memory.oom_kill": None}
    assert diagnostic.observed_events == {"memory.oom_kill": 1}


def test_cpu_throttling_is_observed_without_claiming_assertion_causality() -> None:
    first = ResourceSnapshot(sampled_at="first", cpu_stat={"nr_throttled": 2,
                                                         "throttled_usec": 1000})
    last = ResourceSnapshot(sampled_at="last", cpu_stat={"nr_throttled": 9,
                                                       "throttled_usec": 81000})
    diagnostic = merge_snapshot(merge_snapshot(None, first), last, stop_reason="natural_exit")
    assert diagnostic.event_deltas["cpu.nr_throttled"] == 7
    assert diagnostic.event_deltas["cpu.throttled_usec"] == 80000
    receipt = {"state": "failed", "exit_code": 1, "complete": True,
               "diagnostics": diagnostic.model_dump(mode="json")}
    result = failure_diagnosis(receipt, "AssertionError: elapsed < 400\n1 failed")
    assert result["cause"] == "TEST_ASSERTION_OR_COMMAND_FAILURE"
    assert result["certainty"] == "unresolved"


def test_cpu_missing_first_sample_preserves_unknown_delta_and_old_wire() -> None:
    first = ResourceSnapshot(sampled_at="first")
    assert "cpu_stat" not in first.model_dump(mode="json")
    last = ResourceSnapshot(sampled_at="last", cpu_stat={"nr_throttled": 4})
    diagnostic = merge_snapshot(merge_snapshot(None, first), last)
    assert diagnostic.observed_events["cpu.nr_throttled"] == 4
    assert diagnostic.event_deltas["cpu.nr_throttled"] is None


@pytest.mark.parametrize("word", ["SIGKILL", "EMFILE", "out of memory", "Network is unreachable",
                                   "No module named pytest", "resource temporarily unavailable"])
def test_quoted_error_words_in_assertion_failures_are_not_infrastructure(word: str) -> None:
    output = f"E   AssertionError: expected '{word}' but got another message\n1 failed, 2 passed"
    result = failure_diagnosis({"state": "failed", "exit_code": 1, "complete": True}, output)
    assert result["cause"] == "TEST_ASSERTION_OR_COMMAND_FAILURE"
    assert result["legacy_environment_reason"] is None


def test_network_traceback_is_separate_from_dependencies_and_log_completeness() -> None:
    result = failure_diagnosis(
        {"state": "failed", "exit_code": 1, "complete": True},
        "E   OSError: [Errno 101] Network is unreachable\n4 failed, 597 passed",
    )
    assert result["cause"] == "NETWORK_PRECONDITION_UNAVAILABLE"
    assert result["certainty"] == "observed"


@pytest.mark.parametrize("argv,output", [
    (["node", "ava"], "  ✘ test/error.ts exited due to SIGKILL\n18 tests failed"),
    (["go", "test", "./..."], "fmt: /usr/local/go/pkg/tool/linux_amd64/compile: signal: killed"),
])
def test_real_runner_signal_without_resource_evidence_has_unknown_cause(
    argv: list[str], output: str,
) -> None:
    result = failure_diagnosis(
        {"state": "failed", "exit_code": 1, "complete": True, "argv": argv}, output,
    )
    assert result["cause"] == "UNKNOWN_CAUSE"


@pytest.mark.parametrize("state,reason", [("cancelled", "cancelled"), ("timed_out", "timed_out")])
def test_stop_reason_is_distinct_from_signal(state: str, reason: str) -> None:
    result = failure_diagnosis({
        "state": state, "exit_code": 137, "complete": True,
        "diagnostics": {"stop_reason": reason, "observed_events": {}},
    }, "")
    assert result["cause"] == reason.upper()


CASES = {
    "missing": "import missing_env00_dependency",
    "truncated": "import sys;print('x'*10000);sys.exit(1)",
    "pids": (
        "import subprocess,time,sys\nchildren=[]\n"
        "try:\n"
        " while True: children.append(subprocess.Popen(['sleep','10']))\n"
        "except OSError as e: print(type(e).__name__+': '+str(e),flush=True)\n"
        "time.sleep(1)\n"
        "for child in children: child.terminate();child.wait()\n"
        "sys.exit(1)\n"
    ),
    "oom": "import time;time.sleep(.3);x=bytearray(250*1024*1024);time.sleep(2)",
    "cancel": "import time;time.sleep(10)",
    "deadline": "import time;time.sleep(10)",
    "unknown_kill": "import os,signal;os.kill(os.getpid(),signal.SIGKILL)",
}


def test_real_cpu_quota_records_throttling_without_changing_limits(tmp_path: Path) -> None:
    root = _workspace(tmp_path)
    lease = issue_lease(mode="modify_test", actor="owner", workspace_id="ws-h06",
                        repository="example/project", execution_epoch=3,
                        read_paths=("",), write_paths=("",), allowed_commands=("python3",),
                        cpus=0.1, timeout_seconds=30,
                        approved_by="owner", approval_ref="approval:h12-cpu-probe")
    session = CommandSession(_broker(root, lease))
    try:
        handle = session.run(
            _request(("python3", "-c", "import time\nend=time.monotonic()+2\n"
                      "while time.monotonic()<end: pass"), timeout=20),
            _grant(), CancellationToken(),
        )
        result = session.wait(handle.handle_id, timeout=25)
        assert result.exit_code == 0
        assert result.diagnostics is not None
        assert result.diagnostics.observed_events["cpu.nr_throttled"] > 0
        assert result.diagnostics.observed_events["cpu.throttled_usec"] > 0
    finally:
        session.close()


@pytest.mark.parametrize("case", CASES)
def test_real_owned_docker_diagnostics(tmp_path: Path, case: str) -> None:
    root = _workspace(tmp_path)
    lease = _lease(memory_mb=64, pids=8, max_output_bytes=1024 if case == "truncated" else 65536)
    broker = _broker(root, lease)
    session = CommandSession(broker)
    try:
        handle = session.run(
            _request(("python3", "-c", CASES[case]), timeout=.5 if case == "deadline" else 20),
            _grant(), CancellationToken(),
        )
        if case == "cancel":
            final = session.cancel(handle.handle_id)
        else:
            final = session.wait(handle.handle_id, timeout=25)
        assert final.state != "running"
        page = session.read_log(handle.handle_id)
        receipt = {**final.model_dump(mode="json"), "complete": not page.truncated}
        diagnosis = failure_diagnosis(receipt, page.data)
        (tmp_path / "diagnostic-evidence.json").write_text(json.dumps({
            "case": case, "receipt": receipt, "output": page.data, "diagnosis": diagnosis,
        }, indent=2))
        assert final.diagnostics is not None
        assert final.diagnostics.first.container_id
        assert final.diagnostics.last.nofile_soft == 128
        assert final.diagnostics.sample_count >= 2
        if case == "missing":
            assert diagnosis["cause"] == "DEPENDENCY_OR_COLLECTION_UNAVAILABLE"
        elif case == "truncated":
            assert diagnosis["cause"] == "LOG_INCOMPLETE"
        elif case == "pids":
            assert final.diagnostics.observed_events.get("pids.max", 0) > 0
            assert diagnosis["cause"] == "PID_LIMIT_REACHED"
        elif case == "oom":
            assert diagnosis["cause"] == "MEMORY_OOM_KILL"
            assert final.diagnostics.last.oom_killed is True
        elif case == "unknown_kill":
            assert diagnosis["cause"] == "UNKNOWN_CAUSE"
        else:
            assert diagnosis["cause"] == {"cancel": "CANCELLED", "deadline": "TIMED_OUT"}[case]
    finally:
        session.close()
    assert broker.inspect_command(final.diagnostics.first.container_id)[0] == "missing"
