#!/usr/bin/env python3
from fontTools.ttLib import TTFont
import sys
from pathlib import Path
from collections import deque

INFONT = "LibertinusSerif-SemiboldItalic.otf"
OUTFONT = "LibertinusSerif-SemiboldItalic-patched.otf"

def glyph_for_char(tt: TTFont, ch: str):
    if len(ch) != 1:
        raise SystemExit(f"ERROR: base/diacritic must be exactly 1 character; got {ch!r}")
    uni = ord(ch)
    cmap = tt.getBestCmap()
    if cmap and uni in cmap:
        return cmap[uni]
    for st in tt["cmap"].tables:
        if st.isUnicode() and uni in st.cmap:
            return st.cmap[uni]
    return None

def gsub_targets(tt: TTFont, start_glyph: str, max_depth: int = 6):
    """Follow GSUB SingleSubst (1) + AlternateSubst (3) to include common variants (e.g. a.sc)."""
    out = {start_glyph}
    if "GSUB" not in tt:
        return out
    gsub = tt["GSUB"].table
    ll = getattr(gsub, "LookupList", None)
    if not ll:
        return out

    q = deque([(start_glyph, 0)])
    while q:
        g, d = q.popleft()
        if d >= max_depth:
            continue
        for lookup in ll.Lookup:
            lt = lookup.LookupType
            for st in lookup.SubTable:
                if lt == 1:  # SingleSubst
                    m = getattr(st, "mapping", None)
                    if m and g in m:
                        tgt = m[g]
                        if tgt not in out:
                            out.add(tgt)
                            q.append((tgt, d + 1))
                elif lt == 3:  # AlternateSubst
                    cov = getattr(st, "Coverage", None)
                    altsets = getattr(st, "AlternateSet", None)
                    if not cov or not altsets:
                        continue
                    if g in cov.glyphs:
                        i = cov.glyphs.index(g)
                        for tgt in altsets[i].Alternate:
                            if tgt not in out:
                                out.add(tgt)
                                q.append((tgt, d + 1))
    return out

def mark_candidates(tt: TTFont, mark_glyph: str):
    go = set(tt.getGlyphOrder())
    cands = []
    def add(g):
        if g in go and g not in cands:
            cands.append(g)

    add(mark_glyph)

    # Special-case: combining double acute might be represented as uni030B or hungarumlaut
    if mark_glyph == "uni030B":
        add("hungarumlaut")
    if mark_glyph == "hungarumlaut":
        add("uni030B")

    # Also include any dotted variants if present (uni030B.case etc.)
    for g in tt.getGlyphOrder():
        if g.startswith(mark_glyph + "."):
            add(g)

    return cands

def iter_mark_to_base_subtables(tt: TTFont):
    if "GPOS" not in tt:
        return
    gpos = tt["GPOS"].table
    ll = getattr(gpos, "LookupList", None)
    if not ll:
        return
    for li, lookup in enumerate(ll.Lookup):
        if lookup.LookupType != 4:
            continue
        for si, st in enumerate(lookup.SubTable):
            yield li, si, st

def main():
    if len(sys.argv) != 5:
        raise SystemExit(
            "Usage:\n"
            "  python3 patch_semibold_base_mark.py <base> <diacritic> <xoffset> <yoffset>\n"
            "Example:\n"
            "  python3 patch_semibold_base_mark.py a ̋ 30 0"
        )

    base_char = sys.argv[1]
    mark_char = sys.argv[2]
    dx = int(sys.argv[3])
    dy = int(sys.argv[4])

    if not Path(INFONT).is_file():
        raise SystemExit(f"ERROR: Input font not found: {INFONT}")

    tt = TTFont(INFONT)
    if "GPOS" not in tt:
        raise SystemExit("ERROR: No GPOS table; cannot patch mark attachment.")

    base_g = glyph_for_char(tt, base_char)
    mark_g = glyph_for_char(tt, mark_char)

    if not base_g:
        raise SystemExit(f"ERROR: Could not map base char {base_char!r} (U+{ord(base_char):04X}) via cmap.")
    if not mark_g:
        raise SystemExit(f"ERROR: Could not map diacritic {mark_char!r} (U+{ord(mark_char):04X}) via cmap.")

    base_variants = sorted(gsub_targets(tt, base_g))
    mark_glyphs = mark_candidates(tt, mark_g)

    print(f"Font: {INFONT}")
    print(f"Base: {base_char!r} -> {base_g}")
    print(f"Base variants (GSUB): {base_variants}")
    print(f"Mark: {mark_char!r} -> {mark_g}")
    print(f"Mark candidates: {mark_glyphs}")
    print(f"Offsets: dx={dx}, dy={dy}")

    edits = 0

    for li, si, st in iter_mark_to_base_subtables(tt):
        if not all(hasattr(st, a) for a in ("MarkCoverage", "BaseCoverage", "MarkArray", "BaseArray")):
            continue

        base_cov = st.BaseCoverage.glyphs
        mark_cov = st.MarkCoverage.glyphs
        base_cov_set = set(base_cov)
        mark_cov_set = set(mark_cov)

        # All bases present in this subtable
        present_bases = [b for b in base_variants if b in base_cov_set]
        if not present_bases:
            continue

        # Choose a mark glyph that is actually covered here
        hit_mark = next((m for m in mark_glyphs if m in mark_cov_set), None)
        if not hit_mark:
            continue

        m_idx = mark_cov.index(hit_mark)
        m_class = st.MarkArray.MarkRecord[m_idx].Class

        for bg in present_bases:
            b_idx = base_cov.index(bg)
            b_rec = st.BaseArray.BaseRecord[b_idx]
            if m_class >= len(b_rec.BaseAnchor):
                continue
            anchor = b_rec.BaseAnchor[m_class]
            if anchor is None:
                continue

            old_x, old_y = anchor.XCoordinate, anchor.YCoordinate
            anchor.XCoordinate = old_x + dx
            anchor.YCoordinate = old_y + dy
            edits += 1

            print(
                f"Patched MarkToBase lookup {li} sub {si}: base '{bg}' mark '{hit_mark}' class {m_class}: "
                f"X {old_x}->{anchor.XCoordinate}, Y {old_y}->{anchor.YCoordinate}"
            )

    if edits == 0:
        raise SystemExit(
            "ERROR: No anchors changed.\n"
            "But your scan suggests there *is* a MarkToBase subtable covering uni030B and 'a'.\n"
            "If this happens, paste this script’s printed Base variants and Mark candidates."
        )

    tt.save(OUTFONT)
    print(f"✓ Wrote {OUTFONT} ({edits} anchor edit(s))")

if __name__ == "__main__":
    main()
