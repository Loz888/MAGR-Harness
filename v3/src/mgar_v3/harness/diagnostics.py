"""Bounded, observational process diagnostics; never an execution capability.

Kernel counters and Docker's OOM flag are evidence. Error words in a project's
output are only symptoms, and cannot establish a kernel resource limit.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any, Literal

from pydantic import Field

from mgar_v3.domain import Model


class ResourceSnapshot(Model):
    sampled_at: str
    container_id: str = ""
    container_state: str = "unavailable"
    exit_code: int | None = None
    oom_killed: bool | None = None
    memory_events: dict[str, int] | None = None
    pids_events: dict[str, int] | None = None
    cpu_stat: dict[str, int] | None = Field(default=None, exclude_if=lambda value: value is None)
    values: dict[str, int] = Field(default_factory=dict)
    nofile_soft: int | None = None
    nofile_hard: int | None = None
    unavailable: tuple[str, ...] = ()


class CommandDiagnostics(Model):
    schema_version: Literal["harness-command-diagnostics-v1"] = "harness-command-diagnostics-v1"
    stop_reason: Literal["running", "natural_exit", "timed_out", "cancelled", "revoked", "missing"]
    first: ResourceSnapshot
    last: ResourceSnapshot
    sample_count: int = Field(ge=1)
    peaks: dict[str, int] = Field(default_factory=dict)
    event_deltas: dict[str, int | None] = Field(default_factory=dict)
    observed_events: dict[str, int] = Field(default_factory=dict)


def merge_snapshot(
    previous: CommandDiagnostics | None,
    snapshot: ResourceSnapshot,
    *,
    stop_reason: str = "running",
) -> CommandDiagnostics:
    reason: Any = stop_reason
    if previous is None:
        first, count, peaks, events = snapshot, 1, {}, {}
    else:
        first, count = previous.first, previous.sample_count + 1
        peaks, events = dict(previous.peaks), dict(previous.observed_events)
    for key, value in snapshot.values.items():
        peaks[key] = max(peaks.get(key, 0), value)
    deltas: dict[str, int | None] = dict(previous.event_deltas) if previous else {}
    for prefix, attribute in (("memory", "memory_events"), ("pids", "pids_events"),
                              ("cpu", "cpu_stat")):
        current = getattr(snapshot, attribute)
        initial = getattr(first, attribute)
        for key, value in (current or {}).items():
            label = prefix + "." + key
            events[label] = max(events.get(label, 0), value)
            deltas[label] = (
                max(0, value - initial[key]) if initial is not None and key in initial else None
            )
    return CommandDiagnostics(
        stop_reason=reason,
        first=first,
        last=snapshot,
        sample_count=count,
        peaks=peaks,
        event_deltas=deltas,
        observed_events=events,
    )


def failure_diagnosis(receipt: Mapping[str, Any], output: str) -> dict[str, Any]:
    """Return evidence and certainty separately from the legacy outcome reason.

    A complete assertion failure with quoted error text must remain an assertion
    failure. Only terminal process errors or real traceback exception records
    qualify as runtime symptoms; SIGKILL by itself has no known system cause.
    """
    evidence: list[str] = []
    result: dict[str, Any] = {
        "schema_version": "harness-failure-diagnosis-v1",
        "cause": "TEST_ASSERTION_OR_COMMAND_FAILURE",
        "certainty": "unresolved",
        "evidence": evidence,
        "legacy_environment_reason": None,
    }
    failed = receipt.get("state") != "exited" or receipt.get("exit_code") != 0
    if not failed:
        result.update(cause="NONE", certainty="confirmed")
        return result
    if not receipt.get("complete", False):
        result.update(cause="LOG_INCOMPLETE", certainty="confirmed",
                      legacy_environment_reason="DEPENDENCY_OR_LOG_UNAVAILABLE")
        evidence.append("receipt.complete=false")
        return result
    diagnostic = receipt.get("diagnostics")
    if isinstance(diagnostic, dict):
        stop = diagnostic.get("stop_reason")
        if stop in {"timed_out", "cancelled", "revoked", "missing"}:
            result.update(cause=str(stop).upper(), certainty="confirmed",
                          legacy_environment_reason="SANDBOX_RESOURCE_UNAVAILABLE")
            evidence.append("diagnostics.stop_reason=" + str(stop))
            return result
        events = diagnostic.get("observed_events", {})
        last = diagnostic.get("last", {})
        if isinstance(events, dict) and (
            events.get("memory.oom_kill", 0) > 0
            or isinstance(last, dict) and last.get("oom_killed") is True
        ):
            result.update(cause="MEMORY_OOM_KILL", certainty="confirmed",
                          legacy_environment_reason="SANDBOX_RESOURCE_UNAVAILABLE")
            evidence.append("owned cgroup memory.oom_kill or Docker OOMKilled")
            return result
        if isinstance(events, dict) and events.get("pids.max", 0) > 0:
            result.update(cause="PID_LIMIT_REACHED", certainty="confirmed",
                          legacy_environment_reason="SANDBOX_RESOURCE_UNAVAILABLE")
            evidence.append("owned cgroup pids.events:max")
            return result
    argv = receipt.get("argv", [])
    if (
        isinstance(argv, (list, tuple))
        and ("/node_modules/ava/entrypoints/cli.mjs" in argv or list(argv[:2]) == ["npm", "test"])
        and re.search(r"^\s*Error: (?:ENOENT|EROFS|EACCES):[^\n]*mkdir "
                      r"'/workspace/node_modules/\.cache/ava'", output, re.MULTILINE)
        and re.search(r"^\s+at Api\._createCacheDir \((?:file://)?/node_modules/ava/lib/"
                      r"api\.js:\d+:\d+\)", output, re.MULTILINE)
    ):
        result.update(cause="TEST_RUNNER_CACHE_UNAVAILABLE", certainty="observed",
                      legacy_environment_reason="DEPENDENCY_OR_LOG_UNAVAILABLE")
        evidence.append("AVA cache creation exception and runner stack frame")
        return result
    # Keep tests' expected/quoted exceptions out of infrastructure inference.
    summary = bool(re.search(
        r"^(?:=+\s*)?\s*\d+\s+(?:passed|failed|passing|failing|tests? failed)(?:[,\s]|$)",
        output, re.MULTILINE,
    ))
    lines = output.splitlines()
    traceback = [line.strip() for line in lines if re.match(r"^E\s+\w", line)]
    terminal = [] if summary else [line.strip() for line in lines]
    candidates = traceback + terminal
    for line in candidates:
        if re.search(r"AssertionError|(?:expected|got)\s*[:=]", line, re.IGNORECASE):
            continue
        if re.search(r"(?:OSError|ConnectionError|RequestError|NewConnectionError).*"
                     r"(?:Network is unreachable|ENETUNREACH)", line, re.IGNORECASE):
            result.update(cause="NETWORK_PRECONDITION_UNAVAILABLE", certainty="observed",
                          legacy_environment_reason="DEPENDENCY_OR_LOG_UNAVAILABLE")
            evidence.append("exception record: network unreachable")
            return result
        if re.match(r"(?:E\s+)?(?:ModuleNotFoundError:|ImportError:|No module named|"
                    r"Error: Cannot find (?:module|package)|ERROR collecting|"
                    r"fixture ['\"][^'\"]+['\"] not found|go: downloading|"
                    r".*command not found$)", line):
            result.update(cause="DEPENDENCY_OR_COLLECTION_UNAVAILABLE", certainty="observed",
                          legacy_environment_reason="DEPENDENCY_OR_LOG_UNAVAILABLE")
            evidence.append("terminal dependency/collection error")
            return result
        if re.match(r"(?:E\s+)?(?:Error: EMFILE|OSError:.*(?:Too many open files|"
                    r"Resource temporarily unavailable)|compile: resource temporarily unavailable)",
                    line, re.IGNORECASE):
            result.update(cause="RESOURCE_ERROR_OBSERVED", certainty="observed",
                          legacy_environment_reason="SANDBOX_RESOURCE_UNAVAILABLE")
            evidence.append("terminal resource error; kernel cause not established")
            return result
    killed_runner = re.search(
        r"^\s*(?:✘\s+)?(?:test/)?\S+\.(?:[cm]?js|ts|py) exited due to SIGKILL\s*$",
        output, re.MULTILINE,
    )
    argv = receipt.get("argv", ())
    killed_compiler = (
        isinstance(argv, (tuple, list)) and bool(argv) and argv[0] == "go"
        and re.search(r"^.*\bcompile: signal: killed\s*$", output, re.MULTILINE)
    )
    if receipt.get("exit_code") in {137, -9} or killed_runner or killed_compiler:
        result.update(cause="UNKNOWN_CAUSE", certainty="unresolved",
                      legacy_environment_reason="SANDBOX_RESOURCE_UNAVAILABLE")
        evidence.append("SIGKILL/137 without kernel cause evidence")
    return result
