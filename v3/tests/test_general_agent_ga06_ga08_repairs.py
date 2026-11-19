"""Targeted reproductions of the two independent GA06 and GA08 findings."""

import io
import json
import re
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import httpx
import pytest
from fontTools.ttLib import TTFont
from pypdf import PdfReader
from test_general_agent_ga01 import spec
from test_general_agent_ga02 import capability, grant
from test_general_agent_ga08_google import ACCOUNT, PROVIDER, connector, event

from mgar_v3.domain import digest
from mgar_v3.effects.dispatcher import GeneralActionDispatcher
from mgar_v3.general_agent.actions import ActionApproval, ActionScope
from mgar_v3.general_agent.capabilities import CapabilityRegistry
from mgar_v3.general_agent.contracts import SourceRef, TaskPlan
from mgar_v3.general_agent.office import (
    ChangeCalendar,
    OfficeExecutor,
    OfficeGrant,
    OfficeResource,
    OfficeScope,
)
from mgar_v3.general_agent.research_pdf import UnsupportedPDFGlyph, report_pdf, verified_pdf_text
from mgar_v3.general_agent.verification_checks import check_content
from mgar_v3.general_agent.verification_contracts import Citation


def test_ga06_supplementary_glyphs_are_visible_and_support_the_same_citation(engine):
    quote = "𠮷野发布了趋势 📈"
    url = "https://research.example.test/trend"
    report = f"# {quote}\n\n{url}\n"
    pdf = report_pdf(report)
    page = PdfReader(io.BytesIO(pdf)).pages[0]
    assert quote in page.extract_text() and "\ufffd" not in page.extract_text()
    visible = set()
    for resource in page["/Resources"]["/Font"].values():
        font = resource.get_object()
        cmap = font["/ToUnicode"].get_data()
        cid_font = font["/DescendantFonts"][0].get_object()
        mapping = cid_font["/CIDToGIDMap"].get_data()
        embedded = cid_font["/FontDescriptor"]["/FontFile2"].get_data()
        with TTFont(io.BytesIO(embedded)) as outline:
            for char in "𠮷📈":
                code = char.encode("utf-16-be").hex().upper().encode()
                match = re.search(rb"^<([A-F0-9]+)> <" + code + rb">$", cmap, re.MULTILINE)
                if match is None:
                    continue
                cid = int(match[1], 16)
                glyph = outline.getBestCmap()[ord(char)]
                assert (
                    int.from_bytes(mapping[cid * 2 : cid * 2 + 2], "big")
                    == outline.getGlyphID(glyph)
                    > 0
                )
                assert outline["glyf"][glyph].numberOfContours != 0
                visible.add(char)
    assert visible == {"𠮷", "📈"}
    assert verified_pdf_text(pdf) == report.encode()
    task = engine.general_tasks.create("research", None, uuid4(), spec())
    step = Citation(
        id="source",
        kind="citation",
        output_id="result",
        source=SourceRef(kind="web", reference=url, version="sha256:" + "a" * 64),
        source_sha256="b" * 64,
        excerpt=quote,
    )
    for content in (report.encode(), pdf):
        assert check_content(task, step, quote.encode(), content, "fixture-verifier")
        assert not check_content(task, step, b"Different source text", content, "fixture-verifier")
    assert not check_content(task, step, quote.encode(), pdf + b"tampered", "fixture-verifier")
    with pytest.raises(UnsupportedPDFGlyph, match=r"U\+10FFFF"):
        report_pdf(report + "\U0010ffff")


def test_ga08_split_project_calendar_ids_and_reconcile_legacy_without_resending(engine):
    events: dict[str, dict] = {}
    posts = []
    lookups = []

    def provider(request):
        if request.method == "POST":
            payload = json.loads(request.content)
            external_id = payload["id"]
            posts.append(external_id)
            if external_id in events:
                return httpx.Response(409, json={"error": "event ID already exists"})
            events[external_id] = payload
            # Lose the second acknowledgement after creating the event.
            return httpx.Response(409 if len(posts) == 2 else 200, json=payload)
        assert request.method == "GET"
        external_id = request.url.path.rsplit("/", 1)[1]
        lookups.append(external_id)
        if external_id not in events:
            return httpx.Response(404, json={"error": "missing"})
        return httpx.Response(200, json=events[external_id])

    resource = OfficeResource(kind="calendar", external_id="primary")
    change = ChangeCalendar(operation="create_event", resource=resource, event=event())
    descriptor = capability("office.calendar", provider_id=PROVIDER, effect="external_write")
    engine.general_graphs.registry = CapabilityRegistry([descriptor])
    office = OfficeExecutor(engine, connector(provider))
    store = engine.general_actions

    def approve(project, key):
        task = engine.general_tasks.create(
            project,
            None,
            uuid4(),
            spec("office").model_copy(
                update={"allowed_capabilities": (descriptor.id,), "approval_policy": "manual"}
            ),
        )
        plan = engine.general_tasks.save_plan(
            task.task_id,
            1,
            1,
            TaskPlan(task_revision=1, steps=("Approve calendar event",)),
        )
        task = engine.general_tasks.transition(task.task_id, 1, plan.version, "running")
        scope = ActionScope(
            actor="owner",
            capability=descriptor.id,
            provider_id=PROVIDER,
            target=resource.target(ACCOUNT),
            operation="create_event",
            effect="external_write",
            mode="fixture",
            compensable=True,
        )
        authority = OfficeGrant(
            account=ACCOUNT,
            actor="owner",
            project_id=project,
            scopes=(OfficeScope(operation="create_event", resource=resource),),
            expires_at=datetime.now(UTC) + timedelta(minutes=10),
        )
        lease = store.issue_lease(scope, grant(task, descriptor), task.plan_version)
        record = office.queue(authority, lease, change, idempotency_key=key)
        return store.approve(
            record.intent.action_id,
            ActionApproval(
                intent_sha256=record.intent_sha256,
                lease_id=lease.lease_id,
                epoch=lease.epoch,
                reviewer="fixture-reviewer",
                level="operator",
            ),
        ).intent

    north = approve("north", "same-key")
    south = approve("south", "same-key")
    dispatcher = GeneralActionDispatcher(engine, office)
    assert dispatcher.tick() == north.action_id
    assert dispatcher.tick() == south.action_id
    assert len(set(posts)) == 2
    assert store.get(south.action_id).status == "unknown"
    assert dispatcher.reconcile(south.action_id)
    assert [store.get(item.action_id).receipt.external_id for item in (north, south)] == posts
    assert lookups == [posts[1]]

    legacy = approve("legacy", "same-key")
    attempt = store.claim(legacy.action_id, None)
    assert attempt is not None
    assert store.result(legacy.action_id, attempt).status == "unknown"
    legacy_id = digest(
        {
            "provider": PROVIDER,
            "account": ACCOUNT,
            "target": legacy.scope.target.reference,
            "key": legacy.idempotency_key,
        }
    )
    legacy_event = events[legacy_id] = {
        "id": legacy_id,
        "extendedProperties": {"private": {"magr_action": north.identity()}},
    }
    # A historic ID shared across projects is only evidence for the exact original intent.
    assert not dispatcher.reconcile(legacy.action_id)
    assert store.get(legacy.action_id).status == "unknown"
    legacy_event["extendedProperties"]["private"]["magr_action"] = legacy.identity()
    assert dispatcher.reconcile(legacy.action_id)
    assert store.get(legacy.action_id).receipt.external_id == legacy_id
    assert len(posts) == 2
