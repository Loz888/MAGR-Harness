"""GA08 official Google API shapes through an injected credential-free network fixture."""

from __future__ import annotations

import json
from base64 import urlsafe_b64decode
from datetime import datetime
from email import policy
from email.parser import BytesParser
from uuid import uuid4
from zoneinfo import ZoneInfo

import httpx
import pytest
from pydantic import SecretStr

from mgar_v3.domain import DomainError, digest
from mgar_v3.effects.dispatcher import DefinitiveFailure
from mgar_v3.general_agent.actions import ActionIntent, ActionScope
from mgar_v3.general_agent.office import (
    CalendarEvent,
    ChangeCalendar,
    DraftMail,
    OfficeResource,
    Recurrence,
    SendMail,
    ShareOfficeFile,
)
from mgar_v3.general_agent.office_google import GoogleOfficeConnector

PROVIDER = "google-workspace"
ACCOUNT = "fixture-office"
BEARER = "fixture-secret"


def connector(handler):
    def fixture(request):
        assert request.headers["Authorization"] == f"Bearer {BEARER}"
        assert "cookie" not in request.headers
        return handler(request)

    return GoogleOfficeConnector(
        provider_id=PROVIDER,
        account=ACCOUNT,
        sender_email="writer@example.test",
        oauth_access_token=SecretStr(BEARER),
        mode="fixture",
        test_transport=httpx.MockTransport(fixture),
    )


def intent(resource, operation, *, precondition=None):
    return ActionIntent(
        action_id=uuid4(),
        task_id=uuid4(),
        task_revision=1,
        plan_version=2,
        scope=ActionScope(
            actor="operator",
            capability="office.google",
            provider_id=PROVIDER,
            target=resource.target(ACCOUNT),
            operation=operation,
            effect="external_write",
            mode="fixture",
        ),
        expected_effect="Approved office operation",
        idempotency_key="fixture",
        risk="low",
        precondition_sha256=precondition,
    )


def event():
    zone = ZoneInfo("America/New_York")
    return CalendarEvent(
        title="Quarterly review",
        start=datetime(2026, 11, 16, 10, tzinfo=zone),
        end=datetime(2026, 11, 16, 11, tzinfo=zone),
        timezone=zone.key,
        attendees=("guest@example.test",),
        recurrence=Recurrence(frequency="WEEKLY", count=3),
    )


def test_google_reads_only_fixed_official_paths_and_pins_versions():
    read = []

    def request(c):
        read.append((c.method, c.url.host, c.url.path))
        data = {
            "gmail.googleapis.com": {"id": "message-a", "snippet": "Account message"},
            "www.googleapis.com": {"id": "event-a", "etag": '"v1"'},
            "docs.googleapis.com": {"documentId": "doc-a", "title": "Document"},
            "sheets.googleapis.com": {"range": "Sheet1!A1", "values": [["value"]]},
        }
        return httpx.Response(200, json=data[c.url.host])

    g = connector(request)
    for resource in (
        OfficeResource(kind="email", external_id="message-a"),
        OfficeResource(kind="calendar", external_id="event-a", parent_id="primary"),
        OfficeResource(kind="document", external_id="doc-a"),
        OfficeResource(kind="spreadsheet", external_id="sheet-a", cell_range="Sheet1!A1"),
    ):
        snapshot = g.read(resource)
        assert snapshot.resource == resource and BEARER not in snapshot.model_dump_json()
        if resource.kind == "calendar":
            assert snapshot.version == digest({"etag": '"v1"'})
    assert read == [
        ("GET", "gmail.googleapis.com", "/gmail/v1/users/me/messages/message-a"),
        ("GET", "www.googleapis.com", "/calendar/v3/calendars/primary/events/event-a"),
        ("GET", "docs.googleapis.com", "/v1/documents/doc-a"),
        ("GET", "sheets.googleapis.com", "/v4/spreadsheets/sheet-a/values/Sheet1!A1"),
    ]


