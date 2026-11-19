"""Read bounded tabular formats and emit spreadsheet-safe deterministic files."""

from __future__ import annotations

import csv
import io
import json
import re
import zipfile
from decimal import Decimal, InvalidOperation
from pathlib import PurePosixPath
from typing import Any
from xml.etree import ElementTree as ET
from xml.sax.saxutils import escape

from mgar_v3.general_agent.data_contracts import (
    MAX_CELLS,
    MAX_COLUMNS,
    MAX_DOCUMENT,
    MAX_ROWS,
    DatasetFormat,
)

Value = str | bool | None | Decimal
Row = dict[str, Value]
_HEADER = re.compile(r"[A-Za-z0-9_.:-]{1,100}\Z")
_COORDINATE = re.compile(r"([A-Z]{1,2})([1-9][0-9]{0,4})\Z")
_NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
_RO = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
_RP = "http://schemas.openxmlformats.org/package/2006/relationships"
_INVALID_XML = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\ud800-\udfff\ufffe\uffff]")


def _header(name: str) -> str:
    if not _HEADER.fullmatch(name):
        raise ValueError("Invalid column identifier.")
    return name


def _check_text(text: str) -> str:
    if len(text) > 1024 or _INVALID_XML.search(text):
        raise ValueError("Dataset text exceeds its cell limits.")
    return text


def _number(value: str | int | Decimal) -> Decimal:
    try:
        numeric = Decimal(value)
    except InvalidOperation as exc:
        raise ValueError("Invalid number.") from exc
    exponent = numeric.as_tuple().exponent
    if (
        not isinstance(exponent, int)
        or len(numeric.as_tuple().digits) > 80
        or abs(numeric.adjusted()) > 100
        or exponent < -150
    ):
        raise ValueError("Number exceeds its supported range.")
    return numeric


def _flat(value: object) -> Value:
    if value is None or isinstance(value, bool):
        return value
    if type(value) is int or isinstance(value, Decimal):
        return _number(value)
    if isinstance(value, str):
        checked = _check_text(value)
        return checked if checked else None
    raise ValueError("Dataset cells must be scalar.")


def _parsed_text(text: str) -> Value:
    value = _check_text(text.strip())
    if not value:
        return None
    if value in {"true", "false"}:
        return value == "true"
    if re.fullmatch(r"[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?", value):
        return _number(value)
    return value


def _matrix(records: list[list[Value]]) -> list[Row]:
    if not 2 <= len(records) <= MAX_ROWS + 1 or not 1 <= len(records[0]) <= MAX_COLUMNS:
        raise ValueError("Dataset dimensions exceed their limits.")
    if len(records) * len(records[0]) > MAX_CELLS:
        raise ValueError("Dataset has too many cells.")
    if not all(isinstance(item, str) for item in records[0]):
        raise ValueError("Dataset headers must contain column identifiers.")
    names = [_header(str(item)) for item in records[0]]
    if len(set(names)) != len(names):
        raise ValueError("Duplicate column identifier.")
    if any(len(row) != len(names) for row in records[1:]):
        raise ValueError("Row width differs from the dataset schema.")
    return [dict(zip(names, row, strict=True)) for row in records[1:]]


def _csv(raw: bytes) -> list[Row]:
    stream = io.StringIO(raw.decode("utf-8-sig"), newline="")
    records: list[list[Value]] = []
    for row in csv.reader(stream, strict=True):
        if len(records) > MAX_ROWS or len(row) > MAX_COLUMNS:
            raise ValueError("Dataset dimensions exceed their limits.")
        if not records:
            records.append([_check_text(value.strip()) for value in row])
        else:
            records.append([_parsed_text(value) for value in row])
    return _matrix(records)


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value = dict(pairs)
    if len(value) != len(pairs):
        raise ValueError("Duplicate JSON key.")
    return value


def _json(raw: bytes) -> list[Row]:
    values = json.loads(
        raw.decode("utf-8-sig"), parse_float=Decimal, object_pairs_hook=_unique_object
    )
    if not isinstance(values, list) or not 1 <= len(values) <= MAX_ROWS:
        raise ValueError("JSON must contain a bounded array of records.")
    names: dict[str, None] = {}
    for row in values:
        if not isinstance(row, dict):
            raise ValueError("JSON rows must be objects.")
        for name in row:
            names[_header(name)] = None
        if len(names) > MAX_COLUMNS or len(names) * len(values) > MAX_CELLS:
            raise ValueError("Dataset dimensions exceed their limits.")
    if not names:
        raise ValueError("Dataset columns are missing.")
    return [{name: _flat(row.get(name)) for name in names} for row in values]


