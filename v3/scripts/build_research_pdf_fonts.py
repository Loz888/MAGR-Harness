"""Prepare the pinned OFL research fonts for deterministic PDF embedding."""

import argparse
from pathlib import Path

from fontTools import subset
from fontTools.ttLib import TTFont
from fontTools.varLib.instancer import instantiateVariableFont


def build(source: Path, target: Path, family: str, postscript_name: str) -> None:
    with TTFont(source, recalcTimestamp=False) as font:
        # Report text is laid out by codepoint. Strip unused layout and hinting tables.
        for tag in ("GSUB", "GPOS", "BASE", "DSIG", "vmtx", "vhea", "VORG"):
            if tag in font:
                del font[tag]
        options = subset.Options(hinting=False, name_IDs=[0, 1, 2, 3, 4, 5, 6, 13, 14, 16, 17])
        subsetter = subset.Subsetter(options=options)
        subsetter.populate(unicodes=font.getBestCmap())
        subsetter.subset(font)
        if "fvar" in font:
            instantiateVariableFont(font, {"wght": 400}, inplace=True)
        names = font["name"]
        for name in names.names:
            replacement = {
                1: family,
                2: "Regular",
                3: postscript_name,
                4: family + " Regular",
                6: postscript_name,
                16: family,
                17: "Regular",
            }.get(name.nameID)
            if replacement is not None:
                name.string = replacement
        font.recalcTimestamp = False
        font.flavor = "woff"
        font.save(target, reorderTables=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("cjk", type=Path)
    parser.add_argument("emoji", type=Path)
    parser.add_argument("destination", type=Path)
    args = parser.parse_args()
    args.destination.mkdir(parents=True, exist_ok=True)
    build(args.cjk, args.destination / "research-cjk.woff", "MAGR Research CJK", "MAGRResearchCJK")
    build(
        args.emoji,
        args.destination / "research-emoji.woff",
        "MAGR Research Emoji",
        "MAGRResearchEmoji",
    )


if __name__ == "__main__":
    main()
