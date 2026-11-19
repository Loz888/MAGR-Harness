"""Offline worker for server-defined Python/SQL analysis of bounded scalar datasets."""

from __future__ import annotations

import base64
import codecs
import hashlib
import itertools
import json
import re
import resource
import sqlite3
import sys
from collections.abc import Sequence
from datetime import date
from decimal import Decimal, DecimalException, localcontext
from statistics import quantiles
from typing import Any
from xml.etree import ElementTree as ET
from xml.sax.saxutils import escape
from zipfile import BadZipFile

from mgar_v3.general_agent.data_contracts import (
    DATA_PROTOCOL,
    MAX_GROUPS,
    MAX_INPUT,
    DataAnalysisRequest,
    Formula,
)
from mgar_v3.general_agent.data_formats import Row, Value, read_dataset, write_csv, write_xlsx

_LIMIT = 256 * 1024 * 1024
_SELECT_ONLY = {sqlite3.SQLITE_SELECT, sqlite3.SQLITE_READ}


def _limit_process() -> None:
    # ZIP member names can request CP437 lazily. Resolve built-in codecs
    # before closing filesystem access for analysis.
    codecs.lookup("cp437")
    codecs.lookup("utf-8-sig")
    resource.setrlimit(resource.RLIMIT_CPU, (5, 5))
    resource.setrlimit(resource.RLIMIT_AS, (_LIMIT, _LIMIT))
    resource.setrlimit(resource.RLIMIT_FSIZE, (0, 0))
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))

    def audit(event: str, args: tuple[object, ...]) -> None:
        if (
            event in {"open", "os.chdir", "os.posix_spawn", "os.system", "subprocess.Popen"}
            or event.startswith(("socket.", "ctypes.", "sqlite3.enable_load_extension"))
            or (event == "sqlite3.connect" and args != (":memory:",))
        ):
            raise PermissionError("Dataset workers have no external I/O authority.")

    sys.addaudithook(audit)


def _group_order(rows: list[tuple[str, int]]) -> list[tuple[str, list[int]]]:
    """Only a bound, built-in grouping query can inspect the in-memory table."""
    conn = sqlite3.connect(":memory:")
    try:
        conn.execute("PRAGMA temp_store=MEMORY")
        conn.execute("CREATE TABLE sample (label TEXT, ordinal INTEGER PRIMARY KEY)")
        conn.executemany("INSERT INTO sample (label, ordinal) VALUES (?, ?)", rows)

        def authorizer(operation: int, *args: object) -> int:
            return sqlite3.SQLITE_OK if operation in _SELECT_ONLY else sqlite3.SQLITE_DENY

        conn.set_authorizer(authorizer)
        selected = conn.execute("SELECT label, ordinal FROM sample ORDER BY label, ordinal")
        rows_by_key = [
            (key, [ordinal for _, ordinal in values])
            for key, values in itertools.groupby(selected, key=lambda item: str(item[0]))
        ]
        if len(rows_by_key) > MAX_GROUPS:
            raise ValueError("Analysis contains too many groups.")
        return rows_by_key
    finally:
        conn.close()


def _category(value: Value | int) -> str:
    if value is None:
        return "missing"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, (Decimal, int)):
        return "number"
    return "text"


def _matches(row: Row, request: DataAnalysisRequest) -> bool:
    for item in request.filters:
        actual = row[item.field]
        if _category(actual) != _category(item.equals):
            return False
        if actual != item.equals:
            return False
    return True


def _aggregate(numbers: Sequence[Decimal], formula: Formula) -> Decimal:
    base = sum(numbers, Decimal(0))
    if formula == "count":
        return Decimal(len(numbers))
    return base if formula == "sum" else base / Decimal(len(numbers))


def _outliers(numbers: list[Decimal]) -> int:
    if len(numbers) < 4:
        return 0
    low, _, high = quantiles(numbers, n=4, method="inclusive")
    band = (high - low) * Decimal("1.5")
    return sum(number < low - band or number > high + band for number in numbers)


def _overview(rows: list[Row]) -> dict[str, dict[str, object]]:
    summary: dict[str, dict[str, object]] = {}
    for name in rows[0]:
        values = [row[name] for row in rows]
        observed = {_category(value) for value in values if value is not None}
        summary[name] = {
            "types": sorted(observed),
            "missing": sum(value is None for value in values),
            "outliers_iqr": _outliers([value for value in values if isinstance(value, Decimal)]),
        }
    return summary