def _xml(book: zipfile.ZipFile, member: str) -> ET.Element:
    value = book.read(member).decode("utf-8-sig")
    if "<!DOCTYPE" in value.upper() or "<!ENTITY" in value.upper():
        raise ValueError("Workbook DTDs and entities are disabled.")
    return ET.fromstring(value)


def _sheet_path(book: zipfile.ZipFile) -> str:
    manifest = _xml(book, "[Content_Types].xml")
    if any("macroenabled" in value.lower() for item in manifest for value in item.attrib.values()):
        raise ValueError("Workbook macros are disabled.")
    relation_files = (name for name in book.namelist() if name.endswith(".rels"))
    for name in relation_files:
        if any(item.get("TargetMode", "").lower() == "external" for item in _xml(book, name)):
            raise ValueError("External workbook relationships are disabled.")
    workbook = _xml(book, "xl/workbook.xml")
    sheet = workbook.find(f"./{{{_NS}}}sheets/{{{_NS}}}sheet")
    if sheet is None:
        raise ValueError("Workbook has no worksheet.")
    relationship = sheet.get(f"{{{_RO}}}id")
    refs = _xml(book, "xl/_rels/workbook.xml.rels")
    for item in refs.findall(f"{{{_RP}}}Relationship"):
        if (
            item.get("Id") == relationship
            and item.get("Type") == f"{_RO}/worksheet"
            and (target := item.get("Target"))
        ):
            path = target.lstrip("/") if target.startswith("/") else f"xl/{target}"
            if re.fullmatch(r"xl/worksheets/[A-Za-z0-9_.-]+\.xml", path):
                return path
    raise ValueError("Workbook worksheet does not match an internal relationship.")


def _xlsx_cell(cell: ET.Element, strings: list[str], *, header: bool) -> Value:
    if cell.find(f"{{{_NS}}}f") is not None:
        raise ValueError("Workbook formulas are disabled.")
    text = cell.findtext(f"{{{_NS}}}v")
    kind = cell.get("t", "n")
    if kind == "inlineStr":
        value = "".join(item.text or "" for item in cell.iter(f"{{{_NS}}}t"))
        return _check_text(value) or None
    if text is None:
        return None
    if kind == "s":
        if not text.isdecimal() or int(text) >= len(strings):
            raise ValueError("Invalid workbook shared string.")
        return strings[int(text)] or None
    if kind in {"str", "d"}:
        return _check_text(text) or None
    if kind == "b" and text in {"0", "1"}:
        return text == "1"
    if kind == "n" and not header:
        return _number(text)
    raise ValueError("Unsupported workbook cell type.")


def _xlsx(raw: bytes) -> list[Row]:
    with zipfile.ZipFile(io.BytesIO(raw)) as book:
        entries = book.infolist()
        names = set(book.namelist())
        if (
            not 1 <= len(entries) <= 80
            or len(names) != len(entries)
            or sum(info.file_size for info in entries) > MAX_DOCUMENT
            or any(info.flag_bits & 1 for info in entries)
        ):
            raise ValueError("Workbook archive exceeds its limits.")
        if any(
            name.startswith("/")
            or ".." in PurePosixPath(name).parts
            or "vbaproject" in name.lower()
            or "externallinks/" in name.lower()
            for name in names
        ):
            raise ValueError("Workbook archive contains a disallowed member.")
        path = _sheet_path(book)
        strings = []
        if "xl/sharedStrings.xml" in names:
            for item in _xml(book, "xl/sharedStrings.xml").findall(f"{{{_NS}}}si"):
                strings.append(
                    _check_text("".join(part.text or "" for part in item.iter(f"{{{_NS}}}t")))
                )
        sheet = _xml(book, path)
        records: list[list[Value]] = []
        for index, row in enumerate(sheet.findall(f"./{{{_NS}}}sheetData/{{{_NS}}}row"), 1):
            if index > MAX_ROWS + 1 or row.get("r", str(index)) != str(index):
                raise ValueError("Workbook rows must be contiguous and bounded.")
            values: dict[int, Value] = {}
            for cell in row.findall(f"{{{_NS}}}c"):
                coord = _COORDINATE.fullmatch(cell.get("r", ""))
                if coord is None or int(coord[2]) != index:
                    raise ValueError("Invalid worksheet cell coordinate.")
                ordinal = 0
                for letter in coord[1]:
                    ordinal = ordinal * 26 + ord(letter) - 64
                if ordinal > MAX_COLUMNS or ordinal in values:
                    raise ValueError("Worksheet columns exceed their limits.")
                values[ordinal] = _xlsx_cell(cell, strings, header=index == 1)
            if records and not set(values) <= set(range(1, len(records[0]) + 1)):
                raise ValueError("Worksheet row exceeds the header width.")
            width = len(records[0]) if records else max(values, default=0)
            records.append([values.get(i) for i in range(1, width + 1)])
        return _matrix(records)


