"""A real child can exit between reading a log prefix and observing its PID."""

import time

import pytest
from test_h12_r04_runtime import runtime
from test_harness_v4_h06 import _grant

from mgar_v3.harness.contracts import CancellationToken
from mgar_v3.harness.lifecycle_types import EntryPointSpec


@pytest.mark.parametrize(
    "source,delivery,expected",
    [
        pytest.param("VALUE = 7\n", "normal", "PASS", id="completed-prefix-race"),
        pytest.param(
            "raise RuntimeError('entry cannot load')\n", "normal", "FAIL", id="load-error",
        ),
        pytest.param("VALUE = 7\n", "missing", "FAIL", id="missing-load-observation"),
        pytest.param("VALUE = 7\n", "delayed", "PASS", id="exit-before-load-observation"),
    ],
)
def test_real_exit_snapshot_requires_finished_log(
    tmp_path, monkeypatch, source, delivery, expected,
):
    runner, session, broker = runtime(tmp_path, {"app.py": source})
    original_wire = runner.wire
    original_snapshot = session.runtime_snapshot
    state = {"prefix_returned": False, "snapshot_waited": False, "delayed": False, "reads": 0}

    def earlier_log_prefix(handle):
        output, frames, complete = original_wire(handle)
        deadline = time.monotonic() + 2
        while not any(f.get("kind") == "launch" for f in frames):
            assert time.monotonic() < deadline, "Real supervisor did not publish its child PID"
            time.sleep(0.01)
            output, frames, complete = original_wire(handle)
        state["reads"] += 1
        if not state["prefix_returned"]:
            state["prefix_returned"] = True
            # This is an actual earlier prefix, not a fabricated success frame.
            return "", [f for f in frames if f.get("kind") == "launch"], complete
        if delivery == "delayed" and not state["delayed"]:
            # The control pipe and waitid() can be observed on different turns.
            state["delayed"] = any(frame.get("kind") == "exit" for frame in frames)
            frames = [f for f in frames if f.get("kind") != "entry"]
        elif delivery == "missing":
            frames = [f for f in frames if f.get("kind") != "entry"]
        return output, frames, complete

    def exit_after_prefix(handle, **kwargs):
        snapshot = original_snapshot(handle, **kwargs)
        if state["prefix_returned"] and not state["snapshot_waited"]:
            state["snapshot_waited"] = True
            deadline = time.monotonic() + 2
            while not any(p.state == "Z" for p in snapshot.processes):
                assert time.monotonic() < deadline, "Real child did not exit"
                time.sleep(0.01)
                snapshot = original_snapshot(handle, **kwargs)
        return snapshot

    monkeypatch.setattr(runner, "wire", earlier_log_prefix)
    monkeypatch.setattr(session, "runtime_snapshot", exit_after_prefix)
    tree = broker.workspace_tree_hash()
    try:
        receipt = runner.entry(
            EntryPointSpec(path="app.py", runtime="python", no_listen_on_import=True),
            ("python3", "-B", "-m", "unittest"),
            _grant(),
            "r08-entry-prefix-race",
            tree,
            CancellationToken(),
        )
        assert receipt.status == expected, receipt.model_dump()
        if delivery == "delayed":
            assert state["delayed"]
        assert receipt.detail.strip(), "Failed entry checks must explain the observed mismatch"
        assert state["reads"] >= 2, "An exited PID does not complete a previously read log prefix"
        assert receipt.cleanup.processes_exited is True
        assert receipt.cleanup.port_released is True
        assert receipt.cleanup.container_removed is True
        assert not receipt.cleanup.orphan_pids
        assert receipt.tree_hash == broker.workspace_tree_hash() == tree
    finally:
        session.close()