def _label(value: Value) -> str:
    if value is None:
        return "(missing)"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, Decimal):
        if not value:
            return "0"
        label = format(value, "f")
        return label.rstrip("0").rstrip(".") if "." in label else label
    return str(value)


def _series(
    rows: list[tuple[Row, Decimal]], request: DataAnalysisRequest
) -> list[tuple[str, str, list[Decimal]]]:
    result = [("summary", "all", [number for _, number in rows])]
    if request.group_by is not None:
        column = request.group_by
        labels = [row[column] for row, _ in rows]
        axes = [f"{_category(label)}:{_label(label)}" for label in labels]
        for _, indexes in _group_order(list(zip(axes, range(len(rows)), strict=True))):
            result.append(("group", _label(labels[indexes[0]]), [rows[i][1] for i in indexes]))
    if request.date_field is not None:
        months = []
        for row, _ in rows:
            value = row[request.date_field]
            if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
                raise ValueError("Trend dates must be valid ISO YYYY-MM-DD values.")
            months.append(date.fromisoformat(value).isoformat()[:7])
        for month, indexes in _group_order(list(zip(months, range(len(rows)), strict=True))):
            result.append(("month", month, [rows[i][1] for i in indexes]))
    return result


def _numeric_payload(field: str, formula: Formula, numbers: list[Decimal]) -> bytes:
    value = _aggregate(numbers, formula)
    if abs(value.adjusted()) > 100:
        raise ValueError("The result exceeds the numeric evidence range.")
    return (
        f'{{"sample_count":{len(numbers)},"value":{value},'
        f'"formula":{json.dumps(formula)},"field":{json.dumps(field)}}}'
    ).encode()


def _chart(series: list[tuple[str, str, Decimal]], field: str, formula: Formula) -> bytes:
    visible = series[:40]
    peak = max((abs(value) for _, _, value in visible), default=Decimal(1)) or Decimal(1)
    height = 80 + 22 * len(visible)
    elements = [
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 760 {height}" role="img">',
        f"<title>{escape(field)} by {formula}</title>",
        '<rect width="100%" height="100%" fill="#fff"/>',
        f'<text x="20" y="28" font-size="17">{escape(field)} · {formula}</text>',
        f'<line x1="414" x2="414" y1="47" y2="{height - 25}" stroke="#334155"/>',
    ]
    for index, (kind, label, value) in enumerate(visible):
        y = 54 + index * 22
        width = int(abs(value) * Decimal(210) / peak)
        start = 414 if value >= 0 else 414 - width
        shown = escape((kind + " " + label)[:27])
        elements.append(f'<text x="20" y="{y + 12}" font-size="12">{shown}</text>')
        elements.append(f'<rect x="{start}" y="{y}" width="{width}" height="14" fill="#155e75"/>')
        elements.append(f'<text x="635" y="{y + 12}" font-size="11">{value:.6G}</text>')
    if len(series) > len(visible):
        elements.append(f'<text x="20" y="{height - 6}">First {len(visible)} rows shown</text>')
    return ("".join(elements) + "</svg>").encode()


def _inline(value: str) -> str:
    text = re.sub(r"\s+", " ", value)
    return re.sub(r"([\\`*_[\]{}()|])", r"\\\1", escape(text))


def _report(
    request: DataAnalysisRequest,
    counts: dict[str, int],
    overview: dict[str, dict[str, object]],
    records: list[list[Value]],
) -> bytes:
    count_labels = {
        "input_rows": "Input rows",
        "selected_rows": "Rows after filtering",
        "sample_count": "Numeric samples",
        "missing_metric_after_filter": "Rows with a missing metric after filtering",
    }
    lines = [
        "# Data analysis",
        "",
        f"Field **{_inline(request.field)}**, formula **{request.formula}**.",
        "",
        *[f"- {count_labels[key]} **{value}**" for key, value in counts.items()],
        "- Missing metric cells are excluded before aggregation.",
        "- Anomalies use the inclusive IQR rule with a 1.5 multiplier.",
        "- Mean calculations use 50 significant decimal digits.",
        "",
    ]
    if request.filters:
        lines.append("The analysis applied these equality filters.")
        lines.extend(
            f"- {_inline(item.field)} = {_inline(str(item.equals))}" for item in request.filters
        )
    lines.extend(
        [
            "",
            "## Dataset schema",
            "",
            "| Column | Types | Missing | IQR anomalies |",
            "| --- | --- | --- | --- |",
        ]
    )
    for name, item in overview.items():
        lines.append(
            f"| {_inline(name)} | {item['types']} | {item['missing']} | {item['outliers_iqr']} |"
        )
    lines.extend(
        [
            "",
            "## Results",
            "",
            "| Scope | Label | Samples | Value | Minimum | Maximum |",
            "| --- | --- | ---: | ---: | ---: | ---: |",
        ]
    )
    lines.extend("| " + " | ".join(_inline(str(value)) for value in row) + " |" for row in records)
    return ("\n".join(lines) + "\n").encode()


