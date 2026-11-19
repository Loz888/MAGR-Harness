"""Account-bound adapter for official Google Workspace APIs."""

from __future__ import annotations

import json
from base64 import urlsafe_b64encode
from datetime import UTC, datetime
from email import policy
from email.message import EmailMessage
from typing import Any
from urllib.parse import quote

import httpx
from pydantic import SecretStr, TypeAdapter

from mgar_v3.domain import DomainError, digest
from mgar_v3.effects.dispatcher import DefinitiveFailure
from mgar_v3.general_agent.actions import ActionIntent, ActionMode
from mgar_v3.general_agent.contracts import Identifier
from mgar_v3.general_agent.office import (
    ChangeCalendar,
    DraftMail,
    Email,
    OfficeChange,
    OfficeResource,
    OfficeSnapshot,
    SendMail,
)

_MAIL = "https://gmail.googleapis.com/gmail/v1/users/me/messages"
_CALENDAR = "https://www.googleapis.com/calendar/v3/calendars"
_DOCS = "https://docs.googleapis.com/v1/documents"
_SHEETS = "https://sheets.googleapis.com/v4/spreadsheets"
_MAX_RESPONSE_BYTES = 512_000
_ID = TypeAdapter(Identifier)


class _Missing(Exception):
    pass


def _unavailable() -> DomainError:
    return DomainError("OFFICE_PROVIDER_READ", "Google Workspace read did not complete.", 502)


def _identifier(item: object) -> str:
    return _ID.validate_python(item, strict=True)


def _segment(value: str) -> str:
    # Dot segments are not resource identifiers and can change the resolved request path.
    if value in {".", ".."}:
        raise ValueError("Invalid Google Workspace resource segment.")
    return quote(value, safe="")


def _calendar_path(resource: OfficeResource) -> str:
    calendar = resource.parent_id or resource.external_id
    base = f"{_CALENDAR}/{_segment(calendar)}"
    return f"{base}/events/{_segment(resource.external_id)}" if resource.parent_id else base


def _calendar_tag(event: dict[str, Any]) -> str:
    etag = event.get("etag")
    if (
        not isinstance(etag, str)
        or len(etag) < 3
        or len(etag) > 256
        or not etag.startswith('"')
        or not etag.endswith('"')
        or '"' in etag[1:-1]
        or not etag.isascii()
        or any(ord(ch) < 0x21 or ord(ch) > 0x7E for ch in etag)
    ):
        raise _unavailable()
    return etag


