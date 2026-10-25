"""Rebuild the packaged chart font as a small subset.

Usage: python tools/subset_chart_font.py SOURCE.otf OUTPUT.otf
SOURCE is the SparkChartSans.otf of an earlier release (it already carries the renamed family); OUTPUT is
normally spark_core/chart_assets/SparkChartSans.otf. Glyph outlines are not edited. Requires fontTools.
Characters outside the kept set (rare hanzi, Hangul, emoji) render as missing-glyph boxes in a chart only.
"""
import re
import sys
from pathlib import Path
from fontTools import subset
from fontTools.ttLib import TTFont

ROOT = Path(__file__).resolve().parent.parent
RANGES = ((0x20, 0x7E), (0xA0, 0x17F), (0x370, 0x3FF), (0x400, 0x4FF), (0x2000, 0x206F), (0x2190, 0x21FF),
          (0x2200, 0x22FF), (0x25A0, 0x25FF), (0x3000, 0x303F), (0x3040, 0x30FF), (0xFF00, 0xFFEF))


def gb2312():
    for high in range(0xA1, 0xF8):
        for low in range(0xA1, 0xFF):
            try:
                yield ord(bytes((high, low)).decode('gb2312'))
            except UnicodeDecodeError:
                pass


def literals():
    # Every character the renderer itself can print, so the fixed labels never depend on the ranges above.
    source = (ROOT/'spark_core'/'chart_render.py').read_text(encoding='utf-8')
    return {ord(c) for c in source if ord(c) > 0x7F}


def main(source, target):
    wanted = {c for lo, hi in RANGES for c in range(lo, hi+1)} | set(gb2312()) | literals()
    font = TTFont(source)
    wanted &= set(font.getBestCmap())
    options = subset.Options()
    options.layout_features = ['kern']
    options.hinting = False
    options.notdef_outline = True
    options.name_IDs = ['*']
    options.name_languages = ['*']
    options.glyph_names = False
    subsetter = subset.Subsetter(options)
    subsetter.populate(unicodes=wanted)
    subsetter.subset(font)
    font.save(target)
    print(f'{len(wanted)} characters, {Path(target).stat().st_size} bytes')


if __name__ == '__main__':
    if len(sys.argv) != 3:
        raise SystemExit(__doc__)
    main(sys.argv[1], sys.argv[2])
