"""Minimal Hershey JHF single-stroke font renderer.

Glyphs map to polylines — exactly what a pen-stroke drawing API wants.
Font: futural (Hershey simplex), public domain.
"""

from __future__ import annotations

from pathlib import Path

_FONT = Path(__file__).parent / "futural.jhf"   # clean print face; script rejected as illegible


def _load() -> dict[int, tuple[float, float, list[list[tuple[float, float]]]]]:
    glyphs = {}
    lines = _FONT.read_text().splitlines()
    # JHF continuation lines: a glyph's data may wrap; stitch by vertex count
    stitched: list[str] = []
    for ln in lines:
        if len(ln) >= 10 and ln[0:5].strip().isdigit() and ln[5:8].strip().isdigit():
            stitched.append(ln)
        elif stitched:
            stitched[-1] += ln
    for idx, ln in enumerate(stitched):
        nverts = int(ln[5:8])
        body = ln[8:]
        left = ord(body[0]) - ord("R")
        right = ord(body[1]) - ord("R")
        coords = body[2:]
        strokes: list[list[tuple[float, float]]] = []
        cur: list[tuple[float, float]] = []
        for i in range(0, min(len(coords), 2 * (nverts - 1)), 2):
            pair = coords[i:i + 2]
            if pair == " R":
                if cur:
                    strokes.append(cur)
                cur = []
                continue
            if len(pair) < 2:
                break
            x = ord(pair[0]) - ord("R")
            y = ord(pair[1]) - ord("R")
            cur.append((float(x), float(y)))
        if cur:
            strokes.append(cur)
        glyphs[32 + idx] = (float(left), float(right), strokes)
    return glyphs


_GLYPHS = _load()


def text_to_polylines(
    text: str,
    x: float,
    y: float,
    size: float = 28.0,
    max_width: float | None = None,
) -> tuple[list[list[list[float]]], float]:
    """Render text starting at (x, y) baseline. Returns (polylines, y_after).

    size = cap height in output units. Wraps at max_width on word boundaries.
    Coordinates suit the reMarkable scene space directly.
    """
    import math
    import random
    rnd = random.Random(hash(text) & 0xFFFF)   # deterministic per text
    scale = size / 21.0          # hershey glyphs are ~21 units tall
    line_h = size * 1.85
    out: list[list[list[float]]] = []
    cx, cy = x, y

    for word in text.replace("\n", " \n ").split(" "):
        if word == "\n":
            cx, cy = x, cy + line_h
            continue
        # measure word
        wlen = 0.0
        for ch in word + " ":
            g = _GLYPHS.get(ord(ch))
            if g:
                wlen += (g[1] - g[0]) * scale
        if max_width and cx + wlen > x + max_width and cx > x:
            cx, cy = x, cy + line_h
        for ch in word + " ":
            g = _GLYPHS.get(ord(ch))
            if g is None:
                continue
            left, right, strokes = g
            # human jitter: per-glyph baseline wobble, size breath, slight lean
            gs = scale * (1.0 + rnd.uniform(-0.05, 0.05))
            wob = rnd.uniform(-0.9, 0.9) * scale
            lean = rnd.uniform(-0.035, 0.035)
            for st in strokes:
                pts = []
                for (px, py) in st:
                    gx = (px - left) * gs
                    gy = py * gs
                    pts.append([cx + gx + gy * lean + rnd.uniform(-0.3, 0.3) * scale,
                                cy + gy + wob + rnd.uniform(-0.3, 0.3) * scale])
                out.append(pts)
            cx += (right - left) * gs * (1.0 + rnd.uniform(0.0, 0.06))
    return out, cy + line_h


if __name__ == "__main__":
    polys, yy = text_to_polylines("Check: 8x7=56, not 54", 0, 0, 28)
    n = sum(len(p) - 1 for p in polys)
    print(f"glyphs loaded: {len(_GLYPHS)} | polylines: {len(polys)} | segments: {n} | next y: {yy}")
