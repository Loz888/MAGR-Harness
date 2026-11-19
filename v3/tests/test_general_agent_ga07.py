"""Read-only analysis, safe exports and TaskGraph evidence on real PostgreSQL."""

from __future__ import annotations

import csv
import io
import json
import subprocess
import sys
import zipfile
from datetime import UTC, datetime
from decimal import Decimal
from xml.etree import ElementTree as ET

import pytest
from pydantic import ValidationError
from test_general_agent_ga01 import spec
from test_general_agent_ga02 import capability, graph, node, start

from mgar_v3.domain import DomainError
from mgar_v3.evidence.artifacts import ArtifactStore
from mgar_v3.general_agent.capabilities import CapabilityRegistry
from mgar_v3.general_agent.contracts import Deliverable, OutcomeArtifact, SourceRef
from mgar_v3.general_agent.data_analysis import DataAnalysisExecutor, DataAnalysisRequest
from mgar_v3.general_agent.data_contracts import MAX_INPUT
from mgar_v3.general_agent.data_formats import write_xlsx
from mgar_v3.general_agent.verification_contracts import ResultDraft, VerificationPlan

CSV = (
    b"amount,region,day,include\n"
    b"1,north,2026-01-15,true\n"
    b"2,north,2026-01-29,true\n"
    b"3,south,2026-02-01,true\n"
    b"100,=1+1,2026-02-01,true\n"
    b",north,2026-02-03,true\n"
    b"7,south,2026-01-01,false\n"
)


@pytest.fixture
def executor(tmp_path):
    return DataAnalysisExecutor(ArtifactStore(tmp_path))


def request_for(executor, data, **changes):
    source = SourceRef(
        kind="dataset", reference="authorized-sales", version=executor.artifacts.put(data)
    )
    return DataAnalysisRequest.model_validate({"source": source, "field": "amount", **changes})


def csv_rows(executor, result):
    return list(
        csv.DictReader(io.StringIO(executor.artifacts.read(result.artifacts["csv"]).decode()))
    )


def test_csv_descriptive_group_trend_and_reproducible_exports(executor):
    request = request_for(
        executor,
        CSV,
        formula="mean",
        group_by="region",
        date_field="day",
        filters=({"field": "include", "equals": True},),
        seed=13,
    )
    result = executor.execute(request)
    assert result == executor.execute(request)
    assert (result.input_rows, result.selected_rows, result.sample_count) == (6, 5, 4)
    artifacts = executor.artifacts
    numeric = json.loads(artifacts.read(result.artifacts["numeric"]))
    assert numeric == {"field": "amount", "formula": "mean", "sample_count": 4, "value": 26.5}
    rows = {(row["scope"], row["group"]): row for row in csv_rows(executor, result)}
    assert rows["group", "north"]["value"] == "1.5"
    assert rows["group", "'=1+1"]["sample_count"] == "1"
    assert rows["month", "2026-02"]["value"] == "51.5"
    archive = io.BytesIO(artifacts.read(result.artifacts["xlsx"]))
    with zipfile.ZipFile(archive) as book:
        assert book.testzip() is None
        ws = ET.fromstring(book.read("xl/worksheets/sheet1.xml"))
        assert not any(item.tag.endswith("}f") for item in ws.iter())
        assert any(item.text == "=1+1" and item.tag.endswith("}t") for item in ws.iter())
    chart = ET.fromstring(artifacts.read(result.artifacts["chart"]))
    assert chart.tag == "{http://www.w3.org/2000/svg}svg"
    assert any(item.tag.endswith("}rect") for item in chart)
    report = artifacts.read(result.artifacts["report"]).decode()
    assert "Missing metric cells are excluded" in report and "include = True" in report
    manifest = json.loads(artifacts.read(result.artifacts["manifest"]))
    assert manifest["schema_overview"]["amount"] == {
        "types": ["number"],
        "missing": 1,
        "outliers_iqr": 1,
    }
    assert manifest["request"]["seed"] == 13
    assert manifest["request"]["source"] == request.source.model_dump(mode="json")
    assert manifest["implementation_sha256"] == result.implementation_sha256
    assert manifest["input_sha256"] == request.source.version
    assert manifest["normalized_sha256"] == result.normalized_sha256
    assert len(artifacts.read(result.normalized_sha256)) > 1


