"""Run the research–data–report graph against durable claims and evidence."""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime

import pytest
from test_general_agent_ga02 import allowed, graph, node, start
from test_general_agent_ga06 import DECLARATION, FIRST, PAGE_A, executor

from mgar_v3.domain import DomainError
from mgar_v3.general_agent.contracts import (
    Deliverable,
    OutcomeArtifact,
    SourceRef,
    SuccessCriterion,
)
from mgar_v3.general_agent.data_analysis import DataAnalysisExecutor, DataAnalysisRequest
from mgar_v3.general_agent.graph import Dependency, NodeInput, deterministic_plan
from mgar_v3.general_agent.research import CitationBinding, ResearchCollection
from mgar_v3.general_agent.verification_contracts import ResultDraft, VerificationPlan

DATA = b"amount,region\n1,north\n4,south\n7,north\n"


def pins_for(engine, *keys):
    descriptors = tuple(engine.general_graphs.registry.definition(key) for key in keys)
    assert all(item is not None for item in descriptors)
    return descriptors


def fixture(engine):
    logs = []
    web = executor(engine.artifacts, logs)
    data = DataAnalysisExecutor(engine.artifacts)
    origin = SourceRef(kind="dataset", reference="orders", version=engine.artifacts.put(DATA))
    request = DataAnalysisRequest(source=origin, field="amount", formula="mean", group_by="region")
    citation = SourceRef(
        kind="web", reference=FIRST, version="sha256:" + hashlib.sha256(PAGE_A).hexdigest()
    )
    capabilities = ("research.sources", "data.analyze", "research.report")
    spec = allowed(*capabilities).model_copy(
        update={
            "goal": "Describe the measured orders in their documented context.",
            "source_refs": (citation, origin),
            "deliverables": (
                Deliverable(id="report", kind="report", description="Source and numeric summary"),
                Deliverable(id="metrics", kind="table", description="Verified sample mean"),
            ),
            "success_criteria": (
                SuccessCriterion(
                    id="sources", description="Cite documented context", deliverable_ids=("report",)
                ),
                SuccessCriterion(
                    id="check_data",
                    description="Reproduce the result",
                    deliverable_ids=("metrics",),
                ),
            ),
        }
    )
    plan = deterministic_plan(spec, 1)
    task, state, access = start(engine, plan, spec, *pins_for(engine, *capabilities))

    def run(key, execute):
        return engine.general_executors.run(
            task.task_id, task.revision, state.graph_revision, key, access, execute=execute
        )

    return task, state, spec, run, web, data, request, logs