def test_gmail_write_uses_stable_message_id_and_verify_sent_lookup():
    resource = OfficeResource(kind="email", external_id="outbound")
    approval = intent(resource, "send")
    messages = []
    candidates = [{"id": "returned-mail"}]
    methods = []

    def gmail(request):
        methods.append(request.method)
        if request.method == "POST":
            assert request.url.path.endswith("/messages/send")
            msg = BytesParser(policy=policy.default).parsebytes(
                urlsafe_b64decode(json.loads(request.content)["raw"])
            )
            assert msg["From"] == "writer@example.test"
            assert msg["To"] == "guest@example.test"
            assert msg["Subject"] == "计划评审" and msg.get_content().strip() == "Safe fixture"
            assert BEARER not in msg.as_string()
            messages.append(str(msg["Message-ID"]))
            return httpx.Response(200, json={"id": "returned-mail"})
        if request.url.path.endswith("/messages"):
            assert request.url.params["q"] == f"rfc822msgid:{messages[0]}"
            assert request.url.params["labelIds"] == "SENT"
            return httpx.Response(200, json={"messages": candidates})
        assert request.url.params["metadataHeaders"] == "Message-ID"
        return httpx.Response(
            200,
            json={
                "id": "returned-mail",
                "labelIds": ["SENT"],
                "payload": {"headers": [{"name": "Message-ID", "value": messages[0]}]},
            },
        )

    g = connector(gmail)
    change = SendMail(resource=resource, draft_sha256="1" * 64)
    draft = DraftMail(subject="计划评审", body="Safe fixture", to=("guest@example.test",))
    assert g.write(change, approval, draft) == "returned-mail"
    assert g.find(approval) == "returned-mail"
    candidates.append({"id": "other-mail"})
    assert g.find(approval) is None
    assert methods == ["POST", "GET", "GET", "GET"]


def test_calendar_creation_proves_exact_action_and_recurrence_via_durable_id():
    resource = OfficeResource(kind="calendar", external_id="primary")
    approval = intent(resource, "create_event")
    recorded = {}
    mutations = []

    def calendar(request):
        if request.method == "POST":
            assert request.url.path.endswith("/calendars/primary/events")
            assert request.url.params["sendUpdates"] == "none"
            data = json.loads(request.content)
            assert data["attendees"] == [{"email": "guest@example.test"}]
            assert data["recurrence"] == ["RRULE:FREQ=WEEKLY;COUNT=3"]
            assert data["start"] == {
                "dateTime": "2026-11-16T10:00:00-05:00",
                "timeZone": "America/New_York",
            }
            assert len(data["id"]) == 64
            recorded.update(data)
            mutations.append(request.method)
            # Simulate a conflict/unknown acknowledgement after the provider has the event.
            return httpx.Response(409, json={"error": "not an exact receipt"})
        assert request.method == "GET" and request.url.path.endswith("/" + recorded["id"])
        return httpx.Response(200, json=recorded)

    g = connector(calendar)
    change = ChangeCalendar(operation="create_event", resource=resource, event=event())
    with pytest.raises(TimeoutError):
        g.write(change, approval, None)
    assert g.find(approval) == recorded["id"]
    recorded["extendedProperties"]["private"]["magr_action"] = "another-action"
    assert g.find(approval) is None and mutations == ["POST"]


def test_calendar_update_carries_original_strong_etag_and_refuses_changed_version():
    resource = OfficeResource(kind="calendar", external_id="evt", parent_id="primary")
    versions = {"etag": '"old"'}
    approval = intent(resource, "update_event", precondition=digest(versions))
    patches = []

    def calendar(request):
        assert request.url.path.endswith("/calendars/primary/events/evt")
        if request.method == "GET":
            return httpx.Response(200, json={"id": "evt", **versions})
        assert request.method == "PATCH"
        assert request.headers["If-Match"] == '"old"'
        data = json.loads(request.content)
        patches.append(data)
        return httpx.Response(200, json={"id": "evt", **data})

    g = connector(calendar)
    change = ChangeCalendar(operation="update_event", resource=resource, event=event())
    assert g.observe(approval) == approval.precondition_sha256
    assert g.write(change, approval, None) == "evt"
    versions["etag"] = '"new"'
    assert g.observe(approval) != approval.precondition_sha256
    with pytest.raises(DefinitiveFailure):
        g.write(change, approval, None)
    assert len(patches) == 1


def test_google_never_follows_redirects_or_attempts_unsupported_mutations():
    requests = []

    def redirect(request):
        requests.append((request.method, request.url.host))
        return httpx.Response(302, headers={"Location": "https://untrusted.example/read"})

    g = connector(redirect)
    resource = OfficeResource(kind="document", external_id="doc")
    with pytest.raises(DomainError) as exc:
        g.read(resource)
    assert BEARER not in str(exc.value)
    change = ShareOfficeFile(resource=resource, recipient="guest@example.test")
    with pytest.raises(DefinitiveFailure):
        g.write(change, intent(resource, "share"), None)
    mismatched = intent(resource, "share")
    with pytest.raises(DefinitiveFailure):
        g.find(
            mismatched.model_copy(
                update={
                    "scope": mismatched.scope.model_copy(update={"provider_id": "different"}),
                }
            )
        )
    assert requests == [("GET", "docs.googleapis.com")]
