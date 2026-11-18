import httpx
import pytest

from mgar_v3.harness.client import HarnessAPIError, HarnessClient

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
