"""Research plans, citation provenance and report delivery for GA06."""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Annotated, Literal, Self

from pydantic import AwareDatetime, Field, ValidationError, field_validator, model_validator

from mgar_v3.domain import DomainError, Model, digest
from mgar_v3.evidence.artifacts import ArtifactStore
from mgar_v3.general_agent.contracts import Identifier, Sha256, SourceRef, TaskSnapshot, Text
from mgar_v3.general_agent.graph import BranchValue, GraphNode
from mgar_v3.general_agent.graph_store import NodeClaim
from mgar_v3.general_agent.research_pdf import report_pdf
from mgar_v3.general_agent.research_web import (
    PageRead,
    PublicWebReader,
    extract_page,
    normalized_uri,
)
from mgar_v3.general_agent.verification_contracts import Citation, EvidenceObservation

SourceTrust = Literal["primary", "secondary", "unclassified"]
Category = Literal["fact", "inference", "opinion", "unconfirmed"]
URI = Annotated[str, Field(min_length=1, max_length=2048)]
Excerpt = Annotated[str, Field(min_length=1, max_length=512)]


def _error(code: str, message: str) -> DomainError:
    return DomainError(code, message, 422)


def _store(artifacts: ArtifactStore, value: Model) -> str:
    return artifacts.put(value.model_dump_json().encode())


def _load[T: Model](artifacts: ArtifactStore, value: object, model: type[T]) -> T:
    if not isinstance(value, str):
        raise _error("RESEARCH_ARTIFACT", "Expected a bound research artifact reference.")
    try:
        return model.model_validate_json(artifacts.read(value))
    except ValidationError as exc:
        raise _error("RESEARCH_ARTIFACT", "Research artifact has an invalid contract.") from exc


def _normalized_text(value: str) -> str:
    return " ".join(value.split())


class CatalogSource(Model):
    uri: URI
    title: Text
    topics: Annotated[tuple[Text, ...], Field(max_length=24)] = ()
    trust: SourceTrust = "unclassified"

    @field_validator("uri")
    @classmethod
    def canonical(cls, value: str) -> str:
        return normalized_uri(value)


class SourceQuote(Model):
    uri: URI
    excerpt: Excerpt

    @field_validator("uri")
    @classmethod
    def canonical(cls, value: str) -> str:
        return normalized_uri(value)


class ResearchClaim(Model):
    question_id: Identifier
    category: Category
    statement: Text
    quotes: Annotated[tuple[SourceQuote, ...], Field(max_length=4)] = ()
    compared_value: Text | None = None

    @model_validator(mode="after")
    def check_shape(self) -> Self:
        if self.category != "unconfirmed" and not self.quotes:
            raise ValueError("Confirmed claims and interpretations need explicit evidence.")
        if self.compared_value is not None and (self.category != "fact" or not self.quotes):
            raise ValueError("Comparison values must be directly sourced observations.")
        return self


class ResearchQuestion(Model):
    id: Identifier
    text: Text


class ResearchPlan(Model):
    schema_version: Literal["general-research-plan-v1"] = "general-research-plan-v1"
    title: Text
    category: Literal["technical", "industry", "comparison"]
    questions: Annotated[tuple[ResearchQuestion, ...], Field(min_length=1, max_length=8)]
    claims: Annotated[tuple[ResearchClaim, ...], Field(max_length=32)] = ()
    max_pages: Annotated[int, Field(strict=True, ge=1, le=12)] = 4
    max_age_days: Annotated[int, Field(strict=True, ge=1, le=3650)] = 365
    follow_links: Annotated[bool, Field(strict=True)] = False
    pdf_outputs: Annotated[tuple[Identifier, ...], Field(max_length=12)] = ()

    @model_validator(mode="after")
    def check_shape(self) -> Self:
        questions = {question.id for question in self.questions}
        if len(questions) != len(self.questions) or any(
            claim.question_id not in questions for claim in self.claims
        ):
            raise ValueError("Claims must refer to unique declared questions.")
        return self