@pytest.mark.parametrize("encoding", ["xlsx", "json"])
def test_tabular_encodings_preserve_numbers_groups_and_bounds(executor, encoding):
    raw = (
        write_xlsx([["amount", "region"], [Decimal("1.25"), "A"], [Decimal("6.25"), "A"]])
        if encoding == "xlsx"
        else b'[{"amount":1.25,"region":"A"},{"amount":6.25,"region":"A"}]'
    )
    request = request_for(executor, raw, formula="sum", group_by="region")
    result = executor.execute(request)
    numeric = json.loads(executor.artifacts.read(result.artifacts["numeric"]))
    assert numeric == {"field": "amount", "formula": "sum", "sample_count": 2, "value": 7.5}
    assert csv_rows(executor, result)[1]["group"] == "A"


@pytest.mark.parametrize(
    "data",
    [
        b"amount,amount\n1,2\n",
        b"amount,region\n1\n",
        b'[{"amount":1,"amount":7}]',
        b'[{"amount":{"value":1}}]',
        b'[{"amount":true}]',
        b'[{"amount":NaN}]',
        b'[{"amount":null}]',
        b'[{"amount":1e500}]',
    ],
)
def test_invalid_or_ambiguous_scalar_inputs_fail_closed(executor, data):
    with pytest.raises(DomainError) as rejected:
        executor.execute(request_for(executor, data))
    assert rejected.value.code == "GENERAL_DATA_INPUT"


@pytest.mark.parametrize(
    "member,change",
    [
        ("xl/worksheets/sheet1.xml", b"<f>SUM(A1)</f><v>3</v>"),
        ("_rels/.rels", b'TargetMode="External" Target="https://example.test/secret"'),
        ("[Content_Types].xml", b"application/vnd.ms-excel.sheet.macroEnabled.main+xml"),
    ],
)
def test_workbook_rejects_active_or_external_parts(executor, member, change):
    safe = io.BytesIO(write_xlsx([["amount"], [Decimal(3)]]))
    hostile = io.BytesIO()
    with zipfile.ZipFile(safe) as source, zipfile.ZipFile(hostile, "w") as target:
        for name in source.namelist():
            content = source.read(name)
            if name == member:
                marker = {
                    "xl/worksheets/sheet1.xml": b"<v>3</v>",
                    "_rels/.rels": b'Target="xl/workbook.xml"',
                    "[Content_Types].xml": b"application/vnd.openxmlformats-officedocument."
                    b"spreadsheetml.sheet.main+xml",
                }[name]
                assert marker in content
                content = content.replace(marker, change)
            target.writestr(name, content)
    with pytest.raises(DomainError) as rejected:
        executor.execute(request_for(executor, hostile.getvalue()))
    assert rejected.value.code == "GENERAL_DATA_INPUT"


def test_only_cas_bound_requests_and_bounded_work_are_admitted(executor):
    with pytest.raises(ValidationError):
        DataAnalysisRequest(
            source=SourceRef(kind="dataset", reference="/etc/passwd", version="/etc/passwd"),
            field="amount",
        )
    with pytest.raises(ValidationError):
        DataAnalysisRequest(
            source=SourceRef(kind="web", reference="https://example.test", version="a" * 64),
            field="amount",
        )
    with pytest.raises(ValidationError):
        request_for(executor, CSV, sql="ATTACH DATABASE ':memory:' AS foreign")
    with pytest.raises(DomainError) as denied:
        executor.execute(request_for(executor, b"a" * (MAX_INPUT + 1)))
    assert denied.value.code == "GENERAL_DATA_LIMITS"
    with pytest.raises(DomainError) as expired:
        executor.execute(request_for(executor, CSV), timeout_seconds=0.0001)
    assert expired.value.code == "GENERAL_DATA_DEADLINE"


