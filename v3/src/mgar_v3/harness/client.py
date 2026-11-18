"""Python client for the same Harness V3 routes used by the Web interface."""

from __future__ import annotations

import json
import re
from collections.abc import Iterator
from types import TracebackType
from typing import Any, Self
from urllib.parse import urlsplit

import httpx

from mgar_v3.harness.runner_contract import RUNNER_API

JsonObject = dict[str, Any]
DEFAULT_API = "http://127.0.0.1:18080"
SESSION_EXPORT_VERSION = "mgar.harness.session.v1"
_RESOURCE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,99}")
_AGENT = "/api/v3/agent"
_HARNESS = "/api/v3/harness"


class HarnessAPIError(Exception):
    """The server rejected the request or returned an invalid protocol response."""

    def __init__(self, code: str, message: str, status_code: int = 0, request_id: str = "") -> None:
        super().__init__(message)
        self.code = code
        self.status_code = status_code
        self.request_id = request_id


def _resource(collection: str, value: str) -> str:
    if not _RESOURCE_ID.fullmatch(value):
        raise ValueError("Invalid Harness resource identifier")
    return f"{collection}/{value}"


def _check(response: httpx.Response) -> None:
    if response.is_success:
        return
    response.read()
    try:
        detail = response.json()
    except ValueError:
        detail = {}
    if not isinstance(detail, dict):
        detail = {}
    raise HarnessAPIError(
        str(detail.get("code", "HTTP_ERROR")),
        str(detail.get("message", f"Harness returned HTTP {response.status_code}")),
        response.status_code,
        str(detail.get("request_id", "")),
    )


def _object(value: Any) -> JsonObject:
    if not isinstance(value, dict):
        raise HarnessAPIError("INVALID_API_RESPONSE", "Expected a JSON object")
    return value


