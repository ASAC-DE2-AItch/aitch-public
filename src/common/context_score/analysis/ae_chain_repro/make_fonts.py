"""make_fonts.py — Noto Sans CJK KR(.ttc, CFF) → 사용 문자만 서브셋 → TTF 변환.

reportlab의 TTFont는 postscript(CFF) 아웃라인을 못 읽으므로 cu2qu로 쿼드러틱
변환해 glyf 테이블을 만든다. 서브셋을 먼저 하므로 결과는 수십 KB 수준.
"""
from __future__ import annotations

import sys
from pathlib import Path

from fontTools import subset
from fontTools.ttLib import TTFont, newTable
from fontTools.pens.ttGlyphPen import TTGlyphPen
from fontTools.pens.cu2quPen import Cu2QuPen

SRC = "/usr/share/fonts/opentype/noto/NotoSansCJK-%s.ttc"
OUT = Path("/sessions/sleepy-zealous-cerf/mnt/outputs/fonts")
OUT.mkdir(exist_ok=True)


def kr_index(path):
    """.ttc 안에서 KR 서브폰트 인덱스 찾기."""
    from fontTools.ttLib import TTCollection
    ttc = TTCollection(path, lazy=True)
    for i, f in enumerate(ttc.fonts):
        for rec in f["name"].names:
            if rec.nameID == 1 and "KR" in str(rec.toUnicode()):
                return i
    return 0


def otf_to_ttf(font, max_err=1.0):
    """CFF 아웃라인 → glyf(쿼드러틱)."""
    order = font.getGlyphOrder()
    gs = font.getGlyphSet()
    glyf = newTable("glyf")
    glyf.glyphOrder = order
    glyf.glyphs = {}
    for name in order:
        pen = TTGlyphPen(gs)
        gs[name].draw(Cu2QuPen(pen, max_err, reverse_direction=True))
        glyf.glyphs[name] = pen.glyph()
    font["glyf"] = glyf
    for g in glyf.glyphs.values():
        g.recalcBounds(glyf)

    maxp = newTable("maxp")
    maxp.tableVersion = 0x00010000
    maxp.maxZones = 1
    maxp.maxTwilightPoints = maxp.maxStorage = 0
    maxp.maxFunctionDefs = maxp.maxInstructionDefs = 0
    maxp.maxStackElements = maxp.maxSizeOfInstructions = 0
    maxp.maxComponentElements = max(
        (len(g.components) if g.isComposite() else 0) for g in glyf.glyphs.values())
    font["maxp"] = maxp
    font["maxp"].compile(font)

    post = font["post"]
    post.formatType = 2.0
    post.extraNames = []
    post.mapping = {}
    post.glyphOrder = order

    font["loca"] = newTable("loca")
    for t in ("CFF ", "VORG", "CFF2"):
        if t in font:
            del font[t]
    font.sfntVersion = "\x00\x01\x00\x00"      # OTTO → TrueType
    return font


def build(chars, weight, out_name):
    path = SRC % weight
    idx = kr_index(path)
    f = TTFont(path, fontNumber=idx)
    opt = subset.Options(glyph_names=True, notdef_outline=True,
                         layout_features=[], hinting=False, desubroutinize=True,
                         drop_tables=["FFTM", "GPOS", "GSUB", "GDEF", "VORG",
                                      "BASE", "JSTF", "DSIG", "vmtx", "vhea", "VVAR"])
    sub = subset.Subsetter(options=opt)
    sub.populate(text="".join(sorted(chars)))
    sub.subset(f)
    otf_to_ttf(f)
    p = OUT / out_name
    f.save(str(p))
    print(f"{out_name}: subfont#{idx} · {len(chars)}자 · {p.stat().st_size/1024:.0f} KB")
    return p


if __name__ == "__main__":
    corpus = Path(sys.argv[1]).read_text(encoding="utf-8") if len(sys.argv) > 1 else ""
    chars = set(corpus) | set(
        "0123456789.,-+×÷=%()[]{}<>≤≥≈·—–…‘’“”/\\|:;!?#$&*@_'\"~^ "
        "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ"
        "αβγμσΔ①②③④⑤⑥⑦⑧⑨⑩▶■□●○◆★→←↑↓₀₁₂")
    chars = {ch for ch in chars if ch.isprintable() or ch == " "}
    build(chars, "Regular", "NotoKR.ttf")
    build(chars, "Bold", "NotoKR-Bold.ttf")
