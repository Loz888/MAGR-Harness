"""Overlapping source pages are cached evidence, not endless new investigation."""

import hashlib
import json
from uuid import uuid4

from test_analysis_streaming import automatic, run
from test_v32_repository_analysis import FakeStore, runtime, spec

from mgar_v3.domain import DomainError
from mgar_v3.evidence.artifacts import ArtifactStore
from mgar_v3.repository_analysis.agent import RepoAnalysisAgent, initial_state
from mgar_v3.repository_analysis.domain import AgentDecision
from mgar_v3.repository_analysis.tools import ToolOutcome


class SourcePages:
    def __init__(self, count=200, width=28):
        self.content = "".join(f"line {index:04d}: " + "x" * width + "\n"
                               for index in range(1, count + 1))
        self.reads = []

    async def execute(self, call):
        if call.tool == "list_directory":
            return ToolOutcome(tool="list_directory", reference="directory:/",
                               text=json.dumps({"entries": []}))
        self.reads.append(call.model_dump())
        lines = self.content.splitlines(keepends=True)
        text = self.content
        if call.start_line is not None:
            last = min(call.end_line, len(lines))
            text = (f"(lines {call.start_line}-{last} of {len(lines)}; "
                    "quote only from these lines)\n"
                    + "".join(lines[call.start_line - 1:last]))
        return ToolOutcome(
            tool="read_file", reference="github://octo/example@" + "a" * 40 + "/src/main.py",
            text=text, content=self.content.encode(), source_version="source-blob",
            evidence_kind="file",
        )


def test_overlap_headers_do_not_extend_a_read_loop_or_create_duplicate_evidence():
    store, tools = FakeStore(spec(automatic())), SourcePages(count=50)

    class Model:
        calls = 0

        def complete(self, *args, **kwargs):
            self.calls += 1
            spans = [(None, None), (1, 49), (2, 50), (4, 38), (8, 22), (9, 19)]
            start, end = spans[min(self.calls - 1, len(spans) - 1)]
            return AgentDecision(
                action="tool", thought="Inspect an overlapping section.",
                tool_calls=[{"tool": "read_file", "path": "src/main.py",
                             "start_line": start, "end_line": end}],
            )

    model = Model()
    run(store, model, tools)
    assert len(tools.reads) == 1
    assert len(store.evidence) == 1
    assert 5 < model.calls < 25
    assert store.partial[1] == "INPUT_REQUIRED" and store.success is None


def test_new_source_pages_advance_without_refetching_the_same_fixed_blob():
    store, tools = FakeStore(spec(automatic())), SourcePages()
    agent = RepoAnalysisAgent(runtime(store, None, tools))
    state = initial_state(store.value)
    for round_no, (start, end) in enumerate([(1, 50), (40, 90), (91, 140)], 1):
        state = agent.tools({
            **state, "decision_round": round_no,
            "pending_calls": [{"tool": "read_file", "path": "src/main.py",
                               "start_line": start, "end_line": end}],
        })
        assert state["no_progress_rounds"] == 0
        assert not state.get("stop_reason")
    assert len(tools.reads) == 1 and len(store.evidence) == 1
    observations = [item for item in state["history"] if item["tool"] == "read_file"]
    assert len({item["evidence_id"] for item in observations}) == 1
    assert observations[-1]["locator"] == {"start_line": 91, "end_line": 140}
    assert observations[-1]["artifact_hash"] == hashlib.sha256(tools.content.encode()).hexdigest()
    assert "line 0140:" in observations[-1]["content"]
    state["history"] += [
        {"tool": "decision", "reference": "", "content": f"Later investigation {n}. " * 40}
        for n in range(20)
    ]
    payload = agent._payload(store.value, state, 8, max_tokens=4096, recent_window=2)
    protected = payload["verified_evidence"]
    assert {item["evidence_id"] for item in protected} == {observations[0]["evidence_id"]}
    assert all(item["artifact_hash"] == observations[0]["artifact_hash"] for item in protected)
    assert payload["permissions"]["commit"] == store.value.commit


def test_pagination_reports_actual_visible_lines_and_original_file_length():
    store, tools = FakeStore(spec(automatic())), SourcePages(count=1000, width=90)
    agent = RepoAnalysisAgent(runtime(store, None, tools))
    state = agent.tools({
        **initial_state(store.value),
        "pending_calls": [{"tool": "read_file", "path": "src/main.py",
                           "start_line": 120, "end_line": 300}],
    })
    observation = state["history"][-1]
    assert observation["truncated"]
    assert observation["file_total_lines"] == 1000
    last = observation["locator"]["end_line"]
    assert 120 <= last < 300
    assert "1000 lines" in observation["content"]
    assert f'"start_line":{last + 1}' in observation["content"]
    assert "line 0300:" not in observation["content"]
    checkpoint = json.loads(json.dumps(state))
    restored = RepoAnalysisAgent(runtime(store, None, tools)).tools({
        **checkpoint,
        "pending_calls": [{"tool": "read_file", "path": "src/main.py",
                           "start_line": last + 1, "end_line": last + 20}],
    })
    assert len(tools.reads) == 1
    assert restored["no_progress_rounds"] == 0
    assert restored["history"][-1]["evidence_id"] == observation["evidence_id"]


