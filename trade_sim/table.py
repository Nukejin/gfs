"""全角文字を含む表を桁をそろえて表示する。"""

from __future__ import annotations

import unicodedata
from collections.abc import Sequence


def text_width(text: str) -> int:
    """端末上での表示幅 (全角は 2、半角は 1)。"""
    return sum(2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1 for ch in text)


def _pad(text: str, width: int, align: str) -> str:
    gap = " " * (width - text_width(text))
    return gap + text if align == ">" else text + gap


def render_table(
    headers: Sequence[str],
    rows: Sequence[Sequence[str]],
    aligns: str,
    footer: Sequence[Sequence[str]] = (),
) -> str:
    """aligns は列ごとの寄せ方 ("<" 左寄せ / ">" 右寄せ) を並べた文字列。"""
    all_rows = [headers, *rows, *footer]
    widths = [max(text_width(row[i]) for row in all_rows) for i in range(len(headers))]

    def line(cells: Sequence[str]) -> str:
        return "  ".join(_pad(c, w, a) for c, w, a in zip(cells, widths, aligns)).rstrip()

    separator = "  ".join("-" * w for w in widths)
    lines = [line(headers), separator, *(line(r) for r in rows)]
    if footer:
        lines += [separator, *(line(r) for r in footer)]
    return "\n".join(lines)
