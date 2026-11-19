"""Deterministic research PDFs with embedded outline fonts and Unicode extraction."""

from __future__ import annotations

import hashlib
import io
from base64 import b64decode, b64encode
from collections.abc import Iterator
from dataclasses import dataclass
from functools import lru_cache
from itertools import groupby
from pathlib import Path

from fontTools import subset
from fontTools.ttLib import TTFont
from pypdf import PdfReader, PdfWriter
from pypdf.errors import PdfReadError
from pypdf.generic import (
    ArrayObject,
    ContentStream,
    DecodedStreamObject,
    DictionaryObject,
    IndirectObject,
    NameObject,
    NumberObject,
    PdfObject,
    TextStringObject,
)

_FONT_DIR = Path(__file__).with_name("fonts")
_FONTS = (
    ("research-cjk.woff", "MAGRResearchCJK"),
    ("research-emoji.woff", "MAGRResearchEmoji"),
)
_REPORT_HEADER = b"%PDF-1.3\n%MAGR-RESEARCH-TEXT/2\n%"
_REPORT_END = b"\n%MAGR-RESEARCH-END\n"
_MAX_TEXT_BYTES = 350_000
_MAX_PDF_BYTES = 2_000_000
_LINE_WIDTH = 53_100  # Thousandths of the 10 point font size.
_PAGE_LINES = 52


class UnsupportedPDFGlyph(ValueError):
    """The embedded fonts cannot render a codepoint without substitution."""


@dataclass(frozen=True)
class Glyph:
    resource: str
    code: int
    width: int


def _dictionary(values: dict[str, PdfObject]) -> DictionaryObject:
    return DictionaryObject({NameObject("/" + key): value for key, value in values.items()})


def _stream(writer: PdfWriter, data: bytes, *, font_file: bool = False) -> IndirectObject:
    raw = DecodedStreamObject()
    raw.set_data(data)
    stream = raw.flate_encode()
    if font_file:
        stream[NameObject("/Length1")] = NumberObject(len(data))
    return writer._add_object(stream)


@lru_cache(maxsize=len(_FONTS))
def _coverage(filename: str) -> frozenset[int]:
    with TTFont(_FONT_DIR / filename, lazy=True) as font:
        return frozenset(code for code, glyph in font.getBestCmap().items() if glyph != ".notdef")


def _unicode_map(codepoints: tuple[int, ...]) -> bytes:
    lines = [
        "/CIDInit /ProcSet findresource begin",
        "12 dict begin",
        "begincmap",
        "/CIDSystemInfo << /Registry (Adobe) /Ordering (UCS) /Supplement 0 >> def",
        "/CMapName /MAGR-Unicode def",
        "/CMapType 2 def",
        "1 begincodespacerange",
        "<0000> <FFFF>",
        "endcodespacerange",
    ]
    entries = [
        f"<{code:04X}> <{chr(point).encode('utf-16-be').hex().upper()}>"
        for code, point in enumerate(codepoints, 1)
    ]
    for offset in range(0, len(entries), 100):
        block = entries[offset : offset + 100]
        lines.extend([f"{len(block)} beginbfchar", *block, "endbfchar"])
    lines.extend(["endcmap", "CMapName currentdict /CMap defineresource pop", "end", "end"])
    return "\n".join(lines).encode("ascii")


def _embed_font(
    writer: PdfWriter,
    filename: str,
    basename: str,
    resource: str,
    codepoints: tuple[int, ...],
) -> tuple[IndirectObject, dict[int, Glyph]]:
    with TTFont(_FONT_DIR / filename, recalcTimestamp=False) as font:
        reduced = subset.Subsetter(options=subset.Options(ignore_missing_unicodes=False))
        reduced.populate(unicodes=codepoints)
        reduced.subset(font)
        cmap = font.getBestCmap()
        head = font["head"]
        metrics = font["hmtx"].metrics
        ascent = font["hhea"].ascent
        descent = font["hhea"].descent

        def scale(value: int) -> int:
            return int(round(1000 * value / head.unitsPerEm))

        gids = bytearray(b"\x00\x00")
        widths = ArrayObject()
        glyphs = {}
        for cid, point in enumerate(codepoints, 1):
            name = cmap[point]
            gid = int(font.getGlyphID(name))
            if not gid:
                raise UnsupportedPDFGlyph(f"Research PDF has no glyph for U+{point:04X}.")
            gids.extend(gid.to_bytes(2, "big"))
            width = scale(metrics[name][0])
            widths.append(NumberObject(width))
            glyphs[point] = Glyph(resource, cid, width)
        binary = io.BytesIO()
        font.flavor = None  # PDF embeds the TrueType subset, not the packaged WOFF.
        font.save(binary, reorderTables=True)
        font_data = binary.getvalue()
        # PDF subsets use a six-letter tag to distinguish embedded glyph sets.
        tag = "".join(chr(ord("A") + byte % 26) for byte in hashlib.sha256(font_data).digest()[:6])
        basefont = NameObject("/" + tag + "+" + basename)
        descriptor = _dictionary(
            {
                "Type": NameObject("/FontDescriptor"),
                "FontName": basefont,
                "Flags": NumberObject(4),
                "FontBBox": ArrayObject(
                    [
                        NumberObject(scale(value))
                        for value in (head.xMin, head.yMin, head.xMax, head.yMax)
                    ]
                ),
                "ItalicAngle": NumberObject(0),
                "Ascent": NumberObject(scale(ascent)),
                "Descent": NumberObject(scale(descent)),
                "CapHeight": NumberObject(scale(ascent)),
                "StemV": NumberObject(80),
                "FontFile2": _stream(writer, font_data, font_file=True),
            }
        )
    cid_font = _dictionary(
        {
            "Type": NameObject("/Font"),
            "Subtype": NameObject("/CIDFontType2"),
            "BaseFont": basefont,
            "CIDSystemInfo": _dictionary(
                {
                    "Registry": TextStringObject("Adobe"),
                    "Ordering": TextStringObject("Identity"),
                    "Supplement": NumberObject(0),
                }
            ),
            "CIDToGIDMap": _stream(writer, bytes(gids)),
            "FontDescriptor": writer._add_object(descriptor),
            "W": ArrayObject([NumberObject(1), widths]),
        }
    )
    reference = writer._add_object(
        _dictionary(
            {
                "Type": NameObject("/Font"),
                "Subtype": NameObject("/Type0"),
                "BaseFont": basefont,
                "Encoding": NameObject("/Identity-H"),
                "DescendantFonts": ArrayObject([writer._add_object(cid_font)]),
                "ToUnicode": _stream(writer, _unicode_map(codepoints)),
            }
        )
    )
    return reference, glyphs


