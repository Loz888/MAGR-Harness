"""GA06 source evidence, report artifacts and the shared Executor entry point."""

from __future__ import annotations

import hashlib
import io
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from pypdf import PdfReader
from test_general_agent_ga02 import allowed, capability, graph, node, start

from mgar_v3.domain import DomainError
from mgar_v3.evidence.artifacts import ArtifactStore
from mgar_v3.general_agent.capabilities import CapabilityRegistry
from mgar_v3.general_agent.contracts import OutcomeArtifact, SourceRef
from mgar_v3.general_agent.research import (
    CatalogSource,
    CitationBinding,
    ResearchClaim,
    ResearchExecutor,
    ResearchPlan,
    ResearchQuestion,
    SourceQuote,
)
from mgar_v3.general_agent.research_pdf import report_pdf, verified_pdf_text
from mgar_v3.general_agent.research_web import PublicWebReader, extract_page
from mgar_v3.general_agent.verification_checks import check_content
from mgar_v3.general_agent.verification_contracts import ResultDraft, VerificationPlan

FIRST = "https://first.example/research"
SECOND = "https://second.example/research"
MIRROR = "https://first.example/mirror"
REDIRECT = "https://first.example/redirect"
UNDECLARED = "https://first.example/protected"
DECLARATION = "The documented capacity is 120 requests."
OPPOSITE = "The documented capacity is 180 requests."
PAGE_A = (
    "<html><head><title>Provider A</title>"
    '<meta property="article:published_time" content="2024-01-03T12:00:00Z"></head>'
    f"<body><p>{DECLARATION}</p><script>Do not retain active script</script>"
    '<a href="https://second.example/research">Related</a>'
    '<a href="/mirror#section">Mirror</a></body></html>'
).encode()
PAGE_B = f"<html><title>Provider B</title><p>{OPPOSITE}</p></html>".encode()
HOSTS = frozenset({"first.example", "second.example"})


def response(content, media="text/html"):
    return httpx.Response(200, stream=httpx.ByteStream(content), headers={"Content-Type": media})


def mock_reader(log):
    def fetch(request):
        assert request.method == "GET" and request.url.host == "93.184.216.34"
        assert request.extensions["sni_hostname"] == request.headers["Host"]
        assert "authorization" not in request.headers and "cookie" not in request.headers
        host = request.headers["host"]
        observed = f"https://{host}{request.url.path}"
        log.append(observed)
        if observed == REDIRECT:
            return httpx.Response(302, headers={"location": UNDECLARED})
        return response(
            {FIRST: PAGE_A, SECOND: PAGE_B, MIRROR: PAGE_A, UNDECLARED: PAGE_B}[observed]
        )

    return PublicWebReader(
        HOSTS,
        resolve=lambda _host: ("93.184.216.34",),
        test_transport=httpx.MockTransport(fetch),
    )


def executor(storage, log):
    return ResearchExecutor(
        storage,
        mock_reader(log),
        (
            CatalogSource(uri=FIRST, title="Capacity specification", trust="primary"),
            CatalogSource(
                uri=SECOND, title="Measurements", topics=("capacity industry",), trust="secondary"
            ),
            CatalogSource(uri=MIRROR, title="Capacity archive"),
        ),
    )


def comparison(category):
    return ResearchPlan(
        title="Capacity comparison",
        category=category,
        questions=(ResearchQuestion(id="capacity", text="capacity"),),
        claims=(
            ResearchClaim(
                question_id="capacity",
                category="fact",
                statement=DECLARATION,
                compared_value="120",
                quotes=(SourceQuote(uri=FIRST, excerpt=DECLARATION),),
            ),
            ResearchClaim(
                question_id="capacity",
                category="fact",
                statement=OPPOSITE,
                compared_value="180",
                quotes=(SourceQuote(uri=SECOND, excerpt=OPPOSITE),),
            ),
            ResearchClaim(
                question_id="capacity",
                category="inference",
                statement="Published observations differ.",
                quotes=(
                    SourceQuote(uri=FIRST, excerpt=DECLARATION),
                    SourceQuote(uri=SECOND, excerpt=OPPOSITE),
                ),
            ),
            ResearchClaim(
                question_id="capacity",
                category="opinion",
                statement="Use a measured rollout.",
                quotes=(SourceQuote(uri=FIRST, excerpt=DECLARATION),),
            ),
            ResearchClaim(
                question_id="capacity",
                category="unconfirmed",
                statement="No agreed estimate.",
            ),
        ),
        max_pages=6,
        max_age_days=30,
        follow_links=True,
    )


@pytest.mark.parametrize("category", ("technical", "industry", "comparison"))
def test_multisource_report_retains_conflicts_and_versioned_citations(tmp_path, category):
    logs = []
    store = ArtifactStore(tmp_path)
    service = executor(store, logs)
    plan = comparison(category)
    collection = service.collect(plan)
    assert len(collection.sources) == 2
    assert sorted(logs) == sorted([MIRROR, FIRST, SECOND])
    assert {item.uri for item in service.search("capacity")} == {FIRST, SECOND, MIRROR}
    first = next(item for item in collection.sources if item.uri == FIRST)
    assert first.published_at == datetime(2024, 1, 3, 12, tzinfo=UTC)
    assert first.title == "Provider A" and first.trust == "primary"
    assert service.follow_link(first, SECOND).uri == SECOND
    assert "active script" not in extract_page(service.open(FIRST)).text
    result = service.synthesize(plan, collection)
    assert result.conflicts == ("capacity",)
    output = store.read(result.markdown_sha256).decode()
    assert all(
        value in output
        for value in (
            DECLARATION,
            OPPOSITE,
            "Older publication",
            "Published: unknown",
            "Both are retained.",
            "**Inference**",
            "**Opinion**",
            "**Unconfirmed**",
        )
    )
    proof = result.claims[0].citations[0]
    step, observation = proof.verification("citation-a", "result", "reader")
    assert step.source == observation.source == first.source_ref()
    assert store.read(first.raw_sha256) == PAGE_A
    assert proof.excerpt.encode() in store.read(observation.content_sha256)
    pdf = store.read(result.pdf_sha256)
    assert verified_pdf_text(pdf) == output.encode()
    assert "Capacity comparison" in PdfReader(io.BytesIO(pdf)).pages[0].extract_text()


