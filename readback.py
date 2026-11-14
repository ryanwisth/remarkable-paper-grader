"""Tick reader: does the ledger's footer box carry a mark now?

apply_semantic draws a "Q (tick one)" row of small boxes plus a "done"
compliance box into the AI Feedback layer, and logs their rects to
work/ledger.jsonl under ``boxes`` in CANONICAL page px (1620x2160):

    {"q": [{"rect": [x, y, w, h], "label": str}, ...],
     "compliance": [x, y, w, h]}

The student answers by ticking one with the pen. That tick lands on the writer's own layer,
so it shows up in the next render_page() PNG and in stroke_events() — which
means it would otherwise (a) be invisible to us and (b) look like brand-new
work to the re-grade guard. This module closes both gaps:

  read_boxes()          pixels  -> which box carries a mark
  tick_event_indices()  strokes -> which ink events ARE the tick
  guard_sha()           strokes -> re-grade sha with the ticks removed

METHOD (read_boxes)
-------------------
Binarize the page at grey < 150. For each logged rect, sample the INTERIOR
ONLY: inset 6 px from every edge. The box's own border is a ~4 px stroke
laid on the rect edge, so it reaches ~2 px inward; measured bleed into the
interior is 0.0000 at inset 6 for border widths up to 8 px (0.0333 at inset
2 for a 4 px border, 0.0000 from inset 3 up). Inset 6 also buys margin
against student work that strays across a box edge — see the caveat below.

MEASURED BASELINE (empty boxes, tools/corpus)
---------------------------------------------
Corpus = tools/corpus/ledger.json (45 entries, all with boxes) read against
tools/corpus/page-b{idx}.png, idx resolved through tools/corpus/nb.content.
These pages are pre-ink renders, so every logged box is EMPTY by
construction; whatever the interior reads is pure noise floor.

    rects sampled (on-page)                          163
    rects clipped away entirely (logged at y=2160)    12
    distinct (page, rect) pairs                       98
    interior fraction == 0.0000                      162 of 163
    interior fraction  > 0                             1 of 163
    MAX empty-box interior fraction               0.01446   <- page idx 55,
                                                               rect (469, 2016, 34, 34)
                                                               = 7 ink px of 484

    same rect, sampled WITHOUT the inset:          0.1600   <- 185 ink px of 1156;
                                                               would read TICKED

Synthesized marks in a 34x34 box (interior = 22x22 = 484 px), for scale:

    3 px dot                                       0.0248   (below threshold — a
                                                             pinprick is not a tick)
    cramped half-size check, 2 px nib              0.0682
    slash corner-to-corner, 2 px nib               0.1157
    full check, 2 px nib                           0.1364
    full check, 4 px nib                           0.2624
    X, 3 px nib                                    0.3905

THRESHOLD = 0.05. That is 3.5x the worst empty box measured (0.01446) and
1.4x below the faintest realistic tick (0.0682). In absolute terms it asks
for 24 ink px inside a 484 px interior — roughly a 12 px line at a 2 px nib.

CAVEATS
-------
* Student work that crosses a box is the real false-positive risk, not the
  border and not printed furniture. Page idx 55 proves it: stroke event e31
  (bbox [343, 1969, 224, 86]) runs straight through the q1 rect. The inset
  drops that from 0.16 to 0.0145, which clears the threshold by 3.5x — but
  work landing more centrally in a box would read as a tick. apply_semantic's
  footer collision guard only checks the three label rows, not the 2nd and
  3rd stacked q-box rows, so this can recur.
* No corpus rect had PRINTED page content under it (all 162 on-page empties
  read exactly 0.0000). That is one notebook's footer band, not a proof for
  every book; a rect over printed text would false-positive.
* 12 logged q-rects (pages idx 45 and 55) sit at y=2160 — entirely below the
  2160 px page. Those boxes were never drawn and can never be ticked. They
  are clipped to nothing and reported as fraction 0.0 / un-ticked.
* stroke_events() clusters strokes within GAP_X=110 / GAP_Y=45. Two ticks in
  q-boxes closer than ~110 px apart in x (e.g. the 240 / 368 pair on page idx
  47) merge into ONE event spanning both, which is then contained in neither
  rect and so is NOT recognized as a tick. One q tick plus the far-right
  compliance tick — the expected case — stays separate.

Coordinates everywhere are canonical page px. If a PNG comes in at another
size its rects are scaled to match; nothing else assumes the canvas.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

CANON_W, CANON_H = 1620, 2160

INK_LEVEL = 150      # grey strictly below this counts as ink
BOX_INSET = 6        # px trimmed off every edge before sampling the interior
TICK_FRACTION = 0.05  # interior ink fraction above which a box is TICKED
TICK_INFLATE = 10    # px a logged rect grows by when claiming a stroke event


# ---------------------------------------------------------------- rects

def _rects(entry: dict | None) -> tuple[list[dict], list[int] | None]:
    """(q box dicts, compliance rect or None) out of a ledger entry."""
    boxes = ((entry or {}).get("boxes") or {})
    if not isinstance(boxes, dict):
        return [], None
    q = [b for b in (boxes.get("q") or [])
         if isinstance(b, dict) and _ok(b.get("rect"))]
    comp = boxes.get("compliance")
    return q, (list(comp) if _ok(comp) else None)


def _ok(rect) -> bool:
    return (isinstance(rect, (list, tuple)) and len(rect) == 4
            and all(isinstance(v, (int, float)) for v in rect)
            and rect[2] > 0 and rect[3] > 0)


# ---------------------------------------------------------------- 1. pixels

def _interior_fraction(arr, rect, sx: float, sy: float) -> float:
    """Ink fraction inside `rect`, inset BOX_INSET px on every edge.

    Returns 0.0 when the inset interior falls off the page (a rect logged
    past the page edge was never drawn, so it cannot carry a tick).
    """
    x, y, w, h = rect
    x0 = int(round((x + BOX_INSET) * sx))
    y0 = int(round((y + BOX_INSET) * sy))
    x1 = int(round((x + w - BOX_INSET) * sx))
    y1 = int(round((y + h - BOX_INSET) * sy))
    ih, iw = arr.shape[:2]
    x0, y0 = max(0, x0), max(0, y0)
    x1, y1 = min(iw, x1), min(ih, y1)
    if x1 <= x0 or y1 <= y0:
        return 0.0
    sub = arr[y0:y1, x0:x1]
    return float((sub < INK_LEVEL).mean())


def read_boxes(page_png: Path | str, entry: dict) -> dict:
    """Decide, per logged box, whether it has been TICKED.

    `page_png` is a canonical page render (student ink over the PDF — the AI
    layer's own box borders may or may not be present; the inset makes that
    irrelevant). `entry` is a ledger row carrying "boxes".

    Returns::

        {"q_ticked": int | None,      # index of the ticked q box
         "q_label": str | None,       # its label
         "q_multi": bool,             # 2+ q boxes over threshold
         "compliance_done": bool | None,   # None when no compliance rect
         "samples": {0: 0.0, 1: 0.31, ..., "compliance": 0.0}}

    When 2+ boxes clear the threshold, q_ticked/q_label report the STRONGEST
    one and q_multi flags the ambiguity for the caller to resolve.
    """
    from PIL import Image
    import numpy as np

    q, comp = _rects(entry)
    out: dict = {"q_ticked": None, "q_label": None, "q_multi": False,
                 "compliance_done": None, "samples": {}}
    if not q and comp is None:
        return out

    img = Image.open(page_png).convert("L")
    arr = np.asarray(img)
    sx = img.width / CANON_W
    sy = img.height / CANON_H

    hits = []
    for i, b in enumerate(q):
        f = _interior_fraction(arr, b["rect"], sx, sy)
        out["samples"][i] = f
        if f > TICK_FRACTION:
            hits.append((f, i))
    if hits:
        hits.sort(reverse=True)
        best = hits[0][1]
        out["q_ticked"] = best
        lab = q[best].get("label")
        out["q_label"] = str(lab) if lab is not None else None
        out["q_multi"] = len(hits) > 1
    if comp is not None:
        f = _interior_fraction(arr, comp, sx, sy)
        out["samples"]["compliance"] = f
        out["compliance_done"] = f > TICK_FRACTION
    return out


# ---------------------------------------------------------------- 2. strokes

def _inflated(entry: dict | None) -> list[tuple[float, float, float, float]]:
    """Every logged rect as (x0, y0, x1, y1), grown by TICK_INFLATE px."""
    q, comp = _rects(entry)
    rects = [b["rect"] for b in q] + ([comp] if comp is not None else [])
    g = TICK_INFLATE
    return [(x - g, y - g, x + w + g, y + h + g) for (x, y, w, h) in rects]


def tick_event_indices(events: list, entry: dict | None) -> set[int]:
    """Indices of stroke events that are ticks, not new work.

    An event qualifies only if its bbox lies ENTIRELY inside ONE logged rect
    inflated by TICK_INFLATE px. An event that merely overlaps a box is real
    work straying near it and stays in the "new work" pile.
    """
    zones = _inflated(entry)
    if not zones or not events:
        return set()
    hit: set[int] = set()
    for i, e in enumerate(events):
        bbox = (e or {}).get("bbox") if isinstance(e, dict) else None
        if not _ok(bbox):
            continue
        x, y, w, h = bbox
        ex1, ey1 = x + w, y + h
        for (zx0, zy0, zx1, zy1) in zones:
            if x >= zx0 and y >= zy0 and ex1 <= zx1 and ey1 <= zy1:
                hit.add(i)
                break
    return hit


# ---------------------------------------------------------------- 3. guard

def guard_sha(events: list, entry_or_none: dict | None = None) -> str:
    """Re-grade guard sha over the events, ticks excluded.

    With entry_or_none None this is byte-identical to the sha rmreview
    already writes into work/graded.json:
    ``sha1(json.dumps(events, sort_keys=True).encode()).hexdigest()``.

    With a ledger entry, tick events are dropped first — so a page whose only
    change since the last review is the student's answer to the footer question
    hashes to exactly what it hashed to before the writer ticked, and is correctly
    skipped as "already graded, ink unchanged".
    """
    ticks = tick_event_indices(events, entry_or_none) if entry_or_none else set()
    kept = [e for i, e in enumerate(events) if i not in ticks] if ticks else events
    return hashlib.sha1(json.dumps(kept, sort_keys=True).encode()).hexdigest()


# ---------------------------------------------------------------- cli

def _main(argv: list[str]) -> int:
    if len(argv) < 3:
        print("usage: readback.py PAGE.png LEDGER.jsonl [PAGE_ID]")
        return 2
    png, ledger = argv[1], argv[2]
    want = argv[3] if len(argv) > 3 else None
    text = Path(ledger).read_text()
    rows = ([json.loads(l) for l in text.splitlines() if l.strip()]
            if ledger.endswith("jsonl") else json.loads(text))
    rows = [r for r in rows if (r.get("boxes") or {})]
    if want:
        rows = [r for r in rows if r.get("page_id") == want]
    if not rows:
        print("no ledger row with boxes matched")
        return 1
    row = rows[-1]
    r = read_boxes(Path(png), row)
    print(json.dumps({"page_id": row.get("page_id"), **r}, indent=1, default=str))
    return 0


if __name__ == "__main__":
    import sys
    raise SystemExit(_main(sys.argv))
