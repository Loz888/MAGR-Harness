# Research PDF fonts

The bundled WOFF fonts are static 400-weight outline builds of two SIL Open Font
License 1.1 releases. Each PDF embeds a TrueType subset containing the glyphs
used by that report. The subsets do not depend on host fonts.

| Asset | Pinned upstream file | Upstream SHA-256 |
| --- | --- | --- |
| `research-cjk.woff` | [Noto Sans CJK SC variable](https://raw.githubusercontent.com/notofonts/noto-cjk/523d033d6cb47f4a80c58a35753646f5c3608a78/Sans/Variable/TTF/NotoSansCJKsc-VF.ttf) | `990c807e79c25662a5a9ecf7f971baeb2bf2eab9a559e5ecf15cdfdb8561d21f` |
| `research-emoji.woff` | [Noto Emoji variable](https://raw.githubusercontent.com/google/fonts/cc5661d43cd4f9aad5567033a8e15ca28dc35c86/ofl/notoemoji/NotoEmoji%5Bwght%5D.ttf) | `de6c18832938afc99caf132b39d6a30a19bac7f2e812e28db2535b4608d27551` |

CJK copyright © 2014–2021 Adobe, with Reserved Font Name Source. Emoji copyright
2013 Google LLC. The builds use new family and PostScript names. The upstream
licenses are included in [CJK-OFL.txt](CJK-OFL.txt) and [Emoji-OFL.txt](Emoji-OFL.txt).

To reproduce the font files with the project's locked FontTools version, supply
the two original files in the table in the same order.

```bash
uv run --frozen python scripts/build_research_pdf_fonts.py \
  NotoSansCJKsc-VF.ttf NotoEmoji-variable.ttf src/mgar_v3/general_agent/fonts
```