class ResearchSnapshot(Model):
    uri: URI
    title: Text
    retrieved_at: AwareDatetime
    published_at: AwareDatetime | None
    raw_sha256: Sha256
    text_sha256: Sha256
    trust: SourceTrust
    media_type: Literal["text/html", "text/plain", "text/markdown"]
    links: Annotated[tuple[URI, ...], Field(max_length=100)]

    def source_ref(self) -> SourceRef:
        return SourceRef(kind="web", reference=self.uri, version=f"sha256:{self.raw_sha256}")


class ResearchCollection(Model):
    schema_version: Literal["general-research-collection-v1"] = "general-research-collection-v1"
    plan_sha256: Sha256
    sources: Annotated[tuple[ResearchSnapshot, ...], Field(min_length=1, max_length=12)]
    failed_sources: Annotated[tuple[URI, ...], Field(max_length=12)] = ()


class CitationBinding(Model):
    source: ResearchSnapshot
    excerpt: Excerpt

    def verification(
        self, step_id: str, output_id: str, collector: str
    ) -> tuple[Citation, EvidenceObservation]:
        source = self.source.source_ref()
        return (
            Citation(
                id=step_id,
                output_id=output_id,
                kind="citation",
                source=source,
                source_sha256=self.source.text_sha256,
                excerpt=self.excerpt,
            ),
            EvidenceObservation(
                collector=collector,
                observed_at=self.source.retrieved_at,
                content_sha256=self.source.text_sha256,
                source=source,
            ),
        )


class ResearchClaimEvidence(Model):
    claim: ResearchClaim
    citations: tuple[CitationBinding, ...]


class ResearchResult(Model):
    schema_version: Literal["general-research-result-v1"] = "general-research-result-v1"
    plan_sha256: Sha256
    collection_sha256: Sha256
    claims: tuple[ResearchClaimEvidence, ...]
    conflicts: tuple[Identifier, ...]
    markdown_sha256: Sha256
    pdf_sha256: Sha256