class GoogleOfficeConnector:
    """OAuth Bearer adapter. Mutations are entered only through the GA04 dispatcher."""

    def __init__(
        self,
        *,
        provider_id: str,
        account: str,
        sender_email: str,
        oauth_access_token: SecretStr,
        mode: ActionMode,
        test_transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.provider_id = _identifier(provider_id)
        self.account = _identifier(account)
        self.sender_email = TypeAdapter(Email).validate_python(sender_email)
        if (mode == "fixture") != (test_transport is not None) or mode not in {"fixture", "live"}:
            raise ValueError("Google Workspace fixture mode requires an injected transport.")
        token = oauth_access_token.get_secret_value()
        if (
            not token
            or not token.isascii()
            or any(ord(ch) < 0x21 or ord(ch) > 0x7E for ch in token)
        ):
            raise ValueError("Invalid Google Workspace OAuth credential.")
        self.mode: ActionMode = mode
        self._token = oauth_access_token
        self._test_transport = test_transport

    def _request(
        self,
        method: str,
        url: str,
        *,
        params: dict[str, str] | None = None,
        payload: dict[str, Any] | None = None,
        if_match: str | None = None,
    ) -> dict[str, Any]:
        headers = {
            "Authorization": "Bearer " + self._token.get_secret_value(),
            "Accept": "application/json",
            "Accept-Encoding": "identity",
        }
        if if_match:
            headers["If-Match"] = if_match
        try:
            with (
                httpx.Client(
                    transport=self._test_transport,
                    follow_redirects=False,
                    trust_env=False,
                    timeout=httpx.Timeout(12, connect=4),
                ) as client,
                client.stream(
                    method, url, headers=headers, params=params, json=payload
                ) as response,
            ):
                status = response.status_code
                if method == "GET" and status == 404:
                    raise _Missing()
                if status not in {200, 201}:
                    if method != "GET" and status in {400, 401, 403, 404, 412}:
                        raise DefinitiveFailure("Google Workspace rejected the operation.")
                    raise _unavailable()
                if response.headers.get("content-encoding", "identity").lower() != "identity":
                    raise _unavailable()
                body = bytearray()
                for chunk in response.iter_bytes():
                    body.extend(chunk)
                    if len(body) > _MAX_RESPONSE_BYTES:
                        raise _unavailable()
                parsed = json.loads(body)
                if isinstance(parsed, dict):
                    return parsed
        except (httpx.HTTPError, OSError, ValueError, DomainError, RecursionError):
            pass
        if method != "GET":
            # Preserve unknown disposition after any ambiguous write acknowledgement.
            raise TimeoutError("Google Workspace write outcome requires reconciliation.") from None
        raise _unavailable() from None

    def read(self, resource: OfficeResource) -> OfficeSnapshot:
        identifier = _segment(resource.external_id)
        try:
            if resource.kind == "calendar":
                content = self._request("GET", _calendar_path(resource))
                if content.get("id") != resource.external_id and not (
                    not resource.parent_id and resource.external_id == "primary"
                ):
                    raise _unavailable()
            elif resource.kind == "email":
                if resource.external_id == "outbound":
                    raise _unavailable()
                content = self._request("GET", f"{_MAIL}/{identifier}", params={"format": "full"})
                if content.get("id") != resource.external_id:
                    raise _unavailable()
            elif resource.kind == "document":
                content = self._request("GET", f"{_DOCS}/{identifier}")
                if content.get("documentId") != resource.external_id:
                    raise _unavailable()
            else:
                if resource.cell_range:
                    content = self._request(
                        "GET", f"{_SHEETS}/{identifier}/values/{_segment(resource.cell_range)}"
                    )
                    if not isinstance(content.get("range"), str):
                        raise _unavailable()
                else:
                    content = self._request(
                        "GET", f"{_SHEETS}/{identifier}", params={"includeGridData": "false"}
                    )
                    if content.get("spreadsheetId") != resource.external_id:
                        raise _unavailable()
        except _Missing:
            raise DomainError(
                "OFFICE_PROVIDER_MISSING", "Google Workspace item was not found.", 404
            ) from None
        version = (
            digest({"etag": _calendar_tag(content)})
            if resource.kind == "calendar"
            else digest(content)
        )
        return OfficeSnapshot(
            resource=resource,
            version=version,
            observed_at=datetime.now(UTC),
            content=content,
        )

    def _bound_resource(self, intent: ActionIntent) -> OfficeResource:
        parts = intent.scope.target.reference.split("/")
        if (
            len(parts) != 4
            or parts[0] not in {"email", "calendar", "document", "spreadsheet"}
            or parts[1] != self.account
            or intent.scope.provider_id != self.provider_id
            or intent.scope.mode != self.mode
        ):
            raise DefinitiveFailure("Office action is outside this Google account.")
        kind, _, parent, identifier = parts
        try:
            resource = OfficeResource.model_validate(
                {
                    "kind": kind,
                    "parent_id": None if parent == "-" else parent,
                    "external_id": identifier,
                }
            )
            _segment(identifier)
            if resource.parent_id is not None:
                _segment(resource.parent_id)
        except ValueError:
            raise DefinitiveFailure("Office action has an invalid Google resource.") from None
        if resource.target(self.account) != intent.scope.target:
            raise DefinitiveFailure("Office action is outside this Google resource.")
        return resource

    def observe(self, intent: ActionIntent) -> str | None:
        resource = self._bound_resource(intent)
        if (
            resource.kind != "calendar"
            or not resource.parent_id
            or intent.scope.operation != "update_event"
        ):
            return None
        try:
            return self.read(resource).version
        except DomainError as exc:
            if exc.code != "OFFICE_PROVIDER_MISSING":
                raise
        return None

    def _legacy_effect_id(self, intent: ActionIntent) -> str:
        return digest(
            {
                "provider": self.provider_id,
                "account": self.account,
                "target": intent.scope.target.reference,
                "key": intent.idempotency_key,
            }
        )

    def _message_id(self, intent: ActionIntent) -> str:
        return f"<{self._legacy_effect_id(intent)}@magr.invalid>"

    @staticmethod
    def _calendar_event_id(intent: ActionIntent) -> str:
        # The key is project-scoped in the outbox; the durable Action ID is global.
        return digest({"google_calendar_action": str(intent.action_id)})

    def _send_mail(self, intent: ActionIntent, draft: DraftMail) -> str:
        if intent.precondition_sha256 is not None:
            raise DefinitiveFailure("Google send does not support conditional preconditions.")
        msg = EmailMessage(policy=policy.SMTP)
        msg["From"] = self.sender_email
        msg["To"] = ", ".join(draft.to)
        if draft.cc:
            msg["Cc"] = ", ".join(draft.cc)
        if draft.bcc:
            msg["Bcc"] = ", ".join(draft.bcc)
        msg["Message-ID"] = self._message_id(intent)
        msg["Subject"] = draft.subject
        msg.set_content(draft.body)
        body = self._request(
            "POST", f"{_MAIL}/send", payload={"raw": urlsafe_b64encode(msg.as_bytes()).decode()}
        )
        try:
            return _identifier(body.get("id"))
        except ValueError:
            raise TimeoutError("Google mail receipt requires reconciliation.") from None

    def _event_fields(self, change: ChangeCalendar, intent: ActionIntent) -> dict[str, Any]:
        event = change.event
        fields = {
            "summary": event.title,
            "start": {"dateTime": event.start.isoformat(), "timeZone": event.timezone},
            "end": {"dateTime": event.end.isoformat(), "timeZone": event.timezone},
            "attendees": [{"email": attendee} for attendee in event.attendees],
            "extendedProperties": {"private": {"magr_action": intent.identity()}},
        }
        if event.recurrence is not None:
            fields["recurrence"] = [
                f"RRULE:FREQ={event.recurrence.frequency};COUNT={event.recurrence.count}"
            ]
        return fields

    def _write_calendar(self, change: ChangeCalendar, intent: ActionIntent) -> str:
        resource = change.resource
        fields = self._event_fields(change, intent)
        if change.operation == "create_event":
            if resource.parent_id is not None or intent.precondition_sha256 is not None:
                raise DefinitiveFailure("Google calendar insertion requires a new event ID.")
            event_id = self._calendar_event_id(intent)
            fields["id"] = event_id
            body = self._request(
                "POST",
                f"{_calendar_path(resource)}/events",
                payload=fields,
                params={"sendUpdates": "none"},
            )
        else:
            if resource.parent_id is None or intent.precondition_sha256 is None:
                raise DefinitiveFailure("Google calendar update requires a scoped event version.")
            event_id = resource.external_id
            try:
                existing = self._request("GET", _calendar_path(resource))
                etag = _calendar_tag(existing)
            except (_Missing, DomainError):
                raise DefinitiveFailure("Google calendar version could not be verified.") from None
            if (
                existing.get("id") != event_id
                or digest({"etag": etag}) != intent.precondition_sha256
            ):
                raise DefinitiveFailure("Google calendar event changed before dispatch.")
            if change.event.recurrence is None and existing.get("recurrence"):
                fields["recurrence"] = []
            body = self._request(
                "PATCH",
                _calendar_path(resource),
                params={"sendUpdates": "none"},
                payload=fields,
                if_match=etag,
            )
        if body.get("id") != event_id or self._action_marker(body) != intent.identity():
            raise TimeoutError("Google calendar receipt requires reconciliation.")
        return event_id

    def write(self, change: OfficeChange, intent: ActionIntent, draft: DraftMail | None) -> str:
        resource = self._bound_resource(intent)
        if change.resource != resource or intent.scope.operation != change.operation:
            raise DefinitiveFailure("Google action payload does not match its approved intent.")
        if (
            isinstance(change, SendMail)
            and draft is not None
            and resource.kind == "email"
            and resource.external_id == "outbound"
        ):
            return self._send_mail(intent, draft)
        if isinstance(change, ChangeCalendar) and resource.kind == "calendar":
            return self._write_calendar(change, intent)
        raise DefinitiveFailure("Google adapter has no safe conditional write for this operation.")

    @staticmethod
    def _action_marker(body: dict[str, Any]) -> object:
        extended = body.get("extendedProperties")
        private = extended.get("private") if isinstance(extended, dict) else None
        return private.get("magr_action") if isinstance(private, dict) else None

    def _find_mail(self, intent: ActionIntent) -> str | None:
        expected = self._message_id(intent)
        found = self._request(
            "GET",
            _MAIL,
            params={"q": f"rfc822msgid:{expected}", "labelIds": "SENT", "maxResults": "2"},
        )
        messages = found.get("messages")
        if (
            found.get("nextPageToken")
            or not isinstance(messages, list)
            or len(messages) != 1
            or not isinstance(messages[0], dict)
        ):
            return None
        try:
            message_id = _identifier(messages[0].get("id"))
        except ValueError:
            return None
        message = self._request(
            "GET",
            f"{_MAIL}/{_segment(message_id)}",
            params={"format": "metadata", "metadataHeaders": "Message-ID"},
        )
        payload = message.get("payload")
        headers = payload.get("headers") if isinstance(payload, dict) else None
        labels = message.get("labelIds")
        if message.get("id") != message_id or not isinstance(labels, list) or "SENT" not in labels:
            return None
        if not isinstance(headers, list):
            return None
        ids = [
            row.get("value")
            for row in headers
            if isinstance(row, dict) and str(row.get("name", "")).casefold() == "message-id"
        ]
        return message_id if ids == [expected] else None

    def find(self, intent: ActionIntent) -> str | None:
        resource = self._bound_resource(intent)
        try:
            if intent.scope.operation == "send" and resource.kind == "email":
                return self._find_mail(intent)
            if resource.kind != "calendar":
                return None
            if intent.scope.operation == "create_event" and resource.parent_id is None:
                base = f"{_calendar_path(resource)}/events"
                event_id = self._calendar_event_id(intent)
                try:
                    body = self._request("GET", f"{base}/{event_id}")
                except _Missing:
                    # A write made by an older dispatcher may still be awaiting a receipt.
                    event_id = self._legacy_effect_id(intent)
                    body = self._request("GET", f"{base}/{event_id}")
            elif intent.scope.operation == "update_event" and resource.parent_id is not None:
                event_id = resource.external_id
                body = self._request("GET", _calendar_path(resource))
            else:
                return None
            if body.get("id") == event_id and self._action_marker(body) == intent.identity():
                return event_id
        except _Missing:
            return None
        return None
