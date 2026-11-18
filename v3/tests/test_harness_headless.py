import httpx
import pytest

from mgar_v3.harness.client import HarnessAPIError, HarnessClient
from mgar_v3.harness.service import HarnessCodingService

UPDATE = 'id: 12\nevent: update\ndata: {"cursor":12,"kind":"task_started"}\n\n'


@pytest.mark.parametrize(
    ("tail", "expected"),
    [
        ("", "EVENT_STREAM_INTERRUPTED"),
        ("event: auth_required\ndata: {}\n\n", "AUTH_REQUIRED"),
        ('id: 12\nevent: update\ndata: {"cursor":12}\n\n', "INVALID_API_RESPONSE"),
        ('id: 14\nevent: update\ndata: {"cursor":13}\n\n', "INVALID_API_RESPONSE"),
    ],
)
def test_event_stream_reports_disconnect_expiry_and_bad_cursor(tail, expected):
    def handle(request):
        assert request.headers["Last-Event-ID"] == "10"
        return httpx.Response(
            200, text=UPDATE + tail, headers={"Content-Type": "text/event-stream; charset=utf-8"}
        )

    with HarnessClient("test-token", transport=httpx.MockTransport(handle)) as sdk:
        frames = sdk.events("task-1", after=10)
        assert next(frames)["cursor"] == 12
        with pytest.raises(HarnessAPIError) as error:
            next(frames)
        assert error.value.code == expected


@pytest.mark.parametrize(
    "api",
    [
        "https://example.com",
        "http://127.0.0.1:18080/api",
        "ftp://localhost",
        "http://person@127.0.0.1",
        "http://localhost?redirect=example.com",
    ],
)
def test_operator_token_is_only_configured_for_loopback_origins(api):
    with pytest.raises(ValueError, match="loopback"):
        HarnessClient("test-token", api=api)


def test_redirect_is_not_followed_with_operator_credentials():
    count = 0

    def handle(request):
        nonlocal count
        count += 1
        assert request.url.host == "localhost"
        assert request.headers["X-MGAR-Token"] == "test-token"
        return httpx.Response(307, headers={"Location": "https://example.com"})

    with HarnessClient(
        "test-token", api="http://localhost", transport=httpx.MockTransport(handle)
    ) as sdk:
        with pytest.raises(HarnessAPIError) as error:
            sdk.projects()
        assert error.value.status_code == 307 and count == 1


def test_terminal_page_drains_events_when_the_worker_finishes_between_reads(tmp_path, monkeypatch):
    service = HarnessCodingService(
        tmp_path / "harness", actor="owner", image_lock=tmp_path / "unused-lock.json"
    )
    last_event = {"cursor": 7, "kind": "artifact_ready"}
    events = []

    class FinishingSink:
        def page(self, *, after_cursor, limit):
            return list(events), events[-1]["cursor"] if events else after_cursor, False

    def finish_and_read_status(task_id):
        events.append(last_event)
        return "completed"

    monkeypatch.setattr(service, "_task_exists", lambda _task_id: True)
    monkeypatch.setattr(service, "_event_sink", lambda _task_id: FinishingSink())
    monkeypatch.setattr(service, "_current_status", finish_and_read_status)
    page = service.event_page("h08-000000000000", after=6)
    assert page["terminal"] and page["events"] == [last_event]
    assert page["next_cursor"] == 7
