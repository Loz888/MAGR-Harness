"""R00 freezes evidence and reproduces protocol observations without paid execution."""

import copy
import json
import subprocess
from pathlib import Path

import httpx
import pytest

from scripts.h12_r00 import (
    ReadonlyArchive,
    digest_bytes,
    evidence_status,
    exported_task_metadata,
    replay,
    verify_archive,
)
from scripts.h12_r00_freeze import archived_source, is_cas_object
from scripts.verify import source_fingerprint

FIXTURES = Path(__file__).parent / "fixtures/h12_r00/diagnostic-fixtures/public"


@pytest.mark.parametrize("fixture", sorted(FIXTURES.glob("*.json")), ids=lambda p: p.stem)
def test_eight_public_diagnostics_reproduce_without_original_private_data(fixture):
    data = json.loads(fixture.read_bytes())
    original = copy.deepcopy(data)
    observed = replay(data)
    assert observed["issue_observed"] is True
    assert data == original


@pytest.mark.parametrize(
    "filename,changes",
    [
        ("contract-loss", {"projected_ids": ["http_response", "observed_status_stdout"]}),
        ("output-truncated", {"finish_reason": "stop"}),
        ("invalid-tool-call", {"arguments": '{"path":"source.py"}'}),
        ("path-denied", {"path": "source.py"}),
        ("ready-missing", {"stdout": "Ready at port 31337\n"}),
        ("regression-not-run", {"independent_receipt": {"tests_run": 42}}),
        ("feature-red-baseline", {"compiler_output": "syntax error\nFAIL\n"}),
        ("refactor-structure", {"legacy_baseline_required_new_structure": False}),
    ],
)
def test_diagnostic_negative_controls_do_not_echo_the_failure(filename, changes):
    data = json.loads((FIXTURES / (filename + ".json")).read_bytes())
    data["observation"].update(changes)
    assert replay(data)["issue_observed"] is False


@pytest.mark.parametrize(
    "attempted,blocked,verified,expected",
    [
        (False, False, None, "NOT_RUN"),
        (False, True, None, "BLOCKED"),
        (True, True, None, "BLOCKED"),
        (True, False, None, "BLOCKED"),
        (True, False, False, "FAIL"),
        (True, False, True, "PASS"),
    ],
)
def test_evidence_states_never_turn_unexecuted_or_unknown_into_pass(
    attempted, blocked, verified, expected
):
    assert evidence_status(attempted=attempted, blocked=blocked, verified=verified) == expected


def test_unattempted_check_cannot_claim_a_result():
    with pytest.raises(ValueError):
        evidence_status(attempted=False, verified=True)


def test_private_data_is_rejected_by_public_fixture_replay():
    with pytest.raises(ValueError):
        replay({"kind": "contract_loss", "visibility": "controller_private", "observation": {}})


def test_exported_task_metadata_is_kept_separate_from_local_registration_metadata():
    task = {"task_id": "synthetic", "repository": "example/project", "base_commit": "a" * 40}
    assert exported_task_metadata({"task": task}) == task
    assert exported_task_metadata({"registration": task}) is None
    assert exported_task_metadata({"task": {"task_id": "synthetic"}}) is None


def test_archive_preserves_original_failure_bytes_and_uses_readonly_objects(tmp_path):
    original = tmp_path / "original-score.json"
    original.write_text('{"status":"NOT_RUN","cost_micros":0}\n')
    before = original.read_bytes()
    mode = original.stat().st_mode
    archive = ReadonlyArchive(tmp_path / "attempt")
    row = archive.add_file(original, category="score")
    archive.seal()
    assert verify_archive(archive.attempt)["status"] == "PASS"
    assert original.read_bytes() == before and original.stat().st_mode == mode
    assert row["sha256"] == digest_bytes(before)
    assert not (archive.attempt / row["archive_path"]).stat().st_mode & 0o222


@pytest.mark.parametrize(
    "variant", ["corrupt", "missing", "writable", "escape", "schema", "duplicate"]
)
def test_tampered_or_missing_archival_evidence_is_never_pass(tmp_path, variant):
    archive = ReadonlyArchive(tmp_path / "attempt")
    row = archive.add(b"historical FAIL", origin="fixture://score", category="score")
    manifest = archive.seal()
    target = archive.attempt / row["archive_path"]
    if variant in {"corrupt", "writable"}:
        target.chmod(0o600)
        if variant == "corrupt":
            target.write_bytes(b"altered")
            target.chmod(0o400)
    elif variant == "missing":
        target.parent.chmod(0o700)
        target.unlink()
    else:
        manifest.chmod(0o600)
        data = json.loads(manifest.read_bytes())
        if variant == "escape":
            data["entries"][0]["archive_path"] = "../original"
        elif variant == "schema":
            data["schema"] = "unknown"
        else:
            data["entries"].append(data["entries"][0])
        manifest.write_text(json.dumps(data))
        manifest.chmod(0o400)
    assert verify_archive(archive.attempt)["status"] == "FAIL"