def test_fixed_worker_denies_file_socket_and_external_database_io():
    script = """
import socket
import sqlite3
from mgar_v3.general_agent.data_worker import _limit_process
_limit_process()
for test in (lambda: open('not-an-input'),
             lambda: socket.socket(),
             lambda: sqlite3.connect('not-a-database')):
    try:
        test()
    except PermissionError:
        continue
    raise AssertionError('Unexpected I/O authority')
"""
    process = subprocess.run(
        [sys.executable, "-I", "-c", script], env={}, check=False, capture_output=True, timeout=6
    )
    assert process.returncode == 0, process.stderr.decode()


def test_pg_data_node_and_independent_numeric_evidence(engine):
    executor = DataAnalysisExecutor(engine.artifacts)
    request = request_for(
        executor, CSV, formula="sum", filters=({"field": "include", "equals": True},)
    )
    descriptor = capability("data.analyze")
    engine.general_graphs.registry = CapabilityRegistry([descriptor])
    value = spec("data").model_copy(
        update={
            "source_refs": (request.source,),
            "allowed_capabilities": ("data.analyze",),
            "deliverables": (Deliverable(id="result", kind="table", description="Sum"),),
        }
    )
    task, initial, access = start(
        engine,
        graph(
            node(
                "analysis",
                domain="data",
                outputs=("result", "bundle"),
                capabilities=("data.analyze",),
            )
        ),
        value,
        descriptor,
    )

    handler = executor.handler(request, outputs={"result": "numeric", "bundle": "manifest"})
    state = engine.general_executors.run(
        task.task_id,
        task.revision,
        initial.graph_revision,
        "analysis",
        access,
        execute=handler,
    )
    assert state is not None and state.progress["analysis"].state == "succeeded"
    current = engine.general_tasks.get(task.task_id)
    assert current.state == "verifying" and current.plan_version is not None
    outputs = state.progress["analysis"].outputs
    assert isinstance(outputs["bundle"], str)
    result = executor.verify_manifest(request, outputs["bundle"])
    assert outputs == {
        "result": result.artifacts["numeric"],
        "bundle": result.artifacts["manifest"],
    }
    step, observation = executor.numeric_evidence(
        result,
        request,
        output_id="result",
        collector="data.collector",
        observed_at=datetime.now(UTC),
    )
    plan = VerificationPlan(
        task_id=current.task_id,
        task_revision=current.revision,
        plan_version=current.plan_version,
        expected_outputs=value.deliverables,
        verification_steps=(step,),
    )
    verifier = engine.general_verification
    verification_id = verifier.install(plan, current.version)
    verifier.capture(verification_id, step.id, observation)
    candidate = OutcomeArtifact(deliverable_id="result", sha256=result.artifacts["numeric"])
    checked = verifier.verify(
        verification_id,
        ResultDraft(summary="Dataset result checked", outputs=(candidate,)),
        verifier="independent.check",
    )
    assert checked.status == "verified"
    assert verifier.publish(checked.report_id).state == "completed"


def test_pg_rejects_an_existing_cas_hash_outside_task_sources(engine):
    executor = DataAnalysisExecutor(engine.artifacts)
    private = request_for(executor, b"amount\n1")
    intended = request_for(executor, b"amount\n2")
    descriptor = capability("data.analyze")
    engine.general_graphs.registry = CapabilityRegistry([descriptor])
    value = spec("data").model_copy(
        update={"source_refs": (intended.source,), "allowed_capabilities": ("data.analyze",)}
    )
    task, initial, access = start(
        engine,
        graph(node("analysis", domain="data", outputs=("result",), capabilities=("data.analyze",))),
        value,
        descriptor,
    )
    with pytest.raises(DomainError) as denied:
        engine.general_executors.run(
            task.task_id,
            task.revision,
            initial.graph_revision,
            "analysis",
            access,
            execute=executor.handler(private, outputs={"result": "numeric"}),
        )
    assert denied.value.code == "GENERAL_DATA_SCOPE"
    assert engine.general_graphs.get(task.task_id).progress["analysis"].state == "failed"