def _fonts(
    writer: PdfWriter,
    points: set[int],
) -> tuple[DictionaryObject, dict[int, Glyph]]:
    resource = DictionaryObject()
    glyphs = {}
    for idx, (filename, basename) in enumerate(_FONTS, 1):
        if not points:
            break
        available = points & _coverage(filename)
        if available:
            name = f"/F{idx}"
            font, subset_glyphs = _embed_font(
                writer,
                filename,
                basename,
                name,
                tuple(sorted(available)),
            )
            resource[NameObject(name)] = font
            glyphs.update(subset_glyphs)
            points -= available
    if points:
        raise UnsupportedPDFGlyph(f"Research PDF has no glyph for U+{min(points):04X}.")
    return _dictionary({"Font": resource}), glyphs


def _wrapped(line: str, glyphs: dict[int, Glyph]) -> Iterator[list[Glyph]]:
    row: list[Glyph] = []
    width = 0
    for char in line:
        glyph = glyphs[ord(char)]
        if row and width + glyph.width > _LINE_WIDTH:
            yield row
            row, width = [], 0
        row.append(glyph)
        width += glyph.width
    yield row


def report_pdf(markdown: str) -> bytes:
    text = markdown.encode()
    if len(text) > _MAX_TEXT_BYTES:
        raise ValueError("Research PDF input exceeds the report limit.")
    writer = PdfWriter()
    lines = markdown.expandtabs(tabsize=4).splitlines()
    resource, glyphs = _fonts(writer, {ord(char) for line in lines for char in line})
    rows = [row for line in lines for row in _wrapped(line, glyphs)]
    for offset in range(0, max(1, len(rows)), _PAGE_LINES):
        page = writer.add_blank_page(width=595, height=842)
        page[NameObject("/Resources")] = resource
        commands = ["BT"]
        for index, row in enumerate(rows[offset : offset + _PAGE_LINES]):
            commands.append(f"1 0 0 1 32 {802 - 14 * index} Tm")
            for name, group in groupby(row, key=lambda glyph: glyph.resource):
                codes = "".join(f"{glyph.code:04X}" for glyph in group)
                commands.append(f"{name} 10 Tf <{codes}> Tj")
        commands.append("ET")
        stream = DecodedStreamObject()
        stream.set_data("\n".join(commands).encode("ascii"))
        page.replace_contents(ContentStream(stream, writer))
    output = io.BytesIO()
    encoded = b64encode(text)
    comment = b"\n%".join(encoded[i : i + 72] for i in range(0, len(encoded), 72))
    writer.pdf_header = _REPORT_HEADER + comment + _REPORT_END.rstrip(b"\n")
    writer.write(output)
    return output.getvalue()


def verified_pdf_text(content: bytes) -> bytes:
    """Authenticate generated bytes and verify their visible text against the input."""
    if len(content) > _MAX_PDF_BYTES or not content.startswith(_REPORT_HEADER):
        return b""
    end = content.find(_REPORT_END, len(_REPORT_HEADER), _MAX_TEXT_BYTES * 2)
    if end < 0:
        return b""
    try:
        mirror = content[len(_REPORT_HEADER) : end].replace(b"\n%", b"")
        text = b64decode(mirror, validate=True)
        source = text.decode()
        if report_pdf(source) != content:
            return b""
        # Parse only our authenticated PDF. Ignore whitespace inserted by page layout.
        visible = "".join(page.extract_text() for page in PdfReader(io.BytesIO(content)).pages)
    except (ValueError, PdfReadError):
        return b""
    return text if "".join(visible.split()) == "".join(source.split()) else b""
