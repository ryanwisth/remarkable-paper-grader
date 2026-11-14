"""Ordered ink events from a raw .rm page file.

Stroke ORDER is creation order in the v6 file (no timestamps, but sequence
survives). This is the reviewer's chronology source: it converts attempt
reconstruction from inference to observation. AI Feedback strokes are excluded
by layer label (and tool 19 is unparseable by rmscene anyway).

Output coordinates are CANONICAL page px (1620x2160), same space as captures.
"""
from __future__ import annotations

import contextlib
import io
import logging
from pathlib import Path

W, H = 1620, 2160
# Handwriting is anisotropic: a line continues rightward with tiny vertical
# gaps, while a new thought jumps down or far away. Calibrated on pg-39.rm.
GAP_X = 110
GAP_Y = 45


def _stroke_bboxes(rm_path: Path,
                   tf: dict | None = None) -> list[tuple[float, float, float, float]]:
    from rmscene import read_blocks
    from rmscene.scene_items import Line
    from rmscene.scene_stream import SceneLineItemBlock, TreeNodeBlock
    logging.getLogger("rmscene").setLevel(logging.CRITICAL)
    with contextlib.redirect_stderr(io.StringIO()):
        blocks = list(read_blocks(open(rm_path, "rb")))
    labels = {}
    for b in blocks:
        if isinstance(b, TreeNodeBlock):
            label = getattr(getattr(b.group, "label", None), "value", None)
            if label:
                labels[b.group.node_id] = label
    out = []
    for b in blocks:
        if not isinstance(b, SceneLineItemBlock):
            continue
        line = getattr(getattr(b, "item", None), "value", None)
        if not isinstance(line, Line):
            continue
        if labels.get(getattr(b, "parent_id", None)) == "AI Feedback":
            continue
        k = (tf or {}).get("k", 1.0) or 1.0
        cx = (tf or {}).get("cx", W / 2)
        top = (tf or {}).get("top", 0.0)
        xs = [p.x * k + cx for p in line.points]
        ys = [p.y * k + top for p in line.points]
        if not xs:
            continue
        out.append((min(xs), min(ys), max(xs), max(ys)))
    return out


def stroke_events(rm_path: Path | str, tf: dict | None = None) -> list[dict]:
    """Cluster consecutive strokes into ink events, preserving file order.

    Returns [{"event": "e1", "strokes": n, "bbox": [x, y, w, h]}, ...].
    `tf` is the page's ink->render transform (rmrender.page_transform); without
    it the bboxes cited to the reviewer will not line up with the rendered PNG
    on any page that is not screen-sized.
    """
    try:
        bxs = _stroke_bboxes(Path(rm_path), tf)
    except Exception:
        return []
    events = []
    cur = None
    prev = None
    for (x0, y0, x1, y1) in bxs:
        if cur is not None and prev is not None:
            (px0, py0, px1, py1) = prev
            dx = max(0.0, max(x0 - px1, px0 - x1))
            dy = max(0.0, max(y0 - py1, py0 - y1))
            if dx <= GAP_X and dy <= GAP_Y:
                cx0, cy0, cx1, cy1, n = cur
                cur = (min(cx0, x0), min(cy0, y0), max(cx1, x1), max(cy1, y1), n + 1)
                prev = (x0, y0, x1, y1)
                continue
            events.append(cur)
        cur = (x0, y0, x1, y1, 1)
        prev = (x0, y0, x1, y1)
    if cur is not None:
        events.append(cur)
    return [{"event": f"e{i+1}", "strokes": n,
             "bbox": [round(x0), round(y0), round(x1 - x0), round(y1 - y0)]}
            for i, (x0, y0, x1, y1, n) in enumerate(events)]


def prompt_block(events: list[dict]) -> str:
    if not events:
        return ""
    rows = "\n".join(
        f"  {e['event']:>3}  {e['strokes']:>2} strokes   bbox [x={e['bbox'][0]}, "
        f"y={e['bbox'][1]}, w={e['bbox'][2]}, h={e['bbox'][3]}]"
        for e in events)
    return f"""
STROKE ORDER (observed from the pen data — authoritative chronology; e1 was
written FIRST, the last event LAST. Coordinates are image pixels):
{rows}
Rules: chronological claims come from this block ONLY, never from page layout.
Transcribe events; never assert an ink event that is not in this list. If two
events together form one written line, say so — but their order stands."""