class HarnessClient:
    """Use a local Harness API and the operator token configured for that API."""

    def __init__(
        self,
        token: str,
        *,
        api: str = DEFAULT_API,
        timeout: float = 60.0,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        url = urlsplit(api)
        if (
            url.scheme not in {"http", "https"}
            or url.hostname not in {"localhost", "127.0.0.1", "::1"}
            or url.username is not None
            or url.password is not None
            or url.path not in {"", "/"}
            or url.query
            or url.fragment
        ):
            raise ValueError("Harness API must be a loopback HTTP(S) origin")
        if not token:
            raise ValueError("The Harness operator token is required")
        self._http = httpx.Client(
            base_url=api,
            headers={"X-MGAR-Token": token},
            timeout=timeout,
            transport=transport,
            follow_redirects=False,
            trust_env=False,
        )

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()

    def close(self) -> None:
        self._http.close()

    def _request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        response = self._http.request(method, path, **kwargs)
        _check(response)
        return response

    def _json(self, method: str, path: str, **kwargs: Any) -> Any:
        try:
            return self._request(method, path, **kwargs).json()
        except ValueError as exc:
            raise HarnessAPIError("INVALID_API_RESPONSE", "Expected JSON from Harness") from exc

    def _list(self, path: str) -> list[JsonObject]:
        value = self._json("GET", path)
        if not isinstance(value, list):
            raise HarnessAPIError("INVALID_API_RESPONSE", "Expected a JSON array")
        return [_object(item) for item in value]

    def projects(self) -> list[JsonObject]:
        return self._list(f"{_HARNESS}/projects")

    def issue_runner_pairing(self) -> JsonObject:
        return _object(self._json("POST", f"{RUNNER_API}/pairings"))

    def runner_devices(self) -> list[JsonObject]:
        return self._list(f"{RUNNER_API}/devices")

    def revoke_runner(self, device_id: str) -> JsonObject:
        return _object(self._json("DELETE", _resource(f"{RUNNER_API}/devices", device_id)))

    def runner_workspaces(self) -> list[JsonObject]:
        return self._list(f"{RUNNER_API}/workspaces")

    def queue_runner_job(self, workspace_id: str, *, client_id: str) -> JsonObject:
        return _object(self._json(
            "POST",
            f"{RUNNER_API}/jobs",
            json={"workspace_id": workspace_id, "client_id": client_id},
        ))

    def runner_jobs(self) -> list[JsonObject]:
        return self._list(f"{RUNNER_API}/jobs")

    def runner_events(self, *, after: int = 0) -> list[JsonObject]:
        return self._list(f"{RUNNER_API}/events?after={after}")

    def register_project(self, path: str) -> JsonObject:
        return _object(self._json("POST", f"{_HARNESS}/projects", json={"path": path}))

    def readiness(self, project_id: str) -> JsonObject:
        path = _resource(f"{_AGENT}/projects", project_id)
        return _object(self._json("GET", f"{path}/readiness"))

    def sessions(self) -> list[JsonObject]:
        return self._list(f"{_AGENT}/sessions")

    def submit(self, project_id: str, text: str, *, client_id: str) -> JsonObject:
        return _object(
            self._json(
                "POST",
                f"{_AGENT}/sessions",
                json={
                    "project_id": project_id,
                    "text": text,
                    "client_id": client_id,
                },
            )
        )

    def session(self, session_id: str) -> JsonObject:
        return _object(self._json("GET", _resource(f"{_AGENT}/sessions", session_id)))

    def send(self, session_id: str, text: str, *, client_id: str) -> JsonObject:
        path = _resource(f"{_AGENT}/sessions", session_id)
        return _object(
            self._json(
                "POST",
                f"{path}/messages",
                json={
                    "text": text,
                    "client_id": client_id,
                },
            )
        )

    def export_session(self, session_id: str) -> JsonObject:
        path = _resource(f"{_AGENT}/sessions", session_id)
        result = _object(self._json("GET", f"{path}/export"))
        if result.get("schema_version") != SESSION_EXPORT_VERSION:
            raise HarnessAPIError("UNSUPPORTED_EXPORT_VERSION", "Unknown session export version")
        return result

    def _task_json(self, method: str, task_id: str, suffix: str = "", **kwargs: Any) -> JsonObject:
        path = _resource(f"{_HARNESS}/tasks", task_id)
        return _object(self._json(method, path + suffix, **kwargs))

    def task(self, task_id: str) -> JsonObject:
        return self._task_json("GET", task_id)

    def pause(self, task_id: str) -> JsonObject:
        return self._task_json("POST", task_id, "/pause")

    def resume(self, task_id: str) -> JsonObject:
        return self._task_json("POST", task_id, "/resume")

    def cancel(self, task_id: str, *, reason: str = "") -> JsonObject:
        return self._task_json("POST", task_id, "/cancel", params={"reason": reason})

    def messages(self, task_id: str) -> JsonObject:
        return self._task_json("GET", task_id, "/messages")

    def answer(self, task_id: str, text: str, *, reply_to: str, message_id: str) -> JsonObject:
        return self._task_json(
            "POST",
            task_id,
            "/messages",
            json={
                "kind": "answer",
                "text": text,
                "reply_to": reply_to,
                "message_id": message_id,
            },
        )

    def artifacts(self, task_id: str) -> JsonObject:
        return self._task_json("GET", task_id, "/artifacts")

    def diff(self, task_id: str) -> str:
        return self._request("GET", _resource(f"{_HARNESS}/tasks", task_id) + "/diff").text

    def patch(self, task_id: str) -> bytes:
        return self._request("GET", _resource(f"{_HARNESS}/tasks", task_id) + "/patch").content

    def event_page(self, task_id: str, *, after: int = 0, limit: int = 200) -> JsonObject:
        return self._task_json(
            "GET", task_id, "/events/page", params={"after": after, "limit": limit}
        )

    def events(self, task_id: str, *, after: int = 0, follow: bool = False) -> Iterator[JsonObject]:
        """Yield update/done frames. On disconnect, resume from the last recorded cursor."""
        if after < 0:
            raise ValueError("Event cursor must be nonnegative")
        path = _resource(f"{_HARNESS}/tasks", task_id) + "/events"
        with self._http.stream(
            "GET",
            path,
            params={"follow": str(follow).lower()},
            headers={"Last-Event-ID": str(after)},
        ) as response:
            _check(response)
            if response.headers.get("content-type", "").split(";", 1)[0] != "text/event-stream":
                raise HarnessAPIError("INVALID_API_RESPONSE", "Expected a Harness event stream")
            event, event_id = "", ""
            data: list[str] = []
            for line in response.iter_lines():
                if line:
                    name, _, value = line.partition(":")
                    value = value.removeprefix(" ")
                    if name == "event":
                        event = value
                    elif name == "id":
                        event_id = value
                    elif name == "data":
                        data.append(value)
                    continue
                if data:
                    frame = self._event_frame(event, event_id, data, after)
                    after = frame["cursor"]
                    yield frame
                    if event == "done":
                        return
                event, event_id = "", ""
                data.clear()
        raise HarnessAPIError("EVENT_STREAM_INTERRUPTED", "Resume from the last event cursor")

    @staticmethod
    def _event_frame(event: str, event_id: str, data: list[str], after: int) -> JsonObject:
        if event == "auth_required":
            raise HarnessAPIError("AUTH_REQUIRED", "Harness event authorization expired", 401)
        try:
            payload = _object(json.loads("\n".join(data)))
            cursor = payload["cursor"]
            if type(cursor) is int and (
                (event == "update" and cursor > after and str(cursor) == event_id)
                or (event == "done" and cursor == after)
            ):
                return {"event": event, "cursor": cursor, "data": payload}
        except (ValueError, KeyError):
            pass
        raise HarnessAPIError("INVALID_API_RESPONSE", "Invalid Harness event cursor or frame")
