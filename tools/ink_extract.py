"""Extract the curated MathWriting samples into EMS glyph space -> human_glyphs.json.

Font space: 1000 units/em, y-UP, baseline y=0. Each glyph is (advance, [stroke,...]).
Targets are set against the shipped font's own operator metrics so the new glyphs
sit on the same optical band as = < > + -.
"""
import os, json, math
from ink_harvest import load_class

# label -> chosen sample + target box in font units (y-UP, baseline 0).
#   h  = target height, w = max width (aspect preserved; whichever binds wins)
#   y0 = where the glyph's BOTTOM sits above the baseline
#   adv: None = derive from real ink width + side bearing. A number = forced,
#        used only for the radical, whose overbar must OVERHANG the radicand.
# Boxes are set against the shipped font's own metrics: '<' is 419 wide x 463
# tall, '=' sits y167..422, '7' is 403 wide — so these land on the same band.
PICKS = {
    "\\sqrt":       {"i": 55, "ch": "√", "h": 800, "w": 9e9, "y0": 0,   "adv": 300, "x0": 70},
    # le[14] and ge[9] are both aspect 0.72 — picked so the pair renders symmetric.
    "\\le":         {"i": 14, "ch": "≤", "h": 560, "w": 440, "y0": 30,  "adv": None, "x0": 75},
    "\\ge":         {"i": 9,  "ch": "≥", "h": 560, "w": 440, "y0": 30,  "adv": None, "x0": 75},
    "\\rightarrow": {"i": 8,  "ch": "→", "h": 240, "w": 560, "y0": 175, "adv": None, "x0": 70},
    # ne[12] is aspect 0.93 -> lands on 381 wide, exactly the shipped '=' width.
    "\\ne":         {"i": 12, "ch": "≠", "h": 410, "w": 420, "y0": 90,  "adv": None, "x0": 70},
}
SIDE_BEARING = 48.0


def decimate(stroke, tol=6.0):
    """Ramer-Douglas-Peucker — the raw ink is far denser than the font needs."""
    if len(stroke) < 3:
        return stroke
    a, b = stroke[0], stroke[-1]
    dx, dy = b[0] - a[0], b[1] - a[1]
    n = math.hypot(dx, dy)
    worst, wi = -1.0, 0
    for i in range(1, len(stroke) - 1):
        p = stroke[i]
        if n < 1e-9:
            d = math.hypot(p[0] - a[0], p[1] - a[1])
        else:
            d = abs(dy * p[0] - dx * p[1] + b[0] * a[1] - b[1] * a[0]) / n
        if d > worst:
            worst, wi = d, i
    if worst <= tol:
        return [a, b]
    return decimate(stroke[:wi + 1], tol)[:-1] + decimate(stroke[wi:], tol)


out = {}
for label, cfg in PICKS.items():
    cands = load_class(label)
    src = cands[cfg["i"]]["strokes"]
    pts = [p for s in src for p in s]
    xs = [p[0] for p in pts]; ys = [p[1] for p in pts]
    x_lo, x_hi, y_lo, y_hi = min(xs), max(xs), min(ys), max(ys)
    h = max(y_hi - y_lo, 1e-6)
    w_raw = max(x_hi - x_lo, 1e-6)
    k = min(cfg["h"] / h, cfg["w"] / w_raw)      # fit the box, preserve aspect
    strokes = []
    for s in src:
        # InkML y grows DOWN -> negate into font's y-UP
        q = [[(p[0] - x_lo) * k + cfg["x0"], (y_hi - p[1]) * k + cfg["y0"]] for p in s]
        q = decimate(q, tol=2.0)
        if len(q) >= 2:
            strokes.append([[round(a, 1), round(b, 1)] for a, b in q])
    w, hh = w_raw * k, h * k
    adv = cfg["adv"] if cfg["adv"] else round(cfg["x0"] + w + SIDE_BEARING)
    out[cfg["ch"]] = {"adv": adv, "strokes": strokes,
                      "src": f"MathWriting symbols/{cands[cfg['i']]['file']} ({label})"}
    over = " (overbar overhangs by %d - intentional)" % (cfg["x0"] + w - adv) if cfg["adv"] else ""
    print(f"{cfg['ch']}  U+{ord(cfg['ch']):04X}  {len(strokes)} strokes, "
          f"{sum(len(s) for s in strokes):3d} pts, ink {w:.0f}x{hh:.0f}, "
          f"x {cfg['x0']:.0f}..{cfg['x0']+w:.0f}, y {cfg['y0']}..{cfg['y0']+hh:.0f}, "
          f"adv {adv}{over}")

path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "human_glyphs.json")
json.dump({
    "_readme": ("Real human pen strokes for glyphs EMS Casual Hand lacks. "
                "Harvested from Google MathWriting-2024 symbols/ split "
                "(https://arxiv.org/abs/2404.10690). Data is CC BY-NC-SA 4.0 "
                "(Google LLC) — personal use only, do NOT redistribute this file. "
                "Kept out of EMSCasualHand.svg on purpose: that font is SIL OFL and "
                "must stay unmixed with NC-licensed data. "
                "Font space: 1000 units/em, y-UP, baseline y=0."),
    "glyphs": out,
}, open(path, "w"), indent=1)
print("\nwrote", path)
