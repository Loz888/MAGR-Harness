"""Pure feedback and original-CAS-limit regressions for public probe tools."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from test_harness_public_probe import PLAN, output, raw_receipt, task

from mgar_v3.domain import DomainError
from mgar_v3.evidence.artifacts import ArtifactStore
from mgar_v3.harness.contracts import ExecutionReceipt, HarnessToolResult, sha256_text
from mgar_v3.harness.public_probe import normalize_probe_receipt
from mgar_v3.harness.service import _public_probe_source_data, _test_feedback, _test_summary


def derive(raw, *, limit=65536):
    return normalize_probe_receipt(
        PLAN,
        raw,
        task=task(),
        expected_tree=raw.tree_hash,
        log_complete=True,
        log_limit_bytes=limit,
    ).execution


@pytest.mark.parametrize(
    "outcome,expected,run,failed",
    [
        ("green", "passed", 2, 0),
        ("red", "failed", 2, 1),
        ("invalid", "unverified", 0, 0),
    ],
)
def test_final_public_probe_summary_preserves_actual_outcome_and_counts(
    outcome, expected, run, failed
):
    raw = (
        raw_receipt()
        if outcome == "green"
        else raw_receipt(
            output(statuses=("assertion_failed", "passed")), status="failed", exit_code=1
        )
        if outcome == "red"
        else raw_receipt("not a public probe frame\n")
    )
    receipt = derive(raw)
    result = SimpleNamespace(task_kind="bugfix", receipts=[receipt])
    summary = _test_summary(result)
    assert summary["status"] == expected and summary["run"] == run
    assert summary["failed"] == failed and summary["skipped"] == 0
    assert summary["receipt_id"] == receipt.receipt_id
    assert outcome in summary["detail"] and "pytest" not in summary["detail"].lower()
    # Contradictory DTO labels must never produce a green display.
    if outcome == "green":
        contradictory = receipt.model_copy(update={"exit_code": 1})
        assert (
            _test_summary(SimpleNamespace(task_kind="bugfix", receipts=[contradictory]))["status"]
            == "unverified"
        )


def test_million_ascii_tool_payload_fits_original_cas_and_reconstructs_original_raw(tmp_path):
    store = ArtifactStore(tmp_path / "cas")
    assert store.max_bytes == 2_000_000
    text = "A" * 1_000_000
    raw = raw_receipt(text)
    derived = derive(raw, limit=1_048_576)
    assert derived.public_probe["outcome"] == "invalid"
    log_hash = store.put(text.encode())
    source = _public_probe_source_data(raw, {"artifact": {"artifact_hash": log_hash}})
    assert set(source) == {"raw_receipt_metadata", "raw_output_ref"}
    assert "stdout" not in source["raw_receipt_metadata"]
    assert "stderr" not in source["raw_receipt_metadata"]
    payload = HarnessToolResult(
        call_id="large-probe",
        status="error",
        content=_test_feedback("Public probe invalid", text),
        data={"receipt": derived.model_dump(mode="json"), **source},
    ).model_dump(mode="json")
    encoded = json.dumps(payload, ensure_ascii=False).encode()
    assert len(encoded) < store.max_bytes and encoded.count(text.encode()) == 1
    stored = json.loads(store.read(store.put(encoded)))
    data = stored["data"]
    ref = data["raw_output_ref"]
    assert ref["log_sha256"] == raw.log_sha256 == log_hash
    assert ref["stdout_bytes"] == 1_000_000 and ref["stderr_bytes"] == 0
    recovered = ExecutionReceipt.model_validate(
        {
            **data["raw_receipt_metadata"],
            "stdout": data["receipt"]["stdout"],
            "stderr": "",
        }
    )
    assert recovered.model_dump(mode="json") == raw.model_dump(mode="json")
    # Preserve the original failure mechanism; increasing CAS capacity is not
    # part of the fix. This fixture claims ASCII output only, not all escapes.
    previous = {**payload, "data": {**payload["data"], "raw_receipt": raw.model_dump(mode="json")}}
    with pytest.raises(DomainError) as error:
        store.put(json.dumps(previous, ensure_ascii=False).encode())
    assert error.value.code == "ARTIFACT_TOO_LARGE"


def test_original_process_output_reference_keeps_utf8_stream_boundaries_for_raw_reconstruction():
    stdout, stderr = "public 雪\n", "diagnostic ☃\n"
    combined = (stdout + stderr).encode()
    raw = raw_receipt(stdout, stderr=stderr, log_sha256=sha256_text(stdout + stderr))
    reference = {
        "process_operation_id": "probe:process",
        "raw_log_hash": sha256_text(stdout + stderr),
    }
    source = _public_probe_source_data(raw, reference)
    split = source["raw_output_ref"]["stdout_bytes"]
    assert split == len(stdout.encode()) and source["raw_output_ref"]["stderr_bytes"] == len(
        stderr.encode()
    )
    assert source["raw_output_ref"]["raw_log_hash"] == raw.log_sha256
    assert source["raw_output_ref"]["process_operation_id"] == "probe:process"
    rebuilt = ExecutionReceipt.model_validate(
        {
            **source["raw_receipt_metadata"],
            "stdout": combined[:split].decode(),
            "stderr": combined[split:].decode(),
        }
    )
    assert rebuilt.model_dump(mode="json") == raw.model_dump(mode="json")
