"""Mac-side page renderer: raw .rm ink over the notebook's PDF page.

Replaces the flip-and-screenshot capture phase: page files come over SSH in
seconds, and the AI Feedback layer is excluded at render time (no aiVis dance).
"""

from __future__ import annotations

import json
from pathlib import Path

import contextlib
import io
import logging

import pymupdf
from PIL import Image, ImageDraw
from rmscene import read_blocks
from rmscene.scene_items import Line
from rmscene import scene_items as si
from rmscene.scene_stream import (
    SceneLineItemBlock,
    TreeNodeBlock,
)

W, H = 1620, 2160

# The tablet stores ink in the PAGE's own coordinate space at its native
# resolution, NOT in screen pixels: a native notebook page is 517.9x691pt and
# lands on exactly 1620x2160 ink px, i.e. 3.128 ink px per point. That only
# equals screen space when the page happens to be screen-sized, so ink on any
# imported PDF of a different size must be rescaled by zoom/DEVICE_PX_PER_PT.
# Without this, ink renders ~18% too large: work is flung past the page edge and
# filled answer bubbles land ~150px below the answer strip.
DEVICE_PX_PER_PT = 1620 / 517.9

# Ink outside the canvas is dropped by PIL without complaint. A page whose work
# runs past the edge would then be graded as unfinished, so every render records
# what it lost here and the caller warns on it.
LAST_CLIP: dict = {"clipped": 0, "total": 0, "bounds": None}

# The ink->render transform this page used. Anything that writes ink BACK to the
# tablet must invert it, or tutor marks land at the wrong scale on a PDF page.
LAST_TF: dict = {"k": 1.0, "cx": W / 2, "top": 0.0}


def page_transform(pdf_path: Path | None, pdf_page: int | None) -> dict:
    """The ink->render transform for a page, without rendering it."""
    if not (pdf_path and pdf_page is not None):
        return {"k": 1.0, "cx": W / 2, "top": 0.0}
    doc = pymupdf.open(pdf_path)
    pg = doc[pdf_page]
    zoom = min(W / pg.rect.width, H / pg.rect.height)
    bg_w = pg.rect.width * zoom
    doc.close()
    return {"k": zoom / DEVICE_PX_PER_PT,
            "cx": (W - bg_w) // 2 + bg_w / 2, "top": 0.0}


def _layer_labels(blocks) -> dict:
    """node_id -> layer label for tree group nodes."""
    labels = {}
    for b in blocks:
        if isinstance(b, TreeNodeBlock):
            g = b.group
            label = getattr(getattr(g, "label", None), "value", None)
            if label:
                labels[g.node_id] = label
    return labels


def render_page(rm_path: Path, pdf_path: Path | None, pdf_page: int | None,
                out_path: Path, skip_layers: tuple = ("AI Feedback",),
                only_layers: tuple | None = None,
                background: bool = True) -> Path:
    """background=False still uses the PDF for GEOMETRY but leaves the page
    unpainted — needed for the prior-tutor-ink layer, which must share the main
    render's scale without dragging the printed page into the ink mask."""
    # background: the PDF page, or blank. `k` converts ink px -> render px and
    # (cx, top) is where the page's ink origin sits on the canvas.
    k, cx, top = 1.0, W / 2, 0.0
    if pdf_path and pdf_page is not None:
        doc = pymupdf.open(pdf_path)
        pg = doc[pdf_page]
        zoom = min(W / pg.rect.width, H / pg.rect.height)
        pix = pg.get_pixmap(matrix=pymupdf.Matrix(zoom, zoom), alpha=False)
        bg = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
        canvas = Image.new("RGB", (W, H), "white")
        paste_x = (W - bg.width) // 2
        if background:
            canvas.paste(bg, (paste_x, 0))
        doc.close()
        k = zoom / DEVICE_PX_PER_PT
        cx = paste_x + bg.width / 2
    else:
        canvas = Image.new("RGB", (W, H), "white")

    draw = ImageDraw.Draw(canvas)

    clipped = total = 0
    xs: list[float] = []
    ys: list[float] = []

    if rm_path and rm_path.exists():
        logging.getLogger("rmscene").setLevel(logging.CRITICAL)
        with contextlib.redirect_stderr(io.StringIO()):
            blocks = list(read_blocks(open(rm_path, "rb")))
        labels = _layer_labels(blocks)
        for b in blocks:
            if not isinstance(b, SceneLineItemBlock):
                continue
            item = getattr(b, "item", None)
            line = getattr(item, "value", None)
            if not isinstance(line, Line):
                continue
            parent = getattr(b, "parent_id", None)
            if only_layers is not None:
                if labels.get(parent) not in only_layers:
                    continue
            elif labels.get(parent) in skip_layers:
                continue
            pts = [(p.x * k + cx, p.y * k + top) for p in line.points]
            if len(pts) < 2:
                continue
            # width: rmscene point width is in device px already (approx)
            widths = [getattr(p, "width", 4) for p in line.points]
            wpx = max(2, int(sum(widths) / len(widths) * k / 4))
            total += 1
            xs += [p[0] for p in pts]
            ys += [p[1] for p in pts]
            if any(x < 0 or x > W or y < 0 or y > H for x, y in pts):
                clipped += 1
            draw.line(pts, fill="black", width=wpx, joint="curve")

    LAST_TF.update(k=k, cx=cx, top=top)
    LAST_CLIP.update(
        clipped=clipped, total=total,
        bounds=(min(xs), min(ys), max(xs), max(ys)) if xs else None)
    if clipped:
        print(f"[rmrender] WARNING: {clipped}/{total} strokes fall outside the "
              f"{W}x{H} page and are NOT in this render — "
              f"ink spans x[{min(xs):.0f}..{max(xs):.0f}] y[{min(ys):.0f}..{max(ys):.0f}]. "
              f"The page is being graded on partial work.")

    canvas.save(out_path)
    return out_path