def read_dataset(raw: bytes, declared: DatasetFormat) -> tuple[DatasetFormat, list[Row]]:
    prefix = raw.removeprefix(b"\xef\xbb\xbf").lstrip()
    actual: DatasetFormat = (
        "xlsx"
        if raw.startswith(b"PK\x03\x04")
        else "json"
        if prefix.startswith((b"[", b"{"))
        else "csv"
    )
    if declared not in {"auto", actual}:
        raise ValueError("Dataset encoding differs from its declared format.")
    reader = {"csv": _csv, "json": _json, "xlsx": _xlsx}
    return actual, reader[actual](raw)


def _csv_cell(value: Value) -> Value:
    if isinstance(value, str) and value.lstrip().startswith(("=", "+", "-", "@")):
        return "'" + value
    if isinstance(value, str) and value.startswith(("\t", "\r", "\n")):
        return "'" + value
    return value


def write_csv(records: list[list[Value]]) -> bytes:
    stream = io.StringIO(newline="")
    writer = csv.writer(stream, lineterminator="\r\n")
    writer.writerows([[_csv_cell(value) for value in row] for row in records])
    return stream.getvalue().encode()


def write_xlsx(records: list[list[Value]]) -> bytes:
    contents = {
        "[Content_Types].xml": (
            '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
            '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.'
            'relationships+xml"/><Default Extension="xml" ContentType="application/xml"/>'
            '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.'
            'openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
            '<Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.'
            'openxmlformats-officedocument.spreadsheetml.worksheet+xml"/></Types>'
        ),
        "_rels/.rels": (
            f'<Relationships xmlns="{_RP}"><Relationship Id="rId1" '
            f'Type="{_RO}/officeDocument" Target="xl/workbook.xml"/></Relationships>'
        ),
        "xl/workbook.xml": (
            f'<workbook xmlns="{_NS}" xmlns:r="{_RO}"><sheets>'
            '<sheet name="Analysis" sheetId="1" r:id="rId1"/></sheets></workbook>'
        ),
        "xl/_rels/workbook.xml.rels": (
            f'<Relationships xmlns="{_RP}"><Relationship Id="rId1" '
            f'Type="{_RO}/worksheet" Target="worksheets/sheet1.xml"/></Relationships>'
        ),
    }
    lines = [f'<worksheet xmlns="{_NS}"><sheetData>']
    for index, row in enumerate(records, 1):
        lines.append(f'<row r="{index}">')
        for column, value in enumerate(row):
            if value is None:
                continue
            ref = f"{chr(65 + column)}{index}"
            if isinstance(value, Decimal):
                lines.append(f'<c r="{ref}"><v>{value}</v></c>')
            elif isinstance(value, bool):
                lines.append(f'<c r="{ref}" t="b"><v>{int(value)}</v></c>')
            else:
                text = escape(_check_text(value))
                lines.append(
                    f'<c r="{ref}" t="inlineStr"><is><t xml:space="preserve">{text}</t></is></c>'
                )
        lines.append("</row>")
    lines.append("</sheetData></worksheet>")
    contents["xl/worksheets/sheet1.xml"] = "".join(lines)
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as book:
        for name, content in contents.items():
            book.writestr(zipfile.ZipInfo(name), content, compress_type=zipfile.ZIP_DEFLATED)
    return output.getvalue()