def test_verification_failure_survives_compaction_and_terminal_rejection():
    store = FakeStore(spec(automatic()))
    agent = RepoAnalysisAgent(runtime(store, None, None))
    state = initial_state(store.value)
    for index in range(3):
        state = agent._verification_feedback(state, [f"Exact missing requirement {index}."])
    assert state["stop_reason"] == "EVIDENCE_REJECTED"
    assert "Exact missing requirement 2." in state["verification_feedback"][-1]
    state["history"] += [
        {"tool": "decision", "reference": "", "content": f"Investigation note {n}. " * 40}
        for n in range(20)
    ]
    restored = json.loads(json.dumps(state))
    payload = agent._payload(store.value, restored, 6, max_tokens=4096, recent_window=2)
    assert any("Exact missing requirement 2." in item for item in payload["failed_attempts"])
    assert payload["task"] == store.value.task
    assert payload["permissions"]["repository"] == store.value.repository


def copied_store():
    original, tools = FakeStore(spec(automatic())), SourcePages()
    agent = RepoAnalysisAgent(runtime(original, None, tools))
    agent.tools({
        **initial_state(original.value),
        "pending_calls": [{"tool": "read_file", "path": "src/main.py"}],
    })
    follow = FakeStore(spec(automatic()).model_copy(update={
        "thread_id": original.value.thread_id, "parent_run_id": original.value.run_id,
    }))
    follow.artifacts = dict(original.artifacts)
    for record in original.evidence.values():
        new_id = uuid4()
        follow.evidence[new_id] = record.model_copy(update={"evidence_id": new_id})
    return follow


class DeniedTools:
    calls = 0

    async def execute(self, call):
        self.calls += 1
        raise DomainError("GITHUB_FORBIDDEN", "Controlled external read must not succeed", 403)


def test_followup_reads_copied_evidence_through_current_run_scope_without_network():
    store, tools = copied_store(), DeniedTools()
    checked = []
    read = store.artifact_bytes

    def scoped_read(content_hash, run_id):
        assert run_id == store.value.run_id
        checked.append((content_hash, run_id))
        return read(content_hash, run_id)

    store.artifact_bytes = scoped_read
    state = RepoAnalysisAgent(runtime(store, None, tools)).tools({
        **initial_state(store.value),
        "pending_calls": [{"tool": "read_file", "path": "src/main.py",
                           "start_line": 101, "end_line": 120}],
    })
    assert checked and tools.calls == store.calls == 0
    assert state["evidence"][0]["evidence_id"] == str(next(iter(store.evidence)))
    assert "line 0120:" in state["history"][-1]["content"]
    assert state["no_progress_rounds"] == 0


def test_copied_evidence_from_a_different_fixed_commit_is_not_a_cache_grant():
    store, tools = copied_store(), DeniedTools()
    store.value = store.value.model_copy(update={"commit": "b" * 40})
    state = RepoAnalysisAgent(runtime(store, None, tools)).tools({
        **initial_state(store.value),
        "pending_calls": [{"tool": "read_file", "path": "src/main.py"}],
    })
    assert tools.calls == 1
    assert state["history"][-1]["error_code"] == "GITHUB_FORBIDDEN"
    assert not state["evidence"]


def test_corrupt_cached_source_is_rejected_without_claiming_progress():
    store, tools = copied_store(), DeniedTools()
    content_hash = next(iter(store.artifacts))
    store.artifacts[content_hash] = b"corrupted source"
    state = RepoAnalysisAgent(runtime(store, None, tools)).tools({
        **initial_state(store.value),
        "pending_calls": [{"tool": "read_file", "path": "src/main.py"}],
    })
    assert tools.calls == 0
    assert state["history"][-1]["error_code"] == "ARTIFACT_HASH_MISMATCH"
    assert state["no_progress_rounds"] == 1 and not state["evidence"]


def test_cancellation_during_cached_read_does_not_publish_stale_observation():
    store, tools = copied_store(), DeniedTools()
    read = store.artifact_bytes

    def cancelled_read(content_hash, run_id):
        store.value = store.value.model_copy(update={
            "run_status": "cancelled", "execution_epoch": store.value.execution_epoch + 1,
        })
        return read(content_hash, run_id)

    store.artifact_bytes = cancelled_read
    state = RepoAnalysisAgent(runtime(store, None, tools)).tools({
        **initial_state(store.value),
        "pending_calls": [{"tool": "read_file", "path": "src/main.py"}],
    })
    assert state["stop_reason"] == "FENCED"
    assert tools.calls == 0
    assert not state["history"] and not state["evidence"]


def test_real_artifact_corruption_is_not_silently_refetched_from_github(tmp_path):
    store, tools = copied_store(), DeniedTools()
    artifacts = ArtifactStore(tmp_path / "cas")
    content_hash = next(iter(store.artifacts))
    assert artifacts.put(store.artifacts[content_hash]) == content_hash
    artifacts.path(content_hash).write_bytes(b"tampered source")

    def scoped_read(digest, run_id):
        assert run_id == store.value.run_id
        return artifacts.read(digest)

    store.artifact_bytes = scoped_read
    state = RepoAnalysisAgent(runtime(store, None, tools)).tools({
        **initial_state(store.value),
        "pending_calls": [{"tool": "read_file", "path": "src/main.py"}],
    })
    assert tools.calls == store.calls == 0
    assert state["history"][-1]["error_code"] == "ARTIFACT_CORRUPTED"
    assert not state["evidence"] and state["no_progress_rounds"] == 1


def test_cached_scope_denial_does_not_downgrade_to_a_remote_read():
    store, tools = copied_store(), DeniedTools()

    def denied_read(digest, run_id):
        raise DomainError("MODEL_SCOPE_DENIED", "Controlled scope denial", 403)

    store.artifact_bytes = denied_read
    state = RepoAnalysisAgent(runtime(store, None, tools)).tools({
        **initial_state(store.value),
        "pending_calls": [{"tool": "read_file", "path": "src/main.py"}],
    })
    assert tools.calls == store.calls == 0
    assert state["history"][-1]["error_code"] == "MODEL_SCOPE_DENIED"
