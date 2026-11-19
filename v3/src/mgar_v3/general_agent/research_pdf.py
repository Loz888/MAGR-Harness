"""Small deterministic text PDF renderer for research artifacts."""

from __future__ import annotations

import io
import textwrap
from base64 import b64decode, b64encode

from pypdf import PdfWriter
from pypdf.generic import (
    ArrayObject,
    ContentStream,
    DecodedStreamObject,
    DictionaryObject,
    NameObject,
    NumberObject,
    TextStringObject,
)

_CMAP = b"""
/CIDInit /ProcSet findresource begin
12 dict begin
begincmap
/CIDSystemInfo << /Registry (Adobe) /Ordering (UCS) /Supplement 0 >> def
/CMapName /MAGR-Unicode def
/CMapType 2 def
1 begincodespacerange
<0000> <FFFF>
endcodespacerange
1 beginbfrange
<0000> <FFFF> <0000>
endbfrange
endcmap
CMapName currentdict /CMap defineresource pop
end
end
"""
_REPORT_HEADER = b"%PDF-1.3\n%MAGR-RESEARCH-TEXT/1\n%"
_REPORT_END = b"\n%MAGR-RESEARCH-END\n"
_MAX_TEXT_BYTES = 350_000


def report_pdf(markdown: str) -> bytes:
    text = markdown.encode()
    if len(text) > _MAX_TEXT_BYTES:
        raise ValueError("Research PDF input exceeds the report limit.")
    writer = PdfWriter()
    unicode_map = DecodedStreamObject()
    unicode_map.set_data(_CMAP)
    cid_info = DictionaryObject(
        {
            NameObject("/Registry"): TextStringObject("Adobe"),
            NameObject("/Ordering"): TextStringObject("GB1"),
            NameObject("/Supplement"): NumberObject(4),
        }
    )
    cjk = DictionaryObject(
        {
            NameObject("/Type"): NameObject("/Font"),
            NameObject("/Subtype"): NameObject("/CIDFontType0"),
            NameObject("/BaseFont"): NameObject("/STSong-Light"),
            NameObject("/CIDSystemInfo"): cid_info,
            NameObject("/DW"): NumberObject(1000),
        }
    )
    font = DictionaryObject(
        {
            NameObject("/Type"): NameObject("/Font"),
            NameObject("/Subtype"): NameObject("/Type0"),
            NameObject("/BaseFont"): NameObject("/STSong-Light"),
            NameObject("/Encoding"): NameObject("/UniGB-UCS2-H"),
            NameObject("/DescendantFonts"): ArrayObject([cjk]),
            NameObject("/ToUnicode"): writer._add_object(unicode_map),
        }
    )
    font_resource = DictionaryObject(
        {
            NameObject("/Font"): DictionaryObject({NameObject("/F1"): writer._add_object(font)}),
        }
    )
    lines = [
        section
        for line in markdown.splitlines()
        for section in (textwrap.wrap(line, 52, break_on_hyphens=False) or [""])
    ]
    for offset in range(0, max(1, len(lines)), 52):
        page = writer.add_blank_page(width=595, height=842)
        page[NameObject("/Resources")] = font_resource
        rows = ["BT /F1 10 Tf 14 TL 32 802 Td"]
        for line in lines[offset : offset + 52]:
            bmp = "".join(ch if ord(ch) <= 0xFFFF else "\ufffd" for ch in line)
            rows.append(f"<{bmp.encode('utf-16-be').hex()}> Tj T*")
        rows.append("ET")
        stream = DecodedStreamObject()
        stream.set_data("\n".join(rows).encode("ascii"))
        page.replace_contents(ContentStream(stream, writer))
    output = io.BytesIO()
    encoded = b64encode(text)
    comment = b"\n%".join(encoded[i : i + 72] for i in range(0, len(encoded), 72))
    writer.pdf_header = _REPORT_HEADER + comment + _REPORT_END.rstrip(b"\n")
    writer.write(output)
    return output.getvalue()


def verified_pdf_text(content: bytes) -> bytes:
    """Recover text only by exact deterministic render, without parsing an untrusted PDF."""
    if not content.startswith(_REPORT_HEADER):
        return b""
    end = content.find(_REPORT_END, len(_REPORT_HEADER), _MAX_TEXT_BYTES * 2)
    if end < 0:
        return b""
    try:
        mirror = content[len(_REPORT_HEADER) : end].replace(b"\n%", b"")
        text = b64decode(mirror, validate=True)
        if report_pdf(text.decode()) == content:
            return text
    except (UnicodeError, ValueError):
        return b""
    return b""