def _analysis(raw: bytes, request: DataAnalysisRequest) -> dict[str, Any]:
    actual, rows = read_dataset(raw, request.format)
    fields = {request.field, request.group_by, request.date_field}
    fields.update(item.field for item in request.filters)
    if not (fields - {None}) <= set(rows[0]):
        raise ValueError("Requested columns are outside the dataset schema.")
    selected = [row for row in rows if _matches(row, request)]
    values = []
    for row in selected:
        value = row[request.field]
        if value is None:
            continue
        if not isinstance(value, Decimal):
            raise ValueError("The selected metric must contain only numbers or missing cells.")
        values.append((row, value))
    if not values:
        raise ValueError("No numeric samples remain after filtering.")
    with localcontext() as context:
        context.prec = 50
        numbers = [number for _, number in values]
        groups = _series(values, request)
        aggregate = _numeric_payload(request.field, request.formula, numbers)
        normalized = (
            "[" + ",".join(f"{{{json.dumps(request.field)}:{number}}}" for number in numbers) + "]"
        ).encode()
        schema_overview = _overview(rows)
        counts = {
            "input_rows": len(rows),
            "selected_rows": len(selected),
            "sample_count": len(values),
            "missing_metric_after_filter": len(selected) - len(values),
        }
        table: list[list[Value]] = [
            [
                kind,
                label,
                Decimal(len(metrics)),
                _aggregate(metrics, request.formula),
                min(metrics),
                max(metrics),
            ]
            for kind, label, metrics in groups
        ]
        table_with_headers: list[list[Value]] = [
            ["scope", "group", "sample_count", "value", "minimum", "maximum"],
            *table,
        ]
        artifacts = {
            "numeric": aggregate,
            "csv": write_csv(table_with_headers),
            "xlsx": write_xlsx(table_with_headers),
            "chart": _chart(
                [
                    (kind, label, _aggregate(metrics, request.formula))
                    for kind, label, metrics in groups
                ],
                request.field,
                request.formula,
            ),
            "report": _report(request, counts, schema_overview, table),
        }
    return {
        "protocol": DATA_PROTOCOL,
        "format": actual,
        "input_sha256": hashlib.sha256(raw).hexdigest(),
        "request": request.model_dump(mode="json"),
        "counts": counts,
        "overview": schema_overview,
        "normalized": base64.b64encode(normalized).decode("ascii"),
        "artifacts": {
            kind: base64.b64encode(value).decode("ascii") for kind, value in artifacts.items()
        },
    }


def main() -> int:
    _limit_process()
    try:
        payload = sys.stdin.buffer.read(2 * MAX_INPUT + 1)
        if len(payload) > 2 * MAX_INPUT:
            raise ValueError("Worker request exceeds its byte limit.")
        envelope = json.loads(payload)
        request = DataAnalysisRequest.model_validate(envelope["request"])
        raw = base64.b64decode(envelope["input"], validate=True)
        if not 1 <= len(raw) <= MAX_INPUT:
            raise ValueError("Dataset exceeds its byte limit.")
        if hashlib.sha256(raw).hexdigest() != request.source.version:
            raise ValueError("Dataset hash differs from the request.")
        response = _analysis(raw, request)
        sys.stdout.write(json.dumps(response, ensure_ascii=False, sort_keys=True))
        return 0
    except (
        ValueError,
        KeyError,
        TypeError,
        OverflowError,
        ET.ParseError,
        BadZipFile,
        DecimalException,
        RecursionError,
        sqlite3.DatabaseError,
    ):
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