def test_archive_never_follows_an_original_symlink_or_overwrites_a_changed_origin(tmp_path):
    original = tmp_path / "original"
    original.write_bytes(b"original failure")
    link = tmp_path / "link"
    link.symlink_to(original)
    archive = ReadonlyArchive(tmp_path / "attempt")
    with pytest.raises(ValueError):
        archive.add_file(link, category="score")
    archive.add_file(original, category="score")
    original.write_bytes(b"changed outside collector")
    with pytest.raises(ValueError):
        archive.add_file(original, category="score")


def test_missing_and_empty_archives_are_blocked_or_not_run(tmp_path):
    assert verify_archive(tmp_path)["status"] == "BLOCKED"
    archive = ReadonlyArchive(tmp_path / "empty")
    archive.seal()
    assert verify_archive(archive.attempt)["status"] == "NOT_RUN"


def test_git_source_freeze_binds_original_bytes_not_the_modified_worktree(tmp_path):
    project = tmp_path / "project"
    app = project / "v3"
    (app / "src").mkdir(parents=True)
    (app / "src/sample.py").write_text("value = 1\n")
    (app / "uv.lock").write_text("fixture dependency lock\n")
    (app / "pyproject.toml").write_text("[project]\nname='fixture'\n")
    subprocess.run(["git", "init", "-q", str(project)], check=True)
    subprocess.run(["git", "-C", str(project), "add", "."], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(project),
            "-c",
            "user.name=R00",
            "-c",
            "user.email=r00@localhost",
            "commit",
            "-qm",
            "baseline",
        ],
        check=True,
    )
    expected = source_fingerprint(app)
    (app / "src/sample.py").write_text("value = 2\n")
    _, frozen = archived_source(app, "HEAD")
    assert frozen["source_sha256"] == expected != source_fingerprint(app)


@pytest.mark.parametrize(
    "parent,name,expected",
    [
        ("aa", "a" * 64, True),
        ("bb", "a" * 64, False),
        ("first-model-recovery-locks", "a" * 64 + ".lock", False),
    ],
)
def test_cas_objects_and_non_cas_recovery_locks_are_distinguished(parent, name, expected):
    assert is_cas_object(Path(parent) / name) is expected


@pytest.mark.parametrize("invalid", [False, True])
def test_actual_adapter_keeps_length_and_truncated_tool_arguments_non_executable(tmp_path, invalid):
    from test_harness_product_models import request

    from mgar_v3.evidence.artifacts import ArtifactStore
    from mgar_v3.harness.contracts import CancellationToken
    from mgar_v3.harness.product_models import ProductDeepSeekChatAdapter

    calls = []
    arguments = '{"path":"source.py"' if invalid else '{"path":"source.py"}'
    wire = [
        {
            "choices": [
                {
                    "delta": {
                        "tool_calls": [
                            {
                                "index": 0,
                                "id": "synthetic",
                                "type": "function",
                                "function": {"name": "github__read_file", "arguments": arguments},
                            }
                        ]
                    }
                }
            ]
        },
        {"choices": [{"delta": {}, "finish_reason": "length"}]},
        {"choices": [], "usage": {"prompt_tokens": 40, "completion_tokens": 8192}},
    ]
    payload = (
        "".join("data: " + json.dumps(v) + "\n\n" for v in wire) + "data: [DONE]\n\n"
    ).encode()

    def transport(req):
        calls.append(req)
        return httpx.Response(200, content=payload)

    adapter = ProductDeepSeekChatAdapter(
        api_key="r00-controlled-fixture",
        artifacts=ArtifactStore(tmp_path / "cas"),
        client=httpx.Client(transport=httpx.MockTransport(transport)),
    )
    response = adapter.complete(request(), CancellationToken())
    assert len(calls) == 1 and not response.tool_calls and response.usage.known
    assert response.finish_reason == ("error" if invalid else "length")
    if invalid:
        assert response.error_code == "MODEL_TOOL_ARGUMENTS_INVALID"
