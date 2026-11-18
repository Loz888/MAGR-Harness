"""Absence requires a complete, unambiguous Docker missing-container response."""

from subprocess import CompletedProcess

import pytest
from harness_docker_ownership import DockerOwnership

IDENTIFIER = "a" * 64


@pytest.mark.parametrize(
    "message",
    [
        f"error: no such object: {IDENTIFIER}",
        f"Error: No such object: {IDENTIFIER}\n",
        f"Error: No such container: {IDENTIFIER}",
        f"Error response from daemon: No such container: {IDENTIFIER}",
    ],
)
def test_exact_missing_container_response_accepts_docker_case_variants(message: str) -> None:
    result = CompletedProcess(["docker", "inspect"], 1, stdout=b"\n", stderr=message.encode())
    assert DockerOwnership._inspect(lambda *args, **kwargs: result, IDENTIFIER) is None


@pytest.mark.parametrize(
    "returncode,stdout,message",
    [
        (1, b"", "Cannot connect to the Docker daemon"),
        (1, b"", f"daemon unavailable\nerror: no such object: {IDENTIFIER}"),
        (1, b"", f"error response from daemon: unavailable\nerror: no such object: {IDENTIFIER}"),
        (1, b"[]\n", f"error: no such object: {IDENTIFIER}"),
        (1, b"partial response", f"error response from daemon: no such container: {IDENTIFIER}"),
        (1, b"", "error: no such object: " + "b" * 64),
        (125, b"", f"error: no such object: {IDENTIFIER}"),
    ],
)
def test_daemon_or_mixed_responses_do_not_prove_absence(
    returncode: int,
    stdout: bytes,
    message: str,
) -> None:
    result = CompletedProcess(
        ["docker", "inspect"],
        returncode,
        stdout=stdout,
        stderr=message.encode(),
    )
    with pytest.raises(AssertionError, match="rather than proving absence"):
        DockerOwnership._inspect(lambda *args, **kwargs: result, IDENTIFIER)