def test_research_data_report_end_to_end_uses_independently_replayed_data(engine):
    task, initial, spec, run, web, data, request, logs = fixture(engine)
    assert run("report", web.node_handler) is None
    assert (
        run(
            "analysis",
            data.handler(
                request,
                outputs={"metrics": "numeric", "analysis_evidence": "manifest"},
            ),
        )
        is None
    )
    first = run("research", web.node_handler)
    assert first is not None and logs == [FIRST]
    collection_hash = first.progress["research"].outputs["source_evidence"]
    assert isinstance(collection_hash, str)
    collection = ResearchCollection.model_validate_json(engine.artifacts.read(collection_hash))
    middle = run(
        "analysis",
        data.handler(
            request,
            outputs={"metrics": "numeric", "analysis_evidence": "manifest"},
        ),
    )
    assert middle is not None
    evidence = middle.progress["analysis"].outputs
    manifest_hash, numeric_hash = evidence["analysis_evidence"], evidence["metrics"]
    assert isinstance(manifest_hash, str) and isinstance(numeric_hash, str)
    assert middle.progress["analysis"].claim.inputs == {"upstream": collection_hash}
    final = run("report", web.node_handler)
    assert final is not None and logs == [FIRST]
    assert final.graph_revision == initial.graph_revision
    assert final.spent.tool_calls == 3
    assert all(progress.state == "succeeded" for progress in final.progress.values())
    assert final.progress["report"].claim.inputs == {
        "upstream": numeric_hash,
        "source_collection": collection_hash,
        "analysis_manifest": manifest_hash,
    }
    report_hash = final.progress["report"].outputs["report"]
    assert isinstance(report_hash, str)
    markdown = engine.artifacts.read(report_hash).decode()
    directly_cited, appendix = markdown.split("## Dataset calculation", 1)
    assert DECLARATION in directly_cited and FIRST in directly_cited
    assert "reproduced mean of `amount` is **4**" in appendix
    assert "used **3** numeric samples" in appendix
    assert FIRST not in appendix and DECLARATION not in appendix
    assert manifest_hash in appendix and numeric_hash in appendix
    replay = data.verify_manifest(request, manifest_hash)
    assert replay.normalized_sha256 in appendix
    assert replay.artifacts["numeric"] == numeric_hash

    verifying = engine.general_tasks.get(task.task_id)
    assert verifying.state == "verifying" and verifying.plan_version is not None
    doc, cited = CitationBinding(
        source=collection.sources[0],
        excerpt=DECLARATION,
    ).verification("source-proof", "report", "web-reader")
    numeric, observed = data.numeric_evidence(
        replay,
        request,
        output_id="metrics",
        collector="data-runner",
        observed_at=datetime.now(UTC),
    )
    plan = VerificationPlan(
        task_id=task.task_id,
        task_revision=task.revision,
        plan_version=verifying.plan_version,
        expected_outputs=spec.deliverables,
        verification_steps=(doc, numeric),
    )
    verifier = engine.general_verification
    key = verifier.install(plan, verifying.version)
    verifier.capture(key, doc.id, cited)
    verifier.capture(key, numeric.id, observed)
    review = verifier.verify(
        key,
        ResultDraft(
            summary="Source context and sample mean checked.",
            outputs=(
                OutcomeArtifact(deliverable_id="report", sha256=report_hash),
                OutcomeArtifact(deliverable_id="metrics", sha256=numeric_hash),
            ),
        ),
        verifier="independent-check",
    )
    assert review.status == "verified"
    assert verifier.publish(review.report_id).state == "completed"
    assert logs == [FIRST]


def test_combined_report_rejects_an_upstream_result_that_does_not_reproduce(engine):
    task, _, _, run, web, data, request, logs = fixture(engine)
    assert run("research", web.node_handler) is not None
    actual = data.handler(request, outputs={"metrics": "numeric", "analysis_evidence": "manifest"})
    fake = engine.artifacts.put(b'{"sample_count":3,"value":400,"formula":"mean","field":"amount"}')

    def forged(task, node, claim):
        return {**actual(task, node, claim), "metrics": fake}

    assert run("analysis", forged) is not None
    with pytest.raises(DomainError) as result:
        run("report", web.node_handler)
    assert result.value.code == "RESEARCH_ANALYSIS"
    snapshot = engine.general_graphs.get(task.task_id)
    assert snapshot.progress["report"].state == "failed"
    assert snapshot.progress["report"].outputs == {}
    assert engine.general_tasks.get(task.task_id).state == "blocked"
    assert logs == [FIRST]


def test_legacy_report_upstream_still_accepts_a_collection(engine):
    logs = []
    web = executor(engine.artifacts, logs)
    version = "sha256:" + hashlib.sha256(PAGE_A).hexdigest()
    spec = allowed("research.sources", "research.report").model_copy(
        update={"source_refs": (SourceRef(kind="web", reference=FIRST, version=version),)}
    )
    plan = graph(
        node("find", outputs=("collection",), capabilities=("research.sources",)),
        node(
            "write",
            outputs=("result",),
            capabilities=("research.report",),
            dependencies=(Dependency(node_id="find"),),
            inputs=(NodeInput(name="upstream", node_id="find", output_id="collection"),),
        ),
    )
    task, state, access = start(
        engine, plan, spec, *pins_for(engine, "research.sources", "research.report")
    )
    for key in ("find", "write"):
        updated = engine.general_executors.run(
            task.task_id, task.revision, state.graph_revision, key, access, execute=web.node_handler
        )
        assert updated is not None
    output = updated.progress["write"].outputs["result"]
    assert isinstance(output, str)
    assert DECLARATION.encode() in engine.artifacts.read(output)
    assert b"## Dataset calculation" not in engine.artifacts.read(output)
    assert engine.general_tasks.get(task.task_id).state == "verifying" and logs == [FIRST]