def test_invented_fact_or_quote_cannot_become_a_verified_report(tmp_path):
    storage = ArtifactStore(tmp_path)
    service = executor(storage, [])
    trusted = comparison("technical").claims[0]
    for change in (
        {"statement": "The capacity is 1000 transactions."},
        {"quotes": (SourceQuote(uri=FIRST, excerpt="An invented quote."),)},
    ):
        claim = ResearchClaim.model_validate({**trusted.model_dump(), **change})
        plan = comparison("technical").model_copy(update={"claims": (claim,)})
        with pytest.raises(DomainError) as exc:
            service.synthesize(plan, service.collect(plan))
        assert exc.value.code == "RESEARCH_CITATION"


def test_reader_enforces_exact_source_scope_before_loading_redirect(tmp_path):
    log = []
    service = executor(ArtifactStore(tmp_path), log)
    with pytest.raises(DomainError) as exc:
        service.collect(comparison("technical"), (SourceRef(kind="web", reference=REDIRECT),))
    assert exc.value.code == "RESEARCH_UNAVAILABLE"
    assert log == [REDIRECT]


@pytest.mark.parametrize("ip", ("127.0.0.1", "10.1.4.1", "169.254.169.254", "::1", "ff02::1"))
def test_nonpublic_resolution_does_not_reach_transport(ip):
    def reject(_request):
        raise AssertionError("Non-public destinations must never reach the transport")

    reader = PublicWebReader(
        HOSTS, resolve=lambda _host: (ip,), test_transport=httpx.MockTransport(reject)
    )
    with pytest.raises(DomainError) as exc:
        reader.open(FIRST)
    assert exc.value.code == "RESEARCH_SCOPE"


def test_expired_read_and_forged_content_version_are_rejected(tmp_path):
    storage = ArtifactStore(tmp_path)
    calls = []
    service = executor(storage, calls)
    with pytest.raises(DomainError) as exc:
        service.collect(
            comparison("technical"), deadline_at=datetime.now(UTC) - timedelta(seconds=1)
        )
    assert exc.value.code == "RESEARCH_UNAVAILABLE" and calls == []
    with pytest.raises(DomainError) as exc:
        service.collect(
            comparison("technical"),
            (SourceRef(kind="web", reference=FIRST, version="sha256:" + "0" * 64),),
        )
    assert exc.value.code == "RESEARCH_UNAVAILABLE" and calls == [FIRST]


def test_graph_research_result_is_checked_and_published_by_ga05(engine):
    service = executor(engine.artifacts, [])
    descriptor = capability("research.sources")
    engine.general_graphs.registry = CapabilityRegistry([descriptor])
    pin = SourceRef(
        kind="web", reference=FIRST, version="sha256:" + hashlib.sha256(PAGE_A).hexdigest()
    )
    spec = allowed("research.sources").model_copy(
        update={"goal": "Capacity specification", "source_refs": (pin,)}
    )
    task, state, access = start(
        engine,
        graph(node("find", outputs=("result",), capabilities=("research.sources",))),
        spec,
        descriptor,
    )
    engine.general_executors.run(
        task.task_id,
        task.revision,
        state.graph_revision,
        "find",
        access,
        execute=service.node_handler,
    )
    result = engine.general_graphs.get(task.task_id)
    markdown_hash = result.progress["find"].outputs["result"]
    verifying = engine.general_tasks.get(task.task_id)
    assert verifying.state == "verifying" and verifying.plan_version is not None
    assert DECLARATION.encode() in engine.artifacts.read(markdown_hash)
    page = service.open(FIRST)
    observed_text = extract_page(page).text
    snapshot = service.snapshot(page)
    assert snapshot.source_ref() == pin and DECLARATION in observed_text
    check, observation = CitationBinding(
        source=snapshot,
        excerpt=DECLARATION,
    ).verification("citation", "result", "web-reader")
    plan = VerificationPlan(
        task_id=task.task_id,
        task_revision=task.revision,
        plan_version=verifying.plan_version,
        expected_outputs=spec.deliverables,
        verification_steps=(check,),
    )
    verifier = engine.general_verification
    key = verifier.install(plan, verifying.version)
    verifier.capture(key, check.id, observation)
    pdf = report_pdf(f"{DECLARATION}\n{FIRST}\n中文证据")
    assert check_content(verifying, check, observed_text.encode(), pdf, "reviewer")
    assert not check_content(
        verifying, check, observed_text.encode(), pdf + b"tampering", "reviewer"
    )
    assert "中文证据" in PdfReader(io.BytesIO(pdf)).pages[0].extract_text()
    review = verifier.verify(
        key,
        ResultDraft(
            summary="Reviewed source quotation.",
            outputs=(OutcomeArtifact(deliverable_id="result", sha256=markdown_hash),),
        ),
        verifier="reviewer",
    )
    assert review.status == "verified" and verifier.publish(review.report_id).state == "completed"
