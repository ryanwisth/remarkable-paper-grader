#!/usr/bin/env python3
"""
rmreview — button-triggered whole-page review for the reMarkable Paper Pro.

The user taps the Grade button on the tablet toolbar. The bridge writes a trigger
file; this daemon sees it, captures the page, asks a model (the Claude Code CLI) to review the whole
worked solution (correctness, efficiency, flow), and Claude's annotations are
written back onto the page as real ink: margin notes in single-stroke text,
arrows, underlines — all in the "AI Feedback" layer, never touching the user's own.

Safety invariants:
  * All ink goes through the bridge's aiLines verb -> "AI Feedback" layer only.
  * The bridge has no delete/erase verbs at all.
  * A full on-device snapshot of the notebook store precedes every pass.
  * ANTHROPIC_API_KEY is stripped: claude -p runs on the CLI's own login, never metered.

Configuration (environment variables):
  RM_HOST              ssh target for the tablet over USB (default root@10.11.99.1,
                       the reMarkable's standard USB-network address)
  RM_WIFI_HOST         optional ssh target over Wi-Fi, tried first (e.g. root@TABLET_IP)
  GRADER_CLAUDE_BIN    model CLI to call (default "claude")
  GRADER_MODEL         model for the full review (default: the CLI's default model)
  GRADER_TRIAGE_MODEL  model for the fast first pass (default "sonnet")
  GRADER_STUDENT_NOTE  optional one-paragraph description of the student for the prompt
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import board
import readback
import strokes as strokes_mod
from ems import text_to_polylines

# Optional question-bank adapter. The original deployment resolved each page to its
# question (stem, choices, verified answer, worked solution) through a private
# module named `books` exposing `context(pdf_path, page_index) -> dict | None`.
# That module and its data are not part of this repository. Without it, every page
# is graded from the handwriting alone (the prompt's SEE_DEFAULT block).
try:
    import books as _bank
except ImportError:
    _bank = None


def _page_context(pdf, redir):
    if _bank is None:
        return None
    try:
        return _bank.context(pdf, redir)
    except Exception as e:
        print(f"[rmreview] page context unavailable: {e}")
        return None

def _pick_host() -> str:
    """Prefer Wi-Fi (cable-free), fall back to USB."""
    import subprocess as _sp
    usb = os.environ.get("RM_HOST", "root@10.11.99.1")
    wifi = os.environ.get("RM_WIFI_HOST")
    for host in ([wifi] if wifi else []) + [usb]:
        try:
            r = _sp.run(["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=4", host, "true"],
                        capture_output=True, timeout=8)
            if r.returncode == 0:
                return host
        except (_sp.TimeoutExpired, OSError):
            continue
    return usb


RM = _pick_host()
SSH = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=6",
       "-o", "ControlMaster=auto", "-o", "ControlPath=/tmp/rmgrade-ssh-%r@%h",
       "-o", "ControlPersist=120", RM]
print(f"[rmreview] tablet link: {RM}" + (" (Wi-Fi — cable optional)" if RM == os.environ.get("RM_WIFI_HOST") else " (USB)"))
WORK = Path(__file__).parent / "work"
WORK.mkdir(exist_ok=True)

NOTE_SIZE = 26.0          # cap height of written feedback, scene units

# Paper Pro pen palette (scene-assistant enums)
TONE_COLOR = {
    "error":  (7,  0xFFD90707),   # red
    "guide":  (6,  0xFF0062CC),   # blue
    "praise": (10, 0xFF91DA71),   # green (renders vivid on-device; see INK-LAB)
    "plain":  (0,  0xFF000000),   # black — body mass on the board
}
DEFAULT_TONE = "guide"
TONE_ALIAS = {"green": "praise", "red": "error", "blue": "guide", "black": "plain",
              "keep": "praise", "wrong": "error"}
NOTE_WRAP = 420.0         # wrap width for margin notes
MAX_BATCH = 1900          # stay under the bridge's 2048-line cap per aiLines call


def rmssh(cmd: str, timeout: int = 20) -> str:
    try:
        r = subprocess.run(SSH + [cmd], capture_output=True, text=True, timeout=timeout)
        return r.stdout.strip()
    except (subprocess.TimeoutExpired, OSError):
        return ""          # tablet asleep / link down / pipe wedged — treat as silence


def mb(verb: str, arg: str = "", wait: int = 4) -> str:
    return rmssh(
        f"echo '>u{verb}:{arg}' > /run/xovi-mb; "
        f"(cat /run/xovi-mb-out & C=$!; sleep {wait}; kill $C 2>/dev/null) 2>/dev/null"
    )


def screenshot(name: str) -> Path:
    rmssh(f'echo "etakeScreenshot:/home/root/rmgrade/{name},150" > /run/xovi-mb; sleep 2')
    local = WORK / name
    subprocess.run(["scp", "-q", "-o", "ControlPath=/tmp/rmgrade-ssh-%r@%h",
                    f"{RM}:/home/root/rmgrade/{name}", str(local)],
                   check=True, timeout=40)
    return local


def snapshot_notebook(uuid: str | None = None) -> None:
    """Safety snapshot before ink. With a uuid: just that notebook's files
    (fast, per pass). Without: the whole store (slow, once per daemon boot)."""
    store = "/home/root/.local/share/remarkable/xochitl"
    if uuid:
        rmssh("mkdir -p /home/root/rmgrade/snap && "
              "rm -rf /home/root/rmgrade/snap/nb.tmp && "
              "mkdir -p /home/root/rmgrade/snap/nb.tmp && "
              f"cp -a {store}/{uuid}* /home/root/rmgrade/snap/nb.tmp/ && "
              "rm -rf /home/root/rmgrade/snap/nb && "
              "mv /home/root/rmgrade/snap/nb.tmp /home/root/rmgrade/snap/nb", timeout=60)
    else:
        rmssh("mkdir -p /home/root/rmgrade/snap && "
              f"cp -a {store} /home/root/rmgrade/snap/latest.tmp && "
              "rm -rf /home/root/rmgrade/snap/latest && "
              "mv /home/root/rmgrade/snap/latest.tmp /home/root/rmgrade/snap/latest", timeout=180)


_snap_threads: list = []


def start_snapshot(uuid: str | None = None):
    """Run the snapshot in the background; ink waits on it via wait_snapshots()."""
    import threading
    t = threading.Thread(target=snapshot_notebook, args=(uuid,), daemon=True)
    t.start()
    _snap_threads.append(t)
    return t


def wait_snapshots(timeout: float = 150.0) -> None:
    for t in _snap_threads:
        t.join(timeout)
    _snap_threads[:] = [t for t in _snap_threads if t.is_alive()]
    if _snap_threads:
        print("[rmreview] WARN: snapshot still running past timeout — inking anyway")


def canon_to_scene(x: float, y: float) -> tuple[float, float]:
    """Canonical page px (1620x2160) -> scene. Fixed by page geometry, zoom-proof.

    All capture images are normalized to canonical space BEFORE the model sees
    them (file-pull renders are born canonical; screenshot fallbacks are warped
    by normalize_shot using the pageBorderRect read at CAPTURE time). Drawing
    happens in scene coords, which the view cannot shear — so apply-time zoom
    is irrelevant and no live-view probe may ever be used for anchor math."""
    return (x - 809.5, y + 0.5)


def view_rect() -> tuple[float, float, float, float] | None:
    """Where the page sits in view px right now: pageBorderRect (x, y, w, h)."""
    import re
    r = mb("aiView", wait=4)
    try:
        d = json.loads(r)
        nums = re.findall(r"[-\d.]+", d["vals"]["pageBorderRect"])
        if len(nums) < 4:
            return None
        x, y, w, h = (float(v) for v in nums[:4])
        return (x, y, w, h) if w > 100 and h > 100 else None
    except (json.JSONDecodeError, KeyError, TypeError, ValueError):
        return None


def normalize_shot(path: Path) -> bool:
    """Warp a screenshot into canonical 1620x2160 page space if the view was
    zoomed/panned at capture time. Off-screen page regions become mid-grey
    (128): dark enough that ClearSpace treats them as occupied, and flagged to
    the model as ungradeable. Returns True if a warp was applied."""
    vr = view_rect()
    if vr is None:
        print("[rmreview]   WARN: view rect unreadable — treating shot as canonical")
        return False
    rx, ry, rw, rh = vr
    if abs(rw - 1620) < 8 and abs(rx) < 8 and abs(ry) < 8:
        return False                     # default view: already canonical
    from PIL import Image, ImageDraw
    img = Image.open(path).convert("RGB")
    # blank the tablet chrome IN VIEW SPACE so it cannot masquerade as page ink
    dr = ImageDraw.Draw(img)
    dr.rectangle([0, 0, img.width, 110], fill=(128, 128, 128))
    dr.rectangle([0, 0, 160, img.height], fill=(128, 128, 128))
    # output canonical (x,y) <- input view: v = rect.origin + can * rect.size/canon
    warped = img.transform(
        (1620, 2160), Image.AFFINE,
        (rw / 1620.0, 0.0, rx, 0.0, rh / 2160.0, ry),
        resample=Image.BILINEAR, fillcolor=(128, 128, 128))
    warped.save(path)
    print(f"[rmreview]   normalized zoomed capture (page was {rw:.0f}px wide at view x={rx:.0f})")
    return True


px_to_scene = canon_to_scene


def send_lines(lines: list[dict]) -> str:
    payload = json.dumps({"lines": lines})
    local = WORK / "batch.json"
    local.write_text(payload)
    subprocess.run(["scp", "-q", "-o", "ControlPath=/tmp/rmgrade-ssh-%r@%h",
                    str(local), f"{RM}:/home/root/rmgrade/batch.json"], check=True)
    r = mb("aiLines", "/home/root/rmgrade/batch.json", wait=1)
    if r != "QUEUED":
        return r
    for _ in range(45):
        time.sleep(0.35)
        s = mb("aiStatus", wait=1)
        if s.startswith(("DONE", "ERR")):
            return s
    return "ERR poll-timeout"


# Transform the page under review was rendered with (see rmrender.LAST_TF).
# Review geometry is computed in RENDER px; the tablet wants INK px, so every
# write-back inverts this. Identity for native notebooks, ~1/0.846 for a Letter
# PDF — leave it identity and tutor ink lands ~18% small and creeping up-page.
INK_TF: dict = {"k": 1.0, "cx": 1620 / 2, "top": 0.0}


def _to_ink(x: float, y: float) -> tuple[float, float]:
    k = INK_TF["k"] or 1.0
    return (x - INK_TF["cx"]) / k + 1620 / 2, (y - INK_TF["top"]) / k


def lines_to_items(lines: list[dict]) -> tuple[list[dict], float, float]:
    items = []
    allx, ally = [], []
    k = INK_TF["k"] or 1.0
    for ln in lines:
        pts = [_to_ink(p[0], p[1]) for p in ln["pts"]]
        if len(pts) < 2:
            continue
        xs = [p[0] for p in pts]; ys = [p[1] for p in pts]
        allx += xs; ally += ys
        wv = ln.get("_w")
        if isinstance(wv, (list, tuple)):
            wv = [w / k for w in wv]
        elif wv:
            wv = wv / k

        def _w_at(i, n=len(pts), wv=wv, k=k):
            # round, never truncate: dividing by k makes these fractional, and
            # int() would shave a systematic fraction of a unit off every stroke.
            # The >=4 floor is the standing rule from RESEARCH-FIRMWARE-INK-CHANNELS
            # ("never send width 0"); it was documented but never enforced here.
            if isinstance(wv, (list, tuple)):
                return max(4, round(wv[min(i, len(wv) - 1)]))
            return max(4, round(wv or (16 + 12 * __import__("math").sin(3.1416 * min(1.0, i / max(1, n - 1)))) / k))

        items.append({
            "points": [[float(p[0]), float(p[1]), 25, _w_at(i), 0, 235]
                       for i, p in enumerate(pts)],
            "color": int(ln.get("c", 0)),
            "rgba": int(ln.get("rgba", 0xFF000000)),
            "bounds": [min(xs), min(ys), max(xs) - min(xs), max(ys) - min(ys)],
            "tool": int(ln.get("t", 19)),
            "maskScale": 1.0,
            "thickness": 0.0,
        })
    cx = (min(allx) + max(allx)) / 2 if allx else 0.0
    cy = (min(ally) + max(ally)) / 2 if ally else 0.0
    return items, cx, cy


def draw_items(lines: list[dict]) -> str:
    wait_snapshots()             # never mutate the scene before the snapshot lands
    items, cx, cy = lines_to_items(lines)
    if not items:
        return "nothing"
    local = WORK / "items.json"
    local.write_text(json.dumps(items))
    subprocess.run(["scp", "-q", "-o", "ControlPath=/tmp/rmgrade-ssh-%r@%h",
                    str(local), f"{RM}:/home/root/rmgrade/items.json"], check=True)
    r = mb("aiItems", f"/home/root/rmgrade/items.json,{cx:.1f},{cy:.1f}", wait=1)
    if r != "QUEUED":
        return r
    for _ in range(60):
        time.sleep(0.35)
        st = mb("aiStatus", wait=1)
        if st.startswith(("DONE", "ERR")):
            return st
    return "ERR poll-timeout"


def draw_all(lines: list[dict]) -> str:
    results = []
    for i in range(0, len(lines), MAX_BATCH):
        results.append(send_lines(lines[i:i + MAX_BATCH]))
        if results[-1].startswith("ERR"):
            break
    return "; ".join(results)


# ---------------------------------------------------------------------------
# annotation -> ink
# ---------------------------------------------------------------------------

def _colored(lines: list[dict], tone: str) -> list[dict]:
    tone = TONE_ALIAS.get(str(tone).lower(), tone)
    c, rgba = TONE_COLOR.get(tone, TONE_COLOR[DEFAULT_TONE])
    for ln in lines:
        ln["c"], ln["rgba"] = c, rgba
    return lines


VERDICT_RECT = (1445, 118, 140, 100)     # canonical px; default top-right slot


def verdict_slot(ink_mask) -> tuple[float, float]:
    """Scene offset for the verdict glyph: keep the top-right slot when clear,
    otherwise slide left along the top band until it is."""
    if ink_mask is None:
        return (0.0, 0.0)
    x0, y0, w, h = VERDICT_RECT
    for dy in (0, 110, 220):
        for dx in (0, -150, -300, -450, -600, -900, -1200):
            if not ink_mask.occupied(x0 + dx, y0 + dy, w, h):
                ink_mask.reserve(x0 + dx, y0 + dy, w, h)
                return (float(dx), float(dy))
    ink_mask.reserve(x0, y0, w, h)
    return (0.0, 0.0)


def verdict_lines(verdict: str, offset: tuple[float, float] = (0.0, 0.0)) -> list[dict]:
    """The always-present page verdict, top-right, big and unambiguous."""
    v = (verdict or "").lower()
    out: list[dict] = []
    ox, oy = offset
    def sh(pts):
        return [[p[0] + ox, p[1] + oy] for p in pts]

    if v == "correct":
        out.append({"pts": sh([[655, 175], [683, 205], [745, 130]]), "_w": 30})
        return _colored(out, "praise")
    if v == "wrong":
        out.append({"pts": sh([[665, 135], [740, 205]]), "_w": 30})
        out.append({"pts": sh([[665, 205], [740, 135]]), "_w": 30})
        return _colored(out, "error")
    if v == "mixed":
        t = _colored([{"pts": sh([[650, 190], [668, 208], [700, 155]]), "_w": 26}], "praise")
        x = _colored([{"pts": sh([[715, 150], [755, 190]]), "_w": 26},
                      {"pts": sh([[715, 190], [755, 150]]), "_w": 26}], "error")
        return t + x
    import math
    cx, cy, r = 703 + ox, 170 + oy, 42
    for k in range(0, 12):
        t0 = k * math.pi / 6
        t1 = t0 + math.pi / 9
        out.append({"pts": [[cx + r * math.cos(t0), cy + 0.85 * r * math.sin(t0)],
                            [cx + r * math.cos(t1), cy + 0.85 * r * math.sin(t1)]], "_w": 26})
    return _colored(out, "error")


def note_bbox_scene(text: str) -> tuple[float, float]:
    """Width/height the note will occupy in scene units."""
    polys, y_end = text_to_polylines(text[:220], 0, 0, NOTE_SIZE, max_width=NOTE_WRAP)
    if not polys:
        return (0.0, 0.0)
    w = max(pt[0] for p in polys for pt in p)
    return (min(w, NOTE_WRAP), y_end)


class ClearSpace:
    """Whitespace finder over the user's ink. Guarantees zero-overlap placements:
    dilated ink mask (clearance from stroke edges), integral image for O(1) rect
    sums, whole-page grid search nearest to the requested anchor, and a
    below-all-ink fallback that is always clean."""

    PAD = 28          # px clearance around every placement
    GRID = 24         # search grid step

    def __init__(self, ink_mask):
        import numpy as np
        # <220, not <150: printed workbook furniture (grey labels ~160-216,
        # hairlines) must count as occupied or notes land on top of it.
        # Empty e-ink paper reads 245-255, so the margin is real.
        arr = (np.asarray(ink_mask) < 220)
        arr[:, :140] = False      # toolbar column is not "ink"
        arr[:100, :] = False      # top chrome
        # dilate ~8px by shift-OR so clearance is from stroke EDGES
        d = arr.copy()
        for sh in (4, 8):
            d[sh:, :] |= arr[:-sh, :]; d[:-sh, :] |= arr[sh:, :]
            d[:, sh:] |= arr[:, :-sh]; d[:, :-sh] |= arr[:, sh:]
        self.arr = d
        self.H, self.W = d.shape
        self.integral = d.astype(np.int32).cumsum(0).cumsum(1)
        ys, xs = arr.nonzero()
        self.ink_bottom = int(ys.max()) if len(ys) else 100
        self.placed: list[tuple[float, float, float, float]] = []

    def _sum(self, x, y, w, h):
        I = self.integral
        x, y, w, h = int(x), int(y), int(w), int(h)
        x2, y2 = min(x + w, self.W - 1), min(y + h, self.H - 1)
        x1, y1 = max(x - 1, 0), max(y - 1, 0)
        return int(I[y2, x2] - I[y1, x2] - I[y2, x1] + I[y1, x1])

    def occupied(self, x, y, w, h) -> bool:
        return self._sum(x, y, w, h) > 0

    def reserve(self, x, y, w, h) -> None:
        """Mark a rect as taken so find() never places anything into it."""
        self.placed.append((float(x), float(y), float(w), float(h)))

    def find(self, req_px, bw, bh, max_dist: float | None = None):
        """Nearest clear rect to req_px. With max_dist set, returns None instead
        of teleporting the note across the page — the caller demotes or drops."""
        bw, bh = int(bw) + 2 * self.PAD, int(bh) + 2 * self.PAD
        rx, ry = float(req_px[0]), float(req_px[1])
        cands = []
        for y in range(104, max(105, self.H - bh - 8), self.GRID):
            for x in range(144, max(145, self.W - bw - 8), self.GRID):
                cands.append(((x - rx) ** 2 + (y - ry) ** 2, x, y))
        def hits_placed(x, y):
            for (px_, py_, pw_, ph_) in self.placed:
                if x < px_ + pw_ and x + bw > px_ and y < py_ + ph_ and y + bh > py_:
                    return True
            return False

        cands.sort()
        for d2, x, y in cands:
            if max_dist is not None and d2 > max_dist * max_dist:
                break
            if self._sum(x, y, bw, bh) == 0 and not hits_placed(x, y):
                self.placed.append((x, y, bw, bh))
                return (float(x + self.PAD), float(y + self.PAD))
        if max_dist is not None:
            return None
        # always-clean fallback: below every stroke AND below anything already placed
        fy = max([self.ink_bottom + 60] + [py_ + ph_ + 12 for (_, py_, _, ph_) in self.placed])
        fy = float(min(fy, self.H - bh - 8))
        self.placed.append((160.0, fy, bw, bh))
        return (160.0, fy)


# √ ≤ ≥ → ≠ now EXIST as real human strokes (see human_glyphs.json), so these
# stopped being downgrades and became upgrades: fold the ASCII spellings UP into
# the glyphs instead of spelling the glyphs out. ✓ ☐ have no glyph yet.
# "×" is NOT a downgrade — the font has U+00D7. Mapping it to the letter x made
# the multiplication sign and the variable x the same character on a page that
# uses both ("5 × 10⁻³" next to "solve for x"). ✓ ☐ still have no glyph.
_NO_GLYPH = [("->", "→"), (">=", "≥"), ("<=", "≤"), ("!=", "≠"), ("sqrt", "√"),
             ("✓", "yes"), ("☐", ""), ("—", "-")]
# √ ≤ ≥ → ≠ are not in EMS Casual Hand; they come from the optional human glyph bank
# (human_glyphs.json, built by tools/, not shipped). Without it, spell them in ASCII
# instead of letting the renderer draw blank space.
try:
    import ems as _ems
    _MISSING = [u for u in "→≥≤≠√" if ord(u) not in _ems._GLYPHS]
except Exception:
    _MISSING = list("→≥≤≠√")
if _MISSING:
    _NO_GLYPH = [(b, g) for b, g in _NO_GLYPH if g not in _MISSING] + \
        [(g, b) for b, g in _NO_GLYPH if g in _MISSING]
CHAR_CAPS = {"Q:": 96, "Pace:": 96, "Next:": 60}
DEFAULT_CAP = 64


def _sanitize(text: str) -> str:
    for bad, good in _NO_GLYPH:
        text = text.replace(bad, good)
    return " ".join(text.split())


def _cap_for(text: str) -> int:
    for lead, cap in CHAR_CAPS.items():
        if text.startswith(lead):
            return cap
    return DEFAULT_CAP


def render_annotation(a: dict, ink_mask=None, view_scale: float = 1.0) -> list[dict]:
    kind = a.get("kind")
    tone = a.get("tone", DEFAULT_TONE)
    out: list[dict] = []

    if kind == "strike":
        (x1, y1) = px_to_scene(*a["from_px"])
        (x2, y2) = px_to_scene(*a["to_px"])
        out.append({"pts": [[x1, y1], [(x1 + x2) / 2, (y1 + y2) / 2 - 3], [x2, y2]], "_w": 30})
        return _colored(out, "error")

    if kind == "sketch":
        polys = [p[:200] for p in a.get("polylines_px", [])[:80] if len(p) >= 2]
        if not polys:
            return []
        xs = [pt[0] for p in polys for pt in p]; ys = [pt[1] for p in polys for pt in p]
        x0, y0, w, h = min(xs), min(ys), max(xs) - min(xs), max(ys) - min(ys)
        dx = dy = 0.0
        if ink_mask is not None:
            nx, ny = ink_mask.find((x0, y0), w, h)
            dx, dy = nx - x0, ny - y0
        for poly in polys:
            pts = [list(px_to_scene(pt[0] + dx, pt[1] + dy)) for pt in poly]
            out.append({"pts": pts})
        return _colored(out, tone)

    if kind == "note":
        text = _sanitize(a.get("text", ""))
        if len(text) > _cap_for(text):
            print(f"[rmreview]   REJECT over-cap note ({len(text)} chars): {text[:40]}...")
            return []
        req = list(a["px"])
        is_footer = text.startswith(("Pace:", "Q:", "Next:"))
        if not is_footer and req[0] < 1100:
            req[0] = 1160.0          # notes live in the right gutter
        if ink_mask is not None:
            w_s, h_s = note_bbox_scene(text)
            req = list(ink_mask.find(tuple(req), w_s * view_scale, h_s * view_scale))
        a["_placed_px"] = tuple(req)   # leaders re-bind to this
        sx, sy = px_to_scene(*req)
        lead, sep, body = text.partition(":")
        if sep and len(lead) <= 8:
            lp, _ = text_to_polylines(lead + ":", sx, sy, NOTE_SIZE * 1.25,
                                      max_width=NOTE_WRAP)
            lead_w = max((pt[0] for pp in lp for pt in pp), default=sx) - sx
            out += [{"pts": pp, "_w": 22} for pp in lp]
            out.append({"pts": [[sx - 14, sy + NOTE_SIZE * 0.6],
                                [sx - 4, sy + NOTE_SIZE * 0.6]], "_w": 22})
            bp, _ = text_to_polylines(body.strip(), sx + lead_w + 10, sy,
                                      max(NOTE_SIZE, 24), max_width=NOTE_WRAP - lead_w - 10)
            out += [{"pts": pp, "_w": 14} for pp in bp]
        else:
            polys, _ = text_to_polylines(text, sx, sy, NOTE_SIZE, max_width=NOTE_WRAP)
            out += [{"pts": pp, "_w": 14} for pp in polys]

    elif kind == "arrow":
        (x1, y1) = px_to_scene(*a["from_px"])
        (x2, y2) = px_to_scene(*a["to_px"])
        out.append({"pts": [[x1, y1], [x2, y2]], "_w": 30})
        # arrowhead
        import math
        ang = math.atan2(y2 - y1, x2 - x1)
        for da in (2.6, -2.6):
            out.append({"pts": [[x2, y2],
                                [x2 + 18 * math.cos(ang + da), y2 + 18 * math.sin(ang + da)]]})

    elif kind == "underline":
        (x1, y1) = px_to_scene(*a["from_px"])
        (x2, y2) = px_to_scene(*a["to_px"])
        out.append({"pts": [[x1, y1], [x2, y2]]})

    elif kind == "highlight":
        # t18 translucent band renders UNDER pen ink (INK-LAB finding):
        # lay it over the writer's own line; the text stays readable through it
        (x1, y1) = px_to_scene(*a["from_px"])
        (x2, y2) = px_to_scene(*a["to_px"])
        out.append({"pts": [[x1, y1], [x2, y2]], "_w": 44, "t": 18})

    elif kind == "circle":
        import math
        (cx, cy) = px_to_scene(*a["px"])
        rx = float(a.get("rx", a.get("r", 40)))
        ry = float(a.get("ry", 0.7 * rx))
        if a.get("dashed"):
            for k in range(12):
                t0 = k * math.pi / 6
                t1 = t0 + math.pi / 9
                out.append({"pts": [[cx + rx * math.cos(t0), cy + ry * math.sin(t0)],
                                    [cx + rx * math.cos(t1), cy + ry * math.sin(t1)]],
                            "_w": 26})
        else:
            pts = [[cx + rx * math.cos(t * math.pi / 12), cy + ry * math.sin(t * math.pi / 12)]
                   for t in range(25)]
            out.append({"pts": pts})

    elif kind == "tie":
        # red L-path joining two of the writer's own lines. The vertical rail is placed
        # in the LEAST-INKED corridor right of both endpoints so the path runs
        # through clear paper, never across the writer's hand.
        import math
        fx, fy = a["from_px"]
        tx, ty = a["to_px"]
        y_lo, y_hi = min(fy, ty) - 10, max(fy, ty) + 10
        best_x, best_ink = None, None
        start = int(max(fx, tx)) + 30
        for rx_px in range(start, 1585, 18):
            if ink_mask is None:
                break
            ink = (ink_mask._sum(rx_px - 12, y_lo, 24, y_hi - y_lo)
                   + ink_mask._sum(fx + 8, fy - 10, max(1, rx_px - fx - 8), 20)
                   + ink_mask._sum(tx + 8, ty - 10, max(1, rx_px - tx - 8), 20))
            if best_ink is None or ink < best_ink:
                best_x, best_ink = rx_px, ink
            if ink == 0:
                break
        rail_px = float(a.get("rail_px") or best_x or (max(fx, tx) + 36))
        (x1, y1) = px_to_scene(fx, fy)
        (x2, y2) = px_to_scene(tx, ty)
        rx_ = px_to_scene(rail_px, 0)[0]
        out.append({"pts": [[x1, y1], [rx_, y1]], "_w": 26})
        out.append({"pts": [[rx_, y1], [rx_, y2]], "_w": 26})
        out.append({"pts": [[rx_, y2], [x2, y2]], "_w": 26})
        if a.get("head", True):
            ang = math.atan2(0, x2 - rx_) or 3.1416
            for da in (2.6, -2.6):
                out.append({"pts": [[x2, y2],
                                    [x2 + 18 * math.cos(ang + da), y2 + 18 * math.sin(ang + da)]],
                            "_w": 26})
        return _colored(out, "error")

    return _colored(out, tone)


# ---------------------------------------------------------------------------
# the reviewer
# ---------------------------------------------------------------------------

PROMPT_HEAD = """Read the handwritten work in the image file {png} (a 1620x2160 image of a reMarkable tablet page; the left ~160px and top ~110px are tablet UI — never place a mark there). You are an exam-prep tutor marking one page of a student's scratch work with three pens; your review is rendered as REAL INK on the writer's page. Coordinates are IMAGE PIXELS.