class ResearchExecutor:
    """Trusted source catalog and mechanically checked citations for Research nodes."""

    def __init__(
        self,
        artifacts: ArtifactStore,
        reader: PublicWebReader,
        catalog: tuple[CatalogSource, ...],
    ) -> None:
        if not 1 <= len(catalog) <= 256:
            raise ValueError("Research requires a bounded source catalog.")
        self._artifacts = artifacts
        self._reader = reader
        self._catalog = {reader.authorize(entry.uri): entry for entry in catalog}
        if len(self._catalog) != len(catalog):
            raise ValueError("Research source catalog contains duplicate entries.")

    def search(self, query: str) -> tuple[CatalogSource, ...]:
        """Search the deployment's approved source index by title, topic and URI."""
        tokens = set(re.findall(r"[^\W_]+", query.casefold()))
        ranked = []
        for item in self._catalog.values():
            haystack = " ".join((item.uri, item.title, *item.topics)).casefold()
            score = sum(token in haystack for token in tokens)
            if score:
                ranked.append((-score, item.uri, item))
        return tuple(item for _, _, item in sorted(ranked))

    def open(self, uri: str) -> PageRead:
        return self._reader.open(uri)

    def follow_link(self, source: ResearchSnapshot, target: str) -> PageRead:
        target = self._reader.authorize(target)
        if target not in source.links:
            raise _error("RESEARCH_LINK", "Link is outside the source's extracted links.")
        return self.open(target)

    def snapshot(self, page: PageRead) -> ResearchSnapshot:
        extracted = extract_page(page)
        if not extracted.text.strip():
            raise _error("RESEARCH_CONTENT", "Web source has no readable text.")
        raw = self._artifacts.put(page.content)
        text = self._artifacts.put(extracted.text.encode())
        registered = self._catalog.get(page.uri)
        return ResearchSnapshot.model_validate(
            {
                "uri": self._reader.authorize(page.uri),
                "title": extracted.title,
                "retrieved_at": page.retrieved_at,
                "published_at": extracted.published_at,
                "raw_sha256": raw,
                "text_sha256": text,
                "media_type": page.media_type,
                "trust": registered.trust if registered else "unclassified",
                "links": extracted.links,
            }
        )

    def collect(
        self,
        plan: ResearchPlan,
        source_refs: tuple[SourceRef, ...] = (),
        *,
        deadline_at: datetime | None = None,
    ) -> ResearchCollection:
        if any(ref.kind != "web" for ref in source_refs):
            raise _error("RESEARCH_SCOPE", "Research source references must be web sources.")
        explicit = {normalized_uri(ref.reference): ref.version for ref in source_refs}
        seeds = (
            list(explicit)
            if explicit
            else list(
                dict.fromkeys(
                    [
                        quote.uri
                        for claim in plan.claims
                        for quote in claim.quotes
                        if quote.uri in self._catalog
                    ]
                    + [
                        entry.uri
                        for question in plan.questions
                        for entry in self.search(question.text)
                    ]
                )
            )
        )
        queued: list[tuple[str, ResearchSnapshot | None]] = [(uri, None) for uri in seeds]
        if not queued:
            raise _error(
                "RESEARCH_SOURCE", "No authorized catalog source matched the research plan."
            )
        found: dict[str, ResearchSnapshot] = {}
        failures: list[str] = []
        attempted: set[str] = set()
        seen_body: set[str] = set()
        scope = frozenset(explicit or self._catalog)
        for uri, parent in queued:
            if len(attempted) >= plan.max_pages or (
                deadline_at is not None and datetime.now(UTC) >= deadline_at
            ):
                break
            if uri in attempted or uri in found:
                continue
            attempted.add(uri)
            try:
                if parent is not None and uri not in parent.links:
                    raise _error("RESEARCH_LINK", "Link is outside the source's extracted links.")
                page = self._reader.open(uri, permitted_uris=scope, deadline_at=deadline_at)
                snapshot = self.snapshot(page)
                expected = explicit.get(snapshot.uri)
                if expected and snapshot.source_ref().version != expected:
                    raise _error("RESEARCH_VERSION", "Source content differs from the task's pin.")
            except DomainError:
                failures.append(uri)
                continue
            if snapshot.text_sha256 in seen_body:
                continue
            found[snapshot.uri] = snapshot
            seen_body.add(snapshot.text_sha256)
            if plan.follow_links and not explicit:
                for target in snapshot.links:
                    if target in self._catalog:
                        queued.append((target, snapshot))
        if not found:
            raise _error("RESEARCH_UNAVAILABLE", "No declared sources could be read and verified.")
        return ResearchCollection(
            plan_sha256=digest(plan.model_dump(mode="json")),
            sources=tuple(found.values()),
            failed_sources=tuple(failures),
        )

    def _read_extraction(self, source: ResearchSnapshot) -> str:
        canonical = self._reader.authorize(source.uri)
        actual = extract_page(
            PageRead(
                canonical,
                source.retrieved_at,
                source.media_type,
                self._artifacts.read(source.raw_sha256),
            )
        )
        raw = self._artifacts.read(source.text_sha256)
        registered = self._catalog.get(canonical)
        trust = registered.trust if registered else "unclassified"
        if actual.text.encode() != raw or (
            actual.title,
            actual.published_at,
            actual.links,
            trust,
        ) != (source.title, source.published_at, source.links, source.trust):
            raise _error("RESEARCH_ARTIFACT", "Source text does not match the captured page.")
        return actual.text

    def synthesize(self, plan: ResearchPlan, collection: ResearchCollection) -> ResearchResult:
        if collection.plan_sha256 != digest(plan.model_dump(mode="json")):
            raise _error("RESEARCH_PLAN", "Collection is outside this research plan.")
        sources = {item.uri: (item, self._read_extraction(item)) for item in collection.sources}
        claims = plan.claims or tuple(
            ResearchClaim(
                question_id=plan.questions[0].id,
                category="fact",
                statement=snippet,
                quotes=(SourceQuote(uri=source.uri, excerpt=snippet),),
            )
            for source, text in sources.values()
            if (snippet := self._first_excerpt(text))
        )
        evidence = []
        observations: dict[str, set[tuple[str, str]]] = {}
        for claim in claims:
            bindings = []
            for quote in claim.quotes:
                pair = sources.get(quote.uri)
                excerpt = _normalized_text(quote.excerpt)
                if pair is None or not excerpt or excerpt not in _normalized_text(pair[1]):
                    raise _error("RESEARCH_CITATION", "Claim citation cannot be traced to source.")
                source, text = pair
                # The verifier requires the quoted bytes, so bind the exact captured slice.
                actual = self._exact_excerpt(text, excerpt)
                if claim.category == "fact" and _normalized_text(claim.statement) != excerpt:
                    raise _error(
                        "RESEARCH_CITATION", "Fact must be stated as the sourced quotation."
                    )
                if claim.compared_value is not None and claim.compared_value not in excerpt:
                    raise _error("RESEARCH_CITATION", "Comparison value is absent from citation.")
                bindings.append(CitationBinding(source=source, excerpt=actual))
            if claim.compared_value is not None:
                observations.setdefault(claim.question_id, set()).update(
                    (binding.source.uri, claim.compared_value) for binding in bindings
                )
            evidence.append(ResearchClaimEvidence(claim=claim, citations=tuple(bindings)))
        conflicts = tuple(
            sorted(
                key
                for key, values in observations.items()
                if len({uri for uri, _ in values}) > 1 and len({value for _, value in values}) > 1
            )
        )
        markdown = self._markdown(plan, collection, tuple(evidence), conflicts)
        return ResearchResult(
            plan_sha256=collection.plan_sha256,
            collection_sha256=_store(self._artifacts, collection),
            claims=tuple(evidence),
            conflicts=conflicts,
            markdown_sha256=self._artifacts.put(markdown.encode()),
            pdf_sha256=self._artifacts.put(report_pdf(markdown)),
        )

    @staticmethod
    def _first_excerpt(text: str) -> str:
        line = next((line.strip() for line in text.splitlines() if line.strip()), "")
        return line[:180].strip()

    @staticmethod
    def _exact_excerpt(text: str, excerpt: str) -> str:
        if excerpt in text:
            return excerpt
        parts = re.split(r"\s+", excerpt)
        match = re.search(r"\s+".join(map(re.escape, parts)), text)
        if match is None or len(match[0]) > 512:
            raise _error("RESEARCH_CITATION", "Quoted source span exceeds citation limits.")
        return match[0]

    @staticmethod
    def _markdown(
        plan: ResearchPlan,
        collection: ResearchCollection,
        claims: tuple[ResearchClaimEvidence, ...],
        conflicts: tuple[str, ...],
    ) -> str:
        def plain(text: str) -> str:
            text = _normalized_text(text).replace("&", "&amp;").replace("<", "&lt;")
            return re.sub(r"([\\`*_{}\[\]()#!|>~+])", r"\\\1", text)

        output = [f"# {plain(plan.title)}", "", f"Research category: {plan.category}", ""]
        for question in plan.questions:
            output.extend([f"## {plain(question.text)}", ""])
            if question.id in conflicts:
                output.extend(
                    ["Sources give different directly observed values. Both are retained.", ""]
                )
            answers = [item for item in claims if item.claim.question_id == question.id]
            if not answers:
                output.extend(["Unconfirmed: no verified answer is available.", ""])
            for item in answers:
                output.extend(
                    [f"**{item.claim.category.title()}**: {plain(item.claim.statement)}", ""]
                )
                for binding in item.citations:
                    source = binding.source
                    output.extend([f"- [{plain(source.title)}](<{source.uri}>) [{source.trust}]"])
                    marker = "~" * max(
                        3, max(map(len, re.findall(r"~+", binding.excerpt)), default=0) + 1
                    )
                    output.extend([marker, binding.excerpt, marker, ""])
        output.extend(["## Source chronology", ""])
        for source in collection.sources:
            published = source.published_at
            publication = published.date().isoformat() if published else "unknown"
            stale = (
                " Older publication."
                if published and source.retrieved_at - published > timedelta(days=plan.max_age_days)
                else ""
            )
            output.append(
                f"- <{source.uri}>. Published: {publication}. "
                f"Retrieved: {source.retrieved_at.isoformat()}. "
                f"Snapshot: {source.raw_sha256}.{stale}"
            )
        if collection.failed_sources:
            output.extend(["", "## Sources unavailable during collection", ""])
            output.extend(f"- <{uri}>" for uri in collection.failed_sources)
        return "\n".join(output) + "\n"

    def node_handler(
        self,
        task: TaskSnapshot,
        node: GraphNode,
        claim: NodeClaim,
    ) -> dict[str, BranchValue]:
        if claim.capability_id not in {"research.sources", "research.report"}:
            raise _error("RESEARCH_CAPABILITY", "Node has no claimed research capability.")
        refs = tuple(ref for ref in task.spec.source_refs if ref.kind == "web")
        plan_ref = claim.inputs.get("plan")
        if plan_ref is not None:
            if not any(
                ref.kind == "artifact" and ref.reference == plan_ref
                for ref in task.spec.source_refs
            ):
                raise _error("RESEARCH_PLAN", "Research plan is outside the task's source scope.")
            plan = _load(self._artifacts, plan_ref, ResearchPlan)
        else:
            goal = (task.spec.goal or node.goal)[:2048]
            plan = ResearchPlan(
                title=goal,
                category="technical",
                questions=(ResearchQuestion(id="goal", text=goal),),
            )
        source_collection = claim.inputs.get("source_collection")
        upstream = claim.inputs.get("upstream")
        collection_ref = source_collection if source_collection is not None else upstream
        if claim.capability_id == "research.report" and collection_ref is not None:
            collection = _load(self._artifacts, collection_ref, ResearchCollection)
            scoped = {normalized_uri(ref.reference): ref.version for ref in refs}
            if scoped and any(
                source.uri not in scoped
                or scoped[source.uri] not in {None, source.source_ref().version}
                for source in collection.sources
            ):
                raise _error("RESEARCH_SCOPE", "Collection is outside the task's source scope.")
        else:
            collection = self.collect(plan, refs, deadline_at=claim.expires_at)
        deliverables = {item.id: item for item in task.spec.deliverables}
        if not any(key in deliverables for key in node.outputs):
            collection_hash = _store(self._artifacts, collection)
            return dict.fromkeys(node.outputs, collection_hash)
        result = self.synthesize(plan, collection)
        markdown_hash, pdf_hash = result.markdown_sha256, result.pdf_sha256
        analysis_manifest = claim.inputs.get("analysis_manifest")
        if source_collection is not None or analysis_manifest is not None:
            if claim.capability_id != "research.report" or source_collection is None:
                raise _error(
                    "RESEARCH_ANALYSIS", "Combined reports require an upstream collection."
                )
            appendix = self._analysis_appendix(task, claim, analysis_manifest, upstream)
            markdown = self._artifacts.read(markdown_hash).decode() + appendix
            markdown_hash = self._artifacts.put(markdown.encode())
            if any(output in plan.pdf_outputs for output in node.outputs):
                pdf_hash = self._artifacts.put(report_pdf(markdown))
        return {
            key: (
                pdf_hash
                if key in plan.pdf_outputs
                else markdown_hash
                if key in deliverables
                else result.collection_sha256
            )
            for key in node.outputs
        }

    def _analysis_appendix(
        self,
        task: TaskSnapshot,
        claim: NodeClaim,
        manifest: BranchValue,
        numeric: BranchValue,
    ) -> str:
        from mgar_v3.general_agent.data_analysis import DataAnalysisExecutor

        if not isinstance(manifest, str) or not isinstance(numeric, str):
            raise _error("RESEARCH_ANALYSIS", "Combined reports require bound analysis artifacts.")
        request, replayed = DataAnalysisExecutor(self._artifacts).verify_bound_manifest(
            manifest, task.spec.source_refs, deadline_at=claim.expires_at
        )
        if replayed.artifacts["numeric"] != numeric:
            raise _error("RESEARCH_ANALYSIS", "The numeric evidence differs from the analysis.")
        value = json.loads(self._artifacts.read(numeric), parse_float=Decimal)["value"]
        return "\n".join(
            [
                "",
                "## Dataset calculation",
                "",
                f"The reproduced {request.formula} of `{request.field}` is **{value}**.",
                f"The calculation used **{replayed.sample_count}** numeric samples.",
                "",
                f"- Dataset SHA256 `{replayed.input_sha256}`",
                f"- Normalized sample SHA256 `{replayed.normalized_sha256}`",
                f"- Numeric result SHA256 `{numeric}`",
                f"- Analysis manifest SHA256 `{manifest}`",
                "",
            ]
        )