{see_block}

{student_block}
{stroke_block}
RECONSTRUCTION LAW — arithmetic and chronology, never psychology:
- The trace is the writer's attempt replayed as rows, in STROKE ORDER (when the block above exists) — one row per ink event, each row a transcription of what that event says, at most 34 characters.
- Label each row "read" (you can read it off the page), "derived" (forced by two numbers that ARE on the page), or "unsure" (you are guessing any digit or word). AT MOST ONE unsure row; if you would need two, emit no trace at all and set read_confidence "low".
- You may not assert an ink event that is not in the stroke list, and you may not claim a derivation chain the ink does not force. If the writer's committed value equals a plain benchmark recall (e.g. 87.5 = 7/8), a mis-recall is a LIVE alternative to any arithmetic chain: report both in "ambiguity" and route the question into the Q — never ink either story as fact.
- The break row states the FIRST place the protocol or arithmetic failed, in observable terms, at most 48 characters, plus a "disproof": one true equation a stranger could verify cold (e.g. "24 x 2.5 = 60, not 100").

SHOW-THE-WORK LAW (follow it exactly):
- Page WRONG (committed and incorrect): one annotation must STRIKE or circle the exact wrong ink, and route.steps must be the remaining work CONTINUED from that break — one line per step, every value actually computed, ending with the final answer written out. The writer should be able to read exactly what went wrong and how the work should have continued from the crossed-off spot.
- Page OPEN with "no idea"/"not sure"/a blank answer slot: route.steps = the COMPLETE solution from the first line, line by line, real values at every line, ending with the final answer. Nothing withheld.
- Page CORRECT BUT THE METHOD WAS OFF — this requires POSITIVE evidence in the writer's ink: compensating errors that cancel, written work that contradicts the answer, or a clearly inefficient grind where a short route exists. Set "method_flawed": true, mark the flawed step, and write the clean full method ending at the answer, exactly as for a wrong page. HARD RULE: sparse or absent scratch is NEVER evidence of anything — these pages are TIMED and much of the work rightly happens in the writer's head. A correct answer with little or no written work is simply CORRECT. Inferring a guess from an empty margin is the same banned move as inferring motive.
- Page CORRECT with a sound, efficient method: NO route at all. An empty margin is a valid review of a good page.
- These rules apply to EVERY wrong/flawed/open page in a batch — never just the worst one. The full worked route is the product.
- THE GATE: after the route, one cheap independent check the writer can run unaided ("1/25 is 4, so 1/24 is past 4").
- Every equation you write WILL be evaluated by the renderer before it is inked. A false equation destroys the whole board. Words about feelings, motives, attention, or test day are rejected by a filter — do not write them.

ROUTE LAW: "source" must be "bank" (a verified solution was supplied above) or "self_evident" (every step is checkable arithmetic). With an answer but no verified solution, do NOT teach a named technique — write the work as plain arithmetic lines that reach the verified answer, or set source "none" and say in the q why you could not. Up to 8 steps; each <=56 chars; split long derivations across steps rather than cramming. THE PEN CAN NOW WRITE √ ≤ ≥ → ≠ — use them directly ('√7', 'x ≥ 5', '3x ≥ 21 → x ≥ 7', 'total ≠ 45'), never the spelled-out 'sqrt7' / 'differs' / '>='. The √ overbar covers ONE following character, so keep radicands to a single digit or symbol. There is still no check-mark glyph.

ANNOTATIONS (at most 4, all anchored to THE WRITER'S ink, pixels required):
- strike (through a wrong step), circle (px, rx, ry, dashed: dashed = open loop, solid red = committed and wrong), underline (blue = the writer's own work proves it; green = keep this habit), highlight (from_px, to_px: a translucent band OVER the one line of the writer's own work that already proves the point), tie (from_px, to_px: a red elbow path joining two of the writer's own lines that disagree — routed around the writer's ink automatically), arrow, sketch (blue polylines, e.g. a real fraction bar with cancel strokes).
- Only red may touch the writer's handwriting. Dashed = open, solid = wrong. Green: at most one, only on a habit that caused a win on THIS page; no clean rep, no green.

HEADLINE: at most 24 characters, no lead word — outcome plus cause in plain words ("committed, then checked"). It is the 2-second glance layer.

FOOTER: pace = ONE plain instruction anyone could follow, no jargon ("Rebuild the 1/24 ladder once in ink", "Redo the divide: 160/4"). It renders as "Do now: ..." with a tick box labelled done. OMIT pace entirely on a clean page that needs nothing. q = ONE open question, no hypothesis, with 2-3 tick-box labels that are observable categories (never motives) plus "other ___"; it renders as "Q (tick one): ...". When an "ambiguity" exists, the q MUST ask it (e.g. "87.5 - from 7/8, or from 100-5x2.5?").

{section}

EVERY page gets a verdict first: "correct" (committed, right, closed), "wrong" (committed and incorrect), "open" (no commitment), "mixed" (several problems, different outcomes). A large glyph is drawn from this automatically.

Reply with ONLY this JSON, nothing else:
{{"verdict": "correct" | "wrong" | "open" | "mixed",
  "method_flawed": false,
  "committed_choice": "A-E, the letter the writer committed to, or null",
  "time_s": "the writer's handwritten elapsed time in seconds (often near the answer slot, e.g. time 1min 53.74sec = 114), or null",
  "read_confidence": "high" | "partial" | "low",
  "headline": "<=24 chars",
  "summary": "<2 sentences for the log - never inked>",
  "trace": [{{"n": 1, "event": "e1", "text": "<=34ch transcription", "read": "read|derived|unsure", "state": "tick|slash|open|"}}],
  "break": {{"at": 1, "fact": "<=60ch observable", "disproof": "one true equation"}},
  "ambiguity": null | {{"committed": "...", "alternatives": ["...", "..."]}},
  "route": {{"source": "self_evident|bank|none", "steps": [{{"text": "<=56ch, real values, one work line", "check": "true arithmetic e.g. 100-5*(100/24)>79"}}], "gate": "<=56ch"}},
  "footer": {{"pace": "<=90ch", "q": {{"stem": "<=90ch", "boxes": ["a", "b", "other ___"]}}}},
  "annotations": [{{"kind": "circle", "px": [x, y], "rx": 60, "ry": 40, "dashed": false, "tone": "error"}},
                  {{"kind": "strike", "from_px": [x, y], "to_px": [x, y]}},
                  {{"kind": "tie", "from_px": [x, y], "to_px": [x, y]}},
                  {{"kind": "underline", "from_px": [x, y], "to_px": [x, y], "tone": "guide"}},
                  {{"kind": "highlight", "from_px": [x, y], "to_px": [x, y], "tone": "praise"}},
                  {{"kind": "sketch", "polylines_px": [[[x, y], [x, y]]], "tone": "guide"}}]}}

Omit "trace"/"break"/"route" entirely ONLY on a correct page whose method was sound and efficient (headline + pace + at most a green underline is a complete review of such a page). Omit them too when there is no STROKE ORDER block — without observed chronology you may only anchor, never reconstruct."""

STUDENT_DEFAULT = ("THE STUDENT: depth and durable fixes beat exam-day triage. Never infer WHY "
                   "a step was missed; observable mechanics only; motive questions go in the Q.")
STUDENT_BLOCK = os.environ.get("GRADER_STUDENT_NOTE") or STUDENT_DEFAULT

SEE_DEFAULT = """WHAT YOU CAN SEE: only this page. You do NOT have the question stem, the answer choices, the passage, the data source, or the official key. Never assert anything you cannot read off this page. If a value's source is off-page, you may ask about it but you may not grade it."""

PLAYBOOK_MATH = """MATH WORK PLAYBOOK (priority order):
1. OPEN LOOPS - any expression set up and never evaluated, any "come back later"/skip. The most common gap. Dashed red circle + count them into pace.
2. THE MISSING INVARIANT - if the writer computed before naming what is fixed (total, rate sum, constraint), name it in the break row or route step 1.
3. THE SINKHOLE - 5+ scratch lines on one problem, crossed-out restarts, long by-hand arithmetic: the route must show the cheaper path the writer passed by.
4. On a broken page the route is mandatory when a legal source exists; on a page closed correctly but expensively, route rows show the shorter path with a step count.
5. Green underline goes to a CLOSED loop (named, computed, finished), never to labelling or speed."""

PLAYBOOK_READING = """READING AND ARGUMENT WORK PLAYBOOK (priority order):
1. The chosen answer ALWAYS gets exactly one mark, even when correct: red circle outside the writer's own ring if wrong, green underline if right.
2. THE MATCH-STROKE: if the writer predicted the answer before reading the choices, one blue polyline (sketch) from that prediction line to the choice that restates it.
3. THE TRACE ON A READING PAGE IS THE ELIMINATION LADDER: one row per answer choice IN THE ORDER THE WRITER ACTED (stroke order), text like 'A dead - new actor' / 'C kept alive' / 'E committed'. State per row: tick = sound elimination, slash = unsound elimination or premature commit, open = never touched. The break = the first unsound elimination, or the commit while a better choice was still live.
4. Ring the writer's own hesitation marks ("?", "maybe") in thin blue when they pointed at the answer.
5. Trap vocabulary only from: scope-shift, alt-cause, reversed-direction, quantifier-creep, true-but-irrelevant, half-right-half-wrong. If the writer's reason word cannot be read, it goes to the q, not the trace.
6. Passage maps: mark FUNCTION not content (claim/evidence/counter/concede/resolve), one word per paragraph."""

PLAYBOOK_DATA = """DATA WORK PLAYBOOK (priority order):
1. CROSS-PART CONTRADICTION - one of the writer's own lines disproves a later commitment: blue underline under the proving line, solid red circle outside the answer ring, a tie joining the two heights, and the break row names it.
2. SAME-LETTER both parts: circle both, ask in the q whether part 2 was recomputed or reused.
3. PROVENANCE - any figure whose source is off-page and unverifiable: do not grade it; note it in the q. Never guess whether a transcription is right.
4. THE TRACE ON A DATA PAGE IS THE PART LEDGER: one row per part/statement/blank — text like 'part 1: wrote B (key C)' when the key is known, else the writer's bookkeeping step in stroke order. State: tick = part right, slash = part wrong, open = skipped. The break = the stale sort / carried letter / wrong-cell moment, stated observably.
5. Never state a heuristic you cannot verify from this page or the supplied ground truth."""

# ctx["work_type"] selects one playbook; without it the model classifies the page.
PLAYBOOKS = {"math": ("MATH WORK", PLAYBOOK_MATH),
             "reading": ("READING AND ARGUMENT WORK", PLAYBOOK_READING),
             "data": ("DATA WORK", PLAYBOOK_DATA)}


WARP_NOTE = """

CAPTURE NOTE: the view was zoomed when this page was captured, so the image was
re-projected onto the full page. Solid GREY areas were OFF-SCREEN or under the
tablet UI at capture time: their content is unknown. Never grade, mention, or
place any mark inside a grey area. If most of the page is grey, review only what
is visible and say so in the summary."""


TRIAGE_SUFFIX = """

TRIAGE MODE: you are the fast first-pass reviewer. If this page is a fully closed,
clean rep — final answer committed, every expression evaluated, no crossed-out
restarts, no skip/defer notes, the METHOD sound and efficient (a right answer via
compensating errors, a bare guess-looking commit, or a needless grind is NOT
clean — escalate it), and you are CONFIDENT the answer is right (use the
verified answer if one was supplied above) — produce the normal JSON review:
verdict "correct", a headline, footer.pace, at most one green underline
annotation, and NO trace/break/route. You may ONLY produce a review when the page
is clean AND the answer is right. If the answer is wrong, missing, or you have
ANY doubt — even if you can name the error yourself — reply with EXACTLY
{"escalate": true} and nothing else. Never mark a flawed page; that is the deep
reviewer's job."""


def _see_block(ctx: dict | None) -> str:
    if not ctx:
        return SEE_DEFAULT
    kind = ctx.get("kind")
    if kind == "scoverp" and not ctx.get("resolved"):
        return (SEE_DEFAULT + "\nNOTE: this is a labelled workspace page — the question "
                "was served on screen and is NOT printed here. Grade only the work.")
    if kind not in ("bank", "static", "scoverp"):
        return SEE_DEFAULT
    out = ["GROUND TRUTH FOR THIS PAGE (verified, from the student's own question bank):"]
    if ctx.get("label"):
        out.append(f"- item: {ctx['label']}" + (f"  (part {ctx['part'][0]} of {ctx['part'][1]})" if ctx.get("part") else ""))
    for k, lbl in (("tier", "tier"), ("chapter", "chapter"), ("sub_skill", "sub-skill"), ("work_type", "work type")):
        if ctx.get(k):
            out.append(f"- {lbl}: {ctx[k]}")
    if ctx.get("stem"):
        # passages and multi-source data run long; plain problems stay short
        _cap = 2400 if ctx.get("work_type") in ("reading", "data") else 1400
        out.append(f"- stem: {str(ctx['stem'])[:_cap]}")
    if ctx.get("choices"):
        # lettered, one per line — a reading reviewer's whole task is per-letter
        try:
            ch = ctx["choices"]
            ch = json.loads(ch) if isinstance(ch, str) else ch
            assert isinstance(ch, (list, tuple)) and ch
            out.append("- choices:")
            for i, c in enumerate(ch[:8]):
                out.append(f"    {chr(65 + i)}) {str(c)[:240]}")
        except Exception:
            out.append(f"- choices: {str(ctx['choices'])[:400]}")
    if ctx.get("answer"):
        out.append(f"- VERIFIED ANSWER: {ctx['answer']}. Use it to judge. On a WRONG or "
                   "unanswered page your written work must END at this answer (show-the-work "
                   "law). On a correct page do not restate it — there is nothing to teach.")
    if kind == "scoverp":
        out.append("- MOCK PAGE: the question was served ON SCREEN (not printed here); "
                   "the block above is the recorded ground truth for this page's question.")
        if ctx.get("writer_display") or ctx.get("writer_answer"):
            secs = ctx.get("writer_seconds")
            out.append(f"- THE WRITER'S RECORDED ANSWER on screen: {ctx.get('writer_display') or ctx.get('writer_answer')}"
                       + (f" in {int(secs)}s" if secs else "")
                       + " — grade the scratch work as the path that led there.")
    if kind in ("bank", "scoverp"):
        if ctx.get("trap_map"):
            out.append("- trap map (pre-verified wrong-answer diagnoses):")
            for k, v in list(ctx["trap_map"].items())[:7]:
                out.append(f"    {k}: {str(v)[:260]}")
        if ctx.get("solution"):
            out.append(f"- VERIFIED SOLUTION (route source \"bank\"): {str(ctx['solution'])[:1400]}")
        else:
            out.append("- no verified solution on file: route only from self-evident arithmetic.")
    else:
        out.append("- NO verified solution and no trap map exist for this item. You know THAT "
                   "an answer is right or wrong, not the official WAY. Do NOT teach a named "
                   "technique; write the work as plain arithmetic lines that reach the "
                   "verified answer, or set route source \"none\".")
    return "\n".join(out)


def build_prompt(png: Path, ctx: dict | None, events: list[dict] | None) -> str:
    sb = strokes_mod.prompt_block(events or [])
    clip = (ctx or {}).get("clipped") or 0
    if clip:
        sb = (f"!! PARTIAL PAGE: {clip} of the writer's strokes run past the page edge and are NOT "
              f"in this image. Work continues where you cannot see it. You may NOT conclude "
              f"the writer stopped early, left a loop open, never finished, or never committed — "
              f"the ending may be off-page. Grade only what is visible; if the visible work "
              f"stops mid-route, say the page is truncated, not that the writer abandoned it.\n\n"
              + sb)
    wt = str((ctx or {}).get("work_type") or "").lower()
    if wt in PLAYBOOKS:
        name, book = PLAYBOOKS[wt]
        section = f"This page is {name} — known from the item record.\n\n" + book
    else:
        section = ("FIRST classify the page as math work, reading and argument work, or data "
                   "work and apply that playbook.\n\n"
                   + "\n\n".join(book for _, book in PLAYBOOKS.values()))
    return PROMPT_HEAD.format(png=png, see_block=_see_block(ctx),
                              student_block=STUDENT_BLOCK,
                              stroke_block=("\n" + sb + "\n") if sb else "\n",
                              section=section)


def page_assets(pid: str) -> dict | None:
    """Locate the open notebook for a page id: ground truth + file-pull assets."""
    if not pid:
        return None
    store = "/home/root/.local/share/remarkable/xochitl"
    nb = rmssh(f"grep -l '{pid}' {store}/*.content | head -n 1")
    if not nb:
        return None
    uuid = Path(nb).stem
    content_local = WORK / "nb.content"
    try:
        subprocess.run(["scp", "-q", "-o", "ControlPath=/tmp/rmgrade-ssh-%r@%h",
                        f"{RM}:{nb}", str(content_local)], check=True, timeout=40)
        cont = json.loads(content_local.read_text())
    except (subprocess.SubprocessError, OSError, json.JSONDecodeError):
        return None
    cpages = cont.get("cPages", {}).get("pages", [])
    redir = next((p.get("redir", {}).get("value") for p in cpages if p.get("id") == pid), None)
    pdf_local = Path(__file__).parent / "cache" / f"{uuid}.pdf"
    if cont.get("fileType") == "pdf" and not pdf_local.exists():
        pdf_local.parent.mkdir(exist_ok=True)
        try:
            subprocess.run(["scp", "-q", "-o", "ControlPath=/tmp/rmgrade-ssh-%r@%h",
                            f"{RM}:{store}/{uuid}.pdf", str(pdf_local)], check=True, timeout=180)
        except (subprocess.SubprocessError, OSError):
            pass
    pdf = pdf_local if pdf_local.exists() else None
    return {"uuid": uuid, "redir": redir, "pdf": pdf, "store": store,
            "ctx": _page_context(pdf, redir)}


CLAUDE_BIN = os.environ.get("GRADER_CLAUDE_BIN", "claude")
TRIAGE_MODEL = os.environ.get("GRADER_TRIAGE_MODEL", "sonnet")


def review(png: Path, model: str | None = None, triage: bool = False,
           warped: bool = False, ctx: dict | None = None,
           events: list[dict] | None = None) -> dict:
    env = dict(os.environ)
    env.pop("ANTHROPIC_API_KEY", None)
    prompt = (build_prompt(png, ctx, events) + (WARP_NOTE if warped else "")
              + (TRIAGE_SUFFIX if triage else ""))
    model = model or os.environ.get("GRADER_MODEL") or None
    cmd_model = ["--model", model] if model else []
    last_err = ""
    for _ in (1, 2):
        try:
            r = subprocess.run(
                [CLAUDE_BIN, "-p", prompt, *cmd_model, "--max-turns", "6",
                 "--allowedTools", "Read", "--output-format", "json"],
                capture_output=True, text=True, timeout=300, env=env,
            )
        except subprocess.TimeoutExpired:
            last_err = "timeout"
            continue
        if r.returncode != 0:
            last_err = (r.stderr.strip() or r.stdout.strip() or f"rc={r.returncode}")[:200]
            time.sleep(2)
            continue
        try:
            outer = json.loads(r.stdout)
            text = outer.get("result", "")
            d = json.loads(text[text.find("{"):text.rfind("}") + 1])
            d["cost_usd"] = outer.get("total_cost_usd")
            return d
        except (json.JSONDecodeError, ValueError):
            last_err = f"unparseable: {r.stdout[:150]}"
    return {"summary": f"review failed: {last_err}", "annotations": []}


def review_tiered(png: Path, warped: bool = False, ctx: dict | None = None,
                  events: list[dict] | None = None) -> dict:
    """Fast cheap pass for clean pages; escalate everything else to the full model."""
    t = review(png, model=TRIAGE_MODEL, triage=True, warped=warped, ctx=ctx, events=events)
    if t.get("escalate") or str(t.get("summary", "")).startswith("review failed"):
        return review(png, warped=warped, ctx=ctx, events=events)
    t["_tier"] = "fast"
    return t


LEDGER = WORK / "ledger.jsonl"


def ledger_rows() -> list[dict]:
    rows = []
    try:
        for line in open(LEDGER):
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                pass
    except OSError:
        return []
    return rows


def _latest_boxes(pid: str) -> dict | None:
    # newest ledger row for this page that logged tick-box rects
    best = None
    for e in ledger_rows():
        if e.get("page_id") == pid and e.get("boxes"):
            if best is None or e.get("ts", 0) >= best.get("ts", 0):
                best = e
    return best


def _cap_seconds(stem):
    # the book prints a per-page time cap: CAP 0:30, CAP 2:30 ...
    import re as _re
    m = _re.search(r"CAP\s+(\d+):(\d\d)", stem or "")
    return int(m.group(1)) * 60 + int(m.group(2)) if m else None


def _atomic_write(path: Path, text: str) -> None:
    # tmp-in-same-dir + fsync + rename: a crash can never leave a torn file
    # (a torn graded.json would re-ink the whole notebook)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w") as f:
        f.write(text)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def _ledger_append(entry: dict) -> None:
    try:
        with open(LEDGER, "a") as f:
            f.write(json.dumps(entry) + "\n")
            f.flush()
            os.fsync(f.fileno())
    except OSError as e:
        print(f"[rmreview] WARN ledger append failed: {e}")


def _headline_lines(text: str, verdict: str, ink_mask) -> list[dict]:
    text = _sanitize(text)[:24]
    if not text:
        return []
    cap = 36.0
    from ems import measure as _measure
    w_px = _measure(text, cap, None)[0] + 24
    spot = ink_mask.find((1180, 250), w_px, cap * 1.8, max_dist=650)
    if spot is None:
        return []
    sx, sy = px_to_scene(*spot)
    polys, _ = text_to_polylines(text, sx, sy, cap, max_width=1500)
    from ems import naturalize
    tone = "error" if verdict in ("wrong", "mixed") else "guide"
    out = []
    for pts, ws in naturalize(polys, base_w=22):
        out.append({"pts": pts, "_w": ws})
    return _colored(out, tone)


def _footer_band(ink_mask, board_bottom: float,
                 n_boxes: int = 3) -> tuple[list[int], float] | None:
    # NEVER print on the writer's ink: text laid over handwriting makes both illegible.
    # Try the home band, then shift up, then ANY clear spot on the page; the
    # caller relocates the footer to a back page when nothing here is clean.
    total_h = 44 + 56 + max(1, n_boxes) * 44 + 12

    def band_clear(y0):
        return ink_mask._sum(180, y0 - 6, 1350, total_h) == 0

    for y0 in [1908] + [1908 - s for s in range(46, 700, 46)]:
        if y0 - 10 <= board_bottom + 8:
            break
        if band_clear(y0):
            ink_mask.reserve(180, y0 - 6, 1350, total_h)
            return [y0, y0 + 56, y0 + 100], 1.0
    spot = ink_mask.find((760, 1600), 1350, total_h, max_dist=1400)
    if spot is not None:
        x0, y0 = spot
        return [int(y0), int(y0) + 56, int(y0) + 100], 1.0
    return None


def _footer_lines(footer: dict, rows: list[int], fscale: float) -> tuple[list[dict], dict]:
    from ems import naturalize
    lines: list[dict] = []
    boxes: dict = {}
    size = 30 * fscale
    pace = _sanitize(str(footer.get("pace", "")))[:110]
    for _lead in ("Pace:", "Do now:"):
        if pace.startswith(_lead):
            pace = pace[len(_lead):].strip()
    if pace:
        pace = "Do now: " + pace
        sx, sy = px_to_scene(200, rows[0])
        polys, _ = text_to_polylines(pace, sx, sy, size, max_width=1300)
        for pts, ws in naturalize(polys, base_w=14):
            lines.append({"pts": pts, "_w": ws, "c": 6, "rgba": 0xFF0062CC})
        sq = [1452, rows[0], 30, 30]
        bx, by = px_to_scene(sq[0], sq[1])
        lines += [{"pts": p, "_w": 14, "c": 6, "rgba": 0xFF0062CC} for p in (
            [[bx, by], [bx + 30, by]], [[bx + 30, by], [bx + 30, by + 30]],
            [[bx + 30, by + 30], [bx, by + 30]], [[bx, by + 30], [bx, by]])]
        dl, _ = text_to_polylines("done", *px_to_scene(sq[0] + 40, sq[1] + 2), 20, max_width=200)
        lines += [{"pts": p, "_w": 12, "c": 6, "rgba": 0xFF0062CC} for p in dl]
        boxes["compliance"] = sq
    q = footer.get("q") or {}
    stem = _sanitize(str(q.get("stem", "")))[:110]
    for _lead in ("Q:", "Q (tick one):"):
        if stem.startswith(_lead):
            stem = stem[len(_lead):].strip()
    if stem:
        stem = "Q (tick one): " + stem
        sx, sy = px_to_scene(200, rows[1])
        polys, _ = text_to_polylines(stem, sx, sy, size, max_width=1300)
        for pts, ws in naturalize(polys, base_w=14):
            lines.append({"pts": pts, "_w": ws, "c": 6, "rgba": 0xFF0062CC})
        qrects = []
        n_boxes = len(list(q.get("boxes") or [])[:3])
        if rows[2] + n_boxes * 44 + 8 > 2152:
            n_boxes = max(0, (2152 - 8 - rows[2]) // 44)
            print(f"[rmreview]   footer: page edge — keeping {n_boxes} of the q boxes")
        for bi, label in enumerate(list(q.get("boxes") or [])[:n_boxes]):
            label = _sanitize(str(label))
            if len(label) > 48:
                cut = label.rfind(" ", 1, 49)
                label = label[:cut if cut > 0 else 48]
            by_px = rows[2] + bi * 44
            bx, by = px_to_scene(240, by_px)
            lines += [{"pts": p, "_w": 16, "c": 6, "rgba": 0xFF0062CC} for p in (
                [[bx, by], [bx + 34, by]], [[bx + 34, by], [bx + 34, by + 34]],
                [[bx + 34, by + 34], [bx, by + 34]], [[bx, by + 34], [bx, by]])]
            qrects.append({"rect": [240, by_px, 34, 34], "label": label})
            lp, _ = text_to_polylines(label, bx + 46, by, 24, max_width=1200)
            lines += [{"pts": p, "_w": 12, "c": 6, "rgba": 0xFF0062CC} for p in lp]
        boxes["q"] = qrects
    return lines, boxes


def apply_semantic(shot: Path, result: dict, events: list[dict],
                   ctx: dict | None, page_id: str = "",
                   cont_page: int | None = None) -> dict | None:
    """v24 apply: glance layer + ordered board + fixed footer, all validated."""
    from PIL import Image
    from ems import naturalize
    _im = Image.open(shot).convert("L")
    _ai_p = Path(str(shot).replace(".png", "-ai.png"))
    if _ai_p.exists():
        # prior tutor ink occupies space too — a re-grade must not overprint it
        import numpy as _np
        _im = Image.fromarray(_np.minimum(_np.asarray(_im),
                                          _np.asarray(Image.open(_ai_p).convert("L"))))
    ink_mask = ClearSpace(_im)
    lines: list[dict] = []
    route = result.get("route") or {}
    steps = route.get("steps") or []
    method = " | ".join(
        str(s.get("text", "") if isinstance(s, dict) else s) for s in steps)[:400]
    ledger_entry: dict = {
        "ts": int(time.time()), "run": CURRENT_RUN[0],
        "page_id": page_id, "verdict": result.get("verdict"),
        "method_flawed": bool(result.get("method_flawed")),
        "headline": result.get("headline"), "summary": result.get("summary"),
        "break": (result.get("break") or {}).get("fact"),
        "method": method or None, "source": route.get("source"),
        "committed_choice": result.get("committed_choice") or (ctx or {}).get("writer_answer"),
        "time_s": result.get("time_s") or (ctx or {}).get("writer_seconds"),
        "cap_s": _cap_seconds((ctx or {}).get("stem")),
        "item_id": (ctx or {}).get("item_id"), "redir": (ctx or {}).get("redir"),
        "work_type": (ctx or {}).get("work_type"),
        "cost_usd": result.get("cost_usd"), "ambiguity": result.get("ambiguity"),
        "read_confidence": result.get("read_confidence"),
        "label": (ctx or {}).get("label"), "day": (ctx or {}).get("day"),
        "chapter": (ctx or {}).get("chapter"), "sub_skill": (ctx or {}).get("sub_skill"),
        "tier": (ctx or {}).get("tier"), "kind": (ctx or {}).get("kind"),
        "block": (ctx or {}).get("block"), "boxes": {}}

    # 1. verdict glyph, slot-searched. Correct-but-flawed-method draws the
    # mixed glyph (tick + X): right answer, wrong engine.
    off = verdict_slot(ink_mask)
    glyph_v = result.get("verdict", "open")
    lucky = glyph_v == "correct" and result.get("method_flawed")
    if lucky:
        glyph_v = "mixed"
    lines += verdict_lines(glyph_v, off)
    caption = {"correct": "right", "wrong": "wrong", "open": "left open",
               "mixed": "right, method off" if lucky else "mixed page"}.get(
                   glyph_v, "")
    if caption:
        ctone = {"correct": "praise", "wrong": "error"}.get(glyph_v, "guide")
        cx, cy = 655 + off[0], 232 + off[1]          # scene coords, under glyph
        cp, _ = text_to_polylines(caption, cx - 20, cy, 22, max_width=300)
        lines += _colored([{"pts": p, "_w": 14} for p in cp], ctone)

    # 2. headline
    lines += _headline_lines(str(result.get("headline", "")), result.get("verdict", ""), ink_mask)

    # 3. anchors on the writer's work (<=4, in order; tie/circle/strike/underline/arrow/sketch)
    anns = [a for a in (result.get("annotations") or []) if isinstance(a, dict)][:4]
    for a in anns:
        try:
            lines += render_annotation(a, ink_mask, 1.0)
        except (KeyError, TypeError, ValueError) as e:
            print(f"[rmreview]   skipped malformed annotation: {e}")

    # 4. the board (only when stroke order exists — file-pull pages)
    board_bottom = 1935.0
    overflow = None
    answer = (ctx or {}).get("answer")
    if events and result.get("trace"):
        v = board.validate(result, events, answer)
        for r in v.get("reasons", []):
            print(f"[rmreview]   board gate: {r}")
        if v["mode"] != "none":
            # the fold clears STUDENT ink (stroke events are ground truth);
            # printed furniture below is tolerated up to a light-occupancy cap
            student_bottom = max((e["bbox"][1] + e["bbox"][3]) for e in events)
            fold_y = min(max(student_bottom + 70, 1250), 1885)
            avail = 1935 - (fold_y + 40)
            def room_ok(h):
                return (h <= avail and
                        ink_mask._sum(BOARD_X0, int(fold_y), 1310, int(h) + 40) < 2600)
            # fit ladder: full -> compact full on-page; a full board that cannot
            # fit OVERFLOWS to an appended back page (a second page is
            # always better than a cramped or clipped explanation). Trace-only boards still
            # degrade in place.
            chosen = None
            wt = str((ctx or {}).get("work_type") or "").lower()
            if v["mode"] == "full" and wt == "reading":
                # a reading route is 6-10 wrapped prose rows — it never fits well
                # under the writer's work; give it the full back page every time
                candidates = []
            else:
                candidates = [(v, False), (v, True)]
            for cand, compact in candidates:
                if room_ok(board.height(cand, compact)):
                    chosen = (cand, compact)
                    break
            if chosen is None and v["mode"] == "full":
                overflow = v
                fy = min(fold_y, 1985.0)
                sx, sy = px_to_scene(200, fy)
                from ems import naturalize as _nat
                _ptr = (f"full work: page {cont_page}" if cont_page
                        else "full work: back page")
                pp, _ = text_to_polylines(_ptr, sx, sy + 10, 30, max_width=900)
                for pts, ws in _nat(pp, base_w=18):
                    lines.append({"pts": pts, "_w": ws, "c": 6, "rgba": 0xFF0062CC})
                print("[rmreview]   board: overflowing full work to an appended back page")
            if chosen is None and overflow is None:
                print("[rmreview]   board: no clear room below student ink — dropped (glance layer only)")
            elif chosen is not None:
                cand, compact = chosen
                if cand["mode"] != v["mode"] or compact:
                    print(f"[rmreview]   board: fitted as {cand['mode']}"
                          f"{' compact' if compact else ''}")
                blines, _bmeta = board.render(cand, fold_y, compact)
                lines += blines
                board_bottom = fold_y + 40 + board.height(cand, compact)
                ink_mask.reserve(BOARD_X0, fold_y - 10, 1300, board_bottom - fold_y + 20)
    else:
        print("[rmreview]   no stroke file for this page — glance layer only")

    # 4b. the trap card: the bank's own pre-verified diagnosis of the letter
    # the writer committed — verbatim verified text, never model prose
    letter = str(result.get("committed_choice") or "").strip().upper()[:1]
    tmap = (ctx or {}).get("trap_map") or {}
    if (letter and letter in tmap
            and (result.get("verdict") == "wrong"
                 or (result.get("verdict") == "correct" and result.get("method_flawed")))):
        card = f"the trap you took ({letter}): {tmap[letter]}"
        from ems import measure as _measure
        _, nl = _measure(card, 28, 1280)
        cy0 = min(board_bottom + 12, 1900 - nl * 50)
        if not ink_mask.occupied(190, cy0 - 6, 1300, nl * 50 + 8):
            sx, sy = px_to_scene(200, cy0)
            cp, _ = text_to_polylines(card, sx, sy, 28, max_width=1280)
            for pts, ws in naturalize(cp, base_w=15):
                lines.append({"pts": pts, "_w": ws, "c": 7, "rgba": 0xFFD90707})
            ink_mask.reserve(190, cy0 - 6, 1300, nl * 50 + 8)
            board_bottom = max(board_bottom, cy0 + nl * 50)
        else:
            print("[rmreview]   trap card: no clear room — skipped")

    # 5. footer: Pace + Q + tick boxes — only ever on clean paper
    footer = result.get("footer") or {}
    q0 = footer.get("q") or {}
    fb = _footer_band(ink_mask, board_bottom, len(list(q0.get("boxes") or [])[:3]))
    if fb is None and footer:
        print("[rmreview]   footer: no clear space on this page — relocating to the back page")
        # the reader must know the notes moved: small pointer in any clear sliver
        _ptxt = f"notes: page {cont_page}" if cont_page else "notes: back page"
        _spot = ink_mask.find((1180, 300), 360, 46, max_dist=1600)
        if _spot is not None:
            from ems import naturalize as _nat2
            _sx, _sy = px_to_scene(*_spot)
            _pp, _ = text_to_polylines(_ptxt, _sx, _sy, 26, max_width=380)
            for _pts, _ws in _nat2(_pp, base_w=16):
                lines.append({"pts": _pts, "_w": _ws, "c": 6, "rgba": 0xFF0062CC})
        if overflow is None:
            overflow = {"mode": "none"}         # footer-only back page
        _ledger_append(ledger_entry)
        try:
            rdir = WORK / "receipts"
            rdir.mkdir(exist_ok=True)
            _atomic_write(rdir / f"{ledger_entry['ts']}-{(page_id or 'nopid')[:8]}.json",
                          json.dumps({"result": result, "ledger": ledger_entry,
                                      "n_events": len(events or []),
                                      "ctx_keys": sorted((ctx or {}).keys())}, indent=1))
        except Exception as e:
            print(f"[rmreview] WARN receipt failed: {e}")
        if lines:
            print(f"[rmreview] drawing {len(lines)} strokes: {draw_items(lines)}")
        return {"overflow": overflow, "footer": footer,
                "ledger_ts": ledger_entry["ts"]}
    rows, fscale = fb if fb else ([1908, 1964, 2020], 1.0)
    f_lines, f_boxes = _footer_lines(footer, rows, fscale)
    lines += f_lines
    ledger_entry["boxes"].update(f_boxes)

    _ledger_append(ledger_entry)
    try:
        rdir = WORK / "receipts"
        rdir.mkdir(exist_ok=True)
        _atomic_write(rdir / f"{ledger_entry['ts']}-{(page_id or 'nopid')[:8]}.json",
                      json.dumps({"result": result, "ledger": ledger_entry,
                                  "n_events": len(events or []),
                                  "ctx_keys": sorted((ctx or {}).keys())}, indent=1))
    except Exception as e:
        print(f"[rmreview] WARN receipt failed: {e}")
    if lines:
        print(f"[rmreview] drawing {len(lines)} strokes: {draw_items(lines)}")
    else:
        print("[rmreview] nothing to draw")
    if overflow is not None:
        return {"overflow": overflow}
    return None


BOARD_X0 = 180
CURRENT_RUN = [0]        # run-start timestamp; groups ledger entries per run


def refresh_readme() -> None:
    """After any run, regenerate the run summary if a `report` module is installed."""
    try:
        import report
    except ImportError:
        return
    try:
        p = report.write_report()
        if p:
            print(f"[rmreview] readme updated: {p}")
    except Exception as e:
        print(f"[rmreview] WARN readme generation failed: {e}")


def main() -> None:
    import fcntl
    lock_path = WORK / "rmreview.lock"
    lock_fd = open(lock_path, "w")
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        print("[rmreview] REFUSING to start: another daemon owns the tablet.")
        print("[rmreview] two pollers poison the shared broker FIFO - kill it first.")
        sys.exit(3)
    lock_fd.write(json.dumps({"pid": os.getpid(), "started": int(time.time())}))
    lock_fd.flush()
    main._lock = lock_fd          # hold for process life
    print("[rmreview] button-triggered reviewer starting")
    ping = mb("aiPing")
    print(f"[rmreview] bridge: {ping}")
    start_snapshot(None)         # one full-store safety copy per daemon boot
    last_req = mb("aiPoll")
    # a press made moments before a daemon (re)start must not be swallowed:
    # request strings end in Date.now() ms — honor anything fresher than 90s
    try:
        _ts = int(last_req.split()[-1]) / 1000.0
        if 0 < time.time() - _ts < 90:
            print(f"[rmreview] honoring press from {time.time() - _ts:.0f}s before startup")
            last_req = ""
    except (ValueError, IndexError):
        pass

    def capture_page(tag: str) -> tuple[Path, bool]:
        """Hide AI ink, screenshot to a tagged file, unhide, normalize zoom."""
        mb("aiVis", "0", wait=1)
        time.sleep(1.2)
        shot = screenshot(f"page-{tag}.png")
        mb("aiVis", "1", wait=1)
        return shot, normalize_shot(shot)

    def apply_review(shot: Path, result: dict, events: list[dict] | None = None,
                     ctx: dict | None = None, page_id: str = "",
                     cont_page: int | None = None):
        """Render + draw a review onto the CURRENT page. New-contract results go
        through apply_semantic (glance + board + footer); anything else falls
        back to the legacy renderer. The shot is ALWAYS canonical page space by
        now, so canon_to_scene applies and live zoom is irrelevant."""
        if any(k in result for k in ("trace", "footer", "headline")):
            return apply_semantic(shot, result, events or [], ctx, page_id, cont_page)
        from PIL import Image
        ink_mask = ClearSpace(Image.open(shot).convert("L"))
        view_scale = 1.0
        anns = result.get("annotations", [])
        if len(anns) > 6:
            kept, seen_lead = [], set()
            for a in anns:
                lead = (a.get("text") or "").split(":")[0] + ":" if a.get("kind") == "note" else ""
                if lead in ("Keep:", "Pace:", "Q:") and lead in seen_lead:
                    continue
                seen_lead.add(lead)
                kept.append(a)
                if len(kept) == 6:
                    break
            print(f"[rmreview] budget: kept 6 of {len(anns)} annotations")
            anns = kept
        notes = [a for a in anns if a.get("kind") == "note"]
        others = [a for a in anns if a.get("kind") != "note"]
        lines: list[dict] = []
        for a in notes:
            try:
                lines += render_annotation(a, ink_mask, view_scale)
            except (KeyError, TypeError, ValueError) as e:
                print(f"[rmreview]   skipped malformed annotation: {e}")
        for a in others:
            if a.get("kind") == "arrow" and "from_px" in a:
                fx, fy = a["from_px"]
                for nn in notes:
                    if "_placed_px" not in nn or "px" not in nn:
                        continue
                    ox, oy = nn["px"]
                    if abs(fx - ox) < 160 and abs(fy - oy) < 120:
                        px_, py_ = nn["_placed_px"]
                        a["from_px"] = [px_ - 6, py_ + 14]
                        break
            try:
                lines += render_annotation(a, ink_mask, view_scale)
            except (KeyError, TypeError, ValueError) as e:
                print(f"[rmreview]   skipped malformed annotation: {e}")
        lines += verdict_lines(result.get("verdict", "open"))
        if not lines and result.get("summary"):
            polys, _ = text_to_polylines(str(result["summary"])[:200], -700, 150,
                                         NOTE_SIZE, max_width=1300)
            lines = _colored([{"pts": pp} for pp in polys], "guide")
        if lines:
            print(f"[rmreview] drawing {len(lines)} strokes: {draw_items(lines)}")
        else:
            print("[rmreview] nothing to draw")

    _goto_offset: list[int] = [0]
    _goto_pending: list[int] = [0]     # a delta must repeat before it is believed

    def settled_idx() -> int:
        """Read the page index until two consecutive reads agree (it settles late)."""
        last = -9
        for _ in range(7):
            v = mb("aiPage", wait=1)
            cur = int(v) if v.isdigit() else -1
            if cur >= 0 and cur == last:
                return cur
            last = cur
            time.sleep(0.5)
        return last

    def goto_idx(target: int, avoid_pid: str) -> str:
        """Jump to an absolute page index; learns aiGoto's off-by-one on the fly."""
        for _ in range(4):
            mb("aiGoto", str(target - _goto_offset[0]), wait=1)
            time.sleep(1.6)
            idx = settled_idx()
            pid = current_page_id()
            if idx == target and pid and pid != avoid_pid:
                _goto_pending[0] = 0
                return pid
            if idx >= 0 and idx != target:
                d = idx - target
                # aiGoto's real error is a CONSTANT off-by-one. A single miss is
                # usually transient instead — appending a continuation page moves
                # the notebook end, so a jump aimed at a stale index lands short.
                # Absorbing those into a global offset made it oscillate
                # -1/0/-1/0/-3/0 and cost a page its ink. So: believe a delta only
                # when it repeats, and never one larger than an off-by-one can
                # explain.
                if d == _goto_pending[0] and abs(_goto_offset[0] + d) <= 2:
                    _goto_offset[0] += d
                    _goto_pending[0] = 0
                    print(f"[rmreview] nav: learned goto offset {_goto_offset[0]:+d}")
                else:
                    _goto_pending[0] = d
        return ""

    def draw_continuation(v: dict, label: str, last_idx: int,
                          src_page: int | None = None,
                          footer: dict | None = None):
        """Append a blank page at the notebook end (double aiFwd — the first
        highlights the new-page control, the second commits) and ink the FULL
        board there. Verified blank before any ink; failure leaves the work
        page's glance layer + pointer standing."""
        goto_idx(last_idx, avoid_pid="")
        if settled_idx() != last_idx:
            print("[rmreview]   continuation: cannot reach notebook end — skipped")
            return None
        mb("aiFwd", "", wait=1); time.sleep(1.5)
        mb("aiFwd", "", wait=1); time.sleep(2.5)
        idx = settled_idx()
        try:
            info = json.loads(mb("aiInfo", wait=1))
        except (json.JSONDecodeError, ValueError):
            info = {}
        new_pid = info.get("pageId", "")
        if idx != last_idx + 1 or info.get("layerCount", 9) != 1:
            print(f"[rmreview]   continuation: page-create verify failed "
                  f"(idx {idx}, layers {info.get('layerCount')}) — skipped")
            return None
        sc = screenshot("cont-verify.png")
        from PIL import Image
        import numpy as np
        arr = np.asarray(Image.open(sc).convert("L"))
        if int((arr[110:, 160:] < 100).sum()) > 400:
            print("[rmreview]   continuation: new page not blank — skipped")
            return None
        from ems import naturalize
        lines = []
        head = (f"page {src_page} - {label}" if src_page else f"full work - {label}")
        hp, _ = text_to_polylines(head[:46], -600, 86, 36, max_width=1400)
        for pts, ws in naturalize(hp, base_w=18):
            lines.append({"pts": pts, "_w": ws, "c": 6, "rgba": 0xFF0062CC})
        y_after = 240
        if v.get("mode") not in (None, "none"):
            blines, _m = board.render(v, 170)
            lines += blines
            y_after = 210 + board.height(v) + 60
        f_boxes: dict = {}
        if footer:
            rows_f = [int(y_after), int(y_after) + 56, int(y_after) + 100]
            f_lines, f_boxes = _footer_lines(footer, rows_f, 1.0)
            lines += f_lines
        print(f"[rmreview]   continuation: appended page {idx}: {draw_items(lines)}")
        return {"pid": new_pid, "boxes": f_boxes, "idx": idx}

    def run_batch(max_pages: int) -> None:
        """v2: capture all pages fast, grade them in parallel, then apply inks."""
        import concurrent.futures as cf
        CURRENT_RUN[0] = int(time.time())
        pages: list[tuple] = []   # (page_index, page_id, shot, warped, ctx, events, rm_sha)
        guard_file = WORK / "graded.json"
        try:
            graded = json.loads(guard_file.read_text()) if guard_file.exists() else {}
        except (json.JSONDecodeError, OSError):
            graded = {}
        print(f"[rmreview] batch: file-pull capture ({max_pages} pages max)")
        base = settled_idx()
        pid0 = current_page_id()
        if base < 0 or not pid0:
            print("[rmreview] batch: cannot read page — is a notebook open?")
            return
        store = "/home/root/.local/share/remarkable/xochitl"
        nb = rmssh(f"grep -l '{pid0}' {store}/*.content | head -n 1")
        if not nb:
            print("[rmreview] batch: could not locate notebook — aborting")
            return
        uuid = Path(nb).stem
        start_snapshot(uuid)     # slim safety snapshot in the background
        content_local = WORK / "nb.content"
        subprocess.run(["scp", "-q", "-o", "ControlPath=/tmp/rmgrade-ssh-%r@%h",
                        f"{RM}:{nb}", str(content_local)], check=True, timeout=40)
        cont = json.loads(content_local.read_text())
        cpages = cont.get("cPages", {}).get("pages", [])
        pdf_local = Path(__file__).parent / "cache" / f"{uuid}.pdf"
        if cont.get("fileType") == "pdf" and not pdf_local.exists():
            pdf_local.parent.mkdir(exist_ok=True)
            subprocess.run(["scp", "-q", "-o", "ControlPath=/tmp/rmgrade-ssh-%r@%h",
                            f"{RM}:{store}/{uuid}.pdf", str(pdf_local)], check=True, timeout=180)
        from rmrender import render_page
        idxs = list(range(base, min(base + max_pages, len(cpages))))

        def _pull(i2: int) -> bool:
            r = subprocess.run(["scp", "-q", "-o", "ControlPath=/tmp/rmgrade-ssh-%r@%h",
                                f"{RM}:{store}/{uuid}/{cpages[i2]['id']}.rm",
                                str(WORK / f"pg-{i2}.rm")],
                               capture_output=True, timeout=40)
            return r.returncode == 0

        with cf.ThreadPoolExecutor(max_workers=6) as pex:
            pulled = dict(zip(idxs, pex.map(_pull, idxs)))
        for i2 in idxs:
            entry = cpages[i2]
            pid = entry["id"]
            redir = entry.get("redir", {}).get("value")
            ctx = _page_context(pdf_local if pdf_local.exists() else None, redir)
            rm_local = WORK / f"pg-{i2}.rm"
            shot = WORK / f"page-b{i2}.png"
            if pulled.get(i2):
                _pdf = pdf_local if pdf_local.exists() else None
                import rmrender as _rr
                _tf = _rr.page_transform(_pdf, redir)
                events = strokes_mod.stroke_events(rm_local, _tf)
                if not events:
                    print(f"[rmreview] batch: page idx {i2} has no student ink — skipping")
                    continue
                rm_sha = readback.guard_sha(events, _latest_boxes(pid))
                if graded.get(pid) == rm_sha:
                    print(f"[rmreview] batch: page idx {i2} already graded, ink unchanged — skipping")
                    continue
                render_page(rm_local, _pdf, redir, shot)
                import rmrender as _rr
                INK_TF.update(_rr.LAST_TF)      # capture BEFORE the AI-layer render
                if _rr.LAST_CLIP.get("clipped"):
                    ctx = dict(ctx or {})
                    ctx["clipped"] = _rr.LAST_CLIP["clipped"]
                    print(f"[rmreview] batch: page idx {i2} is TRUNCATED — "
                          f"{ctx['clipped']} strokes off-page; grading partial work")
                try:
                    render_page(rm_local, _pdf, redir,
                                Path(str(shot).replace(".png", "-ai.png")),
                                only_layers=("AI Feedback",), background=False)
                except Exception:
                    pass
                pages.append((i2, pid, shot, False, ctx, events, rm_sha))
                print(f"[rmreview] batch: rendered {len(pages)} (page idx {i2}, {len(events)} ink events)")
            else:
                # no file on disk — ink may be unflushed. Visit + screenshot fallback.
                goto_idx(i2, avoid_pid="")
                if current_page_id() != pid:
                    print(f"[rmreview] batch: page idx {i2} unreachable — skipping")
                    continue
                mb("aiVis", "0", wait=1)
                time.sleep(1.0)
                sc = screenshot(f"page-b{i2}.png")
                mb("aiVis", "1", wait=1)
                warped = normalize_shot(sc)
                from PIL import Image
                import numpy as np
                arr = np.asarray(Image.open(sc).convert("L"))
                ink_px = int((arr[110:, 160:] < 100).sum())
                if ink_px < 300:
                    print(f"[rmreview] batch: page idx {i2} is blank ({ink_px} px) — skipping")
                    continue
                pages.append((i2, pid, sc, warped, ctx, [], ""))
                print(f"[rmreview] batch: screenshot fallback {len(pages)} (page idx {i2}, {ink_px} ink px)")
        if not pages:
            print("[rmreview] batch: nothing captured")
            return
        print(f"[rmreview] batch: grading {len(pages)} pages in parallel (6 workers), inking as they land")
        results: dict[int, dict] = {}

        appended = [0]

        def _apply_one(i: int) -> None:
            idx, pid, shot, _w, ctx_i, events_i, rm_sha = pages[i]
            if idx >= 0:
                goto_idx(idx, avoid_pid="")
            if current_page_id() != pid:
                goto_idx(idx, avoid_pid="")
            if current_page_id() != pid:
                print(f"[rmreview] batch: page {i+1} navigation mismatch — ink skipped")
                return
            cont_page = len(cpages) + appended[0] + 1
            res = apply_review(shot, results[i], events_i, ctx_i, pid, cont_page)
            if res and res.get("overflow"):
                c = ctx_i or {}
                label = c.get("label") or ""
                if c.get("day"):
                    label = f"Day {c['day']} - {label}".strip(" -")
                label = label or f"page {idx+1}"
                done = draw_continuation(res["overflow"], label,
                                         len(cpages) - 1 + appended[0], idx + 1,
                                         footer=res.get("footer"))
                if done:
                    appended[0] += 1
                    if done.get("boxes"):
                        _ledger_append({"ts": int(time.time()), "run": CURRENT_RUN[0],
                                        "page_id": done["pid"], "boxes": done["boxes"],
                                        "redir": None, "relocated_from": pid,
                                        "verdict": None})
            if rm_sha:
                graded[pid] = rm_sha
                _atomic_write(guard_file, json.dumps(graded, indent=1))

        with cf.ThreadPoolExecutor(max_workers=6) as ex:
            futs = {ex.submit(review_tiered, shot, warped, ctx, events): i
                    for i, (_, _, shot, warped, ctx, events, _s2) in enumerate(pages)}
            for fut in cf.as_completed(futs):
                i = futs[fut]
                try:
                    results[i] = fut.result()
                    tier = results[i].pop("_tier", "full")
                    print(f"[rmreview] batch: graded page {i+1} [{tier}]: {str(results[i].get('summary'))[:80]}")
                except Exception as e:
                    print(f"[rmreview] batch: page {i+1} grade FAILED: {e}")
                    continue
                if not str(results[i].get("summary", "")).startswith("review failed"):
                    _apply_one(i)
        failed = [i for i in range(len(pages))
                  if str(results.get(i, {}).get("summary", "")).startswith("review failed")
                  or i not in results]
        for i in failed:
            print(f"[rmreview] batch: retrying page {i+1} serially")
            r = review(pages[i][2], warped=pages[i][3], ctx=pages[i][4], events=pages[i][5])
            if not str(r.get("summary", "")).startswith("review failed"):
                results[i] = r
                print(f"[rmreview] batch: retry OK page {i+1}: {str(r.get('summary'))[:70]}")
            else:
                results.pop(i, None)
                print(f"[rmreview] batch: page {i+1} still failing — inking a failure note")
                try:
                    idx_f, pid_f = pages[i][0], pages[i][1]
                    goto_idx(idx_f, avoid_pid="")
                    if current_page_id() == pid_f:
                        np_, _ = text_to_polylines("review failed - press grade again",
                                                   -160, 120, 24, max_width=900)
                        draw_items(_colored([{"pts": p, "_w": 14} for p in np_], "error"))
                except Exception as e_f:
                    print(f"[rmreview] batch: failure note skipped: {e_f}")
        for i in list(failed):
            if i in results and not str(results[i].get("summary", "")).startswith("review failed"):
                _apply_one(i)
        _atomic_write(WORK / "batch-results.json",
                      json.dumps({str(i): r for i, r in results.items()}, indent=2))
        print(f"[rmreview] batch: DONE — {len(results)}/{len(pages)} pages inked")
        refresh_readme()

    def run_formfill():
        """Fill a practice-set after-action form page from the finished set.

        Renders the page the writer is on, and formfill refuses anything whose rule
        layout is not the after-action form — so a mis-tap writes nothing.
        """
        try:
            import formfill
        except ImportError:
            print("[rmreview] formfill: module not installed (exam-specific, not included)")
            return
        pid = current_page_id()
        assets = page_assets(pid) or {}
        redir = assets.get("redir")
        if assets.get("pdf") is None or redir is None:
            print("[rmreview] formfill: no PDF page under the cursor — nothing written")
            return
        from rmrender import render_page
        shot = WORK / "form-page.png"
        render_page(None, assets["pdf"], redir, shot)
        import rmrender as _rr
        INK_TF.update(_rr.LAST_TF)
        ink = formfill.build(shot)
        if not ink:
            return
        print(f"[rmreview] formfill: {len(ink)} strokes: {draw_items(ink)}")

    def run_eval():
        """Fill the on-screen day-log form from today's grading ledger."""
        pid = current_page_id()
        assets = page_assets(pid) or {}
        redir = assets.get("redir")
        shot = None
        if assets.get("pdf") is not None and redir is not None:
            from rmrender import render_page
            shot = WORK / "eval-page.png"
            render_page(None, assets["pdf"], redir, shot)
        if shot is None:
            shot, _ = capture_page("eval")
        # read the writer's ticks off every page graded today, before filling the form
        try:
            swept = 0
            seen_pids = set()
            store_p = "/home/root/.local/share/remarkable/xochitl"
            for e in reversed(ledger_rows()):
                epid = e.get("page_id")
                if (not e.get("boxes") or epid in seen_pids
                        or time.time() - e.get("ts", 0) > 20 * 3600):
                    continue
                seen_pids.add(epid)
                rm_t = WORK / "ticksweep.rm"
                r = subprocess.run(["scp", "-q", "-o", "ControlPath=/tmp/rmgrade-ssh-%r@%h",
                                    f"{RM}:{store_p}/{assets.get('uuid')}/{epid}.rm",
                                    str(rm_t)], capture_output=True, timeout=30)
                if r.returncode != 0:
                    continue
                png_t = WORK / "ticksweep.png"
                from rmrender import render_page as _rp
                erdir = e.get("redir")
                _rp(rm_t, assets.get("pdf") if erdir is not None else None, erdir, png_t)
                rb = readback.read_boxes(png_t, e)
                if rb.get("q_label") or rb.get("compliance_done"):
                    _ledger_append({"ts": int(time.time()), "run": CURRENT_RUN[0],
                                    "page_id": epid, "tick": True,
                                    "q_label": rb.get("q_label"),
                                    "q_multi": rb.get("q_multi"),
                                    "compliance_done": rb.get("compliance_done")})
                    swept += 1
            if swept:
                print(f"[rmreview] eval: read ticks from {swept} page(s)")
        except Exception as e:
            print(f"[rmreview] eval: tick sweep skipped: {e}")
        try:
            import evalfill
        except ImportError:
            print("[rmreview] eval: module not installed (exam-specific, not included)")
            return
        ink = evalfill.build(shot)
        if not ink:
            print("[rmreview] eval: nothing to fill (no ruled blanks found, or no grading today)")
            return
        print(f"[rmreview] eval: filling the day log: {draw_items(ink)}")
        # tomorrow's day page: pre-ink the three 5-minute-redo blanks
        try:
            day = (assets.get("ctx") or {}).get("day")
            here = settled_idx()
            t = evalfill.build_tomorrow(assets.get("pdf"), day) if day else None
            if t:
                redir_t, ink_t = t
                goto_idx(redir_t, avoid_pid="")
                if settled_idx() == redir_t:
                    print(f"[rmreview] eval: pre-inking tomorrow (page {redir_t + 1}): "
                          f"{draw_items(ink_t)}")
                if here >= 0:
                    goto_idx(here, avoid_pid="")
        except Exception as e:
            print(f"[rmreview] eval: tomorrow pre-ink skipped: {e}")

    def run_pass():
        CURRENT_RUN[0] = int(time.time())
        pid = current_page_id()
        assets = page_assets(pid) or {}
        start_snapshot(assets.get("uuid"))   # slim + async; draw waits on it
        ctx = assets.get("ctx")
        if ctx:
            print(f"[rmreview] ground truth: {ctx.get('kind')} day {ctx.get('day')} "
                  f"item {ctx.get('item_id')} answer {ctx.get('answer')!r}")
        # file-pull first: canonical render + stroke order, when the page is flushed
        events: list[dict] = []
        shot = None
        warped = False
        if assets.get("uuid") and pid:
            rm_local = WORK / "pg-single.rm"
            r = subprocess.run(["scp", "-q", "-o", "ControlPath=/tmp/rmgrade-ssh-%r@%h",
                                f"{RM}:{assets['store']}/{assets['uuid']}/{pid}.rm",
                                str(rm_local)], capture_output=True, timeout=40)
            if r.returncode == 0:
                import rmrender as _rr
                events = strokes_mod.stroke_events(
                    rm_local, _rr.page_transform(assets.get("pdf"), assets.get("redir")))
                if events:
                    from rmrender import render_page
                    shot = WORK / "page-single.png"
                    render_page(rm_local, assets.get("pdf"), assets.get("redir"), shot)
                    import rmrender as _rr
                    INK_TF.update(_rr.LAST_TF)   # capture BEFORE the AI-layer render
                    try:
                        render_page(rm_local, assets.get("pdf"), assets.get("redir"),
                                    WORK / "page-single-ai.png",
                                    only_layers=("AI Feedback",), background=False)
                    except Exception:
                        pass
                    print(f"[rmreview] file-pull capture ({len(events)} ink events)")
        if shot is None:
            shot, warped = capture_page("single")
            print("[rmreview] screenshot capture (page not flushed — no stroke order, glance layer only)")
        # guard: an unchanged, already-marked page must not be double-inked.
        # A cleared AI layer means the writer wants a fresh review; that passes through.
        sha = ""
        if events:
            sha = readback.guard_sha(events, _latest_boxes(pid))
            try:
                graded_s = json.loads((WORK / "graded.json").read_text())
            except (json.JSONDecodeError, OSError):
                graded_s = {}
            if graded_s.get(pid) == sha:
                try:
                    has_ai = "AI Feedback" in json.loads(mb("aiInfo", wait=1)).get("layerNames", [])
                except (json.JSONDecodeError, ValueError):
                    has_ai = True
                if has_ai:
                    print("[rmreview] page unchanged and already marked — skipping "
                          "(clear its AI Feedback layer to force a fresh review)")
                    return None
        result = review(shot, warped=warped, ctx=ctx, events=events)
        print(f"[rmreview] summary: {result.get('summary')}")
        print(f"[rmreview] cost: ${result.get('cost_usd', 0) or 0:.3f}"
              f" | headline: {result.get('headline')!r}")
        try:
            n_pages = len(json.loads((WORK / "nb.content").read_text())
                          .get("cPages", {}).get("pages", []))
        except (OSError, json.JSONDecodeError):
            n_pages = 0
        res = apply_review(shot, result, events, ctx, pid,
                           (n_pages + 1) if n_pages else None)
        if sha and not str(result.get("summary", "")).startswith("review failed"):
            try:
                graded_s = json.loads((WORK / "graded.json").read_text())
            except (json.JSONDecodeError, OSError):
                graded_s = {}
            graded_s[pid] = sha
            _atomic_write(WORK / "graded.json", json.dumps(graded_s, indent=1))
        if res and res.get("overflow") and n_pages:
            c = ctx or {}
            label = c.get("label") or ""
            if c.get("day"):
                label = f"Day {c['day']} - {label}".strip(" -")
            src_idx = None
            try:
                pages_l = json.loads((WORK / "nb.content").read_text())["cPages"]["pages"]
                src_idx = next(i for i, p in enumerate(pages_l) if p.get("id") == pid) + 1
            except (StopIteration, OSError, json.JSONDecodeError, KeyError):
                pass
            done = draw_continuation(res["overflow"], label or "this problem",
                                     n_pages - 1, src_idx, footer=res.get("footer"))
            if done and done.get("boxes"):
                _ledger_append({"ts": int(time.time()), "run": CURRENT_RUN[0],
                                "page_id": done["pid"], "boxes": done["boxes"],
                                "redir": None, "relocated_from": pid,
                                "verdict": None})
        _atomic_write(WORK / "last-review.json", json.dumps(result, indent=2))
        refresh_readme()
        return result

    def current_page_id() -> str:
        try:
            return json.loads(mb("aiInfo", wait=1)).get("pageId", "")
        except (json.JSONDecodeError, ValueError):
            return ""

    while True:
        time.sleep(1.0)
        try:
            req = mb("aiPoll", wait=2)
        except Exception as e:
            print(f"[rmreview] poll error ({e}); retrying in 10s")
            time.sleep(10)
            continue
        if not req or req in ("none", "") or req == last_req or req.startswith("ERR"):
            continue
        last_req = req
        if req.startswith("all"):
            max_pages = 30
        elif req.startswith("n:"):
            try:
                max_pages = max(1, min(30, int(req.split()[0][2:])))
            except ValueError:
                max_pages = 1
        else:
            max_pages = 1

        if req.startswith("form"):
            print("[rmreview] AFTER-ACTION pressed — filling the form page")
            run_formfill()
        elif req.startswith("eval"):
            print("[rmreview] EVALUATE DAY pressed — filling the log form")
            run_eval()
        elif max_pages == 1:
            print("[rmreview] GRADE button pressed — reviewing page")
            run_pass()
        elif True:
            print(f"[rmreview] BATCH v2: {max_pages} pages")
            run_batch(max_pages)
        else:
            print(f"[rmreview] GRADE {max_pages} pages — walking from the current page")
            seen: set[str] = set()
            for n in range(1, max_pages + 1):
                pid = current_page_id()
                if not pid or pid in seen:
                    print(f"[rmreview] grade-all: stopping ({'no scene' if not pid else 'page repeat'}) after {n-1} pages")
                    break
                seen.add(pid)
                print(f"[rmreview] grade-all: page {n} ({pid[:8]})")
                run_pass()
                if n == max_pages:
                    print(f"[rmreview] grade-all: reached requested count ({max_pages})")
                    break
                r = mb("aiNext")
                print(f"[rmreview] grade-all: flip -> {r}")
                if r.startswith("ERR"):
                    break
                time.sleep(2.5)          # page load + render settle
                if current_page_id() == pid:
                    print(f"[rmreview] grade-all: page did not change — last page reached, {n} graded")
                    break
            print("[rmreview] grade-all: done")

        # absorb any button presses made while this pass was running
        settle = mb("aiPoll")
        if settle and not settle.startswith("ERR"):
            last_req = settle


def doctor() -> int:
    fails = 0

    def chk(name, ok, note=""):
        nonlocal fails
        print(f"  {'OK  ' if ok else 'FAIL'} {name}" + (f" - {note}" if note else ""))
        if not ok:
            fails += 1

    print("[rmreview doctor]")
    import fcntl
    lock_path = WORK / "rmreview.lock"
    daemon_alive = False
    try:
        fd = open(lock_path, "a+")
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            fcntl.flock(fd, fcntl.LOCK_UN)
            chk("daemon", True, "not running (lock free)")
        except BlockingIOError:
            daemon_alive = True
            chk("daemon", True, "running (lock held)")
        fd.close()
    except OSError as e:
        chk("lock", False, str(e))
    for mod in ("board", "strokes", "mathcheck", "readback", "rmrender", "ems"):
        try:
            __import__(mod)
            chk(f"import {mod}", True)
        except Exception as e:
            chk(f"import {mod}", False, str(e)[:60])
    import shutil
    chk(f"model CLI ({CLAUDE_BIN})", shutil.which(CLAUDE_BIN) is not None)
    chk("question-bank adapter", True, "loaded" if _bank else "none (grading from handwriting only)")
    try:
        ledger_rows()
        _latest_boxes("smoke")
        _cap_seconds("CAP 0:30")
        import readback as _rb
        _rb.guard_sha([], None)
        chk("daemon plumbing (ledger/guard/readback)", True)
    except Exception as e:
        chk("daemon plumbing (ledger/guard/readback)", False, str(e)[:70])
    try:
        n = len([1 for _l in open(LEDGER) if _l.strip()])
        chk("ledger parse", True, f"{n} rows")
    except OSError:
        chk("ledger parse", True, "no ledger yet")
    if not daemon_alive:
        pong = mb("aiPing", wait=2)
        chk("tablet bridge", pong == "pong", pong[:30])
    else:
        print("  SKIP tablet bridge (daemon owns the broker)")
    print(f"[rmreview doctor] {'ALL GREEN' if fails == 0 else str(fails) + ' FAILURES'}")
    return fails


if __name__ == "__main__":
    if "doctor" in sys.argv:
        sys.exit(doctor())
    try:
        main()
    except KeyboardInterrupt:
        print("\n[rmreview] stopped")
        sys.exit(0)
